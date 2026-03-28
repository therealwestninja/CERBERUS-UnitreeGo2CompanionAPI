"""
CERBERUS — FunScript Player Plugin
====================================
Replays .funscript files as robot choreography.

FunScript format:
  {"version":"1.0","inverted":false,"range":90,
   "actions":[{"at":0,"pos":0},{"at":500,"pos":100},...]}

  • at  — milliseconds from start
  • pos — 0–100 (position/intensity)

Mapping to robot behaviour (robot-as-master model):
  pos → linear interpolation drives:
    • body_height  (0→low  100→high,  centered on default)
    • walk_speed   (pos determines forward velocity)
    • Also published as FUNSCRIPT_TICK so Buttplug/Hismith plugins can mirror it

The player respects ESTOP — it pauses immediately and does not resume
until the safety system clears.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cerberus.core.event_bus import Event, EventType
from cerberus.core.plugin_base import CERBERUSPlugin, PluginManifest, PluginTrustLevel
from cerberus.core.safety import get_safety

logger = logging.getLogger(__name__)

MANIFEST = PluginManifest(
    name        = "FunScript",
    version     = "1.0.0",
    description = "Replay .funscript timeline files as robot choreography",
    author      = "CERBERUS Contributors",
    trust_level = PluginTrustLevel.TRUSTED,
    capabilities = ["robot_motion", "peripheral_output"],
)


@dataclass
class FunScriptAction:
    at_ms:    int     # milliseconds from start
    position: float   # 0.0 – 1.0 (normalised)


@dataclass
class FunScriptFile:
    path:     str
    actions:  list[FunScriptAction]
    inverted: bool    = False
    range_:   int     = 90
    duration_ms: int  = 0


class FunScriptPlugin(CERBERUSPlugin):
    """
    Plays a .funscript file, emitting FUNSCRIPT_TICK events each frame.
    The robot adapter (Go2WebRTCAdapter) should be wired to respond to
    FUNSCRIPT_TICK events via the SportController.
    """

    def __init__(self, robot_adapter: Any | None = None) -> None:
        super().__init__(MANIFEST)
        self._robot        = robot_adapter
        self._script:      FunScriptFile | None = None
        self._playing      = False
        self._paused       = False
        self._start_time   = 0.0
        self._pause_offset = 0.0
        self._current_idx  = 0
        self._last_pos     = 0.0
        self._last_velocity = 0.0

    # ── Plugin lifecycle ──────────────────────────────────────────────────────

    async def on_load(self, config: dict[str, Any]) -> None:
        self.bus.subscribe(EventType.ESTOP_TRIGGERED,  self._on_estop,  priority=1)
        self.bus.subscribe(EventType.ESTOP_CLEARED,    self._on_clear,  priority=2)
        path = config.get("autoload")
        if path:
            await self.load_file(path)

    async def on_start(self) -> None:
        logger.info("FunScript player ready")

    async def on_stop(self) -> None:
        await self.stop()

    async def on_unload(self) -> None:
        self._script = None

    async def on_tick(self, dt: float) -> None:
        if not self._playing or self._paused:
            return
        if get_safety().is_stopped():
            await self.pause()
            return
        await self._advance()

    # ── Public API ────────────────────────────────────────────────────────────

    async def load_file(self, path: str) -> bool:
        p = Path(path)
        if not p.exists():
            logger.error("FunScript file not found: %s", path)
            return False
        try:
            raw = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            logger.error("Invalid FunScript JSON: %s", e)
            return False

        actions = [
            FunScriptAction(a["at"], a["pos"] / 100.0)
            for a in raw.get("actions", [])
        ]
        if not actions:
            logger.warning("FunScript file has no actions: %s", path)
            return False

        actions.sort(key=lambda a: a.at_ms)
        duration = actions[-1].at_ms if actions else 0

        self._script = FunScriptFile(
            path     = str(p),
            actions  = actions,
            inverted = raw.get("inverted", False),
            range_   = raw.get("range", 90),
            duration_ms = duration,
        )
        self._reset_playhead()
        logger.info("Loaded: %s  (%d actions, %.1fs)", p.name, len(actions), duration / 1000)

        await self._emit(EventType.FUNSCRIPT_LOADED, {
            "path":     str(p),
            "actions":  len(actions),
            "duration_ms": duration,
        }, priority=9)
        return True

    async def play(self) -> None:
        if not self._script:
            logger.warning("No script loaded")
            return
        if get_safety().is_stopped():
            logger.warning("Cannot play — ESTOP active")
            return
        if self._paused:
            self._start_time   = time.monotonic() - self._pause_offset
            self._paused       = False
        else:
            self._reset_playhead()
            self._start_time = time.monotonic()
        self._playing = True
        logger.info("FunScript play")
        await self._emit(EventType.FUNSCRIPT_PLAY, priority=5)

    async def pause(self) -> None:
        if not self._playing:
            return
        self._pause_offset = (time.monotonic() - self._start_time) * 1000
        self._paused  = True
        self._playing = False
        await self._command_robot(0.0, 0.0)      # zero velocity on pause
        logger.info("FunScript pause at %.1f ms", self._pause_offset)
        await self._emit(EventType.FUNSCRIPT_PAUSE, {"position_ms": self._pause_offset}, priority=5)

    async def stop(self) -> None:
        self._playing = False
        self._paused  = False
        self._reset_playhead()
        await self._command_robot(0.0, 0.0)
        logger.info("FunScript stop")
        await self._emit(EventType.FUNSCRIPT_STOP, priority=5)

    # ── Advance playhead each tick ────────────────────────────────────────────

    async def _advance(self) -> None:
        if not self._script:
            return

        now_ms = (time.monotonic() - self._start_time) * 1000

        if now_ms >= self._script.duration_ms:
            await self.stop()
            await self._emit(EventType.FUNSCRIPT_ENDED, priority=5)
            return

        # Find the surrounding keyframes
        actions = self._script.actions
        idx     = self._current_idx
        while idx + 1 < len(actions) and actions[idx + 1].at_ms <= now_ms:
            idx += 1
        self._current_idx = idx

        pos = actions[idx].position  # current keyframe position

        # Interpolate toward next keyframe
        if idx + 1 < len(actions):
            nxt = actions[idx + 1]
            cur = actions[idx]
            t   = (now_ms - cur.at_ms) / max(1, nxt.at_ms - cur.at_ms)
            t   = max(0.0, min(1.0, t))
            pos = cur.position + (nxt.position - cur.position) * t

        if self._script.inverted:
            pos = 1.0 - pos

        # Velocity = rate of position change (useful for peripheral plugins)
        velocity = (pos - self._last_pos) / max(0.001, 1 / 30)   # per-tick approx
        self._last_pos      = pos
        self._last_velocity = velocity

        # Publish tick event (Buttplug, Hismith subscribe to this)
        await self._emit(EventType.FUNSCRIPT_TICK, {
            "position":    pos,
            "velocity":    velocity,
            "position_ms": now_ms,
        }, priority=5)

        # Command robot
        await self._command_robot(pos, velocity)

    async def _command_robot(self, pos: float, velocity: float) -> None:
        """Map funscript position (0–1) to robot motion."""
        if not self._robot or not self._robot.connected:
            return

        # pos → forward velocity:  0=still, 1=max forward
        # velocity sign → strafe direction (adds rhythmic sway)
        vx   = pos * 0.4           # max 0.4 m/s forward
        vy   = velocity * 0.08     # gentle side-sway from velocity change
        vyaw = 0.0

        # pos → body height offset:  0=low, 1=high
        height = (pos - 0.5) * 0.1   # ±0.05m from default

        if abs(vx) > 0.02 or abs(vy) > 0.02:
            await self._robot.move(vx, vy, vyaw)
        else:
            await self._robot.stop()

        await self._robot.set_body_height(height)

    # ── Safety handlers ───────────────────────────────────────────────────────

    async def _on_estop(self, event: Event) -> None:
        await self.pause()

    async def _on_clear(self, event: Event) -> None:
        logger.info("ESTOP cleared — FunScript paused, awaiting manual resume")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reset_playhead(self) -> None:
        self._current_idx  = 0
        self._pause_offset = 0.0
        self._last_pos     = 0.0
        self._last_velocity = 0.0

    @property
    def current_position_ms(self) -> float:
        if not self._playing:
            return self._pause_offset
        return (time.monotonic() - self._start_time) * 1000

    @property
    def is_playing(self) -> bool:
        return self._playing and not self._paused
