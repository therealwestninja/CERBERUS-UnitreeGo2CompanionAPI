"""
cerberus/behavior/engine.py  — CERBERUS v3.1
============================================
Priority-queued async behavior engine with 10 built-in canine behaviors.
All behaviors wired to real Go2Bridge calls.
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
    from cerberus.hardware.bridge import Go2Bridge, RobotState

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    SAFETY = 0; CRITICAL = 10; HIGH = 20; NORMAL = 50; LOW = 80; IDLE = 100


BehaviorFn = Callable[["BehaviorContext"], Coroutine[Any, Any, None]]


@dataclass
class BehaviorDescriptor:
    name:          str
    fn:            BehaviorFn
    priority:      Priority = Priority.NORMAL
    interruptible: bool     = True
    cooldown_s:    float    = 0.0
    description:   str      = ""
    last_run:      float    = field(default=0.0, compare=False, repr=False)


@dataclass
class BehaviorContext:
    bridge:      "Go2Bridge"
    state:       "RobotState"
    params:      dict = field(default_factory=dict)
    interrupted: bool = False


class BehaviorEngine:
    def __init__(self, bridge: "Go2Bridge", tick_rate_hz: float = 10.0) -> None:
        self._bridge  = bridge
        self._tick_s  = 1.0 / tick_rate_hz
        self._reg:    dict[str, BehaviorDescriptor] = {}
        self._queue:  asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._ctr:    int = 0
        self._current: Optional[str] = None
        self._running = False
        self._task:   Optional[asyncio.Task] = None
        self._history: list[dict] = []
        self._register_defaults()

    # ── Registration ───────────────────────────────────────────────────── #
    def register(self, d: BehaviorDescriptor) -> None:
        self._reg[d.name] = d

    def _register_defaults(self) -> None:
        for d in [
            BehaviorDescriptor("idle",          _idle,          Priority.IDLE,   True,  0.0,  "Balance stand"),
            BehaviorDescriptor("sit",           _sit,           Priority.NORMAL, True,  1.0,  "Sit down"),
            BehaviorDescriptor("stand",         _stand,         Priority.NORMAL, True,  1.0,  "Stand up"),
            BehaviorDescriptor("greet",         _greet,         Priority.HIGH,   True,  5.0,  "Canine greeting"),
            BehaviorDescriptor("stretch",       _stretch,       Priority.LOW,    True,  10.0, "Full-body stretch"),
            BehaviorDescriptor("dance",         _dance,         Priority.LOW,    True,  15.0, "Dance"),
            BehaviorDescriptor("patrol",        _patrol,        Priority.NORMAL, True,  30.0, "Square patrol"),
            BehaviorDescriptor("wag",           _wag,           Priority.NORMAL, True,  3.0,  "Tail-wag (wallow)"),
            BehaviorDescriptor("alert",         _alert,         Priority.HIGH,   True,  2.0,  "Alert posture"),
            BehaviorDescriptor("emergency_sit", _emergency_sit, Priority.SAFETY, False, 0.0,  "Hard-stop + damp"),
        ]:
            self.register(d)

    # ── Lifecycle ──────────────────────────────────────────────────────── #
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    # ── Scheduling ─────────────────────────────────────────────────────── #
    async def enqueue(self, name: str, params: dict | None = None,
                      priority: Priority | None = None) -> None:
        d = self._reg.get(name)
        if not d:
            raise ValueError(f"Unknown behavior '{name}'. Available: {sorted(self._reg)}")
        if d.cooldown_s > 0 and (time.monotonic() - d.last_run) < d.cooldown_s:
            remaining = d.cooldown_s - (time.monotonic() - d.last_run)
            logger.info("Behavior '%s' on cooldown (%.1fs)", name, remaining)
            return
        p = int(priority if priority is not None else d.priority)
        self._ctr += 1
        await self._queue.put((p, self._ctr, d, params or {}))

    # ── Introspection ──────────────────────────────────────────────────── #
    @property
    def current_behavior(self) -> Optional[str]:
        return self._current

    @property
    def available_behaviors(self) -> list[str]:
        return sorted(self._reg)

    @property
    def history(self) -> list[dict]:
        return list(self._history[-50:])

    # ── Loop ───────────────────────────────────────────────────────────── #
    async def _loop(self) -> None:
        while self._running:
            try:
                _, _ctr, d, params = await asyncio.wait_for(
                    self._queue.get(), timeout=self._tick_s
                )
                await self._execute(d, params)
            except asyncio.TimeoutError: pass
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error("BehaviorEngine error: %s", e, exc_info=True)

    async def _execute(self, d: BehaviorDescriptor, params: dict) -> None:
        state = await self._bridge.get_state()
        ctx = BehaviorContext(bridge=self._bridge, state=state, params=params)
        self._current = d.name
        t0 = time.monotonic()
        try:
            logger.info("Executing behavior '%s'", d.name)
            await d.fn(ctx)
            d.last_run = time.monotonic()
        except asyncio.CancelledError:
            ctx.interrupted = True
        except Exception as e:
            logger.error("Behavior '%s' error: %s", d.name, e, exc_info=True)
        finally:
            self._history.append({
                "behavior": d.name, "started_at": t0,
                "duration_s": time.monotonic() - t0,
                "interrupted": ctx.interrupted, "params": params,
            })
            if len(self._history) > 50:
                self._history = self._history[-50:]
            self._current = None


# ── Built-in behavior implementations ─────────────────────────────────── #
async def _idle(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")

async def _sit(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("sit")
    await asyncio.sleep(0.5)

async def _stand(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("stand_up")
    await asyncio.sleep(1.0)
    await ctx.bridge.set_mode("balance_stand")

async def _greet(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.3)
    await ctx.bridge.set_euler(0.15, 0.0, 0.2)
    await asyncio.sleep(0.6)
    await ctx.bridge.set_euler(0.0, 0.0, 0.0)
    await asyncio.sleep(0.2)
    await ctx.bridge.set_mode("hello")
    await asyncio.sleep(2.0)

async def _stretch(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.3)
    await ctx.bridge.set_mode("stretch")
    await asyncio.sleep(3.0)

async def _dance(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.3)
    await ctx.bridge.set_mode(random.choice(["dance1", "dance2"]))
    await asyncio.sleep(5.0)

async def _patrol(ctx: BehaviorContext) -> None:
    speed = ctx.params.get("speed", 0.3)
    turn  = ctx.params.get("turn_rate", 0.5)
    steps = ctx.params.get("steps", 4)
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.3)
    for _ in range(steps):
        if ctx.interrupted: break
        await ctx.bridge.move(speed, 0.0, 0.0)
        await asyncio.sleep(2.0)
        await ctx.bridge.move(0.0, 0.0, turn)
        await asyncio.sleep(1.5)
    await ctx.bridge.stop()

async def _wag(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await asyncio.sleep(0.2)
    await ctx.bridge.set_mode("wallow")
    await asyncio.sleep(3.0)

async def _alert(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("balance_stand")
    await ctx.bridge.set_body_height(0.46)
    await ctx.bridge.set_euler(-0.1, 0.0, 0.0)
    await asyncio.sleep(ctx.params.get("duration", 5.0))
    await ctx.bridge.set_body_height(0.38)
    await ctx.bridge.set_euler(0.0, 0.0, 0.0)

async def _emergency_sit(ctx: BehaviorContext) -> None:
    await ctx.bridge.emergency_stop()
    await asyncio.sleep(0.3)
    await ctx.bridge.set_mode("sit")
