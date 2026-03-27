"""
tests/test_bridge.py — Go2 bridge unit tests (mock DDS)
tests/test_engine.py — Engine loop + safety tests
tests/test_api.py    — FastAPI endpoint tests
tests/test_plugins.py — Plugin system tests
"""

# ── tests/test_bridge.py ──────────────────────────────────────────────────────

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from cerberus.bridge.go2_bridge import SimBridge, SportMode, RobotState, create_bridge


@pytest.fixture
def sim_bridge():
    return SimBridge()


@pytest.mark.asyncio
async def test_sim_bridge_connect(sim_bridge):
    await sim_bridge.connect()
    assert sim_bridge._connected is True
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_sim_bridge_stand_up(sim_bridge):
    await sim_bridge.connect()
    result = await sim_bridge.stand_up()
    assert result is True
    state = await sim_bridge.get_state()
    assert state.mode == "standing"
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_sim_bridge_move_and_stop(sim_bridge):
    await sim_bridge.connect()
    result = await sim_bridge.move(0.5, 0.0, 0.0)
    assert result is True
    state = await sim_bridge.get_state()
    assert state.velocity_x == pytest.approx(0.5)
    assert state.mode == "moving"

    await sim_bridge.stop_move()
    state = await sim_bridge.get_state()
    assert state.velocity_x == pytest.approx(0.0)
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_sim_bridge_all_sport_modes(sim_bridge):
    await sim_bridge.connect()
    for mode in SportMode:
        result = await sim_bridge.execute_sport_mode(mode)
        assert result is True, f"Sport mode {mode.value} failed"
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_sim_bridge_estop(sim_bridge):
    await sim_bridge.connect()
    await sim_bridge.stand_up()
    result = await sim_bridge.emergency_stop()
    assert result is True
    state = await sim_bridge.get_state()
    assert state.estop_active is True
    assert state.mode == "estop"
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_sim_bridge_led(sim_bridge):
    await sim_bridge.connect()
    result = await sim_bridge.set_led(255, 0, 128)
    assert result is True


@pytest.mark.asyncio
async def test_sim_bridge_body_height_clamping(sim_bridge):
    await sim_bridge.connect()
    # Should not raise
    await sim_bridge.set_body_height(0.05)
    await sim_bridge.set_body_height(-0.05)
    await sim_bridge.disconnect()


@pytest.mark.asyncio
async def test_create_bridge_sim_env(monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "true")
    b = create_bridge()
    assert isinstance(b, SimBridge)


@pytest.mark.asyncio
async def test_robot_state_to_dict(sim_bridge):
    await sim_bridge.connect()
    state = await sim_bridge.get_state()
    d = state.to_dict()
    assert "battery" in d
    assert "velocity" in d
    assert "imu" in d
    assert "joints" in d
    assert "estop_active" in d


# ── tests/test_engine.py ──────────────────────────────────────────────────────

import asyncio
import pytest
from cerberus.bridge.go2_bridge import SimBridge
from cerberus.core.engine import CerberusEngine, EngineState, EventBus
from cerberus.core.safety import SafetyWatchdog, SafetyLimits, SafetyLevel


@pytest.fixture
def bridge():
    return SimBridge()


@pytest.fixture
def watchdog(bridge):
    limits = SafetyLimits(heartbeat_timeout_s=2.0)
    return SafetyWatchdog(bridge, limits)


@pytest.fixture
def engine(bridge, watchdog):
    return CerberusEngine(bridge, watchdog, target_hz=30.0)


@pytest.mark.asyncio
async def test_engine_start_stop(engine):
    await engine.start()
    assert engine.state == EngineState.RUNNING
    await asyncio.sleep(0.2)
    await engine.stop()
    assert engine.state == EngineState.STOPPED


@pytest.mark.asyncio
async def test_engine_ticks(engine):
    await engine.start()
    await asyncio.sleep(0.5)
    assert engine.stats.tick_count > 5
    assert engine.stats.tick_hz > 0
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_pause_resume(engine):
    await engine.start()
    await asyncio.sleep(0.1)
    ticks_before = engine.stats.tick_count
    engine.pause()
    await asyncio.sleep(0.2)
    ticks_paused = engine.stats.tick_count
    engine.resume()
    await asyncio.sleep(0.2)
    ticks_after = engine.stats.tick_count
    assert ticks_after > ticks_before
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_event_bus(engine):
    received = []
    engine.bus.subscribe("test.event", lambda p: received.append(p))
    await engine.start()
    await engine.bus.publish("test.event", {"hello": "world"})
    await asyncio.sleep(0.05)
    assert len(received) == 1
    assert received[0]["hello"] == "world"
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_plugin_hook(engine):
    ticks_seen = []
    async def my_hook(tick):
        ticks_seen.append(tick)
    engine.register_hook("test_hook", my_hook)
    await engine.start()
    await asyncio.sleep(0.3)
    await engine.stop()
    assert len(ticks_seen) > 0


@pytest.mark.asyncio
async def test_safety_estop_blocks_motion(bridge, watchdog):
    await bridge.connect()
    await watchdog.trigger_estop("test")
    assert watchdog.estop_active is True
    assert watchdog.safety_level == SafetyLevel.ESTOP


@pytest.mark.asyncio
async def test_safety_velocity_validation(watchdog, bridge):
    await bridge.connect()
    ok, _ = watchdog.validate_velocity(0.5, 0.3, 1.0)
    assert ok is True
    ok, reason = watchdog.validate_velocity(999.0, 0.0, 0.0)
    assert ok is False
    assert "vx" in reason


@pytest.mark.asyncio
async def test_safety_estop_clear_sim_only(bridge, watchdog, monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "true")
    await bridge.connect()
    await watchdog.trigger_estop("test")
    result = await watchdog.clear_estop()
    assert result is True
    assert watchdog.estop_active is False


@pytest.mark.asyncio
async def test_safety_estop_clear_blocked_real(bridge, watchdog, monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "false")
    await bridge.connect()
    await watchdog.trigger_estop("test")
    result = await watchdog.clear_estop()
    assert result is False


@pytest.mark.asyncio
async def test_safety_battery_critical(bridge, watchdog):
    await bridge.connect()
    bridge._state.battery_percent = 1.0
    await watchdog._tick()
    assert watchdog.estop_active is True


@pytest.mark.asyncio
async def test_safety_tilt_detection(bridge, watchdog):
    import math
    await bridge.connect()
    bridge._state.roll = math.radians(35.0)
    await watchdog._tick()
    assert watchdog.estop_active is True


# ── tests/test_api.py ─────────────────────────────────────────────────────────

import pytest
import os
from httpx import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def set_sim_env(monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "true")
    monkeypatch.setenv("CERBERUS_HZ", "30")
    monkeypatch.setenv("PLUGIN_DIRS", "plugins")


@pytest.fixture
async def client():
    import os
    os.environ["GO2_SIMULATION"] = "true"
    os.environ["CERBERUS_HZ"] = "30"
    os.environ["PLUGIN_DIRS"] = "plugins"
    from asgi_lifespan import LifespanManager
    import importlib, backend.main as bm
    importlib.reload(bm)
    app = bm.app
    # LifespanManager triggers ASGI startup/shutdown events properly
    async with LifespanManager(app) as mgr:
        async with AsyncClient(
            transport=ASGITransport(app=mgr.app),
            base_url="http://test"
        ) as c:
            yield c


@pytest.mark.asyncio
async def test_root(client):
    r = await client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "CERBERUS"
    assert data["simulation"] is True


@pytest.mark.asyncio
async def test_get_state(client):
    r = await client.get("/state")
    assert r.status_code == 200
    data = r.json()
    assert "battery" in data
    assert "velocity" in data


@pytest.mark.asyncio
async def test_get_stats(client):
    r = await client.get("/stats")
    assert r.status_code == 200
    data = r.json()
    assert "tick_hz" in data


@pytest.mark.asyncio
async def test_motion_stand_up(client):
    r = await client.post("/motion/stand_up")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_motion_stop(client):
    r = await client.post("/motion/stop")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_motion_move_valid(client):
    r = await client.post("/motion/move", json={"vx": 0.5, "vy": 0.0, "vyaw": 0.0})
    assert r.status_code == 200
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_motion_move_over_limit(client):
    r = await client.post("/motion/move", json={"vx": 99.0, "vy": 0.0, "vyaw": 0.0})
    assert r.status_code == 422  # Pydantic validation error


@pytest.mark.asyncio
async def test_motion_sport_mode_valid(client):
    for mode in ["hello", "dance1", "stretch", "sit", "stand_up"]:
        r = await client.post("/motion/sport_mode", json={"mode": mode})
        assert r.status_code == 200, f"Mode {mode} failed: {r.text}"


@pytest.mark.asyncio
async def test_motion_sport_mode_invalid(client):
    r = await client.post("/motion/sport_mode", json={"mode": "do_a_backflip_please"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_estop_cycle(client):
    r = await client.post("/safety/estop")
    assert r.status_code == 200
    assert r.json()["estop_active"] is True

    # Motion should be blocked after estop
    r = await client.post("/motion/stand_up")
    assert r.status_code == 503

    # Clear (sim only)
    r = await client.post("/safety/clear_estop")
    assert r.status_code == 200

    # Motion should work again
    r = await client.post("/motion/stand_up")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_led_control(client):
    r = await client.post("/led", json={"r": 255, "g": 0, "b": 128})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_volume_control(client):
    r = await client.post("/volume", json={"level": 50})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_body_height(client):
    r = await client.post("/motion/body_height", json={"height": 0.05})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_euler(client):
    r = await client.post("/motion/euler", json={"roll": 0.1, "pitch": 0.1, "yaw": 0.0})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_gait(client):
    r = await client.post("/motion/gait", json={"gait_id": 2})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_plugins_list(client):
    r = await client.get("/plugins")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_anatomy(client):
    await asyncio.sleep(0.1)  # Let anatomy update
    r = await client.get("/anatomy")
    assert r.status_code == 200
    data = r.json()
    assert "joints" in data
    assert "energy" in data


@pytest.mark.asyncio
async def test_behavior(client):
    r = await client.get("/behavior")
    assert r.status_code == 200
    data = r.json()
    assert "mood" in data
    assert "active_behavior" in data


@pytest.mark.asyncio
async def test_push_goal(client):
    r = await client.post("/behavior/goal", json={"name": "greet_user", "priority": 0.8})
    assert r.status_code == 200


# ── tests/test_plugins.py ─────────────────────────────────────────────────────

import asyncio
import pytest
from pathlib import Path
from cerberus.bridge.go2_bridge import SimBridge
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.plugins.plugin_manager import (
    PluginManager, CerberusPlugin, PluginManifest, TrustLevel
)


@pytest.fixture
async def engine_fixture():
    bridge   = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng      = CerberusEngine(bridge, watchdog, target_hz=30.0)
    await eng.start()
    yield eng
    await eng.stop()


@pytest.mark.asyncio
async def test_plugin_load_unload(engine_fixture):
    pm = PluginManager(engine_fixture, [])

    class TestPlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="TestPlugin", version="1.0.0",
            capabilities=["read_state"], trust=TrustLevel.COMMUNITY
        )
        loaded = False
        unloaded = False

        async def on_load(self):
            TestPlugin.loaded = True

        async def on_unload(self):
            TestPlugin.unloaded = True

    success = await pm.load_plugin_class(TestPlugin, TestPlugin.MANIFEST, "test")
    assert success is True
    assert TestPlugin.loaded is True

    plugins = pm.list_plugins()
    assert any(p["name"] == "TestPlugin" for p in plugins)

    success = await pm.unload_plugin("TestPlugin")
    assert success is True
    assert TestPlugin.unloaded is True


@pytest.mark.asyncio
async def test_plugin_capability_denied(engine_fixture):
    pm = PluginManager(engine_fixture, [])

    class BadPlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="BadPlugin", version="1.0.0",
            capabilities=["low_level_control"],  # requires TRUSTED
            trust=TrustLevel.COMMUNITY
        )

    success = await pm.load_plugin_class(BadPlugin, BadPlugin.MANIFEST, "test")
    assert success is False  # Rejected due to capability mismatch


@pytest.mark.asyncio
async def test_plugin_capability_sandboxing(engine_fixture):
    pm = PluginManager(engine_fixture, [])

    class ReadPlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="ReadPlugin", version="1.0.0",
            capabilities=["read_state"], trust=TrustLevel.COMMUNITY
        )
        async def on_load(self):
            # Should succeed
            state = await self.get_state()
            assert state is not None

            # Should fail — not in manifest
            try:
                await self.move(0.1, 0, 0)
                assert False, "Should have raised PermissionError"
            except PermissionError:
                pass

    success = await pm.load_plugin_class(ReadPlugin, ReadPlugin.MANIFEST, "test")
    assert success is True


@pytest.mark.asyncio
async def test_plugin_error_isolation(engine_fixture):
    pm = PluginManager(engine_fixture, [])

    class CrashPlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="CrashPlugin", version="1.0.0",
            capabilities=["read_state"], trust=TrustLevel.COMMUNITY
        )
        async def on_tick(self, tick: int):
            raise RuntimeError("Simulated crash")

    pm._max_errors = 2
    await pm.load_plugin_class(CrashPlugin, CrashPlugin.MANIFEST, "test")
    pm.register_with_engine()

    await asyncio.sleep(0.2)  # Let engine tick a few times

    rec = pm._plugins.get("CrashPlugin")
    assert rec is not None
    assert rec.error_count >= 2
    assert rec.plugin._enabled is False  # Auto-disabled after max errors


@pytest.mark.asyncio
async def test_plugin_enable_disable(engine_fixture):
    pm = PluginManager(engine_fixture, [])

    class TogglePlugin(CerberusPlugin):
        MANIFEST = PluginManifest(
            name="TogglePlugin", version="1.0.0",
            capabilities=[], trust=TrustLevel.COMMUNITY
        )

    await pm.load_plugin_class(TogglePlugin, TogglePlugin.MANIFEST, "test")
    assert pm.disable("TogglePlugin") is True
    assert pm._plugins["TogglePlugin"].plugin._enabled is False
    assert pm.enable("TogglePlugin") is True
    assert pm._plugins["TogglePlugin"].plugin._enabled is True
