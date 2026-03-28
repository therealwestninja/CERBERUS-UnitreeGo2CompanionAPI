import asyncio, pytest, time
from cerberus.core.event_bus import Event, EventBus, EventType, reset_bus

@pytest.fixture(autouse=True)
def fresh_bus(): reset_bus(); yield; reset_bus()

@pytest.mark.asyncio
async def test_subscribe_and_publish():
    bus = EventBus()
    received = []
    async def handler(event): received.append(event)
    bus.subscribe(EventType.ROBOT_CONNECTED, handler)
    bus.start_background()
    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)
    assert len(received) == 1
    await bus.stop()

@pytest.mark.asyncio
async def test_priority1_bypass_queue():
    bus = EventBus()
    dispatched = []
    async def handler(event): dispatched.append(time.monotonic())
    bus.subscribe(EventType.ESTOP_TRIGGERED, handler, priority=1)
    bus.start_background()
    t0 = time.monotonic()
    await bus.publish(Event(type=EventType.ESTOP_TRIGGERED, source="test", priority=1))
    t1 = time.monotonic()
    assert len(dispatched) == 1
    assert (t1 - t0) < 0.05
    await bus.stop()

@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    received = []
    async def handler(event): received.append(event)
    bus.subscribe(EventType.ROBOT_CONNECTED, handler)
    bus.unsubscribe(EventType.ROBOT_CONNECTED, handler)
    bus.start_background()
    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)
    assert len(received) == 0
    await bus.stop()

@pytest.mark.asyncio
async def test_handler_crash_does_not_kill_bus():
    bus = EventBus()
    good_ran = []
    async def bad_handler(e): raise RuntimeError("crash")
    async def good_handler(e): good_ran.append(True)
    bus.subscribe(EventType.ROBOT_CONNECTED, bad_handler)
    bus.subscribe(EventType.ROBOT_CONNECTED, good_handler)
    bus.start_background()
    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)
    assert len(good_ran) == 1
    await bus.stop()
