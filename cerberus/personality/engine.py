"""
cerberus/personality/engine.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Personality Engine

Models the robot's personality as a persistent, evolving system:

  PersonalityTraits — Big-Five-inspired trait model, stable over long sessions
  MoodState         — short-term emotional state that fluctuates with events
  EmotionalAffect   — current arousal/valence (Russell's circumplex model)
  PersonalityEngine — integrates traits + mood + affect → behavior modulation

Mood affects:
  - Behavior selection (which behaviors the robot initiates)
  - Animation style (speed, amplitude, expressiveness)
  - Interaction distance (how close it approaches humans)
  - Vocalization patterns (audio feedback)
  - Response latency (alert vs. sluggish)

Trait model (Big Five adapted for quadrupeds):
  Openness        — curiosity, exploration drive
  Conscientiousness — reliability, patrol adherence
  Extraversion    — social engagement, approach behavior
  Agreeableness   — compliance, gentleness around humans
  Neuroticism     — anxiety tendency, startle response magnitude

Mood is updated by:
  - Valenced events from EpisodicMemory
  - Physical states (fatigue → subdued, energized → playful)
  - Human interaction outcomes
  - Goal completion/failure
"""

import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..runtime import Subsystem, TickContext, Priority, SystemEventBus

log = logging.getLogger('cerberus.personality')


# ════════════════════════════════════════════════════════════════════════════
# AFFECTIVE CIRCUMPLEX (Russell 1980)
# ════════════════════════════════════════════════════════════════════════════

class MoodLabel(Enum):
    EXCITED    = 'excited'     # high arousal, high valence
    HAPPY      = 'happy'       # med arousal, high valence
    CONTENT    = 'content'     # low arousal, high valence
    RELAXED    = 'relaxed'     # low arousal, med valence
    BORED      = 'bored'       # low arousal, low valence
    SAD        = 'sad'         # low arousal, low valence (slight neg)
    ANXIOUS    = 'anxious'     # high arousal, low valence
    ALERT      = 'alert'       # high arousal, neutral valence
    CURIOUS    = 'curious'     # med-high arousal, slightly positive
    PLAYFUL    = 'playful'     # high arousal, high valence (bouncy)


def arousal_valence_to_mood(arousal: float, valence: float) -> MoodLabel:
    """Map Russell's circumplex coordinates to nearest mood label."""
    if valence > 0.5 and arousal > 0.5:  return MoodLabel.EXCITED
    if valence > 0.5 and arousal > 0.0:  return MoodLabel.HAPPY
    if valence > 0.5:                     return MoodLabel.CONTENT
    if valence > 0.2 and arousal < 0.0:  return MoodLabel.RELAXED
    if valence < -0.3 and arousal > 0.4: return MoodLabel.ANXIOUS
    if valence < -0.2 and arousal < 0.0: return MoodLabel.SAD
    if valence < 0.0 and arousal < -0.3: return MoodLabel.BORED
    if arousal > 0.5 and abs(valence) < 0.3: return MoodLabel.ALERT
    if valence > 0.2 and arousal > 0.3:  return MoodLabel.PLAYFUL
    return MoodLabel.CURIOUS


# ════════════════════════════════════════════════════════════════════════════
# PERSONALITY TRAITS (stable)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PersonalityTraits:
    """
    Stable personality traits — change slowly over many sessions.
    Range: [0.0, 1.0] for each dimension.
    """
    openness:          float = 0.70   # Curiosity, exploration
    conscientiousness: float = 0.65   # Task reliability, patrol adherence
    extraversion:      float = 0.75   # Social engagement (Go2 is naturally friendly)
    agreeableness:     float = 0.80   # Gentle, compliance with humans
    neuroticism:       float = 0.25   # Low anxiety (well-trained robot)

    def to_dict(self) -> dict:
        return {
            'openness':          round(self.openness, 3),
            'conscientiousness': round(self.conscientiousness, 3),
            'extraversion':      round(self.extraversion, 3),
            'agreeableness':     round(self.agreeableness, 3),
            'neuroticism':       round(self.neuroticism, 3),
        }

    def adapt(self, event: str, magnitude: float = 0.001):
        """
        Slowly adapt traits from repeated experiences.
        Magnitude capped at 0.005 per event to ensure slow drift.
        """
        mag = min(0.005, abs(magnitude))
        if event == 'successful_interaction':
            self.extraversion   = min(1.0, self.extraversion + mag)
            self.agreeableness  = min(1.0, self.agreeableness + mag * 0.5)
        elif event == 'safety_trip':
            self.neuroticism    = min(1.0, self.neuroticism + mag)
        elif event == 'goal_complete':
            self.conscientiousness = min(1.0, self.conscientiousness + mag * 0.3)
        elif event == 'exploration_reward':
            self.openness       = min(1.0, self.openness + mag)


# ════════════════════════════════════════════════════════════════════════════
# MOOD STATE (short-term, dynamic)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class MoodState:
    """
    Short-term emotional state on Russell's arousal/valence circumplex.
    Arousal: [-1, 1] — low (sleepy/calm) to high (excited/alert)
    Valence: [-1, 1] — negative (sad/anxious) to positive (happy/joyful)
    """
    arousal:    float = 0.0      # [-1, 1]
    valence:    float = 0.3      # [-1, 1] — slightly positive baseline
    decay_rate: float = 0.02     # per second — mood decays toward baseline
    baseline_v: float = 0.3      # stable valence baseline
    baseline_a: float = 0.0      # stable arousal baseline
    # History for tracking
    history: List[Tuple[float, float, float]] = field(default_factory=list)

    def inject(self, delta_arousal: float, delta_valence: float,
               source: str = ''):
        """Apply an emotional stimulus."""
        self.arousal = max(-1.0, min(1.0, self.arousal + delta_arousal))
        self.valence = max(-1.0, min(1.0, self.valence + delta_valence))
        self.history.append((time.time(), self.arousal, self.valence))
        if len(self.history) > 500: self.history.pop(0)

    def decay(self, dt_s: float):
        """Return mood toward baseline over time."""
        factor = math.exp(-self.decay_rate * dt_s)
        self.arousal = self.arousal * factor + self.baseline_a * (1 - factor)
        self.valence = self.valence * factor + self.baseline_v * (1 - factor)

    @property
    def label(self) -> MoodLabel:
        return arousal_valence_to_mood(self.arousal, self.valence)

    @property
    def intensity(self) -> float:
        """Overall mood intensity — distance from neutral."""
        return math.sqrt(self.arousal**2 + self.valence**2) / math.sqrt(2)

    def to_dict(self) -> dict:
        return {
            'arousal':   round(self.arousal, 3),
            'valence':   round(self.valence, 3),
            'label':     self.label.value,
            'intensity': round(self.intensity, 3),
        }


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR MODULATION
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class BehaviorModulation:
    """
    How current personality/mood modulates behavior parameters.
    Used by BT, AnimationPlayer, and motion controller.
    """
    speed_factor:        float = 1.0   # movement speed multiplier
    expressiveness:      float = 1.0   # animation amplitude multiplier
    approach_willingness: float = 1.0  # how willing to approach humans
    interaction_rate:    float = 1.0   # how often to initiate interactions
    rest_drive:          float = 0.0   # drive to rest/sit (increases with fatigue)
    exploration_drive:   float = 0.5   # drive to explore novel areas
    vigilance:           float = 0.5   # attentiveness to stimuli

    def to_dict(self) -> dict:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


# ════════════════════════════════════════════════════════════════════════════
# PERSONALITY ENGINE (subsystem)
# ════════════════════════════════════════════════════════════════════════════

class PersonalityEngine(Subsystem):
    """
    CERBERUS Personality Engine — integrates traits, mood, and affect.

    Runs at Priority.COGNITION (10Hz).
    Emits personality/mood state that modulates all behavior selection.
    """

    name     = 'personality_engine'
    priority = Priority.COGNITION

    def __init__(self, bus: SystemEventBus,
                 initial_traits: Optional[PersonalityTraits] = None):
        self._bus      = bus
        self.traits    = initial_traits or PersonalityTraits()
        self.mood      = MoodState()
        self._runtime  = None
        self._tick_count = 0
        self._fatigue    = 0.0
        self._last_modulation = BehaviorModulation()

        # Event-to-mood mappings
        self._event_effects: Dict[str, Tuple[float, float]] = {
            # (delta_arousal, delta_valence)
            'behavior_start':      ( 0.15,  0.20),  # performing → excited
            'mission.complete':    ( 0.20,  0.40),  # goal achieved → joy
            'safety.trip':         ( 0.50, -0.60),  # danger → fear/anxiety
            'safety.estop':        ( 0.70, -0.80),  # estop → high fear
            'goal.complete':       ( 0.10,  0.30),  # small reward
            'goal.failed':         (-0.10, -0.20),  # mild disappointment
            'world.object_added':  ( 0.10,  0.10),  # novelty → curiosity
            'watchdog.trip':       ( 0.30, -0.30),  # system issue → alert
        }

        # Subscribe to affective events
        for event_name in self._event_effects:
            bus.subscribe(event_name, self._on_affect_event)

        bus.subscribe('body.state',   self._on_body_state)
        bus.subscribe('detections',   self._on_detections)

    def _on_affect_event(self, event: str, data: Any):
        """Map platform events to emotional stimuli."""
        da, dv = self._event_effects.get(event, (0.0, 0.0))
        # Neuroticism amplifies negative events
        if dv < 0:
            da *= (1.0 + self.traits.neuroticism * 0.5)
            dv *= (1.0 + self.traits.neuroticism * 0.5)
        # Extraversion amplifies positive events
        if dv > 0:
            da *= (1.0 + self.traits.extraversion * 0.3)
            dv *= (1.0 + self.traits.extraversion * 0.3)
        self.mood.inject(da, dv, source=event)

    def _on_body_state(self, event: str, data: Any):
        """Fatigue suppresses arousal and extraversion expression."""
        if isinstance(data, dict):
            self._fatigue = data.get('energy', {}).get('fatigue_level', 0.0)

    def _on_detections(self, event: str, data: Any):
        """Novel detections increase arousal (curiosity)."""
        dets = data if isinstance(data, list) else data.get('detections', [])
        if dets:
            # Only react to high-confidence novel detections
            novel = [d for d in dets if d.get('conf', 0) > 0.7]
            if novel:
                self.mood.inject(0.05 * min(len(novel), 3), 0.05, 'novel_detection')

    async def on_start(self, runtime):
        self._runtime = runtime
        log.info('PersonalityEngine started — initial mood: %s', self.mood.label.value)

    async def on_tick(self, ctx: TickContext):
        self._tick_count += 1

        # Decay mood toward baseline
        self.mood.decay(ctx.dt_s)

        # Fatigue suppresses arousal
        fatigue_suppression = self._fatigue * 0.3
        self.mood.arousal = max(-0.5, self.mood.arousal - fatigue_suppression * ctx.dt_s * 0.5)

        # Add trait-driven spontaneous mood variation (personality 'coloring')
        if self._tick_count % 100 == 0:  # every ~10s
            # Extraversion adds slight positive arousal
            self.mood.inject(
                self.traits.extraversion * 0.02 * random.gauss(0, 1),
                self.traits.agreeableness * 0.01,
                'trait_expression')

        # Compute behavior modulation
        mod = self._compute_modulation()
        self._last_modulation = mod

        # Broadcast every 50 ticks (~5s)
        if self._tick_count % 50 == 0:
            await self._bus.emit('personality.state', {
                'mood':       self.mood.to_dict(),
                'traits':     self.traits.to_dict(),
                'modulation': mod.to_dict(),
                'fatigue':    round(self._fatigue, 3),
            }, 'personality')

    def _compute_modulation(self) -> BehaviorModulation:
        """
        Derive behavior modulation from current mood + traits + fatigue.
        This is what the BT, animator, and motion controller actually use.
        """
        m = self.mood
        t = self.traits
        f = self._fatigue

        # Speed: arousal + valence → lively or sluggish
        speed = 0.6 + (m.arousal * 0.25 + m.valence * 0.15)
        speed *= (1.0 - f * 0.4)   # fatigue slows movement
        speed = max(0.3, min(1.5, speed))

        # Expressiveness: positive mood → bigger animations
        expr = 0.5 + (m.valence * 0.4 + abs(m.arousal) * 0.1)
        expr = max(0.2, min(1.5, expr))

        # Approach willingness: extraversion + positive valence
        approach = t.extraversion * 0.6 + m.valence * 0.4
        approach = max(0.1, min(1.0, approach))

        # Rest drive: fatigue + low arousal
        rest = f * 0.7 + max(0, -m.arousal) * 0.3

        # Exploration: openness + positive valence + high arousal
        explore = t.openness * 0.5 + m.valence * 0.3 + m.arousal * 0.2
        explore = max(0.0, min(1.0, explore))

        # Vigilance: neuroticism + high arousal
        vigilance = t.neuroticism * 0.4 + abs(m.arousal) * 0.6
        vigilance = max(0.1, min(1.0, vigilance))

        return BehaviorModulation(
            speed_factor          = speed,
            expressiveness        = expr,
            approach_willingness  = approach,
            interaction_rate      = t.extraversion * 0.7 + m.valence * 0.3,
            rest_drive            = rest,
            exploration_drive     = explore,
            vigilance             = vigilance,
        )

    def modulation(self) -> BehaviorModulation:
        """Current behavior modulation — queried by other subsystems."""
        return self._last_modulation

    def inject_event(self, event: str, magnitude: float = 1.0):
        """Directly inject a personality/mood event (for testing/plugins)."""
        if event in self._event_effects:
            da, dv = self._event_effects[event]
            self.mood.inject(da * magnitude, dv * magnitude, event)

    def status(self) -> dict:
        return {
            'name':    self.name,
            'enabled': self.enabled,
            'ticks':   self._tick_count,
            'mood':    self.mood.to_dict(),
            'traits':  self.traits.to_dict(),
            'modulation': self._last_modulation.to_dict(),
        }
