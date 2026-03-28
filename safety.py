"""
CERBERUS Safety System
=======================
All safety logic lives here.  This is the ONLY subsystem allowed to issue
an immediate hardware stop without going through the normal motion pipeline.

Hard constraints (enforced on every tick):
  • Battery voltage  < VMIN          → controlled sit-down + stop
  • IMU roll/pitch   > TILT_LIMIT    → emergency recovery
  • Heart rate       > HR_MAX        → pause interaction + operator alert
  • Heart rate       < HR_MIN (if active) → assume operator emergency, e-stop
  • Watchdog miss    > TIMEOUT_S     → halt and wait for reconnect

E-stop flow:
  1. Any coroutine calls safety.trigger_estop(reason)
  2. Safety emits ESTOP_TRIGGERED at priority-1 (bypass queue)
  3. Robot adapter sees the event and immediately zeros velocity commands
  4. All peripheral plugins see the event and cease output
  5. UI reflects ESTOP state
  6. Operator must call safety.clear_estop() to resume
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from cerberus.core.event_bus import Event, EventType, get_bus

logger = logging.getLogger(__name__)


# ── Constraint constants (override via config) ─────────────────────────────────

VMIN_BATTERY         = 21.0   # volts  — Go2 nominal 24 V, cutoff ~21 V
TILT_LIMIT_DEG       = 45.0   # degrees roll or pitch triggers recovery
HR_MAX_BPM           = 180    # above this = operator alert + interaction pause
HR_CRITICAL_MAX_BPM  = 200    # above this = hard estop
HR_MIN_BPM           = 40     # below this while wearable is connected = estop
WATCHDOG_TIMEOUT_S   = 3.0    # seconds without heartbeat from robot = halt


@dataclass
class SafetyState:
    estop:             bool        = False
    estop_reason:      str         = ""
    estop_time:        float       = 0.0
    battery_ok:        bool        = True
    tilt_ok:           bool        = True
    hr_ok:             bool        = True
    wearable_active:   bool        = False
    last_robot_ping:   float       = field(default_factory=time.monotonic)
    violations:        list[str]   = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.estop and self.battery_ok and self.tilt_ok and self.hr_ok


class SafetyManager:
    """
    Singleton safety manager.
    Subscribe it to the bus during startup — it will self-maintain state
    and emit ESTOP / violation events automatically.
    """

    def __init__(self) -> None:
        self.state = SafetyState()
        self.bus   = get_bus()
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def trigger_estop(self, reason: str, source: str = "safety") -> None:
        """
        Hard e-stop.  Priority-1 — dispatched synchronously before any other
        coroutine in the current tick sees control.
        """
        async with self._lock:
            if self.state.estop:
                return                        # already stopped
            self.state.estop        = True
            self.state.estop_reason = reason
            self.state.estop_time   = time.monotonic()

        logger.critical("E-STOP  (%s): %s", source, reason)
        await self.bus.publish(Event(
            type=EventType.ESTOP_TRIGGERED,
            source=source,
            data={"reason": reason},
            priority=1,                      # bypass queue
        ))

    async def clear_estop(self, operator: str = "operator") -> bool:
        """Clears the e-stop.  Returns False if conditions still unsafe."""
        async with self._lock:
            if not self._conditions_safe():
                logger.warning("Clear e-stop refused — unsafe conditions persist")
                return False
            self.state.estop        = False
            self.state.estop_reason = ""

        logger.info("E-stop cleared by %s", operator)
        await self.bus.publish(Event(
            type=EventType.ESTOP_CLEARED,
            source=operator,
            priority=2,
        ))
        return True

    def is_stopped(self) -> bool:
        return self.state.estop

    # ── Event handlers (subscribe these to the bus) ──────────────────────────

    async def on_robot_state(self, event: Event) -> None:
        data = event.data

        # Battery check
        voltage = data.get("battery_voltage", 99.0)
        if voltage < VMIN_BATTERY:
            self.state.battery_ok = False
            await self._soft_violation(f"battery low: {voltage:.1f}V < {VMIN_BATTERY}V")
            if voltage < VMIN_BATTERY - 1.0:
                await self.trigger_estop(f"critical battery {voltage:.1f}V", "safety.battery")
        else:
            self.state.battery_ok = True

        # Tilt check
        roll_deg  = abs(data.get("imu_roll",  0.0))
        pitch_deg = abs(data.get("imu_pitch", 0.0))
        if roll_deg > TILT_LIMIT_DEG or pitch_deg > TILT_LIMIT_DEG:
            self.state.tilt_ok = False
            await self._soft_violation(
                f"tilt exceeded: roll={roll_deg:.1f}° pitch={pitch_deg:.1f}°"
            )
        else:
            self.state.tilt_ok = True

        # Reset watchdog
        self.state.last_robot_ping = time.monotonic()

    async def on_heartrate(self, event: Event) -> None:
        bpm = event.data.get("bpm", 0)
        self.state.wearable_active = True

        if bpm >= HR_CRITICAL_MAX_BPM:
            self.state.hr_ok = False
            await self.trigger_estop(
                f"operator HR critical: {bpm} bpm", "safety.heartrate"
            )
        elif bpm >= HR_MAX_BPM:
            self.state.hr_ok = False
            await self.bus.publish(Event(
                type=EventType.HEARTRATE_ALARM,
                source="safety.heartrate",
                data={"bpm": bpm, "reason": "HR_HIGH"},
                priority=1,
            ))
        elif self.state.wearable_active and bpm < HR_MIN_BPM and bpm > 0:
            # Non-zero but suspiciously low — possible sensor loss / operator issue
            self.state.hr_ok = False
            await self.trigger_estop(
                f"operator HR critically low: {bpm} bpm", "safety.heartrate"
            )
        else:
            self.state.hr_ok = True

    async def on_wearable_disconnected(self, event: Event) -> None:
        # If wearable was actively connected and drops, treat as unknown — don't
        # auto-estop (could be normal disconnect), but flag it.
        self.state.wearable_active = False
        logger.warning("Wearable disconnected — HR monitoring suspended")

    # ── Watchdog tick (call from runtime tick) ────────────────────────────────

    async def watchdog_check(self) -> None:
        if not self.state.estop:
            age = time.monotonic() - self.state.last_robot_ping
            if age > WATCHDOG_TIMEOUT_S:
                await self.trigger_estop(
                    f"robot telemetry timeout ({age:.1f}s)", "safety.watchdog"
                )

    # ── Subscription helper ───────────────────────────────────────────────────

    def register_subscriptions(self) -> None:
        """Call once at startup to wire this manager into the bus."""
        self.bus.subscribe(EventType.ROBOT_STATE_UPDATE,    self.on_robot_state,           priority=1)
        self.bus.subscribe(EventType.HEARTRATE_UPDATE,      self.on_heartrate,             priority=1)
        self.bus.subscribe(EventType.WEARABLE_DISCONNECTED, self.on_wearable_disconnected, priority=2)
        logger.info("SafetyManager subscriptions registered")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _conditions_safe(self) -> bool:
        return self.state.battery_ok and self.state.tilt_ok and self.state.hr_ok

    async def _soft_violation(self, detail: str) -> None:
        self.state.violations.append(detail)
        if len(self.state.violations) > 100:
            self.state.violations = self.state.violations[-100:]
        logger.warning("Safety violation: %s", detail)
        await self.bus.publish(Event(
            type=EventType.SAFETY_VIOLATION,
            source="safety",
            data={"detail": detail},
            priority=2,
        ))


# ── Module singleton ──────────────────────────────────────────────────────────

_safety: SafetyManager | None = None


def get_safety() -> SafetyManager:
    global _safety
    if _safety is None:
        _safety = SafetyManager()
    return _safety
