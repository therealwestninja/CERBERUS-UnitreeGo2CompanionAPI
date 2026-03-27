"""
cerberus/cognitive/mind.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Cognitive Engine — "The Mind"

Three-layer cognitive architecture:
  Reactive Layer    — stimulus-response, <50ms, reflex behaviors
  Deliberative Layer — goal-directed planning, BT execution, 100ms horizon
  Reflective Layer  — introspection, preference learning, long-term adaptation

Memory systems:
  WorkingMemory     — volatile, capacity-limited, recency-weighted (~7 items)
  EpisodicMemory    — timestamped event history, retrievable by recency/relevance
  SemanticMemory    — named facts, object associations, world knowledge
  ProceduralMemory  — learned action sequences, skill library

Goal engine:
  GoalStack         — prioritized goal hierarchy with interruption
  AttentionSystem   — filters perceptual input to focus on relevant stimuli
  CognitivePlanner  — simple forward-chaining planner for action sequences

Integration:
  - CognitiveMind subscribes to EventBus for perceptual events
  - Drives BehaviorTreeRunner via goal commands
  - Updates personality/mood via affective events
  - Feeds LearningSystem with experience data
"""

import asyncio
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.mind')


# ════════════════════════════════════════════════════════════════════════════
# MEMORY SYSTEMS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MemoryItem:
    """A single item in working memory."""
    id:         str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    content:    Any = None
    source:     str = ''
    importance: float = 1.0       # [0, 1] — higher = less likely to be displaced
    created_at: float = field(default_factory=time.monotonic)
    accessed_at: float = field(default_factory=time.monotonic)
    access_count: int = 0

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def recency_score(self) -> float:
        """Higher = more recently accessed. Exponential decay."""
        return self.importance * math.exp(-0.1 * (time.monotonic() - self.accessed_at))

    def access(self):
        self.accessed_at = time.monotonic()
        self.access_count += 1

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'source': self.source,
            'importance': round(self.importance, 2),
            'age_s': round(self.age_s, 1),
            'access_count': self.access_count,
            'content_type': type(self.content).__name__,
        }


class WorkingMemory:
    """
    Capacity-limited, recency-weighted working memory (~Miller's 7±2).
    When capacity is exceeded, least-important items are displaced.
    Models the robot's 'current focus of attention'.
    """

    DEFAULT_CAPACITY = 9

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = capacity
        self._items:   List[MemoryItem] = []

    def store(self, content: Any, source: str = '',
              importance: float = 1.0) -> str:
        """Store an item. Displaces least important if at capacity."""
        item = MemoryItem(content=content, source=source, importance=importance)
        if len(self._items) >= self._capacity:
            # Displace item with lowest recency score
            self._items.sort(key=lambda x: x.recency_score)
            displaced = self._items.pop(0)
            log.debug('WorkingMemory displaced: %s (score=%.3f)',
                      displaced.id, displaced.recency_score)
        self._items.append(item)
        return item.id

    def retrieve(self, item_id: str) -> Optional[Any]:
        for item in self._items:
            if item.id == item_id:
                item.access()
                return item.content
        return None

    def retrieve_by_source(self, source: str) -> List[Any]:
        results = [i for i in self._items if i.source == source]
        for i in results: i.access()
        return [i.content for i in results]

    def most_salient(self, n: int = 3) -> List[Any]:
        """Return top-N items by recency score."""
        sorted_items = sorted(self._items, key=lambda x: x.recency_score, reverse=True)
        for i in sorted_items[:n]: i.access()
        return [i.content for i in sorted_items[:n]]

    def clear_source(self, source: str):
        self._items = [i for i in self._items if i.source != source]

    def snapshot(self) -> List[dict]:
        return [i.to_dict() for i in sorted(self._items,
                key=lambda x: x.recency_score, reverse=True)]


@dataclass
class Episode:
    """A single episode in episodic memory."""
    id:          str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_type:  str = ''
    content:     Any = None
    context:     Dict[str, Any] = field(default_factory=dict)
    emotion_tag: str = ''            # joy / fear / curiosity / surprise / boredom
    valence:     float = 0.0         # [-1, 1] positive/negative
    created_at:  float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'event_type': self.event_type,
            'emotion_tag': self.emotion_tag, 'valence': round(self.valence, 2),
            'created_at': self.created_at,
            'context_keys': list(self.context.keys()),
        }


class EpisodicMemory:
    """
    Timestamped episodic event history.
    Retrieval by recency, event type, or emotional valence.
    Models the robot's autobiographical memory.
    """

    def __init__(self, max_episodes: int = 5000):
        self._episodes: deque = deque(maxlen=max_episodes)
        self._index:    Dict[str, List[Episode]] = {}  # event_type → episodes

    def record(self, event_type: str, content: Any,
               context: Optional[dict] = None,
               emotion: str = '', valence: float = 0.0) -> str:
        ep = Episode(event_type=event_type, content=content,
                     context=context or {}, emotion_tag=emotion, valence=valence)
        self._episodes.append(ep)
        self._index.setdefault(event_type, []).append(ep)
        if len(self._index[event_type]) > 500:
            self._index[event_type] = self._index[event_type][-500:]
        return ep.id

    def recall_recent(self, n: int = 20,
                       event_type: Optional[str] = None) -> List[dict]:
        """Return most recent episodes, optionally filtered by type."""
        if event_type:
            eps = list(self._index.get(event_type, []))[-n:]
        else:
            eps = list(self._episodes)[-n:]
        return [e.to_dict() for e in reversed(eps)]

    def recall_emotional(self, valence_min: float = 0.5,
                          n: int = 10) -> List[dict]:
        """Return episodes with strong positive valence (positive experiences)."""
        positive = [e for e in self._episodes if e.valence >= valence_min]
        return [e.to_dict() for e in sorted(positive,
                key=lambda x: x.valence, reverse=True)[:n]]

    def count_by_type(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self._index.items()}

    def stats(self) -> dict:
        return {
            'total_episodes': len(self._episodes),
            'event_types':    self.count_by_type(),
            'positive_pct':   round(100 * sum(1 for e in self._episodes if e.valence > 0)
                                    / max(len(self._episodes), 1), 1),
        }


class SemanticMemory:
    """
    Named facts and associations about the world.
    Key-value store with confidence scores and source attribution.
    Models the robot's 'world knowledge'.
    """

    @dataclass
    class Fact:
        key:        str
        value:      Any
        confidence: float = 1.0
        source:     str   = ''
        updated_at: float = field(default_factory=time.time)

    def __init__(self):
        self._facts: Dict[str, 'SemanticMemory.Fact'] = {}

    def learn(self, key: str, value: Any, confidence: float = 1.0,
              source: str = ''):
        self._facts[key] = self.Fact(key=key, value=value,
                                      confidence=confidence, source=source)

    def know(self, key: str) -> Tuple[Optional[Any], float]:
        """Returns (value, confidence). confidence=0 if unknown."""
        f = self._facts.get(key)
        return (f.value, f.confidence) if f else (None, 0.0)

    def forget(self, key: str): self._facts.pop(key, None)

    def all_facts(self, min_confidence: float = 0.0) -> Dict[str, Any]:
        return {k: f.value for k, f in self._facts.items()
                if f.confidence >= min_confidence}


# ════════════════════════════════════════════════════════════════════════════
# GOAL ENGINE
# ════════════════════════════════════════════════════════════════════════════

class GoalStatus(Enum):
    PENDING   = 'pending'
    ACTIVE    = 'active'
    SUSPENDED = 'suspended'
    COMPLETE  = 'complete'
    FAILED    = 'failed'
    CANCELLED = 'cancelled'


@dataclass
class Goal:
    """A single goal with priority, preconditions, and success criteria."""
    id:           str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name:         str = ''
    type:         str = ''           # explore / interact / patrol / express / rest
    priority:     float = 0.5        # [0, 1] higher = more urgent
    preconditions: List[str] = field(default_factory=list)  # blackboard keys
    params:       Dict[str, Any] = field(default_factory=dict)
    status:       GoalStatus = GoalStatus.PENDING
    created_at:   float = field(default_factory=time.time)
    deadline:     Optional[float] = None   # wall time deadline (None = no deadline)
    parent_id:    Optional[str] = None     # for hierarchical goals

    @property
    def is_expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline

    @property
    def urgency(self) -> float:
        """Priority boosted by proximity to deadline."""
        if self.deadline is None:
            return self.priority
        time_left = max(0, self.deadline - time.time())
        urgency_boost = max(0, 1.0 - time_left / 60.0)  # boost in final minute
        return min(1.0, self.priority + urgency_boost * 0.3)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'name': self.name, 'type': self.type,
            'priority': round(self.priority, 2), 'urgency': round(self.urgency, 2),
            'status': self.status.value, 'is_expired': self.is_expired,
            'params': self.params,
        }


class GoalStack:
    """
    Prioritized goal hierarchy with preemption.
    Goals are selected by urgency; active goal can be suspended by
    higher-urgency incoming goals.
    """

    def __init__(self, bus: SystemEventBus):
        self._bus    = bus
        self._goals: List[Goal] = []
        self._active: Optional[Goal] = None
        self._history: deque = deque(maxlen=200)

    async def push(self, goal: Goal) -> str:
        """Add goal. Preempts active goal if higher urgency."""
        self._goals.append(goal)
        self._goals.sort(key=lambda g: g.urgency, reverse=True)
        log.info('Goal pushed: %s [priority=%.2f]', goal.name, goal.priority)
        await self._bus.emit('goal.pushed', goal.to_dict(), 'goal_stack')

        # Activate immediately if nothing running
        if self._active is None:
            await self._activate_next()
        # Preempt if new goal is significantly more urgent
        elif goal.urgency > self._active.urgency + 0.2:
            await self._suspend_active()
            await self._activate_next()

        return goal.id

    async def _activate_next(self):
        pending = [g for g in self._goals if g.status == GoalStatus.PENDING]
        if not pending: return
        goal = pending[0]
        goal.status  = GoalStatus.ACTIVE
        self._active = goal
        log.info('Goal activated: %s', goal.name)
        await self._bus.emit('goal.activated', goal.to_dict(), 'goal_stack')

    async def _suspend_active(self):
        if not self._active: return
        self._active.status = GoalStatus.SUSPENDED
        log.debug('Goal suspended: %s', self._active.name)
        await self._bus.emit('goal.suspended', self._active.to_dict(), 'goal_stack')
        self._active = None

    async def complete_active(self, success: bool = True):
        if not self._active: return
        self._active.status = GoalStatus.COMPLETE if success else GoalStatus.FAILED
        self._history.append(self._active.to_dict())
        log.info('Goal %s: %s', 'complete' if success else 'failed', self._active.name)
        await self._bus.emit('goal.complete' if success else 'goal.failed',
                             self._active.to_dict(), 'goal_stack')
        self._goals = [g for g in self._goals if g.id != self._active.id]
        self._active = None
        # Resume highest-priority suspended goal or activate next pending
        suspended = [g for g in self._goals if g.status == GoalStatus.SUSPENDED]
        if suspended:
            suspended[0].status = GoalStatus.ACTIVE
            self._active = suspended[0]
        else:
            await self._activate_next()

    @property
    def active(self) -> Optional[Goal]:
        return self._active

    def pending(self) -> List[Goal]:
        return [g for g in self._goals if g.status == GoalStatus.PENDING]

    def status_dict(self) -> dict:
        return {
            'active': self._active.to_dict() if self._active else None,
            'pending': [g.to_dict() for g in self.pending()],
            'history_count': len(self._history),
        }


# ════════════════════════════════════════════════════════════════════════════
# ATTENTION SYSTEM
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class AttentionTarget:
    """Something the robot is currently paying attention to."""
    target_id:   str
    target_type: str               # person / object / sound / event
    salience:    float = 1.0       # how attention-grabbing [0, 1]
    novelty:     float = 0.0       # how novel (decays over time)
    first_seen:  float = field(default_factory=time.monotonic)
    last_seen:   float = field(default_factory=time.monotonic)
    look_count:  int   = 0


class AttentionSystem:
    """
    Models selective attention — what the robot is 'looking at' or 'thinking about'.
    Prioritizes novel, salient, or goal-relevant stimuli.
    Drives head orientation and body language.
    """

    NOVELTY_DECAY_S = 30.0   # novelty halves every 30s
    MAX_TARGETS     = 5

    def __init__(self):
        self._targets:  Dict[str, AttentionTarget] = {}
        self._focused:  Optional[str] = None

    def attend(self, target_id: str, target_type: str, salience: float = 0.5):
        """Register or update an attention target."""
        now = time.monotonic()
        if target_id in self._targets:
            t = self._targets[target_id]
            t.last_seen = now
            t.look_count += 1
            t.salience = max(t.salience, salience)
        else:
            # Remove lowest-salience if at capacity
            if len(self._targets) >= self.MAX_TARGETS:
                weakest = min(self._targets.values(), key=lambda t: t.salience)
                del self._targets[weakest.target_id]
            self._targets[target_id] = AttentionTarget(
                target_id=target_id, target_type=target_type,
                salience=salience, novelty=1.0)

    def decay(self, dt_s: float):
        """Decay novelty and salience over time."""
        decay = math.exp(-dt_s / self.NOVELTY_DECAY_S)
        for t in self._targets.values():
            t.novelty  *= decay
            t.salience *= (1.0 - 0.01 * dt_s)  # slow salience decay
        # Remove totally forgotten targets
        self._targets = {k: v for k, v in self._targets.items()
                        if v.salience > 0.05}

    def most_salient(self) -> Optional[AttentionTarget]:
        """Return the currently most attention-worthy target."""
        if not self._targets: return None
        return max(self._targets.values(),
                   key=lambda t: t.salience * 0.5 + t.novelty * 0.5)

    def status(self) -> dict:
        focused = self.most_salient()
        return {
            'focused_on': focused.target_id if focused else None,
            'target_count': len(self._targets),
            'targets': [
                {'id': t.target_id, 'type': t.target_type,
                 'salience': round(t.salience, 2), 'novelty': round(t.novelty, 2)}
                for t in sorted(self._targets.values(),
                                key=lambda x: x.salience, reverse=True)
            ],
        }


# ════════════════════════════════════════════════════════════════════════════
# COGNITIVE MIND (subsystem)
# ════════════════════════════════════════════════════════════════════════════

class CognitiveMind(Subsystem):
    """
    The CERBERUS cognitive engine — integrates all memory, goal, and
    attention systems into a unified 'mind' subsystem.

    Runs at Priority.COGNITION (10Hz deliberative tick).
    Emits cognitive events to drive behavior, animation, and learning.
    """

    name     = 'cognitive_mind'
    priority = Priority.COGNITION

    def __init__(self, bus: SystemEventBus):
        self._bus            = bus
        self.working_memory  = WorkingMemory(capacity=9)
        self.episodic_memory = EpisodicMemory()
        self.semantic_memory = SemanticMemory()
        self.goal_stack      = GoalStack(bus)
        self.attention       = AttentionSystem()
        self._runtime        = None
        self._tick_count     = 0
        self._last_goal_gen  = 0.0  # time of last autonomous goal generation

        # Seed semantic knowledge
        self._seed_knowledge()

        # Subscribe to perceptual events
        bus.subscribe('detections',        self._on_detections)
        bus.subscribe('fsm.transition',    self._on_state_change)
        bus.subscribe('safety.trip',       self._on_safety_event)
        bus.subscribe('mission.complete',  self._on_mission_complete)
        bus.subscribe('behavior_start',    self._on_behavior_start)
        bus.subscribe('i18n.locale_changed', self._on_locale_change)

    def _seed_knowledge(self):
        """Pre-load factual world knowledge."""
        sm = self.semantic_memory
        sm.learn('robot.model', 'Unitree Go2', confidence=1.0, source='factory')
        sm.learn('robot.max_speed_ms', 1.5, confidence=1.0, source='spec')
        sm.learn('robot.battery_capacity_mah', 8000, confidence=1.0, source='spec')
        sm.learn('behavior.preferred_style', 'smooth', confidence=0.8, source='default')
        sm.learn('human.preferred_distance_m', 1.2, confidence=0.6, source='heuristic')
        sm.learn('environment.type', 'indoor', confidence=0.5, source='default')

    async def on_start(self, runtime):
        self._runtime = runtime
        # Generate initial idle goal
        await self.goal_stack.push(Goal(
            name='maintain_idle_presence', type='express',
            priority=0.1, params={'behavior': 'idle_breath'}))
        log.info('CognitiveMind started')

    async def on_tick(self, ctx: TickContext):
        self._tick_count += 1
        self.attention.decay(ctx.dt_s)

        # Every ~5 ticks (0.5s) — evaluate goals and generate autonomous behavior
        if self._tick_count % 5 == 0:
            await self._evaluate_goals()

        # Every ~30 ticks (3s) — consider generating a new spontaneous goal
        if self._tick_count % 30 == 0:
            await self._consider_spontaneous_goal()

    async def _evaluate_goals(self):
        """Check active goal validity; complete if preconditions no longer met."""
        active = self.goal_stack.active
        if not active: return

        if active.is_expired:
            log.info('Goal expired: %s', active.name)
            await self.goal_stack.complete_active(success=False)
            return

        # Emit goal status for BT runner to act on
        await self._bus.emit('cognition.active_goal', active.to_dict(), 'mind')

    async def _consider_spontaneous_goal(self):
        """
        Generate autonomous goals based on current state.
        Models the robot 'thinking of something to do'.
        """
        active = self.goal_stack.active
        # Only add spontaneous goals when idle
        if active and active.type not in ('express', 'rest'):
            return

        now = time.time()
        if now - self._last_goal_gen < 10.0:
            return  # Rate limit spontaneous goals

        # Sample spontaneous behaviors based on memory
        import random
        candidates = [
            ('explore_attention', 'explore', 0.3),
            ('idle_expression', 'express', 0.2),
            ('rest_moment', 'rest', 0.15),
        ]
        name, gtype, priority = random.choice(candidates)
        await self.goal_stack.push(Goal(
            name=name, type=gtype, priority=priority,
            deadline=now + 30.0))  # expires in 30s if not acted on
        self._last_goal_gen = now

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_detections(self, event: str, data: Any):
        detections = data if isinstance(data, list) else data.get('detections', [])
        for det in detections:
            label = det.get('label', 'unknown')
            conf  = det.get('conf', 0.5)
            dist  = det.get('dist_m', 2.0)
            self.attention.attend(
                target_id   = f'{label}_{det.get("track_id",0)}',
                target_type = label,
                salience    = conf * max(0.1, 1.0 - dist / 5.0))
            self.working_memory.store(
                {'type': 'detection', 'label': label, 'dist': dist},
                source='perception', importance=conf)

    def _on_state_change(self, event: str, data: Any):
        if isinstance(data, dict):
            new_state = data.get('to', '')
            self.working_memory.store(
                {'type': 'state_change', 'to': new_state},
                source='fsm', importance=0.7)
            # Record in episodic memory
            self.episodic_memory.record(
                'state_transition', data,
                context={'from': data.get('from'), 'to': new_state},
                emotion=self._state_to_emotion(new_state),
                valence=self._state_to_valence(new_state))

    def _on_safety_event(self, event: str, data: Any):
        if isinstance(data, dict):
            self.episodic_memory.record(
                'safety_event', data, emotion='fear', valence=-0.8)
            self.working_memory.store(data, source='safety', importance=1.0)

    def _on_mission_complete(self, event: str, data: Any):
        asyncio.create_task(self.goal_stack.complete_active(success=True))
        self.episodic_memory.record(
            'mission_complete', data, emotion='joy', valence=0.9)

    def _on_behavior_start(self, event: str, data: Any):
        if isinstance(data, dict):
            self.episodic_memory.record(
                'behavior_performed', data,
                emotion='joy', valence=0.6)

    def _on_locale_change(self, event: str, data: Any):
        if isinstance(data, dict):
            self.semantic_memory.learn(
                'ui.locale', data.get('locale'), source='user')

    @staticmethod
    def _state_to_emotion(state: str) -> str:
        return {
            'walking': 'joy', 'following': 'joy', 'performing': 'joy',
            'interacting': 'curiosity', 'patrolling': 'curiosity',
            'fault': 'fear', 'estop': 'fear',
            'idle': 'boredom', 'standing': 'neutral', 'sitting': 'neutral',
        }.get(state, 'neutral')

    @staticmethod
    def _state_to_valence(state: str) -> float:
        return {
            'walking': 0.6, 'following': 0.7, 'performing': 0.8,
            'interacting': 0.9, 'patrolling': 0.5,
            'fault': -0.9, 'estop': -1.0,
            'idle': -0.1, 'standing': 0.1, 'sitting': 0.0,
        }.get(state, 0.0)

    def status(self) -> dict:
        return {
            'name':     self.name,
            'priority': self.priority.name,
            'enabled':  self.enabled,
            'ticks':    self._tick_count,
            'working_memory':  self.working_memory.snapshot(),
            'episodic_stats':  self.episodic_memory.stats(),
            'goal_stack':      self.goal_stack.status_dict(),
            'attention':       self.attention.status(),
            'semantic_facts':  len(self.semantic_memory._facts),
        }
