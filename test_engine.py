import asyncio, pytest
from cerberus.bridge.go2_bridge import SimBridge
from cerberus.core.engine import CerberusEngine, EngineState
from cerberus.core.safety_watchdog import SafetyWatchdog, SafetyLimits

@pytest.fixture
def bridge(): return SimBridge()

@pytest.fixture
def watchdog(bridge): return SafetyWatchdog(bridge, SafetyLimits(heartbeat_timeout_s=2.0))

@pytest.fixture
def engine(bridge, watchdog): return CerberusEngine(bridge, watchdog, target_hz=30.0)

@pytest.mark.asyncio
async def test_engine_start_stop(engine):
    await engine.start()
    assert engine.state == EngineState.RUNNING
    await asyncio.sleep(0.15)
    await engine.stop()
    assert engine.state == EngineState.STOPPED

@pytest.mark.asyncio
async def test_engine_ticks(engine):
    await engine.start()
    await asyncio.sleep(0.4)
    assert engine.stats.tick_count > 5
    assert engine.stats.tick_hz > 0
    await engine.stop()

@pytest.mark.asyncio
async def test_engine_pause_resume(engine):
    await engine.start()
    await asyncio.sleep(0.1)
    engine.pause()
    before = engine.stats.tick_count
    await asyncio.sleep(0.2)
    paused = engine.stats.tick_count
    engine.resume()
    await asyncio.sleep(0.2)
    after = engine.stats.tick_count
    assert paused <= before + 2   # minimal ticks while paused
    assert after > paused
    await engine.stop()

@pytest.mark.asyncio
async def test_engine_plugin_hook(engine):
    seen = []
    async def my_hook(tick): seen.append(tick)
    engine.register_hook("test_hook", my_hook)
    await engine.start()
    await asyncio.sleep(0.25)
    await engine.stop()
    assert len(seen) > 0

@pytest.mark.asyncio
async def test_watchdog_velocity_validation(watchdog, bridge):
    await bridge.connect()
    ok, _ = watchdog.validate_velocity(0.5, 0.3, 1.0)
    assert ok is True
    ok, reason = watchdog.validate_velocity(999.0, 0.0, 0.0)
    assert ok is False and "vx" in reason

@pytest.mark.asyncio
async def test_watchdog_estop(bridge, watchdog):
    await bridge.connect()
    await watchdog.trigger_estop("test")
    assert watchdog.estop_active is True

@pytest.mark.asyncio
async def test_watchdog_clear_sim_only(bridge, watchdog, monkeypatch):
    monkeypatch.setenv("GO2_SIMULATION", "true")
    await bridge.connect()
    await watchdog.trigger_estop("test")
    assert await watchdog.clear_estop() is True
    assert watchdog.estop_active is False
