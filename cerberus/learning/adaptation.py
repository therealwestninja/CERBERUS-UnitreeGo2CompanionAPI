"""
cerberus/learning/adaptation.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Learning & Adaptation System

Three learning pipelines:

1. ReinforcementLearner — Q-learning over behavior-outcome pairs
   State:  (robot_state, mood_label, fatigue_label, time_of_day)
   Action: behavior choice (sit, follow, zoomies, patrol, etc.)
   Reward: derived from episodic valence + goal completion + user interactions

2. ImitationLearner — records and replays user-demonstrated sequences
   When a user triggers a sequence of behaviors manually, the system
   captures the sequence as an 'imitation episode' and can replay it
   autonomously in similar contexts.

3. PreferenceLearner — builds a user preference model
   Tracks which behaviors the user triggers, at what times, in what moods,
   and builds a personalized weighting system for autonomous behavior selection.

Data pipeline:
  EpisodeBuffer → LearningSystem → PreferenceModel → BehaviorModulation
  Events        → ReplayBuffer   → Q-table update

All learning is:
  - Bounded (max Q-table size, episode buffer limits)
  - Safe (never modifies safety limits or FSM transitions)
  - Transparent (all learned weights exposed via API)
  - Resettable (user can clear all learned preferences)
"""

import json
import logging
import math
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.learning')


# ════════════════════════════════════════════════════════════════════════════
# EXPERIENCE BUFFER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Experience:
    """A single (state, action, reward, next_state) reinforcement tuple."""
    state:      Tuple         # discrete state representation
    action:     str           # behavior ID executed
    reward:     float         # observed reward signal
    next_state: Tuple         # state after action
    timestamp:  float = field(default_factory=time.time)
    source:     str   = 'autonomous'   # autonomous / user_triggered

    def to_dict(self) -> dict:
        return {
            'state': str(self.state), 'action': self.action,
            'reward': round(self.reward, 3), 'source': self.source,
            'timestamp': self.timestamp,
        }


class ExperienceBuffer:
    """Bounded experience replay buffer for RL training."""

    def __init__(self, maxlen: int = 5000):
        self._buffer: deque = deque(maxlen=maxlen)

    def add(self, exp: Experience):
        self._buffer.append(exp)

    def sample(self, n: int) -> List[Experience]:
        n = min(n, len(self._buffer))
        return random.sample(list(self._buffer), n)

    def __len__(self) -> int: return len(self._buffer)

    def stats(self) -> dict:
        if not self._buffer:
            return {'count': 0, 'avg_reward': 0.0, 'sources': {}}
        rewards  = [e.reward for e in self._buffer]
        sources  = defaultdict(int)
        for e in self._buffer: sources[e.source] += 1
        return {
            'count':      len(self._buffer),
            'avg_reward': round(sum(rewards) / len(rewards), 3),
            'max_reward': round(max(rewards), 3),
            'sources':    dict(sources),
        }


# ════════════════════════════════════════════════════════════════════════════
# REINFORCEMENT LEARNER (tabular Q-learning)
# ════════════════════════════════════════════════════════════════════════════

class ReinforcementLearner:
    """
    Tabular Q-learning over discretized (state, behavior) space.

    State space (discretized):
      robot_state:  5 buckets (idle/active/performing/following/patrolling)
      mood_valence: 3 buckets (negative/neutral/positive)
      fatigue:      3 buckets (fresh/mild/tired)
      time_bucket:  4 buckets (morning/day/evening/night)

    Action space: all registered behavior IDs

    Q-update: Q(s,a) ← Q(s,a) + α[r + γ·max Q(s',a') - Q(s,a)]
    """

    ALPHA   = 0.05    # learning rate
    GAMMA   = 0.90    # discount factor
    EPSILON = 0.15    # exploration rate (ε-greedy)
    MAX_Q_STATES = 10_000

    def __init__(self):
        self._q:      Dict[Tuple, Dict[str, float]] = {}  # Q(state, action)
        self._buffer  = ExperienceBuffer()
        self._actions: List[str] = []    # registered behavior IDs
        self._updates  = 0
        self._last_state: Optional[Tuple] = None
        self._last_action: Optional[str]  = None

    def register_actions(self, behavior_ids: List[str]):
        self._actions = behavior_ids

    def discretize_state(self, robot_state: str, mood_valence: float,
                          fatigue: float) -> Tuple:
        """Convert continuous state to discrete tuple."""
        state_bucket = {
            'idle': 0, 'standing': 0, 'sitting': 0,
            'walking': 1, 'navigating': 1, 'patrolling': 1,
            'following': 2, 'interacting': 3, 'performing': 4,
        }.get(robot_state, 0)

        mood_bucket  = 0 if mood_valence < -0.2 else (2 if mood_valence > 0.2 else 1)
        fat_bucket   = 0 if fatigue < 0.3 else (2 if fatigue > 0.6 else 1)
        hour         = time.localtime().tm_hour
        time_bucket  = 0 if hour < 6 else (1 if hour < 12 else (2 if hour < 18 else 3))
        return (state_bucket, mood_bucket, fat_bucket, time_bucket)

    def select_action(self, state: Tuple) -> str:
        """ε-greedy action selection."""
        if not self._actions:
            return 'idle_breath'
        if random.random() < self.EPSILON or state not in self._q:
            return random.choice(self._actions)
        q_vals = self._q[state]
        # Return action with highest Q-value
        return max(self._actions, key=lambda a: q_vals.get(a, 0.0))

    def observe(self, state: Tuple, action: str, reward: float,
                next_state: Tuple, source: str = 'autonomous'):
        """Record experience and update Q-table."""
        exp = Experience(state=state, action=action, reward=reward,
                         next_state=next_state, source=source)
        self._buffer.add(exp)
        self._q_update(state, action, reward, next_state)

    def _q_update(self, s: Tuple, a: str, r: float, s_: Tuple):
        if len(self._q) >= self.MAX_Q_STATES:
            return  # prevent unbounded growth

        self._q.setdefault(s,  {})
        self._q.setdefault(s_, {})
        q_sa   = self._q[s].get(a, 0.0)
        q_max_ = max((self._q[s_].get(a_, 0.0) for a_ in self._actions), default=0.0)
        td     = r + self.GAMMA * q_max_ - q_sa
        self._q[s][a] = q_sa + self.ALPHA * td
        self._updates += 1

    def top_actions(self, state: Tuple, n: int = 3) -> List[Tuple[str, float]]:
        """Return top-N actions for state by Q-value."""
        q = self._q.get(state, {})
        return sorted(q.items(), key=lambda x: x[1], reverse=True)[:n]

    def stats(self) -> dict:
        return {
            'q_states':   len(self._q),
            'updates':    self._updates,
            'epsilon':    self.EPSILON,
            'buffer':     self._buffer.stats(),
        }


# ════════════════════════════════════════════════════════════════════════════
# IMITATION LEARNER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ImitationEpisode:
    """A user-demonstrated behavior sequence."""
    id:         str = field(default_factory=lambda: str(__import__('uuid').uuid4())[:8])
    name:       str = ''
    sequence:   List[str] = field(default_factory=list)  # behavior IDs in order
    context:    Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)
    play_count:  int   = 0

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'name': self.name,
            'sequence': self.sequence, 'context': self.context,
            'recorded_at': self.recorded_at, 'play_count': self.play_count,
        }


class ImitationLearner:
    """
    Records user-demonstrated behavior sequences and enables replay.
    When user triggers a sequence manually, system captures it as
    an imitation episode for future autonomous reproduction.
    """

    MAX_EPISODES    = 50
    RECORD_WINDOW_S = 30.0  # window within which actions are grouped

    def __init__(self):
        self._episodes:     List[ImitationEpisode] = []
        self._recording:    bool = False
        self._current_seq:  List[str] = []
        self._current_ctx:  dict = {}
        self._record_start: float = 0.0

    def start_recording(self, name: str = '', context: dict = None):
        """Begin capturing a user behavior sequence."""
        self._recording   = True
        self._current_seq = []
        self._current_ctx = context or {}
        self._record_start = time.time()
        self._current_name = name
        log.info('Imitation recording started: %s', name)

    def observe_behavior(self, behavior_id: str, source: str = 'user'):
        """Record a behavior observation during active recording."""
        if not self._recording: return
        if time.time() - self._record_start > self.RECORD_WINDOW_S:
            self.stop_recording(save=True)
            return
        if source == 'user':
            self._current_seq.append(behavior_id)

    def stop_recording(self, save: bool = True) -> Optional[str]:
        """Stop recording and optionally save the episode."""
        if not self._recording: return None
        self._recording = False
        if save and len(self._current_seq) >= 2:
            if len(self._episodes) >= self.MAX_EPISODES:
                # Remove oldest
                self._episodes.pop(0)
            ep = ImitationEpisode(
                name     = self._current_name,
                sequence = list(self._current_seq),
                context  = self._current_ctx)
            self._episodes.append(ep)
            log.info('Imitation episode saved: %s (%d behaviors)',
                     ep.name, len(ep.sequence))
            return ep.id
        return None

    def get_playback_sequence(self, context: Optional[dict] = None) -> List[str]:
        """
        Return best matching episode's sequence for current context.
        Preference: most-played, or most recently recorded.
        """
        if not self._episodes:
            return []
        # Simple selection: highest play_count
        ep = max(self._episodes, key=lambda e: e.play_count)
        ep.play_count += 1
        return list(ep.sequence)

    def list_episodes(self) -> List[dict]:
        return [e.to_dict() for e in self._episodes]


# ════════════════════════════════════════════════════════════════════════════
# PREFERENCE LEARNER
# ════════════════════════════════════════════════════════════════════════════

class PreferenceLearner:
    """
    Builds a personalized preference model from user interaction history.
    Tracks: which behaviors users trigger, at what times, in what context.
    Produces a preference weight vector over all behaviors.
    """

    DECAY_RATE  = 0.999   # per observation (slow decay of old preferences)
    MAX_HISTORY = 1000

    def __init__(self):
        self._weights:   Dict[str, float] = defaultdict(float)
        self._counts:    Dict[str, int]   = defaultdict(int)
        self._history:   deque = deque(maxlen=self.MAX_HISTORY)
        self._total_obs: int = 0

    def observe(self, behavior_id: str, source: str = 'user',
                context: Optional[dict] = None, reward: float = 1.0):
        """Record a behavior trigger with optional reward."""
        self._total_obs += 1
        # Decay all weights slightly
        for k in self._weights:
            self._weights[k] *= self.DECAY_RATE

        # Boost triggered behavior
        self._weights[behavior_id]  = min(1.0,
            self._weights[behavior_id] + (0.05 if source == 'user' else 0.01) * reward)
        self._counts[behavior_id]  += 1
        self._history.append({
            'behavior': behavior_id, 'source': source,
            'reward': reward, 'ts': time.time()
        })

    def preferred_behaviors(self, n: int = 5) -> List[Tuple[str, float]]:
        """Return top-N preferred behaviors by weight."""
        return sorted(self._weights.items(), key=lambda x: x[1], reverse=True)[:n]

    def weight(self, behavior_id: str) -> float:
        return self._weights.get(behavior_id, 0.0)

    def stats(self) -> dict:
        return {
            'total_observations': self._total_obs,
            'unique_behaviors':   len(self._weights),
            'top_preferences':    self.preferred_behaviors(5),
        }


# ════════════════════════════════════════════════════════════════════════════
# LEARNING SYSTEM (subsystem)
# ════════════════════════════════════════════════════════════════════════════

class LearningSystem(Subsystem):
    """
    CERBERUS Learning System — integrates RL, imitation, and preference learning.
    Runs at Priority.LEARNING (1Hz) to perform batch Q-table updates.
    """

    name     = 'learning_system'
    priority = Priority.LEARNING

    def __init__(self, bus: SystemEventBus):
        self._bus           = bus
        self.rl             = ReinforcementLearner()
        self.imitation      = ImitationLearner()
        self.preferences    = PreferenceLearner()
        self._runtime       = None
        self._tick_count    = 0
        self._last_state:   Optional[Tuple] = None
        self._last_action:  Optional[str]   = None
        self._robot_state   = 'idle'
        self._mood_valence  = 0.3
        self._fatigue       = 0.0
        self._pending_reward = 0.0
        self._save_path     = '/tmp/cerberus_learning.json'

        # Subscribe to reward signals
        bus.subscribe('goal.complete',       lambda e, d: self._add_reward(0.8))
        bus.subscribe('goal.failed',         lambda e, d: self._add_reward(-0.4))
        bus.subscribe('mission.complete',    lambda e, d: self._add_reward(1.0))
        bus.subscribe('safety.trip',         lambda e, d: self._add_reward(-0.9))
        bus.subscribe('behavior_start',      self._on_behavior)
        bus.subscribe('personality.state',   self._on_personality)
        bus.subscribe('fsm.transition',      self._on_state)

    def _add_reward(self, r: float): self._pending_reward += r

    def _on_behavior(self, event: str, data: Any):
        if isinstance(data, dict):
            bid = data.get('id') or data.get('behavior_id', '')
            if bid:
                self.preferences.observe(bid, source='autonomous')
                self.imitation.observe_behavior(bid, 'autonomous')

    def _on_personality(self, event: str, data: Any):
        if isinstance(data, dict):
            self._mood_valence = data.get('mood', {}).get('valence', 0.3)
            self._fatigue      = data.get('fatigue', 0.0)

    def _on_state(self, event: str, data: Any):
        if isinstance(data, dict):
            self._robot_state = data.get('to', 'idle')

    def register_user_behavior(self, behavior_id: str, reward: float = 1.0):
        """Called when user manually triggers a behavior (API endpoint)."""
        self.preferences.observe(behavior_id, source='user', reward=reward)
        self.imitation.observe_behavior(behavior_id, 'user')
        state = self.rl.discretize_state(
            self._robot_state, self._mood_valence, self._fatigue)
        # User-triggered behavior is implicitly a 'good' action
        if self._last_state and self._last_action:
            self.rl.observe(self._last_state, self._last_action,
                            reward, state, source='user')

    async def on_start(self, runtime):
        self._runtime = runtime
        # Register behaviors from behavior registry
        if runtime.platform and hasattr(runtime.platform, 'behaviors'):
            bids = [b['id'] for b in runtime.platform.behaviors.all()]
            self.rl.register_actions(bids)
        self._load_preferences()
        log.info('LearningSystem started — %d Q-states loaded',
                 len(self.rl._q))

    async def on_tick(self, ctx: TickContext):
        self._tick_count += 1

        # Compute current state
        state = self.rl.discretize_state(
            self._robot_state, self._mood_valence, self._fatigue)

        # Flush accumulated reward to Q-table
        if self._last_state and self._last_action and self._pending_reward != 0:
            self.rl.observe(self._last_state, self._last_action,
                            self._pending_reward, state)
            self._pending_reward = 0.0

        # Occasionally emit preferred behavior suggestion
        if self._tick_count % 60 == 0:  # every ~60s
            prefs = self.preferences.preferred_behaviors(3)
            if prefs:
                await self._bus.emit('learning.preferred_behaviors',
                                     {'behaviors': prefs}, 'learning')

        # Save every 300 ticks (~5 min)
        if self._tick_count % 300 == 0:
            self._save_preferences()

        self._last_state = state

    def suggest_behavior(self) -> str:
        """Suggest a behavior based on combined RL + preference signals."""
        if not self._last_state:
            return 'idle_breath'
        # Blend RL Q-values with preference weights
        rl_top = dict(self.rl.top_actions(self._last_state, 5))
        candidates = set(rl_top) | set(k for k, _ in self.preferences.preferred_behaviors(5))
        if not candidates:
            return 'idle_breath'
        best = max(candidates, key=lambda bid:
                   rl_top.get(bid, 0) * 0.6 + self.preferences.weight(bid) * 0.4)
        return best

    def _save_preferences(self):
        try:
            data = {
                'preferences': dict(self.preferences._weights),
                'counts':      dict(self.preferences._counts),
                'q_table':     {str(k): v for k, v in self.rl._q.items()},
                'saved_at':    time.time(),
            }
            with open(self._save_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.warning('Could not save learning data: %s', e)

    def _load_preferences(self):
        if not os.path.exists(self._save_path):
            return
        try:
            with open(self._save_path) as f:
                data = json.load(f)
            for k, v in data.get('preferences', {}).items():
                self.preferences._weights[k] = float(v)
            for k, v in data.get('counts', {}).items():
                self.preferences._counts[k] = int(v)
            log.info('Learning data loaded from %s', self._save_path)
        except Exception as e:
            log.warning('Could not load learning data: %s', e)

    def reset_all(self):
        """Reset all learned preferences (user request)."""
        self.preferences._weights.clear()
        self.preferences._counts.clear()
        self.preferences._history.clear()
        self.rl._q.clear()
        self.rl._updates = 0
        if os.path.exists(self._save_path):
            os.remove(self._save_path)
        log.info('All learning data reset')

    def status(self) -> dict:
        return {
            'name':        self.name,
            'enabled':     self.enabled,
            'ticks':       self._tick_count,
            'rl':          self.rl.stats(),
            'preferences': self.preferences.stats(),
            'imitation_episodes': len(self.imitation._episodes),
            'suggestion':  self.suggest_behavior(),
        }
