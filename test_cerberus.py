"""
tests/test_cerberus.py
======================
Comprehensive CERBERUS test suite.

Covers:
  - SafetyGate: battery, tilt, velocity limits, mode cooldowns
  - Go2Bridge (mock transport): connect, move, set_mode, clamp
  - BehaviorEngine: registration, enqueue, execution, cooldowns
  - PersonalityModel: trait clamp, mood decay, event effects
  - FastAPI endpoints: state, move, stop, mode, behavior, vui, config
  - WebSocket telemetry: connect + receive state message
"""

from __future__ import annotations

import asyncio
import math
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from httpx._transports.asgi import ASGITransport

# ─── Module imports ───────────────────────────────────────────────────────────
from cerberus.hardware.go2_bridge import (
    AVAILABLE_MODES,
    ConnectionState,
    Go2Bridge,
    RobotState,
    TransportMode,
    _MockTransport,
)
from cerberus.safety.gate import SafetyConfig, SafetyGate
from cerberus.behavior.engine import BehaviorDescriptor, BehaviorEngine, Priority
from cerberus.personality.model import Mood, PersonalityModel, Traits


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def safety_gate():
    return SafetyGate(SafetyConfig(
        battery_warn_v=22.0,
        battery_critical_v=20.5,
        max_vx=1.5, max_vy=0.8, max_vyaw=2.0,
        tilt_warn_rad=0.35,
        tilt_block_rad=0.70,
        special_motion_cooldown=0.1,  # short for tests
    ))


@pytest.fixture
def healthy_state():
    return RobotState(
        battery_voltage=24.0,
        pitch=0.0, roll=0.0,
        connection_state=ConnectionState.CONNECTED,
    )


@pytest.fixture
def mock_transport():
    return _MockTransport()


@pytest.fixture
async def bridge(mock_transport):
    """Async fixture so bridge and async tests share the same event loop."""
    b = Go2Bridge(mock_transport)
    await b.connect()
    return b


@pytest.fixture
def bridge_sync(mock_transport):
    """Synchronous bridge for sync-only test classes (TestGo2Bridge uses @pytest.mark.asyncio)."""
    import asyncio
    loop = asyncio.new_event_loop()
    b = Go2Bridge(mock_transport)
    loop.run_until_complete(b.connect())
    loop.close()
    return b


@pytest.fixture
def traits():
    return Traits(sociability=0.7, playfulness=0.5, energy=0.6, curiosity=0.4)


@pytest.fixture
def personality(traits, tmp_path):
    p = tmp_path / "personality.json"
    return PersonalityModel(traits=traits, persistence_path=str(p))


# ─────────────────────────────────────────────────────────────────────────────
# SafetyGate
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyGate:

    def test_allows_normal_move(self, safety_gate, healthy_state):
        assert safety_gate.allow_move(0.5, 0.0, 0.0, healthy_state) is True

    def test_blocks_critical_battery(self, safety_gate, healthy_state):
        healthy_state.battery_voltage = 19.0
        assert safety_gate.allow_move(0.5, 0.0, 0.0, healthy_state) is False
        assert safety_gate.violation_count == 1

    def test_allows_zero_battery_voltage(self, safety_gate, healthy_state):
        # voltage=0 means unknown — should not block
        healthy_state.battery_voltage = 0.0
        assert safety_gate.allow_move(0.5, 0.0, 0.0, healthy_state) is True

    def test_blocks_excessive_tilt(self, safety_gate, healthy_state):
        healthy_state.pitch = 0.8   # > 0.70 rad
        assert safety_gate.allow_move(0.5, 0.0, 0.0, healthy_state) is False

    def test_blocks_vx_over_hard_limit(self, safety_gate, healthy_state):
        assert safety_gate.allow_move(2.0, 0.0, 0.0, healthy_state) is False

    def test_blocks_vy_over_hard_limit(self, safety_gate, healthy_state):
        assert safety_gate.allow_move(0.0, 1.0, 0.0, healthy_state) is False

    def test_blocks_vyaw_over_hard_limit(self, safety_gate, healthy_state):
        assert safety_gate.allow_move(0.0, 0.0, 2.5, healthy_state) is False

    def test_mode_cooldown(self, safety_gate, healthy_state):
        ok, _ = safety_gate.allow_mode("front_flip", healthy_state)
        assert ok is True
        # Immediate second call — should be on cooldown
        ok2, reason = safety_gate.allow_mode("front_flip", healthy_state)
        assert ok2 is False
        assert "cooldown" in reason

    def test_mode_cooldown_expires(self, safety_gate, healthy_state):
        safety_gate.allow_mode("dance1", healthy_state)
        time.sleep(0.15)   # cooldown=0.1 in fixture
        ok, _ = safety_gate.allow_mode("dance1", healthy_state)
        assert ok is True

    def test_config_check_body_height(self, safety_gate):
        ok, _ = safety_gate.check_config(body_height=0.4)
        assert ok is True
        ok2, msg = safety_gate.check_config(body_height=0.9)
        assert ok2 is False
        assert "body_height" in msg

    def test_config_check_euler_out_of_range(self, safety_gate):
        ok, msg = safety_gate.check_config(roll=1.0)
        assert ok is False

    def test_violation_counter_increments(self, safety_gate, healthy_state):
        healthy_state.battery_voltage = 19.0
        safety_gate.allow_move(1.0, 0.0, 0.0, healthy_state)
        safety_gate.allow_move(1.0, 0.0, 0.0, healthy_state)
        assert safety_gate.violation_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# Go2Bridge (mock transport)
# ─────────────────────────────────────────────────────────────────────────────

class TestGo2Bridge:

    @pytest.mark.asyncio
    async def test_connect(self, bridge):
        assert bridge.connected is True

    @pytest.mark.asyncio
    async def test_get_state_returns_robot_state(self, bridge):
        state = await bridge.get_state()
        assert isinstance(state, RobotState)
        assert state.connection_state == ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_move_updates_transport_state(self, bridge, mock_transport):
        await bridge.move(0.3, 0.1, 0.5)
        state = await bridge.get_state()
        assert state.vx == pytest.approx(0.3)
        assert state.vy == pytest.approx(0.1)
        assert state.vyaw == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_move_clamps_vx(self, bridge):
        await bridge.move(5.0, 0.0, 0.0)   # exceeds max 1.5
        state = await bridge.get_state()
        assert abs(state.vx) <= 1.5

    @pytest.mark.asyncio
    async def test_move_clamps_vy(self, bridge):
        await bridge.move(0.0, 2.0, 0.0)
        state = await bridge.get_state()
        assert abs(state.vy) <= 0.8

    @pytest.mark.asyncio
    async def test_stop_zeroes_velocity(self, bridge):
        await bridge.move(0.5, 0.0, 0.0)
        await bridge.stop()
        state = await bridge.get_state()
        assert state.vx == 0.0

    @pytest.mark.asyncio
    async def test_set_mode_valid(self, bridge, mock_transport):
        await bridge.set_mode("hello")
        state = await bridge.get_state()
        assert state.current_mode == "hello"

    @pytest.mark.asyncio
    async def test_set_mode_invalid_raises(self, bridge):
        with pytest.raises(ValueError, match="Unknown mode"):
            await bridge.set_mode("invalid_mode_xyz")

    @pytest.mark.asyncio
    async def test_all_available_modes(self, bridge):
        for mode in AVAILABLE_MODES:
            await bridge.set_mode(mode)   # should not raise

    @pytest.mark.asyncio
    async def test_set_body_height_clamps(self, bridge):
        await bridge.set_body_height(1.0)   # above max 0.5
        state = await bridge.get_state()
        assert state.body_height <= 0.5

    @pytest.mark.asyncio
    async def test_emergency_stop(self, bridge):
        await bridge.move(1.0, 0.5, 0.5)
        await bridge.emergency_stop()
        state = await bridge.get_state()
        # After damp, velocity should be 0
        assert state.vx == 0.0
        assert state.current_mode == "damp"

    @pytest.mark.asyncio
    async def test_from_config_mock(self):
        b = Go2Bridge.from_config({"transport": "mock"})
        await b.connect()
        assert b.connected

    @pytest.mark.asyncio
    async def test_obstacle_avoidance_toggle(self, bridge):
        await bridge.set_obstacle_avoidance(True)
        state = await bridge.get_state()
        assert state.obstacle_avoidance is True
        await bridge.set_obstacle_avoidance(False)
        state = await bridge.get_state()
        assert state.obstacle_avoidance is False

    @pytest.mark.asyncio
    async def test_euler_clamp(self, bridge):
        await bridge.set_euler(2.0, 0.0, 3.0)   # both out of range
        state = await bridge.get_state()
        assert abs(state.roll) <= 0.75
        assert abs(state.yaw)  <= 1.5

    @pytest.mark.asyncio
    async def test_state_listener_called(self, bridge):
        calls = []
        bridge.add_state_listener(lambda s: calls.append(s))
        await bridge.get_state()
        assert len(calls) == 1
        assert isinstance(calls[0], RobotState)


# ─────────────────────────────────────────────────────────────────────────────
# BehaviorEngine
# ─────────────────────────────────────────────────────────────────────────────

class TestBehaviorEngine:

    @pytest.fixture
    async def engine(self, bridge):
        """Async fixture so engine and tests share the same event loop."""
        eng = BehaviorEngine(bridge, tick_rate_hz=100.0)  # fast for tests
        await eng.start()
        yield eng
        await eng.stop()

    @pytest.mark.asyncio
    async def test_default_behaviors_registered(self, engine):
        assert "idle" in engine.available_behaviors
        assert "greet" in engine.available_behaviors
        assert "emergency_sit" in engine.available_behaviors

    @pytest.mark.asyncio
    async def test_enqueue_and_execute(self, engine):
        await engine.enqueue("idle")
        await asyncio.sleep(0.2)
        assert any(h["behavior"] == "idle" for h in engine.history)

    @pytest.mark.asyncio
    async def test_enqueue_unknown_raises(self, engine):
        with pytest.raises(ValueError, match="Unknown behavior"):
            await engine.enqueue("nonexistent_behavior")

    @pytest.mark.asyncio
    async def test_custom_behavior_registered_and_run(self, engine):
        executed = []

        async def my_beh(ctx):
            executed.append(True)

        engine.register(BehaviorDescriptor(
            name="test_custom", fn=my_beh, priority=Priority.NORMAL,
            cooldown_s=0.0,
        ))
        await engine.enqueue("test_custom")
        await asyncio.sleep(0.2)
        assert executed

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_requeue(self, engine):
        engine.register(BehaviorDescriptor(
            name="slow_beh",
            fn=lambda ctx: asyncio.sleep(0),
            cooldown_s=60.0,
        ))
        await engine.enqueue("slow_beh")
        await asyncio.sleep(0.15)
        pre_len = len(engine.history)
        await engine.enqueue("slow_beh")   # blocked by cooldown
        await asyncio.sleep(0.15)
        assert len(engine.history) == pre_len

    @pytest.mark.asyncio
    async def test_history_tracks_executions(self, engine):
        await engine.enqueue("sit")
        await asyncio.sleep(0.8)   # sit behavior has a 0.5s internal sleep
        assert any(h["behavior"] == "sit" for h in engine.history)

    @pytest.mark.asyncio
    async def test_history_capped_at_50(self, engine):
        for i in range(55):
            engine.register(BehaviorDescriptor(
                name=f"beh_{i}",
                fn=lambda ctx: asyncio.sleep(0),
                cooldown_s=0.0,
            ))
        for i in range(55):
            await engine.enqueue(f"beh_{i}")
        await asyncio.sleep(1.2)
        assert len(engine.history) <= 50


# ─────────────────────────────────────────────────────────────────────────────
# PersonalityModel
# ─────────────────────────────────────────────────────────────────────────────

class TestPersonalityModel:

    def test_traits_clamped(self):
        t = Traits(sociability=2.0, playfulness=-0.5)
        assert t.sociability == 1.0
        assert t.playfulness == 0.0

    def test_mood_default(self, personality):
        assert -1.0 <= personality.mood.valence <= 1.0
        assert  0.0 <= personality.mood.arousal  <= 1.0

    def test_interaction_increases_valence(self, personality):
        before = personality.mood.valence
        personality.on_interaction()
        assert personality.mood.valence > before

    def test_battery_low_decreases_valence(self, personality):
        before = personality.mood.valence
        personality.on_battery_low()
        assert personality.mood.valence < before

    def test_mood_decays_toward_baseline(self, personality):
        personality.mood.valence = 0.9   # set high
        personality.tick()
        # should move toward ~0.2-0.3 baseline
        assert personality.mood.valence < 0.9

    def test_mood_label_excited(self, personality):
        personality.mood.valence = 0.8
        personality.mood.arousal  = 0.8
        assert personality.mood_label == "excited"

    def test_mood_label_content(self, personality):
        personality.mood.valence = 0.5
        personality.mood.arousal  = 0.2
        assert personality.mood_label == "content"

    def test_save_and_reload(self, tmp_path, traits):
        path = str(tmp_path / "p.json")
        p1 = PersonalityModel(traits=traits, persistence_path=path)
        p1.on_interaction()
        v = p1.mood.valence
        p1.save()

        p2 = PersonalityModel(traits=traits, persistence_path=path)
        assert abs(p2.mood.valence - v) < 0.01

    def test_to_dict_structure(self, personality):
        d = personality.to_dict()
        assert "traits" in d
        assert "mood" in d
        assert "mood_label" in d
        assert "valence" in d["mood"]


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI endpoints (using sync TestClient with mock bridge)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_app():
    """Return a test FastAPI app with mock bridge pre-injected."""
    from backend.api.server import app, _bridge
    import backend.api.server as srv

    mock_b = MagicMock(spec=Go2Bridge)
    mock_b.connected = True
    mock_b.get_state = AsyncMock(return_value=RobotState(
        battery_voltage=24.0,
        connection_state=ConnectionState.CONNECTED,
    ))
    mock_b.move = AsyncMock()
    mock_b.stop = AsyncMock()
    mock_b.emergency_stop = AsyncMock()
    mock_b.stand_up = AsyncMock()
    mock_b.stand_down = AsyncMock()
    mock_b.set_mode = AsyncMock()
    mock_b.set_body_height = AsyncMock()
    mock_b.set_euler = AsyncMock()
    mock_b.set_speed_level = AsyncMock()
    mock_b.set_foot_raise_height = AsyncMock()
    mock_b.set_obstacle_avoidance = AsyncMock()
    mock_b.set_vui = AsyncMock()

    srv._bridge = mock_b
    srv._behavior = None
    srv._personality = None
    srv._plugins = None
    return app, mock_b


class TestAPIEndpoints:

    def test_health(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_get_state(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/state")
        assert resp.status_code == 200
        body = resp.json()
        assert "battery" in body

    def test_move_valid(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/move", json={"vx": 0.5, "vy": 0.0, "vyaw": 0.0})
        assert resp.status_code == 200
        mock_b.move.assert_called_once()

    def test_move_out_of_range_rejected(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/move", json={"vx": 5.0, "vy": 0.0, "vyaw": 0.0})
        assert resp.status_code == 422  # pydantic validation

    def test_stop(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/stop")
        assert resp.status_code == 200
        mock_b.stop.assert_called_once()

    def test_emergency_stop(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/emergency_stop")
        assert resp.status_code == 200
        mock_b.emergency_stop.assert_called_once()

    def test_mode_valid(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/mode", json={"mode": "hello"})
        assert resp.status_code == 200

    def test_mode_invalid(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/mode", json={"mode": "do_a_backflip"})
        assert resp.status_code == 422

    def test_body_height_valid(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/height", json={"height": 0.4})
        assert resp.status_code == 200
        mock_b.set_body_height.assert_called_once_with(0.4)

    def test_body_height_out_of_range(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/height", json={"height": 1.0})
        assert resp.status_code == 422

    def test_euler_valid(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/euler",
                           json={"roll": 0.1, "pitch": 0.2, "yaw": 0.3})
        assert resp.status_code == 200

    def test_obstacle_avoidance(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/obstacle", json={"enabled": True})
        assert resp.status_code == 200
        mock_b.set_obstacle_avoidance.assert_called_once_with(True)

    def test_vui(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/vui", json={"volume": 70, "brightness": 80})
        assert resp.status_code == 200
        mock_b.set_vui.assert_called_once_with(70, 80)

    def test_stand_up(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/stand", json={"action": "up"})
        assert resp.status_code == 200
        mock_b.stand_up.assert_called_once()

    def test_stand_down(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/stand", json={"action": "down"})
        assert resp.status_code == 200
        mock_b.stand_down.assert_called_once()

    def test_speed_valid(self, test_app):
        app, mock_b = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/speed", json={"level": 1})
        assert resp.status_code == 200

    def test_speed_invalid(self, test_app):
        app, _ = test_app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/v1/config/speed", json={"level": 5})
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Available modes completeness check
# ─────────────────────────────────────────────────────────────────────────────

class TestAvailableModes:
    def test_expected_modes_present(self):
        expected = {
            "damp", "balance_stand", "stop_move", "stand_up", "stand_down",
            "sit", "rise_sit", "hello", "stretch", "wallow", "scrape",
            "front_flip", "front_jump", "front_pounce",
            "dance1", "dance2", "finger_heart",
        }
        assert expected == AVAILABLE_MODES

    def test_mode_count(self):
        assert len(AVAILABLE_MODES) == 17
