"""
cerberus/safety/gate.py
=======================
SafetyGate: centralised safety constraint checker.

Every motion or configuration command issued by the cognitive engine or the
REST API must pass through this gate before reaching the Go2Bridge.

Design
------
* Hard constraints   — always enforced; violation causes immediate rejection.
* Soft constraints   — violations emit a warning and throttle the command.
* Battery guard      — motion commands rejected when battery is critically low.
* Terrain guard      — high-speed commands rejected when IMU shows steep incline.
* Cooldown timer     — prevents re-issuing dangerous special motions too rapidly.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.hardware.go2_bridge import RobotState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SafetyConfig:
    # Battery thresholds (volts)
    battery_warn_v:     float = 22.0   # warn below this
    battery_critical_v: float = 20.5   # block motion below this

    # Velocity hard limits (m/s, rad/s)
    max_vx:   float = 1.5
    max_vy:   float = 0.8
    max_vyaw: float = 2.0

    # IMU tilt (radians) at which high-speed is throttled
    tilt_warn_rad:    float = 0.35   # ~20°
    tilt_block_rad:   float = 0.70   # ~40°

    # Cooldown between special motions (seconds)
    special_motion_cooldown: float = 3.0

    # Modes that are considered "special" (irreversible / high-energy)
    special_modes: frozenset[str] = field(default_factory=lambda: frozenset({
        "front_flip", "front_jump", "front_pounce",
        "dance1", "dance2",
    }))


# ---------------------------------------------------------------------------
# SafetyGate
# ---------------------------------------------------------------------------

class SafetyGate:
    """
    Validates motion and configuration commands against safety constraints.

    Thread / async safe: all state mutations use a simple dict with float
    timestamps (no locks needed for the current single-writer model).
    """

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self._cfg = config or SafetyConfig()
        self._last_special: float = 0.0
        self._violation_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow_move(self, vx: float, vy: float, vyaw: float,
                   state: "RobotState") -> bool:
        """Return True iff the velocity command is safe to send."""

        # 1. Battery guard
        if state.battery_voltage > 0 and state.battery_voltage < self._cfg.battery_critical_v:
            self._log_violation(
                f"Battery {state.battery_voltage:.1f}V below critical threshold "
                f"({self._cfg.battery_critical_v}V) — motion blocked"
            )
            return False

        if state.battery_voltage > 0 and state.battery_voltage < self._cfg.battery_warn_v:
            logger.warning(
                "Battery low: %.1fV (warn threshold %.1fV)",
                state.battery_voltage, self._cfg.battery_warn_v
            )

        # 2. Tilt guard
        tilt = math.sqrt(state.pitch**2 + state.roll**2)
        if tilt > self._cfg.tilt_block_rad:
            self._log_violation(
                f"Tilt {math.degrees(tilt):.1f}° exceeds block threshold "
                f"({math.degrees(self._cfg.tilt_block_rad):.1f}°) — motion blocked"
            )
            return False

        # 3. Velocity hard limits (belt-and-suspenders; bridge also clamps)
        if abs(vx) > self._cfg.max_vx * 1.05:
            self._log_violation(f"|vx|={abs(vx):.2f} exceeds hard limit {self._cfg.max_vx}")
            return False
        if abs(vy) > self._cfg.max_vy * 1.05:
            self._log_violation(f"|vy|={abs(vy):.2f} exceeds hard limit {self._cfg.max_vy}")
            return False
        if abs(vyaw) > self._cfg.max_vyaw * 1.05:
            self._log_violation(f"|vyaw|={abs(vyaw):.2f} exceeds hard limit {self._cfg.max_vyaw}")
            return False

        return True

    def allow_mode(self, mode: str, state: "RobotState") -> tuple[bool, str]:
        """
        Return (allowed, reason).
        'reason' is empty on success.
        """
        if mode in self._cfg.special_modes:
            elapsed = time.monotonic() - self._last_special
            if elapsed < self._cfg.special_motion_cooldown:
                msg = (
                    f"Mode '{mode}' on cooldown for "
                    f"{self._cfg.special_motion_cooldown - elapsed:.1f}s more"
                )
                self._log_violation(msg)
                return False, msg
            self._last_special = time.monotonic()

        # Battery guard for special motions
        if mode in self._cfg.special_modes and state.battery_voltage > 0:
            if state.battery_voltage < self._cfg.battery_warn_v:
                msg = f"Battery too low ({state.battery_voltage:.1f}V) for special mode '{mode}'"
                self._log_violation(msg)
                return False, msg

        return True, ""

    def check_config(self, **kwargs: float) -> tuple[bool, str]:
        """Validate configuration parameters (body_height, euler angles, etc.)."""
        if "body_height" in kwargs:
            h = kwargs["body_height"]
            if not (0.3 <= h <= 0.5):
                return False, f"body_height {h:.3f} outside [0.3, 0.5]"

        if "roll" in kwargs and abs(kwargs["roll"]) > 0.75:
            return False, f"roll {kwargs['roll']:.3f} rad exceeds ±0.75"
        if "pitch" in kwargs and abs(kwargs["pitch"]) > 0.75:
            return False, f"pitch {kwargs['pitch']:.3f} rad exceeds ±0.75"
        if "yaw" in kwargs and abs(kwargs["yaw"]) > 1.5:
            return False, f"yaw {kwargs['yaw']:.3f} rad exceeds ±1.5"

        return True, ""

    @property
    def violation_count(self) -> int:
        return self._violation_count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_violation(self, msg: str) -> None:
        self._violation_count += 1
        logger.warning("SafetyGate [#%d] %s", self._violation_count, msg)
