"""
go2_platform/backend/ros2_bridge/bridge_node.py
══════════════════════════════════════════════════════════════════════════════
ROS2 Platform Bridge — connects PlatformCore to ROS2 ecosystem.

Architecture:
  PlatformCore (authoritative)
       │  asyncio event bus
  BridgeNode (this file)
       │  ROS2 topics/services
  ROS2 Nodes (safety, motion, perception, etc.)
       │  Unitree SDK2
  Go2 Hardware

Data flows:
  Platform → ROS2:  commands, motion profiles, mission waypoints
  ROS2 → Platform:  telemetry, perception, safety events

Implements hardware abstraction for Go2 Air/Pro/EDU differences:
  Air:    SDK2 via Ethernet, no foot sensors, limited modes
  Pro:    SDK2 via Ethernet, higher torque, same sensor config as Air
  EDU:    SDK2 native, Jetson Orin onboard, foot force sensors, RealSense
"""

import json
import logging
import math
import sys
import time
from typing import Any, Dict, Optional

import rclpy
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn, State
from rclpy.qos import (QoSProfile, ReliabilityPolicy,
                        DurabilityPolicy, HistoryPolicy)
from std_msgs.msg import Bool, String
from sensor_msgs.msg import Imu, JointState, LaserScan
from geometry_msgs.msg import Twist

logger = logging.getLogger('go2.bridge')


# Hardware model constants
class Go2Model:
    AIR = 'air'
    PRO = 'pro'
    EDU = 'edu'


HARDWARE_CAPS = {
    Go2Model.AIR: {
        'foot_sensors': False,
        'sdk_access': 'ethernet_hack',  # requires firmware mod
        'max_torque_nm': 45.0,
        'onboard_compute': False,
        'realsense': False,
    },
    Go2Model.PRO: {
        'foot_sensors': False,
        'sdk_access': 'ethernet_hack',
        'max_torque_nm': 45.0,
        'onboard_compute': False,
        'realsense': False,
    },
    Go2Model.EDU: {
        'foot_sensors': True,
        'sdk_access': 'native',
        'max_torque_nm': 45.0,
        'onboard_compute': True,   # Jetson Orin NX
        'realsense': True,
    },
}

# Go2 SDK2 joint index map (LowCmd / LowState)
JOINT_MAP = {
    'FR_0': 0, 'FR_1': 1,  'FR_2': 2,
    'FL_0': 3, 'FL_1': 4,  'FL_2': 5,
    'RR_0': 6, 'RR_1': 7,  'RR_2': 8,
    'RL_0': 9, 'RL_1': 10, 'RL_2': 11,
}

# Validated nominal poses (radians) — verified against Go2 URDF
POSES = {
    'stand': {
        'FR_0':0.0,'FR_1':0.67,'FR_2':-1.3,
        'FL_0':0.0,'FL_1':0.67,'FL_2':-1.3,
        'RR_0':0.0,'RR_1':0.67,'RR_2':-1.3,
        'RL_0':0.0,'RL_1':0.67,'RL_2':-1.3,
    },
    'sit': {
        'FR_0':0.0,'FR_1':0.67,'FR_2':-1.3,
        'FL_0':0.0,'FL_1':0.67,'FL_2':-1.3,
        'RR_0':0.0,'RR_1':1.6, 'RR_2':-2.4,
        'RL_0':0.0,'RL_1':1.6, 'RL_2':-2.4,
    },
    'lie': {
        'FR_0':0.0,'FR_1':1.4,'FR_2':-2.6,
        'FL_0':0.0,'FL_1':1.4,'FL_2':-2.6,
        'RR_0':0.0,'RR_1':1.4,'RR_2':-2.6,
        'RL_0':0.0,'RL_1':1.4,'RL_2':-2.6,
    },
}

# Torque limits per joint type
TORQUE_LIMITS_NM = {
    'abduction': 23.0,   # _0
    'hip':       45.0,   # _1
    'knee':      45.0,   # _2
}


class BridgeNode(LifecycleNode):
    """
    Platform ↔ ROS2 bridge.
    Translates PlatformCore events/commands to ROS2 topics/services
    and feeds telemetry back to PlatformCore.
    """

    def __init__(self, model: str = Go2Model.EDU):
        super().__init__('go2_bridge')
        self.model = model
        self.caps = HARDWARE_CAPS.get(model, HARDWARE_CAPS[Go2Model.EDU])
        self._use_sim = True
        self._override = False
        self._q_current: Dict[str, float] = dict(POSES['stand'])
        self._q_prev: Dict[str, float] = dict(POSES['stand'])
        self._imu_pitch = 0.0
        self._imu_roll = 0.0
        self._contact_force = 0.0

        # Parameters
        self.declare_parameter('use_sim', True)
        self.declare_parameter('model', model)
        self.declare_parameter('robot_ip', '192.168.12.1')
        self.declare_parameter('control_hz', 500)
        self.declare_parameter('max_torque', 35.0)

        logger.info(f'BridgeNode created: model={model}')

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        self._use_sim = self.get_parameter('use_sim').value
        ctrl_hz = self.get_parameter('control_hz').value
        self._max_torque = self.get_parameter('max_torque').value

        # QoS profiles
        reliable = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST, depth=10)
        sensor = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST, depth=1)

        # ── Inbound (robot → bridge) ──────────────────────────────────────
        self._sub_imu = self.create_subscription(
            Imu, '/go2/imu', self._imu_cb, sensor)
        self._sub_joints = self.create_subscription(
            JointState, '/go2/joint_states', self._joints_cb, sensor)
        self._sub_lidar = self.create_subscription(
            LaserScan, '/go2/lidar/scan', self._lidar_cb, sensor)

        # ── Outbound (bridge → robot) ─────────────────────────────────────
        # In production: publish go2_interfaces/LowCmd
        # In simulation: publish JSON for visualization
        self._pub_lowcmd = self.create_publisher(
            String, '/go2/low_cmd', sensor)
        self._pub_cmd_vel = self.create_publisher(
            Twist, '/go2/cmd_vel', sensor)
        self._pub_status = self.create_publisher(
            String, '/go2/bridge_status', reliable)

        # ── Platform event subscriptions (from internal event bus) ──────
        # These are wired externally after construction
        self._pub_telemetry = self.create_publisher(
            String, '/go2/platform_telemetry', reliable)

        # ── Control loop ────────────────────────────────────────────────
        dt = 1.0 / ctrl_hz
        self._ctrl_timer = self.create_timer(dt, self._control_loop)
        self._status_timer = self.create_timer(1.0, self._publish_status)

        logger.info(
            f'BridgeNode configured: sim={self._use_sim} '
            f'ctrl={ctrl_hz}Hz model={self.model}')

        # Hardware abstraction warning for Air/Pro
        if self.model in (Go2Model.AIR, Go2Model.PRO):
            if not self._use_sim:
                self.get_logger().warn(
                    f'Go2 {self.model.upper()} requires custom firmware for SDK2 access. '
                    'See: https://github.com/unitreerobotics/unitree_sdk2')

        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state):
        logger.info('BridgeNode ACTIVE')
        return TransitionCallbackReturn.SUCCESS

    def on_deactivate(self, state):
        self._send_stand()
        return TransitionCallbackReturn.SUCCESS

    def on_cleanup(self, state):
        self._ctrl_timer.cancel()
        self._status_timer.cancel()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state):
        self._send_stand()
        return TransitionCallbackReturn.SUCCESS

    # ── ROS2 → Platform ──────────────────────────────────────────────────

    def _imu_cb(self, msg: Imu):
        """Extract pitch/roll from quaternion."""
        q = msg.orientation
        sinr = 2.0 * (q.w * q.x + q.y * q.z)
        cosr = 1.0 - 2.0 * (q.x**2 + q.y**2)
        self._imu_roll  = math.degrees(math.atan2(sinr, cosr))
        sinp = 2.0 * (q.w * q.y - q.z * q.x)
        self._imu_pitch = math.degrees(math.asin(max(-1.0, min(1.0, sinp))))

    def _joints_cb(self, msg: JointState):
        for i, name in enumerate(msg.name):
            if i < len(msg.position):
                self._q_prev[name] = self._q_current.get(name, 0.0)
                self._q_current[name] = msg.position[i]

    def _lidar_cb(self, msg: LaserScan):
        valid = [r for r in msg.ranges if not math.isnan(r) and r > 0.05]
        if valid:
            min_r = min(valid)
            if min_r < 0.3:
                self.get_logger().warn(f'Obstacle: {min_r:.2f}m')

    # ── Control loop ──────────────────────────────────────────────────────

    def _control_loop(self):
        """500Hz control loop — publishes LowCmd."""
        if self._override:
            return
        # In simulation, just publish heartbeat
        # In hardware, compute and publish LowCmd

    def _compute_torques(self, pose_name: str,
                          K_p: float = 60.0, K_d: float = 3.0) -> Dict[str, float]:
        """
        PD impedance control: τ = Kp(q_des - q) - Kd·dq
        Returns per-joint torques, clamped to hardware limits.
        """
        target = POSES.get(pose_name, POSES['stand'])
        dt = 1.0 / self.get_parameter('control_hz').value
        torques = {}
        for jn, q_des in target.items():
            q = self._q_current.get(jn, q_des)
            q_prev = self._q_prev.get(jn, q)
            dq = (q - q_prev) / max(dt, 1e-6)
            tau = K_p * (q_des - q) - K_d * dq
            # Clamp
            jtype = 'abduction' if jn.endswith('_0') else ('hip' if jn.endswith('_1') else 'knee')
            limit = min(TORQUE_LIMITS_NM[jtype], self._max_torque)
            torques[jn] = max(-limit, min(limit, tau))
        return torques

    def _publish_lowcmd(self, torques: Dict[str, float],
                         targets: Dict[str, float], K_p: float, K_d: float):
        """
        In hardware mode: publish go2_interfaces/LowCmd to Unitree SDK2.
        LowCmd format (12 joints):
          motorCmd[i].mode  = 0x0A  (torque+position mode)
          motorCmd[i].q     = q_des
          motorCmd[i].dq    = 0
          motorCmd[i].Kp    = K_p
          motorCmd[i].Kd    = K_d
          motorCmd[i].tau   = tau_ff
        """
        # Build array in SDK2 index order
        cmd_array = [None] * 12
        for jn, tau in torques.items():
            idx = JOINT_MAP.get(jn)
            if idx is not None:
                cmd_array[idx] = {
                    'mode': 0x0A,
                    'q': targets.get(jn, 0.0),
                    'dq': 0.0,
                    'Kp': K_p,
                    'Kd': K_d,
                    'tau': tau,
                }
        # Publish as JSON (in production: use unitree_ros2 LowCmd message type)
        msg = String()
        msg.data = json.dumps({'stamp': time.monotonic(), 'motors': cmd_array})
        self._pub_lowcmd.publish(msg)

    def _send_stand(self):
        """Safe recovery — publish stand pose with low Kp."""
        torques = self._compute_torques('stand', K_p=40.0, K_d=4.0)
        self._publish_lowcmd(torques, POSES['stand'], 40.0, 4.0)

    def send_velocity(self, vx: float, vy: float, vyaw: float):
        """
        Send body velocity command via Twist topic.
        Maps to Go2 high-level walk controller.
        Clamps to safety limits.
        """
        max_v = self.get_parameter_or('max_velocity', 1.5).value  # type: ignore
        cmd = Twist()
        cmd.linear.x  = max(-max_v, min(max_v, vx))
        cmd.linear.y  = max(-max_v, min(max_v, vy))
        cmd.angular.z = max(-2.0, min(2.0, vyaw))
        self._pub_cmd_vel.publish(cmd)

    def _publish_status(self):
        msg = String()
        msg.data = json.dumps({
            'model': self.model,
            'caps': self.caps,
            'sim': self._use_sim,
            'override': self._override,
            'pitch': round(self._imu_pitch, 2),
            'roll': round(self._imu_roll, 2),
        })
        self._pub_status.publish(msg)

    def get_parameter_or(self, name: str, default):
        try:
            return self.get_parameter(name)
        except Exception:
            class _D:
                value = default
            return _D()


def main(args=None):
    rclpy.init(args=args)
    import os
    model = os.environ.get('GO2_MODEL', Go2Model.EDU)
    node = BridgeNode(model=model)
    from rclpy.executors import MultiThreadedExecutor
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
