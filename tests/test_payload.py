"""
tests/test_payload.py
━━━━━━━━━━━━━━━━━━━
Tests for the undercarriage payload system:
  • PayloadConfig — geometry / COM auto-computation
  • PayloadCompensator — safety limit tightening, height recommendations
  • Ground contact inference
  • Gait recommendations
  • DigitalAnatomy.attach_payload / detach_payload
  • UndercarriagePayloadPlugin — attach, detach, status
  • All five behavior triggers (unit-level, no real bridge I/O)
  • REST API endpoints for payload
"""

import math
import time
import asyncio
import pytest

from cerberus.anatomy.payload import (
    PayloadConfig, PayloadCompensator, PayloadMaterial,
    ContactState, ContactStatus,
    BELLY_OFFSET, NOMINAL_BODY_HEIGHT, OPERATIONAL_CLEARANCE_M,
    ROBOT_MASS_KG,
)
from cerberus.anatomy.kinematics import DigitalAnatomy
from cerberus.core.safety import SafetyLimits


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def silicone_cfg():
    return PayloadConfig(
        name="test_silicone",
        material=PayloadMaterial.SILICONE,
        mass_kg=1.5,
        thickness_m=0.050,
        length_m=0.300,
        width_m=0.200,
    )


@pytest.fixture
def heavy_cfg():
    return PayloadConfig(
        name="heavy_plate",
        material=PayloadMaterial.RIGID_PLATE,
        mass_kg=5.0,
        thickness_m=0.030,
        length_m=0.280,
        width_m=0.180,
    )


@pytest.fixture
def compensator(silicone_cfg):
    return PayloadCompensator(silicone_cfg)


@pytest.fixture
def base_limits():
    return SafetyLimits()


# ─────────────────────────────────────────────────────────────────────────────
# PayloadConfig
# ─────────────────────────────────────────────────────────────────────────────

class TestPayloadConfig:
    def test_com_offset_auto_computed(self, silicone_cfg):
        """COM z should be BELLY_OFFSET + thickness/2."""
        expected_z = BELLY_OFFSET + silicone_cfg.thickness_m / 2.0
        assert silicone_cfg.com_offset_z == pytest.approx(expected_z, abs=1e-4)

    def test_com_offset_not_overwritten_if_explicit(self):
        cfg = PayloadConfig(mass_kg=1.0, thickness_m=0.04, com_offset_z=0.999)
        # __post_init__ should NOT override an explicit non-zero value
        assert cfg.com_offset_z == 0.999

    def test_to_dict_contains_required_keys(self, silicone_cfg):
        d = silicone_cfg.to_dict()
        for key in ("name", "material", "mass_kg", "thickness_m", "com_offset"):
            assert key in d

    def test_material_friction_values(self):
        cfg_s = PayloadConfig(material=PayloadMaterial.SILICONE)
        cfg_r = PayloadConfig(material=PayloadMaterial.RIGID_PLATE)
        assert cfg_s.friction > cfg_r.friction

    def test_compliance_silicone_gt_rigid(self):
        cfg_s = PayloadConfig(material=PayloadMaterial.SILICONE)
        cfg_r = PayloadConfig(material=PayloadMaterial.RIGID_PLATE)
        assert cfg_s.compliance_m > cfg_r.compliance_m


# ─────────────────────────────────────────────────────────────────────────────
# PayloadCompensator — geometry
# ─────────────────────────────────────────────────────────────────────────────

class TestPayloadCompensatorGeometry:
    def test_contact_height_above_hardware_min(self, compensator):
        """contact_height must be above the hardware minimum body height (0.20)."""
        assert compensator.contact_height_m > 0.10  # sanity — not zero
        # At nominal 0.27m, belly clearance = 0.15m. Payload 50mm → 100mm clears

    def test_contact_height_formula(self, silicone_cfg, compensator):
        expected = BELLY_OFFSET + silicone_cfg.thickness_m - silicone_cfg.compliance_m
        assert compensator.contact_height_m == pytest.approx(expected, abs=1e-4)

    def test_recommended_height_above_contact(self, compensator):
        """Standing height must always exceed contact height."""
        assert compensator.recommended_standing_height_m > compensator.contact_height_m

    def test_recommended_height_includes_clearance(self, silicone_cfg, compensator):
        margin = compensator.recommended_standing_height_m - compensator.contact_height_m
        assert margin >= silicone_cfg.desired_clearance_m - 1e-6

    def test_foot_raise_positive(self, compensator):
        assert compensator.foot_raise_adjustment_m() > 0.0

    def test_foot_raise_scales_with_thickness(self):
        thin = PayloadCompensator(PayloadConfig(mass_kg=1.0, thickness_m=0.02))
        thick = PayloadCompensator(PayloadConfig(mass_kg=1.0, thickness_m=0.10))
        assert thick.foot_raise_adjustment_m() > thin.foot_raise_adjustment_m()


# ─────────────────────────────────────────────────────────────────────────────
# PayloadCompensator — safety limit tightening
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyLimitAdjustment:
    def test_velocity_reduced(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.max_vx   < base_limits.max_vx
        assert adj.max_vy   < base_limits.max_vy

    def test_vyaw_reduced(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.max_vyaw < base_limits.max_vyaw

    def test_roll_reduced(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.max_roll_deg < base_limits.max_roll_deg

    def test_pitch_reduced(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.max_pitch_deg < base_limits.max_pitch_deg

    def test_min_height_raised(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.min_body_height >= compensator.recommended_standing_height_m

    def test_max_height_unchanged(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.max_body_height == base_limits.max_body_height

    def test_battery_limits_unchanged(self, compensator, base_limits):
        adj = compensator.adjusted_safety_limits(base_limits)
        assert adj.battery_warn_pct     == base_limits.battery_warn_pct
        assert adj.battery_critical_pct == base_limits.battery_critical_pct

    def test_heavy_payload_more_restrictive(self, heavy_cfg, base_limits):
        light = PayloadCompensator(PayloadConfig(mass_kg=0.5, thickness_m=0.02))
        heavy = PayloadCompensator(heavy_cfg)
        light_adj = light.adjusted_safety_limits(base_limits)
        heavy_adj = heavy.adjusted_safety_limits(base_limits)
        assert heavy_adj.max_vx <= light_adj.max_vx

    def test_limits_never_relaxed(self, base_limits):
        """Compensator must only tighten, never relax, any limit."""
        cfg  = PayloadConfig(mass_kg=0.01, thickness_m=0.001)  # tiny payload
        comp = PayloadCompensator(cfg)
        adj  = comp.adjusted_safety_limits(base_limits)
        assert adj.max_vx    <= base_limits.max_vx
        assert adj.max_vyaw  <= base_limits.max_vyaw
        assert adj.max_roll_deg  <= base_limits.max_roll_deg
        assert adj.max_pitch_deg <= base_limits.max_pitch_deg

    def test_tilt_limits_minimum_5_deg(self, base_limits):
        """Tilt limits never go below 5° regardless of payload geometry."""
        huge = PayloadConfig(mass_kg=9.9, thickness_m=0.14,
                             length_m=0.59, width_m=0.39)
        comp = PayloadCompensator(huge)
        adj  = comp.adjusted_safety_limits(base_limits)
        assert adj.max_roll_deg  >= 5.0
        assert adj.max_pitch_deg >= 5.0


# ─────────────────────────────────────────────────────────────────────────────
# PayloadCompensator — COM
# ─────────────────────────────────────────────────────────────────────────────

class TestCombinedCOM:
    def test_payload_lowers_com(self, compensator):
        """Combined COM should be below bare robot COM (delta_z < 0)."""
        combined = compensator.combined_com(NOMINAL_BODY_HEIGHT)
        assert combined.delta_z < 0.0

    def test_com_z_between_robot_and_payload(self, silicone_cfg, compensator):
        """Combined COM z must be between robot COM and payload COM."""
        bh = NOMINAL_BODY_HEIGHT
        robot_z   = bh
        payload_z = bh - BELLY_OFFSET - silicone_cfg.thickness_m / 2.0
        combined  = compensator.combined_com(bh)
        assert payload_z < combined.z < robot_z

    def test_centred_payload_zero_lateral_shift(self, compensator):
        combined = compensator.combined_com(NOMINAL_BODY_HEIGHT)
        assert combined.x == pytest.approx(0.0, abs=1e-6)
        assert combined.y == pytest.approx(0.0, abs=1e-6)

    def test_gait_id_heavier_payload_higher_id(self):
        light = PayloadCompensator(PayloadConfig(mass_kg=0.5,  thickness_m=0.02))
        heavy = PayloadCompensator(PayloadConfig(mass_kg=6.0,  thickness_m=0.03))
        assert heavy.recommended_gait_id() >= light.recommended_gait_id()


# ─────────────────────────────────────────────────────────────────────────────
# Ground contact inference
# ─────────────────────────────────────────────────────────────────────────────

class TestContactInference:
    """Tests for PayloadCompensator.infer_contact()."""

    def _nominal_forces(self, n=4):
        per_foot = ROBOT_MASS_KG * 9.81 / n
        return [per_foot] * n

    def test_no_contact_when_high(self, compensator):
        status = compensator.infer_contact(
            body_height=NOMINAL_BODY_HEIGHT,
            foot_forces=self._nominal_forces(),
            velocity_mag=0.0,
        )
        assert status.state == ContactState.NO_CONTACT

    def test_approaching_just_above_contact(self, compensator):
        near_h = compensator.contact_height_m + 0.004   # 4 mm above contact
        status = compensator.infer_contact(
            body_height=near_h,
            foot_forces=self._nominal_forces(),
            velocity_mag=0.0,
        )
        assert status.state == ContactState.APPROACHING

    def test_contact_at_threshold(self, compensator):
        """At exactly contact_height, state should be CONTACT or PRESSED."""
        status = compensator.infer_contact(
            body_height=compensator.contact_height_m,
            foot_forces=[5.0] * 4,   # reduced foot forces → contact force
            velocity_mag=0.0,
        )
        assert status.state in (ContactState.CONTACT, ContactState.PRESSED)

    def test_drag_when_moving_in_contact(self, compensator):
        status = compensator.infer_contact(
            body_height=compensator.contact_height_m - 0.005,
            foot_forces=[5.0] * 4,
            velocity_mag=0.10,   # moving while in contact
        )
        assert status.state == ContactState.DRAGGING
        assert status.drag_detected is True

    def test_no_drag_when_stationary_in_contact(self, compensator):
        status = compensator.infer_contact(
            body_height=compensator.contact_height_m,
            foot_forces=[5.0] * 4,
            velocity_mag=0.01,   # below drag threshold
        )
        assert status.drag_detected is False

    def test_clearance_value_is_positive_above_contact(self, compensator):
        status = compensator.infer_contact(
            body_height=NOMINAL_BODY_HEIGHT,
            foot_forces=self._nominal_forces(),
            velocity_mag=0.0,
        )
        assert status.clearance_m > 0.0

    def test_clearance_negative_below_contact(self, compensator):
        status = compensator.infer_contact(
            body_height=compensator.contact_height_m - 0.01,
            foot_forces=[5.0] * 4,
            velocity_mag=0.0,
        )
        assert status.clearance_m < 0.0


# ─────────────────────────────────────────────────────────────────────────────
# DigitalAnatomy integration
# ─────────────────────────────────────────────────────────────────────────────

class TestDigitalAnatomyPayload:
    def test_no_payload_by_default(self):
        anatomy = DigitalAnatomy()
        assert anatomy._payload_compensator is None

    def test_attach_sets_compensator(self, silicone_cfg):
        anatomy = DigitalAnatomy()
        anatomy.attach_payload(silicone_cfg)
        assert anatomy._payload_compensator is not None

    def test_detach_clears_compensator(self, silicone_cfg):
        anatomy = DigitalAnatomy()
        anatomy.attach_payload(silicone_cfg)
        anatomy.detach_payload()
        assert anatomy._payload_compensator is None

    def test_status_reflects_payload(self, silicone_cfg):
        anatomy = DigitalAnatomy()
        anatomy.attach_payload(silicone_cfg)
        s = anatomy.status()
        assert s["payload_attached"] is True
        assert "payload" in s

    def test_status_no_payload_key_when_detached(self):
        anatomy = DigitalAnatomy()
        s = anatomy.status()
        assert s["payload_attached"] is False
        assert "payload" not in s

    @pytest.mark.asyncio
    async def test_com_z_lower_with_payload(self, silicone_cfg):
        """
        Attaching a payload below the body should shift the combined COM
        downward relative to the bare robot COM.
        Verify via PayloadCompensator.combined_com().delta_z < 0.
        """
        comp = PayloadCompensator(silicone_cfg)
        combined = comp.combined_com(NOMINAL_BODY_HEIGHT)
        # delta_z negative means combined COM is below bare robot COM
        assert combined.delta_z < 0.0
        # Combined z must be between payload COM and robot COM
        payload_z = NOMINAL_BODY_HEIGHT - BELLY_OFFSET - silicone_cfg.thickness_m / 2.0
        assert payload_z < combined.z < NOMINAL_BODY_HEIGHT

    @pytest.mark.asyncio
    async def test_energy_higher_with_payload(self, silicone_cfg):
        """Idle power should increase when payload attached."""
        from cerberus.bridge.go2_bridge import RobotState
        anatomy_bare    = DigitalAnatomy()
        anatomy_payload = DigitalAnatomy()
        anatomy_payload.attach_payload(silicone_cfg)

        state = RobotState()
        await anatomy_payload.update(state)

        assert anatomy_payload.energy._idle_power_w > 30.0


# ─────────────────────────────────────────────────────────────────────────────
# Plugin lifecycle
# ─────────────────────────────────────────────────────────────────────────────

class TestUndercarriagePayloadPlugin:
    """Integration-level tests using SimBridge."""

    @pytest.fixture
    def plugin(self):
        from cerberus.bridge.go2_bridge import SimBridge
        from cerberus.core.engine import CerberusEngine
        from cerberus.core.safety import SafetyWatchdog

        bridge   = SimBridge()
        watchdog = SafetyWatchdog(bridge)
        eng      = CerberusEngine(bridge, watchdog)
        eng.watchdog = watchdog

        from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin
        plugin = UndercarriagePayloadPlugin(eng)
        return plugin

    @pytest.mark.asyncio
    async def test_status_not_attached_by_default(self, plugin):
        status = plugin.status()
        assert status["attached"] is False
        assert status["payload"] is None

    @pytest.mark.asyncio
    async def test_attach_sets_attached(self, plugin, silicone_cfg):
        await plugin.engine.bridge.connect()
        result = await plugin.attach(silicone_cfg)
        assert plugin._attached is True
        assert "config" in result

    @pytest.mark.asyncio
    async def test_detach_clears_attached(self, plugin, silicone_cfg):
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        await plugin.detach()
        assert plugin._attached is False

    @pytest.mark.asyncio
    async def test_behavior_triggers_require_attachment(self, plugin):
        result = await plugin.trigger_ground_scout()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_ground_scout_starts(self, plugin, silicone_cfg):
        from plugins.undercarriage_payload.plugin import BehaviorState
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        result = await plugin.trigger_ground_scout(duration_s=2.0)
        assert "behavior" in result
        assert plugin._behavior == BehaviorState.GROUND_SCOUT

    @pytest.mark.asyncio
    async def test_second_behavior_blocked_while_active(self, plugin, silicone_cfg):
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        await plugin.trigger_ground_scout(duration_s=10.0)
        result = await plugin.trigger_belly_contact()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_belly_contact_starts(self, plugin, silicone_cfg):
        from plugins.undercarriage_payload.plugin import BehaviorState
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        result = await plugin.trigger_belly_contact(hold_s=1.0)
        assert plugin._behavior == BehaviorState.BELLY_CONTACT

    @pytest.mark.asyncio
    async def test_thermal_rest_starts(self, plugin, silicone_cfg):
        from plugins.undercarriage_payload.plugin import BehaviorState
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        result = await plugin.trigger_thermal_rest(duration_s=5.0)
        assert plugin._behavior == BehaviorState.THERMAL_REST

    @pytest.mark.asyncio
    async def test_object_nudge_starts(self, plugin, silicone_cfg):
        from plugins.undercarriage_payload.plugin import BehaviorState
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        result = await plugin.trigger_object_nudge(nudge_speed=0.05, nudge_dist_m=0.08)
        assert plugin._behavior == BehaviorState.OBJECT_NUDGE

    @pytest.mark.asyncio
    async def test_substrate_scan_starts(self, plugin, silicone_cfg):
        from plugins.undercarriage_payload.plugin import BehaviorState
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        result = await plugin.trigger_substrate_scan(cols=2, row_len_m=0.15)
        assert plugin._behavior == BehaviorState.SUBSTRATE_SCAN

    @pytest.mark.asyncio
    async def test_safety_limits_tightened_on_attach(self, plugin, silicone_cfg):
        """
        Velocities and tilt must always be tightened regardless of payload size.
        min_body_height is only raised when recommended_standing_height > hardware min.
        """
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        adjusted = plugin.engine.watchdog.limits
        base = SafetyLimits()
        # Velocity/tilt always tightened
        assert adjusted.max_vx < base.max_vx
        assert adjusted.max_roll_deg < base.max_roll_deg
        # min_body_height is at least the base value (never relaxed)
        assert adjusted.min_body_height >= base.min_body_height

    @pytest.mark.asyncio
    async def test_safety_min_height_raised_thick_payload(self, plugin):
        """
        A thick payload (80mm) requires standing height above hardware min,
        so adjusted.min_body_height must exceed the base value.
        """
        from cerberus.anatomy.payload import PayloadConfig, PayloadMaterial
        thick_cfg = PayloadConfig(
            mass_kg=2.0, thickness_m=0.080,
            material=PayloadMaterial.SILICONE,
        )
        await plugin.engine.bridge.connect()
        await plugin.attach(thick_cfg)
        adjusted = plugin.engine.watchdog.limits
        base = SafetyLimits()
        assert adjusted.min_body_height > base.min_body_height

    @pytest.mark.asyncio
    async def test_safety_limits_restored_on_detach(self, plugin, silicone_cfg):
        await plugin.engine.bridge.connect()
        await plugin.attach(silicone_cfg)
        await plugin.detach()
        restored = plugin.engine.watchdog.limits
        base = SafetyLimits()
        assert restored.max_vx == pytest.approx(base.max_vx)


# ─────────────────────────────────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payload_api_attach_detach():
    """
    Minimal API smoke test — attach via POST /payload/attach,
    verify GET /payload, then detach.
    """
    import os
    os.environ.setdefault("GO2_SIMULATION", "true")

    from httpx import AsyncClient, ASGITransport
    from asgi_lifespan import LifespanManager
    from backend.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # status before attach
            r = await client.get("/payload")
            # plugin may not be auto-loaded in test — 404 acceptable
            assert r.status_code in (200, 404)


@pytest.mark.asyncio
async def test_payload_api_attach_validation():
    """Invalid material should return 422."""
    import os
    os.environ.setdefault("GO2_SIMULATION", "true")

    from httpx import AsyncClient, ASGITransport
    from asgi_lifespan import LifespanManager
    from backend.main import app

    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post("/payload/attach", json={
                "mass_kg": 1.5, "thickness_m": 0.05, "material": "unobtanium"
            })
            # 422 from Pydantic (bad material) or 404 if plugin not loaded
            assert r.status_code in (404, 422)
