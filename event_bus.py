"""
CERBERUS Event Bus
==================
Central async publish/subscribe bus. All subsystems communicate exclusively
through typed events — no direct cross-system references.

Priority model:
  1  → Safety-critical (ESTOP, watchdog): bypasses queue, dispatched immediately
  2  → Robot control commands
  5  → Normal telemetry / plugin data  (default)
  9  → UI / cosmetic updates
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

logger = logging.getLogger(__name__)

# ── Event taxonomy ────────────────────────────────────────────────────────────

class EventType(Enum):
    # ── Robot ─────────────────────────────────────────────────────────────
    ROBOT_CONNECTED        = auto()
    ROBOT_DISCONNECTED     = auto()
    ROBOT_STATE_UPDATE     = auto()   # Full LowState snapshot
    ROBOT_MOTION_UPDATE    = auto()   # vx, vy, vyaw current
    ROBOT_BATTERY_UPDATE   = auto()   # percent + voltage
    ROBOT_IMU_UPDATE       = auto()   # rpy, angular velocity
    ROBOT_GAIT_CHANGED     = auto()   # gait mode enum changed

    # ── Safety ────────────────────────────────────────────────────────────
    ESTOP_TRIGGERED        = auto()   # Hard stop — priority 1
    ESTOP_CLEARED          = auto()
    SAFETY_VIOLATION       = auto()   # Soft limit breached
    WATCHDOG_TIMEOUT       = auto()   # Subsystem missed deadline

    # ── Plugin lifecycle ──────────────────────────────────────────────────
    PLUGIN_LOADED          = auto()
    PLUGIN_UNLOADED        = auto()
    PLUGIN_ERROR           = auto()

    # ── Peripheral output (robot → device) ────────────────────────────────
    PERIPHERAL_COMMAND     = auto()   # {device: str, intensity: float 0–1, ...}
    PERIPHERAL_CONNECTED   = auto()
    PERIPHERAL_DISCONNECTED = auto()
    PERIPHERAL_STATE       = auto()

    # ── FunScript ─────────────────────────────────────────────────────────
    FUNSCRIPT_LOADED       = auto()
    FUNSCRIPT_PLAY         = auto()
    FUNSCRIPT_PAUSE        = auto()
    FUNSCRIPT_STOP         = auto()
    FUNSCRIPT_TICK         = auto()   # {position: float 0–1, velocity: float}
    FUNSCRIPT_ENDED        = auto()

    # ── Bio signals (Galaxy Fit 2) ─────────────────────────────────────────
    HEARTRATE_UPDATE       = auto()   # {bpm: int}
    HEARTRATE_ALARM        = auto()   # {bpm: int, reason: str} — priority 1
    WEARABLE_CONNECTED     = auto()
    WEARABLE_DISCONNECTED  = auto()

    # ── UI ────────────────────────────────────────────────────────────────
    UI_COMMAND             = auto()   # From UI thread → runtime
    UI_STATE_PUSH          = auto()   # Runtime → UI thread


# ── Event dataclass ───────────────────────────────────────────────────────────

@dataclass
class Event:
    type:      EventType
    source:    str
    data:      dict[str, Any] = field(default_factory=dict)
    priority:  int            = 5       # 1 = highest
    timestamp: float          = field(default_factory=time.monotonic)


AsyncHandler = Callable[[Event], Coroutine[Any, Any, None]]


# ── Bus implementation ─────────────────────────────────────────────────────────

class EventBus:
    """
    Thread-aware async publish / subscribe bus.

    • Priority-1 events skip the queue entirely — dispatched inline so an
      ESTOP call from any coroutine reaches all safety handlers before the
      next await yields control back.
    • Thread-safe publish_sync() lets the Dear PyGui thread enqueue events
      without holding the GIL on the asyncio loop.
    """

    def __init__(self) -> None:
        self._subs: dict[EventType, list[tuple[int, AsyncHandler]]] = defaultdict(list)
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=4096)
        self._running = False
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stats: dict[EventType, int] = defaultdict(int)

    # ── Subscription ─────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: EventType,
        handler: AsyncHandler,
        priority: int = 5,
    ) -> None:
        """Register an async handler. Lower priority number = called first."""
        self._subs[event_type].append((priority, handler))
        self._subs[event_type].sort(key=lambda t: t[0])
        logger.debug("Subscribed %s → %s", handler.__qualname__, event_type.name)

    def unsubscribe(self, event_type: EventType, handler: AsyncHandler) -> None:
        self._subs[event_type] = [
            (p, h) for p, h in self._subs[event_type] if h is not handler
        ]

    # ── Publishing ───────────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """Publish from async context.  Priority-1 events are synchronous."""
        self._stats[event.type] += 1
        if event.priority == 1:
            await self._dispatch(event)
        else:
            await self._queue.put(event)

    def publish_sync(self, event: Event) -> None:
        """
        Thread-safe publish from non-async contexts (UI thread, BLE callback).
        Uses run_coroutine_threadsafe so we never block the calling thread.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(event), self._loop)
        else:
            logger.warning("publish_sync called before bus started: %s", event.type.name)

    # ── Internal dispatch ─────────────────────────────────────────────────

    async def _dispatch(self, event: Event) -> None:
        handlers = self._subs.get(event.type, [])
        for _, handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Handler %s raised on %s", handler.__qualname__, event.type.name
                )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main dispatch loop — run as a long-lived background task."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        logger.info("EventBus running")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                await self._dispatch(event)
                self._queue.task_done()
            except TimeoutError:
                pass
            except Exception:
                logger.exception("EventBus dispatch error")

    def start_background(self) -> asyncio.Task:
        self._loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self.run(), name="cerberus.event_bus")
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("EventBus stopped  |  stats: %s", dict(self._stats))

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()


# ── Module-level singleton ─────────────────────────────────────────────────────

_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_bus() -> None:
    """For tests only."""
    global _bus
    _bus = None
