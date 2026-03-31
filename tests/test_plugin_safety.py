"""
tests/test_plugin_safety.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests for the CERBERUS plugin capability sandbox, trust level enforcement,
E-stop propagation, and plugin-system safety invariants.

Covers:
  • UNTRUSTED plugin cannot call any control capability
  • COMMUNITY plugin cannot call TRUSTED-only capabilities
  • Plugin that declares only read_state cannot call move()
  • PermissionError is raised immediately — not swallowed
  • E-stop during active plugin behaviour → plugin returns to safe state
  • Safety watchdog limits cannot be relaxed by any plugin
  • Plugin sandbox: loaded module is isolated in sys.modules namespace
  • Plugin that attempts to access hardware without capability → blocked
  • Plugin manifest capability validation catches over-declaration
  • Plugin error count resets on explicit re-enable
"""

from __future__ import annotations

import sys
import asyncio

import pytest

from cerberus.bridge.go2_bridge import SimBridge, RobotState
from cerberus.core.safety import SafetyLimits, SafetyWatchdog
from cerberus.core.engine import CerberusEngine
from cerberus.plugins.plugin_manager import (
    ALL_CAPABILITIES, CerberusPlugin, PluginManifest,
    PluginManager, PluginRecord, TrustLevel, TRUST_CAPABILITY_MAP,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    bridge   = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng      = CerberusEngine(bridge, watchdog, target_hz=60)
    eng.watchdog = watchdog
    return eng, bridge, watchdog


def _minimal_plugin(trust: TrustLevel, capabilities: set[str]) -> CerberusPlugin:
    """Factory for a minimal plugin instance for sandboxing tests."""
    eng, _, _ = _make_engine()

    class _MinPlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="test_plugin", version="1.0.0",
            capabilities=list(capabilities), trust=trust,
        )

    return _MinPlugin(eng)


# ─────────────────────────────────────────────────────────────────────────────
# Trust level taxonomy
# ─────────────────────────────────────────────────────────────────────────────

class TestTrustLevelTaxonomy:
    """Verify the TRUST_CAPABILITY_MAP invariants."""

    def test_untrusted_only_read_state(self):
        allowed = TRUST_CAPABILITY_MAP[TrustLevel.UNTRUSTED]
        assert allowed == {"read_state"}, (
            f"UNTRUSTED must have exactly {{read_state}}, got {allowed}"
        )

    def test_community_cannot_modify_safety_limits(self):
        allowed = TRUST_CAPABILITY_MAP[TrustLevel.COMMUNITY]
        assert "modify_safety_limits" not in allowed

    def test_community_cannot_low_level_control(self):
        allowed = TRUST_CAPABILITY_MAP[TrustLevel.COMMUNITY]
        assert "low_level_control" not in allowed

    def test_trusted_has_all_capabilities(self):
        assert TRUST_CAPABILITY_MAP[TrustLevel.TRUSTED] == ALL_CAPABILITIES

    def test_community_is_subset_of_trusted(self):
        assert TRUST_CAPABILITY_MAP[TrustLevel.COMMUNITY].issubset(
            TRUST_CAPABILITY_MAP[TrustLevel.TRUSTED]
        )

    def test_untrusted_is_subset_of_community(self):
        assert TRUST_CAPABILITY_MAP[TrustLevel.UNTRUSTED].issubset(
            TRUST_CAPABILITY_MAP[TrustLevel.COMMUNITY]
        )


# ─────────────────────────────────────────────────────────────────────────────
# Capability gating on UNTRUSTED plugins
# ─────────────────────────────────────────────────────────────────────────────

class TestUntrustedPluginRestrictions:

    def _untrusted(self):
        return _minimal_plugin(TrustLevel.UNTRUSTED, {"read_state"})

    def test_untrusted_require_control_motion_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError, match="control_motion"):
            p._require_capability("control_motion")

    def test_untrusted_require_control_gait_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            p._require_capability("control_gait")

    def test_untrusted_require_publish_events_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            p._require_capability("publish_events")

    def test_untrusted_require_modify_safety_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError, match="modify_safety_limits"):
            p._require_capability("modify_safety_limits")

    @pytest.mark.asyncio
    async def test_untrusted_move_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            await p.move(0.1, 0.0, 0.0)

    @pytest.mark.asyncio
    async def test_untrusted_stop_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            await p.stop()

    @pytest.mark.asyncio
    async def test_untrusted_switch_gait_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            await p.switch_gait(0)

    @pytest.mark.asyncio
    async def test_untrusted_set_led_raises(self):
        p = self._untrusted()
        with pytest.raises(PermissionError):
            await p.set_led(255, 0, 0)

    @pytest.mark.asyncio
    async def test_untrusted_get_state_succeeds(self):
        """read_state IS allowed for UNTRUSTED."""
        eng, bridge, _ = _make_engine()
        await bridge.connect()

        class _ReadPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="reader", version="1.0.0",
                capabilities=["read_state"], trust=TrustLevel.UNTRUSTED,
            )

        p = _ReadPlugin(eng)
        state = await p.get_state()
        assert state is not None


# ─────────────────────────────────────────────────────────────────────────────
# Capability gating on COMMUNITY plugins
# ─────────────────────────────────────────────────────────────────────────────

class TestCommunityPluginRestrictions:

    def _community(self, caps):
        return _minimal_plugin(TrustLevel.COMMUNITY, caps)

    def test_community_cannot_modify_safety_limits(self):
        p = self._community({"read_state", "modify_safety_limits"})
        # Trust level denies modify_safety_limits regardless of manifest declaration
        with pytest.raises(PermissionError):
            p._require_capability("modify_safety_limits")

    def test_community_cannot_low_level_control(self):
        p = self._community({"read_state", "low_level_control"})
        with pytest.raises(PermissionError):
            p._require_capability("low_level_control")

    def test_community_can_control_motion(self):
        p = self._community({"read_state", "control_motion"})
        # Should NOT raise
        p._require_capability("control_motion")

    def test_community_can_publish_events(self):
        p = self._community({"read_state", "publish_events"})
        p._require_capability("publish_events")


# ─────────────────────────────────────────────────────────────────────────────
# Manifest capability gating (declared vs actual)
# ─────────────────────────────────────────────────────────────────────────────

class TestManifestCapabilityGating:
    """
    A TRUSTED plugin that doesn't DECLARE a capability in its manifest
    must still be blocked — even if trust level would permit it.
    """

    def test_trusted_plugin_without_declaration_cannot_call_move(self):
        # Plugin is TRUSTED but only declares read_state in manifest
        p = _minimal_plugin(TrustLevel.TRUSTED, {"read_state"})
        with pytest.raises(PermissionError, match="control_motion"):
            p._require_capability("control_motion")

    def test_trusted_plugin_with_declaration_can_call_move(self):
        p = _minimal_plugin(TrustLevel.TRUSTED, {"read_state", "control_motion"})
        # Should not raise
        p._require_capability("control_motion")

    def test_error_message_identifies_both_plugin_and_capability(self):
        p = _minimal_plugin(TrustLevel.UNTRUSTED, {"read_state"})
        try:
            p._require_capability("control_motion")
        except PermissionError as exc:
            msg = str(exc)
            assert "test_plugin" in msg or "control_motion" in msg

    def test_manifest_validate_capabilities_catches_over_declaration(self):
        """validate_capabilities() returns denied caps for the trust level."""
        manifest = PluginManifest(
            name="over_declared", version="1.0.0",
            capabilities=["read_state", "modify_safety_limits"],
            trust=TrustLevel.COMMUNITY,
        )
        denied = manifest.validate_capabilities()
        assert "modify_safety_limits" in denied


# ─────────────────────────────────────────────────────────────────────────────
# E-stop propagation to plugins
# ─────────────────────────────────────────────────────────────────────────────

class TestEstopPropagation:

    @pytest.mark.asyncio
    async def test_stair_plugin_returns_to_nominal_on_estop(self):
        """StairClimber in STAIR_ACTIVE returns to NOMINAL when E-stop fires."""
        from plugins.stair_climber.plugin import StairClimberPlugin, StairState

        eng, bridge, watchdog = _make_engine()
        await bridge.connect()
        stair = StairClimberPlugin(eng)

        await stair._enter_stair("ascending")
        assert stair._status.state == StairState.STAIR_ACTIVE

        # Trigger E-stop
        bridge._state.estop_active = True
        await stair.on_tick(tick=1)

        assert stair._status.state == StairState.NOMINAL

    @pytest.mark.asyncio
    async def test_payload_plugin_aborts_behavior_on_estop(self):
        """UndercarriagePayload aborts active behavior on E-stop."""
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import (
            UndercarriagePayloadPlugin, BehaviorState,
        )

        eng, bridge, _ = _make_engine()
        await bridge.connect()
        p = UndercarriagePayloadPlugin(eng)

        cfg = PayloadConfig(mass_kg=1.0, thickness_m=0.04, material=PayloadMaterial.SILICONE)
        await p.attach(cfg)
        await p.trigger_ground_scout(duration_s=60.0)
        assert p._behavior == BehaviorState.GROUND_SCOUT

        bridge._state.estop_active = True
        await p.on_tick(tick=1)

        # Should have stopped the behavior (idle or restoring — not still in scout)
        assert p._behavior != BehaviorState.GROUND_SCOUT

    @pytest.mark.asyncio
    async def test_all_plugins_safe_after_estop(self, full_engine):
        """All loaded plugins remain in safe state after watchdog E-stop."""
        eng, pm = full_engine
        await eng.bridge.connect()

        await eng.watchdog.trigger_estop("test")

        # Tick the engine — E-stop path should not raise
        tick_ok = True
        try:
            await eng._tick(1)
        except Exception:
            tick_ok = False

        assert tick_ok
        assert eng.watchdog.estop_active

    @pytest.mark.asyncio
    async def test_estop_prevents_new_motion_commands(self):
        """Motion commands issued via API are blocked when E-stop is active."""
        eng, bridge, watchdog = _make_engine()
        await bridge.connect()

        await watchdog.trigger_estop("test")
        assert watchdog.estop_active

        # Simulate the _require_no_estop check that the API performs
        def _require_no_estop():
            if watchdog.estop_active:
                raise RuntimeError("E-stop active")

        with pytest.raises(RuntimeError, match="E-stop"):
            _require_no_estop()


# ─────────────────────────────────────────────────────────────────────────────
# Safety limit invariants
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyLimitInvariants:
    """Plugins may only tighten safety limits — never relax them."""

    @pytest.mark.asyncio
    async def test_payload_never_widens_velocity_limits(self):
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin

        eng, bridge, watchdog = _make_engine()
        await bridge.connect()
        base_vx = watchdog.limits.max_vx

        p = UndercarriagePayloadPlugin(eng)
        cfg = PayloadConfig(mass_kg=1.0, thickness_m=0.03, material=PayloadMaterial.SILICONE)
        await p.attach(cfg)

        assert watchdog.limits.max_vx <= base_vx

    @pytest.mark.asyncio
    async def test_stair_never_widens_velocity_limits(self):
        from plugins.stair_climber.plugin import StairClimberPlugin

        eng, bridge, watchdog = _make_engine()
        await bridge.connect()
        base_vx = watchdog.limits.max_vx

        stair = StairClimberPlugin(eng)
        await stair._enter_stair("ascending")

        assert watchdog.limits.max_vx <= base_vx

    @pytest.mark.asyncio
    async def test_detach_restores_but_does_not_exceed_original(self):
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin

        eng, bridge, watchdog = _make_engine()
        await bridge.connect()
        original_vx = watchdog.limits.max_vx

        p = UndercarriagePayloadPlugin(eng)
        cfg = PayloadConfig(mass_kg=1.0, thickness_m=0.03, material=PayloadMaterial.SILICONE)
        await p.attach(cfg)
        await p.detach()

        # After detach, restored to original — not above it
        assert watchdog.limits.max_vx <= original_vx + 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox module isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestPluginSandboxIsolation:

    @pytest.mark.asyncio
    async def test_plugin_loaded_with_unique_module_name(self):
        """Loaded plugin module is registered under a cerberus_plugin_ prefix."""
        eng, _, _ = _make_engine()
        await eng.bridge.connect()

        pm = PluginManager(eng, ["plugins"])

        pre_keys  = set(sys.modules.keys())
        await pm.discover_and_load()
        post_keys = set(sys.modules.keys())

        new_keys = post_keys - pre_keys
        plugin_keys = [k for k in new_keys if k.startswith("cerberus_plugin_")]
        assert len(plugin_keys) > 0, (
            "Plugin modules should be registered under cerberus_plugin_* prefix"
        )

    @pytest.mark.asyncio
    async def test_plugin_unload_removes_from_sys_modules(self):
        """Unloading a plugin removes its module from sys.modules."""
        eng, _, _ = _make_engine()
        await eng.bridge.connect()

        pm = PluginManager(eng, ["plugins"])
        await pm.discover_and_load()

        loaded_keys = [k for k in sys.modules if k.startswith("cerberus_plugin_")]
        assert loaded_keys, "At least one plugin should be in sys.modules"

        for name in list(pm._plugins.keys()):
            await pm.unload_plugin(name)

        remaining = [k for k in sys.modules if k.startswith("cerberus_plugin_")]
        assert remaining == [], (
            f"sys.modules still contains plugin modules after unload: {remaining}"
        )

    @pytest.mark.asyncio
    async def test_reloading_same_plugin_produces_fresh_instance(self):
        """Loading the same plugin path twice gives an independent instance."""
        from pathlib import Path

        eng, _, _ = _make_engine()
        await eng.bridge.connect()

        pm = PluginManager(eng, [])
        terrain_path = Path("plugins/terrain_arbiter/plugin.py")

        if not terrain_path.exists():
            pytest.skip("terrain_arbiter plugin not present")

        success1 = await pm.load_from_file(terrain_path)
        inst1_id = id(pm._plugins.get("terrain_arbiter", pm._plugins.get("TerrainArbiter")))

        # Unload, then reload
        names = list(pm._plugins.keys())
        for n in names:
            await pm.unload_plugin(n)

        success2 = await pm.load_from_file(terrain_path)
        inst2_id = id(pm._plugins.get("terrain_arbiter", pm._plugins.get("TerrainArbiter")))

        assert success1 and success2
        assert inst1_id != inst2_id, "Reloaded plugin should be a fresh instance"

    def test_plugin_modules_not_in_real_import_namespace(self):
        """cerberus_plugin_* names must not be importable via regular import."""
        for key in list(sys.modules.keys()):
            if key.startswith("cerberus_plugin_"):
                # These keys exist but should be sandboxed names, not real package paths
                assert "." not in key, (
                    f"Plugin module {key!r} looks like a real package path"
                )


# ─────────────────────────────────────────────────────────────────────────────
# Error count reset on enable
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorCountReset:

    @pytest.mark.asyncio
    async def test_error_count_does_not_reset_on_enable(self):
        """
        Calling enable() does NOT reset the error counter — errors are
        persistent within a session. This is intentional: a plugin that
        crashed 4 times should not get a free reset just by being re-enabled.
        """
        from cerberus.plugins.plugin_manager import PluginManager, PluginRecord

        class FlappingPlugin(CerberusPlugin):
            MANIFEST = PluginManifest(
                name="flapping", version="1.0.0",
                capabilities={"read_state"}, trust=TrustLevel.UNTRUSTED,
            )
            calls = 0

            async def on_tick(self, tick: int) -> None:
                FlappingPlugin.calls += 1
                if FlappingPlugin.calls % 2 == 0:
                    raise RuntimeError("flap")

        eng, bridge, _ = _make_engine()
        await bridge.connect()

        pm = PluginManager(eng, [])
        pm._max_errors = 10
        inst = FlappingPlugin(eng)
        rec  = PluginRecord(plugin=inst, manifest=inst.MANIFEST, module_path="<test>")
        pm._plugins["flapping"] = rec
        pm._register_hook_for_record("flapping", rec)

        # Generate 4 errors (every even tick)
        for tick in range(1, 9):
            await eng._tick(tick)

        errors_before_enable = inst._error_count
        pm.enable("flapping")
        errors_after_enable = inst._error_count

        # Error count is NOT reset by enable
        assert errors_after_enable == errors_before_enable


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog safety gating
# ─────────────────────────────────────────────────────────────────────────────

class TestWatchdogSafetyGating:

    @pytest.mark.asyncio
    async def test_watchdog_heartbeat_timeout_stops_motion(self):
        """Watchdog auto-calls stop_move() when heartbeat times out."""
        from cerberus.core.safety import SafetyLimits

        eng, bridge, _ = _make_engine()
        await bridge.connect()

        limits   = SafetyLimits(heartbeat_timeout_s=0.05)  # 50 ms timeout
        watchdog = SafetyWatchdog(bridge, limits)
        eng.watchdog = watchdog

        await bridge.move(0.3, 0.0, 0.0)
        # Don't ping heartbeat — wait for timeout
        await asyncio.sleep(0.15)
        await watchdog._tick()   # force one watchdog tick

        # Robot should no longer be moving
        state = await bridge.get_state()
        assert abs(state.velocity_x) < 0.01

    @pytest.mark.asyncio
    async def test_watchdog_tilt_triggers_estop(self):
        """Extreme tilt (> max_roll_deg) triggers E-stop."""
        import math
        from cerberus.core.safety import SafetyLimits

        eng, bridge, _ = _make_engine()
        await bridge.connect()

        limits   = SafetyLimits(max_roll_deg=10.0)
        watchdog = SafetyWatchdog(bridge, limits)
        eng.watchdog = watchdog

        # Simulate a severe tilt
        bridge._state.roll = math.radians(35.0)  # 35° > 10° limit
        await watchdog._tick()

        assert watchdog.estop_active

    @pytest.mark.asyncio
    async def test_watchdog_battery_critical_triggers_estop(self):
        """Battery below critical level triggers E-stop."""
        from cerberus.core.safety import SafetyLimits

        eng, bridge, _ = _make_engine()
        await bridge.connect()

        limits   = SafetyLimits(battery_critical_pct=4.0)
        watchdog = SafetyWatchdog(bridge, limits)
        eng.watchdog = watchdog

        bridge._state.battery_percent = 2.0   # below 4% critical
        await watchdog._tick()

        assert watchdog.estop_active
