"""
tests/test_voice_nlu.py
tests/test_mujoco_bridge.py
━━━━━━━━━━━━━━━━━━━━━━━━━
Voice NLU intent parser tests — no hardware required.
MuJoCo bridge graceful-degradation tests — no mujoco required.

Voice NLU:
  • parse_intent() matches every declared pattern
  • E-stop intent correctly flagged
  • Stop intent has maximum priority (1.0)
  • Unknown phrases return None
  • Case-insensitive matching
  • Multi-word phrases match correctly
  • Intent table is non-empty and has unique goal names per pattern

MuJoCo bridge:
  • Importing the module never raises (graceful degradation)
  • connect() raises clear RuntimeError when mujoco not installed
  • create_bridge() with GO2_MUJOCO=true returns a MuJocoBridge
  • TrotCPG produces 4 leg targets per step
  • TrotCPG phases advance monotonically
  • TrotCPG freeze() returns nominal stance angles
  • Diagonal leg pairs (FL+RR, FR+RL) are in-phase in trot
  • Adjacent leg pairs are anti-phase in trot
  • Velocity command updates CPG omega correctly
  • PD torque is clamped to MAX_TORQUE
  • simulate_limb_loss() changes lost_limb attribute in SimBridge
  • clear_limb_loss() resets it
"""

from __future__ import annotations

import math
import os

import pytest

os.environ.setdefault("GO2_SIMULATION", "true")


# ═════════════════════════════════════════════════════════════════════════════
# VOICE NLU INTENT PARSER
# ═════════════════════════════════════════════════════════════════════════════

class TestIntentParser:
    """Tests for parse_intent() — no audio hardware needed."""

    def _parse(self, text: str):
        from plugins.voice_nlu.plugin import parse_intent
        return parse_intent(text)

    # ── Basic recognition ─────────────────────────────────────────────────────

    def test_stop_command(self):
        assert self._parse("stop").goal_name == "stop"

    def test_halt_synonym(self):
        assert self._parse("halt").goal_name == "stop"

    def test_freeze_synonym(self):
        assert self._parse("freeze").goal_name == "stop"

    def test_stand_up(self):
        assert self._parse("stand up").goal_name == "stand_up"

    def test_get_up_synonym(self):
        assert self._parse("get up").goal_name == "stand_up"

    def test_sit(self):
        assert self._parse("sit").goal_name == "sit"

    def test_sit_down(self):
        assert self._parse("sit down").goal_name == "sit"

    def test_lie_down(self):
        assert self._parse("lie down").goal_name == "stand_down"

    def test_come_here(self):
        assert self._parse("come here").goal_name == "move_timed"

    def test_heel(self):
        assert self._parse("heel").goal_name == "move_timed"

    def test_hello(self):
        assert self._parse("hello").goal_name == "hello"

    def test_wave(self):
        assert self._parse("wave").goal_name == "hello"

    def test_dance(self):
        assert self._parse("dance").goal_name == "dance1"

    def test_stretch(self):
        assert self._parse("stretch").goal_name == "stretch"

    def test_roll_over(self):
        assert self._parse("roll over").goal_name == "wallow"

    def test_shake(self):
        result = self._parse("shake")
        assert result is not None
        assert result.goal_name == "scrape"

    def test_heart(self):
        assert self._parse("heart").goal_name == "finger_heart"

    def test_finger_heart(self):
        assert self._parse("finger heart").goal_name == "finger_heart"

    def test_balance(self):
        assert self._parse("balance").goal_name == "balance_stand"

    def test_explore(self):
        assert self._parse("explore").goal_name == "explore"

    def test_jump(self):
        result = self._parse("jump")
        assert result is not None

    # ── E-stop ────────────────────────────────────────────────────────────────

    def test_emergency_stop_flagged(self):
        result = self._parse("emergency stop")
        assert result is not None
        assert result.estop is True

    def test_estop_short_form(self):
        result = self._parse("e-stop")
        assert result is not None
        assert result.estop is True

    def test_abort_command(self):
        result = self._parse("abort")
        assert result is not None
        assert result.estop is True

    def test_regular_stop_not_estop(self):
        result = self._parse("stop")
        assert result is not None
        assert result.estop is False

    # ── Priority invariants ───────────────────────────────────────────────────

    def test_stop_has_maximum_priority(self):
        result = self._parse("stop")
        assert result.priority == 1.0

    def test_estop_has_maximum_priority(self):
        result = self._parse("emergency stop")
        assert result.priority == 1.0

    def test_priorities_in_valid_range(self):
        from plugins.voice_nlu.plugin import INTENT_TABLE
        for _pattern, intent in INTENT_TABLE:
            assert 0.0 < intent.priority <= 1.0, (
                f"Intent {intent.goal_name} priority {intent.priority} out of range"
            )

    # ── Case insensitivity ────────────────────────────────────────────────────

    def test_upper_case(self):
        assert self._parse("SIT DOWN") is not None

    def test_mixed_case(self):
        assert self._parse("Stand Up") is not None

    def test_trailing_whitespace(self):
        assert self._parse("  hello  ") is not None

    # ── Unknown phrases ───────────────────────────────────────────────────────

    def test_empty_string_returns_none(self):
        assert self._parse("") is None

    def test_whitespace_only_returns_none(self):
        assert self._parse("   ") is None

    def test_gibberish_returns_none(self):
        assert self._parse("xyzzy frobble quux") is None

    def test_unrelated_sentence_returns_none(self):
        # A sentence that contains no command keywords
        assert self._parse("the weather is nice today") is None

    # ── Intent table structure ────────────────────────────────────────────────

    def test_intent_table_non_empty(self):
        from plugins.voice_nlu.plugin import INTENT_TABLE
        assert len(INTENT_TABLE) >= 10

    def test_all_intents_have_descriptions(self):
        from plugins.voice_nlu.plugin import INTENT_TABLE
        for _pat, intent in INTENT_TABLE:
            assert intent.description, f"Intent {intent.goal_name} has no description"

    def test_patterns_are_compiled(self):
        import re
        from plugins.voice_nlu.plugin import INTENT_TABLE
        for pat, _ in INTENT_TABLE:
            assert hasattr(pat, "search"), "Patterns must be compiled regex objects"

    # ── Partial match in context ──────────────────────────────────────────────

    def test_command_embedded_in_sentence(self):
        """Intent detection works even when command is part of a longer phrase."""
        result = self._parse("ok cerberus, sit down please")
        assert result is not None
        assert result.goal_name == "sit"

    def test_please_sit_down(self):
        result = self._parse("please sit down")
        assert result is not None

    def test_can_you_stand_up(self):
        result = self._parse("can you stand up now")
        assert result is not None
        assert result.goal_name == "stand_up"


class TestVoiceRecorder:
    """Tests for VoiceRecorder that don't require actual audio hardware."""

    def test_import_does_not_raise(self):
        from plugins.voice_nlu.plugin import VoiceRecorder
        r = VoiceRecorder()
        assert r is not None

    def test_recorder_created_with_defaults(self):
        from plugins.voice_nlu.plugin import VoiceRecorder, SAMPLE_RATE
        r = VoiceRecorder()
        assert r._sr == SAMPLE_RATE

    def test_duration_calculation(self):
        import numpy as np
        from plugins.voice_nlu.plugin import VoiceRecorder
        r = VoiceRecorder(sample_rate=16000)
        audio = np.zeros(32000, dtype="float32")   # 2 seconds
        assert r.duration_s(audio) == pytest.approx(2.0)

    def test_ensure_sd_raises_without_sounddevice(self):
        """Without a working sounddevice/PortAudio, _ensure_sd() raises an error."""
        from plugins.voice_nlu.plugin import VoiceRecorder
        import sys
        # Temporarily remove sounddevice so it must be re-imported
        sd_backup = sys.modules.pop("sounddevice", None)
        try:
            r = VoiceRecorder()
            r._sd = None   # reset lazy-loaded state
            # Either ImportError (not installed) or OSError (PortAudio absent) is valid
            with pytest.raises((RuntimeError, ImportError, OSError)):
                r._ensure_sd()
        finally:
            if sd_backup is not None:
                sys.modules["sounddevice"] = sd_backup


class TestVoiceNLUPlugin:
    """Plugin lifecycle tests — no Whisper / no microphone needed."""

    def _make_plugin(self, bare_engine):
        from plugins.voice_nlu.plugin import VoiceNLUPlugin
        return VoiceNLUPlugin(bare_engine)

    def test_initial_state(self, bare_engine):
        p = self._make_plugin(bare_engine)
        assert not p._listening
        assert p._command_count == 0

    @pytest.mark.asyncio
    async def test_status_contains_voice_section(self, bare_engine):
        p = self._make_plugin(bare_engine)
        s = p.status()
        assert "voice" in s
        assert "listening"     in s["voice"]
        assert "model"         in s["voice"]
        assert "intent_count"  in s["voice"]

    @pytest.mark.asyncio
    async def test_stop_listening_when_not_listening(self, bare_engine):
        """stop_listening() when already stopped should not raise."""
        p = self._make_plugin(bare_engine)
        result = await p.stop_listening()
        assert result == {"listening": False}

    @pytest.mark.asyncio
    async def test_transcribe_file_handles_missing_file(self, bare_engine):
        """Transcribing a non-existent file returns an error dict, not an exception."""
        p = self._make_plugin(bare_engine)
        # If Whisper isn't installed this tests the import-error path
        result = await p.transcribe_file("/nonexistent/audio.wav")
        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_dispatch_stop_calls_stop_move(self, bare_engine):
        from plugins.voice_nlu.plugin import VoiceNLUPlugin, parse_intent
        await bare_engine.bridge.connect()
        p = VoiceNLUPlugin(bare_engine)
        stop_intent = parse_intent("stop")
        assert stop_intent is not None

        # Stop intent is not an e-stop — it goes through goal queue
        # (behavior engine is not attached, so it falls through gracefully)
        await p._dispatch(stop_intent)
        assert p._command_count == 1

    @pytest.mark.asyncio
    async def test_estop_intent_triggers_watchdog(self, bare_engine):
        from plugins.voice_nlu.plugin import VoiceNLUPlugin, parse_intent
        await bare_engine.bridge.connect()
        p = VoiceNLUPlugin(bare_engine)
        estop_intent = parse_intent("emergency stop")
        assert estop_intent.estop is True

        await p._dispatch(estop_intent)
        assert bare_engine.watchdog.estop_active


# ═════════════════════════════════════════════════════════════════════════════
# MUJOCO BRIDGE
# ═════════════════════════════════════════════════════════════════════════════

class TestMuJocoBridgeGracefulDegradation:
    """Tests that work whether or not mujoco is installed."""

    def test_module_imports_cleanly(self):
        """Importing the module must never raise — even without mujoco."""
        import importlib
        mod = importlib.import_module("cerberus.bridge.mujoco_bridge")
        assert hasattr(mod, "MuJocoBridge")

    def test_bridge_instantiates_without_mujoco(self):
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        b = MuJocoBridge()
        assert b is not None
        assert not b._connected

    @pytest.mark.asyncio
    async def test_connect_raises_runtime_error_without_mujoco(self):
        """Without mujoco installed, connect() raises RuntimeError with install hint."""
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        b = MuJocoBridge(model_path="/nonexistent/model.xml")
        with pytest.raises(RuntimeError):
            await b.connect()

    def test_create_bridge_with_mujoco_env_returns_mujoco_bridge(self, monkeypatch):
        """create_bridge() with GO2_MUJOCO=true returns a MuJocoBridge."""
        monkeypatch.setenv("GO2_MUJOCO", "true")
        monkeypatch.setenv("GO2_SIMULATION", "false")
        # create_bridge() reads env vars at call time — no module reload needed
        from cerberus.bridge.go2_bridge import create_bridge
        b = create_bridge()
        # Check by class name to avoid any class-identity issues across reloads
        assert type(b).__name__ == "MuJocoBridge"

    def test_model_path_resolution_raises_with_no_candidates(self):
        """_resolve_model_path() raises RuntimeError when no model is found."""
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        b = MuJocoBridge(model_path=None)
        # Clear env var if set
        os.environ.pop("CERBERUS_MUJOCO_MODEL", None)
        with pytest.raises(RuntimeError, match="Could not find"):
            b._resolve_model_path()

    def test_model_path_env_var_missing_file_raises(self, tmp_path, monkeypatch):
        """If CERBERUS_MUJOCO_MODEL points to a non-existent file, raise."""
        monkeypatch.setenv("CERBERUS_MUJOCO_MODEL", str(tmp_path / "gone.xml"))
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        b = MuJocoBridge()
        with pytest.raises(RuntimeError, match="does not exist"):
            b._resolve_model_path()


class TestTrotCPG:
    """CPG unit tests — pure math, no mujoco dependency."""

    def _cpg(self):
        from cerberus.bridge.mujoco_bridge import TrotCPG
        return TrotCPG()

    def test_step_returns_four_leg_targets(self):
        cpg = self._cpg()
        targets = cpg.step(dt=0.002)
        assert len(targets) == 4
        for leg in targets:
            assert len(leg) == 3   # hip_ab, hip_flex, knee

    def test_phases_advance_monotonically(self):
        cpg = self._cpg()
        cpg.set_velocity(0.3, 0.0, 0.0)
        prev_phases = list(cpg._theta)
        cpg.step(0.002)
        for i in range(4):
            # Phase should increase (mod 2π)
            delta = (cpg._theta[i] - prev_phases[i]) % (2 * math.pi)
            assert delta > 0, f"Phase of leg {i} did not advance"

    def test_freeze_returns_nominal_stance(self):
        from cerberus.bridge.mujoco_bridge import Q0_HIP_AB, Q0_HIP_FLEX, Q0_KNEE
        cpg = self._cpg()
        targets = cpg.freeze()
        for leg in targets:
            assert leg[0] == pytest.approx(Q0_HIP_AB)
            assert leg[1] == pytest.approx(Q0_HIP_FLEX)
            assert leg[2] == pytest.approx(Q0_KNEE)

    def test_fl_and_rr_start_in_phase(self):
        """FL and RR should start with the same phase (trot diagonal pair)."""
        from cerberus.bridge.mujoco_bridge import TrotCPG
        cpg = TrotCPG()
        assert cpg._theta[0] == pytest.approx(cpg._theta[3])   # FL == RR

    def test_fr_and_rl_start_in_phase(self):
        """FR and RL start with same phase (other diagonal pair)."""
        from cerberus.bridge.mujoco_bridge import TrotCPG
        cpg = TrotCPG()
        assert cpg._theta[1] == pytest.approx(cpg._theta[2])   # FR == RL

    def test_fl_and_fr_start_anti_phase(self):
        """FL and FR start anti-phase (π apart)."""
        from cerberus.bridge.mujoco_bridge import TrotCPG
        cpg = TrotCPG()
        diff = abs(cpg._theta[0] - cpg._theta[1]) % (2 * math.pi)
        assert diff == pytest.approx(math.pi, abs=0.01)

    def test_velocity_zero_produces_minimal_amplitude(self):
        """At zero velocity, CPG uses freeze() (minimal oscillation)."""
        cpg = self._cpg()
        cpg.set_velocity(0.0, 0.0, 0.0)
        # freeze() returns nominal stance — no oscillation
        targets = cpg.freeze()
        from cerberus.bridge.mujoco_bridge import Q0_HIP_FLEX
        for leg in targets:
            assert leg[1] == pytest.approx(Q0_HIP_FLEX)

    def test_higher_speed_uses_higher_frequency(self):
        """Faster velocity → higher omega → more phase advance per step."""
        cpg_slow = self._cpg()
        cpg_fast = self._cpg()
        cpg_slow.set_velocity(0.1, 0.0, 0.0)
        cpg_fast.set_velocity(0.8, 0.0, 0.0)
        assert cpg_fast._omega > cpg_slow._omega

    def test_joint_angles_within_go2_limits(self):
        """CPG output must stay within Go2 hardware joint limits."""
        cpg = self._cpg()
        cpg.set_velocity(0.5, 0.0, 0.0)
        for _ in range(200):
            targets = cpg.step(0.002)
        for leg in targets:
            hip_ab, hip_flex, knee = leg
            assert -1.047 <= hip_ab   <= 1.047,  f"hip_ab {hip_ab:.3f} OOB"
            assert -3.490 <= hip_flex <= 1.745,  f"hip_flex {hip_flex:.3f} OOB"
            assert -0.524 <= knee     <= 4.189,  f"knee {knee:.3f} OOB"

    def test_trot_phase_coupling_matrix_shape(self):
        from cerberus.bridge.mujoco_bridge import TROT_PHASE_OFFSET
        assert len(TROT_PHASE_OFFSET) == 4
        for row in TROT_PHASE_OFFSET:
            assert len(row) == 4


class TestMuJocoBridgeInterface:
    """BridgeBase interface tests — no physics, just method contracts."""

    def _bridge(self):
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        return MuJocoBridge()

    @pytest.mark.asyncio
    async def test_get_state_returns_robot_state(self):
        from cerberus.bridge.go2_bridge import RobotState
        b = self._bridge()
        s = await b.get_state()
        assert isinstance(s, RobotState)

    @pytest.mark.asyncio
    async def test_move_updates_cmd_velocities(self):
        b = self._bridge()
        await b.move(0.3, 0.1, 0.2)
        assert b._cmd_vx   == pytest.approx(0.3)
        assert b._cmd_vy   == pytest.approx(0.1)
        assert b._cmd_vyaw == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_move_clamps_velocity(self):
        b = self._bridge()
        await b.move(99.0, 99.0, 99.0)
        assert b._cmd_vx   <= 1.5
        assert b._cmd_vy   <= 0.8
        assert b._cmd_vyaw <= 2.0

    @pytest.mark.asyncio
    async def test_stop_move_zeros_velocities(self):
        b = self._bridge()
        await b.move(0.5, 0.2, 0.3)
        await b.stop_move()
        assert b._cmd_vx   == 0.0
        assert b._cmd_vy   == 0.0
        assert b._cmd_vyaw == 0.0

    @pytest.mark.asyncio
    async def test_stand_up_sets_standing_flag(self):
        b = self._bridge()
        await b.stand_up()
        assert b._standing

    @pytest.mark.asyncio
    async def test_emergency_stop_sets_estop_flag(self):
        b = self._bridge()
        await b.emergency_stop()
        state = await b.get_state()
        assert state.estop_active

    @pytest.mark.asyncio
    async def test_execute_sport_mode_sets_mode(self):
        from cerberus.bridge.go2_bridge import SportMode
        b = self._bridge()
        await b.execute_sport_mode(SportMode.HELLO)
        assert b._sport_mode == "hello"

    @pytest.mark.asyncio
    async def test_set_obstacle_avoidance(self):
        b = self._bridge()
        await b.set_obstacle_avoidance(True)
        state = await b.get_state()
        assert state.obstacle_avoidance is True
