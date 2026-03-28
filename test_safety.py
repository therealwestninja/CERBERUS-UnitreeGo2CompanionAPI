import asyncio, pytest
from cerberus.core.event_bus import EventBus, EventType, reset_bus
from cerberus.core.safety import SafetyManager, get_safety

@pytest.fixture(autouse=True)
def fresh_bus(): reset_bus(); yield; reset_bus()

@pytest.fixture
def bus(): return EventBus()

@pytest.fixture
def safety(bus):
    sm = SafetyManager(); sm.bus = bus; sm.register_subscriptions(); return sm

@pytest.mark.asyncio
async def test_estop_trigger_and_clear(bus, safety):
    events = []
    async def on_estop(e): events.append(e)
    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()
    assert not safety.is_stopped()
    await safety.trigger_estop("test reason")
    assert safety.is_stopped()
    assert len(events) == 1 and events[0].data["reason"] == "test reason"
    assert await safety.clear_estop()
    assert not safety.is_stopped()
    await bus.stop()

@pytest.mark.asyncio
async def test_double_estop_idempotent(bus, safety):
    fired = []
    async def on_estop(e): fired.append(e)
    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()
    await safety.trigger_estop("first"); await safety.trigger_estop("second")
    assert len(fired) == 1
    await bus.stop()

@pytest.mark.asyncio
async def test_hr_critical_triggers_estop(bus, safety):
    estop_events = []
    async def on_estop(e): estop_events.append(e)
    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()
    from cerberus.core.event_bus import Event
    await safety.on_heartrate(Event(type=EventType.HEARTRATE_UPDATE, source="test", data={"bpm": 210}))
    assert safety.is_stopped()
    await bus.stop()

@pytest.mark.asyncio
async def test_battery_low_violation(bus, safety):
    violations = []
    async def on_v(e): violations.append(e)
    bus.subscribe(EventType.SAFETY_VIOLATION, on_v, priority=1)
    bus.start_background()
    from cerberus.core.event_bus import Event
    await safety.on_robot_state(Event(type=EventType.ROBOT_STATE_UPDATE, source="test", data={"battery_voltage": 20.5}))
    await asyncio.sleep(0.05)
    assert not safety.state.battery_ok
    await bus.stop()
