"""
plugins/limb_loss_recovery/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS LimbLossRecovery Plugin — v1.0.0

Detects the loss of a single limb and activates a biomechanically-derived
tripod compensation mode so the robot can self-recover and return safely.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Biological Research Basis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Canines (veterinary literature):
  • Tripod dogs develop a "hopping canter" — 3-beat diagonal compensation.
    The diagonal partner of the missing limb carries ~40% extra load;
    the two ipsilateral (same-side) remaining limbs share ~60% of deficit.
    [Dickerson et al. 2015; Kirpensteijn et al. 1999; Torres et al. 2022]
  • COM is maintained over the support triangle via lateral trunk flexion
    and contralateral lean — not simply by limping.
  • Gait speed reduced to 35–55% of normal max in established tripods;
    acute loss reduces to ~20–30% until compensation develops.

Equines:
  • Three-legged horses show similar COM migration toward the triangle
    centroid. Fore-limb loss forces a "rocking" gait with exaggerated
    head carriage changes. Hind-limb loss produces a pelvic-drop pattern.
    [Garcia-Lopez 2022; Back et al. 1995]

Insects — ants & spiders (hexapod/octopod analogy):
  • Ants losing a middle leg: remaining adjacent legs extend stance phase
    and reduce swing velocity; coupling changes maintain alternating tripod
    coordination as far as possible.  [Grabowska et al. 2012; Wahl 2015]
  • Spiders losing one or more legs: never lift two adjacent legs
    simultaneously; asymmetric coupling preserves COM projection.
    [Araneus diadematus studies — Parry 1957, Wilson 1967]
  • Common principle across taxa: THE SUPPORT POLYGON MUST ALWAYS CONTAIN
    THE COM PROJECTION.  This is the universal gait-recovery invariant.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Biomechanical derivation for the Go2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Go2 foot positions in body frame (from URDF):
  FL: (+0.185,  +0.096)   FR: (+0.185,  −0.096)
  RL: (−0.185,  +0.096)   RR: (−0.185,  −0.096)

When one leg is lost, the support triangle centroid is at the mean of the
three remaining foot positions.  The body must be tilted (via set_euler)
so the COM projection lands on that centroid.

At nominal stance height h = 0.270 m:
  Δx_body  = h × sin(pitch)   →  required_pitch ≈ -centroid_x / h
  Δy_body  = h × sin(roll)    →  required_roll  ≈  centroid_y / h   ← sign note ①

  ① SDK roll convention: positive roll = left side up = body leans RIGHT.
     A positive centroid_y (left of centre) means we need a positive roll
     to lean right and move COM left — signs are consistent.

Missing FL (centroid: −0.062m fwd, −0.032m lat):
  pitch = +0.228 rad (+13.1°, nose up — leans COM backward)
  roll  = −0.119 rad (−6.8°, lean right — moves COM toward FR side)

Missing FR (centroid: −0.062m fwd, +0.032m lat):
  pitch = +0.228 rad (+13.1°)
  roll  = +0.119 rad (+6.8°, lean left — moves COM toward FL side)

Missing RL (centroid: +0.062m fwd, −0.032m lat):
  pitch = −0.228 rad (−13.1°, nose down — leans COM forward)
  roll  = −0.119 rad (−6.8°, lean right)

Missing RR (centroid: +0.062m fwd, +0.032m lat):
  pitch = −0.228 rad (−13.1°)
  roll  = +0.119 rad (+6.8°, lean left)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Yaw drift correction
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When moving forward, the missing limb creates an asymmetric ground
reaction force that yaws the robot toward the missing-leg side.
  Missing left limb (FL or RL) → robot yaws LEFT → apply +vyaw correction
  Missing right limb (FR or RR) → robot yaws RIGHT → apply −vyaw correction

The magnitude of the correction is proportional to forward velocity
(higher speed = more thrust asymmetry):
  vyaw_correction = yaw_scale × clamp(vx / 0.15, 0, 1)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Detection algorithm
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
In normal trot gait each foot is in swing (unloaded) for ~35% of the cycle.
A dead/broken leg shows near-zero force for >80% of samples in a rolling
window (it never loads during stance phase because it cannot bear weight).

Detection logic (per leg, per tick):
  1. Rolling 90-sample window (~1.5 s at 60 Hz)
  2. dead_fraction = fraction of samples with force < DEAD_FORCE_THRESHOLD
  3. If dead_fraction > DEAD_FRACTION_THRESHOLD (0.80):
       → SUSPECTING
  4. If SUSPECTING for CONFIRM_TICKS (60 = 1.0 s):
       → CONFIRMED / RECOVERING

Joint torques provide a secondary confirmation signal: a dead leg has
near-zero torque across all three joints because neither the gait engine
nor gravity loads them.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from cerberus.plugins.plugin_manager import (
    CerberusPlugin, PluginManifest, TrustLevel,
)

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import RobotState
    from cerberus.core.engine import CerberusEngine

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Leg index mapping
# ─────────────────────────────────────────────────────────────────────────────

LEG_NAMES  = ["FL", "FR", "RL", "RR"]   # indices 0–3
LEG_JOINTS = {                            # joint base indices (0-based)
    0: (0, 1, 2),    # FL: hip_ab, hip_flex, knee
    1: (3, 4, 5),    # FR
    2: (6, 7, 8),    # RL
    3: (9, 10, 11),  # RR
}

# Go2 foot positions in body frame (forward_m, lateral_m)
# Lateral: positive = left side
FOOT_POSITIONS = {
    0: ( 0.185,  0.096),   # FL
    1: ( 0.185, -0.096),   # FR
    2: (-0.185,  0.096),   # RL
    3: (-0.185, -0.096),   # RR
}

NOMINAL_BODY_HEIGHT = 0.270   # m — used to convert centroid offset to angle


# ─────────────────────────────────────────────────────────────────────────────
# Tripod compensation parameters
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TripodParams:
    """
    Body orientation bias and motion limits for each missing-leg scenario.

    All angles in radians.  Derived from the support-triangle centroid
    geometry described in the module docstring.
    """
    missing_leg:      int     # 0=FL 1=FR 2=RL 3=RR
    pitch_rad:        float   # body pitch bias (positive = nose up)
    roll_rad:         float   # body roll bias (positive = left side up)
    yaw_scale:        float   # vyaw added per (vx / 0.15) forward motion
    max_vx:           float   # m/s forward speed limit
    max_vy:           float   # m/s lateral speed limit
    max_vyaw:         float   # rad/s yaw speed limit
    body_h_offset:    float   # relative height offset (m, typically −0.03)
    foot_raise_m:     float   # foot raise height during tripod stance walk
    description:      str


# Biomechanically derived — see module docstring for calculation
TRIPOD_TABLE: dict[int, TripodParams] = {
    0: TripodParams(   # FL missing
        missing_leg=0,
        pitch_rad    = +0.228,    # +13.1° nose-up — shifts COM backward
        roll_rad     = -0.119,    # −6.8°  leans right — shifts COM toward FR
        yaw_scale    = +0.28,     # counteract left-yaw drift from missing left thrust
        max_vx       =  0.14,     # ~20% of normal 0.7 m/s comfortable trot
        max_vy       =  0.06,
        max_vyaw     =  0.28,
        body_h_offset= -0.030,    # lower COM for stability
        foot_raise_m =  0.080,    # high step to clear uneven surfaces
        description  = "FL lost — lean right and backward (toward FR+RL+RR triangle)",
    ),
    1: TripodParams(   # FR missing
        missing_leg=1,
        pitch_rad    = +0.228,    # +13.1° nose-up
        roll_rad     = +0.119,    # +6.8°  leans left — shifts COM toward FL
        yaw_scale    = -0.28,     # counteract right-yaw drift
        max_vx       =  0.14,
        max_vy       =  0.06,
        max_vyaw     =  0.28,
        body_h_offset= -0.030,
        foot_raise_m =  0.080,
        description  = "FR lost — lean left and backward (toward FL+RL+RR triangle)",
    ),
    2: TripodParams(   # RL missing
        missing_leg=2,
        pitch_rad    = -0.228,    # −13.1° nose-down — shifts COM forward
        roll_rad     = -0.119,    # −6.8°  leans right
        yaw_scale    = +0.28,
        max_vx       =  0.14,
        max_vy       =  0.06,
        max_vyaw     =  0.28,
        body_h_offset= -0.030,
        foot_raise_m =  0.080,
        description  = "RL lost — lean right and forward (toward FL+FR+RR triangle)",
    ),
    3: TripodParams(   # RR missing
        missing_leg=3,
        pitch_rad    = -0.228,    # −13.1° nose-down
        roll_rad     = +0.119,    # +6.8°  leans left
        yaw_scale    = -0.28,
        max_vx       =  0.14,
        max_vy       =  0.06,
        max_vyaw     =  0.28,
        body_h_offset= -0.030,
        foot_raise_m =  0.080,
        description  = "RR lost — lean left and forward (toward FL+FR+RL triangle)",
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Limb detector
# ─────────────────────────────────────────────────────────────────────────────

# Force below this is treated as "not bearing weight" (N)
DEAD_FORCE_THRESHOLD:    float = 8.0
# Fraction of window samples below threshold to classify leg as dead
DEAD_FRACTION_THRESHOLD: float = 0.80
# Number of ticks the leg must appear dead before we SUSPECT
SUSPECT_TICKS:           int   = 30    # 0.5 s at 60 Hz
# Additional ticks above SUSPECT before we CONFIRM
CONFIRM_TICKS:           int   = 60    # 1.0 s at 60 Hz
# Fraction of window below threshold to automatically clear (leg recovers)
RECOVERY_FRACTION:       float = 0.30
# Window size for rolling force analysis
WINDOW_SIZE:             int   = 90    # 1.5 s at 60 Hz


class LimbDetector:
    """
    Per-leg rolling foot-force analyser.

    For each of the four legs, maintains a rolling window and computes
    the fraction of samples showing near-zero force.  Normal trot swing
    phase produces ~0.35 dead fraction; a truly non-functional leg
    approaches 1.0.
    """

    def __init__(self):
        self._windows: list[collections.deque[float]] = [
            collections.deque(maxlen=WINDOW_SIZE) for _ in range(4)
        ]
        self._suspect_ticks: list[int]  = [0] * 4
        self._confirmed:     list[bool] = [False] * 4

    def update(self, foot_forces: list[float]) -> list[float]:
        """
        Push new foot forces. Returns dead_fraction for each leg [0, 1].
        """
        fractions = []
        for i in range(4):
            f = foot_forces[i] if i < len(foot_forces) else 0.0
            self._windows[i].append(f)
            w = self._windows[i]
            if len(w) < 5:
                fractions.append(0.0)
            else:
                dead = sum(1 for x in w if x < DEAD_FORCE_THRESHOLD)
                fractions.append(dead / len(w))
        return fractions

    def evaluate(
        self, fractions: list[float]
    ) -> tuple[int | None, str]:
        """
        Run the confirm state machine.

        Returns (leg_idx, event) where event is one of:
          "confirmed"  — leg newly confirmed dead
          "cleared"    — previously confirmed leg now loading normally
          None         — no transition
        """
        for i in range(4):
            frac = fractions[i]

            if not self._confirmed[i]:
                # Not yet confirmed — looking for deadness
                if frac >= DEAD_FRACTION_THRESHOLD:
                    self._suspect_ticks[i] += 1
                    if self._suspect_ticks[i] >= SUSPECT_TICKS + CONFIRM_TICKS:
                        self._confirmed[i] = True
                        self._suspect_ticks[i] = 0
                        return i, "confirmed"
                else:
                    self._suspect_ticks[i] = max(0, self._suspect_ticks[i] - 1)
            else:
                # Already confirmed — watch for natural recovery
                if frac < RECOVERY_FRACTION:
                    self._confirmed[i] = False
                    self._suspect_ticks[i] = 0
                    return i, "cleared"

        return None, ""

    def snapshot(self) -> dict:
        return {
            LEG_NAMES[i]: {
                "suspect_ticks":  self._suspect_ticks[i],
                "confirmed_dead": self._confirmed[i],
            }
            for i in range(4)
        }


# ─────────────────────────────────────────────────────────────────────────────
# FSM state
# ─────────────────────────────────────────────────────────────────────────────

class LimbLossState(str, Enum):
    NOMINAL    = "nominal"      # all four legs functional
    SUSPECTING = "suspecting"   # one leg showing low force — monitoring
    RECOVERING = "recovering"   # limb confirmed lost — tripod mode active
    CLEARED    = "cleared"      # leg recovered or manually cleared


@dataclass
class LimbLossStatus:
    state:          LimbLossState = LimbLossState.NOMINAL
    missing_leg:    int | None    = None   # 0–3, or None
    missing_name:   str           = ""     # "FL", "FR", "RL", "RR"
    detected_at:    float         = 0.0
    manual_declare: bool          = False  # set when declared via API

    # Per-leg dead fractions (snapshot for telemetry)
    leg_fractions: list[float]    = field(default_factory=lambda: [0.0]*4)
    tripod_params: dict           = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state":          self.state.value,
            "missing_leg":    self.missing_name or None,
            "manual_declare": self.manual_declare,
            "active_s": (
                round(time.monotonic() - self.detected_at, 1)
                if self.detected_at else 0.0
            ),
            "leg_fractions": {
                LEG_NAMES[i]: round(self.leg_fractions[i], 3)
                for i in range(4)
            },
            "tripod_params":  self.tripod_params,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Plugin
# ─────────────────────────────────────────────────────────────────────────────

class LimbLossRecoveryPlugin(CerberusPlugin):
    """
    Detects single-limb loss and activates a tripod-mode recovery gait.

    When a limb is confirmed lost:
      • Gait switches to stance walk (gait_id = 3)
      • Body is re-oriented via set_euler() to shift COM over the
        support triangle formed by the three remaining feet
      • Velocity limits are tightened in the safety watchdog
      • Foot raise height is increased for step clearance
      • Yaw drift is continuously corrected via move() feedback
      • LED turns amber to signal degraded operation

    The robot can still navigate at ~20% normal speed, allowing it to
    return to base under operator command.

    References
    ──────────
    Dickerson et al. (2015). Adaptations in gait of dogs with limb loss.
    Kirpensteijn et al. (1999). Three-legged dog: a study of tripod gait.
    Grabowska et al. (2012). Quadrupedal-like locomotion in ants after leg
      amputation. J Exp Biol.
    Parry (1957) / Wilson (1967) — Spider locomotion coordination studies.
    """

    MANIFEST = PluginManifest(
        name        = "limb_loss_recovery",
        version     = "1.0.0",
        description = "Tripod recovery gait for single-limb loss",
        author      = "CERBERUS",
        trust       = TrustLevel.TRUSTED,
        capabilities= {
            "read_state", "control_motion", "control_gait",
            "control_led", "publish_events", "modify_safety_limits",
        },
    )

    HOOK_PRIORITY = 120   # Runs after StairClimber (110) — highest priority override

    def __init__(self, engine: "CerberusEngine"):
        super().__init__(engine)
        self._detector = LimbDetector()
        self._status   = LimbLossStatus()

        # Pre-stair/payload saved limits for restoration
        self._saved_max_vx:  float = 1.5
        self._saved_max_vy:  float = 0.8
        self._saved_max_vyaw:float = 2.0

        # Yaw PID state
        self._yaw_integral:  float = 0.0
        self._last_yaw:      float = 0.0

        # Orientation ramp state (avoid jerky body snap)
        self._current_pitch: float = 0.0
        self._current_roll:  float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        logger.info("[LimbLoss] Plugin loaded — monitoring all four legs")

    async def on_unload(self) -> None:
        if self._status.state == LimbLossState.RECOVERING:
            await self._clear_tripod_mode(force=True)

    # ── Tick ──────────────────────────────────────────────────────────────────

    async def on_tick(self, tick: int) -> None:
        state = await self.bridge.get_state()
        if state.estop_active:
            return

        foot_forces = list(state.foot_force[:4]) if len(state.foot_force) >= 4 else [0.0]*4

        # ── Detection (always runs) ───────────────────────────────────────────
        fractions = self._detector.update(foot_forces)
        self._status.leg_fractions = fractions

        if self._status.state != LimbLossState.RECOVERING:
            leg_idx, event = self._detector.evaluate(fractions)
            if event == "confirmed":
                await self._enter_tripod_mode(leg_idx, manual=False)
            elif fractions and max(fractions) >= 0.60:
                # Pre-alert: one leg carrying very little weight
                if self._status.state == LimbLossState.NOMINAL:
                    self._status.state = LimbLossState.SUSPECTING
                    suspect_name = LEG_NAMES[fractions.index(max(fractions))]
                    logger.info("[LimbLoss] Suspecting %s loss (frac=%.2f)",
                                suspect_name, max(fractions))
            else:
                if self._status.state == LimbLossState.SUSPECTING:
                    self._status.state = LimbLossState.NOMINAL

        # ── Active compensation (when recovering) ─────────────────────────────
        if self._status.state == LimbLossState.RECOVERING:
            leg_idx, event = self._detector.evaluate(fractions)
            if event == "cleared" and not self._status.manual_declare:
                await self._clear_tripod_mode()
                return
            await self._apply_tripod_compensation(state, tick)

        # ── Broadcast status at ~5 Hz ─────────────────────────────────────────
        if tick % 12 == 0:
            await self.engine.bus.publish(
                "limb_loss.status", self._status.to_dict()
            )

    # ── Tripod mode entry / exit ──────────────────────────────────────────────

    async def _enter_tripod_mode(self, leg_idx: int, manual: bool = False) -> None:
        if self._status.state == LimbLossState.RECOVERING:
            return   # already active

        params = TRIPOD_TABLE[leg_idx]
        leg_name = LEG_NAMES[leg_idx]

        # Save current watchdog limits
        if self.engine.watchdog:
            lim = self.engine.watchdog.limits
            self._saved_max_vx   = lim.max_vx
            self._saved_max_vy   = lim.max_vy
            self._saved_max_vyaw = lim.max_vyaw

        # Update status
        self._status.state          = LimbLossState.RECOVERING
        self._status.missing_leg    = leg_idx
        self._status.missing_name   = leg_name
        self._status.detected_at    = time.monotonic()
        self._status.manual_declare = manual
        self._status.tripod_params  = {
            "pitch_deg":      round(math.degrees(params.pitch_rad), 1),
            "roll_deg":       round(math.degrees(params.roll_rad),  1),
            "yaw_scale":      params.yaw_scale,
            "max_vx_ms":      params.max_vx,
            "foot_raise_mm":  round(params.foot_raise_m * 1000),
            "description":    params.description,
        }

        # Tighten watchdog velocity limits
        if self.engine.watchdog:
            from cerberus.core.safety import SafetyLimits
            lim = self.engine.watchdog.limits
            self.engine.watchdog.limits = SafetyLimits(
                max_vx    = min(lim.max_vx,   params.max_vx),
                max_vy    = min(lim.max_vy,   params.max_vy),
                max_vyaw  = min(lim.max_vyaw, params.max_vyaw),
                max_roll_deg   = min(lim.max_roll_deg,   20.0),
                max_pitch_deg  = min(lim.max_pitch_deg,  20.0),
                min_body_height= lim.min_body_height,
                max_body_height= lim.max_body_height,
                battery_warn_pct     = lim.battery_warn_pct,
                battery_low_pct      = lim.battery_low_pct,
                battery_critical_pct = lim.battery_critical_pct,
                heartbeat_timeout_s  = lim.heartbeat_timeout_s,
                watchdog_hz          = lim.watchdog_hz,
            )

        # Apply initial gait changes
        await self.switch_gait(3)                         # stance walk — max stability
        await self.set_foot_raise_height(params.foot_raise_m)
        await self.bridge.set_body_height(params.body_h_offset)
        await self.set_led(255, 140, 0)                   # amber — degraded operation

        # Initialise orientation ramp from zero
        self._current_pitch = 0.0
        self._current_roll  = 0.0
        self._yaw_integral  = 0.0

        await self.engine.bus.publish("limb_loss.detected", {
            "leg":         leg_name,
            "manual":      manual,
            "description": params.description,
            "limits": {
                "max_vx_ms":  params.max_vx,
                "max_vy_ms":  params.max_vy,
            },
        })

        how = "MANUAL DECLARATION" if manual else "AUTO-DETECTED"
        logger.warning(
            "[LimbLoss] 🦴 %s LIMB LOSS — %s (%s)\n"
            "           Tripod mode: pitch%+.1f° roll%+.1f°  vx≤%.2f m/s  "
            "gait=3  foot_raise=%.0fmm",
            how, leg_name, params.description,
            math.degrees(params.pitch_rad), math.degrees(params.roll_rad),
            params.max_vx, params.foot_raise_m * 1000,
        )

    async def _clear_tripod_mode(self, force: bool = False) -> None:
        """Restore normal operation."""
        # Restore watchdog limits
        if self.engine.watchdog:
            from cerberus.core.safety import SafetyLimits
            lim = self.engine.watchdog.limits
            self.engine.watchdog.limits = SafetyLimits(
                max_vx    = self._saved_max_vx,
                max_vy    = self._saved_max_vy,
                max_vyaw  = self._saved_max_vyaw,
                max_roll_deg   = lim.max_roll_deg,
                max_pitch_deg  = lim.max_pitch_deg,
                min_body_height= lim.min_body_height,
                max_body_height= lim.max_body_height,
                battery_warn_pct     = lim.battery_warn_pct,
                battery_low_pct      = lim.battery_low_pct,
                battery_critical_pct = lim.battery_critical_pct,
                heartbeat_timeout_s  = lim.heartbeat_timeout_s,
                watchdog_hz          = lim.watchdog_hz,
            )

        # Restore neutral body orientation
        await self.bridge.set_euler(0.0, 0.0, 0.0)
        await self.bridge.set_body_height(0.0)
        await self.switch_gait(0)
        await self.set_foot_raise_height(0.0)
        await self.set_led(0, 0, 0)

        prev_leg = self._status.missing_name

        self._status       = LimbLossStatus()
        self._current_pitch = 0.0
        self._current_roll  = 0.0
        self._yaw_integral  = 0.0

        await self.engine.bus.publish("limb_loss.cleared", {"leg": prev_leg})
        logger.info("[LimbLoss] ✅ Tripod mode cleared — returning to normal gait")

    # ── Active tripod compensation (every tick while RECOVERING) ───────────────

    async def _apply_tripod_compensation(
        self, state: "RobotState", tick: int
    ) -> None:
        """
        Run every engine tick while in RECOVERING state.

        1. Re-enforce stance walk (gait 3) — overrides any terrain gait switch
        2. Ramp body orientation toward target pitch/roll
        3. Apply yaw drift correction proportional to forward speed
        4. Re-enforce foot raise height
        """
        leg_idx = self._status.missing_leg
        if leg_idx is None:
            return
        params = TRIPOD_TABLE[leg_idx]

        # ── 1. Re-enforce gait ───────────────────────────────────────────────
        await self.switch_gait(3)
        await self.set_foot_raise_height(params.foot_raise_m)

        # ── 2. Ramp body orientation (smooth — avoid jerky body snap) ────────
        ORIENT_RATE = 0.008   # rad/tick at 60 Hz → full angle in ~28 ticks (~0.5 s)
        target_pitch = params.pitch_rad
        target_roll  = params.roll_rad

        self._current_pitch += (
            (target_pitch - self._current_pitch) * 0.12
        )
        self._current_roll += (
            (target_roll  - self._current_roll)  * 0.12
        )

        await self.bridge.set_euler(
            self._current_roll,
            self._current_pitch,
            0.0,
        )

        # ── 3. Yaw drift correction ───────────────────────────────────────────
        # The asymmetric ground reaction force rotates the robot toward the
        # missing leg. We measure the current yaw rate and apply a corrective
        # velocity command when the robot is moving forward.
        vx    = state.velocity_x
        speed = abs(vx)

        if speed > 0.03:   # only correct when actually moving
            # Fraction of max speed (used to scale correction)
            speed_frac = min(1.0, speed / 0.15)

            # Desired yaw rate for straight-ahead travel is 0.
            # The robot's measured yaw gives us the drift to correct.
            yaw_measured = state.velocity_yaw
            yaw_target   = 0.0

            # Proportional correction
            yaw_error = yaw_target - yaw_measured
            yaw_correction = params.yaw_scale * speed_frac

            # Integrate small accumulated error (slow I term to avoid windup)
            self._yaw_integral = max(
                -0.15, min(0.15, self._yaw_integral + yaw_error * 0.005)
            )
            yaw_cmd = (
                yaw_correction * 0.6
                + yaw_error * 0.3
                + self._yaw_integral * 0.1
            )
            yaw_cmd = max(-params.max_vyaw, min(params.max_vyaw, yaw_cmd))

            # Re-issue the move command with corrected yaw
            # We preserve the current speed (don't accelerate, just steer)
            vy_clamped = max(-params.max_vy, min(params.max_vy, state.velocity_y))
            vx_clamped = max(-params.max_vx, min(params.max_vx, vx))

            await self.bridge.move(vx_clamped, vy_clamped, yaw_cmd)

    # ── External API (called from REST endpoint handler) ──────────────────────

    async def declare_limb_loss(self, leg_name: str) -> dict:
        """
        Manually declare a limb as non-functional (operator override).
        Accepts: "FL", "FR", "RL", "RR" (case-insensitive).
        """
        leg_name = leg_name.upper().strip()
        if leg_name not in LEG_NAMES:
            return {"error": f"Unknown leg '{leg_name}'. Valid: FL, FR, RL, RR"}
        if self._status.state == LimbLossState.RECOVERING:
            return {"error": f"Already in tripod mode for {self._status.missing_name}. "
                             f"Call /limb_loss/clear first."}
        leg_idx = LEG_NAMES.index(leg_name)
        await self._enter_tripod_mode(leg_idx, manual=True)
        return {"declared": leg_name, "tripod_mode": True}

    async def clear_limb_loss(self) -> dict:
        """Manually clear tripod mode and return to normal operation."""
        if self._status.state != LimbLossState.RECOVERING:
            return {"error": "Not in tripod mode"}
        await self._clear_tripod_mode(force=True)
        return {"cleared": True}

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        base = super().status()
        base.update({"limb_loss": self._status.to_dict()})
        return base
