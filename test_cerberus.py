"""
tests/test_cerberus.py  — CERBERUS v3.1
========================================
Comprehensive test suite: 75+ tests across 8 classes.

TestSafetyGate      (12 tests)
TestGo2Bridge       (14 tests)
TestBehaviorEngine  (8 tests)
TestPersonality     (10 tests)
TestNLU             (18 tests)
TestDataLogger      (6 tests)
TestAPIEndpoints    (17 tests)  — mock bridge
TestAvailableModes  (2 tests)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cerberus.behavior.engine import BehaviorDescriptor, BehaviorEngine, Priority
from cerberus.hardware.bridge import (
    AVAILABLE_MODES, ConnectionState, Go2Bridge, RobotState, _MockTransport,
)
from cerberus.nlu.interpreter import NLUAction, interpret, rule_interpret
from cerberus.personality.model import Mood, PersonalityModel, Traits
from cerberus.safety.gate import SafetyConfig, SafetyGate


# ══════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def safety():
    return SafetyGate(SafetyConfig(
        battery_warn_v=22.0, battery_critical_v=20.5,
        max_vx=1.5, max_vy=0.8, max_vyaw=2.0,
        tilt_warn_rad=0.35, tilt_block_rad=0.70,
        special_motion_cooldown=0.05,   # short for tests
    ))

@pytest.fixture
def healthy():
    return RobotState(battery_voltage=24.0, pitch=0.0, roll=0.0,
                      connection_state=ConnectionState.CONNECTED)

@pytest.fixture
def mock_transport():
    return _MockTransport()

@pytest.fixture
async def bridge(mock_transport):
    b = Go2Bridge(mock_transport)
    await b.connect()
    return b

@pytest.fixture
async def engine(bridge):
    e = BehaviorEngine(bridge, tick_rate_hz=100.0)
    await e.start()
    yield e
    await e.stop()

@pytest.fixture
def traits():
    return Traits(sociability=0.7, playfulness=0.5, energy=0.6, curiosity=0.4)

@pytest.fixture
def personality(traits, tmp_path):
    return PersonalityModel(traits=traits, persistence_path=str(tmp_path / "p.json"))


# ══════════════════════════════════════════════════════════════════════════
# SafetyGate
# ══════════════════════════════════════════════════════════════════════════

class TestSafetyGate:
    def test_allows_normal(self, safety, healthy):
        assert safety.allow_move(0.5, 0.0, 0.0, healthy) is True

    def test_blocks_critical_battery(self, safety, healthy):
        healthy.battery_voltage = 19.0
        assert safety.allow_move(0.5, 0.0, 0.0, healthy) is False
        assert safety.violation_count == 1

    def test_allows_zero_voltage(self, safety, healthy):
        healthy.battery_voltage = 0.0
        assert safety.allow_move(0.5, 0.0, 0.0, healthy) is True

    def test_blocks_tilt(self, safety, healthy):
        healthy.pitch = 0.8
        assert safety.allow_move(0.5, 0.0, 0.0, healthy) is False

    def test_blocks_vx_over_limit(self, safety, healthy):
        assert safety.allow_move(2.0, 0.0, 0.0, healthy) is False

    def test_blocks_vy_over_limit(self, safety, healthy):
        assert safety.allow_move(0.0, 1.0, 0.0, healthy) is False

    def test_blocks_vyaw_over_limit(self, safety, healthy):
        assert safety.allow_move(0.0, 0.0, 2.5, healthy) is False

    def test_mode_cooldown(self, safety, healthy):
        ok, _ = safety.allow_mode("front_flip", healthy)
        assert ok is True
        ok2, reason = safety.allow_mode("front_flip", healthy)
        assert ok2 is False
        assert "cooldown" in reason

    def test_mode_cooldown_expires(self, safety, healthy):
        safety.allow_mode("dance1", healthy)
        time.sleep(0.08)
        ok, _ = safety.allow_mode("dance1", healthy)
        assert ok is True

    def test_config_height_valid(self, safety):
        ok, _ = safety.check_config(body_height=0.4)
        assert ok is True

    def test_config_height_invalid(self, safety):
        ok, msg = safety.check_config(body_height=0.9)
        assert ok is False and "body_height" in msg

    def test_violation_counter(self, safety, healthy):
        healthy.battery_voltage = 19.0
        safety.allow_move(1.0, 0.0, 0.0, healthy)
        safety.allow_move(1.0, 0.0, 0.0, healthy)
        assert safety.violation_count == 2


# ══════════════════════════════════════════════════════════════════════════
# Go2Bridge
# ══════════════════════════════════════════════════════════════════════════

class TestGo2Bridge:
    @pytest.mark.asyncio
    async def test_connect(self, bridge): assert bridge.connected

    @pytest.mark.asyncio
    async def test_get_state(self, bridge):
        s = await bridge.get_state()
        assert isinstance(s, RobotState)
        assert s.connection_state == ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_move_velocity(self, bridge):
        await bridge.move(0.3, 0.1, 0.5)
        s = await bridge.get_state()
        assert s.vx == pytest.approx(0.3)
        assert s.vy == pytest.approx(0.1)
        assert s.vyaw == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_clamps_vx(self, bridge):
        await bridge.move(5.0, 0.0, 0.0)
        s = await bridge.get_state()
        assert abs(s.vx) <= 1.5

    @pytest.mark.asyncio
    async def test_clamps_vy(self, bridge):
        await bridge.move(0.0, 2.0, 0.0)
        s = await bridge.get_state()
        assert abs(s.vy) <= 0.8

    @pytest.mark.asyncio
    async def test_stop(self, bridge):
        await bridge.move(0.5, 0.0, 0.0)
        await bridge.stop()
        s = await bridge.get_state()
        assert s.vx == 0.0

    @pytest.mark.asyncio
    async def test_set_mode_valid(self, bridge):
        await bridge.set_mode("hello")
        s = await bridge.get_state()
        assert s.current_mode == "hello"

    @pytest.mark.asyncio
    async def test_set_mode_invalid(self, bridge):
        with pytest.raises(ValueError, match="Unknown mode"):
            await bridge.set_mode("do_the_worm")

    @pytest.mark.asyncio
    async def test_all_modes(self, bridge):
        for m in AVAILABLE_MODES:
            await bridge.set_mode(m)

    @pytest.mark.asyncio
    async def test_body_height_clamp(self, bridge):
        await bridge.set_body_height(1.0)
        s = await bridge.get_state()
        assert s.body_height <= 0.5

    @pytest.mark.asyncio
    async def test_emergency_stop(self, bridge):
        await bridge.move(1.0, 0.0, 0.0)
        await bridge.emergency_stop()
        s = await bridge.get_state()
        assert s.vx == 0.0
        assert s.current_mode == "damp"

    @pytest.mark.asyncio
    async def test_obstacle_avoidance(self, bridge):
        await bridge.set_obstacle_avoidance(True)
        s = await bridge.get_state()
        assert s.obstacle_avoidance is True

    @pytest.mark.asyncio
    async def test_euler_clamp(self, bridge):
        await bridge.set_euler(2.0, 0.0, 3.0)
        s = await bridge.get_state()
        assert abs(s.roll) <= 0.75
        assert abs(s.yaw)  <= 1.5

    @pytest.mark.asyncio
    async def test_state_listener(self, bridge):
        calls = []
        bridge.add_state_listener(calls.append)
        await bridge.get_state()
        assert len(calls) == 1


# ══════════════════════════════════════════════════════════════════════════
# BehaviorEngine
# ══════════════════════════════════════════════════════════════════════════

class TestBehaviorEngine:
    @pytest.mark.asyncio
    async def test_defaults_registered(self, engine):
        assert "idle" in engine.available_behaviors
        assert "greet" in engine.available_behaviors
        assert "emergency_sit" in engine.available_behaviors

    @pytest.mark.asyncio
    async def test_enqueue_executes(self, engine):
        await engine.enqueue("idle")
        await asyncio.sleep(0.15)
        assert any(h["behavior"] == "idle" for h in engine.history)

    @pytest.mark.asyncio
    async def test_unknown_raises(self, engine):
        with pytest.raises(ValueError):
            await engine.enqueue("nonexistent_xyz")

    @pytest.mark.asyncio
    async def test_custom_behavior(self, engine):
        ran = []
        async def fn(ctx): ran.append(True)
        engine.register(BehaviorDescriptor("custom_test", fn, cooldown_s=0.0))
        await engine.enqueue("custom_test")
        await asyncio.sleep(0.15)
        assert ran

    @pytest.mark.asyncio
    async def test_cooldown_blocks(self, engine):
        engine.register(BehaviorDescriptor("cd_test", lambda ctx: asyncio.sleep(0), cooldown_s=60.0))
        await engine.enqueue("cd_test")
        await asyncio.sleep(0.1)
        pre = len(engine.history)
        await engine.enqueue("cd_test")
        await asyncio.sleep(0.1)
        assert len(engine.history) == pre

    @pytest.mark.asyncio
    async def test_history_records(self, engine):
        await engine.enqueue("sit")
        await asyncio.sleep(0.9)
        assert any(h["behavior"] == "sit" for h in engine.history)

    @pytest.mark.asyncio
    async def test_history_capped_50(self, engine):
        for i in range(55):
            engine.register(BehaviorDescriptor(f"b{i}", lambda ctx: asyncio.sleep(0), cooldown_s=0.0))
        for i in range(55):
            await engine.enqueue(f"b{i}")
        await asyncio.sleep(1.5)
        assert len(engine.history) <= 50

    @pytest.mark.asyncio
    async def test_priority_order(self, engine):
        results = []
        async def fn_a(ctx): results.append("A")
        async def fn_b(ctx): results.append("B")
        engine.register(BehaviorDescriptor("p_low",  fn_a, Priority.LOW,  cooldown_s=0.0))
        engine.register(BehaviorDescriptor("p_high", fn_b, Priority.HIGH, cooldown_s=0.0))
        await engine.enqueue("p_low")
        await engine.enqueue("p_high")
        await asyncio.sleep(0.3)
        if results:
            assert results[0] == "B"   # HIGH executes first


# ══════════════════════════════════════════════════════════════════════════
# PersonalityModel
# ══════════════════════════════════════════════════════════════════════════

class TestPersonality:
    def test_trait_clamp(self):
        t = Traits(sociability=2.0, playfulness=-0.5)
        assert t.sociability == 1.0
        assert t.playfulness == 0.0

    def test_mood_in_range(self, personality):
        assert -1.0 <= personality.mood.valence <= 1.0
        assert  0.0 <= personality.mood.arousal  <= 1.0

    def test_interaction_raises_valence(self, personality):
        before = personality.mood.valence
        personality.on_interaction()
        assert personality.mood.valence > before

    def test_battery_low_drops_valence(self, personality):
        before = personality.mood.valence
        personality.on_battery_low()
        assert personality.mood.valence < before

    def test_decay_toward_baseline(self, personality):
        personality.mood.valence = 0.9
        personality.tick()
        assert personality.mood.valence < 0.9

    def test_mood_label_excited(self, personality):
        personality.mood.valence = 0.8
        personality.mood.arousal  = 0.8
        assert personality.mood_label == "excited"

    def test_mood_label_content(self, personality):
        personality.mood.valence = 0.5
        personality.mood.arousal  = 0.2
        assert personality.mood_label == "content"

    def test_mood_label_distressed(self, personality):
        personality.mood.valence = -0.6
        personality.mood.arousal  = 0.3
        assert personality.mood_label == "distressed"

    def test_save_reload(self, tmp_path, traits):
        path = str(tmp_path / "pers.json")
        p1 = PersonalityModel(traits=traits, persistence_path=path)
        p1.on_interaction()
        v = p1.mood.valence
        p1.save()
        p2 = PersonalityModel(traits=traits, persistence_path=path)
        assert abs(p2.mood.valence - v) < 0.01

    def test_to_dict(self, personality):
        d = personality.to_dict()
        assert "traits" in d and "mood" in d and "mood_label" in d


# ══════════════════════════════════════════════════════════════════════════
# NLU Interpreter
# ══════════════════════════════════════════════════════════════════════════

class TestNLU:
    # Rule-based tests
    def test_stop(self):
        a = rule_interpret("stop")
        assert a and a[0].action_type == "stop"

    def test_emergency(self):
        a = rule_interpret("emergency stop!")
        assert a and a[0].action_type == "emergency_stop"

    def test_forward(self):
        a = rule_interpret("go forward")
        assert a and a[0].action_type == "move"
        assert a[0].params["vx"] > 0

    def test_backward(self):
        a = rule_interpret("move backward")
        assert a and any(act.params.get("vx", 0) < 0 for act in a)

    def test_left_strafe(self):
        a = rule_interpret("go left")
        assert a and any(act.params.get("vy", 0) > 0 for act in a)

    def test_right_strafe(self):
        a = rule_interpret("go right")
        assert a and any(act.params.get("vy", 0) < 0 for act in a)

    def test_sit(self):
        a = rule_interpret("sit down")
        assert a and a[0].action_type == "behavior"
        assert a[0].params["behavior"] == "sit"

    def test_stand(self):
        a = rule_interpret("stand up")
        assert a and a[0].params["behavior"] == "stand"

    def test_greet(self):
        a = rule_interpret("say hello")
        assert a and a[0].params["behavior"] == "greet"

    def test_dance(self):
        a = rule_interpret("dance!")
        assert a and a[0].params["behavior"] == "dance"

    def test_stretch(self):
        a = rule_interpret("stretch")
        assert a and a[0].params["behavior"] == "stretch"

    def test_tired_triggers_stretch(self):
        a = rule_interpret("the robot is tired")
        assert a and a[0].params.get("behavior") == "stretch"

    def test_patrol(self):
        a = rule_interpret("patrol the area")
        assert a and a[0].params["behavior"] == "patrol"

    def test_explicit_speed(self):
        a = rule_interpret("move forward at 0.8 m/s")
        assert a and abs(a[0].params["vx"] - 0.8) < 0.01

    def test_slow_modifier(self):
        a = rule_interpret("walk forward slowly")
        assert a and a[0].params["vx"] < 0.5

    def test_height_config(self):
        a = rule_interpret("set height 0.45")
        assert a and a[0].action_type == "config"
        assert abs(a[0].params["height"] - 0.45) < 0.01

    def test_no_match_returns_empty(self):
        a = rule_interpret("the sky is blue today")
        assert a == []

    @pytest.mark.asyncio
    async def test_interpret_fallback_to_stop(self):
        # No match + no LLM → default safe stop
        a = await interpret("the quick brown fox", llm_fallback=False)
        assert a and a[0].action_type == "stop"


# ══════════════════════════════════════════════════════════════════════════
# DataLogger
# ══════════════════════════════════════════════════════════════════════════

class TestDataLogger:
    def test_creates_log_file(self, tmp_path):
        from cerberus.learning.data_logger import DataLogger
        dl = DataLogger(logs_dir=str(tmp_path), max_mb=1.0, compress=False)
        dl.log_event("test_event", {"key": "value"})
        dl.close()
        files = list(tmp_path.glob("*.ndjson"))
        assert len(files) == 1

    def test_action_logged(self, tmp_path):
        from cerberus.learning.data_logger import DataLogger
        dl = DataLogger(logs_dir=str(tmp_path), compress=False)
        dl.log_action("move", {"vx": 0.5, "vy": 0.0})
        dl.close()
        f = list(tmp_path.glob("*.ndjson"))[0]
        lines = f.read_text().strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["type"] == "action"
        assert rec["data"]["action"] == "move"

    def test_list_sessions(self, tmp_path):
        from cerberus.learning.data_logger import DataLogger
        dl = DataLogger(logs_dir=str(tmp_path), compress=False)
        dl.log_event("x")
        dl.close()
        sessions = dl.list_sessions()
        assert len(sessions) == 1

    def test_state_listener_attached(self, tmp_path):
        from cerberus.learning.data_logger import DataLogger
        dl = DataLogger(logs_dir=str(tmp_path), compress=False)
        state = RobotState(battery_voltage=24.0)
        dl._on_state(state)
        dl.close()
        f = list(tmp_path.glob("*.ndjson"))[0]
        lines = f.read_text().strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["type"] == "state"

    def test_record_roundtrip(self):
        from cerberus.learning.data_logger import LogRecord
        r = LogRecord(ts=1.5, wall_time="2026-01-01T00:00:00", record_type="event", data={"k":"v"})
        j = r.to_json()
        r2 = LogRecord.from_json(j)
        assert r2.ts == 1.5
        assert r2.data == {"k": "v"}

    def test_compressed_log(self, tmp_path):
        from cerberus.learning.data_logger import DataLogger
        dl = DataLogger(logs_dir=str(tmp_path), compress=True)
        dl.log_event("compressed_test")
        dl.close()
        files = list(tmp_path.glob("*.ndjson.gz"))
        assert len(files) == 1


# ══════════════════════════════════════════════════════════════════════════
# API Endpoints
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_bridge():
    b = MagicMock(spec=Go2Bridge)
    b.connected = True
    b.get_state  = AsyncMock(return_value=RobotState(
        battery_voltage=24.0, connection_state=ConnectionState.CONNECTED))
    b.move                 = AsyncMock()
    b.stop                 = AsyncMock()
    b.emergency_stop       = AsyncMock()
    b.stand_up             = AsyncMock()
    b.stand_down           = AsyncMock()
    b.set_mode             = AsyncMock()
    b.set_body_height      = AsyncMock()
    b.set_euler            = AsyncMock()
    b.set_speed_level      = AsyncMock()
    b.set_foot_raise_height = AsyncMock()
    b.set_obstacle_avoidance = AsyncMock()
    b.set_vui              = AsyncMock()
    return b

@pytest.fixture
def api_client(mock_bridge):
    from fastapi.testclient import TestClient
    import backend.api.server as srv
    srv._bridge    = mock_bridge
    srv._behavior  = None
    srv._personality = None
    srv._plugins   = None
    srv._data_logger = None
    from fastapi.testclient import TestClient
    return TestClient(srv.app, raise_server_exceptions=False), mock_bridge

class TestAPIEndpoints:
    def test_health(self, api_client):
        c, _ = api_client
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_state(self, api_client):
        c, _ = api_client
        r = c.get("/api/v1/state")
        assert r.status_code == 200
        assert "battery" in r.json()

    def test_move_valid(self, api_client):
        c, b = api_client
        r = c.post("/api/v1/move", json={"vx":0.5,"vy":0.0,"vyaw":0.0})
        assert r.status_code == 200
        b.move.assert_called_once()

    def test_move_out_of_range(self, api_client):
        c, _ = api_client
        assert c.post("/api/v1/move", json={"vx":5.0,"vy":0.0,"vyaw":0.0}).status_code == 422

    def test_stop(self, api_client):
        c, b = api_client
        assert c.post("/api/v1/stop").status_code == 200
        b.stop.assert_called_once()

    def test_emergency_stop(self, api_client):
        c, b = api_client
        assert c.post("/api/v1/emergency_stop").status_code == 200
        b.emergency_stop.assert_called_once()

    def test_mode_valid(self, api_client):
        c, b = api_client
        assert c.post("/api/v1/mode", json={"mode":"hello"}).status_code == 200

    def test_mode_invalid(self, api_client):
        c, _ = api_client
        assert c.post("/api/v1/mode", json={"mode":"backflip_xyz"}).status_code == 422

    def test_height_valid(self, api_client):
        c, b = api_client
        r = c.post("/api/v1/config/height", json={"height":0.4})
        assert r.status_code == 200
        b.set_body_height.assert_called_once_with(0.4)

    def test_height_invalid(self, api_client):
        c, _ = api_client
        assert c.post("/api/v1/config/height", json={"height":1.0}).status_code == 422

    def test_euler(self, api_client):
        c, b = api_client
        assert c.post("/api/v1/config/euler", json={"roll":0.1,"pitch":0.2,"yaw":0.3}).status_code == 200

    def test_obstacle(self, api_client):
        c, b = api_client
        c.post("/api/v1/config/obstacle", json={"enabled":True})
        b.set_obstacle_avoidance.assert_called_once_with(True)

    def test_vui(self, api_client):
        c, b = api_client
        c.post("/api/v1/vui", json={"volume":70,"brightness":80})
        b.set_vui.assert_called_once_with(70, 80)

    def test_stand_up(self, api_client):
        c, b = api_client
        c.post("/api/v1/stand", json={"action":"up"})
        b.stand_up.assert_called_once()

    def test_stand_down(self, api_client):
        c, b = api_client
        c.post("/api/v1/stand", json={"action":"down"})
        b.stand_down.assert_called_once()

    def test_nlu_stop(self, api_client):
        c, _ = api_client
        r = c.post("/api/v1/nlu/command", json={"text":"stop","execute":False})
        assert r.status_code == 200
        data = r.json()
        assert data["actions"][0]["action_type"] == "stop"

    def test_nlu_forward(self, api_client):
        c, _ = api_client
        r = c.post("/api/v1/nlu/command", json={"text":"walk forward","execute":False})
        assert r.status_code == 200
        assert r.json()["actions"][0]["action_type"] == "move"


# ══════════════════════════════════════════════════════════════════════════
# Available Modes
# ══════════════════════════════════════════════════════════════════════════

class TestAvailableModes:
    def test_all_modes_present(self):
        expected = {
            "damp","balance_stand","stop_move","stand_up","stand_down",
            "sit","rise_sit","hello","stretch","wallow","scrape",
            "front_flip","front_jump","front_pounce","dance1","dance2","finger_heart",
        }
        assert expected == AVAILABLE_MODES

    def test_mode_count(self):
        assert len(AVAILABLE_MODES) == 17


# ══════════════════════════════════════════════════════════════════════════
# Simulator
# ══════════════════════════════════════════════════════════════════════════

class TestSimulator:
    @pytest.mark.asyncio
    async def test_simulator_emits_state(self):
        from cerberus.simulation.simulator import RobotSimulator, SimConfig
        states = []
        sim = RobotSimulator(SimConfig(update_hz=50.0, noise_scale=0.0))
        sim.on_state(states.append)
        await sim.start()
        await asyncio.sleep(0.08)
        await sim.stop()
        assert len(states) >= 2

    @pytest.mark.asyncio
    async def test_simulator_position_updates(self):
        from cerberus.simulation.simulator import RobotSimulator, SimConfig
        states = []
        sim = RobotSimulator(SimConfig(update_hz=100.0, noise_scale=0.0))
        sim.on_state(states.append)
        await sim.start()
        sim.command_move(1.0, 0.0, 0.0)
        await asyncio.sleep(0.1)
        await sim.stop()
        # position_x should have increased
        last = states[-1]
        assert last.position_x > 0.0

    def test_edu_plus_safety_config(self):
        from cerberus.safety.gate import SafetyConfig
        cfg = SafetyConfig.for_edu_plus()
        assert cfg.battery_warn_v == 25.0
        assert cfg.battery_critical_v == 23.5

    def test_standard_safety_defaults(self):
        from cerberus.safety.gate import SafetyConfig
        cfg = SafetyConfig()
        assert cfg.battery_warn_v == 22.0
        assert cfg.battery_critical_v == 20.5


# ══════════════════════════════════════════════════════════════════════════
# Perception Pipeline (stub)
# ══════════════════════════════════════════════════════════════════════════

class TestPerceptionPipeline:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        from cerberus.perception.pipeline import PerceptionPipeline
        p = PerceptionPipeline({"enabled": False})
        await p.start()   # should not raise
        await p.stop()
        assert p.last_frame is not None

    def test_detection_dataclass(self):
        from cerberus.perception.pipeline import Detection, PerceptionFrame
        d = Detection("person", 0.95, (0.5, 0.5, 0.1, 0.2))
        f = PerceptionFrame(detections=[d], person_nearby=True)
        assert f.person_nearby
        assert f.detections[0].class_name == "person"


# ══════════════════════════════════════════════════════════════════════════
# NLU — Expanded patterns (v3.1 additions)
# ══════════════════════════════════════════════════════════════════════════

class TestNLUExpanded:
    """Tests for the expanded NLU pattern set added in v3.1."""

    def test_wag(self):
        a = rule_interpret("wag your tail")
        assert a and a[0].action_type == "behavior" and a[0].params["behavior"] == "wag"

    def test_finger_heart(self):
        a = rule_interpret("do a finger heart")
        assert a and a[0].action_type == "mode" and a[0].params["mode"] == "finger_heart"

    def test_scrape(self):
        a = rule_interpret("scrape the ground")
        assert a and a[0].action_type == "mode" and a[0].params["mode"] == "scrape"

    def test_front_flip(self):
        a = rule_interpret("front flip")
        assert a and a[0].params["mode"] == "front_flip"

    def test_jump(self):
        a = rule_interpret("jump!")
        assert a and a[0].params["mode"] == "front_jump"

    def test_spin_left(self):
        a = rule_interpret("spin left")
        assert a
        assert a[0].action_type == "move"
        assert a[0].params["vyaw"] > 0
        assert a[0].params["vy"] == 0.0   # pure rotation, no strafe

    def test_spin_right(self):
        a = rule_interpret("spin right")
        assert a
        assert a[0].action_type == "move"
        assert a[0].params["vyaw"] < 0
        assert a[0].params["vy"] == 0.0

    def test_obstacle_avoidance_on(self):
        a = rule_interpret("turn obstacle avoidance on")
        assert a and a[0].action_type == "config_obstacle" and a[0].params["enabled"] is True

    def test_obstacle_avoidance_off(self):
        a = rule_interpret("disable obstacle avoidance")
        assert a and a[0].action_type == "config_obstacle" and a[0].params["enabled"] is False

    def test_lights_on(self):
        a = rule_interpret("turn the lights on")
        assert a and a[0].action_type == "vui" and a[0].params["brightness"] == 100

    def test_lights_off(self):
        a = rule_interpret("dim the lights")
        assert a and a[0].action_type == "vui" and a[0].params["brightness"] == 0

    def test_volume_up(self):
        a = rule_interpret("volume up")
        assert a and a[0].action_type == "vui" and a[0].params["volume"] > 50

    def test_volume_down(self):
        a = rule_interpret("turn it down")
        assert a and a[0].action_type == "vui" and a[0].params["volume"] < 50

    def test_height_in_cm(self):
        a = rule_interpret("set height to 45cm")
        assert a and a[0].action_type == "config"
        assert abs(a[0].params["height"] - 0.45) < 0.01

    def test_height_in_m(self):
        a = rule_interpret("height 0.4m")
        assert a and a[0].action_type == "config"
        assert abs(a[0].params["height"] - 0.4) < 0.01

    def test_lie_down(self):
        a = rule_interpret("lie down")
        assert a and a[0].action_type == "mode" and a[0].params["mode"] == "stand_down"

    def test_follow_me(self):
        a = rule_interpret("follow me")
        assert a and a[0].action_type == "behavior"

    def test_spin_not_strafe(self):
        # "spin left" should be pure rotation, NOT strafe + rotation
        a = rule_interpret("spin left")
        assert len(a) == 1, f"Expected 1 action, got {len(a)}: {a}"
        assert a[0].params.get("vy", 0) == 0.0

    def test_confidence_emergency(self):
        a = rule_interpret("emergency stop")
        assert a and a[0].confidence >= 0.95

    @pytest.mark.asyncio
    async def test_interpret_new_patterns(self):
        from cerberus.nlu.interpreter import interpret
        # Should resolve via rules without LLM
        a = await interpret("wag your tail", llm_fallback=False)
        assert a and a[0].action_type == "behavior"

    @pytest.mark.asyncio
    async def test_interpret_spin(self):
        from cerberus.nlu.interpreter import interpret
        a = await interpret("spin left", llm_fallback=False)
        assert a and a[0].params["vyaw"] > 0


# ══════════════════════════════════════════════════════════════════════════
# NLU API endpoint — expanded
# ══════════════════════════════════════════════════════════════════════════

class TestNLUAPIExpanded:
    def test_nlu_wag(self, api_client):
        c, _ = api_client
        r = c.post("/api/v1/nlu/command", json={"text": "wag your tail", "execute": False})
        assert r.status_code == 200
        assert r.json()["actions"][0]["action_type"] == "behavior"

    def test_nlu_obstacle_on(self, api_client):
        c, _ = api_client
        r = c.post("/api/v1/nlu/command", json={"text": "turn obstacle avoidance on", "execute": False})
        assert r.status_code == 200
        assert r.json()["actions"][0]["action_type"] == "config_obstacle"

    def test_nlu_spin_right(self, api_client):
        c, _ = api_client
        r = c.post("/api/v1/nlu/command", json={"text": "spin right", "execute": False})
        assert r.status_code == 200
        acts = r.json()["actions"]
        assert acts[0]["action_type"] == "move"
        assert acts[0]["params"]["vyaw"] < 0

    def test_nlu_execute_true(self, api_client):
        c, b = api_client
        r = c.post("/api/v1/nlu/command", json={"text": "stop", "execute": True})
        assert r.status_code == 200
        b.stop.assert_called()


# ══════════════════════════════════════════════════════════════════════════
# CLI integration (unit level — no live server needed)
# ══════════════════════════════════════════════════════════════════════════

class TestCLI:
    def test_parser_builds(self):
        from cerberus.cli import build_parser
        p = build_parser()
        assert p is not None

    def test_serve_subcommand_exists(self):
        from cerberus.cli import build_parser
        p = build_parser()
        # Should not raise
        args = p.parse_args(["serve", "--dev"])
        assert args.dev is True

    def test_nlu_subcommand(self):
        from cerberus.cli import build_parser
        p = build_parser()
        args = p.parse_args(["nlu", "walk forward", "--no-execute"])
        assert args.text == "walk forward"
        assert args.execute is False

    def test_move_subcommand(self):
        from cerberus.cli import build_parser
        p = build_parser()
        args = p.parse_args(["move", "0.5", "0.0", "0.3"])
        assert args.vx == 0.5
        assert args.vy == 0.0
        assert args.vyaw == 0.3

    def test_mode_subcommand(self):
        from cerberus.cli import build_parser
        p = build_parser()
        args = p.parse_args(["mode", "hello"])
        assert args.mode == "hello"


# ══════════════════════════════════════════════════════════════════════════
# Public package API
# ══════════════════════════════════════════════════════════════════════════

class TestPackageAPI:
    def test_version(self):
        import cerberus
        assert cerberus.__version__ == "3.1.0"

    def test_all_exports(self):
        import cerberus
        for name in cerberus.__all__:
            assert hasattr(cerberus, name), f"Missing export: {name}"

    def test_go2bridge_importable(self):
        from cerberus import Go2Bridge, RobotState, AVAILABLE_MODES
        assert len(AVAILABLE_MODES) == 17

    def test_safety_importable(self):
        from cerberus import SafetyGate, SafetyConfig
        cfg = SafetyConfig()
        assert cfg.max_vx == 1.5

    def test_nlu_importable(self):
        from cerberus import interpret, rule_interpret, NLUAction
        a = rule_interpret("stop")
        assert a[0].action_type == "stop"

    def test_edu_plus_config(self):
        from cerberus import SafetyConfig
        cfg = SafetyConfig.for_edu_plus()
        assert cfg.battery_warn_v == 25.0
        assert cfg.battery_critical_v == 23.5
