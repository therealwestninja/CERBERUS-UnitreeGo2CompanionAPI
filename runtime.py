"""
CERBERUS Runtime
================
The main orchestration layer.

Tick priority order (matches Vision Document §4.A):
  1. Safety checks (watchdog, constraint verification)
  2. Robot state polling
  3. Cognition / behavior
  4. Plugin ticks (peripheral, FunScript, etc.)
  5. UI state push

Plugins are registered here and ticked each frame.
The runtime owns the event bus and safety manager lifecycles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from cerberus.core.event_bus import Event, EventBus, EventType, get_bus
from cerberus.core.plugin_base import CERBERUSPlugin, PluginState
from cerberus.core.safety import SafetyManager, get_safety

logger = logging.getLogger(__name__)

TARGET_HZ      = 30          # nominal tick rate
TICK_PERIOD_S  = 1.0 / TARGET_HZ


@dataclass
class RuntimeStats:
    ticks:         int   = 0
    tick_overruns: int   = 0
    uptime_s:      float = 0.0
    plugins_active: int  = 0


class CERBERUSRuntime:
    """
    Top-level runtime.  One instance per process.

    Usage
    ─────
    runtime = CERBERUSRuntime()
    await runtime.load_plugin(FunScriptPlugin(), config={...})
    await runtime.start()
    # runs until Ctrl+C or runtime.shutdown()
    """

    def __init__(self, robot_adapter: Any | None = None) -> None:
        self.bus:     EventBus     = get_bus()
        self.safety:  SafetyManager = get_safety()
        self.robot    = robot_adapter
        self._plugins: dict[str, CERBERUSPlugin] = {}
        self._running  = False
        self._stats    = RuntimeStats()
        self._start_ts = 0.0

        # Wire safety into bus before anything else
        self.safety.register_subscriptions()

        # Subscribe runtime-level handlers
        self.bus.subscribe(EventType.ESTOP_TRIGGERED, self._on_estop, priority=1)
        self.bus.subscribe(EventType.UI_COMMAND,      self._on_ui_command, priority=5)

    # ── Plugin management ─────────────────────────────────────────────────────

    async def load_plugin(
        self,
        plugin: CERBERUSPlugin,
        config: dict[str, Any] | None = None,
    ) -> bool:
        name = plugin.manifest.name
        if name in self._plugins:
            logger.warning("Plugin already loaded: %s", name)
            return False
        ok = await plugin.load(config)
        if ok:
            self._plugins[name] = plugin
            logger.info("Plugin registered: %s", name)
        return ok

    async def unload_plugin(self, name: str) -> None:
        plugin = self._plugins.pop(name, None)
        if plugin:
            await plugin.unload()

    def get_plugin(self, name: str) -> CERBERUSPlugin | None:
        return self._plugins.get(name)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("CERBERUS Runtime starting")
        self._running  = True
        self._start_ts = time.monotonic()

        # Start event bus
        self.bus.start_background()

        # Connect robot
        if self.robot:
            try:
                await self.robot.connect()
            except Exception:
                logger.exception("Robot connection failed — continuing in simulation mode")

        # Start all plugins
        for plugin in self._plugins.values():
            await plugin.start()

        # Enter tick loop
        await self._tick_loop()

    async def shutdown(self) -> None:
        logger.info("Runtime shutting down")
        self._running = False
        for plugin in list(self._plugins.values()):
            await plugin.unload()
        if self.robot:
            await self.robot.disconnect()
        await self.bus.stop()
        self._stats.uptime_s = time.monotonic() - self._start_ts
        logger.info(
            "Shutdown complete  |  uptime=%.1fs  ticks=%d  overruns=%d",
            self._stats.uptime_s,
            self._stats.ticks,
            self._stats.tick_overruns,
        )

    # ── Main tick loop ────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        logger.info("Tick loop started at %dHz", TARGET_HZ)
        last_tick = time.monotonic()

        while self._running:
            tick_start = time.monotonic()
            dt = tick_start - last_tick
            last_tick = tick_start

            await self._tick(dt)

            self._stats.ticks += 1
            elapsed = time.monotonic() - tick_start
            sleep_t = TICK_PERIOD_S - elapsed

            if sleep_t < 0:
                self._stats.tick_overruns += 1
                logger.debug("Tick overrun: %.3fms over budget", -sleep_t * 1000)
                sleep_t = 0.0

            await asyncio.sleep(sleep_t)

    async def _tick(self, dt: float) -> None:
        # 1 ── Safety ────────────────────────────────────────────────────
        await self.safety.watchdog_check()
        if self.safety.is_stopped():
            await self._push_ui_state()
            return               # halt all further processing while estoped

        # 2 ── Robot state poll ─────────────────────────────────────────
        if self.robot and self.robot.connected:
            state = await self.robot.get_state()
            if state:
                await self.bus.publish(Event(
                    type=EventType.ROBOT_STATE_UPDATE,
                    source="robot",
                    data=state,
                    priority=2,
                ))

        # 3 ── Plugin ticks ─────────────────────────────────────────────
        active = [p for p in self._plugins.values() if p.state == PluginState.ACTIVE]
        self._stats.plugins_active = len(active)
        await asyncio.gather(*(p.on_tick(dt) for p in active), return_exceptions=True)

        # 4 ── UI push ──────────────────────────────────────────────────
        await self._push_ui_state()

    # ── UI state push ─────────────────────────────────────────────────────────

    async def _push_ui_state(self) -> None:
        robot_state = {}
        if self.robot:
            robot_state = self.robot.last_state or {}

        await self.bus.publish(Event(
            type=EventType.UI_STATE_PUSH,
            source="runtime",
            data={
                "estop":          self.safety.is_stopped(),
                "estop_reason":   self.safety.state.estop_reason,
                "robot_connected": bool(self.robot and self.robot.connected),
                "robot_state":    robot_state,
                "plugins": {
                    name: p.state.name for name, p in self._plugins.items()
                },
                "ticks":          self._stats.ticks,
                "overruns":       self._stats.tick_overruns,
                "queue_depth":    self.bus.queue_depth,
            },
            priority=9,
        ))

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _on_estop(self, event: Event) -> None:
        """When e-stop fires, command the robot to halt immediately."""
        logger.critical("Runtime received ESTOP: %s", event.data.get("reason"))
        if self.robot and self.robot.connected:
            try:
                await self.robot.emergency_stop()
            except Exception:
                logger.exception("Robot emergency_stop() failed")

    async def _on_ui_command(self, event: Event) -> None:
        cmd = event.data.get("command")
        if cmd == "estop":
            await self.safety.trigger_estop("operator-requested", source="ui")
        elif cmd == "clear_estop":
            await self.safety.clear_estop("operator")
        elif cmd == "load_funscript":
            path = event.data.get("path")
            fs = self.get_plugin("FunScript")
            if fs and path:
                await fs.load_file(path)
        elif cmd == "play":
            fs = self.get_plugin("FunScript")
            if fs:
                await fs.play()
        elif cmd == "pause":
            fs = self.get_plugin("FunScript")
            if fs:
                await fs.pause()
        elif cmd == "stop":
            fs = self.get_plugin("FunScript")
            if fs:
                await fs.stop()
