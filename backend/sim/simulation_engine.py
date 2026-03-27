"""
go2_platform/backend/sim/simulation_engine.py
══════════════════════════════════════════════════════════════════════════════
Simulation Engine — Full robot state simulation for testing/demo.

Design principles:
  - Mirrors the real ROS2/hardware pipeline exactly
  - PlatformCore operates identically in SIM and LIVE modes
  - SimEngine replaces the ROS2 Bridge in simulation, feeding realistic
    telemetry directly to PlatformCore's safety/FSM loop
  - Gazebo-compatible state representation (can be swapped for real Gazebo)

Simulated systems:
  - Kinematic model (12-DOF quadruped, simplified)
  - IMU (pitch/roll/yaw with drift noise)
  - Battery drain model
  - Contact/foot forces
  - Motor temperature model
  - Obstacle field
  - Object detection (simulated YOLO)
  - LiDAR scan (synthetic radial obstacle map)
  - Navigation (waypoint-to-waypoint with timing)
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from ..core.platform import Telemetry, RobotState


# ════════════════════════════════════════════════════════════════════════════
# PHYSICS CONSTANTS
# ════════════════════════════════════════════════════════════════════════════

GRAVITY          = 9.81    # m/s²
ROBOT_MASS_KG    = 15.0
BATTERY_MAH      = 8000
CTRL_VOLTAGE     = 29.4

# Current draw estimates (A)
CURRENT_IDLE     = 0.8
CURRENT_WALK     = 4.5
CURRENT_ACTIVE   = 7.0
CURRENT_ESTOP    = 0.1

# Thermal model
MOTOR_THERMAL_R  = 0.12    # °C/W
AMBIENT_TEMP     = 22.0
BASE_DISSIPATION = 0.15    # °C/s natural cooling rate

# Noise model
IMU_NOISE_DEG_WALK = 2.8   # deg RMS in motion
IMU_NOISE_DEG_IDLE = 0.4
FORCE_NOISE_N      = 3.0


# ════════════════════════════════════════════════════════════════════════════
# KINEMATICS (simplified 12-DOF quadruped)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LegState:
    """Single leg kinematic state."""
    label:     str
    phase:     float = 0.0       # gait phase [0, 2π]
    in_stance: bool  = True
    q_hip:     float = 0.67      # rad
    q_knee:    float = -1.3      # rad
    q_abduct:  float = 0.0       # rad
    foot_force_n: float = 13.0
    contact:   bool = True


@dataclass
class RobotKinematics:
    """Full kinematic state of the Go2."""
    body_x:  float = 0.0      # m world frame
    body_y:  float = 0.0
    body_z:  float = 0.30     # m height
    pitch:   float = 0.0      # rad
    roll:    float = 0.0
    yaw:     float = 0.0
    vx:      float = 0.0      # m/s body frame
    vy:      float = 0.0
    vyaw:    float = 0.0
    legs:    Dict[str, LegState] = field(default_factory=lambda: {
        k: LegState(k) for k in ('FL', 'FR', 'RL', 'RR')
    })
    gait_phase: float = 0.0   # master gait clock

    def joint_positions(self) -> Dict[str, float]:
        return {
            f'{k}_0': leg.q_abduct for k, leg in self.legs.items()
            for _ in [None]
        } | {
            f'{k}_1': leg.q_hip for k, leg in self.legs.items()
        } | {
            f'{k}_2': leg.q_knee for k, leg in self.legs.items()
        }


# ════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE
# ════════════════════════════════════════════════════════════════════════════

class SimulationEngine:
    """
    Authoritative simulation of Unitree Go2 for the platform.

    Replaces ROS2 bridge + hardware in simulation mode.
    Feeds realistic telemetry directly to PlatformCore.

    Usage:
        engine = SimulationEngine(platform_core)
        await engine.start()
    """

    SIM_HZ      = 200    # simulation tick rate
    TELEMETRY_HZ = 10    # telemetry publish rate to platform
    LIDAR_HZ     = 10    # LiDAR update rate

    def __init__(self, platform):
        self._platform = platform
        self._kinematics = RobotKinematics()
        self._battery_pct = 87.0
        self._battery_charge_mah = BATTERY_MAH * 0.87
        self._motor_temps = {k: 42.0 for k in ('FL','FR','RL','RR')}
        self._obstacles: List[dict] = []   # {x, y, r} obstacle circles
        self._detected_objects: List[dict] = []
        self._lidar_scan: List[float] = [8.0] * 360   # radial distances
        self._start_t = time.monotonic()
        self._sim_task: Optional[asyncio.Task] = None
        self._tel_task: Optional[asyncio.Task] = None
        self._seed_obstacles()
        self._frames = 0

    def _seed_obstacles(self):
        """Populate a simple obstacle field."""
        self._obstacles = [
            {'x':  1.5, 'y':  0.5, 'r': 0.3, 'label': 'chair'},
            {'x': -1.0, 'y':  1.2, 'r': 0.25,'label': 'cushion'},
            {'x':  0.8, 'y': -1.0, 'r': 0.2, 'label': 'wall_section'},
        ]

    async def start(self):
        self._sim_task = asyncio.create_task(self._sim_loop())
        self._tel_task = asyncio.create_task(self._telemetry_loop())
        self._lidar_task = asyncio.create_task(self._lidar_loop())

    async def stop(self):
        for t in (self._sim_task, self._tel_task, self._lidar_task):
            if t: t.cancel()

    # ── Simulation loop (200 Hz) ──────────────────────────────────────────

    async def _sim_loop(self):
        dt = 1.0 / self.SIM_HZ
        while True:
            try:
                t0 = time.monotonic()
                self._tick(dt)
                elapsed = time.monotonic() - t0
                sleep_t = max(0, dt - elapsed)
                await asyncio.sleep(sleep_t)
            except asyncio.CancelledError:
                break
            except Exception as e:
                import logging
                logging.getLogger('go2.sim').error(f'Sim tick error: {e}')
                await asyncio.sleep(0.01)

    def _tick(self, dt: float):
        """Single simulation step."""
        self._frames += 1
        state = self._platform.fsm.state
        active = state in (
            RobotState.WALKING, RobotState.FOLLOWING,
            RobotState.NAVIGATING, RobotState.INTERACTING,
            RobotState.PERFORMING, RobotState.PATROLLING)
        estop = state == RobotState.ESTOP

        # ── Battery drain ────────────────────────────────────────────────
        current_a = (CURRENT_ESTOP if estop else
                     CURRENT_ACTIVE if active else CURRENT_IDLE)
        dq = current_a * dt / 3600  # Ah drain
        self._battery_charge_mah = max(0, self._battery_charge_mah - dq * 1000)
        self._battery_pct = (self._battery_charge_mah / BATTERY_MAH) * 100

        # ── Kinematics ───────────────────────────────────────────────────
        k = self._kinematics
        if estop:
            # Come to rest
            k.vx   *= 0.8
            k.vy   *= 0.8
            k.vyaw *= 0.8
        elif active:
            # Simulate natural oscillation
            t = time.monotonic() - self._start_t
            noise = lambda f, a: a * math.sin(t * f + random.gauss(0, 0.1))
            k.pitch = noise(1.7, math.radians(1.5))
            k.roll  = noise(2.3, math.radians(0.8))
            k.yaw  += k.vyaw * dt + noise(0.3, math.radians(0.1))
            # Advance gait phase
            k.gait_phase = (k.gait_phase + 2 * math.pi * 2.0 * dt) % (2 * math.pi)
            # Move body
            k.body_x += k.vx * math.cos(k.yaw) * dt - k.vy * math.sin(k.yaw) * dt
            k.body_y += k.vx * math.sin(k.yaw) * dt + k.vy * math.cos(k.yaw) * dt
            # Animate legs
            self._update_gait(k, dt, active=True)
        else:
            k.pitch += -k.pitch * 0.1 + random.gauss(0, math.radians(0.05))
            k.roll  += -k.roll  * 0.1 + random.gauss(0, math.radians(0.03))
            self._update_gait(k, dt, active=False)

        # ── Motor thermals ────────────────────────────────────────────────
        for leg_key in self._motor_temps:
            is_rl = leg_key in ('RL', 'RR')
            power_w = (current_a * CTRL_VOLTAGE) / 4
            if is_rl and active: power_w *= 1.3   # rear legs work harder
            delta_temp = (MOTOR_THERMAL_R * power_w -
                         BASE_DISSIPATION * (self._motor_temps[leg_key] - AMBIENT_TEMP)) * dt
            self._motor_temps[leg_key] = max(
                AMBIENT_TEMP, self._motor_temps[leg_key] + delta_temp)

        # ── Object detection (simulated YOLO) ────────────────────────────
        if self._frames % 20 == 0:
            self._update_detections()

        # ── Collision / obstacle check ────────────────────────────────────
        self._check_obstacles()

    def _update_gait(self, k: RobotKinematics, dt: float, active: bool):
        """Update leg kinematics for trot gait."""
        gait_pairs = {'FL': 0.0, 'RR': 0.0, 'FR': math.pi, 'RL': math.pi}
        for leg_label, leg_offset in gait_pairs.items():
            leg = k.legs[leg_label]
            phase = (k.gait_phase + leg_offset) % (2 * math.pi)
            leg.phase = phase
            if active:
                in_stance = phase < math.pi
                leg.in_stance = in_stance
                leg.q_hip   = 0.67 + (0.12 if not in_stance else -0.04) * math.sin(phase)
                leg.q_knee  = -1.3 + (0.15 if not in_stance else 0.05)  * math.cos(phase)
                leg.foot_force_n = max(0, (15.0 + FORCE_NOISE_N * random.gauss(0, 1))
                                       if in_stance else 0.0)
            else:
                leg.q_hip  = 0.67 + random.gauss(0, 0.005)
                leg.q_knee = -1.3 + random.gauss(0, 0.005)
                leg.foot_force_n = max(0, 13.0 + FORCE_NOISE_N * 0.3 * random.gauss(0, 1))

    def _update_detections(self):
        """Simulate object detection output."""
        k = self._kinematics
        detected = []
        for obs in self._obstacles:
            dx, dy = obs['x'] - k.body_x, obs['y'] - k.body_y
            dist = math.sqrt(dx**2 + dy**2)
            if dist < 4.0:  # within camera range
                conf = max(0.3, min(0.99, 1.0 - dist / 4.0 + random.gauss(0, 0.05)))
                angle = math.degrees(math.atan2(dy, dx) - k.yaw)
                # Project to pixel coords (normalized)
                px = int(320 + angle * 320 / 60)
                py = int(240 - (0.3 / max(dist, 0.1)) * 200)
                w = int(max(20, min(200, 100 / max(dist, 0.3))))
                detected.append({
                    'label':  obs['label'],
                    'conf':   round(conf, 2),
                    'dist_m': round(dist, 2),
                    'bbox':   [px-w//2, py-w//2, px+w//2, py+w//2],
                })
        self._detected_objects = detected

    def _check_obstacles(self):
        """Trigger safety event if collision imminent."""
        k = self._kinematics
        for obs in self._obstacles:
            dx, dy = obs['x'] - k.body_x, obs['y'] - k.body_y
            dist = math.sqrt(dx**2 + dy**2) - obs['r'] - 0.35  # robot radius
            if dist < 0.25 and k.vx != 0:
                # Schedule async safety notification
                asyncio.create_task(
                    self._platform.bus.emit(
                        'sim.collision_imminent',
                        {'obstacle': obs['label'], 'dist': round(dist, 2)},
                        'simulation'))

    # ── Telemetry publisher (10 Hz → platform) ────────────────────────────

    async def _telemetry_loop(self):
        dt = 1.0 / self.TELEMETRY_HZ
        while True:
            try:
                tel = self._build_telemetry()
                self._platform.safety.update_telemetry(tel)
                self._platform.safety.update_perception(
                    human_in_zone=False,
                    obstacle_dist=self._nearest_obstacle_dist())
                self._platform.telemetry = tel
                await self._platform._broadcast({'type': 'telemetry', 'data': tel.to_dict()})
                await self._platform._broadcast({
                    'type': 'detections',
                    'data': self._detected_objects})
                await asyncio.sleep(dt)
            except asyncio.CancelledError:
                break

    def _build_telemetry(self) -> Telemetry:
        k = self._kinematics
        state = self._platform.fsm.state
        active = state in (RobotState.WALKING, RobotState.FOLLOWING,
                           RobotState.NAVIGATING, RobotState.INTERACTING,
                           RobotState.PERFORMING, RobotState.PATROLLING)

        # IMU noise
        noise_scale = IMU_NOISE_DEG_WALK if active else IMU_NOISE_DEG_IDLE
        pitch_noisy = math.degrees(k.pitch) + random.gauss(0, noise_scale * 0.2)
        roll_noisy  = math.degrees(k.roll)  + random.gauss(0, noise_scale * 0.15)

        # Contact force (total)
        total_cf = sum(leg.foot_force_n for leg in k.legs.values()
                       if leg.in_stance) / max(1, sum(
                           1 for l in k.legs.values() if l.in_stance))

        return Telemetry(
            ts=time.monotonic(),
            battery_pct=round(self._battery_pct, 1),
            voltage=round((self._battery_pct / 100.0) * 33.6, 2),
            pitch_deg=round(pitch_noisy, 2),
            roll_deg=round(roll_noisy, 2),
            yaw_deg=round(math.degrees(k.yaw) % 360, 1),
            contact_force_n=round(max(0, total_cf + random.gauss(0, 1)), 1),
            com_x=round(k.body_x - 0.0, 3),  # offset from neutral
            foot_forces={lk: round(leg.foot_force_n, 1)
                        for lk, leg in {
                            'fl': k.legs['FL'], 'fr': k.legs['FR'],
                            'rl': k.legs['RL'], 'rr': k.legs['RR']}.items()},
            motor_temps={lk: round(self._motor_temps[lk.upper()], 1)
                        for lk in ('fl','fr','rl','rr')},
            joint_positions={f'{lk.upper()}_1': round(leg.q_hip, 3)
                             for lk, leg in k.legs.items()},
            ctrl_hz=500.0,
            safety_level=self._platform.safety.level.value,
        )

    def _nearest_obstacle_dist(self) -> float:
        k = self._kinematics
        if not self._obstacles:
            return float('inf')
        dists = [math.sqrt((o['x']-k.body_x)**2 + (o['y']-k.body_y)**2) - o['r']
                 for o in self._obstacles]
        return max(0, min(dists))

    # ── LiDAR simulation (10 Hz) ──────────────────────────────────────────

    async def _lidar_loop(self):
        dt = 1.0 / self.LIDAR_HZ
        while True:
            try:
                self._update_lidar()
                await self._platform._broadcast({
                    'type': 'lidar',
                    'data': {
                        'scan': self._lidar_scan[::4],  # downsample to 90 pts
                        'angle_min': 0,
                        'angle_step': 4,
                        'robot': {
                            'x': round(self._kinematics.body_x, 2),
                            'y': round(self._kinematics.body_y, 2),
                            'yaw': round(math.degrees(self._kinematics.yaw), 1),
                        }
                    }})
                await asyncio.sleep(dt)
            except asyncio.CancelledError:
                break

    def _update_lidar(self):
        """Generate synthetic 360° LiDAR scan."""
        k = self._kinematics
        scan = []
        for angle_i in range(360):
            angle_rad = math.radians(angle_i) + k.yaw
            # Base range with noise
            r = 8.0 + random.gauss(0, 0.05)
            # Check intersection with each obstacle
            for obs in self._obstacles:
                dx = obs['x'] - k.body_x
                dy = obs['y'] - k.body_y
                angle_to_obs = math.atan2(dy, dx)
                angular_diff = abs(math.atan2(
                    math.sin(angle_rad - angle_to_obs),
                    math.cos(angle_rad - angle_to_obs)))
                dist_to_obs = math.sqrt(dx**2 + dy**2)
                # Approximate half-angle of obstacle at distance
                half_angle = math.atan2(obs['r'], max(dist_to_obs, 0.1))
                if angular_diff < half_angle:
                    hit_dist = dist_to_obs - obs['r']
                    r = min(r, max(0.1, hit_dist + random.gauss(0, 0.02)))
            scan.append(round(r, 2))
        self._lidar_scan = scan

    # ── Simulation controls ───────────────────────────────────────────────

    def set_velocity(self, vx: float, vy: float, vyaw: float):
        """External velocity injection (from joystick / navigation)."""
        self._kinematics.vx   = vx
        self._kinematics.vy   = vy
        self._kinematics.vyaw = vyaw

    def add_obstacle(self, x: float, y: float, r: float, label: str):
        self._obstacles.append({'x': x, 'y': y, 'r': r, 'label': label})

    def teleport(self, x: float, y: float, yaw_deg: float = 0.0):
        """Teleport robot to position (for test scenarios)."""
        self._kinematics.body_x = x
        self._kinematics.body_y = y
        self._kinematics.yaw = math.radians(yaw_deg)

    def status(self) -> dict:
        k = self._kinematics
        return {
            'sim_hz': self.SIM_HZ,
            'frames': self._frames,
            'battery_pct': round(self._battery_pct, 1),
            'position': {'x': round(k.body_x, 2), 'y': round(k.body_y, 2)},
            'heading_deg': round(math.degrees(k.yaw) % 360, 1),
            'obstacles': len(self._obstacles),
            'detections': len(self._detected_objects),
            'motor_temps': {k2: round(v, 1) for k2, v in self._motor_temps.items()},
        }
