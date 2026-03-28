"""
cerberus/core/cognitive.py  — CERBERUS v3.1
============================================
Three-layer cognitive decision system:
  Layer 1 – Reactive      20 Hz safety loop (battery, tilt)
  Layer 2 – Deliberative   1 Hz goal planner
  Layer 3 – Reflective     personality modulates goal thresholds
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cerberus.hardware.bridge import RobotState
    from cerberus.behavior.engine import BehaviorEngine
    from cerberus.personality.model import PersonalityModel

logger = logging.getLogger(__name__)


class GoalType(str, Enum):
    IDLE = "idle"; EXPLORE = "explore"; GREET = "greet"
    PATROL = "patrol"; USER_CUSTOM = "user_custom"


@dataclass
class Goal:
    type:      GoalType
    priority:  int   = 50
    params:    dict  = field(default_factory=dict)
    created:   float = field(default_factory=time.monotonic)
    timeout_s: float = 60.0
    satisfied: bool  = False


@dataclass
class WorkingMemory:
    last_human_seen:   float = 0.0
    obstacle_ahead:    bool  = False
    battery_low:       bool  = False
    battery_critical:  bool  = False
    tilt_alert:        bool  = False
    active_goal:       Optional[Goal] = None
    idle_since:        float = field(default_factory=time.monotonic)
    interaction_count: int   = 0


class CognitiveEngine:
    REACTIVE_HZ   = 20.0
    DELIBERATE_HZ = 1.0
    BATTERY_LOW_V = 22.0
    BATTERY_CRIT_V = 20.5
    TILT_WARN_RAD  = 0.35
    IDLE_GREET_S   = 30.0
    IDLE_STRETCH_S = 120.0

    def __init__(self, behavior: "BehaviorEngine",
                 personality: Optional["PersonalityModel"] = None) -> None:
        self._beh  = behavior
        self._pers = personality
        self._mem  = WorkingMemory()
        self._goals: list[Goal] = [Goal(GoalType.IDLE)]
        self._running = False
        self._r_task: Optional[asyncio.Task] = None
        self._d_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._r_task = asyncio.create_task(self._reactive_loop())
        self._d_task = asyncio.create_task(self._deliberate_loop())
        logger.info("CognitiveEngine started")

    async def stop(self) -> None:
        self._running = False
        for t in (self._r_task, self._d_task):
            if t:
                t.cancel()
                try: await t
                except asyncio.CancelledError: pass

    # ── External stimuli ──────────────────────────────────────────────── #
    def notify_human(self)              -> None: self._mem.last_human_seen = time.monotonic(); self._mem.interaction_count += 1
    def notify_obstacle(self, on: bool) -> None: self._mem.obstacle_ahead = on
    def set_goal(self, g: Goal)         -> None: self._goals.insert(0, g); self._goals.sort(key=lambda x: x.priority)

    def update_state(self, s: "RobotState") -> None:
        self._mem.battery_low      = 0 < s.battery_voltage < self.BATTERY_LOW_V
        self._mem.battery_critical = 0 < s.battery_voltage < self.BATTERY_CRIT_V
        self._mem.tilt_alert       = math.sqrt(s.pitch**2 + s.roll**2) > self.TILT_WARN_RAD

    # ── Reactive loop ─────────────────────────────────────────────────── #
    async def _reactive_loop(self) -> None:
        iv = 1.0 / self.REACTIVE_HZ
        while self._running:
            try:
                if self._mem.battery_critical:
                    logger.warning("Cognitive: battery critical → emergency sit")
                    await self._beh.enqueue("emergency_sit")
                    await asyncio.sleep(5.0)
                elif self._mem.tilt_alert:
                    await self._beh.enqueue("idle")
            except Exception as e:
                logger.error("Reactive loop: %s", e)
            await asyncio.sleep(iv)

    # ── Deliberative loop ─────────────────────────────────────────────── #
    async def _deliberate_loop(self) -> None:
        iv = 1.0 / self.DELIBERATE_HZ
        while self._running:
            try:
                await self._deliberate_tick()
            except Exception as e:
                logger.error("Deliberate loop: %s", e)
            await asyncio.sleep(iv)

    async def _deliberate_tick(self) -> None:
        now = time.monotonic()
        self._goals = [g for g in self._goals
                       if not g.satisfied and (now - g.created) < g.timeout_s]
        if not self._goals:
            self._goals.append(Goal(GoalType.IDLE))

        goal = self._goals[0]
        self._mem.active_goal = goal
        idle_s = now - self._mem.idle_since
        greet_s = self.IDLE_GREET_S * max(0.3, 1.2 - (self._pers.sociability if self._pers else 0.7))

        match goal.type:
            case GoalType.IDLE:
                if idle_s > self.IDLE_STRETCH_S:
                    await self._beh.enqueue("stretch"); self._mem.idle_since = now
                elif idle_s > greet_s and self._mem.interaction_count > 0:
                    await self._beh.enqueue("greet"); self._mem.idle_since = now
                else:
                    await self._beh.enqueue("idle")
            case GoalType.EXPLORE:
                await self._beh.enqueue("patrol", goal.params)
            case GoalType.GREET:
                await self._beh.enqueue("greet"); goal.satisfied = True
            case GoalType.PATROL:
                await self._beh.enqueue("patrol", goal.params)
            case GoalType.USER_CUSTOM:
                if beh := goal.params.get("behavior"):
                    await self._beh.enqueue(beh, goal.params)
                goal.satisfied = True

    @property
    def memory(self) -> WorkingMemory:
        return self._mem
