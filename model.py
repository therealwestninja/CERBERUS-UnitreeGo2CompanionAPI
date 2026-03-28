"""
cerberus/personality/model.py  — CERBERUS v3.1
===============================================
Personality: stable traits + dynamic decaying mood.
Persists across restarts via JSON.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Traits:
    sociability:  float = 0.7
    playfulness:  float = 0.6
    energy:       float = 0.7
    curiosity:    float = 0.6

    def __post_init__(self):
        for a in ("sociability","playfulness","energy","curiosity"):
            setattr(self, a, max(0.0, min(1.0, getattr(self, a))))


@dataclass
class Mood:
    valence:       float = 0.3   # -1 very negative → +1 very positive
    arousal:       float = 0.4   # 0 calm → 1 excited
    _last_tick:    float = field(default_factory=time.monotonic, repr=False)

    def decay(self, base_v: float = 0.2, base_a: float = 0.4) -> None:
        dt = time.monotonic() - self._last_tick
        self._last_tick = time.monotonic()
        self.valence += (base_v - self.valence) * 0.01 * dt
        self.arousal  += (base_a - self.arousal)  * 0.015 * dt
        self.valence = max(-1.0, min(1.0, self.valence))
        self.arousal  = max(0.0,  min(1.0, self.arousal))

    def nudge(self, dv: float = 0.0, da: float = 0.0) -> None:
        self.valence = max(-1.0, min(1.0, self.valence + dv))
        self.arousal  = max(0.0,  min(1.0, self.arousal  + da))


class PersonalityModel:
    _FILE = "cerberus_personality.json"

    def __init__(self, traits: Traits | None = None,
                 persistence_path: str | None = None) -> None:
        self._traits = traits or Traits()
        self._mood   = Mood()
        self._path   = Path(persistence_path or self._FILE)
        self._load()

    # ── Trait shortcuts ────────────────────────────────────────────────── #
    @property
    def sociability(self)  -> float: return self._traits.sociability
    @property
    def playfulness(self)  -> float: return self._traits.playfulness
    @property
    def energy(self)       -> float: return self._traits.energy
    @property
    def curiosity(self)    -> float: return self._traits.curiosity
    @property
    def mood(self)         -> Mood:  return self._mood

    # ── Mood events ────────────────────────────────────────────────────── #
    def tick(self)              -> None: self._mood.decay(0.2+0.1*self._traits.playfulness, 0.3+0.2*self._traits.energy)
    def on_interaction(self)    -> None: self._mood.nudge(+0.15, +0.20)
    def on_task_success(self)   -> None: self._mood.nudge(+0.05, +0.05)
    def on_battery_low(self)    -> None: self._mood.nudge(-0.20, -0.10)
    def on_obstacle(self)       -> None: self._mood.nudge(-0.05, +0.15)

    @property
    def mood_label(self) -> str:
        v, a = self._mood.valence, self._mood.arousal
        if v > 0.5 and a > 0.6: return "excited"
        if v > 0.3 and a < 0.4: return "content"
        if v > 0.0:              return "calm"
        if v < -0.4:             return "distressed"
        if a > 0.7:              return "anxious"
        return "neutral"

    # ── Persistence ────────────────────────────────────────────────────── #
    def save(self) -> None:
        try:
            self._path.write_text(json.dumps({
                "traits": asdict(self._traits),
                "mood":   {"valence": self._mood.valence, "arousal": self._mood.arousal},
            }, indent=2))
        except OSError as e:
            logger.warning("Could not save personality: %s", e)

    def _load(self) -> None:
        if not self._path.exists(): return
        try:
            d = json.loads(self._path.read_text())
            if "traits" in d: self._traits = Traits(**d["traits"])
            if "mood"   in d:
                self._mood.valence = float(d["mood"].get("valence", 0.3))
                self._mood.arousal  = float(d["mood"].get("arousal",  0.4))
        except Exception as e:
            logger.warning("Could not load personality: %s", e)

    def to_dict(self) -> dict:
        self._mood.decay()
        return {
            "traits":     asdict(self._traits),
            "mood":       {"valence": round(self._mood.valence, 3),
                           "arousal":  round(self._mood.arousal,  3)},
            "mood_label": self.mood_label,
        }
