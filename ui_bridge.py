"""
CERBERUS UI Bridge
==================
Thread-safe bridge between the asyncio event loop (robot/plugin runtime)
and the Dear PyGui render thread.

Architecture
────────────
  asyncio thread   →  UIBridge.push_state(state_dict)  →  thread-safe queue
  DPG render thread ← UIBridge.get_latest_state()       ← reads latest state
  DPG UI action     →  UIBridge.send_command(cmd_dict)  →  publish_sync to bus
  asyncio thread   ←  ESTOP_TRIGGERED event            ←  bus dispatch

The bridge holds only ONE state snapshot at a time.  If the render thread
is slow, older states are discarded (last-write-wins).  This keeps the
UI always reflecting the most recent reality without lag accumulating.

Commands from the UI (button clicks, file loads) are sent via send_command()
which calls bus.publish_sync() — that is the ONLY way UI code should talk
to the runtime.  No direct function calls across threads.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIState:
    """Snapshot of all UI-relevant state.  Replaced atomically each tick."""
    # Connection
    robot_connected:  bool  = False
    intiface_connected: bool = False
    hismith_connected: bool = False
    wearable_connected: bool = False

    # Safety
    estop:            bool  = False
    estop_reason:     str   = ""

    # Robot telemetry
    battery_pct:      int   = 0
    battery_voltage:  float = 0.0
    imu_roll:         float = 0.0
    imu_pitch:        float = 0.0
    imu_yaw:          float = 0.0
    vx:               float = 0.0
    vy:               float = 0.0
    vyaw:             float = 0.0
    gait_mode:        int   = 0

    # Bio
    heart_rate_bpm:   int   = 0
    hr_alarm:         bool  = False

    # FunScript
    fs_loaded:        bool  = False
    fs_path:          str   = ""
    fs_playing:       bool  = False
    fs_duration_ms:   int   = 0
    fs_position_ms:   float = 0.0
    fs_position_norm: float = 0.0

    # Plugins
    plugin_states:    dict[str, str] = field(default_factory=dict)

    # Runtime
    tick_count:       int   = 0
    tick_overruns:    int   = 0
    bus_queue_depth:  int   = 0


class UIBridge:
    """
    Singleton bridge.  Instantiate once and share between runtime and UI.

    Thread safety
    ─────────────
    push_state   : called from asyncio (may be on asyncio thread) — uses a lock
    get_state    : called from DPG thread                         — uses a lock
    send_command : called from DPG thread                         — uses publish_sync
    """

    def __init__(self) -> None:
        self._state      = UIState()
        self._lock       = threading.Lock()
        self._cmd_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=64)
        self._bus        = None    # set via set_bus()

    def set_bus(self, bus: Any) -> None:
        self._bus = bus

    # ── State flow: runtime → UI ──────────────────────────────────────────────

    def push_state(self, raw: dict[str, Any]) -> None:
        """
        Called from the asyncio tick (runtime._push_ui_state).
        Translates the raw runtime dict into a typed UIState.
        """
        robot = raw.get("robot_state") or {}
        plugins = raw.get("plugins", {})

        # FunScript sub-state from plugin events is merged in separately
        # via update_fs_state() — don't overwrite those fields here unless
        # they're in the raw dict.

        new_state = UIState(
            estop            = raw.get("estop", False),
            estop_reason     = raw.get("estop_reason", ""),
            robot_connected  = raw.get("robot_connected", False),
            battery_pct      = robot.get("battery_percent", 0),
            battery_voltage  = robot.get("battery_voltage", 0.0),
            imu_roll         = robot.get("imu_roll", 0.0),
            imu_pitch        = robot.get("imu_pitch", 0.0),
            imu_yaw          = robot.get("imu_yaw", 0.0),
            vx               = robot.get("vx", 0.0),
            vy               = robot.get("vy", 0.0),
            vyaw             = robot.get("vyaw", 0.0),
            gait_mode        = robot.get("gait_mode", 0),
            plugin_states    = dict(plugins),
            tick_count       = raw.get("ticks", 0),
            tick_overruns    = raw.get("overruns", 0),
            bus_queue_depth  = raw.get("queue_depth", 0),
        )

        # Preserve fields that come from other sources
        with self._lock:
            new_state.heart_rate_bpm   = self._state.heart_rate_bpm
            new_state.hr_alarm         = self._state.hr_alarm
            new_state.fs_loaded        = self._state.fs_loaded
            new_state.fs_path          = self._state.fs_path
            new_state.fs_playing       = self._state.fs_playing
            new_state.fs_duration_ms   = self._state.fs_duration_ms
            new_state.fs_position_ms   = self._state.fs_position_ms
            new_state.fs_position_norm = self._state.fs_position_norm
            new_state.intiface_connected = self._state.intiface_connected
            new_state.hismith_connected  = self._state.hismith_connected
            new_state.wearable_connected = self._state.wearable_connected
            self._state = new_state

    def update_hr(self, bpm: int, alarm: bool = False) -> None:
        with self._lock:
            self._state.heart_rate_bpm = bpm
            self._state.hr_alarm       = alarm

    def update_fs(
        self,
        loaded: bool | None = None,
        path: str | None = None,
        playing: bool | None = None,
        duration_ms: int | None = None,
        position_ms: float | None = None,
        position_norm: float | None = None,
    ) -> None:
        with self._lock:
            if loaded       is not None: self._state.fs_loaded        = loaded
            if path         is not None: self._state.fs_path          = path
            if playing      is not None: self._state.fs_playing       = playing
            if duration_ms  is not None: self._state.fs_duration_ms   = duration_ms
            if position_ms  is not None: self._state.fs_position_ms   = position_ms
            if position_norm is not None: self._state.fs_position_norm = position_norm

    def update_peripheral(self, device: str, connected: bool) -> None:
        with self._lock:
            if device == "Intiface":
                self._state.intiface_connected = connected
            elif device == "Hismith":
                self._state.hismith_connected  = connected
            elif device == "GalaxyFit2":
                self._state.wearable_connected = connected

    # ── State flow: UI reads ──────────────────────────────────────────────────

    def get_state(self) -> UIState:
        """Non-blocking read.  Returns a COPY to avoid lock contention in DPG."""
        with self._lock:
            # Shallow copy — fine since UIState only contains primitives and small dicts
            import copy
            return copy.copy(self._state)

    # ── Command flow: UI → runtime ─────────────────────────────────────────────

    def send_command(self, command: str, **kwargs: Any) -> None:
        """
        Thread-safe command from DPG button/action → asyncio runtime.
        Valid commands: estop, clear_estop, play, pause, stop, load_funscript
        """
        if self._bus is None:
            return
        from cerberus.core.event_bus import Event, EventType
        self._bus.publish_sync(Event(
            type=EventType.UI_COMMAND,
            source="ui",
            data={"command": command, **kwargs},
            priority=5,
        ))


# ── Module singleton ──────────────────────────────────────────────────────────

_bridge: UIBridge | None = None


def get_bridge() -> UIBridge:
    global _bridge
    if _bridge is None:
        _bridge = UIBridge()
    return _bridge
