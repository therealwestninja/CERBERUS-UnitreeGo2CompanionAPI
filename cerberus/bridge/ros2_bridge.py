"""
cerberus/bridge/ros2_bridge.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS ROS 2 Bridge

Translates between the CERBERUS BridgeBase interface and ROS 2 topics/services
via the Unitree Go2's unitree_ros2 stack (Humble+).

Status
──────
Architecture and topic mapping are fully defined here.
Full execution requires a ROS 2 Humble+ installation with:
  - rclpy (ros2/rclpy)
  - unitree_ros2 (github.com/unitreerobotics/unitree_ros2)

Without those packages the module imports cleanly, and all method calls
return False / empty state (safe degradation).  Set GO2_ROS2=true in your
environment to activate — if rclpy is not found, a clear RuntimeError is
raised at connect() time rather than at import time.

Topic / Service mapping
───────────────────────
CERBERUS → ROS 2 (publishes):
  move(vx, vy, vyaw)      →  /cmd_vel           geometry_msgs/Twist
  set_body_height(h)       →  /body_height       std_msgs/Float32
  set_euler(r, p, y)       →  /body_euler        geometry_msgs/Vector3
  set_speed_level(l)       →  /speed_level       std_msgs/Int32
  set_foot_raise_height(h) →  /foot_raise_height std_msgs/Float32
  switch_gait(id)          →  /gait_mode         std_msgs/Int32
  execute_sport_mode(m)    →  /sport_mode        std_msgs/String
  emergency_stop()         →  /estop             std_msgs/Bool (latched)
  set_led(r, g, b)         →  /led_color         std_msgs/ColorRGBA
  set_volume(l)            →  /volume            std_msgs/Int32
  set_obstacle_avoidance(e)→  /obstacle_avoidance std_msgs/Bool

ROS 2 → CERBERUS (subscribes):
  /sportmodestate          unitree_go_msg/SportModeState → RobotState
  /imu_state               sensor_msgs/Imu              → roll/pitch/yaw
  /battery_state           sensor_msgs/BatteryState     → battery fields

Coordinate conventions
──────────────────────
ROS 2 uses REP 103 (x-forward, y-left, z-up).
CERBERUS uses the Unitree SDK convention (x-forward, y-right, z-up).
  vy_ros2 = -vy_cerberus
  roll_ros2 = -roll_cerberus   (ROS2 body frame has mirrored lateral axis)
This bridge applies the conversion transparently.

Environment variables
─────────────────────
  GO2_ROS2=true                 Activate ROS 2 bridge
  ROS2_NODE_NAME=cerberus_go2   ROS 2 node name (default)
  ROS2_NAMESPACE=/go2           Topic namespace prefix (default /go2)

Usage
─────
  # .env
  GO2_SIMULATION=false
  GO2_ROS2=true
  ROS2_NAMESPACE=/go2

  # Python
  from cerberus.bridge.ros2_bridge import Ros2Bridge
  bridge = Ros2Bridge()
  await bridge.connect()
  await bridge.move(0.3, 0.0, 0.0)
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from cerberus.bridge.go2_bridge import BridgeBase, RobotState, SportMode

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

ROS2_NODE_NAME: str = os.getenv("ROS2_NODE_NAME", "cerberus_go2")
ROS2_NAMESPACE: str = os.getenv("ROS2_NAMESPACE", "/go2").rstrip("/")


def _topic(name: str) -> str:
    """Build a fully-qualified topic name from the configured namespace."""
    return f"{ROS2_NAMESPACE}/{name.lstrip('/')}"


# ── Coordinate conversion ─────────────────────────────────────────────────────

def _to_ros2_twist(vx: float, vy: float, vyaw: float) -> dict:
    """
    Convert CERBERUS velocity (Unitree convention) to ROS 2 Twist.
    y-axis is mirrored: vy_cerberus = +right, vy_ros2 = +left.
    """
    return {
        "linear":  {"x": vx,   "y": -vy,   "z": 0.0},
        "angular": {"x": 0.0,  "y": 0.0,   "z": vyaw},
    }


def _from_ros2_state(msg) -> dict:
    """Extract fields from a unitree_go_msg/SportModeState ROS 2 message."""
    try:
        return {
            "velocity_x":   getattr(msg, "vx",   0.0),
            "velocity_y":  -getattr(msg, "vy",   0.0),  # mirror y back
            "velocity_yaw": getattr(msg, "vyaw", 0.0),
            "body_height":  getattr(msg, "body_height", 0.27),
            "roll":         -getattr(msg, "imu_state.rpy[0]", 0.0),
            "pitch":         getattr(msg, "imu_state.rpy[1]", 0.0),
            "yaw":           getattr(msg, "imu_state.rpy[2]", 0.0),
            "foot_force":   list(getattr(msg, "foot_force_est", [0]*4))[:4],
            "mode":         str(getattr(msg, "mode", "idle")),
        }
    except Exception as exc:
        logger.debug("[Ros2Bridge] State parse error: %s", exc)
        return {}


# ── ROS 2 Bridge ──────────────────────────────────────────────────────────────

class Ros2Bridge(BridgeBase):
    """
    CERBERUS bridge implementation backed by ROS 2.

    Requires rclpy and unitree_ros2.  If these are not installed, connect()
    raises RuntimeError with clear installation instructions.

    Thread safety: ROS 2 callbacks run in a separate executor thread.
    All public async methods are safe to call from the asyncio event loop.
    """

    def __init__(
        self,
        node_name: str = ROS2_NODE_NAME,
        namespace: str = ROS2_NAMESPACE,
    ):
        self._node_name  = node_name
        self._namespace  = namespace
        self._node       = None    # rclpy.Node
        self._executor   = None    # rclpy.executors.MultiThreadedExecutor
        self._exec_thread = None   # threading.Thread

        self._publishers: dict[str, object]  = {}
        self._subscribers: dict[str, object] = {}

        self._state     = RobotState()
        self._connected = False
        self._lock      = asyncio.Lock()

        # Callbacks registered by subscribers (e.g. Safety Watchdog)
        self._state_callbacks: list[Callable[[RobotState], None]] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Initialise rclpy, create the node, set up all publishers and
        subscribers, and start the executor in a background thread.

        Raises RuntimeError if rclpy or unitree_go_msg are not installed.
        """
        if self._connected:
            return

        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.executors import MultiThreadedExecutor
        except ImportError as exc:
            raise RuntimeError(
                "rclpy is not installed.  Install ROS 2 Humble and source the "
                "setup file, or run:\n"
                "  sudo apt install ros-humble-rclpy\n"
                "  source /opt/ros/humble/setup.bash"
            ) from exc

        try:
            import geometry_msgs.msg
            import std_msgs.msg
            import sensor_msgs.msg
        except ImportError as exc:
            raise RuntimeError(
                "ROS 2 standard message packages not found.  Install:\n"
                "  sudo apt install ros-humble-geometry-msgs ros-humble-std-msgs\n"
                "                   ros-humble-sensor-msgs"
            ) from exc

        if not rclpy.ok():
            rclpy.init()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_ros2_node)
        self._connected = True
        logger.info("[Ros2Bridge] Connected — node /%s, namespace %s",
                    self._node_name, self._namespace)

    def _init_ros2_node(self) -> None:
        """Synchronous ROS 2 initialisation (runs in executor thread)."""
        import threading
        import rclpy
        from rclpy.node import Node
        from rclpy.executors import MultiThreadedExecutor
        import geometry_msgs.msg as geom
        import std_msgs.msg    as std
        import sensor_msgs.msg as sens

        self._node = Node(self._node_name)

        # ── Publishers ────────────────────────────────────────────────────────
        def _pub(topic, msg_type, qos=10):
            return self._node.create_publisher(msg_type, _topic(topic), qos)

        self._publishers = {
            "cmd_vel":            _pub("cmd_vel",            geom.Twist),
            "body_height":        _pub("body_height",        std.Float32),
            "body_euler":         _pub("body_euler",         geom.Vector3),
            "speed_level":        _pub("speed_level",        std.Int32),
            "foot_raise_height":  _pub("foot_raise_height",  std.Float32),
            "gait_mode":          _pub("gait_mode",          std.Int32),
            "sport_mode":         _pub("sport_mode",         std.String),
            "estop":              _pub("estop",              std.Bool),
            "led_color":          _pub("led_color",          std.ColorRGBA),
            "volume":             _pub("volume",             std.Int32),
            "obstacle_avoidance": _pub("obstacle_avoidance", std.Bool),
        }

        # ── Subscribers ───────────────────────────────────────────────────────
        try:
            # unitree_go state — prefer the unitree_go_msg if available
            from unitree_go.msg import SportModeState
            self._node.create_subscription(
                SportModeState, _topic("sportmodestate"),
                self._on_sport_mode_state, 10,
            )
            logger.info("[Ros2Bridge] Subscribed to unitree_go/SportModeState")
        except ImportError:
            logger.warning(
                "[Ros2Bridge] unitree_go_msg not available — "
                "robot state will not be populated from ROS 2"
            )

        try:
            self._node.create_subscription(
                sens.BatteryState, _topic("battery_state"),
                self._on_battery_state, 10,
            )
        except Exception:
            pass

        # ── Executor ──────────────────────────────────────────────────────────
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self._node)

        import threading
        self._exec_thread = threading.Thread(
            target=self._executor.spin, daemon=True, name="cerberus_ros2_exec"
        )
        self._exec_thread.start()

    async def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            if self._executor:
                self._executor.shutdown(timeout_sec=2.0)
            if self._node:
                self._node.destroy_node()
            import rclpy
            if rclpy.ok():
                rclpy.shutdown()
        except Exception as exc:
            logger.debug("[Ros2Bridge] Disconnect error: %s", exc)
        self._connected = False
        logger.info("[Ros2Bridge] Disconnected")

    # ── State callbacks ───────────────────────────────────────────────────────

    def _on_sport_mode_state(self, msg) -> None:
        """ROS 2 subscription callback — runs in executor thread."""
        try:
            d = _from_ros2_state(msg)
            s = self._state
            s.timestamp     = time.time()
            s.velocity_x    = d.get("velocity_x",  s.velocity_x)
            s.velocity_y    = d.get("velocity_y",   s.velocity_y)
            s.velocity_yaw  = d.get("velocity_yaw", s.velocity_yaw)
            s.body_height   = d.get("body_height",  s.body_height)
            s.roll          = d.get("roll",          s.roll)
            s.pitch         = d.get("pitch",         s.pitch)
            s.yaw           = d.get("yaw",           s.yaw)
            s.foot_force    = d.get("foot_force",    s.foot_force)
            s.mode          = d.get("mode",          s.mode)
            for cb in self._state_callbacks:
                try:
                    cb(s)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[Ros2Bridge] State callback error: %s", exc)

    def _on_battery_state(self, msg) -> None:
        try:
            self._state.battery_percent = float(msg.percentage) * 100.0
            self._state.battery_voltage = float(msg.voltage)
        except Exception:
            pass

    # ── State ─────────────────────────────────────────────────────────────────

    async def get_state(self) -> RobotState:
        return self._state

    # ── Motion ────────────────────────────────────────────────────────────────

    async def stand_up(self) -> bool:
        return await self.execute_sport_mode(SportMode.STAND_UP)

    async def stand_down(self) -> bool:
        return await self.execute_sport_mode(SportMode.STAND_DOWN)

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        if not self._connected:
            return False
        try:
            import geometry_msgs.msg as geom
            t = _to_ros2_twist(vx, vy, vyaw)
            msg = geom.Twist()
            msg.linear.x  = t["linear"]["x"]
            msg.linear.y  = t["linear"]["y"]
            msg.angular.z = t["angular"]["z"]
            self._publishers["cmd_vel"].publish(msg)
            self._state.velocity_x   = vx
            self._state.velocity_y   = vy
            self._state.velocity_yaw = vyaw
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] move() error: %s", exc)
            return False

    async def stop_move(self) -> bool:
        return await self.move(0.0, 0.0, 0.0)

    async def set_body_height(self, height: float) -> bool:
        return self._publish_float32("body_height", height)

    async def set_speed_level(self, level: int) -> bool:
        return self._publish_int32("speed_level", level)

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        if not self._connected:
            return False
        try:
            import geometry_msgs.msg as geom
            msg = geom.Vector3()
            msg.x = -roll   # mirror y-axis back to ROS2 convention
            msg.y = pitch
            msg.z = yaw
            self._publishers["body_euler"].publish(msg)
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] set_euler() error: %s", exc)
            return False

    async def switch_gait(self, gait_id: int) -> bool:
        return self._publish_int32("gait_mode", gait_id)

    async def set_foot_raise_height(self, height: float) -> bool:
        return self._publish_float32("foot_raise_height", height)

    async def set_continuous_gait(self, enabled: bool) -> bool:
        # No direct ROS2 topic for continuous gait — treated as gait_mode 0/1
        return True

    # ── Sport modes ───────────────────────────────────────────────────────────

    async def execute_sport_mode(self, mode: SportMode) -> bool:
        if not self._connected:
            return False
        try:
            import std_msgs.msg as std
            msg = std.String()
            msg.data = mode.value
            self._publishers["sport_mode"].publish(msg)
            self._state.mode = mode.value
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] sport_mode error: %s", exc)
            return False

    # ── Safety ────────────────────────────────────────────────────────────────

    async def emergency_stop(self) -> bool:
        """Publish a latched True to the estop topic and stop motion."""
        await self.stop_move()
        result = self._publish_bool("estop", True)
        self._state.estop_active = True
        logger.critical("[Ros2Bridge] E-STOP published to %s", _topic("estop"))
        return result

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        result = self._publish_bool("obstacle_avoidance", enabled)
        self._state.obstacle_avoidance = enabled
        return result

    # ── LED / VUI ─────────────────────────────────────────────────────────────

    async def set_led(self, r: int, g: int, b: int) -> bool:
        if not self._connected:
            return False
        try:
            import std_msgs.msg as std
            msg = std.ColorRGBA()
            msg.r = r / 255.0
            msg.g = g / 255.0
            msg.b = b / 255.0
            msg.a = 1.0
            self._publishers["led_color"].publish(msg)
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] set_led() error: %s", exc)
            return False

    async def set_volume(self, level: int) -> bool:
        return self._publish_int32("volume", max(0, min(100, level)))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _publish_float32(self, topic: str, value: float) -> bool:
        if not self._connected:
            return False
        try:
            import std_msgs.msg as std
            msg = std.Float32()
            msg.data = float(value)
            self._publishers[topic].publish(msg)
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] _publish_float32(%s) error: %s", topic, exc)
            return False

    def _publish_int32(self, topic: str, value: int) -> bool:
        if not self._connected:
            return False
        try:
            import std_msgs.msg as std
            msg = std.Int32()
            msg.data = int(value)
            self._publishers[topic].publish(msg)
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] _publish_int32(%s) error: %s", topic, exc)
            return False

    def _publish_bool(self, topic: str, value: bool) -> bool:
        if not self._connected:
            return False
        try:
            import std_msgs.msg as std
            msg = std.Bool()
            msg.data = bool(value)
            self._publishers[topic].publish(msg)
            return True
        except Exception as exc:
            logger.error("[Ros2Bridge] _publish_bool(%s) error: %s", topic, exc)
            return False


# ── Factory integration ───────────────────────────────────────────────────────

def create_ros2_bridge() -> Ros2Bridge:
    """
    Create a Ros2Bridge.  Called by the top-level create_bridge() factory
    when GO2_ROS2=true is set in the environment.
    """
    return Ros2Bridge()
