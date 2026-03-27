"""
cerberus/core/cognitive.py
==========================
Cognitive Engine — three-layer decision architecture:

  Layer 1  Reactive      Immediate stimulus-response (obstacle, fall, battery)
  Layer 2  Deliberative  Goal-directed planning over short horizons
  Layer 3  Reflective    Mood / personality modulation of deliberative output

The engine runs as an async task at a configurable tick rate and emits
BehaviorEngine requests.  It never calls the hardware bridge directly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from cerberus.hardware.go2_bridge import RobotState
    from cerberus.behavior.engine import BehaviorEngine
    from cerberus.personality.model import PersonalityModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Attention / Goals
# ---------------------------------------------------------------------------

class GoalType(str, Enum):
    IDLE        = "idle"
    EXPLORE     = "explore"
    GREET       = "greet"
    PATROL      = "patrol"
    CHARGE      = "charge"    # seek charging dock (future)
    USER_CUSTOM = "user_custom"


@dataclass
class Goal:
    type:       GoalType
    priority:   int             = 50
    params:     dict            = field(default_factory=dict)
    created_at: float           = field(default_factory=time.monotonic)
    timeout_s:  float           = 60.0
    satisfied:  bool            = False


# ---------------------------------------------------------------------------
# Working memory (short-term context)
# ---------------------------------------------------------------------------

@dataclass
class WorkingMemory:
    """Holds the transient facts the cognitive engine reasons about."""
    last_human_seen:  float     = 0.0   # monotonic timestamp
    obstacle_ahead:   bool      = False
    battery_low:      bool      = False
    battery_critical: bool      = False
    tilt_alert:       bool      = False
    active_goal:      Optional[Goal] = None
    idle_since:       float     = field(default_factory=time.monotonic)
    interaction_count: int      = 0


# ---------------------------------------------------------------------------
# Cognitive Engine
# ---------------------------------------------------------------------------

class CognitiveEngine:
    """
    Pulls sensor state, updates working memory, selects and schedules
    behaviors on the BehaviorEngine.

    Reactive rules have veto power over deliberative goals.
    Personality modulates which deliberative behavior is chosen when
    multiple options have equal priority.
    """

    REACTIVE_TICK_HZ   = 20.0   # fast loop for safety
    DELIBERATE_TICK_HZ = 1.0    # slower goal-planning loop

    # Thresholds
    BATTERY_LOW_V      = 22.0
    BATTERY_CRIT_V     = 20.5
    TILT_WARN_RAD      = 0.35
    IDLE_GREET_AFTER_S = 30.0   # greet after idle for this long
    IDLE_STRETCH_AFTER_S = 120.0

    def __init__(self,
                 behavior_engine: "BehaviorEngine",
                 personality: Optional["PersonalityModel"] = None) -> None:
        self._beh     = behavior_engine
        self._pers    = personality
        self._mem     = WorkingMemory()
        self._goals:  list[Goal] = [Goal(GoalType.IDLE)]

        self._reactive_task:   Optional[asyncio.Task] = None
        self._deliberate_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._reactive_task   = asyncio.create_task(self._reactive_loop())
        self._deliberate_task = asyncio.create_task(self._deliberate_loop())
        logger.info("CognitiveEngine started")

    async def stop(self) -> None:
        self._running = False
        for t in (self._reactive_task, self._deliberate_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # External stimuli
    # ------------------------------------------------------------------

    def notify_human_detected(self) -> None:
        self._mem.last_human_seen = time.monotonic()
        self._mem.interaction_count += 1

    def notify_obstacle(self, present: bool) -> None:
        self._mem.obstacle_ahead = present

    def set_goal(self, goal: Goal) -> None:
        self._goals.insert(0, goal)
        self._goals.sort(key=lambda g: g.priority)

    def update_from_state(self, state: "RobotState") -> None:
        import math
        self._mem.battery_low      = state.battery_voltage < self.BATTERY_LOW_V
        self._mem.battery_critical = state.battery_voltage < self.BATTERY_CRIT_V
        tilt = math.sqrt(state.pitch**2 + state.roll**2)
        self._mem.tilt_alert = tilt > self.TILT_WARN_RAD

    # ------------------------------------------------------------------
    # Reactive loop (fast — safety override)
    # ------------------------------------------------------------------

    async def _reactive_loop(self) -> None:
        interval = 1.0 / self.REACTIVE_TICK_HZ
        while self._running:
            try:
                # Battery critical → emergency sit
                if self._mem.battery_critical:
                    logger.warning("Cognitive reactive: battery critical → emergency sit")
                    await self._beh.enqueue("emergency_sit")
                    await asyncio.sleep(5.0)   # debounce
                    continue

                # Tilt alert → stop and balance
                if self._mem.tilt_alert:
                    await self._beh.enqueue("idle")

            except Exception as exc:
                logger.error("Reactive loop error: %s", exc)
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Deliberative loop (slow — goal selection)
    # ------------------------------------------------------------------

    async def _deliberate_loop(self) -> None:
        interval = 1.0 / self.DELIBERATE_TICK_HZ
        while self._running:
            try:
                await self._deliberate_tick()
            except Exception as exc:
                logger.error("Deliberate loop error: %s", exc)
            await asyncio.sleep(interval)

    async def _deliberate_tick(self) -> None:
        # Expire timed-out goals
        now = time.monotonic()
        self._goals = [
            g for g in self._goals
            if not g.satisfied and (now - g.created_at) < g.timeout_s
        ]
        if not self._goals:
            self._goals.append(Goal(GoalType.IDLE))

        goal = self._goals[0]
        self._mem.active_goal = goal
        idle_s = now - self._mem.idle_since

        # Personality modulation: sociable dogs greet sooner
        greet_threshold = self.IDLE_GREET_AFTER_S
        if self._pers:
            # More sociable → lower threshold
            greet_threshold *= max(0.3, 1.2 - self._pers.sociability)

        match goal.type:
            case GoalType.IDLE:
                # After idle for a while, do autonomous behaviors
                if idle_s > self.IDLE_STRETCH_AFTER_S:
                    await self._beh.enqueue("stretch")
                    self._mem.idle_since = now
                elif idle_s > greet_threshold and self._mem.interaction_count > 0:
                    await self._beh.enqueue("greet")
                    self._mem.idle_since = now
                else:
                    await self._beh.enqueue("idle")

            case GoalType.EXPLORE:
                await self._beh.enqueue("patrol", goal.params)

            case GoalType.GREET:
                await self._beh.enqueue("greet")
                goal.satisfied = True

            case GoalType.PATROL:
                await self._beh.enqueue("patrol", goal.params)

            case GoalType.USER_CUSTOM:
                beh_name = goal.params.get("behavior")
                if beh_name:
                    await self._beh.enqueue(beh_name, goal.params)
                goal.satisfied = True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def memory(self) -> WorkingMemory:
        return self._mem

    @property
    def active_goal(self) -> Optional[Goal]:
        return self._mem.active_goal
