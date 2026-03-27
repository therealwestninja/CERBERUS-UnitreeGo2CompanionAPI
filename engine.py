"""
cerberus/behavior/engine.py
===========================
Behavior Engine: maps canine-emulative behaviors to Go2 SDK calls.

Architecture
------------
Behaviors are registered by name. The engine maintains:
  - An active behavior stack (priority queue)
  - An interrupt system for safety-critical overrides
  - Async execution so the FastAPI event loop is not blocked

Built-in behaviors (Go2 native)
--------------------------------
idle, sit, greet, dance, stretch, explore, follow_person, patrol

Imported from go2_robot / Unitree sport service
-----------------------------------------------
All modes from AVAILABLE_MODES are mapped directly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

if TYPE_CHECKING:
    from cerberus.hardware.go2_bridge import Go2Bridge, RobotState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority levels
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    SAFETY    = 0
    CRITICAL  = 10
    HIGH      = 20
    NORMAL    = 50
    LOW       = 80
    IDLE      = 100


# ---------------------------------------------------------------------------
# Behavior descriptor
# ---------------------------------------------------------------------------

BehaviorFn = Callable[["BehaviorContext"], Coroutine[Any, Any, None]]


@dataclass
class BehaviorDescriptor:
    name:        str
    fn:          BehaviorFn
    priority:    Priority = Priority.NORMAL
    interruptible: bool   = True
    cooldown_s:  float    = 0.0
    description: str      = ""

    # Runtime state (not part of identity)
    last_run:    float    = field(default=0.0, compare=False, repr=False)


@dataclass
class BehaviorContext:
    bridge:      "Go2Bridge"
    state:       "RobotState"
    params:      dict[str, Any] = field(default_factory=dict)
    interrupted: bool           = False
    start_time:  float          = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Behavior Engine
# ---------------------------------------------------------------------------

class BehaviorEngine:
    """
    Registry and executor for CERBERUS behaviors.

    Lifecycle
    ---------
    1. Register behaviors via `register()` or use the built-in defaults.
    2. Call `start()` to launch the background tick loop.
    3. Request behavior execution with `enqueue()`.
    4. Call `stop()` on shutdown.
    """

    def __init__(self, bridge: "Go2Bridge", tick_rate_hz: float = 10.0) -> None:
        self._bridge    = bridge
        self._tick_s    = 1.0 / tick_rate_hz
        self._registry: dict[str, BehaviorDescriptor] = {}
        # Queue items: (priority_int, counter, descriptor, params)
        # Counter ensures FIFO within same priority, avoids comparing descriptors.
        self._queue: asyncio.PriorityQueue[tuple[int, int, BehaviorDescriptor, dict]] = (
            asyncio.PriorityQueue()
        )
        self._enqueue_counter = 0
        self._current:  Optional[BehaviorDescriptor] = None
        self._running   = False
        self._task:     Optional[asyncio.Task] = None
        self._history:  list[dict[str, Any]] = []
        self._register_defaults()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, descriptor: BehaviorDescriptor) -> None:
        self._registry[descriptor.name] = descriptor
        logger.debug("Registered behavior '%s'", descriptor.name)

    def _register_defaults(self) -> None:
        behaviors: list[BehaviorDescriptor] = [
            BehaviorDescriptor("idle",          _beh_idle,          Priority.IDLE,   True,  0.0,  "Stand idle"),
            BehaviorDescriptor("sit",           _beh_sit,           Priority.NORMAL, True,  1.0,  "Sit down"),
            BehaviorDescriptor("stand",         _beh_stand,         Priority.NORMAL, True,  1.0,  "Stand up"),
            BehaviorDescriptor("greet",         _beh_greet,         Priority.HIGH,   True,  5.0,  "Greet a person"),
            BehaviorDescriptor("stretch",       _beh_stretch,       Priority.LOW,    True,  10.0, "Stretch"),
            BehaviorDescriptor("dance",         _beh_dance,         Priority.LOW,    True,  15.0, "Dance"),
            BehaviorDescriptor("patrol",        _beh_patrol,        Priority.NORMAL, True,  30.0, "Patrol a small area"),
            BehaviorDescriptor("wag",           _beh_wag,           Priority.NORMAL, True,  3.0,  "Wag (wallow motion)"),
            BehaviorDescriptor("alert",         _beh_alert,         Priority.HIGH,   True,  2.0,  "Alert / attentive posture"),
            BehaviorDescriptor("emergency_sit", _beh_emergency_sit, Priority.SAFETY, False, 0.0,  "Emergency sit/damp"),
        ]
        for b in behaviors:
            self.register(b)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("BehaviorEngine started (%.0fHz)", 1.0 / self._tick_s)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    async def enqueue(self, name: str, params: dict | None = None,
                      priority: Priority | None = None) -> None:
        desc = self._registry.get(name)
        if not desc:
            raise ValueError(f"Unknown behavior '{name}'. Available: {sorted(self._registry)}")

        now = time.monotonic()
        if desc.cooldown_s > 0 and (now - desc.last_run) < desc.cooldown_s:
            remaining = desc.cooldown_s - (now - desc.last_run)
            logger.info("Behavior '%s' on cooldown (%.1fs remaining)", name, remaining)
            return

        p = priority if priority is not None else desc.priority
        self._enqueue_counter += 1
        await self._queue.put((int(p), self._enqueue_counter, desc, params or {}))
        logger.debug("Enqueued behavior '%s' (priority %d)", name, p)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def current_behavior(self) -> Optional[str]:
        return self._current.name if self._current else None

    @property
    def available_behaviors(self) -> list[str]:
        return sorted(self._registry.keys())

    @property
    def history(self) -> list[dict]:
        return list(self._history[-50:])  # last 50

    # ------------------------------------------------------------------
    # Internal tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                _, _ctr, desc, params = await asyncio.wait_for(
                    self._queue.get(), timeout=self._tick_s
                )
                await self._execute(desc, params)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("BehaviorEngine tick error: %s", exc, exc_info=True)

    async def _execute(self, desc: BehaviorDescriptor, params: dict) -> None:
        state = await self._bridge.get_state()
        ctx = BehaviorContext(bridge=self._bridge, state=state, params=params)
        self._current = desc
        t0 = time.monotonic()
        try:
            logger.info("Executing behavior '%s'", desc.name)
            await desc.fn(ctx)
            desc.last_run = time.monotonic()
        except asyncio.CancelledError:
            ctx.interrupted = True
        except Exception as exc:
            logger.error("Behavior '%s' raised: %s", desc.name, exc, exc_info=True)
        finally:
            elapsed = time.monotonic() - t0
            self._history.append({
                "behavior": desc.name,
                "started_at": t0,
                "duration_s": elapsed,
                "interrupted": ctx.interrupted,
                "params": params,
            })
            self._current = None


# ---------------------------------------------------------------------------
# Built-in behavior implementations
# ---------------------------------------------------------------------------

async def _beh_idle(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")

async def _beh_sit(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("sit")
    await asyncio.sleep(0.5)

async def _beh_stand(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("stand_up")
    await asyncio.sleep(1.0)
    await ctx.bridge.set_mode("balance_stand")

async def _beh_greet(ctx: BehaviorContext) -> None:
    """Canine greeting: stand, head tilt, hello motion."""
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.5)
    # Tilt head / look attentive
    await ctx.bridge.set_euler(0.15, 0.0, 0.2)
    await asyncio.sleep(0.8)
    await ctx.bridge.set_euler(0.0, 0.0, 0.0)
    await asyncio.sleep(0.3)
    await ctx.bridge.set_mode("hello")
    await asyncio.sleep(2.0)

async def _beh_stretch(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.5)
    await ctx.bridge.set_mode("stretch")
    await asyncio.sleep(3.0)

async def _beh_dance(ctx: BehaviorContext) -> None:
    dance_choice = random.choice(["dance1", "dance2"])
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.5)
    await ctx.bridge.set_mode(dance_choice)
    await asyncio.sleep(5.0)

async def _beh_patrol(ctx: BehaviorContext) -> None:
    """Walk a small square patrol loop."""
    speed = ctx.params.get("speed", 0.3)
    turn  = ctx.params.get("turn_rate", 0.5)
    steps = ctx.params.get("steps", 4)
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.5)
    for _ in range(steps):
        if ctx.interrupted:
            break
        await ctx.bridge.move(speed, 0.0, 0.0)
        await asyncio.sleep(2.0)
        await ctx.bridge.move(0.0, 0.0, turn)
        await asyncio.sleep(1.5)
    await ctx.bridge.stop()

async def _beh_wag(ctx: BehaviorContext) -> None:
    """Canine tail-wag emulation using wallow motion."""
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.3)
    await ctx.bridge.set_mode("wallow")
    await asyncio.sleep(3.0)

async def _beh_alert(ctx: BehaviorContext) -> None:
    """Alert/attentive posture — raise body height, head up."""
    await ctx.bridge.set_mode("balance_stand")
    await ctx.bridge.set_body_height(0.46)
    await ctx.bridge.set_euler(-0.1, 0.0, 0.0)
    await asyncio.sleep(ctx.params.get("duration", 5.0))
    await ctx.bridge.set_body_height(0.38)
    await ctx.bridge.set_euler(0.0, 0.0, 0.0)

async def _beh_emergency_sit(ctx: BehaviorContext) -> None:
    await ctx.bridge.emergency_stop()
    await asyncio.sleep(0.5)
    await ctx.bridge.set_mode("sit")
