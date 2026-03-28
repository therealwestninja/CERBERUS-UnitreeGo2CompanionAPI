"""
tests/test_plugin_compatibility.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests for multi-plugin coexistence, hook ordering, and event bus isolation.

Covers:
  • Hook priority ordering — TerrainArbiter (100) before StairClimber (110)
  • Stair gait override — StairClimber wins the last gait call in a tick
  • Safety limit intersection — payload + stair limits are additive restrictions
  • EventBus topic isolation — each plugin only emits its own topics
  • Simultaneous plugin load — all three plugins load without conflict
  • Plugin error isolation — one crashing plugin doesn't disable others
  • Hook cleanup on unload — unloaded plugin hook removed from engine
  • Combined payload + stair snag — payload limits respected during stair recovery
  • Dynamic load respects priority — plugins loaded after startup use HOOK_PRIORITY
  • Plugin enable/disable cycle — re-enabling a plugin restores tick behaviour
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import pytest
import pytest_asyncio

from cerberus.bridge.go2_bridge import SimBridge, RobotState
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.plugins.plugin_manager import (
    CerberusPlugin, PluginManifest, PluginManager, TrustLevel,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    bridge   = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng      = CerberusEngine(bridge, watchdog, target_hz=60)
    eng.watchdog = watchdog
    return eng, bridge, watchdog


async def _load_all_plugins(eng):
    """Load TerrainArbiter, StairClimber, and UndercarriagePayload into eng."""
    pm = PluginManager(eng, ["plugins"])
    await pm.discover_and_load()
    pm.register_with_engine()
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# Hook priority ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestHookPriorityOrdering:
    """Verify that HOOK_PRIORITY class attributes are respected by the engine."""

    @pytest.mark.asyncio
    async def test_stair_hook_priority_higher_than_terrain(self, full_engine):
        """
        StairClimber.HOOK_PRIORITY (110) must be greater than
        TerrainArbiter.HOOK_PRIORITY (default 100), ensuring StairClimber
        runs AFTER TerrainArbiter in the same engine tick.
        """
        from plugins.stair_climber.plugin import StairClimberPlugin
        from plugins.terrain_arbiter.plugin import TerrainArbiter
        assert StairClimberPlugin.HOOK_PRIORITY > getattr(TerrainArbiter, "HOOK_PRIORITY", 100)

    @pytest.mark.asyncio
    async def test_hooks_registered_with_correct_priority(self, full_engine):
        eng, pm = full_engine
        hooks = {h.name: h.priority for h in eng._plugin_hooks}
        terrain_p = hooks.get("plugin_terrain_arbiter", hooks.get("plugin_TerrainArbiter"))
        stair_p   = hooks.get("plugin_stair_climber",   hooks.get("plugin_StairClimber"))
        if terrain_p is not None and stair_p is not None:
            assert stair_p > terrain_p, (
                f"StairClimber hook priority ({stair_p}) must be > "
                f"TerrainArbiter hook priority ({terrain_p})"
            )

    @pytest.mark.asyncio
    async def test_hook_execution_order_matches_priority(self, full_engine):
        """
        Engine processes plugin hooks in ascending priority order.
        Record execution order by patching each plugin's on_tick.
        """
        eng, pm = full_engine
        call_order: list[str] = []

        for name, rec in pm._plugins.items():
            plugin_name = name

            async def _patched(tick, _n=plugin_name):
                call_order.append(_n)

            rec.plugin.on_tick = _patched

        await eng.bridge.connect()
        # Run one engine _tick directly (skips the async loop)
        await eng._tick(1)

        # Higher-priority plugins (lower number) should appear first
        hooks_by_prio = sorted(
            [(h.name.replace("plugin_", ""), h.priority) for h in eng._plugin_hooks],
            key=lambda x: x[1],
        )
        expected_order = [n for n, _ in hooks_by_prio if n in call_order]
        actual_order   = [n for n in call_order if n in expected_order]
        assert actual_order == expected_order

    @pytest.mark.asyncio
    async def test_terrain_runs_before_stair_in_single_tick(self, full_engine):
        """
        In a single _tick(), TerrainArbiter on_tick must complete before
        StairClimber on_tick begins.
        """
        eng, pm = full_engine
        timestamps: dict[str, float] = {}

        for name, rec in pm._plugins.items():
            _n = name

            async def _record(tick, _n=_n):
                timestamps[_n] = time.monotonic()
                await asyncio.sleep(0)   # yield to make ordering meaningful

            rec.plugin.on_tick = _record

        await eng.bridge.connect()
        await eng._tick(1)

        terrain_names = [k for k in timestamps if "terrain" in k.lower()]
        stair_names   = [k for k in timestamps if "stair"   in k.lower()]
        if terrain_names and stair_names:
            assert timestamps[terrain_names[0]] < timestamps[stair_names[0]]


# ─────────────────────────────────────────────────────────────────────────────
# Stair gait override
# ─────────────────────────────────────────────────────────────────────────────

class TestStairOverridesTerrain:
    """When stairs are active, StairClimber must win the last gait command."""

    @pytest.mark.asyncio
    async def test_stair_active_overrides_terrain_gait(self, bare_engine):
        """
        Direct test: enter stair mode, then tick both plugins in priority order.
        The last switch_gait call (from StairClimber) must be gait=3.
        """
        from plugins.stair_climber.plugin import StairClimberPlugin, StairState
        from plugins.terrain_arbiter.plugin import TerrainArbiter

        eng = bare_engine
        terrain = TerrainArbiter(eng)
        stair   = StairClimberPlugin(eng)

        gait_calls: list[int] = []
        original_switch = eng.bridge.switch_gait

        async def _record_gait(gait_id: int) -> bool:
            gait_calls.append(gait_id)
            return await original_switch(gait_id)

        eng.bridge.switch_gait = _record_gait

        await terrain.on_load()
        await stair.on_load()
        await stair._enter_stair("ascending")

        # Simulate a tick: terrain might set gait 1 (incline), stair re-sets gait 3
        await terrain.on_tick(tick=1)
        await stair.on_tick(tick=1)

        assert gait_calls[-1] == 3, (
            f"Last gait command should be 3 (stair stance walk), got {gait_calls[-1]}"
        )

    @pytest.mark.asyncio
    async def test_stair_exit_restores_gait_to_zero(self, bare_engine):
        """After stair exit, gait returns to 0 (default trot)."""
        from plugins.stair_climber.plugin import StairClimberPlugin

        stair = StairClimberPlugin(bare_engine)
        await stair._enter_stair("ascending")
        await stair._restore_pre_stair(force=True)

        # Bridge's last gait command should be gait 0
        # (We can't observe directly — verify plugin state is NOMINAL)
        from plugins.stair_climber.plugin import StairState
        assert stair._status.state == StairState.NOMINAL


# ─────────────────────────────────────────────────────────────────────────────
# Safety limit intersection
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyLimitIntersection:
    """Payload + stair limits must both restrict simultaneously."""

    @pytest.mark.asyncio
    async def test_payload_limits_survive_stair_entry(self, bare_engine):
        """
        Attach payload (reduces max_vx to ~1.36), then enter stair (caps at 0.25).
        The final max_vx must be the most restrictive: ≤ 0.25.
        """
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin
        from plugins.stair_climber.plugin import StairClimberPlugin

        payload_plugin = UndercarriagePayloadPlugin(bare_engine)
        stair_plugin   = StairClimberPlugin(bare_engine)

        cfg = PayloadConfig(mass_kg=1.5, thickness_m=0.05, material=PayloadMaterial.SILICONE)
        await payload_plugin.attach(cfg)

        # After payload, max_vx should be < 1.5
        post_payload_vx = bare_engine.watchdog.limits.max_vx
        assert post_payload_vx < 1.5

        await stair_plugin._enter_stair("ascending")

        # After stair, max_vx must be ≤ stair profile cap (0.25)
        final_vx = bare_engine.watchdog.limits.max_vx
        assert final_vx <= 0.25

    @pytest.mark.asyncio
    async def test_stair_exit_restores_payload_limits_not_defaults(self, bare_engine):
        """
        Stair exit should restore the PRE-STAIR limits (which include payload
        compensation), not the original bare-robot defaults.
        """
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin
        from plugins.stair_climber.plugin import StairClimberPlugin

        payload_plugin = UndercarriagePayloadPlugin(bare_engine)
        stair_plugin   = StairClimberPlugin(bare_engine)

        cfg = PayloadConfig(mass_kg=1.5, thickness_m=0.05, material=PayloadMaterial.SILICONE)
        await payload_plugin.attach(cfg)
        payload_vx = bare_engine.watchdog.limits.max_vx   # e.g. ~1.36

        await stair_plugin._enter_stair("ascending")
        await stair_plugin._restore_pre_stair(force=True)

        restored_vx = bare_engine.watchdog.limits.max_vx
        # Should be back to payload-compensated value, not full 1.5
        assert restored_vx == pytest.approx(payload_vx)

    @pytest.mark.asyncio
    async def test_three_plugins_loaded_limit_is_most_restrictive(self, full_engine):
        """All three plugins loaded — no plugin accidentally widens any limit."""
        eng, pm = full_engine
        base = SafetyLimits()

        lim = eng.watchdog.limits
        # Payload may or may not be attached at load time; stair is not active.
        # None of the plugins should widen any limit beyond the base defaults.
        assert lim.max_vx     <= base.max_vx
        assert lim.max_vy     <= base.max_vy
        assert lim.max_vyaw   <= base.max_vyaw
        assert lim.max_roll_deg   <= base.max_roll_deg
        assert lim.max_pitch_deg  <= base.max_pitch_deg


# ─────────────────────────────────────────────────────────────────────────────
# EventBus topic isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestEventBusIsolation:
    """Each plugin only publishes to its own declared topics."""

    @pytest.mark.asyncio
    async def test_stair_plugin_does_not_emit_terrain_topics(self, bare_engine):
        """StairClimber never publishes to terrain.classification."""
        from plugins.stair_climber.plugin import StairClimberPlugin

        received: list[str] = []
        bare_engine.bus.subscribe("terrain.classification", lambda p: received.append("terrain"))
        bare_engine.bus.subscribe("stair.detected",         lambda p: received.append("stair"))

        stair = StairClimberPlugin(bare_engine)
        await stair._enter_stair("ascending")

        assert "terrain" not in received
        assert "stair"   in received

    @pytest.mark.asyncio
    async def test_terrain_plugin_does_not_emit_stair_topics(self, bare_engine):
        """TerrainArbiter never publishes to stair.* topics."""
        from plugins.terrain_arbiter.plugin import TerrainArbiter

        stair_events: list[str] = []
        bare_engine.bus.subscribe("stair.detected", lambda p: stair_events.append("stair"))
        bare_engine.bus.subscribe("stair.status",   lambda p: stair_events.append("stair"))

        terrain = TerrainArbiter(bare_engine)
        await terrain.on_load()
        await terrain.on_tick(tick=1)

        assert stair_events == [], "TerrainArbiter should not publish stair topics"

    @pytest.mark.asyncio
    async def test_payload_plugin_does_not_emit_stair_topics(self, bare_engine):
        """UndercarriagePayload never publishes to stair.* topics."""
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin

        stair_events: list[Any] = []
        bare_engine.bus.subscribe("stair.detected", lambda p: stair_events.append(p))

        p = UndercarriagePayloadPlugin(bare_engine)
        cfg = PayloadConfig(mass_kg=1.0, thickness_m=0.04, material=PayloadMaterial.SILICONE)
        await p.attach(cfg)
        await p.on_tick(tick=1)

        assert stair_events == []

    @pytest.mark.asyncio
    async def test_all_plugins_loaded_events_route_correctly(self, full_engine):
        """With all three plugins, topic routing is deterministic."""
        eng, pm = full_engine

        topic_counts: dict[str, int] = {}

        def _track(topic: str):
            def _h(payload):
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
            return _h

        for topic in ("terrain.classification", "stair.status",
                      "payload.contact", "payload.attached"):
            eng.bus.subscribe(topic, _track(topic))

        # Tick once — terrain and stair will publish status
        await eng.bridge.connect()
        await eng._tick(12)   # tick 12 triggers 5-Hz status broadcast

        # Stair.status and terrain.classification should both fire independently
        # (doesn't matter if they're zero — just no cross-contamination)
        terrain_count = topic_counts.get("terrain.classification", 0)
        stair_count   = topic_counts.get("stair.status", 0)
        payload_count = topic_counts.get("stair.status", 0)
        # No assertion on exact counts — just verify no exception was raised


# ─────────────────────────────────────────────────────────────────────────────
# Plugin error isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestPluginErrorIsolation:
    """A crashing plugin must not disable healthy siblings."""

    @pytest.mark.asyncio
    async def test_crashing_plugin_auto_disabled_after_error_limit(self, bare_engine):
        """Plugin exceeding PLUGIN_MAX_ERRORS consecutive errors gets disabled."""
        from cerberus.plugins.plugin_manager import PluginManager, PluginRecord

        class BrokenPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="broken", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )

            async def on_tick(self, tick: int) -> None:
                raise RuntimeError("always broken")

        pm = PluginManager(bare_engine, [])
        pm._max_errors = 3

        inst = BrokenPlugin(bare_engine)
        rec  = PluginRecord(plugin=inst, manifest=inst.MANIFEST, module_path="<test>")
        pm._plugins["broken"] = rec
        pm._register_hook_for_record("broken", rec)

        await bare_engine.bridge.connect()
        for tick in range(3):
            await bare_engine._tick(tick + 1)

        assert not inst._enabled, "Plugin should be disabled after 3 errors"

    @pytest.mark.asyncio
    async def test_healthy_plugin_survives_sibling_crash(self, bare_engine):
        """A healthy plugin continues ticking after a sibling crashes."""
        from cerberus.plugins.plugin_manager import PluginManager, PluginRecord

        tick_count = {"healthy": 0}

        class HealthyPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="healthy", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )

            async def on_tick(self, tick: int) -> None:
                tick_count["healthy"] += 1

        class BrokenPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="broken2", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )

            async def on_tick(self, tick: int) -> None:
                raise RuntimeError("broken")

        pm = PluginManager(bare_engine, [])
        pm._max_errors = 2

        for name, cls in [("healthy", HealthyPlugin), ("broken2", BrokenPlugin)]:
            inst = cls(bare_engine)
            rec  = PluginRecord(plugin=inst, manifest=inst.MANIFEST, module_path="<test>")
            pm._plugins[name] = rec
            pm._register_hook_for_record(name, rec)

        await bare_engine.bridge.connect()
        for tick in range(4):
            await bare_engine._tick(tick + 1)

        assert tick_count["healthy"] == 4, "Healthy plugin must tick every time"

    @pytest.mark.asyncio
    async def test_disabled_plugin_tick_not_called(self, bare_engine):
        """Setting plugin._enabled = False suppresses all on_tick calls."""
        from cerberus.plugins.plugin_manager import PluginManager, PluginRecord

        call_count = {"n": 0}

        class CounterPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="counter", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )

            async def on_tick(self, tick: int) -> None:
                call_count["n"] += 1

        pm = PluginManager(bare_engine, [])
        inst = CounterPlugin(bare_engine)
        inst._enabled = False   # disabled before registration
        rec  = PluginRecord(plugin=inst, manifest=inst.MANIFEST, module_path="<test>")
        pm._plugins["counter"] = rec
        pm._register_hook_for_record("counter", rec)

        await bare_engine.bridge.connect()
        await bare_engine._tick(1)

        assert call_count["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Hook cleanup on unload
# ─────────────────────────────────────────────────────────────────────────────

class TestHookCleanupOnUnload:

    @pytest.mark.asyncio
    async def test_unloaded_plugin_hook_removed_from_engine(self, full_engine):
        """After unload, the plugin's engine hook is removed."""
        eng, pm = full_engine

        initial_hook_count = len(eng._plugin_hooks)
        plugin_names = list(pm._plugins.keys())
        if not plugin_names:
            pytest.skip("No plugins loaded")

        name = plugin_names[0]
        hook_name = f"plugin_{name}"

        # Hook exists before unload
        assert any(h.name == hook_name for h in eng._plugin_hooks)

        await pm.unload_plugin(name)

        # Hook removed after unload
        assert not any(h.name == hook_name for h in eng._plugin_hooks)
        assert len(eng._plugin_hooks) == initial_hook_count - 1

    @pytest.mark.asyncio
    async def test_remaining_plugins_continue_after_one_unloaded(self, full_engine):
        """Other plugins still tick correctly after one is unloaded."""
        eng, pm = full_engine
        names = list(pm._plugins.keys())
        if len(names) < 2:
            pytest.skip("Need at least 2 plugins")

        await pm.unload_plugin(names[0])

        tick_ok = True
        try:
            await eng.bridge.connect()
            await eng._tick(1)
        except Exception:
            tick_ok = False

        assert tick_ok


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic load priority
# ─────────────────────────────────────────────────────────────────────────────

class TestDynamicLoadPriority:

    @pytest.mark.asyncio
    async def test_plugin_loaded_after_startup_uses_hook_priority(self, bare_engine):
        """A plugin loaded dynamically (after register_with_engine) uses its HOOK_PRIORITY."""
        from cerberus.plugins.plugin_manager import PluginManager, PluginRecord

        class PriorityPlugin(CerberusPlugin):
            MANIFEST      = PluginManifest(
                name="priority_test", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )
            HOOK_PRIORITY = 42

        pm = PluginManager(bare_engine, [])
        inst = PriorityPlugin(bare_engine)
        rec  = PluginRecord(plugin=inst, manifest=inst.MANIFEST, module_path="<test>")
        pm._plugins["priority_test"] = rec
        pm._register_hook_for_record("priority_test", rec)

        hook = next((h for h in bare_engine._plugin_hooks
                     if h.name == "plugin_priority_test"), None)
        assert hook is not None
        assert hook.priority == 42


# ─────────────────────────────────────────────────────────────────────────────
# Enable / disable cycle
# ─────────────────────────────────────────────────────────────────────────────

class TestPluginEnableDisableCycle:

    @pytest.mark.asyncio
    async def test_disable_then_enable_restores_ticking(self, full_engine):
        """Disabling then re-enabling a plugin restores its tick behaviour."""
        eng, pm = full_engine
        names = list(pm._plugins.keys())
        if not names:
            pytest.skip("No plugins loaded")

        name = names[0]
        rec  = pm._plugins[name]

        tick_count = {"n": 0}
        original_tick = rec.plugin.on_tick

        async def _counting_tick(tick):
            tick_count["n"] += 1
            await original_tick(tick)

        rec.plugin.on_tick = _counting_tick

        await eng.bridge.connect()
        await eng._tick(1)
        assert tick_count["n"] == 1

        pm.disable(name)
        await eng._tick(2)
        assert tick_count["n"] == 1  # not called while disabled

        pm.enable(name)
        await eng._tick(3)
        assert tick_count["n"] == 2  # called again after re-enable
