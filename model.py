"""
cerberus/personality/model.py
==============================
Personality & Mood system.

Traits (stable, set at init or learned over time)
------
  sociability    0–1   likelihood of initiating interaction
  playfulness    0–1   preference for dance / playful behaviors
  energy         0–1   base activity level
  curiosity      0–1   tendency to explore

Mood (dynamic, decays toward baseline)
----
  valence        -1–1  negative ↔ positive affect
  arousal        0–1   calm ↔ excited

Mood is updated by:
  - Interactions (human greets → positive valence boost)
  - Battery state (low battery → negative valence)
  - Successful task completion (small positive boost)
  - Time (slow decay toward neutral / baseline)
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Personality traits (stable)
# ---------------------------------------------------------------------------

@dataclass
class Traits:
    sociability:  float = 0.7    # 0 = aloof, 1 = very social
    playfulness:  float = 0.6    # 0 = reserved, 1 = very playful
    energy:       float = 0.7    # 0 = lethargic, 1 = hyperactive
    curiosity:    float = 0.6    # 0 = incurious, 1 = very curious

    def __post_init__(self) -> None:
        for attr in ("sociability", "playfulness", "energy", "curiosity"):
            setattr(self, attr, max(0.0, min(1.0, getattr(self, attr))))


# ---------------------------------------------------------------------------
# Mood (dynamic)
# ---------------------------------------------------------------------------

@dataclass
class Mood:
    valence: float = 0.3    # -1 = very negative, +1 = very positive
    arousal: float = 0.4    # 0 = calm, 1 = excited

    # Decay rate toward baseline per second
    valence_decay: float = field(default=0.01, repr=False)
    arousal_decay: float = field(default=0.015, repr=False)

    _last_update: float = field(default_factory=time.monotonic, repr=False)

    def decay(self, baseline_valence: float = 0.2,
              baseline_arousal: float = 0.4) -> None:
        now = time.monotonic()
        dt  = now - self._last_update
        self._last_update = now

        # Exponential decay toward baseline
        self.valence += (baseline_valence - self.valence) * self.valence_decay * dt
        self.arousal  += (baseline_arousal - self.arousal) * self.arousal_decay * dt
        self.valence = max(-1.0, min(1.0, self.valence))
        self.arousal  = max(0.0,  min(1.0, self.arousal))

    def apply_event(self, d_valence: float = 0.0, d_arousal: float = 0.0) -> None:
        self.valence = max(-1.0, min(1.0, self.valence + d_valence))
        self.arousal  = max(0.0,  min(1.0, self.arousal  + d_arousal))


# ---------------------------------------------------------------------------
# PersonalityModel — the public class
# ---------------------------------------------------------------------------

class PersonalityModel:
    """
    Combines stable traits with a dynamic mood.

    Persistence: saves/loads JSON from `persistence_path` so the
    dog's learned personality survives restarts.
    """

    _PERSISTENCE_FILE = "cerberus_personality.json"

    def __init__(self, traits: Optional[Traits] = None,
                 persistence_path: Optional[str] = None) -> None:
        self._traits  = traits or Traits()
        self._mood    = Mood()
        self._path    = Path(persistence_path or self._PERSISTENCE_FILE)
        self._load()

    # ------------------------------------------------------------------
    # Trait accessors (read-only shortcuts for CognitiveEngine)
    # ------------------------------------------------------------------

    @property
    def sociability(self) -> float:
        return self._traits.sociability

    @property
    def playfulness(self) -> float:
        return self._traits.playfulness

    @property
    def energy(self) -> float:
        return self._traits.energy

    @property
    def curiosity(self) -> float:
        return self._traits.curiosity

    # ------------------------------------------------------------------
    # Mood API
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Call periodically to decay mood toward baseline."""
        self._mood.decay(
            baseline_valence=0.2 + 0.1 * self._traits.playfulness,
            baseline_arousal=0.3 + 0.2 * self._traits.energy,
        )

    def on_interaction(self) -> None:
        """A human interacted with the robot."""
        self._mood.apply_event(d_valence=+0.15, d_arousal=+0.2)

    def on_task_success(self) -> None:
        self._mood.apply_event(d_valence=+0.05, d_arousal=+0.05)

    def on_battery_low(self) -> None:
        self._mood.apply_event(d_valence=-0.20, d_arousal=-0.10)

    def on_obstacle(self) -> None:
        self._mood.apply_event(d_valence=-0.05, d_arousal=+0.15)

    @property
    def mood(self) -> Mood:
        return self._mood

    @property
    def mood_label(self) -> str:
        """Human-readable mood description."""
        v, a = self._mood.valence, self._mood.arousal
        if v > 0.5 and a > 0.6:
            return "excited"
        if v > 0.3 and a < 0.4:
            return "content"
        if v > 0.0:
            return "calm"
        if v < -0.4:
            return "distressed"
        if a > 0.7:
            return "anxious"
        return "neutral"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        data = {
            "traits": asdict(self._traits),
            "mood":   {
                "valence": self._mood.valence,
                "arousal": self._mood.arousal,
            },
        }
        try:
            self._path.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            logger.warning("Could not save personality: %s", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            if "traits" in data:
                self._traits = Traits(**data["traits"])
            if "mood" in data:
                m = data["mood"]
                self._mood.valence = float(m.get("valence", 0.3))
                self._mood.arousal  = float(m.get("arousal",  0.4))
            logger.info("Loaded personality from %s", self._path)
        except Exception as exc:
            logger.warning("Could not load personality: %s", exc)

    # ------------------------------------------------------------------
    # Serialisation for API
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        self._mood.decay()
        return {
            "traits":     asdict(self._traits),
            "mood":       {"valence": round(self._mood.valence, 3),
                           "arousal":  round(self._mood.arousal,  3)},
            "mood_label": self.mood_label,
        }
