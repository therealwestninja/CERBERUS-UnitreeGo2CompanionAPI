import pytest
from cerberus.bridge.go2_bridge import SimBridge, SportMode, RobotState, create_bridge

@pytest.fixture
def sim_bridge(): return SimBridge()

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
    await sim_bridge.move(0.5, 0.0, 0.0)
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
    result = await sim_bridge.emergency_stop()
    assert result is True
    state = await sim_bridge.get_state()
    assert state.estop_active is True
    await sim_bridge.disconnect()

@pytest.mark.asyncio
async def test_sim_bridge_led(sim_bridge):
    await sim_bridge.connect()
    assert await sim_bridge.set_led(255, 0, 128) is True

@pytest.mark.asyncio
async def test_create_bridge_sim_env(monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "true")
    b = create_bridge()
    assert isinstance(b, SimBridge)

@pytest.mark.asyncio
async def test_robot_state_to_dict(sim_bridge):
    await sim_bridge.connect()
    d = (await sim_bridge.get_state()).to_dict()
    for key in ("battery", "velocity", "imu", "joints", "estop_active"):
        assert key in d
