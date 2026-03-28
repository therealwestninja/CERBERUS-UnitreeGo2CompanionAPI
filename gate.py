"""
cerberus/safety/gate.py  — CERBERUS v3.1.1
===========================================
SafetyGate: centralised constraint checker.

Battery thresholds updated to reflect real Go2 hardware specs:
  Standard Air/Pro/EDU (8000mAh, ~25.2V):  warn 22V, block 20.5V
  EDU+ extended (15000mAh, ~28.8V):        warn 25.0V, block 23.5V
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.hardware.bridge import RobotState

logger = logging.getLogger(__name__)


@dataclass
class SafetyConfig:
    # Go2 battery voltages (nominal fully-charged / warn / critical):
    #   Standard Air/Pro/EDU (8000 mAh):  ~25.2 V / 22.0 V / 20.5 V
    #   EDU+ extended    (15000 mAh):     ~28.8 V / 25.0 V / 23.5 V
    battery_warn_v:          float = 22.0
    battery_critical_v:      float = 20.5
    max_vx:                  float = 1.5    # Go2 max ~2 m/s; 1.5 m/s safe limit
    max_vy:                  float = 0.8
    max_vyaw:                float = 2.0
    tilt_warn_rad:           float = 0.35   # ~20 deg
    tilt_block_rad:          float = 0.70   # ~40 deg (Go2 slope limit)
    special_motion_cooldown: float = 3.0
    special_modes: frozenset = field(default_factory=lambda: frozenset({
        "front_flip", "front_jump", "front_pounce", "dance1", "dance2",
    }))

    @classmethod
    def for_edu_plus(cls) -> "SafetyConfig":
        """Pre-configured thresholds for Go2 EDU+ with 28.8 V / 15000 mAh battery."""
        return cls(battery_warn_v=25.0, battery_critical_v=23.5)


class SafetyGate:
    """Validates every motion/config command. No bypass except emergency_stop."""

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self._cfg = config or SafetyConfig()
        self._last_special: float = 0.0
        self._violations: int = 0

    def allow_move(self, vx: float, vy: float, vyaw: float,
                   state: "RobotState") -> bool:
        v = state.battery_voltage
        if v > 0 and v < self._cfg.battery_critical_v:
            return self._block(f"Battery {v:.1f}V < critical {self._cfg.battery_critical_v}V")
        if v > 0 and v < self._cfg.battery_warn_v:
            logger.warning("Battery low: %.1fV", v)

        tilt = math.sqrt(state.pitch**2 + state.roll**2)
        if tilt > self._cfg.tilt_block_rad:
            return self._block(f"Tilt {math.degrees(tilt):.1f}° exceeds block threshold")

        if abs(vx)   > self._cfg.max_vx   * 1.05: return self._block(f"|vx|={abs(vx):.2f} > {self._cfg.max_vx}")
        if abs(vy)   > self._cfg.max_vy   * 1.05: return self._block(f"|vy|={abs(vy):.2f} > {self._cfg.max_vy}")
        if abs(vyaw) > self._cfg.max_vyaw * 1.05: return self._block(f"|vyaw|={abs(vyaw):.2f} > {self._cfg.max_vyaw}")
        return True

    def allow_mode(self, mode: str, state: "RobotState") -> tuple[bool, str]:
        if mode in self._cfg.special_modes:
            elapsed = time.monotonic() - self._last_special
            remaining = self._cfg.special_motion_cooldown - elapsed
            if remaining > 0:
                msg = f"Mode '{mode}' on cooldown for {remaining:.1f}s"
                self._violations += 1
                return False, msg
            self._last_special = time.monotonic()
        return True, ""

    def check_config(self, **kw: float) -> tuple[bool, str]:
        if "body_height" in kw and not (0.3 <= kw["body_height"] <= 0.5):
            return False, f"body_height {kw['body_height']:.3f} outside [0.3, 0.5]"
        if "roll"  in kw and abs(kw["roll"])  > 0.75: return False, "roll out of range"
        if "pitch" in kw and abs(kw["pitch"]) > 0.75: return False, "pitch out of range"
        if "yaw"   in kw and abs(kw["yaw"])   > 1.5:  return False, "yaw out of range"
        return True, ""

    @property
    def violation_count(self) -> int:
        return self._violations

    def _block(self, msg: str) -> bool:
        self._violations += 1
        logger.warning("SafetyGate [#%d] blocked: %s", self._violations, msg)
        return False
