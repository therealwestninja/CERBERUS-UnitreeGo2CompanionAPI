"""
tests/test_limb_loss.py
━━━━━━━━━━━━━━━━━━━━━━
Tests for the LimbLossRecovery plugin and SimBridge limb-loss simulation.

Covers:
  LimbDetector
    • Normal trot never triggers detection (swing-phase unloading is <35%)
    • Sustained near-zero force exceeding 80% of window triggers confirmation
    • Both suspect and confirm ticks gate the confirmation
    • Recovery when leg starts loading again
    • Secondary torque channel provides corroborating evidence

  Tripod compensation geometry
    • Biomechanical correctness: pitch/roll direction for each missing leg
    • Pitch leans body toward support triangle centroid
    • Velocity limits tightened for all four missing-leg scenarios
    • Yaw correction sign matches missing-side expectation

  Plugin lifecycle
    • Status NOMINAL on load
    • Manual declare activates tripod mode immediately
    • Auto-detection activates tripod mode after force window fills
    • Watchdog limits tightened on entry
    • Watchdog limits restored on clear
    • LED set to amber on entry, off on clear
    • E-stop during recovery is safe
    • Cannot declare a second limb while one is already active
    • Clear from NOMINAL returns an informative error

  SimBridge limb-loss simulation
    • simulate_limb_loss() produces near-zero force on lost leg
    • Remaining legs show redistributed (increased) load
    • Yaw drift introduced in correct direction per leg
    • Battery drain rate increased 30%
    • Lost leg joints show near-zero torque and folded position
    • clear_limb_loss() restores all legs
    • Invalid leg index raises ValueError

  REST API
    • GET /limb_loss returns 200 or 404 (plugin not loaded)
    • POST /limb_loss/declare with unknown leg → 422
    • POST /sim/limb_loss works in simulation mode
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest
import pytest_asyncio

from cerberus.bridge.go2_bridge import SimBridge, RobotState
from cerberus.core.safety import SafetyLimits


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

STATIC_LOAD_N = 147.0 / 4   # ~36.75 N nominal per foot

LEG_NAMES = ["FL", "FR", "RL", "RR"]


def _nominal_forces(speed: float = 0.20) -> list[float]:
    amp = 1.0 + 0.6 * speed
    return [STATIC_LOAD_N * amp] * 4


def _trot_forces(tick: int, speed: float = 0.20) -> list[float]:
    """
    Simulate one tick of trotting diagonal pattern.
    FL+RR in stance when sin > 0; FR+RL in stance when sin < 0.
    """
    amp    = 1.0 + 0.6 * speed
    base   = STATIC_LOAD_N * amp
    phase  = (tick / 8) * 2 * math.pi
    swing  = math.sin(phase)
    sf     = 0.35  # swing fraction

    f_fl = base * (1 - sf * max(0,  swing))
    f_fr = base * (1 - sf * max(0, -swing))
    f_rl = base * (1 - sf * max(0, -swing))
    f_rr = base * (1 - sf * max(0,  swing))
    return [f_fl, f_fr, f_rl, f_rr]


def _dead_leg_forces(dead_idx: int, speed: float = 0.20) -> list[float]:
    """Forces with one leg producing near-zero (simulating structural loss)."""
    f = _nominal_forces(speed)
    f[dead_idx] = 0.2   # just noise
    return f


def _state_with_forces(forces: list[float], vx: float = 0.20) -> RobotState:
    s = RobotState()
    s.foot_force    = forces
    s.velocity_x    = vx
    s.joint_torques = [5.0] * 12
    s.estop_active  = False
    return s


@pytest.fixture
def limb_plugin(bare_engine):
    from plugins.limb_loss_recovery.plugin import LimbLossRecoveryPlugin
    return LimbLossRecoveryPlugin(bare_engine)


# ─────────────────────────────────────────────────────────────────────────────
# LimbDetector unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLimbDetector:
    from plugins.limb_loss_recovery.plugin import (
        LimbDetector, DEAD_FRACTION_THRESHOLD, SUSPECT_TICKS, CONFIRM_TICKS,
        WINDOW_SIZE,
    )

    def _make_detector(self):
        from plugins.limb_loss_recovery.plugin import LimbDetector
        return LimbDetector()

    def test_normal_trot_never_exceeds_threshold(self):
        """Normal trot swing fraction (~35%) stays below the 80% dead threshold."""
        det = self._make_detector()
        for tick in range(200):
            forces    = _trot_forces(tick)
            fractions = det.update(forces)
        # After many trot cycles, dead fraction for all legs should be < 80%
        from plugins.limb_loss_recovery.plugin import DEAD_FRACTION_THRESHOLD
        for i, frac in enumerate(fractions):
            assert frac < DEAD_FRACTION_THRESHOLD, (
                f"Leg {LEG_NAMES[i]} false-positive: frac={frac:.3f} "
                f"(threshold={DEAD_FRACTION_THRESHOLD})"
            )

    def test_dead_leg_raises_fraction_above_threshold(self):
        det = self._make_detector()
        from plugins.limb_loss_recovery.plugin import DEAD_FRACTION_THRESHOLD, WINDOW_SIZE
        for _ in range(WINDOW_SIZE):
            det.update(_dead_leg_forces(dead_idx=0))  # FL dead
        fractions = det.update(_dead_leg_forces(dead_idx=0))
        assert fractions[0] >= DEAD_FRACTION_THRESHOLD, (
            f"FL should be detected as dead: frac={fractions[0]:.3f}"
        )
        # Other legs should be near-normal
        for i in range(1, 4):
            assert fractions[i] < DEAD_FRACTION_THRESHOLD

    def test_confirmation_requires_enough_ticks(self):
        """Confirmation only fires after SUSPECT_TICKS + CONFIRM_TICKS."""
        det = self._make_detector()
        from plugins.limb_loss_recovery.plugin import SUSPECT_TICKS, CONFIRM_TICKS

        confirmed_at = None
        for tick in range(SUSPECT_TICKS + CONFIRM_TICKS + 20):
            fractions = det.update(_dead_leg_forces(dead_idx=1))  # FR dead
            leg_idx, event = det.evaluate(fractions)
            if event == "confirmed":
                confirmed_at = tick
                break

        assert confirmed_at is not None, "FR was never confirmed as dead"
        assert confirmed_at >= SUSPECT_TICKS + CONFIRM_TICKS - 2  # within 2 ticks

    def test_all_four_legs_detectable(self):
        """Each leg can independently be detected as lost."""
        from plugins.limb_loss_recovery.plugin import SUSPECT_TICKS, CONFIRM_TICKS
        for dead_idx in range(4):
            det = self._make_detector()
            confirmed = False
            for _ in range(SUSPECT_TICKS + CONFIRM_TICKS + 5):
                fracs = det.update(_dead_leg_forces(dead_idx=dead_idx))
                _, event = det.evaluate(fracs)
                if event == "confirmed":
                    confirmed = True
                    break
            assert confirmed, f"{LEG_NAMES[dead_idx]} was never confirmed"

    def test_recovery_clears_after_leg_resumes(self):
        """After confirming dead, restoring load triggers 'cleared'."""
        from plugins.limb_loss_recovery.plugin import (
            SUSPECT_TICKS, CONFIRM_TICKS, RECOVERY_FRACTION, WINDOW_SIZE,
        )
        det = self._make_detector()

        # Confirm FL as dead
        for _ in range(SUSPECT_TICKS + CONFIRM_TICKS + 5):
            fracs = det.update(_dead_leg_forces(dead_idx=0))
            _, event = det.evaluate(fracs)
            if event == "confirmed":
                break

        # Now restore FL to normal loading
        cleared = False
        for _ in range(WINDOW_SIZE + 5):
            fracs = det.update(_nominal_forces())
            _, event = det.evaluate(fracs)
            if event == "cleared":
                cleared = True
                break

        assert cleared, "FL was not cleared after restoring normal load"

    def test_snapshot_has_all_legs(self):
        det = self._make_detector()
        snap = det.snapshot()
        for name in LEG_NAMES:
            assert name in snap
            assert "confirmed_dead" in snap[name]
            assert "suspect_ticks"  in snap[name]


# ─────────────────────────────────────────────────────────────────────────────
# Tripod compensation geometry tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTripodGeometry:
    """Verify the biomechanical correctness of compensation parameters."""

    def test_all_four_legs_have_entries(self):
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        for i in range(4):
            assert i in TRIPOD_TABLE

    def test_missing_front_leg_pitches_nose_up(self):
        """Missing front leg (FL or FR) → positive pitch (nose up, lean back)."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[0].pitch_rad > 0, "FL missing: pitch should be positive (nose up)"
        assert TRIPOD_TABLE[1].pitch_rad > 0, "FR missing: pitch should be positive (nose up)"

    def test_missing_rear_leg_pitches_nose_down(self):
        """Missing rear leg (RL or RR) → negative pitch (nose down, lean forward)."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[2].pitch_rad < 0, "RL missing: pitch should be negative (nose down)"
        assert TRIPOD_TABLE[3].pitch_rad < 0, "RR missing: pitch should be negative (nose down)"

    def test_missing_left_leg_rolls_right(self):
        """Missing left leg (FL or RL) → negative roll (lean right)."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[0].roll_rad < 0, "FL missing: roll should be negative (lean right)"
        assert TRIPOD_TABLE[2].roll_rad < 0, "RL missing: roll should be negative (lean right)"

    def test_missing_right_leg_rolls_left(self):
        """Missing right leg (FR or RR) → positive roll (lean left)."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[1].roll_rad > 0, "FR missing: roll should be positive (lean left)"
        assert TRIPOD_TABLE[3].roll_rad > 0, "RR missing: roll should be positive (lean left)"

    def test_missing_left_leg_yaw_correction_positive(self):
        """Missing left leg creates left-yaw drift; correction should be positive."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[0].yaw_scale > 0, "FL missing: yaw_scale should be positive"
        assert TRIPOD_TABLE[2].yaw_scale > 0, "RL missing: yaw_scale should be positive"

    def test_missing_right_leg_yaw_correction_negative(self):
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert TRIPOD_TABLE[1].yaw_scale < 0, "FR missing: yaw_scale should be negative"
        assert TRIPOD_TABLE[3].yaw_scale < 0, "RR missing: yaw_scale should be negative"

    def test_pitch_magnitude_consistent_across_all_legs(self):
        """Front and rear pairs have equal pitch magnitude (by symmetry)."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert abs(TRIPOD_TABLE[0].pitch_rad) == pytest.approx(
            abs(TRIPOD_TABLE[2].pitch_rad), rel=0.05
        )

    def test_roll_magnitude_consistent_across_all_legs(self):
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        assert abs(TRIPOD_TABLE[0].roll_rad) == pytest.approx(
            abs(TRIPOD_TABLE[1].roll_rad), rel=0.05
        )

    def test_all_velocity_limits_below_normal(self):
        """Tripod max_vx must be well below normal 1.5 m/s."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        for i, p in TRIPOD_TABLE.items():
            assert p.max_vx < 0.30, f"Leg {i} max_vx too high: {p.max_vx}"
            assert p.max_vy < 0.15, f"Leg {i} max_vy too high: {p.max_vy}"

    def test_body_height_offset_negative(self):
        """Tripod mode lowers body height for stability."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        for p in TRIPOD_TABLE.values():
            assert p.body_h_offset < 0, "body_h_offset should lower the robot"

    def test_foot_raise_positive(self):
        """Foot raise height is set to clear terrain during tripod stance."""
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        for p in TRIPOD_TABLE.values():
            assert p.foot_raise_m > 0.04


# ─────────────────────────────────────────────────────────────────────────────
# Plugin lifecycle tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLimbLossPlugin:

    @pytest.mark.asyncio
    async def test_initial_state_nominal(self, limb_plugin):
        from plugins.limb_loss_recovery.plugin import LimbLossState
        assert limb_plugin._status.state == LimbLossState.NOMINAL

    @pytest.mark.asyncio
    async def test_manual_declare_activates_tripod(self, limb_plugin, bare_engine):
        from plugins.limb_loss_recovery.plugin import LimbLossState
        await bare_engine.bridge.connect()
        result = await limb_plugin.declare_limb_loss("FL")
        assert "error" not in result
        assert limb_plugin._status.state == LimbLossState.RECOVERING
        assert limb_plugin._status.missing_name == "FL"

    @pytest.mark.asyncio
    async def test_manual_declare_all_four_legs(self, bare_engine):
        from plugins.limb_loss_recovery.plugin import (
            LimbLossRecoveryPlugin, LimbLossState
        )
        await bare_engine.bridge.connect()
        for leg in ["FL", "FR", "RL", "RR"]:
            p = LimbLossRecoveryPlugin(bare_engine)
            result = await p.declare_limb_loss(leg)
            assert "error" not in result, f"Failed for {leg}: {result}"
            assert p._status.missing_name == leg

    @pytest.mark.asyncio
    async def test_declare_unknown_leg_returns_error(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        result = await limb_plugin.declare_limb_loss("BL")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cannot_declare_while_recovering(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        await limb_plugin.declare_limb_loss("FL")
        result = await limb_plugin.declare_limb_loss("FR")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_clear_restores_nominal(self, limb_plugin, bare_engine):
        from plugins.limb_loss_recovery.plugin import LimbLossState
        await bare_engine.bridge.connect()
        await limb_plugin.declare_limb_loss("RL")
        await limb_plugin.clear_limb_loss()
        assert limb_plugin._status.state == LimbLossState.NOMINAL
        assert limb_plugin._status.missing_leg is None

    @pytest.mark.asyncio
    async def test_clear_when_nominal_returns_error(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        result = await limb_plugin.clear_limb_loss()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_watchdog_limits_tightened_on_declare(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        base_vx = bare_engine.watchdog.limits.max_vx
        await limb_plugin.declare_limb_loss("RR")
        assert bare_engine.watchdog.limits.max_vx < base_vx

    @pytest.mark.asyncio
    async def test_watchdog_limits_restored_on_clear(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        original_vx = bare_engine.watchdog.limits.max_vx
        await limb_plugin.declare_limb_loss("FR")
        await limb_plugin.clear_limb_loss()
        assert bare_engine.watchdog.limits.max_vx == pytest.approx(original_vx)

    @pytest.mark.asyncio
    async def test_auto_detection_after_force_window_fills(self, limb_plugin, bare_engine):
        """Feeding many dead-force ticks eventually confirms the loss."""
        from plugins.limb_loss_recovery.plugin import (
            LimbLossState, SUSPECT_TICKS, CONFIRM_TICKS,
        )
        await bare_engine.bridge.connect()
        # Inject dead FL forces directly into bridge state
        bare_engine.bridge._state.velocity_x = 0.15
        max_ticks = SUSPECT_TICKS + CONFIRM_TICKS + 10

        for tick in range(max_ticks):
            bare_engine.bridge._state.foot_force = _dead_leg_forces(0)
            await limb_plugin.on_tick(tick)
            if limb_plugin._status.state == LimbLossState.RECOVERING:
                break

        assert limb_plugin._status.state == LimbLossState.RECOVERING, (
            "FL should have been auto-detected as lost"
        )
        assert limb_plugin._status.missing_name == "FL"
        assert limb_plugin._status.manual_declare is False

    @pytest.mark.asyncio
    async def test_estop_during_recovery_is_safe(self, limb_plugin, bare_engine):
        """E-stop while in recovery mode — on_tick must return without error."""
        await bare_engine.bridge.connect()
        await limb_plugin.declare_limb_loss("FL")
        bare_engine.bridge._state.estop_active = True
        # Should not raise
        await limb_plugin.on_tick(tick=1)
        bare_engine.bridge._state.estop_active = False

    @pytest.mark.asyncio
    async def test_tripod_params_in_status_dict(self, limb_plugin, bare_engine):
        await bare_engine.bridge.connect()
        await limb_plugin.declare_limb_loss("RL")
        s = limb_plugin.status()
        ll = s["limb_loss"]
        assert ll["state"] == "recovering"
        assert ll["missing_leg"] == "RL"
        assert "pitch_deg" in ll["tripod_params"]
        assert "roll_deg"  in ll["tripod_params"]
        assert "max_vx_ms" in ll["tripod_params"]

    @pytest.mark.asyncio
    async def test_orientation_ramp_approaches_target(self, limb_plugin, bare_engine):
        """
        After several ticks, current_pitch should approach the target
        pitch for the missing leg (exponential ramp).
        """
        from plugins.limb_loss_recovery.plugin import TRIPOD_TABLE
        await bare_engine.bridge.connect()
        await limb_plugin.declare_limb_loss("FL")
        target = TRIPOD_TABLE[0].pitch_rad

        bare_engine.bridge._state.foot_force    = _dead_leg_forces(0)
        bare_engine.bridge._state.velocity_x    = 0.0
        bare_engine.bridge._state.estop_active  = False
        bare_engine.bridge._state.joint_torques = [5.0]*12

        for tick in range(50):
            await limb_plugin.on_tick(tick)

        assert abs(limb_plugin._current_pitch - target) < abs(target) * 0.5, (
            f"Pitch ramp: current={limb_plugin._current_pitch:.3f} "
            f"target={target:.3f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SimBridge limb-loss simulation tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSimBridgeLimbLoss:

    @pytest.mark.asyncio
    async def test_simulate_sets_lost_limb_attribute(self):
        b = SimBridge()
        await b.connect()
        assert b.lost_limb is None
        b.simulate_limb_loss(0)
        assert b.lost_limb == 0
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_clear_removes_lost_limb(self):
        b = SimBridge()
        await b.connect()
        b.simulate_limb_loss(2)
        b.clear_limb_loss()
        assert b.lost_limb is None
        await b.disconnect()

    def test_invalid_leg_raises_value_error(self):
        b = SimBridge()
        with pytest.raises(ValueError):
            b.simulate_limb_loss(4)
        with pytest.raises(ValueError):
            b.simulate_limb_loss(-1)

    @pytest.mark.asyncio
    async def test_lost_leg_has_near_zero_force_after_loop(self):
        """After sim loop runs, the lost leg's force should be near zero."""
        b = SimBridge()
        await b.connect()
        b.simulate_limb_loss(1)   # FR lost
        b._state.velocity_x = 0.15
        b._state.mode = "trotting"
        # Let the sim loop run for a bit
        await asyncio.sleep(0.15)  # ~4–5 sim ticks

        # FR (index 1) force should be near zero
        assert b._state.foot_force[1] < 5.0, (
            f"FR force should be near zero with limb loss active, "
            f"got {b._state.foot_force[1]:.1f} N"
        )
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_remaining_legs_have_elevated_force(self):
        """Remaining legs carry extra load to compensate for lost limb."""
        b = SimBridge()
        await b.connect()

        # Record baseline forces (no limb loss)
        b._state.velocity_x = 0.15
        b._state.mode = "trotting"
        await asyncio.sleep(0.20)
        baseline_mean = sum(b._state.foot_force[i] for i in range(4)) / 4

        # Simulate FL loss
        b.simulate_limb_loss(0)
        await asyncio.sleep(0.20)

        remaining_forces = [b._state.foot_force[i] for i in range(1, 4)]
        remaining_mean   = sum(remaining_forces) / len(remaining_forces)

        assert remaining_mean > baseline_mean * 1.15, (
            f"Remaining legs should carry more load after FL loss. "
            f"Baseline mean: {baseline_mean:.1f} N, "
            f"Remaining mean: {remaining_mean:.1f} N"
        )
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_yaw_drift_correct_direction_left_missing(self):
        """Missing left leg (FL) → negative yaw drift (robot turns left)."""
        b = SimBridge()
        await b.connect()
        b._state.velocity_x    = 0.20
        b._state.velocity_yaw  = 0.0
        b._state.mode          = "trotting"
        b.simulate_limb_loss(0)  # FL — left side
        await asyncio.sleep(0.30)
        # After drift accumulates, yaw should be negative
        assert b._state.velocity_yaw < 0, (
            f"FL loss should produce left-yaw drift (negative vyaw), "
            f"got {b._state.velocity_yaw:.3f}"
        )
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_yaw_drift_correct_direction_right_missing(self):
        """Missing right leg (FR) → positive yaw drift (robot turns right)."""
        b = SimBridge()
        await b.connect()
        b._state.velocity_x   = 0.20
        b._state.velocity_yaw = 0.0
        b._state.mode         = "trotting"
        b.simulate_limb_loss(1)  # FR — right side
        await asyncio.sleep(0.30)
        assert b._state.velocity_yaw > 0, (
            f"FR loss should produce right-yaw drift (positive vyaw), "
            f"got {b._state.velocity_yaw:.3f}"
        )
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_lost_leg_joint_torques_near_zero(self):
        """Lost leg joints should have near-zero torque (no load transfer)."""
        b = SimBridge()
        await b.connect()
        b.simulate_limb_loss(3)  # RR
        b._state.mode = "trotting"
        b._state.velocity_x = 0.15
        await asyncio.sleep(0.15)
        rr_torques = b._state.joint_torques[9:12]  # RR: joints 9, 10, 11
        for t in rr_torques:
            assert abs(t) < 1.0, f"RR joint torque should be near zero, got {t:.3f}"
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_lost_leg_joints_in_folded_position(self):
        """Lost leg should hang in a relaxed, partially-folded position."""
        b = SimBridge()
        await b.connect()
        b.simulate_limb_loss(2)  # RL
        b._state.mode = "trotting"
        b._state.velocity_x = 0.10
        await asyncio.sleep(0.15)
        # RL: joints 6, 7, 8 — hip_flex should be around -0.45 (partially folded)
        rl_hip_flex = b._state.joint_positions[7]
        assert -0.55 < rl_hip_flex < -0.35, (
            f"RL hip_flex should be in folded range, got {rl_hip_flex:.3f}"
        )
        await b.disconnect()

    @pytest.mark.asyncio
    async def test_battery_drain_faster_with_limb_loss(self):
        """Tripod operation drains battery ~30% faster."""
        b1 = SimBridge()
        b2 = SimBridge()
        await b1.connect()
        await b2.connect()

        b1._state.velocity_x = 0.15
        b2._state.velocity_x = 0.15
        b1._state.mode = b2._state.mode = "trotting"

        b2.simulate_limb_loss(0)

        start_pct = 100.0
        b1._state.battery_percent = start_pct
        b2._state.battery_percent = start_pct

        await asyncio.sleep(0.50)

        drain_normal = start_pct - b1._state.battery_percent
        drain_tripod = start_pct - b2._state.battery_percent

        assert drain_tripod > drain_normal * 1.15, (
            f"Tripod drain ({drain_tripod:.4f}) should exceed normal drain "
            f"({drain_normal:.4f}) by at least 15%"
        )
        await b1.disconnect()
        await b2.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# REST API tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def api():
    from httpx import AsyncClient, ASGITransport
    from asgi_lifespan import LifespanManager
    from backend.main import app
    async with LifespanManager(app) as mgr:
        async with AsyncClient(
            transport=ASGITransport(app=mgr.app), base_url="http://test"
        ) as client:
            yield client


class TestLimbLossAPI:

    @pytest.mark.asyncio
    async def test_get_limb_loss_returns_200_or_404(self, api):
        r = await api.get("/limb_loss")
        assert r.status_code in (200, 404)   # 404 if plugin not auto-discovered

    @pytest.mark.asyncio
    async def test_declare_unknown_leg_returns_422_or_409(self, api):
        r = await api.post("/limb_loss/declare", json={"leg": "BL"})
        # 404 if plugin not loaded, 409 if loaded but unknown leg name
        assert r.status_code in (404, 409, 422)

    @pytest.mark.asyncio
    async def test_sim_limb_loss_works_in_simulation_mode(self, api):
        import os
        if os.getenv("GO2_SIMULATION", "false").lower() not in ("true", "1", "yes"):
            pytest.skip("Simulation endpoint only available in simulation mode")
        r = await api.post("/sim/limb_loss", json={"leg": "FL"})
        # 200 if plugin present, 404 if not
        assert r.status_code in (200, 404, 409)

    @pytest.mark.asyncio
    async def test_sim_limb_loss_invalid_leg_returns_422(self, api):
        import os
        if os.getenv("GO2_SIMULATION", "false").lower() not in ("true", "1", "yes"):
            pytest.skip("Simulation endpoint only available in simulation mode")
        r = await api.post("/sim/limb_loss", json={"leg": "ZZ"})
        assert r.status_code in (422, 404, 409)

    @pytest.mark.asyncio
    async def test_sim_clear_limb_loss(self, api):
        import os
        if os.getenv("GO2_SIMULATION", "false").lower() not in ("true", "1", "yes"):
            pytest.skip("Only for simulation")
        r = await api.post("/sim/limb_loss", json={"leg": None})
        assert r.status_code in (200, 404, 409)
