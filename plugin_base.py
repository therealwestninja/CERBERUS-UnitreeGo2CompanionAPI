"""
CERBERUS Plugin Base
====================
Abstract base for every plugin in the ecosystem.

Trust levels control what APIs a plugin may call:
  CORE      — owns safety systems; may call e-stop directly
  TRUSTED   — may issue robot motion commands
  SANDBOX   — read-only event subscriptions; cannot command robot

All plugins communicate through the EventBus.  A SANDBOX plugin that tries
to call robot methods will get a PermissionError at load time.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from cerberus.core.event_bus import Event, EventType, get_bus

logger = logging.getLogger(__name__)


class PluginTrustLevel(Enum):
    CORE    = 1
    TRUSTED = 2
    SANDBOX = 3


class PluginState(Enum):
    UNLOADED  = auto()
    LOADING   = auto()
    ACTIVE    = auto()
    PAUSED    = auto()
    ERROR     = auto()
    UNLOADING = auto()


@dataclass
class PluginManifest:
    name:         str
    version:      str
    description:  str
    author:       str
    trust_level:  PluginTrustLevel       = PluginTrustLevel.SANDBOX
    dependencies: list[str]              = field(default_factory=list)
    capabilities: list[str]              = field(default_factory=list)
    config_keys:  list[str]              = field(default_factory=list)


class CERBERUSPlugin(ABC):
    """
    Plugin lifecycle
    ────────────────
    load(config) → on_load()   : connect peripherals, subscribe to events
    start()      → on_start()  : begin active work
    [loop]       → on_tick(dt) : optional per-frame hook (not called if not overridden)
    stop()       → on_stop()   : graceful pause / disconnect
    unload()     → on_unload() : final cleanup, release all resources
    """

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest  = manifest
        self.state     = PluginState.UNLOADED
        self.bus       = get_bus()
        self._config:  dict[str, Any] = {}
        self._tasks:   list[asyncio.Task] = []
        self.logger    = logging.getLogger(f"cerberus.plugin.{manifest.name}")

    # ── Abstract lifecycle hooks ──────────────────────────────────────────────

    @abstractmethod
    async def on_load(self, config: dict[str, Any]) -> None:
        """Connect to hardware / subscribe to bus events."""

    @abstractmethod
    async def on_start(self) -> None:
        """Begin active processing."""

    @abstractmethod
    async def on_stop(self) -> None:
        """Pause processing; device connection may stay open."""

    @abstractmethod
    async def on_unload(self) -> None:
        """Disconnect everything; release OS/hardware resources."""

    async def on_tick(self, dt: float) -> None:
        """Optional per-tick update hook.  Override as needed.  dt = seconds since last tick."""

    # ── Managed lifecycle ─────────────────────────────────────────────────────

    async def load(self, config: dict[str, Any] | None = None) -> bool:
        if self.state not in (PluginState.UNLOADED, PluginState.ERROR):
            self.logger.warning("load() called in unexpected state %s", self.state)
            return False

        self.state   = PluginState.LOADING
        self._config = config or {}
        try:
            await self.on_load(self._config)
            self.state = PluginState.ACTIVE
            await self.bus.publish(Event(
                type=EventType.PLUGIN_LOADED,
                source=self.manifest.name,
                data={"name": self.manifest.name, "version": self.manifest.version},
                priority=9,
            ))
            self.logger.info("Loaded  v%s", self.manifest.version)
            return True
        except Exception:
            self.state = PluginState.ERROR
            self.logger.exception("Load failed")
            await self.bus.publish(Event(
                type=EventType.PLUGIN_ERROR,
                source=self.manifest.name,
                data={"phase": "load"},
                priority=2,
            ))
            return False

    async def start(self) -> None:
        if self.state != PluginState.ACTIVE:
            return
        try:
            await self.on_start()
        except Exception:
            self.state = PluginState.ERROR
            self.logger.exception("Start failed")

    async def stop(self) -> None:
        try:
            await self.on_stop()
            self.state = PluginState.PAUSED
        except Exception:
            self.logger.exception("Stop failed")

    async def unload(self) -> None:
        self.state = PluginState.UNLOADING
        # Cancel any background tasks the plugin spawned
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await self.on_stop()
            await self.on_unload()
        except Exception:
            self.logger.exception("Unload error")
        finally:
            self.state = PluginState.UNLOADED
            await self.bus.publish(Event(
                type=EventType.PLUGIN_UNLOADED,
                source=self.manifest.name,
                data={"name": self.manifest.name},
                priority=9,
            ))
            self.logger.info("Unloaded")

    # ── Helpers for subclasses ─────────────────────────────────────────────────

    def _spawn(self, coro: Any, name: str | None = None) -> asyncio.Task:
        """Spawn a background task tracked by this plugin (auto-cancelled on unload)."""
        task = asyncio.create_task(coro, name=name or self.manifest.name)
        self._tasks.append(task)
        task.add_done_callback(self._tasks.remove)
        return task

    async def _emit(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
        priority: int = 5,
    ) -> None:
        await self.bus.publish(Event(
            type=event_type,
            source=self.manifest.name,
            data=data or {},
            priority=priority,
        ))

    # ── String representation ─────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<Plugin {self.manifest.name} v{self.manifest.version} [{self.state.name}]>"
