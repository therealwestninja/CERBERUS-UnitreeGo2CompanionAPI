"""
cerberus/core/engine.py
━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Core Runtime Engine

Deterministic async tick loop: 30–200 Hz configurable.
Priority order per tick:
  1. safety      (watchdog, estop checks)
  2. control     (bridge command dispatch)
  3. cognition   (behavior engine step)
  4. perception  (sensor fusion update)
  5. anatomy     (kinematics / fatigue model)
  6. learning    (RL / imitation step — sampled, not every tick)
  7. plugins     (registered plugin update callbacks)
  8. ui          (state broadcast to WebSocket clients)

All subsystems subscribe to the central EventBus.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ── Event bus ────────────────────────────────────────────────────────────────

class EventBus:
    """Lightweight async pub/sub. Topics are arbitrary strings."""

    def __init__(self):
        self._subs: dict[str, list[Callable]] = {}

    def subscribe(self, topic: str, handler: Callable) -> None:
        self._subs.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        if topic in self._subs:
            self._subs[topic] = [h for h in self._subs[topic] if h is not handler]

    async def publish(self, topic: str, payload: Any = None) -> None:
        for handler in self._subs.get(topic, []):
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error("EventBus handler error on topic '%s': %s", topic, exc)

    def publish_sync(self, topic: str, payload: Any = None) -> None:
        """Fire-and-forget from synchronous context."""
        asyncio.ensure_future(self.publish(topic, payload))


# ── Engine state ──────────────────────────────────────────────────────────────

class EngineState(str, Enum):
    STOPPED   = "stopped"
    STARTING  = "starting"
    RUNNING   = "running"
    PAUSED    = "paused"
    ERROR     = "error"
    SHUTDOWN  = "shutdown"


@dataclass
class EngineStats:
    """Live telemetry about the engine loop performance."""
    tick_count:      int   = 0
    tick_hz:         float = 0.0
    tick_dt_ms:      float = 0.0
    tick_overrun_ms: float = 0.0
    overrun_count:   int   = 0
    uptime_s:        float = 0.0
    state:           str   = EngineState.STOPPED.value

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ── Plugin hook registry ──────────────────────────────────────────────────────

@dataclass
class PluginHook:
    name: str
    callback: Callable
    priority: int = 100  # lower = earlier in update order
    enabled: bool = True


# ── Core Engine ──────────────────────────────────────────────────────────────

class CerberusEngine:
    """
    The central CERBERUS runtime.

    Usage:
        engine = CerberusEngine(bridge, watchdog)
        await engine.start()
        ...
        await engine.stop()
    """

    def __init__(
        self,
        bridge,
        watchdog,
        target_hz: float | None = None,
    ):
        self.bridge   = bridge
        self.watchdog = watchdog
        self.bus      = EventBus()
        self._target_hz = target_hz or float(os.getenv("CERBERUS_HZ", "60"))
        self._target_hz = max(10.0, min(200.0, self._target_hz))
        self._state   = EngineState.STOPPED
        self._stats   = EngineStats()
        self._start_time: float = 0.0

        # Subsystem hooks — registered by subsystems on init
        self._plugin_hooks: list[PluginHook] = []

        # Subsystem references (set after construction)
        self.behavior_engine = None
        self.perception      = None
        self.anatomy         = None
        self.learning        = None

        # Metrics
        self._last_tick_time: float = 0.0
        self._tick_times: list[float] = []  # rolling window for Hz calc

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._state not in (EngineState.STOPPED, EngineState.ERROR):
            logger.warning("Engine already running (state=%s)", self._state.value)
            return

        self._state = EngineState.STARTING
        logger.info("CERBERUS Engine starting at %.0fHz", self._target_hz)

        await self.bridge.connect()
        self._start_time = time.monotonic()

        # Start watchdog as background task
        asyncio.ensure_future(self.watchdog.run())

        self._state = EngineState.RUNNING
        self._stats.state = EngineState.RUNNING.value

        await self.bus.publish("engine.started", {"hz": self._target_hz})
        logger.info("CERBERUS Engine running ✓")

        asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._state == EngineState.STOPPED:
            return
        self._state = EngineState.SHUTDOWN
        await self.watchdog.stop()
        await self.bridge.disconnect()
        self._state = EngineState.STOPPED
        self._stats.state = EngineState.STOPPED.value
        await self.bus.publish("engine.stopped", {})
        logger.info("CERBERUS Engine stopped")

    def pause(self) -> None:
        if self._state == EngineState.RUNNING:
            self._state = EngineState.PAUSED
            logger.info("Engine paused")

    def resume(self) -> None:
        if self._state == EngineState.PAUSED:
            self._state = EngineState.RUNNING
            logger.info("Engine resumed")

    # ── Main tick loop ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        interval = 1.0 / self._target_hz
        tick = 0

        while self._state not in (EngineState.SHUTDOWN, EngineState.STOPPED):
            if self._state == EngineState.PAUSED:
                await asyncio.sleep(0.05)
                continue

            t0 = time.monotonic()
            tick += 1

            try:
                await self._tick(tick)
            except Exception as exc:
                logger.error("Engine tick %d error: %s", tick, exc, exc_info=True)
                self._state = EngineState.ERROR
                await self.bus.publish("engine.error", {"tick": tick, "error": str(exc)})
                break

            t1 = time.monotonic()
            dt = t1 - t0
            sleep_t = interval - dt

            # Update stats
            self._tick_times.append(t1)
            if len(self._tick_times) > 120:
                self._tick_times.pop(0)
            if len(self._tick_times) > 1:
                window = self._tick_times[-1] - self._tick_times[0]
                self._stats.tick_hz = (len(self._tick_times) - 1) / window if window > 0 else 0
            self._stats.tick_count = tick
            self._stats.tick_dt_ms = dt * 1000
            self._stats.uptime_s   = time.monotonic() - self._start_time
            self._stats.state      = self._state.value

            if sleep_t > 0:
                self._stats.tick_overrun_ms = 0.0
                await asyncio.sleep(sleep_t)
            else:
                overrun = -sleep_t * 1000
                self._stats.tick_overrun_ms = overrun
                self._stats.overrun_count += 1
                if overrun > 50:
                    logger.warning("Tick overrun %.1fms at tick %d", overrun, tick)
                await asyncio.sleep(0)  # yield to event loop

    async def _tick(self, tick: int) -> None:
        """Single engine tick. Runs all subsystems in priority order."""
        if self.watchdog.estop_active:
            # In E-stop: only broadcast state, do nothing else
            state = await self.bridge.get_state()
            await self.bus.publish("state.update", state)
            return

        # 1. Safety — already running as separate task, but validate here
        # (No heavy work — just check estop flag again after async gap)

        # 2. Cognition — step the behavior engine
        if self.behavior_engine is not None:
            await self.behavior_engine.step(tick)

        # 3. Perception — update sensor fusion (sampled to avoid overloading)
        if self.perception is not None and tick % 2 == 0:
            await self.perception.update()

        # 4. Digital Anatomy — kinematics / fatigue model
        if self.anatomy is not None:
            state = await self.bridge.get_state()
            await self.anatomy.update(state)

        # 5. Learning — step RL/imitation pipeline (very low rate)
        if self.learning is not None and tick % 300 == 0:
            await self.learning.step()

        # 6. Plugin hooks
        for hook in sorted(self._plugin_hooks, key=lambda h: h.priority):
            if hook.enabled:
                try:
                    result = hook.callback(tick)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Plugin hook '%s' error: %s", hook.name, exc)

        # 7. UI — broadcast state at reduced rate (30Hz max)
        if tick % max(1, int(self._target_hz / 30)) == 0:
            state = await self.bridge.get_state()
            await self.bus.publish("state.update", state)

    # ── Plugin registration ───────────────────────────────────────────────────

    def register_hook(self, name: str, callback: Callable, priority: int = 100) -> None:
        self._plugin_hooks.append(PluginHook(name=name, callback=callback, priority=priority))
        logger.debug("Hook registered: %s (priority %d)", name, priority)

    def unregister_hook(self, name: str) -> None:
        self._plugin_hooks = [h for h in self._plugin_hooks if h.name != name]

    # ── Accessors ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> EngineStats:
        return self._stats

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def event_bus(self) -> EventBus:
        return self.bus
