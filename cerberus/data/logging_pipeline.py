"""
cerberus/data/logging_pipeline.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Data Logging & Replay Pipeline

Provides:
  DataLogger    — structured event logging with ring-buffer and disk persistence
  SessionRecorder — records complete robot sessions for later replay/analysis
  ScenarioReplayer — replays recorded sessions against the platform
  DatasetExporter — exports session data in formats for ML training

Log format: JSONL (one JSON object per line) for easy streaming and parsing.
All logs are time-indexed and compressed on rotation.

Session data schema:
  {
    "session_id": "...",
    "start_time": ...,
    "platform_version": "...",
    "events": [...],          # all EventBus events
    "telemetry": [...],       # telemetry samples
    "behaviors": [...],       # behaviors performed
    "learning": {...},        # preference state snapshot
    "personality": {...},     # mood/trait snapshot
  }
"""

import asyncio
import gzip
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.data')


# ════════════════════════════════════════════════════════════════════════════
# DATA LOGGER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LogEntry:
    ts:       float
    category: str         # telemetry / event / behavior / safety / learning
    data:     dict
    source:   str = ''

    def to_jsonl(self) -> str:
        return json.dumps({
            'ts': self.ts, 'category': self.category,
            'source': self.source, **self.data
        }, default=str)


class DataLogger(Subsystem):
    """
    Structured event logger with in-memory ring buffer + optional disk persistence.
    Exports JSONL format suitable for ML training pipelines.
    Runs at Priority.TELEMETRY (5Hz background flush).
    """

    name     = 'data_logger'
    priority = Priority.TELEMETRY

    RING_CAPACITY = 50_000      # in-memory entries
    MAX_FILE_BYTES = 50_000_000  # 50MB per log file before rotation

    def __init__(self, bus: SystemEventBus,
                 log_dir: str = '/tmp/cerberus_logs'):
        self._bus       = bus
        self._log_dir   = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._ring:     deque = deque(maxlen=self.RING_CAPACITY)
        self._session_id = str(uuid.uuid4())[:12]
        self._file:     Optional[Any] = None
        self._file_path: Optional[Path] = None
        self._file_bytes = 0
        self._entries_written = 0
        self._enabled_categories = {
            'telemetry', 'event', 'behavior', 'safety', 'learning',
            'personality', 'body', 'cognitive',
        }

        # Subscribe to all relevant events
        for event_name in ['telemetry', 'fsm.transition', 'behavior_start',
                           'safety.trip', 'safety.estop', 'goal.complete',
                           'goal.failed', 'mission.complete', 'personality.state',
                           'body.state', 'learning.preferred_behaviors',
                           'watchdog.trip', 'i18n.locale_changed']:
            bus.subscribe(event_name, self._on_event)

    def _on_event(self, event: str, data: Any):
        category = self._event_category(event)
        if category not in self._enabled_categories:
            return
        entry = LogEntry(
            ts       = time.time(),
            category = category,
            data     = {'event': event, 'payload': data},
            source   = 'event_bus',
        )
        self._ring.append(entry)

    def _event_category(self, event: str) -> str:
        if 'safety' in event or 'estop' in event: return 'safety'
        if 'telemetry' in event:                   return 'telemetry'
        if 'behavior' in event:                    return 'behavior'
        if 'personality' in event:                 return 'personality'
        if 'body' in event:                        return 'body'
        if 'learning' in event:                    return 'learning'
        return 'event'

    def log(self, category: str, data: dict, source: str = ''):
        entry = LogEntry(ts=time.time(), category=category, data=data, source=source)
        self._ring.append(entry)

    async def on_start(self, runtime):
        self._open_log_file()
        log.info('DataLogger started — session=%s dir=%s', self._session_id, self._log_dir)

    async def on_tick(self, ctx: TickContext):
        """Flush ring buffer to disk periodically."""
        await asyncio.get_event_loop().run_in_executor(None, self._flush)

    async def on_stop(self):
        self._flush()
        if self._file:
            self._file.close()
            log.info('DataLogger: %d entries written to %s',
                     self._entries_written, self._file_path)

    def _open_log_file(self):
        fname = f'cerberus_{self._session_id}_{int(time.time())}.jsonl'
        self._file_path = self._log_dir / fname
        self._file      = open(self._file_path, 'w', buffering=1)  # line-buffered
        self._file_bytes = 0
        log.debug('Log file opened: %s', self._file_path)

    def _flush(self):
        if not self._file or not self._ring:
            return
        try:
            while self._ring:
                entry = self._ring.popleft()
                line  = entry.to_jsonl() + '\n'
                self._file.write(line)
                self._file_bytes   += len(line)
                self._entries_written += 1
            # Rotate if file is too large
            if self._file_bytes >= self.MAX_FILE_BYTES:
                self._file.close()
                self._open_log_file()
        except Exception as e:
            log.error('DataLogger flush error: %s', e)

    def recent(self, n: int = 100, category: Optional[str] = None) -> List[dict]:
        entries = list(self._ring)[-n:]
        if category:
            entries = [e for e in entries if e.category == category]
        return [json.loads(e.to_jsonl()) for e in entries]

    def export_session_jsonl(self, output_path: str) -> str:
        """Export all logged data as a gzipped JSONL file."""
        out = Path(output_path)
        with gzip.open(str(out) + '.gz', 'wt', encoding='utf-8') as f:
            header = {
                'session_id': self._session_id,
                'exported_at': time.time(),
                'platform': 'CERBERUS-2.0',
                'entry_count': self._entries_written,
            }
            f.write(json.dumps(header) + '\n')
            if self._file_path and self._file_path.exists():
                with open(self._file_path) as src:
                    for line in src:
                        f.write(line)
        return str(out) + '.gz'

    def status(self) -> dict:
        return {
            'name':    self.name,
            'enabled': self.enabled,
            'session_id': self._session_id,
            'ring_size':  len(self._ring),
            'entries_written': self._entries_written,
            'log_file': str(self._file_path) if self._file_path else None,
            'categories': list(self._enabled_categories),
        }


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO REPLAYER
# ════════════════════════════════════════════════════════════════════════════

class ScenarioReplayer:
    """
    Replays a recorded session against the platform for testing/validation.
    Injects events at their original timestamps (scaled by playback speed).
    """

    def __init__(self, bus: SystemEventBus, platform=None):
        self._bus      = bus
        self._platform = platform
        self._speed    = 1.0
        self._task:    Optional[asyncio.Task] = None

    async def replay_file(self, path: str, speed: float = 1.0):
        """Replay a JSONL log file at the given speed multiplier."""
        self._speed = speed
        opener = gzip.open if path.endswith('.gz') else open
        events = []
        with opener(path, 'rt') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get('category') in ('event', 'behavior', 'telemetry'):
                        events.append(entry)
                except json.JSONDecodeError:
                    pass

        if not events:
            log.warning('ScenarioReplayer: no playable events in %s', path)
            return

        log.info('Replaying %d events from %s at %.1fx speed', len(events), path, speed)
        t0_log  = events[0].get('ts', 0.0)
        t0_wall = time.monotonic()

        for entry in events:
            # Calculate when this event should fire
            log_age  = (entry.get('ts', t0_log) - t0_log) / speed
            wall_age = time.monotonic() - t0_wall
            delay    = log_age - wall_age
            if delay > 0:
                await asyncio.sleep(delay)

            # Re-emit event
            event_name = entry.get('payload', {}).get('event', '')
            event_data = entry.get('payload', {}).get('payload')
            if event_name:
                await self._bus.emit(event_name, event_data, source='replayer')

        log.info('Scenario replay complete')

    def abort(self):
        if self._task:
            self._task.cancel()


# ════════════════════════════════════════════════════════════════════════════
# DATASET EXPORTER (for ML training)
# ════════════════════════════════════════════════════════════════════════════

class DatasetExporter:
    """
    Exports session data in formats suitable for ML training:
      - Behavior sequence dataset (for imitation learning)
      - Telemetry dataset (for sensor modeling)
      - Reward dataset (for RL training)
    """

    @staticmethod
    def export_behavior_sequences(log_path: str, output_path: str) -> dict:
        """Extract behavior sequences from log → CSV for imitation learning."""
        sequences = []
        opener = gzip.open if log_path.endswith('.gz') else open
        try:
            with opener(log_path, 'rt') as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get('category') == 'behavior':
                        payload = entry.get('payload', {}).get('payload', {})
                        sequences.append({
                            'ts':          entry['ts'],
                            'behavior_id': payload.get('id', ''),
                            'source':      'autonomous',
                        })
        except Exception as e:
            return {'error': str(e), 'sequences': 0}

        import csv
        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['ts', 'behavior_id', 'source'])
            writer.writeheader()
            writer.writerows(sequences)

        return {'sequences': len(sequences), 'output': output_path}

    @staticmethod
    def export_telemetry_csv(log_path: str, output_path: str) -> dict:
        """Extract telemetry samples → CSV for sensor modeling."""
        samples = []
        opener  = gzip.open if log_path.endswith('.gz') else open
        try:
            with opener(log_path, 'rt') as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get('category') == 'telemetry':
                        payload = entry.get('payload', {}).get('payload', {})
                        if isinstance(payload, dict):
                            samples.append({'ts': entry['ts'], **payload})
        except Exception as e:
            return {'error': str(e)}

        if not samples:
            return {'samples': 0}

        import csv
        fields = list(samples[0].keys())
        with open(output_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(samples)

        return {'samples': len(samples), 'output': output_path}
