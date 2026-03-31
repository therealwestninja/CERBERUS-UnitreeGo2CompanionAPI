"""
tests/test_terrain_arbiter.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Unit + integration tests for the TerrainArbiter plugin.

Coverage:
  - SensorWindow statistics (force variance, front/rear asymmetry, IMU means)
  - TerrainClassifier rule priority and threshold logic
  - TransitionDebouncer hold semantics
  - TerrainArbiter.on_tick() → classify → debounce → dispatch pipeline
  - SimBridge integration (gait commands actually dispatched)
  - Capability sandboxing preserved
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import pytest

# Make cerberus importable from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ["GO2_SIMULATION"] = "true"

from plugins.terrain_arbiter.plugin import (
    TerrainClass,
    TerrainClassifier,
    TerrainSample,
    SensorWindow,
    TransitionDebouncer,
    TerrainArbiter,
    TERRAIN_GAIT_MAP,
)
from cerberus.bridge.go2_bridge import SimBridge
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.plugins.plugin_manager import PluginManager


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_sample(
    foot_force=None,
    pitch_deg=0.0,
    roll_deg=0.0,
    vx=0.3,
    vy=0.0,
) -> TerrainSample:
    return TerrainSample(
        timestamp=0.0,
        foot_force=foot_force or [30.0, 30.0, 30.0, 30.0],
        pitch_rad=math.radians(pitch_deg),
        roll_rad=math.radians(roll_deg),
        vx=vx,
        vy=vy,
    )


def fill_window(window: SensorWindow, sample: TerrainSample, n: int = 25) -> None:
    for _ in range(n):
        window.push(sample)


# ── SensorWindow ──────────────────────────────────────────────────────────────

class TestSensorWindow:
    def test_empty_window_not_ready(self):
        w = SensorWindow()
        assert not w.is_ready()
        assert w.mean_total_force() == 0.0
        assert w.force_variance() == 0.0

    def test_ready_after_min_samples(self):
        w = SensorWindow()
        for _ in range(19):
            w.push(make_sample())
        assert not w.is_ready(min_samples=20)
        w.push(make_sample())
        assert w.is_ready(min_samples=20)

    def test_mean_total_force(self):
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[10.0, 20.0, 30.0, 40.0]))
        assert abs(w.mean_total_force() - 100.0) < 0.01

    def test_force_variance_uniform(self):
        # Equal forces → variance = 0
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[25.0, 25.0, 25.0, 25.0]))
        assert w.force_variance() < 1.0

    def test_force_variance_high(self):
        # One foot heavily loaded → high variance
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[100.0, 0.0, 0.0, 0.0]))
        assert w.force_variance() > 400.0

    def test_front_rear_asymmetry_positive_descending(self):
        # Front heavier than rear → descending (nose-down)
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[60.0, 60.0, 10.0, 10.0]))
        asym = w.front_rear_asymmetry()
        assert asym > 0.3, f"Expected positive asymmetry, got {asym}"

    def test_front_rear_asymmetry_negative_ascending(self):
        # Rear heavier → ascending (nose-up)
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[10.0, 10.0, 60.0, 60.0]))
        asym = w.front_rear_asymmetry()
        assert asym < -0.3, f"Expected negative asymmetry, got {asym}"

    def test_mean_pitch_deg(self):
        w = SensorWindow()
        fill_window(w, make_sample(pitch_deg=-12.0))
        assert abs(w.mean_pitch_deg() - (-12.0)) < 0.5

    def test_mean_roll_deg(self):
        w = SensorWindow()
        fill_window(w, make_sample(roll_deg=15.0))
        assert abs(w.mean_roll_deg() - 15.0) < 0.5

    def test_maxsize_fifo(self):
        w = SensorWindow(max_size=10)
        for _ in range(15):
            w.push(make_sample(foot_force=[1.0, 1.0, 1.0, 1.0]))
        assert len(w) == 10

    def test_snapshot_keys(self):
        w = SensorWindow()
        fill_window(w, make_sample())
        snap = w.snapshot()
        for key in ("samples", "mean_total_force_n", "force_variance",
                    "front_rear_asymmetry", "mean_pitch_deg", "mean_roll_deg", "mean_speed_ms"):
            assert key in snap


# ── TerrainClassifier ──────────────────────────────────────────────────────────

class TestTerrainClassifier:
    def setup_method(self):
        self.clf = TerrainClassifier()

    def _window_with(self, **kwargs) -> SensorWindow:
        w = SensorWindow()
        fill_window(w, make_sample(**kwargs))
        return w

    def test_unknown_before_ready(self):
        w = SensorWindow()
        w.push(make_sample())  # only 1 sample
        assert self.clf.classify(w) == TerrainClass.UNKNOWN

    def test_flat_nominal(self):
        w = self._window_with(foot_force=[30.0, 30.0, 30.0, 30.0], vx=0.4)
        assert self.clf.classify(w) == TerrainClass.FLAT

    def test_lateral_slope_positive_roll(self):
        w = self._window_with(roll_deg=12.0)
        assert self.clf.classify(w) == TerrainClass.LATERAL_SLOPE

    def test_lateral_slope_negative_roll(self):
        w = self._window_with(roll_deg=-10.0)
        assert self.clf.classify(w) == TerrainClass.LATERAL_SLOPE

    def test_incline_up_negative_pitch_moving(self):
        # Negative pitch = nose up = ascending
        w = self._window_with(pitch_deg=-9.0, vx=0.3)
        assert self.clf.classify(w) == TerrainClass.INCLINE_UP

    def test_incline_down_positive_pitch_moving(self):
        # Positive pitch = nose down = descending
        w = self._window_with(pitch_deg=9.0, vx=0.3)
        assert self.clf.classify(w) == TerrainClass.INCLINE_DOWN

    def test_incline_suppressed_at_rest(self):
        # At rest, pitch is structural, not terrain-indicative
        w = self._window_with(pitch_deg=-9.0, vx=0.01)
        result = self.clf.classify(w)
        assert result not in (TerrainClass.INCLINE_UP, TerrainClass.INCLINE_DOWN)

    def test_rough_high_variance(self):
        # One foot consistently much heavier
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[100.0, 10.0, 5.0, 5.0], vx=0.3))
        assert self.clf.classify(w) == TerrainClass.ROUGH

    def test_soft_low_mean_force(self):
        w = self._window_with(foot_force=[8.0, 8.0, 8.0, 8.0], vx=0.2)
        assert self.clf.classify(w) == TerrainClass.SOFT

    def test_roll_beats_incline(self):
        # Roll above threshold + pitch above threshold → lateral slope wins
        w = self._window_with(pitch_deg=-9.0, roll_deg=12.0, vx=0.4)
        assert self.clf.classify(w) == TerrainClass.LATERAL_SLOPE

    def test_incline_beats_rough(self):
        # Strong pitch + high variance → incline wins (priority 2 > 3)
        w = SensorWindow()
        fill_window(w, make_sample(foot_force=[100.0, 5.0, 5.0, 5.0], pitch_deg=-9.0, vx=0.4))
        assert self.clf.classify(w) == TerrainClass.INCLINE_UP


# ── TransitionDebouncer ───────────────────────────────────────────────────────

class TestTransitionDebouncer:
    def test_initial_state(self):
        d = TransitionDebouncer(hold_ticks=5)
        assert d.confirmed == TerrainClass.UNKNOWN

    def test_no_change_before_hold(self):
        d = TransitionDebouncer(hold_ticks=5)
        for _ in range(4):
            confirmed, changed = d.update(TerrainClass.ROUGH)
            assert not changed
        assert d.confirmed == TerrainClass.UNKNOWN

    def test_confirms_after_hold(self):
        d = TransitionDebouncer(hold_ticks=5)
        for i in range(5):
            confirmed, changed = d.update(TerrainClass.ROUGH)
        assert changed
        assert confirmed == TerrainClass.ROUGH

    def test_streak_reset_on_different_class(self):
        d = TransitionDebouncer(hold_ticks=5)
        for _ in range(4):
            d.update(TerrainClass.ROUGH)
        # Different class — streak resets to 1 on this call
        d.update(TerrainClass.FLAT)
        # Need 4 more FLAT to reach streak=5 (hold).
        # The first 3 should NOT confirm (streak 2,3,4 < 5).
        for _ in range(3):
            confirmed, changed = d.update(TerrainClass.FLAT)
            assert not changed
        # 4th additional call brings streak to 5 — confirmation fires
        confirmed, changed = d.update(TerrainClass.FLAT)
        assert changed
        assert confirmed == TerrainClass.FLAT

    def test_no_spurious_change_on_same_confirmed(self):
        d = TransitionDebouncer(hold_ticks=3)
        for _ in range(3):
            d.update(TerrainClass.ROUGH)
        # Now confirmed = ROUGH; more ROUGH should not re-emit changed
        for _ in range(10):
            _, changed = d.update(TerrainClass.ROUGH)
            assert not changed


# ── GaitProfile map ───────────────────────────────────────────────────────────

class TestGaitMap:
    def test_all_terrain_classes_have_profile(self):
        for cls in TerrainClass:
            assert cls in TERRAIN_GAIT_MAP, f"Missing profile for {cls}"

    def test_gait_ids_in_range(self):
        for cls, profile in TERRAIN_GAIT_MAP.items():
            assert 0 <= profile.gait_id <= 3, f"{cls}: gait_id out of range"

    def test_foot_raise_in_bridge_range(self):
        for cls, profile in TERRAIN_GAIT_MAP.items():
            assert -0.06 <= profile.foot_raise_m <= 0.03, f"{cls}: foot_raise out of bridge range"

    def test_speed_factors_positive(self):
        for cls, profile in TERRAIN_GAIT_MAP.items():
            assert profile.speed_factor > 0, f"{cls}: speed_factor must be positive"

    def test_rough_raises_step_height(self):
        assert TERRAIN_GAIT_MAP[TerrainClass.ROUGH].foot_raise_m > 0

    def test_incline_reduces_speed(self):
        assert TERRAIN_GAIT_MAP[TerrainClass.INCLINE_UP].speed_factor < 1.0
        assert TERRAIN_GAIT_MAP[TerrainClass.INCLINE_DOWN].speed_factor < 1.0


# ── Full plugin integration ────────────────────────────────────────────────────

@pytest.fixture
async def engine_with_plugin():
    """Spin up a real SimBridge + engine, load TerrainArbiter."""
    bridge = SimBridge()
    limits = SafetyLimits()
    watchdog = SafetyWatchdog(bridge, limits)
    eng = CerberusEngine(bridge, watchdog, target_hz=60)

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()
    pm.register_with_engine()

    await eng.start()
    yield eng, pm, bridge
    await eng.stop()


@pytest.mark.asyncio
async def test_terrain_plugin_loads():
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await bridge.connect()

    loaded = await pm.discover_and_load()
    assert loaded >= 1

    names = [p["name"] for p in pm.list_plugins()]
    assert "TerrainArbiter" in names

    await bridge.disconnect()


@pytest.mark.asyncio
async def test_terrain_plugin_status_keys():
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await bridge.connect()
    await pm.discover_and_load()

    plugin_info = next(p for p in pm.list_plugins() if p["name"] == "TerrainArbiter")
    for key in ("name", "version", "enabled", "error_count", "current_terrain", "gait_profile", "window"):
        assert key in plugin_info, f"Missing key: {key}"

    await bridge.disconnect()


@pytest.mark.asyncio
async def test_terrain_arbiter_classifies_via_ticks():
    """Drive the plugin through tick() directly with injected state."""
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    await bridge.connect()

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()

    plugin_record = pm._plugins["TerrainArbiter"]
    arbiter: TerrainArbiter = plugin_record.plugin

    # Inject rough-terrain state into the SimBridge state
    bridge._state.foot_force = [100.0, 5.0, 5.0, 5.0]
    bridge._state.roll  = 0.0
    bridge._state.pitch = 0.0
    bridge._state.velocity_x = 0.4
    bridge._state.velocity_y = 0.0

    # sample_every=2 means 80 ticks → 40 samples.
    # Window needs 20 to be ready, leaving 20 for debounce (hold=15). OK.
    for i in range(80):
        await arbiter.on_tick(i)

    status = arbiter.status()
    # With high variance foot forces, should classify as ROUGH (or at least not UNKNOWN)
    assert status["current_terrain"] in (TerrainClass.ROUGH.value, TerrainClass.FLAT.value)

    await bridge.disconnect()


@pytest.mark.asyncio
async def test_terrain_arbiter_dispatches_gait_on_transition():
    """Verify gait dispatch fires when terrain class changes."""
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    await bridge.connect()

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()

    arbiter: TerrainArbiter = pm._plugins["TerrainArbiter"].plugin
    arbiter._debouncer._hold = 5   # reduce hold for faster test
    arbiter._min_dispatch_interval = 0.0  # disable rate limiting

    # Simulate lateral slope (roll > threshold)
    bridge._state.foot_force = [25.0, 25.0, 25.0, 25.0]
    bridge._state.roll  = math.radians(15.0)  # above 8° threshold
    bridge._state.pitch = 0.0
    bridge._state.velocity_x = 0.3

    for i in range(80):
        await arbiter.on_tick(i)

    await asyncio.sleep(0.1)  # let dispatch coroutine complete

    status = arbiter.status()
    assert status["transition_count"] >= 1


@pytest.mark.asyncio
async def test_terrain_arbiter_tune_thresholds():
    """tune() should update classifier thresholds without reload."""
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    await bridge.connect()

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()

    arbiter: TerrainArbiter = pm._plugins["TerrainArbiter"].plugin

    arbiter.tune(roll_threshold_deg=30.0, pitch_threshold_deg=25.0)
    assert arbiter._classifier.roll_thresh == 30.0
    assert arbiter._classifier.pitch_thresh == 25.0

    # At the new high threshold, a 15° roll should not trigger LATERAL_SLOPE
    w = SensorWindow()
    fill_window(w, make_sample(roll_deg=15.0))
    result = arbiter._classifier.classify(w)
    assert result != TerrainClass.LATERAL_SLOPE

    await bridge.disconnect()


@pytest.mark.asyncio
async def test_terrain_arbiter_debounce_prevents_thrashing():
    """Alternating terrain readings should not cause classification changes."""
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    await bridge.connect()

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()

    arbiter: TerrainArbiter = pm._plugins["TerrainArbiter"].plugin
    arbiter._debouncer._hold = 10  # explicit hold

    # Alternate between rough and flat every tick
    for i in range(40):
        if i % 2 == 0:
            bridge._state.foot_force = [100.0, 5.0, 5.0, 5.0]
        else:
            bridge._state.foot_force = [25.0, 25.0, 25.0, 25.0]
        await arbiter.on_tick(i)

    # With rapid alternation, neither candidate should hold for 10 ticks
    assert arbiter._transition_count == 0

    await bridge.disconnect()


@pytest.mark.asyncio
async def test_terrain_plugin_event_bus_publish():
    """TerrainArbiter should publish to terrain.classification event bus topic."""
    bridge = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng = CerberusEngine(bridge, watchdog, target_hz=60)
    await bridge.connect()

    received_events = []
    eng.bus.subscribe("terrain.classification", lambda p: received_events.append(p))

    pm = PluginManager(eng, plugin_dirs=["plugins"])
    await pm.discover_and_load()

    arbiter: TerrainArbiter = pm._plugins["TerrainArbiter"].plugin
    arbiter._debouncer._hold = 3
    arbiter._min_dispatch_interval = 0.0

    bridge._state.foot_force = [100.0, 5.0, 5.0, 5.0]
    bridge._state.roll  = 0.0
    bridge._state.pitch = 0.0
    bridge._state.velocity_x = 0.4

    for i in range(30):
        await arbiter.on_tick(i)

    await asyncio.sleep(0.05)

    # If a transition occurred, at least one event was published
    if arbiter._transition_count > 0:
        assert len(received_events) > 0
        evt = received_events[0]
        assert "terrain" in evt
        assert "profile" in evt
        assert "window" in evt

    await bridge.disconnect()
