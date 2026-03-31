"""
cerberus/cognitive/behavior_engine.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Cognitive / Behavior Engine

Three-layer architecture:
  Layer 1 — Reactive:     Immediate, reflex-like responses (obstacle → swerve)
  Layer 2 — Deliberative: Goal-oriented planning (explore → navigate → return)
  Layer 3 — Reflective:   Self-evaluation, personality adaptation

Behavior tree nodes:
  Selector  — try children in order, succeed on first success (fallback)
  Sequence  — run children in order, fail on first failure
  Condition — evaluate a predicate
  Action    — execute a behavior

Personality traits (Big Five inspired, mapped to canine behavior):
  energy        — how active the robot is
  friendliness  — how much it initiates interaction
  curiosity     — how much it explores novel stimuli
  loyalty       — how closely it follows the user
  playfulness   — frequency of play behaviors
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.bridge.go2_bridge import BridgeBase, RobotState

logger = logging.getLogger(__name__)


# ── Personality ───────────────────────────────────────────────────────────────

@dataclass
class PersonalityTraits:
    energy:       float = 0.7   # 0.0 = lethargic, 1.0 = hyperactive
    friendliness: float = 0.8
    curiosity:    float = 0.6
    loyalty:      float = 0.9
    playfulness:  float = 0.65

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def modulate(self, mood: "MoodState") -> "PersonalityTraits":
        """Return a copy adjusted by current mood."""
        factor = 1.0 + mood.valence * 0.2
        return PersonalityTraits(
            energy       = min(1.0, self.energy       * factor),
            friendliness = min(1.0, self.friendliness * factor),
            curiosity    = min(1.0, self.curiosity    * (1.0 + mood.arousal * 0.15)),
            loyalty      = self.loyalty,
            playfulness  = min(1.0, self.playfulness  * factor),
        )


class MoodState(str, Enum):
    CALM      = "calm"
    HAPPY     = "happy"
    EXCITED   = "excited"
    CURIOUS   = "curious"
    TIRED     = "tired"
    ALERT     = "alert"
    BORED     = "bored"

    @property
    def valence(self) -> float:  # -1 = negative, +1 = positive
        return {
            "calm": 0.2, "happy": 0.9, "excited": 0.7, "curious": 0.5,
            "tired": -0.3, "alert": 0.1, "bored": -0.5,
        }[self.value]

    @property
    def arousal(self) -> float:  # 0 = low energy, 1 = high energy
        return {
            "calm": 0.2, "happy": 0.6, "excited": 0.95, "curious": 0.7,
            "tired": 0.1, "alert": 0.85, "bored": 0.25,
        }[self.value]


# ── Behavior tree nodes ───────────────────────────────────────────────────────

class BTStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    RUNNING = "running"


class BTNode:
    name: str = "node"

    async def tick(self, context: dict) -> BTStatus:
        raise NotImplementedError


class Sequence(BTNode):
    """All children must succeed."""
    def __init__(self, name: str, children: list[BTNode]):
        self.name = name
        self.children = children

    async def tick(self, ctx: dict) -> BTStatus:
        for child in self.children:
            status = await child.tick(ctx)
            if status != BTStatus.SUCCESS:
                return status
        return BTStatus.SUCCESS


class Selector(BTNode):
    """First successful child wins."""
    def __init__(self, name: str, children: list[BTNode]):
        self.name = name
        self.children = children

    async def tick(self, ctx: dict) -> BTStatus:
        for child in self.children:
            status = await child.tick(ctx)
            if status != BTStatus.FAILURE:
                return status
        return BTStatus.FAILURE


class Condition(BTNode):
    def __init__(self, name: str, predicate: Callable[[dict], bool]):
        self.name = name
        self.predicate = predicate

    async def tick(self, ctx: dict) -> BTStatus:
        return BTStatus.SUCCESS if self.predicate(ctx) else BTStatus.FAILURE


class Action(BTNode):
    def __init__(self, name: str, fn: Callable[[dict], Any]):
        self.name = name
        self.fn = fn

    async def tick(self, ctx: dict) -> BTStatus:
        try:
            result = self.fn(ctx)
            if asyncio.iscoroutine(result):
                result = await result
            return BTStatus.SUCCESS if result is not False else BTStatus.FAILURE
        except Exception as e:
            logger.error("Action '%s' error: %s", self.name, e)
            return BTStatus.FAILURE


# ── Working memory ────────────────────────────────────────────────────────────

class WorkingMemory:
    """Short-term key-value store for behavioral context."""
    def __init__(self, capacity: int = 256):
        self._store: dict[str, tuple[Any, float]] = {}
        self._capacity = capacity

    def set(self, key: str, value: Any, ttl_s: float = 30.0) -> None:
        if len(self._store) >= self._capacity:
            # Evict oldest
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]
        self._store[key] = (value, time.monotonic() + ttl_s)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return default
        value, expires = entry
        if time.monotonic() > expires:
            del self._store[key]
            return default
        return value

    def clear(self) -> None:
        self._store.clear()

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {k: v for k, (v, exp) in self._store.items() if exp > now}


# ── Goal system ───────────────────────────────────────────────────────────────

@dataclass
class Goal:
    name: str
    priority: float = 0.5  # 0.0–1.0
    params: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    deadline: float | None = None

    def is_expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline


class GoalQueue:
    def __init__(self):
        self._goals: list[Goal] = []

    def push(self, goal: Goal) -> None:
        self._goals.append(goal)
        self._goals.sort(key=lambda g: g.priority, reverse=True)

    def pop(self) -> Goal | None:
        # Remove expired goals first
        self._goals = [g for g in self._goals if not g.is_expired()]
        return self._goals.pop(0) if self._goals else None

    def peek(self) -> Goal | None:
        self._goals = [g for g in self._goals if not g.is_expired()]
        return self._goals[0] if self._goals else None

    def clear(self) -> None:
        self._goals.clear()

    def to_list(self) -> list[dict]:
        return [{"name": g.name, "priority": g.priority, "params": g.params} for g in self._goals]


# ── Main Behavior Engine ──────────────────────────────────────────────────────

class BehaviorEngine:
    """
    Three-layer behavior engine for CERBERUS.

    Attach to CerberusEngine:
        engine.behavior_engine = BehaviorEngine(bridge, personality)
    """

    def __init__(
        self,
        bridge: "BridgeBase",
        personality: PersonalityTraits | None = None,
    ):
        self.bridge      = bridge
        self.personality = personality or PersonalityTraits()
        self.mood        = MoodState.CALM
        self.memory      = WorkingMemory()
        self.goals       = GoalQueue()

        self._tick_count = 0
        self._last_interaction = time.monotonic()
        self._boredom_timer    = time.monotonic()
        self._active_behavior  = "idle"

        # Session statistics — accumulated this engine run, saved by SessionStore
        from cerberus.cognitive.session_store import SessionStats
        self._session_stats = SessionStats()

        # Build behavior tree
        self._tree = self._build_tree()

    def _build_tree(self) -> BTNode:
        """Construct the default canine behavior tree."""
        bridge = self.bridge

        # ── Reactive layer (Layer 1) ──────────────────────────────────────────
        estop_check = Condition(
            "estop_clear",
            lambda ctx: not ctx.get("estop_active", False)
        )
        obstacle_avoid = Selector("obstacle_avoid", [
            Condition("no_obstacle", lambda ctx: not ctx.get("obstacle_near", False)),
            Action("stop_for_obstacle", lambda ctx: asyncio.ensure_future(bridge.stop_move())),
        ])

        reactive = Sequence("reactive_layer", [estop_check, obstacle_avoid])

        # ── Deliberative layer (Layer 2) ──────────────────────────────────────
        from cerberus.bridge.go2_bridge import SportMode as _SportMode

        # Goal-driven sub-tree: consume the highest-priority pending goal.
        # Goals are dispatched by name → action mapping. Unknown goal names
        # are logged and popped so they don't block the queue forever.
        goal_dispatch = Sequence("goal_dispatch", [
            Condition("has_goal", lambda ctx: ctx.get("pending_goal") is not None),
            Action("execute_goal", lambda ctx: asyncio.ensure_future(
                self._execute_goal(ctx["pending_goal"])
            )),
        ])

        greet_human = Sequence("greet_human", [
            Condition("human_detected", lambda ctx: ctx.get("human_detected", False)),
            Condition("not_greeted_recently", lambda ctx: ctx.get("last_greet_elapsed", 999) > 30),
            Action("hello_wave", lambda ctx: asyncio.ensure_future(bridge.execute_sport_mode(
                _SportMode.HELLO
            ))),
        ])

        explore_idle = Selector("explore_or_idle", [
            Sequence("explore", [
                Condition("should_explore", lambda ctx: ctx.get("curiosity", 0) > 0.5 and
                          ctx.get("uptime_min", 0) % 5 < 1),
                Action("start_explore", lambda ctx: self._set_behavior("exploring")),
            ]),
            Action("idle_stand", lambda ctx: None),
        ])

        # Goal dispatch has highest priority in the deliberative layer —
        # it runs before greet and explore so operator commands preempt
        # autonomous social behavior.
        deliberative = Selector("deliberative_layer", [
            goal_dispatch,
            greet_human,
            explore_idle,
        ])

        # ── Reflective layer (Layer 3) ────────────────────────────────────────
        boredom_check = Selector("boredom", [
            Condition("not_bored", lambda ctx: ctx.get("boredom_level", 0) < 0.7),
            Action("play_behavior", lambda ctx: asyncio.ensure_future(self._play_behavior())),
        ])

        reflective = Sequence("reflective_layer", [boredom_check])

        return Selector("root", [reactive, deliberative, reflective])

    async def step(self, tick: int) -> None:
        """Called once per engine tick."""
        self._tick_count = tick
        state = await self.bridge.get_state()

        # Build behavior context
        now = time.monotonic()
        ctx = {
            "tick": tick,
            "estop_active": state.estop_active,
            "battery_pct": state.battery_percent,
            "mode": state.mode,
            "human_detected": self.memory.get("human_detected", False),
            "obstacle_near":  self.memory.get("obstacle_near", False),
            "last_greet_elapsed": now - self.memory.get("last_greet_time", 0),
            "curiosity": self.personality.curiosity,
            "boredom_level": self._compute_boredom(now),
            "uptime_min": (now - self._boredom_timer) / 60.0,
            "active_behavior": self._active_behavior,
            # Expose the highest-priority pending goal (if any) to the BT.
            # peek() removes expired goals but does NOT pop — the goal persists
            # until _execute_goal() explicitly calls self.goals.pop().
            "pending_goal": self.goals.peek(),
        }

        # Run behavior tree
        status = await self._tree.tick(ctx)

        # Update mood every 10 ticks
        if tick % 10 == 0:
            self._update_mood(state, ctx)

        logger.debug("BT tick %d → %s (mood=%s)", tick, status.value, self.mood.value)

    def _compute_boredom(self, now: float) -> float:
        """Boredom increases over time without novel stimuli."""
        elapsed = now - self._boredom_timer
        base = min(1.0, elapsed / 300.0)  # max bored after 5 min
        return base * (1.0 - self.personality.playfulness * 0.3)

    def _update_mood(self, state: "RobotState", ctx: dict) -> None:
        """Transition mood based on context."""
        battery = state.battery_percent
        if battery < 10:
            self.mood = MoodState.TIRED
        elif ctx.get("human_detected"):
            self.mood = MoodState.HAPPY if self.personality.friendliness > 0.6 else MoodState.ALERT
        elif ctx.get("obstacle_near"):
            self.mood = MoodState.ALERT
        elif ctx.get("boredom_level", 0) > 0.7:
            self.mood = MoodState.BORED
        elif self._active_behavior == "exploring":
            self.mood = MoodState.CURIOUS
        else:
            self.mood = MoodState.CALM
        logger.debug("Mood → %s", self.mood.value)

    def _set_behavior(self, name: str) -> None:
        if self._active_behavior != name:
            logger.info("Behavior transition: %s → %s", self._active_behavior, name)
            self._active_behavior = name
            self._boredom_timer = time.monotonic()
        if name == "exploring":
            self._session_stats.explore_ticks += 1

    async def _execute_goal(self, goal: Goal) -> None:
        """
        Execute a goal by name and pop it from the queue.

        Supported goal names map to bridge or sport mode commands.
        Unknown names are logged and popped to prevent queue starvation.

        Goal params (optional, passed as kwargs to push_goal):
          move:       vx, vy, vyaw  (floats)
          move_timed: vx, vy, vyaw, duration_s (float)
          height:     offset (float, relative m)
        """
        from cerberus.bridge.go2_bridge import SportMode

        name = goal.name.lower().strip()
        logger.info("Executing goal: '%s' (priority=%.2f)", name, goal.priority)
        self._set_behavior(f"goal:{name}")

        SPORT_MODE_GOALS = {
            "sit": SportMode.SIT,
            "stand_up": SportMode.STAND_UP,
            "stand_down": SportMode.STAND_DOWN,
            "hello": SportMode.HELLO,
            "stretch": SportMode.STRETCH,
            "dance": SportMode.DANCE1,
            "dance1": SportMode.DANCE1,
            "dance2": SportMode.DANCE2,
            "wallow": SportMode.WALLOW,
            "scrape": SportMode.SCRAPE,
            "front_flip": SportMode.FRONT_FLIP,
            "front_jump": SportMode.FRONT_JUMP,
            "pounce": SportMode.FRONT_POUNCE,
            "finger_heart": SportMode.FINGER_HEART,
            "balance_stand": SportMode.BALANCE_STAND,
            "rise_sit": SportMode.RISE_SIT,
        }

        try:
            if name in SPORT_MODE_GOALS:
                await self.bridge.execute_sport_mode(SPORT_MODE_GOALS[name])

            elif name == "stop":
                await self.bridge.stop_move()

            elif name == "move":
                vx   = float(goal.params.get("vx", 0.0))
                vy   = float(goal.params.get("vy", 0.0))
                vyaw = float(goal.params.get("vyaw", 0.0))
                await self.bridge.move(vx, vy, vyaw)

            elif name == "move_timed":
                vx       = float(goal.params.get("vx", 0.0))
                vy       = float(goal.params.get("vy", 0.0))
                vyaw     = float(goal.params.get("vyaw", 0.0))
                duration = float(goal.params.get("duration_s", 2.0))
                await self.bridge.move(vx, vy, vyaw)
                await asyncio.sleep(max(0.1, min(30.0, duration)))
                await self.bridge.stop_move()

            elif name == "height":
                offset = float(goal.params.get("offset", 0.0))
                await self.bridge.set_body_height(offset)

            elif name == "explore":
                self._set_behavior("exploring")

            else:
                logger.warning("Unknown goal name '%s' — popping to avoid starvation", name)

        except Exception as exc:
            logger.error("Goal '%s' execution error: %s", name, exc)
        finally:
            # Always pop regardless of outcome so we don't get stuck
            self.goals.pop()
            self._set_behavior("idle")
            self._session_stats.goals_completed += 1

    async def _play_behavior(self) -> None:
        from cerberus.bridge.go2_bridge import SportMode
        play_modes = [SportMode.STRETCH, SportMode.DANCE1, SportMode.HELLO, SportMode.WALLOW]
        chosen = random.choice(play_modes)
        logger.info("Play behavior: %s", chosen.value)
        await self.bridge.execute_sport_mode(chosen)
        self._boredom_timer = time.monotonic()
        self._session_stats.play_behaviors += 1

    # ── External perception input ─────────────────────────────────────────────

    def on_human_detected(self, detected: bool) -> None:
        self.memory.set("human_detected", detected, ttl_s=5.0)
        if detected:
            self.memory.set("last_greet_time", time.monotonic(), ttl_s=60.0)
            self._session_stats.human_interactions += 1

    def on_obstacle_detected(self, detected: bool) -> None:
        self.memory.set("obstacle_near", detected, ttl_s=2.0)

    def push_goal(self, name: str, priority: float = 0.5, **params) -> None:
        self.goals.push(Goal(name=name, priority=priority, params=params))

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "active_behavior": self._active_behavior,
            "mood": self.mood.value,
            "personality": self.personality.to_dict(),
            "memory": self.memory.snapshot(),
            "goal_queue": self.goals.to_list(),
            "tick_count": self._tick_count,
        }
