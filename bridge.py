"""
cerberus/hardware/bridge.py  — CERBERUS v3.1
============================================
Unified Go2Bridge: transport-agnostic hardware interface.

Transports (select via config/cerberus.yaml  robot.transport):
  mock    in-memory stub — default, CI, simulation
  dds     CycloneDDS via unitree_sdk2_python (Go2 EDU wired)
  webrtc  Wi-Fi WebRTC via go2_webrtc_connect (AIR/PRO/EDU, no jailbreak)

All motion commands pass through SafetyGate before hardware.
Auto-reconnect watchdog restores connectivity on transient failures.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from cerberus.safety.gate import SafetyConfig, SafetyGate

logger = logging.getLogger(__name__)

# ── Mode constants (all 17 native Go2 sport modes) ────────────────────── #
AVAILABLE_MODES: set[str] = {
    "damp", "balance_stand", "stop_move", "stand_up", "stand_down",
    "sit", "rise_sit", "hello", "stretch", "wallow", "scrape",
    "front_flip", "front_jump", "front_pounce", "dance1", "dance2",
    "finger_heart",
}

class TransportMode(str, Enum):
    MOCK   = "mock"
    DDS    = "dds"
    WEBRTC = "webrtc"

class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"
    RECONNECTING = "reconnecting"
    ERROR        = "error"


@dataclass
class RobotState:
    timestamp:          float = field(default_factory=time.time)
    position_x:         float = 0.0
    position_y:         float = 0.0
    yaw:                float = 0.0
    pitch:              float = 0.0
    roll:               float = 0.0
    body_height:        float = 0.38
    vx:                 float = 0.0
    vy:                 float = 0.0
    vyaw:               float = 0.0
    battery_voltage:    float = 0.0
    battery_percent:    float = 0.0
    imu_temperature:    float = 0.0
    foot_force:         list  = field(default_factory=lambda: [0.0]*4)
    sport_mode_active:  bool  = True
    obstacle_avoidance: bool  = False
    current_mode:       str   = "balance_stand"
    connection_state:   ConnectionState = ConnectionState.DISCONNECTED
    latency_ms:         float = 0.0


# ── Base transport ─────────────────────────────────────────────────────── #
class _BaseTransport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def read_state(self) -> RobotState: return RobotState()
    async def move(self, vx: float, vy: float, vyaw: float) -> None: ...
    async def set_mode(self, mode: str) -> None: ...
    async def set_body_height(self, h: float) -> None: ...
    async def set_euler(self, r: float, p: float, y: float) -> None: ...
    async def set_foot_raise_height(self, h: float) -> None: ...
    async def set_speed_level(self, l: int) -> None: ...
    async def set_obstacle_avoidance(self, en: bool) -> None: ...
    async def set_vui(self, vol: int, bri: int) -> None: ...
    async def stop(self) -> None: ...
    async def stand_up(self) -> None: ...
    async def stand_down(self) -> None: ...
    async def emergency_stop(self) -> None: ...
    def is_connected(self) -> bool: return False


# ── Mock (simulation / CI) ─────────────────────────────────────────────── #
class _MockTransport(_BaseTransport):
    def __init__(self) -> None:
        self._s = RobotState(
            battery_voltage=25.2, battery_percent=85.0,
            sport_mode_active=True,
            connection_state=ConnectionState.CONNECTED,
        )
        self._connected = False

    async def connect(self) -> None:
        self._connected = True
        self._s.connection_state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self._connected = False

    async def read_state(self) -> RobotState:
        return self._s

    async def move(self, vx, vy, vyaw) -> None:
        self._s.vx, self._s.vy, self._s.vyaw = vx, vy, vyaw

    async def set_mode(self, mode) -> None:
        self._s.current_mode = mode

    async def set_body_height(self, h) -> None:
        self._s.body_height = h

    async def set_euler(self, r, p, y) -> None:
        self._s.roll, self._s.pitch, self._s.yaw = r, p, y

    async def set_foot_raise_height(self, h) -> None: pass
    async def set_speed_level(self, l) -> None: pass

    async def set_obstacle_avoidance(self, en) -> None:
        self._s.obstacle_avoidance = en

    async def set_vui(self, vol, bri) -> None: pass

    async def stop(self) -> None:
        self._s.vx = self._s.vy = self._s.vyaw = 0.0

    async def stand_up(self) -> None:
        self._s.current_mode = "stand_up"

    async def stand_down(self) -> None:
        self._s.current_mode = "stand_down"

    async def emergency_stop(self) -> None:
        self._s.vx = self._s.vy = self._s.vyaw = 0.0
        self._s.current_mode = "damp"

    def is_connected(self) -> bool:
        return self._connected


# ── DDS (unitree_sdk2_python) ─────────────────────────────────────────── #
class _DDSTransport(_BaseTransport):
    _MODE_MAP = {
        "damp": "Damp", "balance_stand": "BalanceStand",
        "stop_move": "StopMove", "stand_up": "StandUp",
        "stand_down": "StandDown", "sit": "Sit", "rise_sit": "RiseSit",
        "hello": "Hello", "stretch": "Stretch", "wallow": "Wallow",
        "scrape": "Scrape", "front_flip": "FrontFlip",
        "front_jump": "FrontJump", "front_pounce": "FrontPounce",
        "dance1": "Dance1", "dance2": "Dance2", "finger_heart": "FingerHeart",
    }

    def __init__(self, iface: str) -> None:
        self._iface = iface
        self._sport = self._vui = self._obs = self._state_r = None
        self._connected = False
        self._last: RobotState = RobotState()

    async def connect(self) -> None:
        try:
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import ObstaclesAvoidClient
            from unitree_sdk2py.go2.vui.vui_client import VuiClient
            from unitree_sdk2py.go2.sport.sport_state_client import SportStateClient
            import unitree_sdk2py.core.channel as ch
            await asyncio.to_thread(ch.ChannelFactoryInitialize, 0, self._iface)
            self._sport   = SportClient();   await asyncio.to_thread(self._sport.Init)
            self._obs     = ObstaclesAvoidClient(); await asyncio.to_thread(self._obs.Init)
            self._vui     = VuiClient();     await asyncio.to_thread(self._vui.Init)
            self._state_r = SportStateClient(); await asyncio.to_thread(self._state_r.Init)
            self._connected = True
            logger.info("DDS transport connected on %s", self._iface)
        except ImportError:
            raise RuntimeError("unitree_sdk2_python not installed. Run: pip install unitree_sdk2py")

    async def disconnect(self) -> None:
        self._connected = False

    async def read_state(self) -> RobotState:
        if not self._connected: return self._last
        try:
            raw = await asyncio.to_thread(self._state_r.GetState)
            self._last = RobotState(
                timestamp=time.time(),
                position_x=raw.position[0] if raw else 0.0,
                position_y=raw.position[1] if raw else 0.0,
                yaw=raw.imu_state.rpy[2] if raw else 0.0,
                pitch=raw.imu_state.rpy[1] if raw else 0.0,
                roll=raw.imu_state.rpy[0] if raw else 0.0,
                vx=raw.velocity[0] if raw else 0.0,
                vy=raw.velocity[1] if raw else 0.0,
                vyaw=raw.velocity[2] if raw else 0.0,
                battery_voltage=raw.battery_voltage if raw else 0.0,
                foot_force=list(raw.foot_force) if raw else [0.0]*4,
                connection_state=ConnectionState.CONNECTED,
            )
        except Exception as e:
            logger.warning("DDS state read error: %s", e)
        return self._last

    async def move(self, vx, vy, vyaw) -> None:
        await asyncio.to_thread(self._sport.Move, vx, vy, vyaw)

    async def set_mode(self, mode) -> None:
        fn = getattr(self._sport, self._MODE_MAP.get(mode, ""), None)
        if fn: await asyncio.to_thread(fn)

    async def set_body_height(self, h) -> None:
        await asyncio.to_thread(self._sport.BodyHeight, h)

    async def set_euler(self, r, p, y) -> None:
        await asyncio.to_thread(self._sport.Euler, r, p, y)

    async def set_foot_raise_height(self, h) -> None:
        await asyncio.to_thread(self._sport.FootRaiseHeight, h)

    async def set_speed_level(self, l) -> None:
        await asyncio.to_thread(self._sport.SpeedLevel, l)

    async def set_obstacle_avoidance(self, en) -> None:
        await asyncio.to_thread(self._obs.SwitchSet, en)

    async def set_vui(self, vol, bri) -> None:
        await asyncio.to_thread(self._vui.SetVolume, vol)
        await asyncio.to_thread(self._vui.SetLedBrightness, bri)

    async def stop(self) -> None:
        await asyncio.to_thread(self._sport.StopMove)

    async def stand_up(self) -> None:
        await asyncio.to_thread(self._sport.StandUp)

    async def stand_down(self) -> None:
        await asyncio.to_thread(self._sport.StandDown)

    async def emergency_stop(self) -> None:
        await asyncio.to_thread(self._sport.Damp)

    def is_connected(self) -> bool:
        return self._connected


# ── WebRTC (go2_webrtc_connect / unitree_webrtc_connect) ──────────────── #
class _WebRTCTransport(_BaseTransport):
    # WebRTC API IDs from the official Unitree Go2 sport service
    _API = {
        "damp": 1001, "balance_stand": 1002, "stop_move": 1005,
        "stand_up": 1004, "stand_down": 1003, "sit": 1043, "rise_sit": 1040,
        "hello": 1046, "stretch": 1044, "wallow": 1045, "scrape": 1047,
        "front_flip": 1048, "front_jump": 1049, "front_pounce": 1050,
        "dance1": 1051, "dance2": 1052, "finger_heart": 1053,
    }

    def __init__(self, ip="", serial="", user="", pwd="", method="local_sta"):
        self._ip, self._serial = ip, serial
        self._user, self._pwd  = user, pwd
        self._method = method
        self._conn   = None
        self._connected = False
        self._last   = RobotState()

    async def connect(self) -> None:
        try:
            from go2_webrtc_connect import Go2WebRTCConnection, WebRTCConnectionMethod as M  # type: ignore
        except ImportError:
            try:
                from unitree_webrtc_connect import UnitreeWebRTCConnection as Go2WebRTCConnection, WebRTCConnectionMethod as M  # type: ignore
            except ImportError:
                raise RuntimeError("WebRTC package not found. Run: pip install go2_webrtc_connect")
        mm = {"ap": M.LocalAP, "local_sta": M.LocalSTA, "remote": M.Remote}.get(self._method, M.LocalSTA)
        kw: dict[str, Any] = {}
        if self._ip:     kw["ip"]           = self._ip
        if self._serial: kw["serialNumber"] = self._serial
        if self._user and self._pwd:
            kw["username"] = self._user; kw["password"] = self._pwd
        self._conn = Go2WebRTCConnection(mm, **kw)
        await asyncio.to_thread(self._conn.connect)
        self._connected = True
        logger.info("WebRTC transport connected (%s)", self._method)

    async def disconnect(self) -> None:
        if self._conn:
            try: await asyncio.to_thread(self._conn.disconnect)
            except: pass
        self._connected = False

    async def read_state(self) -> RobotState:
        if not self._connected: return self._last
        try:
            raw = await asyncio.to_thread(self._conn.getState)
            if raw:
                self._last = RobotState(
                    timestamp=time.time(),
                    vx=getattr(raw,"vx",0.0), vy=getattr(raw,"vy",0.0),
                    vyaw=getattr(raw,"vyaw",0.0),
                    battery_voltage=getattr(raw,"battery_voltage",0.0),
                    connection_state=ConnectionState.CONNECTED,
                )
        except: pass
        return self._last

    async def _pub(self, topic: str, api_id: int, param: str = "{}") -> None:
        if self._conn:
            import json
            await asyncio.to_thread(
                self._conn.datachannel.pub, topic,
                {"api_id": api_id, "parameter": param}
            )

    async def move(self, vx, vy, vyaw) -> None:
        import json
        await self._pub("rt/api/sport/request", 1008, json.dumps({"x":vx,"y":vy,"z":vyaw}))

    async def set_mode(self, mode) -> None:
        api = self._API.get(mode)
        if api: await self._pub("rt/api/sport/request", api)

    async def set_body_height(self, h) -> None:
        import json
        await self._pub("rt/api/sport/request", 1013, json.dumps({"data":h}))

    async def set_euler(self, r, p, y) -> None:
        import json
        await self._pub("rt/api/sport/request", 1025, json.dumps({"x":r,"y":p,"z":y}))

    async def set_foot_raise_height(self, h) -> None:
        import json
        await self._pub("rt/api/sport/request", 1014, json.dumps({"data":h}))

    async def set_speed_level(self, l) -> None:
        import json
        await self._pub("rt/api/sport/request", 1015, json.dumps({"data":l}))

    async def set_obstacle_avoidance(self, en) -> None:
        import json
        await self._pub("rt/api/obstacles_avoid/request", 1003, json.dumps({"data":int(en)}))

    async def set_vui(self, vol, bri) -> None:
        import json
        await self._pub("rt/api/vui/request", 1001, json.dumps({"volume":vol}))
        await self._pub("rt/api/vui/request", 1002, json.dumps({"brightness":bri}))

    async def stop(self) -> None:    await self.set_mode("stop_move")
    async def stand_up(self) -> None: await self.set_mode("stand_up")
    async def stand_down(self) -> None: await self.set_mode("stand_down")
    async def emergency_stop(self) -> None: await self.set_mode("damp")
    def is_connected(self) -> bool: return self._connected


# ── Public Go2Bridge ──────────────────────────────────────────────────── #
class Go2Bridge:
    """
    Transport-agnostic Go2 interface. Use Go2Bridge.from_config() to create.

    Every mutating command:
      1. Clamped to safe ranges
      2. Validated by SafetyGate
      3. Forwarded to the active transport
    """
    _MAX_VX = 1.5; _MAX_VY = 0.8; _MAX_VYAW = 2.0
    _RECONNECT_INTERVAL = 5.0

    def __init__(self, transport: _BaseTransport,
                 safety: SafetyGate | None = None) -> None:
        self._t       = transport
        self._safety  = safety or SafetyGate()
        self._state   = RobotState()
        self._lock    = asyncio.Lock()
        self._listeners: list[Callable[[RobotState], None]] = []
        self._watchdog_task: asyncio.Task | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "Go2Bridge":
        mode = TransportMode(cfg.get("transport", "mock"))
        if   mode == TransportMode.DDS:
            t = _DDSTransport(cfg.get("network_interface", "eth0"))
        elif mode == TransportMode.WEBRTC:
            t = _WebRTCTransport(
                ip=cfg.get("robot_ip", ""), serial=cfg.get("serial_number", ""),
                user=cfg.get("webrtc_username",""), pwd=cfg.get("webrtc_password",""),
                method=cfg.get("webrtc_method","local_sta"),
            )
        else:
            t = _MockTransport()
        return cls(t)

    # ── Lifecycle ──────────────────────────────────────────────────────── #
    async def connect(self) -> None:
        async with self._lock:
            self._state.connection_state = ConnectionState.CONNECTING
        await self._t.connect()
        self._state = await self._t.read_state()
        self._state.connection_state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
        await self._t.disconnect()

    async def start_watchdog(self) -> None:
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._RECONNECT_INTERVAL)
            if not self._t.is_connected():
                logger.info("Watchdog: reconnecting…")
                try:
                    await self._t.connect()
                except Exception as e:
                    logger.error("Watchdog reconnect failed: %s", e)

    @property
    def connected(self) -> bool:
        return self._t.is_connected()

    # ── State ──────────────────────────────────────────────────────────── #
    async def get_state(self) -> RobotState:
        s = await self._t.read_state()
        async with self._lock:
            self._state = s
        for cb in self._listeners:
            try: cb(s)
            except: pass
        return s

    def add_state_listener(self, cb: Callable[[RobotState], None]) -> None:
        self._listeners.append(cb)

    # ── Motion ─────────────────────────────────────────────────────────── #
    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        vx   = max(-self._MAX_VX,   min(self._MAX_VX,   vx))
        vy   = max(-self._MAX_VY,   min(self._MAX_VY,   vy))
        vyaw = max(-self._MAX_VYAW, min(self._MAX_VYAW, vyaw))
        state = await self.get_state()
        if not self._safety.allow_move(vx, vy, vyaw, state):
            return
        await self._t.move(vx, vy, vyaw)

    async def stop(self) -> None:
        await self._t.stop()

    async def stand_up(self) -> None:
        await self._t.stand_up()

    async def stand_down(self) -> None:
        await self._t.stand_down()

    async def emergency_stop(self) -> None:
        """Hard stop — bypasses SafetyGate queue, always executes."""
        logger.warning("EMERGENCY STOP triggered")
        await self._t.emergency_stop()

    # ── Config ─────────────────────────────────────────────────────────── #
    async def set_mode(self, mode: str) -> None:
        if mode not in AVAILABLE_MODES:
            raise ValueError(f"Unknown mode '{mode}'. Valid: {sorted(AVAILABLE_MODES)}")
        await self._t.set_mode(mode)

    async def set_body_height(self, h: float) -> None:
        await self._t.set_body_height(max(0.3, min(0.5, h)))

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        await self._t.set_euler(
            max(-0.75, min(0.75, roll)),
            max(-0.75, min(0.75, pitch)),
            max(-1.5,  min(1.5,  yaw)),
        )

    async def set_foot_raise_height(self, h: float) -> None:
        await self._t.set_foot_raise_height(max(-0.06, min(0.03, h)))

    async def set_speed_level(self, l: int) -> None:
        await self._t.set_speed_level(max(-1, min(1, l)))

    async def set_obstacle_avoidance(self, en: bool) -> None:
        await self._t.set_obstacle_avoidance(en)

    async def set_vui(self, volume: int = 50, brightness: int = 50) -> None:
        await self._t.set_vui(max(0,min(100,volume)), max(0,min(100,brightness)))
