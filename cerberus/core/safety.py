"""
cerberus/core/safety.py
━━━━━━━━━━━━━━━━━━━━━━
Safety-critical watchdog system.

Priority: HIGHEST — runs before every other CERBERUS subsystem.

Responsibilities:
  • Heartbeat monitor — if no command within timeout, damp motors
  • Emergency stop — hard kill, no override
  • Velocity / pose guardrails
  • Battery low-power mode
  • Tilt / fall detection
  • Audit log of all safety events
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import BridgeBase, RobotState

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = Path(os.getenv("CERBERUS_AUDIT_LOG", "logs/safety_audit.jsonl"))


class SafetyLevel(str, Enum):
    NOMINAL  = "nominal"
    CAUTION  = "caution"   # non-critical alert, continue operation
    WARNING  = "warning"   # reduce speed, alert operator
    CRITICAL = "critical"  # initiate controlled stop
    ESTOP    = "estop"     # hard stop, damp all motors


@dataclass
class SafetyEvent:
    timestamp: float
    level: SafetyLevel
    code: str
    message: str
    state_snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "level": self.level.value,
            "code": self.code,
            "msg": self.message,
            "snap": self.state_snapshot,
        }


# ── Configurable limits ───────────────────────────────────────────────────────

@dataclass
class SafetyLimits:
    # Velocity
    max_vx: float    = 1.5    # m/s
    max_vy: float    = 0.8
    max_vyaw: float  = 2.0    # rad/s

    # Pose
    max_roll_deg:  float = 30.0
    max_pitch_deg: float = 30.0

    # Body height (absolute)
    min_body_height: float = 0.20  # m
    max_body_height: float = 0.55

    # Battery
    battery_warn_pct:   float = 15.0
    battery_low_pct:    float = 8.0
    battery_critical_pct: float = 4.0

    # Heartbeat — if no command for this long, stop motion
    heartbeat_timeout_s: float = 5.0

    # Watchdog tick rate
    watchdog_hz: float = 50.0  # 50 Hz watchdog


class SafetyWatchdog:
    """
    Central safety supervisor.

    Must be started as an asyncio task before the engine loop.
    All motion commands must call `ping_heartbeat()` to prevent auto-stop.
    """

    def __init__(self, bridge: "BridgeBase", limits: SafetyLimits | None = None):
        self.bridge = bridge
        self.limits = limits or SafetyLimits()
        self._level = SafetyLevel.NOMINAL
        self._estop = False
        self._last_heartbeat = time.monotonic()
        self._events: list[SafetyEvent] = []
        self._running = False
        self._audit_enabled = True
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def ping_heartbeat(self) -> None:
        """Call this on every motion command to keep the watchdog happy."""
        self._last_heartbeat = time.monotonic()

    @property
    def estop_active(self) -> bool:
        return self._estop

    @property
    def safety_level(self) -> SafetyLevel:
        return self._level

    async def trigger_estop(self, reason: str = "manual") -> None:
        """Hard emergency stop — cannot be cleared without restart."""
        if self._estop:
            return
        self._estop = True
        self._level = SafetyLevel.ESTOP
        await self._emit_event(SafetyLevel.ESTOP, "ESTOP_TRIGGERED", reason)
        await self.bridge.emergency_stop()
        logger.critical("🛑 EMERGENCY STOP: %s", reason)

    async def clear_estop(self) -> bool:
        """Re-arm after E-stop — only allowed in simulation."""
        sim = os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1")
        if not sim:
            logger.error("E-Stop clearance only allowed in simulation mode")
            return False
        self._estop = False
        self._level = SafetyLevel.NOMINAL
        self._last_heartbeat = time.monotonic()
        await self._emit_event(SafetyLevel.NOMINAL, "ESTOP_CLEARED", "operator reset (sim)")
        return True

    # ── Guardrail validators ───────────────────────────────────────────────────

    def validate_velocity(self, vx: float, vy: float, vyaw: float) -> tuple[bool, str]:
        """Returns (ok, reason). Clamps not applied here — use bridge clamping."""
        if abs(vx) > self.limits.max_vx:
            return False, f"vx={vx:.2f} exceeds limit {self.limits.max_vx}"
        if abs(vy) > self.limits.max_vy:
            return False, f"vy={vy:.2f} exceeds limit {self.limits.max_vy}"
        if abs(vyaw) > self.limits.max_vyaw:
            return False, f"vyaw={vyaw:.2f} exceeds limit {self.limits.max_vyaw}"
        return True, ""

    def validate_body_height(self, height: float) -> tuple[bool, str]:
        if height < self.limits.min_body_height or height > self.limits.max_body_height:
            return False, f"body_height={height:.2f} out of range [{self.limits.min_body_height}, {self.limits.max_body_height}]"
        return True, ""

    # ── Main watchdog loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Async watchdog task. Runs at watchdog_hz until stopped."""
        self._running = True
        interval = 1.0 / self.limits.watchdog_hz
        logger.info("Safety watchdog started at %.0fHz", self.limits.watchdog_hz)

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Safety watchdog tick error: %s", exc)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False

    async def _tick(self) -> None:
        if self._estop:
            return  # Already stopped — nothing to do

        state = await self.bridge.get_state()
        now = time.monotonic()

        # 1. Heartbeat timeout → controlled stop
        elapsed = now - self._last_heartbeat
        if elapsed > self.limits.heartbeat_timeout_s:
            await self._emit_event(
                SafetyLevel.CRITICAL, "HEARTBEAT_TIMEOUT",
                f"No command for {elapsed:.1f}s — stopping motion"
            )
            await self.bridge.stop_move()
            self._last_heartbeat = now  # Reset so we don't spam
            return

        # 2. Tilt / fall detection
        import math
        roll_deg  = abs(math.degrees(state.roll))
        pitch_deg = abs(math.degrees(state.pitch))
        if roll_deg > self.limits.max_roll_deg or pitch_deg > self.limits.max_pitch_deg:
            await self.trigger_estop(
                f"Tilt limit exceeded: roll={roll_deg:.1f}° pitch={pitch_deg:.1f}°"
            )
            return

        # 3. Battery level
        pct = state.battery_percent
        if pct <= self.limits.battery_critical_pct:
            await self.trigger_estop(f"Battery critical: {pct:.1f}%")
        elif pct <= self.limits.battery_low_pct and self._level == SafetyLevel.NOMINAL:
            await self._emit_event(SafetyLevel.WARNING, "BATTERY_LOW", f"{pct:.1f}%")
            self._level = SafetyLevel.WARNING
        elif pct <= self.limits.battery_warn_pct and self._level == SafetyLevel.NOMINAL:
            await self._emit_event(SafetyLevel.CAUTION, "BATTERY_WARN", f"{pct:.1f}%")
            self._level = SafetyLevel.CAUTION
        elif pct > self.limits.battery_warn_pct and self._level in (SafetyLevel.CAUTION, SafetyLevel.WARNING):
            self._level = SafetyLevel.NOMINAL

    # ── Audit logging ──────────────────────────────────────────────────────────

    async def _emit_event(self, level: SafetyLevel, code: str, message: str) -> None:
        state = await self.bridge.get_state()
        event = SafetyEvent(
            timestamp=time.time(),
            level=level,
            code=code,
            message=message,
            state_snapshot=state.to_dict(),
        )
        self._events.append(event)
        self._level = level
        log_fn = {
            SafetyLevel.NOMINAL:  logger.debug,
            SafetyLevel.CAUTION:  logger.info,
            SafetyLevel.WARNING:  logger.warning,
            SafetyLevel.CRITICAL: logger.error,
            SafetyLevel.ESTOP:    logger.critical,
        }.get(level, logger.warning)
        log_fn("[SAFETY] %s: %s", code, message)

        if self._audit_enabled:
            try:
                with open(AUDIT_LOG_PATH, "a") as f:
                    f.write(json.dumps(event.to_dict()) + "\n")
            except Exception as e:
                logger.debug("Audit log write failed: %s", e)

    def get_recent_events(self, n: int = 50) -> list[dict]:
        return [e.to_dict() for e in self._events[-n:]]
