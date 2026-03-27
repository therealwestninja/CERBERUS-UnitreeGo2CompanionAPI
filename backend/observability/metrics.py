"""
go2_platform/backend/observability/metrics.py
══════════════════════════════════════════════════════════════════════════════
Observability System — Metrics, Health Checks, Distributed Tracing, Logging

Provides:
  MetricsRegistry  — counters, gauges, histograms (Prometheus-compatible)
  HealthChecker    — per-subsystem health probes with status aggregation
  TraceContext     — lightweight request/event tracing with span IDs
  StructuredLogger — JSON-structured logging with context fields

Output formats:
  /api/v1/metrics          → Prometheus text exposition format
  /api/v1/health           → JSON health aggregation (K8s-compatible)
  /api/v1/health/ready     → Readiness probe
  /api/v1/health/live      → Liveness probe

Design: zero-dependency, no Prometheus client library needed.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Dict, List, Optional

log = logging.getLogger('go2.observability')


# ════════════════════════════════════════════════════════════════════════════
# METRICS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MetricSample:
    value:  float
    labels: Dict[str, str] = field(default_factory=dict)
    ts:     float = field(default_factory=time.time)


class Counter:
    """Monotonically increasing counter (resets on restart)."""
    def __init__(self, name: str, help_text: str = '', labels: tuple = ()):
        self.name = name
        self.help = help_text
        self._values: Dict[str, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **label_values) -> None:
        key = self._label_key(label_values)
        self._values[key] += amount

    def value(self, **label_values) -> float:
        return self._values[self._label_key(label_values)]

    def _label_key(self, lv: dict) -> str:
        return ','.join(f'{k}={v}' for k, v in sorted(lv.items()))

    def prometheus_lines(self) -> List[str]:
        lines = [f'# HELP {self.name} {self.help}',
                 f'# TYPE {self.name} counter']
        for key, val in self._values.items():
            labels = f'{{{key}}}' if key else ''
            lines.append(f'{self.name}{labels} {val}')
        return lines


class Gauge:
    """Arbitrarily settable numeric value."""
    def __init__(self, name: str, help_text: str = ''):
        self.name = name
        self.help = help_text
        self._values: Dict[str, float] = defaultdict(float)

    def set(self, value: float, **label_values) -> None:
        self._values[self._label_key(label_values)] = value

    def inc(self, delta: float = 1.0, **label_values) -> None:
        self._values[self._label_key(label_values)] += delta

    def dec(self, delta: float = 1.0, **label_values) -> None:
        self._values[self._label_key(label_values)] -= delta

    def value(self, **label_values) -> float:
        return self._values.get(self._label_key(label_values), 0.0)

    def _label_key(self, lv: dict) -> str:
        return ','.join(f'{k}={v}' for k, v in sorted(lv.items()))

    def prometheus_lines(self) -> List[str]:
        lines = [f'# HELP {self.name} {self.help}',
                 f'# TYPE {self.name} gauge']
        for key, val in self._values.items():
            labels = f'{{{key}}}' if key else ''
            lines.append(f'{self.name}{labels} {val:.4f}')
        return lines


class Histogram:
    """
    Samples distribution with configurable buckets.
    Useful for latency, force values, etc.
    """
    DEFAULT_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

    def __init__(self, name: str, help_text: str = '',
                 buckets: Optional[List[float]] = None):
        self.name    = name
        self.help    = help_text
        self.buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        self._sum:    float = 0.0
        self._count:  int   = 0
        self._bucket_counts: Dict[float, int] = {b: 0 for b in self.buckets}
        self._samples: deque = deque(maxlen=100)  # recent samples

    def observe(self, value: float) -> None:
        self._sum   += value
        self._count += 1
        for b in self.buckets:
            if value <= b:
                self._bucket_counts[b] += 1
        self._samples.append(value)

    @property
    def mean(self) -> float:
        return self._sum / max(self._count, 1)

    @property
    def p95(self) -> float:
        if not self._samples: return 0.0
        s = sorted(self._samples)
        return s[int(len(s) * 0.95)]

    @property
    def p99(self) -> float:
        if not self._samples: return 0.0
        s = sorted(self._samples)
        return s[int(len(s) * 0.99)]

    def prometheus_lines(self) -> List[str]:
        lines = [f'# HELP {self.name} {self.help}',
                 f'# TYPE {self.name} histogram']
        for b, cnt in self._bucket_counts.items():
            lines.append(f'{self.name}_bucket{{le="{b}"}} {cnt}')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self._count}')
        lines.append(f'{self.name}_sum {self._sum:.6f}')
        lines.append(f'{self.name}_count {self._count}')
        return lines


class MetricsRegistry:
    """
    Central registry for all platform metrics.
    Generates Prometheus exposition format text.
    """

    def __init__(self):
        self._counters:   Dict[str, Counter]   = {}
        self._gauges:     Dict[str, Gauge]     = {}
        self._histograms: Dict[str, Histogram] = {}
        self._start_time = time.time()
        self._register_platform_metrics()

    def counter(self, name: str, help_text: str = '') -> Counter:
        if name not in self._counters:
            self._counters[name] = Counter(name, help_text)
        return self._counters[name]

    def gauge(self, name: str, help_text: str = '') -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, help_text)
        return self._gauges[name]

    def histogram(self, name: str, help_text: str = '',
                  buckets: Optional[List[float]] = None) -> Histogram:
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, help_text, buckets)
        return self._histograms[name]

    def _register_platform_metrics(self):
        """Pre-register all Go2 Platform metrics."""
        # Commands
        self.counter('go2_commands_total', 'Total commands received')
        self.counter('go2_commands_failed', 'Commands rejected by safety/validation')
        self.counter('go2_estop_total', 'E-STOP triggers')
        self.counter('go2_safety_trips_total', 'Safety threshold trips')
        # State
        self.gauge('go2_battery_pct', 'Battery charge percentage')
        self.gauge('go2_pitch_deg', 'Robot pitch in degrees')
        self.gauge('go2_roll_deg', 'Robot roll in degrees')
        self.gauge('go2_contact_force_n', 'Contact force in Newtons')
        self.gauge('go2_armed', '1 if armed, 0 if not')
        self.gauge('go2_ws_clients', 'Active WebSocket clients')
        # Performance
        self.histogram('go2_command_latency_s', 'Command end-to-end latency',
                       buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5])
        self.histogram('go2_safety_eval_s', 'Safety evaluation duration',
                       buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01])
        # Uptime
        self.gauge('go2_uptime_s', 'Platform uptime in seconds')
        # Behaviors
        self.counter('go2_behaviors_total', 'Behaviors executed')
        self.counter('go2_behaviors_complete', 'Behaviors completed successfully')
        # WS
        self.counter('go2_ws_connections_total', 'WebSocket connections')
        self.counter('go2_ws_messages_total', 'WebSocket messages processed')
        # Animation
        self.counter('go2_animations_played', 'Animation clips played')
        # BT
        self.counter('go2_bt_ticks_total', 'Behavior tree ticks')

    def prometheus_text(self) -> str:
        """Generate Prometheus text exposition format."""
        lines = [
            '# Go2 Platform Metrics',
            f'# Generated: {time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}',
            '',
        ]
        # Update uptime gauge
        self.gauge('go2_uptime_s').set(time.time() - self._start_time)

        for m in list(self._counters.values()):
            lines.extend(m.prometheus_lines())
            lines.append('')
        for m in list(self._gauges.values()):
            lines.extend(m.prometheus_lines())
            lines.append('')
        for m in list(self._histograms.values()):
            lines.extend(m.prometheus_lines())
            lines.append('')
        return '\n'.join(lines)

    def to_dict(self) -> dict:
        """JSON-friendly summary."""
        return {
            'uptime_s':   round(time.time() - self._start_time, 1),
            'commands':   self._counters.get('go2_commands_total', Counter('')).value(),
            'estops':     self._counters.get('go2_estop_total', Counter('')).value(),
            'safety_trips': self._counters.get('go2_safety_trips_total', Counter('')).value(),
            'battery_pct':  self._gauges.get('go2_battery_pct', Gauge('')).value(),
            'armed':        bool(self._gauges.get('go2_armed', Gauge('')).value()),
            'ws_clients':   int(self._gauges.get('go2_ws_clients', Gauge('')).value()),
            'cmd_p95_ms':   round(self._histograms.get(
                'go2_command_latency_s', Histogram('')).p95 * 1000, 2),
        }


# ════════════════════════════════════════════════════════════════════════════
# HEALTH CHECKS
# ════════════════════════════════════════════════════════════════════════════

class HealthStatus(Enum):
    OK       = 'ok'
    DEGRADED = 'degraded'
    DOWN     = 'down'


@dataclass
class HealthResult:
    name:     str
    status:   HealthStatus
    message:  str = ''
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'status': self.status.value,
            'message': self.message,
            'latency_ms': round(self.latency_ms, 2),
            'metadata': self.metadata,
        }


class HealthChecker:
    """
    Aggregates health checks from all platform subsystems.
    Kubernetes-compatible: /health/live (liveness) and /health/ready (readiness).
    """

    def __init__(self):
        self._checks: Dict[str, Callable] = {}
        self._cache:  Dict[str, HealthResult] = {}
        self._cache_ttl: float = 5.0   # seconds

    def register(self, name: str, check_fn: Callable):
        """Register a health check function (sync or async)."""
        self._checks[name] = check_fn

    async def run_all(self) -> List[HealthResult]:
        results = []
        for name, fn in self._checks.items():
            t0 = time.monotonic()
            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await asyncio.wait_for(fn(), timeout=3.0)
                else:
                    result = fn()
                latency = (time.monotonic() - t0) * 1000
                if isinstance(result, HealthResult):
                    result.latency_ms = latency
                    results.append(result)
                elif result is True:
                    results.append(HealthResult(name, HealthStatus.OK,
                                                latency_ms=latency))
                else:
                    results.append(HealthResult(name, HealthStatus.DEGRADED,
                                                str(result), latency_ms=latency))
            except asyncio.TimeoutError:
                results.append(HealthResult(name, HealthStatus.DOWN,
                                            'check timed out'))
            except Exception as e:
                results.append(HealthResult(name, HealthStatus.DOWN, str(e)))
        return results

    async def aggregate(self) -> dict:
        results = await self.run_all()
        statuses = [r.status for r in results]
        overall = (HealthStatus.DOWN if HealthStatus.DOWN in statuses
                   else HealthStatus.DEGRADED if HealthStatus.DEGRADED in statuses
                   else HealthStatus.OK)
        return {
            'status': overall.value,
            'checks': [r.to_dict() for r in results],
            'ts': time.time(),
        }

    def register_platform_checks(self, platform=None):
        """Register standard Go2 Platform health checks."""

        def check_safety():
            if platform is None: return True
            lvl = platform.safety.level.value
            if lvl == 'estop': return HealthResult('safety', HealthStatus.DOWN, 'E-STOP active')
            if lvl == 'critical': return HealthResult('safety', HealthStatus.DEGRADED, 'Safety critical')
            return HealthResult('safety', HealthStatus.OK, f'level={lvl}')

        def check_battery():
            if platform is None: return True
            pct = platform.telemetry.battery_pct
            if pct < 10: return HealthResult('battery', HealthStatus.DOWN, f'{pct:.0f}%')
            if pct < 25: return HealthResult('battery', HealthStatus.DEGRADED, f'{pct:.0f}%')
            return HealthResult('battery', HealthStatus.OK, f'{pct:.0f}%')

        def check_telemetry():
            if platform is None: return True
            age = time.monotonic() - platform.telemetry.ts
            if age > 5.0: return HealthResult('telemetry', HealthStatus.DOWN,
                                               f'stale {age:.1f}s')
            if age > 2.0: return HealthResult('telemetry', HealthStatus.DEGRADED,
                                               f'delayed {age:.1f}s')
            return HealthResult('telemetry', HealthStatus.OK)

        def check_memory():
            try:
                import resource
                mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                if mem_mb > 2000: return HealthResult('memory', HealthStatus.DEGRADED,
                                                       f'{mem_mb:.0f}MB')
                return HealthResult('memory', HealthStatus.OK, f'{mem_mb:.0f}MB')
            except Exception:
                return HealthResult('memory', HealthStatus.OK, 'unavailable')

        self.register('safety', check_safety)
        self.register('battery', check_battery)
        self.register('telemetry', check_telemetry)
        self.register('memory', check_memory)


# ════════════════════════════════════════════════════════════════════════════
# TRACING
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Span:
    """A single unit of work in a distributed trace."""
    trace_id:   str
    span_id:    str
    name:       str
    start_time: float = field(default_factory=time.monotonic)
    end_time:   Optional[float] = None
    parent_id:  Optional[str]   = None
    tags:       Dict[str, str]  = field(default_factory=dict)
    status:     str = 'ok'      # ok | error

    def finish(self, status: str = 'ok'):
        self.end_time = time.monotonic()
        self.status   = status

    @property
    def duration_ms(self) -> float:
        end = self.end_time or time.monotonic()
        return (end - self.start_time) * 1000

    def to_dict(self) -> dict:
        return {
            'trace_id': self.trace_id,
            'span_id':  self.span_id,
            'parent':   self.parent_id,
            'name':     self.name,
            'duration_ms': round(self.duration_ms, 3),
            'status':   self.status,
            'tags':     self.tags,
        }


class Tracer:
    """
    Lightweight request tracer.
    Compatible with OpenTelemetry trace format (subset).
    """

    def __init__(self, max_traces: int = 200):
        self._traces: deque = deque(maxlen=max_traces)
        self._active: Dict[str, Span] = {}

    @asynccontextmanager
    async def span(self, name: str, trace_id: Optional[str] = None,
                   parent_id: Optional[str] = None, **tags):
        """Async context manager for creating spans."""
        tid = trace_id or str(uuid.uuid4())[:16]
        sid = str(uuid.uuid4())[:8]
        s = Span(trace_id=tid, span_id=sid, name=name,
                 parent_id=parent_id, tags=tags)
        self._active[sid] = s
        try:
            yield s
            s.finish('ok')
        except Exception as e:
            s.finish('error')
            s.tags['error'] = str(e)
            raise
        finally:
            self._active.pop(sid, None)
            self._traces.append(s)

    def recent_traces(self, n: int = 20) -> List[dict]:
        return [s.to_dict() for s in list(self._traces)[-n:]]

    def active_spans(self) -> List[dict]:
        return [s.to_dict() for s in self._active.values()]


# ════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING
# ════════════════════════════════════════════════════════════════════════════

class StructuredLogger:
    """
    JSON-structured logger with contextual fields.
    Output: {"ts": "...", "level": "INFO", "logger": "go2.api",
             "msg": "...", "robot_id": "go2_01", ...}
    """

    def __init__(self, context: Dict[str, Any] = None):
        self._ctx = context or {}

    def with_context(self, **kwargs) -> 'StructuredLogger':
        return StructuredLogger({**self._ctx, **kwargs})

    def _emit(self, level: str, msg: str, **extra):
        record = {
            'ts':    time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()),
            'level': level,
            'msg':   msg,
            **self._ctx,
            **extra,
        }
        print(json.dumps(record, default=str), file=sys.stderr)

    def debug(self, msg: str, **kw):
        if log.isEnabledFor(logging.DEBUG): self._emit('DEBUG', msg, **kw)

    def info(self, msg: str, **kw):  self._emit('INFO',  msg, **kw)
    def warn(self, msg: str, **kw):  self._emit('WARN',  msg, **kw)
    def error(self, msg: str, **kw): self._emit('ERROR', msg, **kw)


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI OBSERVABILITY ROUTES
# ════════════════════════════════════════════════════════════════════════════

def register_observability_routes(app, metrics: MetricsRegistry,
                                    health: HealthChecker, tracer: Tracer):
    """Attach observability endpoints to FastAPI app."""
    from fastapi.responses import PlainTextResponse

    @app.get('/api/v1/metrics', tags=['observability'],
             response_class=PlainTextResponse)
    async def get_metrics():
        """Prometheus text exposition format metrics."""
        return PlainTextResponse(
            metrics.prometheus_text(),
            media_type='text/plain; version=0.0.4; charset=utf-8',
        )

    @app.get('/api/v1/metrics/json', tags=['observability'])
    async def get_metrics_json():
        """Metrics in JSON format."""
        return metrics.to_dict()

    @app.get('/api/v1/health', tags=['observability'])
    async def health_check():
        """Aggregated health check (all subsystems)."""
        result = await health.aggregate()
        status_code = 200 if result['status'] == 'ok' else 503
        from fastapi.responses import JSONResponse
        return JSONResponse(result, status_code=status_code)

    @app.get('/api/v1/health/live', tags=['observability'])
    async def liveness():
        """Kubernetes liveness probe — is the server running?"""
        return {'status': 'ok', 'ts': time.time()}

    @app.get('/api/v1/health/ready', tags=['observability'])
    async def readiness():
        """Kubernetes readiness probe — is the server ready to handle traffic?"""
        result = await health.aggregate()
        from fastapi.responses import JSONResponse
        code = 200 if result['status'] != 'down' else 503
        return JSONResponse({'ready': code == 200, **result}, status_code=code)

    @app.get('/api/v1/traces', tags=['observability'])
    async def get_traces(n: int = 20):
        return {'traces': tracer.recent_traces(min(n, 100)),
                'active': tracer.active_spans()}
