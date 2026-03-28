"""
cerberus/bridge/go2_bridge.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Primary bridge between CERBERUS and the Unitree Go2 robot.

Uses the official unitree_sdk2_python DDS layer (CycloneDDS pub/sub),
NOT direct HTTP/IP. Falls back to a SimBridge when GO2_SIMULATION=true.

Communication pattern:
  ChannelFactoryInitialize(0, interface) → SportClient → DDS topics
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


# ── Sport mode registry ────────────────────────────────────────────────────────

class SportMode(str, Enum):
    DAMP           = "damp"
    BALANCE_STAND  = "balance_stand"
    STOP_MOVE      = "stop_move"
    STAND_UP       = "stand_up"
    STAND_DOWN     = "stand_down"
    SIT            = "sit"
    RISE_SIT       = "rise_sit"
    HELLO          = "hello"
    STRETCH        = "stretch"
    WALLOW         = "wallow"
    SCRAPE         = "scrape"
    FRONT_FLIP     = "front_flip"
    FRONT_JUMP     = "front_jump"
    FRONT_POUNCE   = "front_pounce"
    DANCE1         = "dance1"
    DANCE2         = "dance2"
    FINGER_HEART   = "finger_heart"


# ── Robot state snapshot ───────────────────────────────────────────────────────

@dataclass
class RobotState:
    """Snapshot of the robot's current state (populated from DDS highstate)."""
    timestamp: float = field(default_factory=time.time)
    # Motion
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    velocity_yaw: float = 0.0
    body_height: float = 0.27
    # IMU
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    imu_acc_x: float = 0.0
    imu_acc_y: float = 0.0
    imu_acc_z: float = -9.81
    # Power
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    battery_percent: float = 100.0
    # Status
    mode: str = "idle"
    foot_force: list[float] = field(default_factory=lambda: [0.0] * 4)
    joint_positions: list[float] = field(default_factory=lambda: [0.0] * 12)
    joint_velocities: list[float] = field(default_factory=lambda: [0.0] * 12)
    joint_torques: list[float] = field(default_factory=lambda: [0.0] * 12)
    # Safety
    estop_active: bool = False
    obstacle_avoidance: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "velocity": {"x": self.velocity_x, "y": self.velocity_y, "yaw": self.velocity_yaw},
            "body_height": self.body_height,
            "imu": {"roll": self.roll, "pitch": self.pitch, "yaw": self.yaw,
                    "acc": [self.imu_acc_x, self.imu_acc_y, self.imu_acc_z]},
            "battery": {"voltage": self.battery_voltage, "current": self.battery_current,
                        "percent": self.battery_percent},
            "mode": self.mode,
            "foot_force": self.foot_force,
            "joints": {"positions": self.joint_positions,
                       "velocities": self.joint_velocities,
                       "torques": self.joint_torques},
            "estop_active": self.estop_active,
            "obstacle_avoidance": self.obstacle_avoidance,
        }


# ── Base bridge interface ──────────────────────────────────────────────────────

class BridgeBase:
    """Abstract interface — implemented by RealBridge or SimBridge."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def get_state(self) -> RobotState: ...

    # Motion
    async def stand_up(self) -> bool: ...
    async def stand_down(self) -> bool: ...
    async def move(self, vx: float, vy: float, vyaw: float) -> bool: ...
    async def stop_move(self) -> bool: ...
    async def set_body_height(self, height: float) -> bool: ...
    async def set_speed_level(self, level: int) -> bool: ...
    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool: ...
    async def switch_gait(self, gait_id: int) -> bool: ...
    async def set_foot_raise_height(self, height: float) -> bool: ...
    async def set_continuous_gait(self, enabled: bool) -> bool: ...

    # Sport modes
    async def execute_sport_mode(self, mode: SportMode) -> bool: ...

    # Safety
    async def emergency_stop(self) -> bool: ...
    async def set_obstacle_avoidance(self, enabled: bool) -> bool: ...

    # LED / VUI
    async def set_led(self, r: int, g: int, b: int) -> bool: ...
    async def set_volume(self, level: int) -> bool: ...


# ── Real hardware bridge (uses unitree_sdk2_python) ────────────────────────────

class RealBridge(BridgeBase):
    """
    Connects to the physical Go2 via CycloneDDS (unitree_sdk2_python).

    Required env:
        GO2_NETWORK_INTERFACE  — e.g. "eth0" or "enp2s0"
    """

    def __init__(self, network_interface: str):
        self._iface = network_interface
        self._sport_client = None
        self._state_sub = None
        self._state = RobotState()
        self._connected = False
        self._state_callbacks: list[Callable[[RobotState], None]] = []

    async def connect(self) -> None:
        try:
            # Import here so the rest of CERBERUS works without the SDK installed
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            from unitree_sdk2py.go2.sport.sport_api import (
                SportApi,
            )
            from unitree_sdk2py.idl.default import (
                unitree_go_msg_dds__SportModeState_,
            )
            from unitree_sdk2py.core.channel import ChannelSubscriber

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._init_dds)
            self._connected = True
            logger.info("Go2 real bridge connected via DDS on %s", self._iface)
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2py not installed. Run: pip install unitree-sdk2py"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to connect to Go2: {exc}") from exc

    def _init_dds(self) -> None:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.sport.sport_client import SportClient

        ChannelFactoryInitialize(0, self._iface)
        self._sport_client = SportClient()
        self._sport_client.SetTimeout(10.0)
        self._sport_client.Init()

        # Subscribe to high-level state topic
        try:
            from unitree_sdk2py.core.channel import ChannelSubscriber
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
            self._state_sub = ChannelSubscriber("rt/sportmodestate", SportModeState_)
            self._state_sub.Init(self._on_state_update, 10)
        except Exception as e:
            logger.warning("State subscription failed (non-fatal): %s", e)

    def _on_state_update(self, msg) -> None:
        """DDS callback — update robot state from highstate message."""
        try:
            s = self._state
            s.timestamp = time.time()
            s.velocity_x = getattr(msg, "velocity", [0, 0, 0])[0]
            s.velocity_y = getattr(msg, "velocity", [0, 0, 0])[1]
            s.velocity_yaw = getattr(msg, "yaw_speed", 0.0)
            s.body_height = getattr(msg, "body_height", 0.27)
            imu = getattr(msg, "imu_state", None)
            if imu:
                rpy = getattr(imu, "rpy", [0, 0, 0])
                s.roll, s.pitch, s.yaw = rpy[0], rpy[1], rpy[2]
                acc = getattr(imu, "accelerometer", [0, 0, -9.81])
                s.imu_acc_x, s.imu_acc_y, s.imu_acc_z = acc[0], acc[1], acc[2]
            motors = getattr(msg, "motor_state", [])
            if motors:
                s.joint_positions  = [m.q   for m in motors[:12]]
                s.joint_velocities = [m.dq  for m in motors[:12]]
                s.joint_torques    = [m.tau_est for m in motors[:12]]
            foot = getattr(msg, "foot_force_est", [0, 0, 0, 0])
            s.foot_force = list(foot[:4])
        except Exception as e:
            logger.debug("State parse error: %s", e)

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("Go2 real bridge disconnected")

    async def get_state(self) -> RobotState:
        return self._state

    # ── Motion helpers ──────────────────────────────────────────────────────────

    def _run_sync(self, fn, *args):
        """Run a blocking SDK call in a thread pool."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, fn, *args)

    async def stand_up(self) -> bool:
        code, _ = await self._run_sync(self._sport_client.StandUp)
        return code == 0

    async def stand_down(self) -> bool:
        code, _ = await self._run_sync(self._sport_client.StandDown)
        return code == 0

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        # Clamp to safe limits
        vx    = max(-1.5, min(1.5, vx))
        vy    = max(-0.8, min(0.8, vy))
        vyaw  = max(-2.0, min(2.0, vyaw))
        code, _ = await self._run_sync(self._sport_client.Move, vx, vy, vyaw)
        return code == 0

    async def stop_move(self) -> bool:
        code, _ = await self._run_sync(self._sport_client.StopMove)
        return code == 0

    async def set_body_height(self, height: float) -> bool:
        height = max(-0.1, min(0.1, height))  # relative offset
        code, _ = await self._run_sync(self._sport_client.BodyHeight, height)
        return code == 0

    async def set_speed_level(self, level: int) -> bool:
        level = max(-1, min(1, level))
        code, _ = await self._run_sync(self._sport_client.SpeedLevel, level)
        return code == 0

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        roll  = max(-0.75, min(0.75, roll))
        pitch = max(-0.75, min(0.75, pitch))
        yaw   = max(-1.5,  min(1.5,  yaw))
        code, _ = await self._run_sync(self._sport_client.Euler, roll, pitch, yaw)
        return code == 0

    async def switch_gait(self, gait_id: int) -> bool:
        gait_id = max(0, min(4, gait_id))
        code, _ = await self._run_sync(self._sport_client.SwitchGait, gait_id)
        return code == 0

    async def set_foot_raise_height(self, height: float) -> bool:
        height = max(-0.06, min(0.03, height))
        code, _ = await self._run_sync(self._sport_client.FootRaiseHeight, height)
        return code == 0

    async def set_continuous_gait(self, enabled: bool) -> bool:
        code, _ = await self._run_sync(self._sport_client.ContinuousGait, enabled)
        return code == 0

    async def execute_sport_mode(self, mode: SportMode) -> bool:
        _mode_map = {
            SportMode.DAMP:          self._sport_client.Damp,
            SportMode.BALANCE_STAND: self._sport_client.BalanceStand,
            SportMode.STOP_MOVE:     self._sport_client.StopMove,
            SportMode.STAND_UP:      self._sport_client.StandUp,
            SportMode.STAND_DOWN:    self._sport_client.StandDown,
            SportMode.SIT:           self._sport_client.Sit,
            SportMode.RISE_SIT:      self._sport_client.RiseSit,
            SportMode.HELLO:         self._sport_client.Hello,
            SportMode.STRETCH:       self._sport_client.Stretch,
            SportMode.WALLOW:        self._sport_client.Wallow,
            SportMode.SCRAPE:        self._sport_client.Scrape,
            SportMode.FRONT_FLIP:    self._sport_client.FrontFlip,
            SportMode.FRONT_JUMP:    self._sport_client.FrontJump,
            SportMode.FRONT_POUNCE:  self._sport_client.FrontPounce,
            SportMode.DANCE1:        self._sport_client.Dance1,
            SportMode.DANCE2:        self._sport_client.Dance2,
            SportMode.FINGER_HEART:  self._sport_client.FingerHeart,
        }
        fn = _mode_map.get(mode)
        if fn is None:
            logger.error("Unknown sport mode: %s", mode)
            return False
        code, _ = await self._run_sync(fn)
        return code == 0

    async def emergency_stop(self) -> bool:
        """Hard stop — damp all motors, disable sport mode."""
        logger.critical("EMERGENCY STOP TRIGGERED")
        self._state.estop_active = True
        code, _ = await self._run_sync(self._sport_client.Damp)
        return code == 0

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        try:
            from unitree_sdk2py.go2.obstacles_avoid.obstacles_avoid_client import (
                ObstaclesAvoidClient,
            )
            client = ObstaclesAvoidClient()
            client.Init()
            if enabled:
                code, _ = await self._run_sync(client.SwitchSet, True)
            else:
                code, _ = await self._run_sync(client.SwitchSet, False)
            self._state.obstacle_avoidance = enabled
            return code == 0
        except Exception as e:
            logger.warning("Obstacle avoidance not available: %s", e)
            return False

    async def set_led(self, r: int, g: int, b: int) -> bool:
        try:
            from unitree_sdk2py.go2.vui.vui_client import VuiClient
            client = VuiClient()
            client.Init()
            code, _ = await self._run_sync(client.SetLedColor, r, g, b)
            return code == 0
        except Exception as e:
            logger.warning("LED control not available: %s", e)
            return False

    async def set_volume(self, level: int) -> bool:
        try:
            from unitree_sdk2py.go2.vui.vui_client import VuiClient
            client = VuiClient()
            client.Init()
            code, _ = await self._run_sync(client.SetVolume, max(0, min(100, level)))
            return code == 0
        except Exception as e:
            logger.warning("Volume control not available: %s", e)
            return False


# ── Simulation bridge ──────────────────────────────────────────────────────────

class SimBridge(BridgeBase):
    """
    Full simulation bridge — no hardware required.
    Logs all commands, simulates state updates, integrates with
    unitree_mujoco if available.
    """

    def __init__(self):
        self._state = RobotState(mode="sim_idle", battery_percent=100.0)
        self._connected = False
        self._mujoco_sim = None
        self._tick = 0

    async def connect(self) -> None:
        self._connected = True
        logger.info("CERBERUS SimBridge connected (no hardware)")
        self._start_state_sim()

    def _start_state_sim(self) -> None:
        """Simulate state drift — realistic battery drain, IMU noise, etc."""
        asyncio.ensure_future(self._sim_loop())

    async def _sim_loop(self) -> None:
        import math
        import random
        while self._connected:
            self._tick += 1
            t = self._tick * 0.033  # ~30Hz
            self._state.timestamp = time.time()
            # Simulate IMU noise
            self._state.roll  = math.sin(t * 0.5) * 0.02 + random.gauss(0, 0.001)
            self._state.pitch = math.sin(t * 0.3) * 0.015 + random.gauss(0, 0.001)
            # Battery drain
            self._state.battery_percent = max(0.0, 100.0 - self._tick * 0.001)
            self._state.battery_voltage = 24.0 * (self._state.battery_percent / 100.0) + 10.0
            await asyncio.sleep(0.033)

    async def disconnect(self) -> None:
        self._connected = False

    async def get_state(self) -> RobotState:
        return self._state

    async def stand_up(self) -> bool:
        self._state.mode = "standing"
        self._state.body_height = 0.35
        logger.info("[SIM] Stand up")
        return True

    async def stand_down(self) -> bool:
        self._state.mode = "lying"
        self._state.body_height = 0.0
        logger.info("[SIM] Stand down")
        return True

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        self._state.velocity_x = vx
        self._state.velocity_y = vy
        self._state.velocity_yaw = vyaw
        self._state.mode = "moving"
        logger.info("[SIM] Move vx=%.2f vy=%.2f vyaw=%.2f", vx, vy, vyaw)
        return True

    async def stop_move(self) -> bool:
        self._state.velocity_x = 0.0
        self._state.velocity_y = 0.0
        self._state.velocity_yaw = 0.0
        self._state.mode = "standing"
        logger.info("[SIM] Stop")
        return True

    async def set_body_height(self, height: float) -> bool:
        self._state.body_height = 0.27 + height
        logger.info("[SIM] Body height offset %.2f", height)
        return True

    async def set_speed_level(self, level: int) -> bool:
        logger.info("[SIM] Speed level %d", level)
        return True

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        self._state.roll = roll
        self._state.pitch = pitch
        logger.info("[SIM] Euler roll=%.2f pitch=%.2f yaw=%.2f", roll, pitch, yaw)
        return True

    async def switch_gait(self, gait_id: int) -> bool:
        logger.info("[SIM] Gait %d", gait_id)
        return True

    async def set_foot_raise_height(self, height: float) -> bool:
        logger.info("[SIM] Foot raise height %.3f", height)
        return True

    async def set_continuous_gait(self, enabled: bool) -> bool:
        logger.info("[SIM] Continuous gait: %s", enabled)
        return True

    async def execute_sport_mode(self, mode: SportMode) -> bool:
        self._state.mode = mode.value
        logger.info("[SIM] Sport mode: %s", mode.value)
        return True

    async def emergency_stop(self) -> bool:
        self._state.estop_active = True
        self._state.velocity_x = 0.0
        self._state.velocity_y = 0.0
        self._state.velocity_yaw = 0.0
        self._state.mode = "estop"
        logger.critical("[SIM] EMERGENCY STOP")
        return True

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        self._state.obstacle_avoidance = enabled
        logger.info("[SIM] Obstacle avoidance: %s", enabled)
        return True

    async def set_led(self, r: int, g: int, b: int) -> bool:
        logger.info("[SIM] LED rgb(%d,%d,%d)", r, g, b)
        return True

    async def set_volume(self, level: int) -> bool:
        logger.info("[SIM] Volume: %d", level)
        return True


# ── Factory ───────────────────────────────────────────────────────────────────

def create_bridge() -> BridgeBase:
    """Instantiate the correct bridge based on environment variables."""
    simulation = os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1", "yes")
    if simulation:
        logger.info("Creating SimBridge (GO2_SIMULATION=true)")
        return SimBridge()
    iface = os.getenv("GO2_NETWORK_INTERFACE", os.getenv("GO2_IFACE", "eth0"))
    logger.info("Creating RealBridge on interface '%s'", iface)
    return RealBridge(iface)
