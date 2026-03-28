"""
cerberus/learning/data_logger.py  — CERBERUS v3.1  (NEW)
=========================================================
Data logging and replay system for training data collection.

Logging:
  - Records timestamped RobotState + action events to NDJSON (newline-delimited JSON).
  - Rotates log files at configurable size/time limits.
  - Inspired by Unitree's logging-mp multiprocessing logger pattern.

Replay:
  - Load a session file and play back commands at original timing.
  - Used for behaviour imitation and regression testing.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Optional

if TYPE_CHECKING:
    from cerberus.hardware.bridge import RobotState

logger = logging.getLogger(__name__)


# ── Log record ─────────────────────────────────────────────────────────── #

@dataclass
class LogRecord:
    ts:         float                    # monotonic timestamp (seconds)
    wall_time:  str                      # ISO-8601 wall clock
    record_type: str                     # "state" | "action" | "event"
    data:       dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "ts": self.ts, "wall_time": self.wall_time,
            "type": self.record_type, "data": self.data,
        })

    @classmethod
    def from_json(cls, line: str) -> "LogRecord":
        d = json.loads(line)
        return cls(
            ts=d["ts"], wall_time=d["wall_time"],
            record_type=d["type"], data=d.get("data", {}),
        )


# ── DataLogger ─────────────────────────────────────────────────────────── #

class DataLogger:
    """
    Writes NDJSON log files to logs_dir.

    File naming: cerberus_YYYYMMDD_HHMMSS.ndjson[.gz]
    Auto-rotates when max_bytes is reached.
    """

    def __init__(self, logs_dir: str = "logs",
                 max_mb: float = 50.0,
                 compress: bool = True,
                 state_interval_s: float = 0.1) -> None:
        self._dir        = Path(logs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes  = int(max_mb * 1_048_576)
        self._compress   = compress
        self._interval   = state_interval_s
        self._fh         = None
        self._path:      Optional[Path] = None
        self._written    = 0
        self._session_start = time.monotonic()
        self._task:      Optional[asyncio.Task] = None
        self._bridge     = None
        self._open_new()

    # ── Public API ──────────────────────────────────────────────────────── #

    def attach_bridge(self, bridge: "Any") -> None:  # type: ignore[name-defined]
        """Attach a Go2Bridge for automatic state recording."""
        self._bridge = bridge
        bridge.add_state_listener(self._on_state)

    def log_action(self, action_type: str, params: dict) -> None:
        """Log a command/action issued to the robot."""
        self._write(LogRecord(
            ts=time.monotonic() - self._session_start,
            wall_time=datetime.now(timezone.utc).isoformat(),
            record_type="action",
            data={"action": action_type, "params": params},
        ))

    def log_event(self, event: str, data: dict | None = None) -> None:
        """Log a named event (obstacle, human_detected, etc.)."""
        self._write(LogRecord(
            ts=time.monotonic() - self._session_start,
            wall_time=datetime.now(timezone.utc).isoformat(),
            record_type="event",
            data={"event": event, **(data or {})},
        ))

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
            logger.info("DataLogger closed: %s (%d bytes)", self._path, self._written)

    # ── Internal ────────────────────────────────────────────────────────── #

    def _on_state(self, state: "RobotState") -> None:
        from dataclasses import asdict
        self._write(LogRecord(
            ts=time.monotonic() - self._session_start,
            wall_time=datetime.now(timezone.utc).isoformat(),
            record_type="state",
            data=_state_to_dict(state),
        ))

    def _write(self, record: LogRecord) -> None:
        if self._fh is None:
            self._open_new()
        line = record.to_json() + "\n"
        data = line.encode()
        try:
            self._fh.write(data)
            self._fh.flush()
            self._written += len(data)
        except Exception as e:
            logger.error("DataLogger write error: %s", e)
            return
        if self._written >= self._max_bytes:
            self._rotate()

    def _open_new(self) -> None:
        if self._fh:
            self._fh.close()
        ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext = ".ndjson.gz" if self._compress else ".ndjson"
        self._path   = self._dir / f"cerberus_{ts}{ext}"
        self._fh     = gzip.open(self._path, "wb") if self._compress else open(self._path, "wb")
        self._written = 0
        logger.info("DataLogger opened: %s", self._path)

    def _rotate(self) -> None:
        logger.info("DataLogger rotating (%.1f MB written)", self._written / 1_048_576)
        self._open_new()

    def list_sessions(self) -> list[Path]:
        patterns = ["*.ndjson", "*.ndjson.gz"]
        files = []
        for p in patterns:
            files.extend(self._dir.glob(p))
        return sorted(files)


def _state_to_dict(state: "RobotState") -> dict:
    """Lightweight state → dict for logging (avoids importing dataclasses in hot path)."""
    return {
        "pos":      [state.position_x, state.position_y],
        "orient":   [state.yaw, state.pitch, state.roll],
        "vel":      [state.vx, state.vy, state.vyaw],
        "batt":     state.battery_voltage,
        "batt_pct": state.battery_percent,
        "foot":     state.foot_force,
        "mode":     state.current_mode,
    }


# ── SessionReplayer ─────────────────────────────────────────────────────── #

class SessionReplayer:
    """
    Reads a recorded NDJSON session and replays actions at original timing.

    Useful for imitation learning and regression testing.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def iter_records(self) -> AsyncIterator[LogRecord]:
        return self._aiter()

    async def _aiter(self):
        opener = gzip.open if str(self._path).endswith(".gz") else open
        with opener(self._path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    yield LogRecord.from_json(line)
                except Exception as e:
                    logger.warning("Skipping malformed record: %s", e)

    async def replay(self, bridge: "Any",  # type: ignore[name-defined]
                     speed: float = 1.0,
                     actions_only: bool = True) -> None:
        """
        Replay a session against a live bridge.

        speed: 1.0 = real time, 2.0 = 2× speed, 0 = as fast as possible
        actions_only: if True, only replay "action" records (skip state/event)
        """
        logger.info("Replaying session %s at %.1f× speed", self._path.name, speed)
        last_ts: float | None = None

        async for rec in self.iter_records():
            if actions_only and rec.record_type != "action":
                continue

            if last_ts is not None and speed > 0:
                delay = (rec.ts - last_ts) / speed
                if delay > 0:
                    await asyncio.sleep(delay)
            last_ts = rec.ts

            action = rec.data.get("action", "")
            params  = rec.data.get("params", {})

            try:
                match action:
                    case "move":
                        await bridge.move(params.get("vx",0), params.get("vy",0), params.get("vyaw",0))
                    case "stop":
                        await bridge.stop()
                    case "mode":
                        await bridge.set_mode(params["mode"])
                    case "set_body_height":
                        await bridge.set_body_height(params["height"])
                    case "emergency_stop":
                        await bridge.emergency_stop()
                    case _:
                        logger.debug("Replay: unknown action '%s'", action)
            except Exception as e:
                logger.error("Replay action '%s' failed: %s", action, e)

        logger.info("Replay complete")
