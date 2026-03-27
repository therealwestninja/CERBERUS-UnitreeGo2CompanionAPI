"""
CERBERUS — Core Tests
======================
pytest-asyncio test suite for event bus, safety manager, and FunScript player.
Run: pytest tests/
"""
from __future__ import annotations

import asyncio
import pytest
import time

from cerberus.core.event_bus import Event, EventBus, EventType, reset_bus, get_bus
from cerberus.core.safety import SafetyManager


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_bus():
    reset_bus()
    yield
    reset_bus()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def safety(bus) -> SafetyManager:
    sm = SafetyManager()
    sm.bus = bus
    sm.register_subscriptions()
    return sm


# ── EventBus tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_and_publish(bus: EventBus):
    received = []

    async def handler(event: Event):
        received.append(event)

    bus.subscribe(EventType.ROBOT_CONNECTED, handler)
    bus.start_background()

    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)

    assert len(received) == 1
    assert received[0].type == EventType.ROBOT_CONNECTED
    await bus.stop()


@pytest.mark.asyncio
async def test_priority1_bypass_queue(bus: EventBus):
    """Priority-1 events must be dispatched synchronously, not via queue."""
    dispatched = []

    async def handler(event: Event):
        dispatched.append(time.monotonic())

    bus.subscribe(EventType.ESTOP_TRIGGERED, handler, priority=1)
    bus.start_background()

    t0 = time.monotonic()
    await bus.publish(Event(
        type=EventType.ESTOP_TRIGGERED,
        source="test",
        priority=1,
    ))
    t1 = time.monotonic()

    # Priority-1 dispatch is synchronous — should complete within a single await
    assert len(dispatched) == 1
    assert (t1 - t0) < 0.05    # well under one tick
    await bus.stop()


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_crash_bus(bus: EventBus):
    async def bad_handler(event: Event):
        raise RuntimeError("handler crash")

    async def good_handler(event: Event):
        pass

    bus.subscribe(EventType.ROBOT_CONNECTED, bad_handler)
    bus.subscribe(EventType.ROBOT_CONNECTED, good_handler)
    bus.start_background()

    # Should not raise
    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)
    await bus.stop()


@pytest.mark.asyncio
async def test_unsubscribe(bus: EventBus):
    received = []

    async def handler(event: Event):
        received.append(event)

    bus.subscribe(EventType.ROBOT_CONNECTED, handler)
    bus.unsubscribe(EventType.ROBOT_CONNECTED, handler)
    bus.start_background()

    await bus.publish(Event(type=EventType.ROBOT_CONNECTED, source="test"))
    await asyncio.sleep(0.1)

    assert len(received) == 0
    await bus.stop()


# ── SafetyManager tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_estop_trigger_and_clear(bus: EventBus, safety: SafetyManager):
    estop_events = []

    async def on_estop(e: Event):
        estop_events.append(e)

    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()

    assert not safety.is_stopped()
    await safety.trigger_estop("test reason")
    assert safety.is_stopped()
    assert len(estop_events) == 1
    assert estop_events[0].data["reason"] == "test reason"

    cleared = await safety.clear_estop()
    assert cleared
    assert not safety.is_stopped()
    await bus.stop()


@pytest.mark.asyncio
async def test_double_estop_is_idempotent(bus: EventBus, safety: SafetyManager):
    fired = []
    async def on_estop(e): fired.append(e)
    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()

    await safety.trigger_estop("first")
    await safety.trigger_estop("second")  # should be a no-op
    assert len(fired) == 1
    await bus.stop()


@pytest.mark.asyncio
async def test_battery_low_triggers_violation(bus: EventBus, safety: SafetyManager):
    violations = []
    async def on_violation(e): violations.append(e)
    bus.subscribe(EventType.SAFETY_VIOLATION, on_violation, priority=1)
    bus.start_background()

    await safety.on_robot_state(Event(
        type=EventType.ROBOT_STATE_UPDATE,
        source="test",
        data={"battery_voltage": 20.5},
    ))

    await asyncio.sleep(0.05)
    assert not safety.state.battery_ok
    await bus.stop()


@pytest.mark.asyncio
async def test_hr_critical_triggers_estop(bus: EventBus, safety: SafetyManager):
    estop_events = []
    async def on_estop(e): estop_events.append(e)
    bus.subscribe(EventType.ESTOP_TRIGGERED, on_estop, priority=1)
    bus.start_background()

    await safety.on_heartrate(Event(
        type=EventType.HEARTRATE_UPDATE,
        source="test",
        data={"bpm": 210},
    ))

    assert safety.is_stopped()
    assert "HR critical" in safety.state.estop_reason or "critical" in safety.state.estop_reason.lower()
    await bus.stop()


# ── FunScript tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funscript_load_and_play(tmp_path):
    import json
    from plugins.funscript.funscript_player import FunScriptPlugin

    script = {
        "version": "1.0",
        "inverted": False,
        "range": 90,
        "actions": [
            {"at": 0,    "pos": 0},
            {"at": 500,  "pos": 100},
            {"at": 1000, "pos": 0},
        ]
    }
    f = tmp_path / "test.funscript"
    f.write_text(json.dumps(script))

    plugin = FunScriptPlugin(robot_adapter=None)
    await plugin.load({})

    ok = await plugin.load_file(str(f))
    assert ok
    assert plugin._script is not None
    assert len(plugin._script.actions) == 3


@pytest.mark.asyncio
async def test_funscript_bad_file_returns_false(tmp_path):
    from plugins.funscript.funscript_player import FunScriptPlugin
    plugin = FunScriptPlugin()
    await plugin.load({})
    result = await plugin.load_file("/nonexistent/path.funscript")
    assert result is False
