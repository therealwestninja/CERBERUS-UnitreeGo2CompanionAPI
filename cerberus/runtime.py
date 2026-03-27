"""
cerberus/runtime.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Runtime Engine
Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

Tick-based priority scheduler:
  Priority 0 — SAFETY   (hard real-time reflex, <1ms)
  Priority 1 — CONTROL  (joint/motion control, 500Hz hardware)
  Priority 2 — COGNITION (behavior tree, goal engine, 10Hz)
  Priority 3 — ANIMATION (animation player, 50Hz)
  Priority 4 — LEARNING  (model updates, 1Hz)
  Priority 5 — UI/TELEMETRY (WebSocket push, 5Hz)

Architecture:
  CerberusRuntime
   ├── TickScheduler     — deterministic multi-rate execution
   ├── SystemEventBus    — typed, prioritized pub/sub
   ├── SubsystemRegistry — lifecycle-managed components
   └── WatchdogMonitor   — health + automatic recovery

This engine coordinates all CERBERUS subsystems in a single coherent
execution loop while maintaining hard real-time guarantees for safety-
critical paths and soft real-time for deliberative systems.
"""

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger('cerberus.runtime')


# ════════════════════════════════════════════════════════════════════════════
# TICK PRIORITY LEVELS
# ════════════════════════════════════════════════════════════════════════════

class Priority(IntEnum):
    SAFETY    = 0   # E-STOP, reflex layer — never skipped
    CONTROL   = 1   # Joint torque, motion controller
    COGNITION = 2   # BT, goal engine, world model
    ANIMATION = 3   # Animation player, blending
    LEARNING  = 4   # Model updates, preference learning
    TELEMETRY = 5   # WS push, logging, UI updates


# Target tick rates per priority (Hz)
TARGET_HZ: Dict[Priority, float] = {
    Priority.SAFETY:    1000.0,   # 1ms max latency
    Priority.CONTROL:    500.0,   # 2ms
    Priority.COGNITION:   10.0,   # 100ms — deliberative
    Priority.ANIMATION:   50.0,   # 20ms — smooth playback
    Priority.LEARNING:     1.0,   # 1s — batch updates
    Priority.TELEMETRY:    5.0,   # 200ms — WS push
}


# ════════════════════════════════════════════════════════════════════════════
# TICK CONTEXT — passed to every subsystem each tick
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TickContext:
    """Snapshot of runtime state passed to subsystems each tick."""
    tick_id:     int
    priority:    Priority
    dt_s:        float             # time since last tick at this priority
    wall_time:   float             # monotonic wall time
    runtime:     'CerberusRuntime'  # back-reference for cross-subsystem calls
    overrun:     bool = False       # True if we missed our deadline


# ════════════════════════════════════════════════════════════════════════════
# SUBSYSTEM BASE
# ════════════════════════════════════════════════════════════════════════════

class Subsystem:
    """
    Base class for all CERBERUS subsystems.
    Registered with SubsystemRegistry and ticked by TickScheduler.
    """

    name:     str = 'unnamed'
    priority: Priority = Priority.COGNITION
    enabled:  bool = True

    async def on_start(self, ctx: 'CerberusRuntime'): pass
    async def on_tick(self, ctx: TickContext): pass
    async def on_stop(self): pass
    async def on_fault(self, error: Exception): pass

    def status(self) -> dict:
        return {'name': self.name, 'priority': self.priority.name, 'enabled': self.enabled}


# ════════════════════════════════════════════════════════════════════════════
# TICK SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TickStats:
    """Per-priority tick statistics."""
    priority:     Priority
    tick_count:   int   = 0
    total_time_s: float = 0.0
    overruns:     int   = 0
    last_tick_t:  float = field(default_factory=time.monotonic)

    @property
    def mean_ms(self) -> float:
        return (self.total_time_s / max(self.tick_count, 1)) * 1000

    @property
    def achieved_hz(self) -> float:
        elapsed = time.monotonic() - self.last_tick_t
        if elapsed <= 0: return 0.0
        return self.tick_count / elapsed

    def to_dict(self) -> dict:
        return {
            'priority':   self.priority.name,
            'ticks':      self.tick_count,
            'mean_ms':    round(self.mean_ms, 3),
            'overruns':   self.overruns,
            'achieved_hz': round(self.achieved_hz, 1),
            'target_hz':  TARGET_HZ[self.priority],
        }


class TickScheduler:
    """
    Multi-rate deterministic tick scheduler.
    Each priority level runs at its own target frequency.
    Safety priority is never deferred — other priorities can be skipped
    under CPU pressure to maintain safety loop timing.
    """

    def __init__(self, runtime: 'CerberusRuntime'):
        self._runtime  = runtime
        self._tasks:   Dict[Priority, asyncio.Task] = {}
        self._stats:   Dict[Priority, TickStats] = {
            p: TickStats(priority=p) for p in Priority
        }
        self._running  = False
        self._tick_id  = 0

    async def start(self):
        self._running = True
        for priority in Priority:
            self._tasks[priority] = asyncio.create_task(
                self._tick_loop(priority))
        log.info('TickScheduler started — %d priority levels', len(Priority))

    async def stop(self):
        self._running = False
        for task in self._tasks.values():
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

    async def _tick_loop(self, priority: Priority):
        """Dedicated tick loop for a single priority level."""
        target_dt = 1.0 / TARGET_HZ[priority]
        stats     = self._stats[priority]

        while self._running:
            t0 = time.monotonic()
            self._tick_id += 1
            overrun = (t0 - stats.last_tick_t) > target_dt * 1.5

            ctx = TickContext(
                tick_id   = self._tick_id,
                priority  = priority,
                dt_s      = t0 - stats.last_tick_t,
                wall_time = t0,
                runtime   = self._runtime,
                overrun   = overrun,
            )
            stats.last_tick_t = t0

            try:
                await self._runtime._dispatch_tick(ctx)
            except Exception as e:
                log.error('Tick error [%s]: %s', priority.name, e)

            elapsed = time.monotonic() - t0
            stats.tick_count   += 1
            stats.total_time_s += elapsed
            if elapsed > target_dt:
                stats.overruns += 1
                if priority == Priority.SAFETY:
                    log.warning('SAFETY tick overrun: %.1fms (budget %.1fms)',
                                elapsed * 1000, target_dt * 1000)

            sleep_t = max(0.0, target_dt - elapsed)
            await asyncio.sleep(sleep_t)

    def stats(self) -> Dict[str, dict]:
        return {p.name: s.to_dict() for p, s in self._stats.items()}


# ════════════════════════════════════════════════════════════════════════════
# TYPED EVENT BUS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Event:
    """A typed, prioritized platform event."""
    id:       str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name:     str = ''
    data:     Any = None
    source:   str = 'runtime'
    priority: Priority = Priority.COGNITION
    ts:       float = field(default_factory=time.time)


class SystemEventBus:
    """
    Typed, priority-aware publish/subscribe event bus.
    High-priority events (SAFETY) are dispatched immediately via asyncio tasks.
    Normal events are queued and dispatched in order.
    """

    def __init__(self, max_history: int = 1000):
        self._handlers:  Dict[str, List[Callable]] = defaultdict(list)
        self._history:   deque = deque(maxlen=max_history)
        self._pending:   asyncio.Queue = asyncio.Queue(maxsize=500)
        self._task:      Optional[asyncio.Task] = None

    def subscribe(self, event_name: str, handler: Callable):
        self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: Callable):
        try: self._handlers[event_name].remove(handler)
        except ValueError: pass

    async def emit(self, name: str, data: Any = None,
                   source: str = 'runtime',
                   priority: Priority = Priority.COGNITION) -> str:
        event = Event(name=name, data=data, source=source, priority=priority)
        self._history.append(event)

        # Safety events dispatched immediately (bypass queue)
        if priority == Priority.SAFETY:
            await self._dispatch(event)
        else:
            try:
                self._pending.put_nowait(event)
            except asyncio.QueueFull:
                log.warning('EventBus queue full — dropping event: %s', name)

        return event.id

    async def _dispatch(self, event: Event):
        for handler in list(self._handlers.get(event.name, [])):
            try:
                result = handler(event.name, event.data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error('EventBus handler error [%s]: %s', event.name, e)

    async def start_dispatch_loop(self):
        self._task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self):
        while True:
            try:
                event = await asyncio.wait_for(self._pending.get(), timeout=1.0)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    def recent(self, n: int = 50) -> List[dict]:
        events = list(self._history)[-n:]
        return [{'id': e.id, 'name': e.name, 'source': e.source,
                 'priority': e.priority.name, 'ts': e.ts,
                 'data': e.data if not callable(e.data) else str(e.data)}
                for e in events]


# ════════════════════════════════════════════════════════════════════════════
# SUBSYSTEM REGISTRY
# ════════════════════════════════════════════════════════════════════════════

class SubsystemRegistry:
    """
    Lifecycle-managed registry for all CERBERUS subsystems.
    Handles ordered start/stop and per-priority dispatch.
    """

    def __init__(self):
        self._systems: Dict[str, Subsystem] = {}
        self._by_priority: Dict[Priority, List[Subsystem]] = defaultdict(list)

    def register(self, subsystem: Subsystem):
        self._systems[subsystem.name] = subsystem
        self._by_priority[subsystem.priority].append(subsystem)
        log.debug('Subsystem registered: %s [%s]', subsystem.name, subsystem.priority.name)

    def get(self, name: str) -> Optional[Subsystem]:
        return self._systems.get(name)

    def at_priority(self, priority: Priority) -> List[Subsystem]:
        return [s for s in self._by_priority[priority] if s.enabled]

    async def start_all(self, runtime: 'CerberusRuntime'):
        # Start in priority order (safety first)
        for priority in Priority:
            for s in self._by_priority[priority]:
                try:
                    await s.on_start(runtime)
                    log.info('Subsystem started: %s', s.name)
                except Exception as e:
                    log.error('Subsystem start failed [%s]: %s', s.name, e)

    async def stop_all(self):
        # Stop in reverse order
        for priority in reversed(list(Priority)):
            for s in self._by_priority[priority]:
                try: await s.on_stop()
                except Exception as e:
                    log.error('Subsystem stop failed [%s]: %s', s.name, e)

    def status(self) -> List[dict]:
        return [s.status() for s in self._systems.values()]


# ════════════════════════════════════════════════════════════════════════════
# WATCHDOG MONITOR
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class WatchdogEntry:
    name:      str
    timeout_s: float
    last_kick: float = field(default_factory=time.monotonic)
    tripped:   bool  = False
    trip_count: int  = 0
    on_trip:   Optional[Callable] = None


class WatchdogMonitor:
    """
    Multi-watchdog health monitor.
    Each registered subsystem kicks its watchdog periodically.
    On timeout: fires callback, marks faulted, triggers recovery.
    """

    CHECK_HZ = 10.0

    def __init__(self, bus: SystemEventBus):
        self._bus     = bus
        self._dogs:   Dict[str, WatchdogEntry] = {}
        self._task:   Optional[asyncio.Task] = None
        self._running = False

    def register(self, name: str, timeout_s: float,
                 on_trip: Optional[Callable] = None):
        self._dogs[name] = WatchdogEntry(name=name, timeout_s=timeout_s,
                                          on_trip=on_trip)

    def kick(self, name: str):
        """Subsystem calls this to signal it is alive."""
        if name in self._dogs:
            self._dogs[name].last_kick = time.monotonic()
            self._dogs[name].tripped   = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        self._running = False
        if self._task: self._task.cancel()

    async def _monitor_loop(self):
        dt = 1.0 / self.CHECK_HZ
        while self._running:
            now = time.monotonic()
            for dog in self._dogs.values():
                if dog.tripped: continue
                age = now - dog.last_kick
                if age > dog.timeout_s:
                    dog.tripped    = True
                    dog.trip_count += 1
                    log.error('Watchdog trip: %s (%.1fs stale)', dog.name, age)
                    await self._bus.emit(
                        'watchdog.trip',
                        {'name': dog.name, 'age_s': round(age, 2),
                         'count': dog.trip_count},
                        source='watchdog',
                        priority=Priority.SAFETY,
                    )
                    if dog.on_trip:
                        try: await dog.on_trip(dog.name)
                        except Exception as e:
                            log.error('Watchdog trip handler error: %s', e)
            await asyncio.sleep(dt)

    def status(self) -> dict:
        now = time.monotonic()
        return {
            name: {
                'age_s':  round(now - d.last_kick, 2),
                'timeout': d.timeout_s,
                'tripped': d.tripped,
                'trips':   d.trip_count,
                'healthy': not d.tripped and (now - d.last_kick) < d.timeout_s,
            }
            for name, d in self._dogs.items()
        }


# ════════════════════════════════════════════════════════════════════════════
# CERBERUS RUNTIME (top-level coordinator)
# ════════════════════════════════════════════════════════════════════════════

class CerberusRuntime:
    """
    CERBERUS Runtime Engine — the central nervous system.

    Coordinates all subsystems through a priority-scheduled tick loop.
    Provides: event bus, watchdog, subsystem registry, diagnostic APIs.

    Usage:
        runtime = CerberusRuntime()
        runtime.register(MySubsystem())
        await runtime.start()
        # ... robot operates ...
        await runtime.stop()
    """

    VERSION = 'CERBERUS-2.0.0'

    def __init__(self, platform=None):
        self.platform   = platform          # PlatformCore back-reference
        self.bus        = SystemEventBus()
        self.scheduler  = TickScheduler(self)
        self.registry   = SubsystemRegistry()
        self.watchdog   = WatchdogMonitor(self.bus)
        self._started   = False
        self._start_t   = 0.0
        self._shared: Dict[str, Any] = {}   # shared state store

        # Wire watchdog trip → safety system
        self.bus.subscribe('watchdog.trip', self._on_watchdog_trip)
        log.info('%s runtime initialized', self.VERSION)

    def register(self, subsystem: Subsystem):
        """Register a subsystem for lifecycle management and tick dispatch."""
        self.registry.register(subsystem)

    def share(self, key: str, value: Any):
        """Store cross-subsystem shared state."""
        self._shared[key] = value

    def shared(self, key: str, default: Any = None) -> Any:
        return self._shared.get(key, default)

    async def start(self):
        if self._started: return
        self._start_t = time.monotonic()
        await self.bus.start_dispatch_loop()
        await self.registry.start_all(self)
        await self.watchdog.start()
        await self.scheduler.start()
        self._started = True
        log.info('%s runtime started — %d subsystems active',
                 self.VERSION, len(list(self.registry._systems.values())))
        await self.bus.emit('runtime.started', {'version': self.VERSION}, 'runtime')

    async def stop(self):
        await self.scheduler.stop()
        await self.watchdog.stop()
        await self.registry.stop_all()
        self._started = False
        log.info('%s runtime stopped', self.VERSION)

    async def _dispatch_tick(self, ctx: TickContext):
        """Called by TickScheduler — dispatches to all subsystems at this priority."""
        for subsystem in self.registry.at_priority(ctx.priority):
            try:
                await subsystem.on_tick(ctx)
            except Exception as e:
                log.error('Subsystem tick error [%s]: %s', subsystem.name, e)
                await subsystem.on_fault(e)

    def _on_watchdog_trip(self, event: str, data: dict):
        """Escalate watchdog trips to platform E-STOP if safety-critical."""
        name = data.get('name', '')
        CRITICAL = {'safety_monitor', 'control_loop', 'telemetry_heartbeat'}
        if name in CRITICAL and self.platform:
            log.critical('Critical watchdog trip: %s — triggering E-STOP', name)
            asyncio.create_task(
                self.platform.safety.trigger_estop(f'watchdog:{name}'))

    def status(self) -> dict:
        return {
            'version':      self.VERSION,
            'uptime_s':     round(time.monotonic() - self._start_t, 1) if self._started else 0,
            'started':      self._started,
            'subsystems':   self.registry.status(),
            'scheduler':    self.scheduler.stats(),
            'watchdogs':    self.watchdog.status(),
            'shared_keys':  list(self._shared.keys()),
        }
