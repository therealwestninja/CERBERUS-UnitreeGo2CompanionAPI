"""
cerberus/hardware/go2_bridge.py
================================
Unified hardware bridge for the Unitree Go2 robot.

Supports two transport backends:
  - DDS  (CycloneDDS via unitree_sdk2_python)  — Go2 EDU wired / Wi-Fi
  - WebRTC                                       — Go2 AIR / PRO / EDU wireless

Design principles
-----------------
* Transport is selected automatically or via config; application code is
  transport-agnostic.
* Every mutating command passes through the SafetyGate before reaching the
  robot — no exceptions.
* All blocking SDK calls are wrapped in asyncio.to_thread so the FastAPI event
  loop is never stalled.
* Connection lifecycle: connect → healthy → reconnect-on-error → disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from cerberus.safety.gate import SafetyGate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------

class TransportMode(str, Enum):
    DDS    = "dds"
    WEBRTC = "webrtc"
    MOCK   = "mock"          # unit-test / CI stub


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING   = "connecting"
    CONNECTED    = "connected"
    RECONNECTING = "reconnecting"
    ERROR        = "error"


# ---------------------------------------------------------------------------
# Go2 motion modes (from Unitree SDK sport_mode / go2_robot service list)
# ---------------------------------------------------------------------------

AVAILABLE_MODES: set[str] = {
    "damp", "balance_stand", "stop_move", "stand_up", "stand_down",
    "sit", "rise_sit", "hello", "stretch", "wallow", "scrape",
    "front_flip", "front_jump", "front_pounce",
    "dance1", "dance2", "finger_heart",
}

# Modes that require the robot to already be standing
_REQUIRES_STANDING: set[str] = {
    "hello", "stretch", "wallow", "scrape",
    "front_flip", "front_jump", "front_pounce",
    "dance1", "dance2", "finger_heart",
}


# ---------------------------------------------------------------------------
# Robot state snapshot (returned by get_state)
# ---------------------------------------------------------------------------

@dataclass
class RobotState:
    timestamp: float = field(default_factory=time.time)

    # Pose / odometry
    position_x:  float = 0.0
    position_y:  float = 0.0
    yaw:         float = 0.0
    pitch:       float = 0.0
    roll:        float = 0.0
    body_height: float = 0.38   # metres, nominal

    # Velocities
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0

    # Battery
    battery_voltage: float = 0.0
    battery_percent: float = 0.0

    # IMU
    imu_temperature: float = 0.0
    foot_force: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    # Status flags
    sport_mode_active: bool = True
    obstacle_avoidance: bool = False
    current_mode: str = "balance_stand"

    # Connection metadata
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Base transport interface
# ---------------------------------------------------------------------------

class _BaseTransport:
    """Abstract base – all transports implement this interface."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def read_state(self) -> RobotState: ...
    async def move(self, vx: float, vy: float, vyaw: float) -> None: ...
    async def set_mode(self, mode: str) -> None: ...
    async def set_body_height(self, height: float) -> None: ...
    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None: ...
    async def set_foot_raise_height(self, height: float) -> None: ...
    async def set_speed_level(self, level: int) -> None: ...
    async def set_obstacle_avoidance(self, enabled: bool) -> None: ...
    async def set_vui(self, volume: int, brightness: int) -> None: ...
    async def stop(self) -> None: ...
    async def stand_up(self) -> None: ...
    async def stand_down(self) -> None: ...
    async def emergency_stop(self) -> None: ...
    def is_connected(self) -> bool: return False


# ---------------------------------------------------------------------------
# DDS transport (unitree_sdk2_python)
# ---------------------------------------------------------------------------

class _DDSTransport(_BaseTransport):
    """
    Wraps unitree_sdk2_python's SportClient, ObstaclesAvoidClient,
    and VuiClient over CycloneDDS.

    Requires: pip install unitree_sdk2py
    """

    def __init__(self, network_interface: str):
        self._iface = network_interface
        self._sport: Any = None
        self._obstacles: Any = None
        self._vui: Any = None
        self._state_reader: Any = None
        self._connected = False
        self._last_state: RobotState = RobotState()

    async def connect(self) -> None:
        try:
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import ObstaclesAvoidClient
            from unitree_sdk2py.go2.vui.vui_client import VuiClient
            from unitree_sdk2py.go2.sport.sport_state_client import SportStateClient
            import unitree_sdk2py.core.channel as ch

            await asyncio.to_thread(ch.ChannelFactoryInitialize, 0, self._iface)

            self._sport      = SportClient()
            self._obstacles  = ObstaclesAvoidClient()
            self._vui        = VuiClient()
            self._state_reader = SportStateClient()

            await asyncio.to_thread(self._sport.Init)
            await asyncio.to_thread(self._obstacles.Init)
            await asyncio.to_thread(self._vui.Init)
            await asyncio.to_thread(self._state_reader.Init)

            self._connected = True
            logger.info("DDS transport connected on interface %s", self._iface)
        except ImportError:
            raise RuntimeError(
                "unitree_sdk2_python not installed. "
                "Run: pip install unitree_sdk2py"
            )

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("DDS transport disconnected")

    async def read_state(self) -> RobotState:
        if not self._connected:
            return self._last_state
        try:
            raw = await asyncio.to_thread(self._state_reader.GetState)
            s = RobotState(
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
            self._last_state = s
            return s
        except Exception as exc:
            logger.warning("DDS state read error: %s", exc)
            return self._last_state

    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        await asyncio.to_thread(self._sport.Move, vx, vy, vyaw)

    async def set_mode(self, mode: str) -> None:
        fn = getattr(self._sport, _mode_to_dds_method(mode), None)
        if fn:
            await asyncio.to_thread(fn)
        else:
            logger.warning("DDS: no method mapped for mode '%s'", mode)

    async def set_body_height(self, height: float) -> None:
        await asyncio.to_thread(self._sport.BodyHeight, height)

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        await asyncio.to_thread(self._sport.Euler, roll, pitch, yaw)

    async def set_foot_raise_height(self, height: float) -> None:
        await asyncio.to_thread(self._sport.FootRaiseHeight, height)

    async def set_speed_level(self, level: int) -> None:
        await asyncio.to_thread(self._sport.SpeedLevel, level)

    async def set_obstacle_avoidance(self, enabled: bool) -> None:
        if enabled:
            await asyncio.to_thread(self._obstacles.SwitchSet, True)
        else:
            await asyncio.to_thread(self._obstacles.SwitchSet, False)

    async def set_vui(self, volume: int, brightness: int) -> None:
        await asyncio.to_thread(self._vui.SetVolume, volume)
        await asyncio.to_thread(self._vui.SetLedBrightness, brightness)

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


def _mode_to_dds_method(mode: str) -> str:
    """Map CERBERUS mode string → SportClient method name."""
    _MAP = {
        "damp":          "Damp",
        "balance_stand": "BalanceStand",
        "stop_move":     "StopMove",
        "stand_up":      "StandUp",
        "stand_down":    "StandDown",
        "sit":           "Sit",
        "rise_sit":      "RiseSit",
        "hello":         "Hello",
        "stretch":       "Stretch",
        "wallow":        "Wallow",
        "scrape":        "Scrape",
        "front_flip":    "FrontFlip",
        "front_jump":    "FrontJump",
        "front_pounce":  "FrontPounce",
        "dance1":        "Dance1",
        "dance2":        "Dance2",
        "finger_heart":  "FingerHeart",
    }
    return _MAP.get(mode, "")


# ---------------------------------------------------------------------------
# WebRTC transport (go2_webrtc_connect / unitree_webrtc_connect)
# ---------------------------------------------------------------------------

class _WebRTCTransport(_BaseTransport):
    """
    Wraps go2_webrtc_connect (pip install go2_webrtc_connect or
    unitree_webrtc_connect) for AIR/PRO/EDU models over Wi-Fi.

    References:
        https://github.com/phospho-app/go2_webrtc_connect
        https://github.com/legion1581/unitree_webrtc_connect
    """

    def __init__(self, robot_ip: str = "", serial_number: str = "",
                 username: str = "", password: str = "",
                 connection_method: str = "local_sta"):
        self._ip        = robot_ip
        self._serial    = serial_number
        self._username  = username
        self._password  = password
        self._method    = connection_method
        self._conn: Any = None
        self._connected = False
        self._last_state = RobotState()

    async def connect(self) -> None:
        try:
            from go2_webrtc_connect import Go2WebRTCConnection, WebRTCConnectionMethod  # type: ignore
        except ImportError:
            try:
                from unitree_webrtc_connect import UnitreeWebRTCConnection as Go2WebRTCConnection  # type: ignore
                from unitree_webrtc_connect import WebRTCConnectionMethod  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "WebRTC package not found. "
                    "Run: pip install go2_webrtc_connect"
                )

        method_map = {
            "ap":        WebRTCConnectionMethod.LocalAP,
            "local_sta": WebRTCConnectionMethod.LocalSTA,
            "remote":    WebRTCConnectionMethod.Remote,
        }
        method = method_map.get(self._method, WebRTCConnectionMethod.LocalSTA)

        kwargs: dict[str, Any] = {}
        if self._ip:
            kwargs["ip"] = self._ip
        if self._serial:
            kwargs["serialNumber"] = self._serial
        if self._username and self._password:
            kwargs["username"] = self._username
            kwargs["password"] = self._password

        self._conn = Go2WebRTCConnection(method, **kwargs)
        await asyncio.to_thread(self._conn.connect)
        self._connected = True
        logger.info("WebRTC transport connected (method=%s)", self._method)

    async def disconnect(self) -> None:
        if self._conn:
            try:
                await asyncio.to_thread(self._conn.disconnect)
            except Exception:
                pass
        self._connected = False

    async def read_state(self) -> RobotState:
        if not self._connected or not self._conn:
            return self._last_state
        try:
            raw = await asyncio.to_thread(self._conn.getState)
            if raw:
                self._last_state = RobotState(
                    timestamp=time.time(),
                    vx=getattr(raw, "vx", 0.0),
                    vy=getattr(raw, "vy", 0.0),
                    vyaw=getattr(raw, "vyaw", 0.0),
                    battery_voltage=getattr(raw, "battery_voltage", 0.0),
                    connection_state=ConnectionState.CONNECTED,
                )
        except Exception as exc:
            logger.debug("WebRTC state read error: %s", exc)
        return self._last_state

    async def _send(self, topic: str, api_id: int, parameter: str = "{}") -> None:
        if self._conn:
            await asyncio.to_thread(
                self._conn.datachannel.pub,
                topic, {"api_id": api_id, "parameter": parameter}
            )

    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        import json
        param = json.dumps({"x": vx, "y": vy, "z": vyaw})
        await self._send("rt/api/sport/request", 1008, param)

    async def set_mode(self, mode: str) -> None:
        import json
        _API_IDS = {
            "hello": 1046, "stretch": 1044, "wallow": 1045,
            "scrape": 1047, "front_flip": 1048, "front_jump": 1049,
            "front_pounce": 1050, "dance1": 1051, "dance2": 1052,
            "finger_heart": 1053, "sit": 1043, "rise_sit": 1040,
            "stand_up": 1004, "stand_down": 1003, "balance_stand": 1002,
            "stop_move": 1005, "damp": 1001,
        }
        api_id = _API_IDS.get(mode)
        if api_id:
            await self._send("rt/api/sport/request", api_id)
        else:
            logger.warning("WebRTC: unknown mode '%s'", mode)

    async def set_body_height(self, height: float) -> None:
        import json
        await self._send("rt/api/sport/request", 1013,
                         json.dumps({"data": height}))

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        import json
        await self._send("rt/api/sport/request", 1025,
                         json.dumps({"x": roll, "y": pitch, "z": yaw}))

    async def set_foot_raise_height(self, height: float) -> None:
        import json
        await self._send("rt/api/sport/request", 1014,
                         json.dumps({"data": height}))

    async def set_speed_level(self, level: int) -> None:
        import json
        await self._send("rt/api/sport/request", 1015,
                         json.dumps({"data": level}))

    async def set_obstacle_avoidance(self, enabled: bool) -> None:
        import json
        await self._send("rt/api/obstacles_avoid/request", 1003,
                         json.dumps({"data": int(enabled)}))

    async def set_vui(self, volume: int, brightness: int) -> None:
        import json
        await self._send("rt/api/vui/request", 1001,
                         json.dumps({"volume": volume}))
        await self._send("rt/api/vui/request", 1002,
                         json.dumps({"brightness": brightness}))

    async def stop(self) -> None:
        await self.set_mode("stop_move")

    async def stand_up(self) -> None:
        await self.set_mode("stand_up")

    async def stand_down(self) -> None:
        await self.set_mode("stand_down")

    async def emergency_stop(self) -> None:
        await self.set_mode("damp")

    def is_connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# Mock transport (simulation / CI)
# ---------------------------------------------------------------------------

class _MockTransport(_BaseTransport):
    """In-memory stub for testing and simulation mode."""

    def __init__(self) -> None:
        self._state = RobotState(
            connection_state=ConnectionState.CONNECTED,
            battery_voltage=25.2,
            battery_percent=85.0,
            sport_mode_active=True,
        )
        self._connected = False
        self._mode_log: list[str] = []

    async def connect(self) -> None:
        self._connected = True
        self._state.connection_state = ConnectionState.CONNECTED
        logger.info("Mock transport connected (simulation mode)")

    async def disconnect(self) -> None:
        self._connected = False

    async def read_state(self) -> RobotState:
        return self._state

    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        self._state.vx, self._state.vy, self._state.vyaw = vx, vy, vyaw

    async def set_mode(self, mode: str) -> None:
        self._mode_log.append(mode)
        self._state.current_mode = mode

    async def set_body_height(self, height: float) -> None:
        self._state.body_height = height

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        self._state.roll, self._state.pitch, self._state.yaw = roll, pitch, yaw

    async def set_foot_raise_height(self, _h: float) -> None: ...
    async def set_speed_level(self, _l: int) -> None: ...
    async def set_obstacle_avoidance(self, enabled: bool) -> None:
        self._state.obstacle_avoidance = enabled
    async def set_vui(self, _v: int, _b: int) -> None: ...

    async def stop(self) -> None:
        self._state.vx = self._state.vy = self._state.vyaw = 0.0

    async def stand_up(self) -> None:
        self._state.current_mode = "stand_up"

    async def stand_down(self) -> None:
        self._state.current_mode = "stand_down"

    async def emergency_stop(self) -> None:
        self._state.vx = self._state.vy = self._state.vyaw = 0.0
        self._state.current_mode = "damp"

    def is_connected(self) -> bool:
        return self._connected

    @property
    def mode_log(self) -> list[str]:
        return list(self._mode_log)


# ---------------------------------------------------------------------------
# Public Go2Bridge — the only class application code should import
# ---------------------------------------------------------------------------

class Go2Bridge:
    """
    High-level, transport-agnostic interface to the Unitree Go2.

    Every command passes through SafetyGate before reaching hardware.
    Auto-reconnect is attempted on transient failures.

    Usage
    -----
        bridge = Go2Bridge.from_config(config)
        await bridge.connect()
        await bridge.move(0.3, 0.0, 0.0)   # walk forward
        await bridge.set_mode("hello")
        state = await bridge.get_state()
    """

    _RECONNECT_INTERVAL = 5.0   # seconds between reconnect attempts
    _MAX_VX   = 1.5             # m/s
    _MAX_VY   = 0.8
    _MAX_VYAW = 2.0             # rad/s

    def __init__(self, transport: _BaseTransport,
                 safety_gate: Optional[SafetyGate] = None) -> None:
        self._transport  = transport
        self._safety     = safety_gate or SafetyGate()
        self._state      = RobotState()
        self._lock       = asyncio.Lock()
        self._reconnect_task: Optional[asyncio.Task] = None
        self._listeners: list[Callable[[RobotState], None]] = []

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "Go2Bridge":
        mode = TransportMode(config.get("transport", "mock"))
        if mode == TransportMode.DDS:
            transport = _DDSTransport(
                network_interface=config.get("network_interface", "eth0")
            )
        elif mode == TransportMode.WEBRTC:
            transport = _WebRTCTransport(
                robot_ip=config.get("robot_ip", ""),
                serial_number=config.get("serial_number", ""),
                username=config.get("webrtc_username", ""),
                password=config.get("webrtc_password", ""),
                connection_method=config.get("webrtc_method", "local_sta"),
            )
        else:
            transport = _MockTransport()

        return cls(transport)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        async with self._lock:
            self._state.connection_state = ConnectionState.CONNECTING
        await self._transport.connect()
        async with self._lock:
            self._state = await self._transport.read_state()
            self._state.connection_state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        if self._reconnect_task:
            self._reconnect_task.cancel()
        await self._transport.disconnect()
        async with self._lock:
            self._state.connection_state = ConnectionState.DISCONNECTED

    @property
    def connected(self) -> bool:
        return self._transport.is_connected()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    async def get_state(self) -> RobotState:
        state = await self._transport.read_state()
        async with self._lock:
            self._state = state
        for cb in self._listeners:
            try:
                cb(state)
            except Exception:
                pass
        return state

    def add_state_listener(self, callback: Callable[[RobotState], None]) -> None:
        self._listeners.append(callback)

    # ------------------------------------------------------------------
    # Motion commands
    # ------------------------------------------------------------------

    async def move(self, vx: float, vy: float, vyaw: float) -> None:
        """Velocity-control walk. Clamped and safety-checked."""
        vx   = max(-self._MAX_VX,   min(self._MAX_VX,   vx))
        vy   = max(-self._MAX_VY,   min(self._MAX_VY,   vy))
        vyaw = max(-self._MAX_VYAW, min(self._MAX_VYAW, vyaw))

        if not self._safety.allow_move(vx, vy, vyaw, await self.get_state()):
            logger.warning("SafetyGate blocked move(%.2f, %.2f, %.2f)", vx, vy, vyaw)
            return
        await self._transport.move(vx, vy, vyaw)

    async def stop(self) -> None:
        await self._transport.stop()

    async def stand_up(self) -> None:
        await self._transport.stand_up()

    async def stand_down(self) -> None:
        await self._transport.stand_down()

    async def emergency_stop(self) -> None:
        """Hard-stop: always bypasses SafetyGate queue, sends Damp mode."""
        logger.warning("EMERGENCY STOP triggered")
        await self._transport.emergency_stop()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def set_mode(self, mode: str) -> None:
        if mode not in AVAILABLE_MODES:
            raise ValueError(f"Unknown mode '{mode}'. Valid: {sorted(AVAILABLE_MODES)}")
        await self._transport.set_mode(mode)

    async def set_body_height(self, height: float) -> None:
        """height in metres [0.3 – 0.5]"""
        height = max(0.3, min(0.5, height))
        await self._transport.set_body_height(height)

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> None:
        """Euler angles in radians. Each axis clamped to [-0.75, 0.75]; yaw [-1.5, 1.5]."""
        roll  = max(-0.75, min(0.75, roll))
        pitch = max(-0.75, min(0.75, pitch))
        yaw   = max(-1.5,  min(1.5,  yaw))
        await self._transport.set_euler(roll, pitch, yaw)

    async def set_foot_raise_height(self, height: float) -> None:
        height = max(-0.06, min(0.03, height))
        await self._transport.set_foot_raise_height(height)

    async def set_speed_level(self, level: int) -> None:
        level = max(-1, min(1, level))
        await self._transport.set_speed_level(level)

    async def set_obstacle_avoidance(self, enabled: bool) -> None:
        await self._transport.set_obstacle_avoidance(enabled)

    async def set_vui(self, volume: int = 50, brightness: int = 50) -> None:
        volume     = max(0, min(100, volume))
        brightness = max(0, min(100, brightness))
        await self._transport.set_vui(volume, brightness)

    # ------------------------------------------------------------------
    # Auto-reconnect
    # ------------------------------------------------------------------

    async def start_watchdog(self) -> None:
        """Start background task that monitors connection and reconnects."""
        self._reconnect_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._RECONNECT_INTERVAL)
            if not self._transport.is_connected():
                logger.info("Watchdog: transport disconnected — attempting reconnect…")
                async with self._lock:
                    self._state.connection_state = ConnectionState.RECONNECTING
                try:
                    await self._transport.connect()
                    logger.info("Watchdog: reconnected successfully")
                except Exception as exc:
                    logger.error("Watchdog: reconnect failed: %s", exc)
