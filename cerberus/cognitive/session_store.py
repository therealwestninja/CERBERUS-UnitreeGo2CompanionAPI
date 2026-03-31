"""
cerberus/cognitive/session_store.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Session Persistence — Personality Evolution

Saves and restores the BehaviorEngine's personality state across restarts
so that the robot's character evolves over its operational lifetime.

What is persisted:
  • PersonalityTraits (all five dimensions)
  • Last known MoodState
  • Session statistics (uptime, interaction counts, play count)
  • Session number (cumulative restart counter)

What is NOT persisted:
  • WorkingMemory (intentionally ephemeral — TTL-based)
  • GoalQueue (stale goals from a previous session are wrong)
  • Sensor state / bridge state (always fresh from hardware)

Evolution model
───────────────
At the END of each session, personality traits drift slightly based on
what happened during that session:

  friendliness += 0.003 × clamp(human_interactions / 10, 0, 1)
    (meeting people makes the robot more sociable)

  curiosity    += 0.002 × clamp(explore_ticks / 300, 0, 1)
    (exploring builds curiosity)

  playfulness  += 0.002 × clamp(play_behaviors / 5, 0, 1)
    (playing makes the robot more playful)

  energy       += 0.001 × clamp(uptime_h / 2, 0, 1) − 0.0005
    (slight energy drift — active sessions energise, idle sessions deplete)

  loyalty      — unchanged (unconditionally stable, like a good dog)

All deltas are clamped so each trait stays in [0.05, 0.98].
The net change per session is intentionally tiny (~0.002 per trait) so
personality takes weeks of operation to shift noticeably.

Storage
───────
JSON file at `logs/personality_session.json` (configurable via
CERBERUS_SESSION_FILE env var).  The file is written atomically (write
to temp then rename) to prevent corruption on power loss.

Usage
─────
  store = SessionStore()

  # On engine start:
  traits, stats = store.load()
  behavior_engine = BehaviorEngine(bridge, traits)
  behavior_engine._session_stats = stats

  # On engine stop:
  store.save(behavior_engine)
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits

logger = logging.getLogger(__name__)

SESSION_FILE = Path(
    os.getenv("CERBERUS_SESSION_FILE", "logs/personality_session.json")
)
SESSION_SCHEMA_VERSION = 2


# ─────────────────────────────────────────────────────────────────────────────
# Session statistics (accumulated during one engine run)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionStats:
    """Counters reset to zero at the start of each session."""
    human_interactions: int   = 0    # on_human_detected(True) calls
    play_behaviors:     int   = 0    # _play_behavior() calls
    explore_ticks:      int   = 0    # ticks spent in "exploring" active_behavior
    boredom_events:     int   = 0    # times boredom > 0.9 triggered play
    goals_completed:    int   = 0    # goals successfully popped
    session_start:      float = field(default_factory=time.time)
    session_number:     int   = 1

    @property
    def uptime_s(self) -> float:
        return time.time() - self.session_start

    @property
    def uptime_h(self) -> float:
        return self.uptime_s / 3600.0

    def to_dict(self) -> dict:
        return {
            "human_interactions": self.human_interactions,
            "play_behaviors":     self.play_behaviors,
            "explore_ticks":      self.explore_ticks,
            "boredom_events":     self.boredom_events,
            "goals_completed":    self.goals_completed,
            "session_number":     self.session_number,
            "uptime_s":           round(self.uptime_s, 1),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Personality evolution
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(value: float, lo: float = 0.05, hi: float = 0.98) -> float:
    return max(lo, min(hi, value))


def evolve_personality(
    traits: "PersonalityTraits",
    stats: SessionStats,
) -> "PersonalityTraits":
    """
    Return a NEW PersonalityTraits with small session-driven evolution.
    The original traits object is not mutated.
    """
    from cerberus.cognitive.behavior_engine import PersonalityTraits

    # Scale factors — each clamped 0→1 so a short/quiet session barely moves
    interaction_scale = _clamp(stats.human_interactions / 10.0, 0.0, 1.0)
    explore_scale     = _clamp(stats.explore_ticks / 300.0,     0.0, 1.0)
    play_scale        = _clamp(stats.play_behaviors / 5.0,       0.0, 1.0)
    uptime_scale      = _clamp(stats.uptime_h / 2.0,             0.0, 1.0)

    new_friendliness = _clamp(traits.friendliness + 0.003 * interaction_scale)
    new_curiosity    = _clamp(traits.curiosity    + 0.002 * explore_scale)
    new_playfulness  = _clamp(traits.playfulness  + 0.002 * play_scale)
    # Energy: active sessions energise, very short sessions slightly deplete
    energy_delta     = 0.001 * uptime_scale - 0.0005
    new_energy       = _clamp(traits.energy + energy_delta)
    # Loyalty: unconditionally stable
    new_loyalty      = traits.loyalty

    evolved = PersonalityTraits(
        energy       = new_energy,
        friendliness = new_friendliness,
        curiosity    = new_curiosity,
        loyalty      = new_loyalty,
        playfulness  = new_playfulness,
    )

    # Log only meaningful changes (> 0.0005)
    for attr in ("energy", "friendliness", "curiosity", "playfulness"):
        delta = getattr(evolved, attr) - getattr(traits, attr)
        if abs(delta) > 0.0005:
            logger.info(
                "[SessionStore] Personality evolution: %s %.4f → %.4f (Δ%+.4f)",
                attr, getattr(traits, attr), getattr(evolved, attr), delta
            )

    return evolved


# ─────────────────────────────────────────────────────────────────────────────
# SessionStore
# ─────────────────────────────────────────────────────────────────────────────

class SessionStore:
    """
    Loads and saves personality state to a JSON file.

    Thread/asyncio safe: writes are atomic (tmp file + rename).
    """

    def __init__(self, path: Path | None = None):
        self._path = path or SESSION_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load ──────────────────────────────────────────────────────────────────

    def load(self) -> tuple["PersonalityTraits", SessionStats]:
        """
        Load personality from disk.

        Returns (traits, stats) where stats is a fresh SessionStats with
        session_number incremented from the last saved value.

        If no session file exists (first boot), returns default traits.
        """
        from cerberus.cognitive.behavior_engine import PersonalityTraits

        if not self._path.exists():
            logger.info("[SessionStore] No session file found — starting with defaults")
            return PersonalityTraits(), SessionStats(session_number=1)

        try:
            with open(self._path) as f:
                data = json.load(f)

            if data.get("schema_version", 1) < SESSION_SCHEMA_VERSION:
                data = self._migrate(data)

            p = data.get("personality", {})
            traits = PersonalityTraits(
                energy       = float(p.get("energy",       0.7)),
                friendliness = float(p.get("friendliness", 0.8)),
                curiosity    = float(p.get("curiosity",    0.6)),
                loyalty      = float(p.get("loyalty",      0.9)),
                playfulness  = float(p.get("playfulness",  0.65)),
            )

            prev_session_num = int(data.get("session_number", 0))
            stats = SessionStats(session_number=prev_session_num + 1)

            logger.info(
                "[SessionStore] Loaded session #%d — "
                "energy=%.3f friendliness=%.3f curiosity=%.3f "
                "playfulness=%.3f",
                stats.session_number,
                traits.energy, traits.friendliness,
                traits.curiosity, traits.playfulness
            )
            return traits, stats

        except Exception as exc:
            logger.error(
                "[SessionStore] Failed to load session file (%s) — using defaults", exc
            )
            return PersonalityTraits(), SessionStats(session_number=1)

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self, engine_or_be) -> bool:
        """
        Save current personality + session stats to disk.

        Accepts either a BehaviorEngine or CerberusEngine (with .behavior_engine).
        Applies personality evolution before saving.

        Returns True on success.
        """
        try:
            be = getattr(engine_or_be, "behavior_engine", engine_or_be)
            if be is None:
                logger.warning("[SessionStore] No BehaviorEngine — nothing to save")
                return False

            traits: "PersonalityTraits" = be.personality
            stats:  SessionStats        = getattr(be, "_session_stats", SessionStats())

            # Apply one-session evolution
            evolved = evolve_personality(traits, stats)

            payload = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "saved_at":       time.time(),
                "session_number": stats.session_number,
                "personality": {
                    "energy":       evolved.energy,
                    "friendliness": evolved.friendliness,
                    "curiosity":    evolved.curiosity,
                    "loyalty":      evolved.loyalty,
                    "playfulness":  evolved.playfulness,
                },
                "last_mood":      be.mood.value,
                "evolution_stats": stats.to_dict(),
                "lifetime": self._load_lifetime_stats(stats),
            }

            # Atomic write
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            tmp.rename(self._path)

            logger.info(
                "[SessionStore] Session #%d saved — uptime=%.1fs",
                stats.session_number, stats.uptime_s
            )
            return True

        except Exception as exc:
            logger.error("[SessionStore] Save failed: %s", exc)
            return False

    # ── Lifetime stats accumulation ───────────────────────────────────────────

    def _load_lifetime_stats(self, current: SessionStats) -> dict:
        """Accumulate lifetime counters from previous sessions + current."""
        try:
            if self._path.exists():
                with open(self._path) as f:
                    prev = json.load(f)
                lt = prev.get("lifetime", {})
            else:
                lt = {}

            return {
                "total_sessions":        lt.get("total_sessions", 0) + 1,
                "total_uptime_s":        lt.get("total_uptime_s", 0.0) + current.uptime_s,
                "total_human_interactions": (
                    lt.get("total_human_interactions", 0) + current.human_interactions
                ),
                "total_play_behaviors":  lt.get("total_play_behaviors", 0)  + current.play_behaviors,
                "total_goals_completed": lt.get("total_goals_completed", 0) + current.goals_completed,
            }
        except Exception:
            return {}

    # ── Schema migration ──────────────────────────────────────────────────────

    def _migrate(self, data: dict) -> dict:
        """Upgrade session file from schema v1 → v2."""
        if data.get("schema_version", 1) == 1:
            data["schema_version"] = 2
            data.setdefault("lifetime", {})
            logger.info("[SessionStore] Migrated session file v1 → v2")
        return data

    # ── Quick read (for status endpoint) ─────────────────────────────────────

    def read_file(self) -> dict | None:
        """Return raw saved session dict, or None if not found."""
        try:
            if self._path.exists():
                with open(self._path) as f:
                    return json.load(f)
        except Exception:
            pass
        return None
