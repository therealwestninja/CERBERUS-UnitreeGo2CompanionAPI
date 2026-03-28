"""
plugins/stair_climber/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS StairClimber Plugin — v1.1.0

Detects stair traversal via proprioceptive signal analysis, applies a safe
gait profile, and recovers autonomously when a foot catches on an uneven step.

──────────────────────────────────────────────────────────────────────────────
Stair detection fingerprint (three correlated signals)
──────────────────────────────────────────────────────────────────────────────
  1. Front-rear asymmetry variance — oscillates at gait frequency on stairs
  2. Pitch oscillation amplitude   — body rocks ±5–12° per step pair
  3. Diagonal alternation index    — FL+RR vs FR+RL systematic alternation

All three must be elevated simultaneously. Distinguishes stairs from:
  ROUGH terrain  — random variance, aperiodic asymmetry
  INCLINE        — sustained pitch, low oscillation
  LATERAL_SLOPE  — roll dominates

──────────────────────────────────────────────────────────────────────────────
Foot-catch / snag compensation  ◄── v1.1
──────────────────────────────────────────────────────────────────────────────
Uneven stairs (damaged risers, bullnose chips, wet stone) cause feet to catch
in three distinct failure modes:

  FORCE_SPIKE     — foot hits riser mid-swing
                    Signal: foot force > 3× rolling baseline AND delta > 40 N
                            AND velocity drops simultaneously

  VELOCITY_STALL  — robot stalls without detectable impact
                    Signal: mean_vx over 6 ticks < 35% of recent baseline

  TORQUE_SPIKE    — hip/knee torque fighting a stuck foot
                    Signal: any hip_flex or knee joint > 3.2× its baseline

Recovery sequence (all three modes, 5 phases):
  HALTING  → stop_move()
  MICRO_LIFT → body_height += 4 mm  (relieves catching foot load)
  ADAPTING   → foot_raise_height += RAISE_INCREMENT (adaptive ratchet)
  PAUSING    → hold 0.30 s
  RESUMING   → restore height, re-tighten velocity limit × speed_fraction

Adaptive ratchet (AdaptiveFootRaise):
  • On each snag: +12 mm  (fast — corrects immediately)
  • After 120 clear ticks (~2 s): −0.08 mm/tick decay
  • Hard cap: 130 mm (~72% of standard 180 mm riser)
  • Reset to baseline gait profile value on each new stair flight entry

Speed fraction after snags:
  1st snag: resume at 85% of stair speed cap
  2nd snag: resume at 75%
  3rd+:     resume at 65%  (maximum caution)
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
# Physical constants
# ─────────────────────────────────────────────────────────────────────────────

ROBOT_WEIGHT_N = 147.0           # 15 kg × 9.81 m/s²
STATIC_LOAD_N  = ROBOT_WEIGHT_N / 4   # ≈ 36.75 N per foot at rest

# Joint indices in the 12-DOF array for snag-sensitive joints
HIP_FLEX_IDX = [1, 4, 7, 10]    # FL/FR/RL/RR hip flexor
KNEE_IDX     = [2, 5, 8, 11]    # FL/FR/RL/RR knee
LEG_NAMES    = ["FL", "FR", "RL", "RR"]


# ─────────────────────────────────────────────────────────────────────────────
# Stair detection window
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StairSample:
    timestamp:  float
    foot_force: list[float]   # [FL, FR, RL, RR] N
    pitch_rad:  float
    vx:         float


class StairWindow:
    """Rolling window with stair-specific feature extraction."""

    def __init__(self, max_size: int = 80):
        self._buf: collections.deque[StairSample] = collections.deque(maxlen=max_size)

    def push(self, s: StairSample) -> None:
        self._buf.append(s)

    def __len__(self) -> int:
        return len(self._buf)

    def is_ready(self, min_samples: int = 30) -> bool:
        return len(self._buf) >= min_samples

    def asym_series(self) -> list[float]:
        out = []
        for s in self._buf:
            total = sum(s.foot_force) or 1.0
            out.append((s.foot_force[0] + s.foot_force[1]
                        - s.foot_force[2] - s.foot_force[3]) / total)
        return out

    def asym_variance(self) -> float:
        a = self.asym_series()
        if not a:
            return 0.0
        mean = sum(a) / len(a)
        return sum((x - mean) ** 2 for x in a) / len(a)

    def asym_dir_changes(self) -> int:
        a = self.asym_series()
        return sum(1 for i in range(1, len(a)) if a[i] * a[i - 1] < 0)

    def peak_asym(self) -> float:
        a = self.asym_series()
        return max(abs(x) for x in a) if a else 0.0

    def diagonal_alternation(self) -> float:
        if len(self._buf) < 4:
            return 0.0
        dom = [(s.foot_force[0] + s.foot_force[3]) >
               (s.foot_force[1] + s.foot_force[2]) for s in self._buf]
        flips = sum(1 for i in range(1, len(dom)) if dom[i] != dom[i - 1])
        return flips / max(1, len(dom) - 1)

    def pitch_range(self) -> float:
        if not self._buf:
            return 0.0
        pitches = [s.pitch_rad for s in self._buf]
        return max(pitches) - min(pitches)

    def mean_pitch_deg(self) -> float:
        if not self._buf:
            return 0.0
        return math.degrees(sum(s.pitch_rad for s in self._buf) / len(self._buf))

    def mean_speed(self) -> float:
        if not self._buf:
            return 0.0
        return sum(abs(s.vx) for s in self._buf) / len(self._buf)

    def snapshot(self) -> dict:
        return {
            "samples":              len(self._buf),
            "asym_variance":        round(self.asym_variance(), 4),
            "asym_dir_changes":     self.asym_dir_changes(),
            "peak_asym":            round(self.peak_asym(), 3),
            "pitch_range_deg":      round(math.degrees(self.pitch_range()), 2),
            "diagonal_alternation": round(self.diagonal_alternation(), 3),
            "mean_pitch_deg":       round(self.mean_pitch_deg(), 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Stair classifier
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StairThresholds:
    asym_variance_min:    float = 0.018
    dir_changes_min:      int   = 3
    dir_changes_max:      int   = 18
    pitch_range_min_rad:  float = 0.08
    peak_asym_min:        float = 0.25
    diagonal_alt_min:     float = 0.30
    min_speed_ms:         float = 0.05
    confirm_ticks:        int   = 20
    exit_ticks:           int   = 30


class StairClassifier:
    def __init__(self, thresholds: StairThresholds | None = None):
        self.t = thresholds or StairThresholds()

    def score(self, window: StairWindow) -> float:
        if not window.is_ready() or window.mean_speed() < self.t.min_speed_ms:
            return 0.0
        av = window.asym_variance()
        dc = window.asym_dir_changes()
        pr = window.pitch_range()
        pa = window.peak_asym()
        da = window.diagonal_alternation()

        s_av = min(1.0, av / (self.t.asym_variance_min  * 2))
        s_pr = min(1.0, pr / (self.t.pitch_range_min_rad * 2))
        s_pa = min(1.0, pa / (self.t.peak_asym_min       * 2))
        s_da = min(1.0, da / (self.t.diagonal_alt_min    * 2))

        if dc < self.t.dir_changes_min:
            s_dc = 0.0
        elif dc > self.t.dir_changes_max:
            s_dc = max(0.0, 1.0 - (dc - self.t.dir_changes_max) / self.t.dir_changes_max)
        else:
            s_dc = 1.0

        product = s_av * s_pr * s_pa * s_da * s_dc
        return product ** 0.2

    def direction(self, window: StairWindow) -> str:
        p = window.mean_pitch_deg()
        if p < -4.0:
            return "ascending"
        if p >  4.0:
            return "descending"
        return "level"


# ─────────────────────────────────────────────────────────────────────────────
# Snag detection
# ─────────────────────────────────────────────────────────────────────────────

class SnagType(str, Enum):
    FORCE_SPIKE    = "force_spike"     # foot impact on riser/edge
    VELOCITY_STALL = "velocity_stall"  # forward progress lost
    TORQUE_SPIKE   = "torque_spike"    # joint fighting resistance


@dataclass
class SnagEvent:
    snag_type:     SnagType
    leg:           str
    force_n:       float
    velocity_drop: float
    timestamp:     float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type":          self.snag_type.value,
            "leg":           self.leg,
            "force_n":       round(self.force_n, 1),
            "velocity_drop": round(self.velocity_drop, 3),
            "timestamp":     self.timestamp,
        }


class FootForceWindow:
    """
    Per-foot exponential mean baseline + spike detector.

    Slow EMA (α=0.05) tracks the expected gait load.
    Current force / EMA baseline = spike ratio.
    """
    _ALPHA = 0.05

    def __init__(self):
        self._mean: list[float] = [STATIC_LOAD_N] * 4
        self._prev: list[float] = [STATIC_LOAD_N] * 4

    def update(self, forces: list[float]) -> None:
        for i in range(4):
            self._prev[i] = forces[i]
            self._mean[i] = (1 - self._ALPHA) * self._mean[i] + self._ALPHA * forces[i]

    def baseline(self, i: int) -> float:
        return max(1.0, self._mean[i])

    def spike_ratio(self, forces: list[float]) -> tuple[float, int]:
        ratios = [forces[i] / self.baseline(i) for i in range(4)]
        idx    = max(range(4), key=lambda k: ratios[k])
        return ratios[idx], idx

    def delta(self, forces: list[float]) -> list[float]:
        return [forces[i] - self._prev[i] for i in range(4)]


class VelocityWindow:
    """
    Rolling velocity tracker for stall detection.
    Uses upper-70th-percentile as the 'expected' speed baseline so that
    transient stalls don't corrupt the reference.
    """
    def __init__(self, size: int = 30, stall_size: int = 6):
        self._buf:   collections.deque[float] = collections.deque(maxlen=size)
        self._short: collections.deque[float] = collections.deque(maxlen=stall_size)

    def update(self, vx: float) -> None:
        v = abs(vx)
        self._buf.append(v)
        self._short.append(v)

    def recent_mean(self) -> float:
        if not self._buf:
            return 0.0
        s = sorted(self._buf)
        upper = s[int(len(s) * 0.70):]
        return sum(upper) / len(upper) if upper else 0.0

    def current_mean(self) -> float:
        if not self._short:
            return 0.0
        return sum(self._short) / len(self._short)

    def stall_fraction(self) -> float:
        base = self.recent_mean()
        return 1.0 if base < 0.05 else self.current_mean() / base


class TorqueWindow:
    """Per-joint EMA baseline for torque spike detection (hip_flex + knee only).

    Initialised to a realistic standing torque (~4 N·m) so the first
    non-zero reading does not trigger a false spike against a zero baseline.
    """
    _ALPHA        = 0.04
    _INITIAL_MEAN = 4.0   # N·m — typical Go2 hip/knee at static stand load

    def __init__(self):
        self._mean: list[float] = [self._INITIAL_MEAN] * 12

    def update(self, torques: list[float]) -> None:
        for i in range(min(12, len(torques))):
            self._mean[i] = ((1 - self._ALPHA) * self._mean[i]
                             + self._ALPHA * abs(torques[i]))

    def spike_ratio(self, torques: list[float]) -> tuple[float, int]:
        relevant = HIP_FLEX_IDX + KNEE_IDX
        ratios = [abs(torques[i]) / max(0.5, self._mean[i])
                  for i in relevant if i < len(torques)]
        if not ratios:
            return 0.0, 0
        best = max(range(len(ratios)), key=lambda k: ratios[k])
        return ratios[best], relevant[best]


@dataclass
class SnagDetectorConfig:
    force_spike_ratio:        float = 3.0
    force_delta_min_n:        float = 40.0
    stall_fraction_threshold: float = 0.35
    torque_spike_ratio:       float = 3.2
    min_speed_ms:             float = 0.05
    stall_confirm_ticks:      int   = 6
    cooldown_ticks:           int   = 20   # refractory ticks after each event


class SnagDetector:
    """
    Three-channel foot-catch detector.
    Returns a SnagEvent on detection, None otherwise.
    """

    def __init__(self, config: SnagDetectorConfig | None = None):
        self.cfg          = config or SnagDetectorConfig()
        self.force_win    = FootForceWindow()
        self.velocity_win = VelocityWindow()
        self.torque_win   = TorqueWindow()
        self._stall_ticks = 0
        self._cooldown    = 0

    def update(self, state: "RobotState") -> SnagEvent | None:
        forces  = list(state.foot_force[:4]) if len(state.foot_force) >= 4 else [0.0]*4
        torques = list(state.joint_torques) if state.joint_torques else []
        vx      = state.velocity_x

        # Compute force delta BEFORE updating baselines so _prev reflects
        # the previous tick — not the current one (which would give Δ=0).
        deltas = self.force_win.delta(forces)

        # Now advance all baselines
        self.force_win.update(forces)
        self.velocity_win.update(vx)
        self.torque_win.update(torques)

        if self._cooldown > 0:
            self._cooldown -= 1
            return None

        if abs(vx) < self.cfg.min_speed_ms:
            self._stall_ticks = 0
            return None

        # ── Channel 1: Force spike ────────────────────────────────────────────
        ratio, leg_idx = self.force_win.spike_ratio(forces)
        # deltas computed above (before baseline update)
        if (ratio >= self.cfg.force_spike_ratio
                and deltas[leg_idx] >= self.cfg.force_delta_min_n):
            base     = self.velocity_win.recent_mean()
            vdrop    = max(0.0, 1.0 - (abs(vx) / base)) if base > 0.01 else 0.0
            event    = SnagEvent(
                snag_type=SnagType.FORCE_SPIKE,
                leg=LEG_NAMES[leg_idx],
                force_n=forces[leg_idx],
                velocity_drop=vdrop,
            )
            self._cooldown    = self.cfg.cooldown_ticks
            self._stall_ticks = 0
            return event

        # ── Channel 2: Velocity stall ─────────────────────────────────────────
        stall_frac = self.velocity_win.stall_fraction()
        if stall_frac < self.cfg.stall_fraction_threshold:
            self._stall_ticks += 1
            if self._stall_ticks >= self.cfg.stall_confirm_ticks:
                event = SnagEvent(
                    snag_type=SnagType.VELOCITY_STALL,
                    leg="unknown",
                    force_n=max(forces),
                    velocity_drop=1.0 - stall_frac,
                )
                self._cooldown    = self.cfg.cooldown_ticks + 10
                self._stall_ticks = 0
                return event
        else:
            self._stall_ticks = max(0, self._stall_ticks - 1)

        # ── Channel 3: Torque spike ───────────────────────────────────────────
        if torques:
            t_ratio, t_idx = self.torque_win.spike_ratio(torques)
            if t_ratio >= self.cfg.torque_spike_ratio:
                leg_idx = min(t_idx // 3, 3)
                event   = SnagEvent(
                    snag_type=SnagType.TORQUE_SPIKE,
                    leg=LEG_NAMES[leg_idx],
                    force_n=forces[leg_idx],
                    velocity_drop=1.0 - stall_frac,
                )
                self._cooldown = self.cfg.cooldown_ticks
                return event

        return None

    def reset(self) -> None:
        self._stall_ticks = 0
        self._cooldown    = 0


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive foot raise ratchet
# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveFootRaise:
    """
    Ratchets foot raise up on each snag, decays slowly during clean traversal.

    On each snag event:         current_m += RAISE_INCREMENT_M  (fast)
    After CLEAR_TICKS_BEFORE_DECAY clean ticks:
                                current_m -= DECAY_RATE_M_PER_TICK (slow)
    Hard cap:                   MAX_FOOT_RAISE_M = 0.130 m
    """

    RAISE_INCREMENT_M:        float = 0.012
    DECAY_RATE_M_PER_TICK:    float = 0.00008   # ≈ 6 mm / minute at 60 Hz
    CLEAR_TICKS_BEFORE_DECAY: int   = 120        # 2 s
    MAX_FOOT_RAISE_M:         float = 0.130

    def __init__(self, baseline_m: float = 0.08):
        self.baseline_m   = baseline_m
        self.current_m    = baseline_m
        self._clear_ticks = 0
        self._total_snags = 0

    def on_snag(self) -> float:
        self.current_m    = min(self.MAX_FOOT_RAISE_M,
                                self.current_m + self.RAISE_INCREMENT_M)
        self._clear_ticks = 0
        self._total_snags += 1
        logger.info("[AdaptiveFootRaise] Snag #%d → %.0f mm",
                    self._total_snags, self.current_m * 1000)
        return self.current_m

    def on_clear_tick(self) -> float:
        self._clear_ticks += 1
        if self._clear_ticks > self.CLEAR_TICKS_BEFORE_DECAY:
            self.current_m = max(self.baseline_m,
                                 self.current_m - self.DECAY_RATE_M_PER_TICK)
        return self.current_m

    def reset(self, baseline_m: float | None = None) -> None:
        if baseline_m is not None:
            self.baseline_m = baseline_m
        self.current_m    = self.baseline_m
        self._clear_ticks = 0

    def to_dict(self) -> dict:
        return {
            "current_mm":  round(self.current_m * 1000, 1),
            "baseline_mm": round(self.baseline_m * 1000, 1),
            "max_mm":      round(self.MAX_FOOT_RAISE_M * 1000, 1),
            "clear_ticks": self._clear_ticks,
            "total_snags": self._total_snags,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Recovery state machine
# ─────────────────────────────────────────────────────────────────────────────

class RecoveryPhase(str, Enum):
    IDLE       = "idle"
    HALTING    = "halting"      # stop_move() issued
    MICRO_LIFT = "micro_lift"   # body_height +4 mm
    ADAPTING   = "adapting"     # set_foot_raise_height with ratcheted value
    PAUSING    = "pausing"      # hold RECOVERY_PAUSE_S
    RESUMING   = "resuming"     # restore height, tighten velocity


RECOVERY_BODY_LIFT_M = 0.004    # 4 mm micro-lift
RECOVERY_PAUSE_S     = 0.30     # hold duration after lift


@dataclass
class RecoveryState:
    phase:             RecoveryPhase = RecoveryPhase.IDLE
    phase_entered_at:  float         = 0.0
    consecutive_snags: int           = 0
    pre_snag_height:   float         = 0.27
    event:             SnagEvent | None = None

    @property
    def speed_fraction(self) -> float:
        """Decreasing resume speed after repeated snags."""
        return max(0.65, 0.85 - 0.10 * max(0, self.consecutive_snags - 1))

    def to_dict(self) -> dict:
        return {
            "phase":             self.phase.value,
            "consecutive_snags": self.consecutive_snags,
            "speed_fraction":    round(self.speed_fraction, 2),
            "event":             self.event.to_dict() if self.event else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Main FSM state
# ─────────────────────────────────────────────────────────────────────────────

class StairState(str, Enum):
    NOMINAL      = "nominal"
    STAIR_ACTIVE = "stair_active"
    RECOVERING   = "recovering"
    EXITING      = "exiting"


@dataclass
class StairStatus:
    state:           StairState = StairState.NOMINAL
    direction:       str        = "unknown"
    score:           float      = 0.0
    confirm_streak:  int        = 0
    exit_streak:     int        = 0
    entered_at:      float      = 0.0
    step_count:      int        = 0
    snag_count:      int        = 0
    window_snapshot: dict       = field(default_factory=dict)
    recovery:        dict       = field(default_factory=dict)
    adaptive_raise:  dict       = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state":       self.state.value,
            "direction":   self.direction,
            "score":       round(self.score, 3),
            "confirm_streak": self.confirm_streak,
            "active_duration_s": (
                round(time.monotonic() - self.entered_at, 1)
                if self.state != StairState.NOMINAL else 0.0
            ),
            "step_count":  self.step_count,
            "snag_count":  self.snag_count,
            "window":      self.window_snapshot,
            "recovery":    self.recovery,
            "adaptive_raise": self.adaptive_raise,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Gait profiles
# ─────────────────────────────────────────────────────────────────────────────

STAIR_GAIT_PROFILE = {
    "ascending":  {"gait_id": 3, "foot_raise_m": 0.080, "max_vx": 0.25, "max_vy": 0.10},
    "descending": {"gait_id": 3, "foot_raise_m": 0.060, "max_vx": 0.20, "max_vy": 0.08},
    "level":      {"gait_id": 3, "foot_raise_m": 0.070, "max_vx": 0.22, "max_vy": 0.09},
}


# ─────────────────────────────────────────────────────────────────────────────
# Plugin
# ─────────────────────────────────────────────────────────────────────────────

class StairClimberPlugin(CerberusPlugin):
    """
    Stair traversal detection + safe gait + foot-catch compensation.

    Engine hook priority 70 — runs AFTER TerrainArbiter (default 100)
    so this plugin can override any terrain-driven gait switch every tick.
    """

    MANIFEST = PluginManifest(
        name         = "stair_climber",
        version      = "1.1.0",
        description  = "Stair detection, safe traversal, and foot-catch compensation",
        author       = "CERBERUS",
        trust        = TrustLevel.TRUSTED,
        capabilities = {"read_state", "control_motion", "control_gait",
                        "control_led", "publish_events", "modify_safety_limits"},
    )

    HOOK_PRIORITY    = 110   # runs AFTER TerrainArbiter (default 100) to override its gait
    DETECT_THRESHOLD = 0.45
    EXIT_THRESHOLD   = 0.25

    def __init__(self, engine: "CerberusEngine"):
        super().__init__(engine)
        self._window     = StairWindow(max_size=80)
        self._classifier = StairClassifier()
        self._thresholds = StairThresholds()
        self._status     = StairStatus()

        self._snag_detector  = SnagDetector()
        self._adaptive_raise = AdaptiveFootRaise(baseline_m=0.08)
        self._recovery       = RecoveryState()

        self._pre_stair_max_vx: float = 1.5
        self._pre_stair_max_vy: float = 0.8

        self._last_asym_sign:  int = 0
        self._step_half_count: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def on_load(self) -> None:
        logger.info("[StairClimber] v1.1 loaded — detection + snag compensation active")

    async def on_unload(self) -> None:
        if self._status.state != StairState.NOMINAL:
            await self._restore_pre_stair(force=True)

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def on_tick(self, tick: int) -> None:
        state = await self.bridge.get_state()

        if state.estop_active:
            if self._status.state != StairState.NOMINAL:
                await self._restore_pre_stair(force=True)
            return

        # Feed window
        self._window.push(StairSample(
            timestamp  = time.time(),
            foot_force = list(state.foot_force[:4]) if len(state.foot_force) >= 4 else [0.0]*4,
            pitch_rad  = state.pitch,
            vx         = state.velocity_x,
        ))

        score     = self._classifier.score(self._window)
        direction = self._classifier.direction(self._window)
        self._status.score     = score
        self._status.direction = direction

        s = self._status.state

        if s == StairState.NOMINAL:
            await self._tick_nominal(score, direction)

        elif s == StairState.STAIR_ACTIVE:
            await self._tick_active(score, direction, state)

        elif s == StairState.RECOVERING:
            await self._tick_recovery(state)

        elif s == StairState.EXITING:
            if score >= self.DETECT_THRESHOLD:
                logger.info("[StairClimber] Score recovered during exit — back to ACTIVE")
                self._status.state      = StairState.STAIR_ACTIVE
                self._status.exit_streak = 0
            else:
                await self._restore_pre_stair()

        # Broadcast status at ~5 Hz
        if tick % 12 == 0:
            self._status.window_snapshot = self._window.snapshot()
            self._status.recovery        = self._recovery.to_dict()
            self._status.adaptive_raise  = self._adaptive_raise.to_dict()
            await self.engine.bus.publish("stair.status", self._status.to_dict())

    # ── FSM: NOMINAL ──────────────────────────────────────────────────────────

    async def _tick_nominal(self, score: float, direction: str) -> None:
        if score >= self.DETECT_THRESHOLD:
            self._status.confirm_streak += 1
            if self._status.confirm_streak >= self._thresholds.confirm_ticks:
                await self._enter_stair(direction)
        else:
            self._status.confirm_streak = max(0, self._status.confirm_streak - 1)

    # ── FSM: STAIR_ACTIVE ─────────────────────────────────────────────────────

    async def _tick_active(
        self, score: float, direction: str, state: "RobotState"
    ) -> None:
        # Snag detection — highest priority
        snag = self._snag_detector.update(state)
        if snag:
            await self._enter_recovery(snag, state)
            return

        # Clean tick: decay foot raise and re-enforce stair gait
        new_raise = self._adaptive_raise.on_clear_tick()
        await self._enforce_stair_gait(direction, foot_raise_override=new_raise)
        self._count_step()

        # Exit detection
        if score < self.EXIT_THRESHOLD:
            self._status.exit_streak += 1
            if self._status.exit_streak >= self._thresholds.exit_ticks:
                await self._begin_exit()
        else:
            self._status.exit_streak = max(0, self._status.exit_streak - 2)

    # ── FSM: RECOVERING ───────────────────────────────────────────────────────

    async def _tick_recovery(self, state: "RobotState") -> None:
        rec   = self._recovery
        now   = time.monotonic()
        dt    = now - rec.phase_entered_at

        if rec.phase == RecoveryPhase.HALTING:
            # stop_move was already issued on entry; advance immediately
            rec.phase            = RecoveryPhase.MICRO_LIFT
            rec.phase_entered_at = now

        elif rec.phase == RecoveryPhase.MICRO_LIFT:
            # Pass as relative offset (+4 mm lift from current neutral position)
            await self.bridge.set_body_height(RECOVERY_BODY_LIFT_M)
            rec.phase            = RecoveryPhase.ADAPTING
            rec.phase_entered_at = now

        elif rec.phase == RecoveryPhase.ADAPTING:
            new_raise = self._adaptive_raise.on_snag()
            await self.bridge.set_foot_raise_height(new_raise)
            self._status.snag_count  += 1
            rec.consecutive_snags    += 1

            await self.engine.bus.publish("stair.snag_compensated", {
                "snag":             rec.event.to_dict() if rec.event else {},
                "new_foot_raise_mm": round(new_raise * 1000, 1),
                "consecutive":      rec.consecutive_snags,
                "speed_fraction":   rec.speed_fraction,
            })
            logger.warning(
                "[StairClimber] 🦶 SNAG compensated — type=%s leg=%s "
                "foot_raise=%.0fmm resume_speed=×%.0f%%",
                rec.event.snag_type.value if rec.event else "?",
                rec.event.leg            if rec.event else "?",
                new_raise * 1000,
                rec.speed_fraction * 100,
            )
            rec.phase            = RecoveryPhase.PAUSING
            rec.phase_entered_at = now

        elif rec.phase == RecoveryPhase.PAUSING:
            if dt >= RECOVERY_PAUSE_S:
                rec.phase            = RecoveryPhase.RESUMING
                rec.phase_entered_at = now

        elif rec.phase == RecoveryPhase.RESUMING:
            # Return to neutral offset (0.0 = SDK default standing height)
            await self.bridge.set_body_height(0.0)

            # Tighten velocity cap by speed_fraction for this snag severity
            if self.engine.watchdog:
                profile = STAIR_GAIT_PROFILE.get(
                    self._status.direction, STAIR_GAIT_PROFILE["level"]
                )
                from cerberus.core.safety import SafetyLimits
                lim = self.engine.watchdog.limits
                self.engine.watchdog.limits = SafetyLimits(
                    max_vx   = min(lim.max_vx, profile["max_vx"] * rec.speed_fraction),
                    max_vy   = min(lim.max_vy, profile["max_vy"] * rec.speed_fraction),
                    max_vyaw = lim.max_vyaw,
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

            rec.phase              = RecoveryPhase.IDLE
            self._status.state     = StairState.STAIR_ACTIVE
            self._snag_detector.reset()

    # ── Transition helpers ────────────────────────────────────────────────────

    async def _enter_stair(self, direction: str) -> None:
        if self.engine.watchdog:
            lim = self.engine.watchdog.limits
            self._pre_stair_max_vx = lim.max_vx
            self._pre_stair_max_vy = lim.max_vy

        profile = STAIR_GAIT_PROFILE.get(direction, STAIR_GAIT_PROFILE["level"])

        self._status.state          = StairState.STAIR_ACTIVE
        self._status.entered_at     = time.monotonic()
        self._status.confirm_streak = 0
        self._status.step_count     = 0
        self._status.snag_count     = 0
        self._step_half_count       = 0

        self._adaptive_raise.reset(baseline_m=profile["foot_raise_m"])
        self._snag_detector.reset()
        self._recovery = RecoveryState()

        if self.engine.watchdog:
            from cerberus.core.safety import SafetyLimits
            lim = self.engine.watchdog.limits
            self.engine.watchdog.limits = SafetyLimits(
                max_vx   = min(lim.max_vx,   profile["max_vx"]),
                max_vy   = min(lim.max_vy,   profile["max_vy"]),
                max_vyaw = lim.max_vyaw,
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

        await self.switch_gait(profile["gait_id"])
        await self.set_foot_raise_height(profile["foot_raise_m"])
        await self.set_led(0, 80, 255)

        await self.engine.bus.publish("stair.detected", {
            "direction": direction, "profile": profile,
            "score": round(self._status.score, 3),
        })
        await self.engine.bus.publish("stair.active", True)

        logger.info(
            "[StairClimber] 🪜 STAIR DETECTED — %s  score=%.2f  "
            "gait=%d  foot_raise=%.0fmm  vx≤%.2f",
            direction, self._status.score,
            profile["gait_id"], profile["foot_raise_m"] * 1000, profile["max_vx"]
        )

    async def _enter_recovery(self, snag: SnagEvent, state: "RobotState") -> None:
        await self.bridge.stop_move()
        self._status.state          = StairState.RECOVERING
        self._recovery.phase        = RecoveryPhase.HALTING
        self._recovery.phase_entered_at = time.monotonic()
        self._recovery.event        = snag
        self._recovery.pre_snag_height  = state.body_height
        await self.engine.bus.publish("stair.snag", snag.to_dict())

    async def _begin_exit(self) -> None:
        self._status.state = StairState.EXITING
        logger.info("[StairClimber] Score dropped — beginning exit "
                    "(steps=%d snags=%d)", self._status.step_count, self._status.snag_count)

    async def _restore_pre_stair(self, force: bool = False) -> None:
        if self.engine.watchdog:
            from cerberus.core.safety import SafetyLimits
            lim = self.engine.watchdog.limits
            self.engine.watchdog.limits = SafetyLimits(
                max_vx   = self._pre_stair_max_vx,
                max_vy   = self._pre_stair_max_vy,
                max_vyaw = lim.max_vyaw,
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

        await self.switch_gait(0)
        await self.set_foot_raise_height(0.0)
        await self.set_led(0, 0, 0)

        await self.engine.bus.publish("stair.exited", {
            "steps_taken": self._status.step_count,
            "snags":       self._status.snag_count,
            "active_s":    round(time.monotonic() - self._status.entered_at, 1),
        })
        await self.engine.bus.publish("stair.active", False)

        logger.info(
            "[StairClimber] ✅ Exit — %d steps, %d snags, %.1fs",
            self._status.step_count, self._status.snag_count,
            time.monotonic() - self._status.entered_at
        )
        self._status = StairStatus()

    async def _enforce_stair_gait(
        self, direction: str, foot_raise_override: float | None = None
    ) -> None:
        profile = STAIR_GAIT_PROFILE.get(direction, STAIR_GAIT_PROFILE["level"])
        raise_h = foot_raise_override if foot_raise_override is not None \
                  else profile["foot_raise_m"]
        await self.switch_gait(profile["gait_id"])
        await self.set_foot_raise_height(raise_h)

    def _count_step(self) -> None:
        a = self._window.asym_series()
        if len(a) >= 2:
            sign = 1 if a[-1] >= 0 else -1
            if sign != self._last_asym_sign and self._last_asym_sign != 0:
                self._step_half_count += 1
                if self._step_half_count % 2 == 0:
                    self._status.step_count += 1
            self._last_asym_sign = sign

    # ── Tune / status ─────────────────────────────────────────────────────────

    def tune(self, **kwargs) -> dict:
        for k, v in kwargs.items():
            if hasattr(self._thresholds, k):
                setattr(self._thresholds, k, v)
                logger.info("[StairClimber] Tuned stair threshold: %s=%s", k, v)
            elif hasattr(self._snag_detector.cfg, k):
                setattr(self._snag_detector.cfg, k, v)
                logger.info("[StairClimber] Tuned snag config: %s=%s", k, v)
        return {
            "stair_thresholds": self._thresholds.__dict__.copy(),
            "snag_config":      self._snag_detector.cfg.__dict__.copy(),
        }

    def status(self) -> dict:
        base = super().status()
        base.update({
            "stair":          self._status.to_dict(),
            "adaptive_raise": self._adaptive_raise.to_dict(),
            "recovery":       self._recovery.to_dict(),
        })
        return base
