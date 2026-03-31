"""
plugins/terrain_arbiter/plugin.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS TerrainArbiter Plugin — v1.0.0

Proprioceptive terrain classification and adaptive gait management.
Uses foot-force statistics, IMU pitch/roll, and velocity from
RobotState to classify terrain and dispatch gait/foot-raise commands.

Terrain classes:
  FLAT          — nominal walking surface, default gait
  ROUGH         — high foot-force variance, raise feet more
  SOFT          — low mean force, reduce speed (sand/carpet)
  INCLINE_UP    — sustained positive pitch, slower trot
  INCLINE_DOWN  — sustained negative pitch, slower trot
  LATERAL_SLOPE — sustained roll, lean compensation

Gait map (Unitree SDK gait IDs):
  0 = trot  (default, fastest)
  1 = slow trot
  2 = walking trot (most stable)
  3 = stance walk   (slowest, max stability)

Capabilities required:
  read_state, control_motion, publish_events

Compatible with: SimBridge + RealBridge
Min CERBERUS version: 2.1.0
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

from cerberus.plugins.plugin_manager import CerberusPlugin, PluginManifest, TrustLevel

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import RobotState

logger = logging.getLogger(__name__)


# ── Terrain classification ─────────────────────────────────────────────────────

class TerrainClass(str, Enum):
    UNKNOWN       = "unknown"
    FLAT          = "flat"
    ROUGH         = "rough"
    SOFT          = "soft"
    INCLINE_UP    = "incline_up"
    INCLINE_DOWN  = "incline_down"
    LATERAL_SLOPE = "lateral_slope"


@dataclass
class GaitProfile:
    """Commands dispatched when a terrain class is confirmed."""
    gait_id:         int   = 0     # 0–3
    foot_raise_m:    float = 0.0   # relative offset, clamped to [-0.06, 0.03]
    speed_factor:    float = 1.0   # multiplied against watchdog max_vx/vy
    description:     str   = ""

    def to_dict(self) -> dict:
        return {
            "gait_id": self.gait_id,
            "foot_raise_m": self.foot_raise_m,
            "speed_factor": self.speed_factor,
            "description": self.description,
        }


# Terrain → GaitProfile map
TERRAIN_GAIT_MAP: dict[TerrainClass, GaitProfile] = {
    TerrainClass.UNKNOWN:       GaitProfile(0, 0.0,   1.0, "default — no classification yet"),
    TerrainClass.FLAT:          GaitProfile(0, 0.0,   1.0, "normal trot"),
    TerrainClass.ROUGH:         GaitProfile(2, 0.02,  0.8, "walking trot, raised step"),
    TerrainClass.SOFT:          GaitProfile(0, 0.005, 0.7, "trot, reduced speed"),
    TerrainClass.INCLINE_UP:    GaitProfile(1, 0.01,  0.7, "slow trot, ascending"),
    TerrainClass.INCLINE_DOWN:  GaitProfile(1, 0.0,   0.6, "slow trot, descending"),
    TerrainClass.LATERAL_SLOPE: GaitProfile(1, 0.005, 0.75, "slow trot, lateral compensation"),
}


# ── Sensor window ─────────────────────────────────────────────────────────────

@dataclass
class TerrainSample:
    timestamp: float
    foot_force: list[float]   # [FL, FR, RL, RR] in N
    pitch_rad: float
    roll_rad: float
    vx: float
    vy: float


class SensorWindow:
    """
    Fixed-size rolling window of TerrainSamples.
    Provides statistical aggregates used for classification.
    """

    def __init__(self, max_size: int = 60):
        self._buf: collections.deque[TerrainSample] = collections.deque(maxlen=max_size)

    def push(self, sample: TerrainSample) -> None:
        self._buf.append(sample)

    def __len__(self) -> int:
        return len(self._buf)

    def is_ready(self, min_samples: int = 20) -> bool:
        return len(self._buf) >= min_samples

    # -- Foot force aggregates --

    def mean_total_force(self) -> float:
        """Mean sum of all four foot forces over the window."""
        if not self._buf:
            return 0.0
        return sum(sum(s.foot_force) for s in self._buf) / len(self._buf)

    def force_variance(self) -> float:
        """
        Mean of per-sample variance across the four feet.
        High variance = uneven terrain (rough/stairs).
        """
        if not self._buf:
            return 0.0
        variances = []
        for s in self._buf:
            forces = s.foot_force
            mean_f = sum(forces) / 4
            var = sum((f - mean_f) ** 2 for f in forces) / 4
            variances.append(var)
        return sum(variances) / len(variances)

    def front_rear_asymmetry(self) -> float:
        """
        Mean front-rear force imbalance (positive = front heavier = descending).
        Normalised by mean total force to be scale-independent.
        """
        if not self._buf:
            return 0.0
        asym = []
        for s in self._buf:
            total = sum(s.foot_force) or 1.0
            front = s.foot_force[0] + s.foot_force[1]  # FL + FR
            rear  = s.foot_force[2] + s.foot_force[3]  # RL + RR
            asym.append((front - rear) / total)
        return sum(asym) / len(asym)

    # -- IMU aggregates --

    def mean_pitch_deg(self) -> float:
        if not self._buf:
            return 0.0
        return math.degrees(sum(s.pitch_rad for s in self._buf) / len(self._buf))

    def mean_roll_deg(self) -> float:
        if not self._buf:
            return 0.0
        return math.degrees(sum(s.roll_rad for s in self._buf) / len(self._buf))

    def mean_speed(self) -> float:
        if not self._buf:
            return 0.0
        return sum(math.hypot(s.vx, s.vy) for s in self._buf) / len(self._buf)

    def snapshot(self) -> dict:
        return {
            "samples": len(self._buf),
            "mean_total_force_n": round(self.mean_total_force(), 1),
            "force_variance": round(self.force_variance(), 2),
            "front_rear_asymmetry": round(self.front_rear_asymmetry(), 3),
            "mean_pitch_deg": round(self.mean_pitch_deg(), 2),
            "mean_roll_deg": round(self.mean_roll_deg(), 2),
            "mean_speed_ms": round(self.mean_speed(), 3),
        }


# ── Classifier ────────────────────────────────────────────────────────────────

class TerrainClassifier:
    """
    Rule-based classifier.  All thresholds are tunable via constructor kwargs.

    Classification priority (highest to lowest):
      1. LATERAL_SLOPE — roll dominates
      2. INCLINE_UP / INCLINE_DOWN — pitch dominates
      3. ROUGH — force variance dominates
      4. SOFT — low mean force
      5. FLAT — default
    """

    def __init__(
        self,
        # IMU thresholds (degrees)
        roll_threshold_deg: float  = 8.0,
        pitch_threshold_deg: float = 6.0,
        # Force thresholds
        rough_variance_threshold: float = 400.0,   # N²
        soft_force_threshold: float     = 60.0,    # N total (< = soft)
        # Minimum speed to trust IMU-derived incline (avoids false positives at rest)
        min_speed_for_incline: float = 0.05,       # m/s
    ):
        self.roll_thresh  = roll_threshold_deg
        self.pitch_thresh = pitch_threshold_deg
        self.rough_var    = rough_variance_threshold
        self.soft_force   = soft_force_threshold
        self.min_speed    = min_speed_for_incline

    def classify(self, window: SensorWindow) -> TerrainClass:
        if not window.is_ready():
            return TerrainClass.UNKNOWN

        roll_deg  = window.mean_roll_deg()
        pitch_deg = window.mean_pitch_deg()
        variance  = window.force_variance()
        mean_force = window.mean_total_force()
        speed     = window.mean_speed()

        # 1. Lateral slope (roll dominates)
        if abs(roll_deg) > self.roll_thresh:
            return TerrainClass.LATERAL_SLOPE

        # 2. Incline — only trust when robot is actually moving
        if speed > self.min_speed:
            if pitch_deg > self.pitch_thresh:
                return TerrainClass.INCLINE_DOWN   # nose down = descending
            if pitch_deg < -self.pitch_thresh:
                return TerrainClass.INCLINE_UP     # nose up = ascending

        # 3. Rough terrain — high intra-foot variance
        if variance > self.rough_var:
            return TerrainClass.ROUGH

        # 4. Soft terrain — low total load (compliant surface absorbs force)
        if 0 < mean_force < self.soft_force:
            return TerrainClass.SOFT

        return TerrainClass.FLAT


# ── Debounce ──────────────────────────────────────────────────────────────────

class TransitionDebouncer:
    """
    Requires a new classification to hold for `hold_ticks` consecutive ticks
    before accepting it as the confirmed terrain class.
    Prevents gait thrashing on transient sensor noise.
    """

    def __init__(self, hold_ticks: int = 15):
        self._hold     = hold_ticks
        self._candidate: TerrainClass = TerrainClass.UNKNOWN
        self._streak:  int = 0
        self._confirmed: TerrainClass = TerrainClass.UNKNOWN

    def update(self, raw: TerrainClass) -> tuple[TerrainClass, bool]:
        """
        Returns (confirmed_class, changed).
        changed=True only when the confirmed class transitions.
        """
        if raw == self._candidate:
            self._streak += 1
        else:
            self._candidate = raw
            self._streak = 1

        if self._streak >= self._hold and raw != self._confirmed:
            self._confirmed = raw
            return self._confirmed, True

        return self._confirmed, False

    @property
    def confirmed(self) -> TerrainClass:
        return self._confirmed


# ── Plugin ────────────────────────────────────────────────────────────────────

class TerrainArbiter(CerberusPlugin):
    """
    CERBERUS TerrainArbiter Plugin.

    Hooks into the engine tick loop, reads RobotState every tick,
    classifies terrain, debounces transitions, and dispatches gait
    commands when the terrain class changes.

    Status is published to topic 'terrain.classification' on every
    confirmed transition, and is available via the plugin's status()
    method for polling.
    """

    MANIFEST = PluginManifest(
        name         = "TerrainArbiter",
        version      = "1.0.0",
        author       = "CERBERUS Core",
        description  = "Proprioceptive terrain classification and adaptive gait management",
        capabilities = ["read_state", "control_motion", "publish_events"],
        trust        = TrustLevel.TRUSTED,
        min_cerberus = "2.1.0",
    )

    def __init__(self, engine):
        super().__init__(engine)
        self._window     = SensorWindow(max_size=60)
        self._classifier = TerrainClassifier()
        self._debouncer  = TransitionDebouncer(hold_ticks=15)
        self._profile    = TERRAIN_GAIT_MAP[TerrainClass.UNKNOWN]

        # State
        self._current_terrain: TerrainClass = TerrainClass.UNKNOWN
        self._last_dispatch_time: float = 0.0
        self._transition_count: int = 0
        self._dispatch_lock = asyncio.Lock()

        # Config
        self._sample_every_n_ticks = 2   # sample at ~30Hz when engine runs at 60Hz
        self._min_dispatch_interval = 1.0  # seconds between gait commands

    async def on_load(self) -> None:
        logger.info("TerrainArbiter loaded — terrain-adaptive gait management active")

    async def on_unload(self) -> None:
        logger.info("TerrainArbiter unloaded")

    async def on_tick(self, tick: int) -> None:
        # Sample at reduced rate to avoid redundant computation
        if tick % self._sample_every_n_ticks != 0:
            return

        # Collect sensor sample
        state = await self.get_state()
        sample = TerrainSample(
            timestamp  = state.timestamp,
            foot_force = list(state.foot_force),
            pitch_rad  = state.pitch,
            roll_rad   = state.roll,
            vx         = state.velocity_x,
            vy         = state.velocity_y,
        )
        self._window.push(sample)

        # Classify
        raw_class = self._classifier.classify(self._window)

        # Debounce
        confirmed, changed = self._debouncer.update(raw_class)

        if changed:
            await self._on_terrain_change(confirmed)

    async def _on_terrain_change(self, terrain: TerrainClass) -> None:
        """Called when terrain class transitions and debounce holds."""
        self._current_terrain = terrain
        self._profile = TERRAIN_GAIT_MAP[terrain]
        self._transition_count += 1

        logger.info(
            "Terrain transition → %s (gait=%d, raise=%.3f, speed=×%.2f)",
            terrain.value, self._profile.gait_id,
            self._profile.foot_raise_m, self._profile.speed_factor,
        )

        # Rate-limit actual bridge dispatches
        now = time.monotonic()
        if now - self._last_dispatch_time < self._min_dispatch_interval:
            logger.debug("Terrain gait dispatch rate-limited (%.1fs since last)", now - self._last_dispatch_time)
            # Still publish the event — just don't send bridge commands
            await self._publish_status()
            return

        # Dispatch gait commands
        async with self._dispatch_lock:
            await self._dispatch_gait(self._profile)
            self._last_dispatch_time = time.monotonic()

        await self._publish_status()

    async def _dispatch_gait(self, profile: GaitProfile) -> None:
        """Send switch_gait and set_foot_raise_height to the bridge."""
        try:
            # switch_gait requires control_motion capability — checked by base class
            await self.engine.bridge.switch_gait(profile.gait_id)
            await asyncio.sleep(0.05)  # small gap so SDK doesn't coalesce commands
            await self.engine.bridge.set_foot_raise_height(profile.foot_raise_m)
            logger.debug("Gait dispatched: id=%d raise=%.3f", profile.gait_id, profile.foot_raise_m)
        except Exception as exc:
            logger.error("TerrainArbiter gait dispatch error: %s", exc)

    async def _publish_status(self) -> None:
        """Emit terrain classification status to event bus."""
        await self.publish("terrain.classification", {
            "terrain": self._current_terrain.value,
            "profile": self._profile.to_dict(),
            "window": self._window.snapshot(),
            "transitions": self._transition_count,
            "timestamp": time.time(),
        })

    # ── External control API ─────────────────────────────────────────────────

    def tune(
        self,
        roll_threshold_deg:  float | None = None,
        pitch_threshold_deg: float | None = None,
        rough_variance:      float | None = None,
        soft_force:          float | None = None,
        hold_ticks:          int   | None = None,
    ) -> None:
        """Runtime tuning — update classifier/debouncer without reload."""
        if roll_threshold_deg  is not None: self._classifier.roll_thresh  = roll_threshold_deg
        if pitch_threshold_deg is not None: self._classifier.pitch_thresh = pitch_threshold_deg
        if rough_variance      is not None: self._classifier.rough_var    = rough_variance
        if soft_force          is not None: self._classifier.soft_force   = soft_force
        if hold_ticks          is not None: self._debouncer._hold         = hold_ticks
        logger.info("TerrainArbiter thresholds updated")

    def status(self) -> dict:
        base = super().status()
        return {
            **base,
            "current_terrain": self._current_terrain.value,
            "gait_profile": self._profile.to_dict(),
            "window": self._window.snapshot(),
            "transition_count": self._transition_count,
            "debounce_candidate": self._debouncer._candidate.value,
            "debounce_streak": self._debouncer._streak,
        }
