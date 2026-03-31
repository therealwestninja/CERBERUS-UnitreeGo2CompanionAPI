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
import math
import os
import random
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

    Auto-reconnect (closes KI-05)
    ─────────────────────────────
    If the DDS connection drops (cable disconnect, network interrupt), a
    background task retries with exponential back-off:
        delay = min(RECONNECT_MAX_WAIT_S, RECONNECT_BASE_S * 2^attempt)
    Consecutive command failures increment a stale counter; once it reaches
    RECONNECT_STALE_THRESHOLD the reconnect loop activates automatically.
    Successful reconnection resets the counter and resumes normal operation.
    """

    RECONNECT_BASE_S:          float = 1.0
    RECONNECT_MAX_WAIT_S:      float = 60.0
    RECONNECT_STALE_THRESHOLD: int   = 5

    def __init__(self, network_interface: str):
        self._iface = network_interface
        self._sport_client = None
        self._state_sub    = None
        self._state        = RobotState()
        self._connected    = False
        self._state_callbacks: list[Callable[[RobotState], None]] = []

        # Reconnect state
        self._stale_count:      int  = 0
        self._reconnecting:     bool = False
        self._reconnect_attempt:int  = 0
        self._reconnect_task         = None

    async def connect(self) -> None:
        try:
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # noqa: F401
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._init_dds)
            self._connected          = True
            self._stale_count        = 0
            self._reconnect_attempt  = 0
            self._reconnecting       = False
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
        try:
            from unitree_sdk2py.core.channel import ChannelSubscriber
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
            self._state_sub = ChannelSubscriber("rt/sportmodestate", SportModeState_)
            self._state_sub.Init(self._on_state_update, 10)
        except Exception as e:
            logger.warning("State subscription failed (non-fatal): %s", e)

    def _on_state_update(self, msg) -> None:
        try:
            s = self._state
            s.timestamp    = time.time()
            s.velocity_x   = getattr(msg, "velocity", [0, 0, 0])[0]
            s.velocity_y   = getattr(msg, "velocity", [0, 0, 0])[1]
            s.velocity_yaw = getattr(msg, "yaw_speed", 0.0)
            s.body_height  = getattr(msg, "body_height", 0.27)
            imu = getattr(msg, "imu_state", None)
            if imu:
                rpy = getattr(imu, "rpy", [0, 0, 0])
                s.roll, s.pitch, s.yaw = rpy[0], rpy[1], rpy[2]
                acc = getattr(imu, "accelerometer", [0, 0, -9.81])
                s.imu_acc_x, s.imu_acc_y, s.imu_acc_z = acc[0], acc[1], acc[2]
            motors = getattr(msg, "motor_state", [])
            if motors:
                s.joint_positions  = [m.q       for m in motors[:12]]
                s.joint_velocities = [m.dq      for m in motors[:12]]
                s.joint_torques    = [m.tau_est  for m in motors[:12]]
            foot = getattr(msg, "foot_force_est", [0, 0, 0, 0])
            s.foot_force = list(foot[:4])
            self._stale_count = 0   # live data received — reset stale counter
        except Exception as e:
            logger.debug("State parse error: %s", e)

    async def disconnect(self) -> None:
        self._connected = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        logger.info("Go2 real bridge disconnected")

    async def get_state(self) -> RobotState:
        return self._state

    # ── Reconnect ─────────────────────────────────────────────────────────────

    def _mark_command_result(self, success: bool) -> None:
        """Track command success; activate reconnect after repeated failures."""
        if success:
            self._stale_count = 0
            return
        self._stale_count += 1
        if (self._stale_count >= self.RECONNECT_STALE_THRESHOLD
                and not self._reconnecting):
            logger.warning(
                "RealBridge: %d consecutive failures — starting reconnect loop",
                self._stale_count,
            )
            self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """
        Retry DDS connection with exponential back-off.
        attempt 0→1s  1→2s  2→4s  3→8s  4→16s  5→32s  6+→60s
        """
        self._reconnecting      = True
        self._connected         = False
        self._reconnect_attempt = 0

        while not self._connected:
            delay = min(
                self.RECONNECT_MAX_WAIT_S,
                self.RECONNECT_BASE_S * (2 ** self._reconnect_attempt),
            )
            logger.info(
                "RealBridge: reconnect attempt %d — waiting %.0f s",
                self._reconnect_attempt + 1, delay,
            )
            await asyncio.sleep(delay)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._init_dds)
                self._connected         = True
                self._stale_count       = 0
                self._reconnect_attempt = 0
                self._reconnecting      = False
                logger.info("RealBridge: reconnected ✓ (attempt %d)",
                            self._reconnect_attempt + 1)
            except Exception as exc:
                self._reconnect_attempt += 1
                logger.warning("RealBridge: reconnect attempt %d failed: %s",
                               self._reconnect_attempt, exc)

    # ── Command helpers ───────────────────────────────────────────────────────

    def _run_sync(self, fn, *args):
        """Run a blocking SDK call off the event loop thread."""
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, fn, *args)

    async def _cmd(self, fn, *args) -> bool:
        """Execute an SDK command; track success for reconnect detection."""
        if not self._connected or self._reconnecting:
            return False
        try:
            code, _ = await self._run_sync(fn, *args)
            ok = (code == 0)
            self._mark_command_result(ok)
            return ok
        except Exception as exc:
            logger.error("RealBridge command error: %s", exc)
            self._mark_command_result(False)
            return False

    async def stand_up(self) -> bool:
        return await self._cmd(self._sport_client.StandUp)

    async def stand_down(self) -> bool:
        return await self._cmd(self._sport_client.StandDown)

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        # Clamp at bridge layer in addition to API validation
        vx   = max(-1.5, min(1.5, vx))
        vy   = max(-0.8, min(0.8, vy))
        vyaw = max(-2.0, min(2.0, vyaw))
        ok   = await self._cmd(self._sport_client.Move, vx, vy, vyaw)
        if ok:
            self._state.velocity_x   = vx
            self._state.velocity_y   = vy
            self._state.velocity_yaw = vyaw
        return ok

    async def stop_move(self) -> bool:
        ok = await self._cmd(self._sport_client.StopMove)
        if ok:
            self._state.velocity_x = self._state.velocity_y = self._state.velocity_yaw = 0.0
        return ok

    async def set_body_height(self, height: float) -> bool:
        return await self._cmd(self._sport_client.BodyHeight, height)

    async def set_speed_level(self, level: int) -> bool:
        return await self._cmd(self._sport_client.SpeedLevel, int(level))

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        return await self._cmd(self._sport_client.Euler, roll, pitch, yaw)

    async def switch_gait(self, gait_id: int) -> bool:
        return await self._cmd(self._sport_client.SwitchGait, int(gait_id))

    async def set_foot_raise_height(self, height: float) -> bool:
        return await self._cmd(self._sport_client.FootRaiseHeight, height)

    async def set_continuous_gait(self, enabled: bool) -> bool:
        return await self._cmd(self._sport_client.ContinuousGait, int(enabled))

    async def execute_sport_mode(self, mode: SportMode) -> bool:
        _sport_map = {
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
        fn = _sport_map.get(mode)
        if fn is None:
            logger.error("Unknown SportMode: %s", mode)
            return False
        return await self._cmd(fn)

    async def emergency_stop(self) -> bool:
        ok = await self._cmd(self._sport_client.Damp)
        self._state.estop_active = True
        return ok

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        ok = await self._cmd(self._sport_client.SwitchJoystick, int(not enabled))
        if ok:
            self._state.obstacle_avoidance = enabled
        return ok

    async def set_led(self, r: int, g: int, b: int) -> bool:
        # LED via the Go2's auxiliary lights API — best-effort
        try:
            if self._connected and self._sport_client:
                code, _ = await self._run_sync(
                    self._sport_client.SetBodyLight,
                    int(r), int(g), int(b),
                )
                return code == 0
        except Exception:
            pass
        return False

    async def set_volume(self, level: int) -> bool:
        try:
            if self._connected and self._sport_client:
                code, _ = await self._run_sync(
                    self._sport_client.SetVolume, max(0, min(100, level))
                )
                return code == 0
        except Exception:
            pass
        return False


# ── Simulation bridge ─────────────────────────────────────────────────────────

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

        # Limb-loss simulation state
        # Set via simulate_limb_loss(leg_idx) for plugin testing
        self._lost_limb: int | None = None   # 0=FL 1=FR 2=RL 3=RR

    async def connect(self) -> None:
        self._connected = True
        logger.info("CERBERUS SimBridge connected (no hardware)")
        self._start_state_sim()

    def _start_state_sim(self) -> None:
        """Simulate state drift — realistic battery drain, IMU noise, etc."""
        asyncio.ensure_future(self._sim_loop())

    async def _sim_loop(self) -> None:
        """
        Physically-motivated state simulator.

        Models (all approximations, not true physics):
          Foot forces  — baseline static load split across 4 feet, modulated by
                         gait frequency (trotting produces periodic load peaks),
                         speed (faster = more dynamic load), and mode.
          Joint torques — proportional to foot force and leg geometry; hip flexors
                          carry most load, knee joints follow.
          IMU           — pitch tilts with acceleration onset, rolls with vyaw;
                          both decay back to zero with damping.
          Battery       — slow drain, faster when moving.
        """
        import random

        # Physical constants for Go2
        ROBOT_WEIGHT_N = 147.0       # 15 kg × 9.81
        FOOT_COUNT     = 4
        STATIC_LOAD    = ROBOT_WEIGHT_N / FOOT_COUNT   # ~36.75 N per foot at rest

        # State for running dynamics
        pitch_vel  = 0.0   # angular rate (rad/s) for IMU pitch integration
        roll_vel   = 0.0
        gait_phase = 0.0   # 0..2π, advances with speed

        GAIT_FREQ  = 2.2   # Hz, Go2 trot cadence at 0.5 m/s
        DT         = 0.033 # sim tick period (≈30 Hz)

        while self._connected:
            self._tick += 1
            self._state.timestamp = time.time()

            # ── Read commanded state ──────────────────────────────────────────
            vx   = self._state.velocity_x
            vy   = self._state.velocity_y
            speed = math.hypot(vx, vy)
            mode  = self._state.mode

            # ── Gait phase ────────────────────────────────────────────────────
            # Phase advances faster at higher speed
            freq = GAIT_FREQ * max(0.3, speed / 0.5)
            gait_phase = (gait_phase + 2 * math.pi * freq * DT) % (2 * math.pi)

            # ── Foot forces ───────────────────────────────────────────────────
            if mode in ("lying", "sim_idle", "estop", "damp"):
                # Robot on ground / not standing — minimal force
                forces = [5.0 + random.gauss(0, 0.5)] * 4
            else:
                # Trotting produces a diagonal alternating pattern:
                # FL+RR swing while FR+RL stance, then swap
                # swing foot briefly unloads, stance foot takes extra load
                swing_fraction = 0.35                   # fraction of cycle spent in swing
                trot_swing_fl_rr = math.sin(gait_phase)         # > 0 = FL/RR in swing
                swing_fl = max(0.0, trot_swing_fl_rr)
                swing_fr = max(0.0, -trot_swing_fl_rr)

                # Dynamic load: speed adds impact forces, vyaw adds lateral asymmetry
                dynamic_amp = 1.0 + 0.6 * speed + 0.2 * abs(self._state.velocity_yaw)
                # Noise (terrain roughness proxy)
                noise = [random.gauss(0, 3.0) for _ in range(4)]

                f_fl = STATIC_LOAD * dynamic_amp * (1.0 - swing_fraction * swing_fl) + noise[0]
                f_fr = STATIC_LOAD * dynamic_amp * (1.0 - swing_fraction * swing_fr) + noise[1]
                f_rl = STATIC_LOAD * dynamic_amp * (1.0 - swing_fraction * swing_fr) + noise[2]
                f_rr = STATIC_LOAD * dynamic_amp * (1.0 - swing_fraction * swing_fl) + noise[3]

                forces = [max(0.0, f_fl), max(0.0, f_fr), max(0.0, f_rl), max(0.0, f_rr)]

            # ── Limb-loss physics ─────────────────────────────────────────────
            # When simulate_limb_loss() has been called, model the effects of
            # a non-functional leg: near-zero force & torque, redistributed load
            # on remaining legs, yaw drift, and degraded velocity.
            lost = self._lost_limb
            if lost is not None:
                # 1. Zero out the lost leg's force (just noise)
                forces[lost] = max(0.0, random.gauss(0.5, 0.4))

                # 2. Redistribute lost load across remaining legs
                # Each remaining leg carries ~1/3 extra of the lost leg's normal share
                #   Normal tripod redistribution: diagonal partner +40%, others +30%
                lost_load = STATIC_LOAD * dynamic_amp
                DIAGONAL_PARTNER = {0: 3, 1: 2, 2: 1, 3: 0}   # FL↔RR, FR↔RL
                diag = DIAGONAL_PARTNER[lost]
                remaining = [i for i in range(4) if i != lost]
                for i in remaining:
                    extra = lost_load * (0.40 if i == diag else 0.30)
                    forces[i] = max(0.0, forces[i] + extra)

                # 3. Velocity degradation — tripod can only move at reduced speed
                #    Effective vx is reduced; vy is also compromised
                if abs(self._state.velocity_x) > 0.14:
                    self._state.velocity_x *= 0.14 / abs(self._state.velocity_x)
                if abs(self._state.velocity_y) > 0.06:
                    self._state.velocity_y *= 0.06 / abs(self._state.velocity_y)

                # 4. Yaw drift — asymmetric thrust rotates robot toward missing leg
                #    Left-side missing (FL/RL): negative yaw (rotate left)
                #    Right-side missing (FR/RR): positive yaw (rotate right)
                yaw_drift_sign = -1.0 if lost in (0, 2) else +1.0
                yaw_drift_mag  = 0.04 * min(1.0, abs(self._state.velocity_x) / 0.10)
                self._state.velocity_yaw += yaw_drift_sign * yaw_drift_mag

            self._state.foot_force = forces

            # ── Joint torques ─────────────────────────────────────────────────
            # Simplified: hip flexors ~50% of foot normal force × lever arm (0.213m)
            # knee joints ~30%, hip abductors ~15%
            torques = []
            for leg_idx in range(4):
                ff = forces[leg_idx]
                torques.append(ff * 0.015 + random.gauss(0, 0.1))   # hip_ab
                torques.append(ff * 0.213 * 0.5 + random.gauss(0, 0.3))  # hip_flex
                torques.append(ff * 0.213 * 0.3 + random.gauss(0, 0.2))  # knee
            # Lost leg joints have near-zero torque (no load, no motor effort needed)
            if lost is not None:
                base_j = lost * 3
                torques[base_j]     = random.gauss(0, 0.04)
                torques[base_j + 1] = random.gauss(0, 0.04)
                torques[base_j + 2] = random.gauss(0, 0.04)
            self._state.joint_torques = torques

            # Joint positions — nominal trot pose with gait-phase oscillation
            positions = []
            for leg_idx in range(4):
                phase_offset = math.pi if leg_idx in (1, 2) else 0.0  # FR/RL offset
                osc = math.sin(gait_phase + phase_offset) * 0.15 * min(1.0, speed / 0.3)
                positions.append(0.0 + random.gauss(0, 0.005))       # hip_ab
                positions.append(-0.67 + osc + random.gauss(0, 0.01)) # hip_flex
                positions.append(1.40 - osc * 0.5 + random.gauss(0, 0.01))  # knee
            # Lost leg hangs in a relaxed, partially-folded position
            if lost is not None:
                base_j = lost * 3
                positions[base_j]     = 0.05 * (1 if lost in (0, 2) else -1)  # slight abduction
                positions[base_j + 1] = -0.45 + random.gauss(0, 0.005)        # hip partially flexed
                positions[base_j + 2] =  1.20 + random.gauss(0, 0.005)        # knee partially bent
            self._state.joint_positions  = positions
            self._state.joint_velocities = [random.gauss(0, 0.05)] * 12

            # ── IMU simulation ─────────────────────────────────────────────────
            # Pitch: negative = nose-up; accelerating forward pitches nose up
            PITCH_DAMP = 0.85
            PITCH_GAIN = 0.04
            pitch_vel  = pitch_vel * PITCH_DAMP - vx * PITCH_GAIN
            # Lost front leg → nose drops slightly; lost rear → nose rises
            if lost is not None:
                pitch_bias = +0.03 if lost in (2, 3) else -0.03   # RL/RR loss → nose up
                pitch_vel += pitch_bias * 0.1
            self._state.pitch = self._state.pitch + pitch_vel * DT + random.gauss(0, 0.001)
            self._state.pitch = max(-0.4, min(0.4, self._state.pitch))

            # Roll: lateral acceleration and yaw rate
            ROLL_DAMP = 0.88
            ROLL_GAIN = 0.03
            roll_vel  = roll_vel * ROLL_DAMP + vy * ROLL_GAIN + self._state.velocity_yaw * 0.01
            # Lost leg causes slight body lean toward missing side
            if lost is not None:
                roll_bias = +0.02 if lost in (0, 2) else -0.02   # FL/RL loss → lean right (+)
                roll_vel += roll_bias * 0.15
            self._state.roll  = self._state.roll + roll_vel * DT + random.gauss(0, 0.001)
            self._state.roll  = max(-0.4, min(0.4, self._state.roll))

            # Accelerometer (gravity-dominant, small dynamic component)
            self._state.imu_acc_x = vx * 0.5 + random.gauss(0, 0.1)
            self._state.imu_acc_y = vy * 0.3 + random.gauss(0, 0.1)
            self._state.imu_acc_z = -9.81 + random.gauss(0, 0.05)

            # ── Battery ────────────────────────────────────────────────────────
            # Drain faster when moving; tripod gait is ~30% less efficient
            drain_rate = 0.0004 + speed * 0.0006
            if lost is not None:
                drain_rate *= 1.30   # remaining legs work harder
            self._state.battery_percent = max(0.0, self._state.battery_percent - drain_rate)
            self._state.battery_voltage = 24.0 * (self._state.battery_percent / 100.0) + 10.0

            await asyncio.sleep(DT)

    async def disconnect(self) -> None:
        self._connected = False

    async def get_state(self) -> RobotState:
        return self._state

    # ── Limb-loss simulation API ──────────────────────────────────────────────

    def simulate_limb_loss(self, leg_idx: int) -> None:
        """
        Mark one leg as non-functional in the simulation.

        Effects modelled in _sim_loop:
          • Foot force → near zero (just noise)
          • Joint torques → near zero for that leg's three joints
          • Remaining foot forces increase by ~30–40% (weight redistribution)
          • Forward velocity capped at tripod safe speed (~0.14 m/s)
          • Yaw drift introduced (asymmetric thrust)
          • Body roll/pitch bias toward missing-leg side
          • Battery drain increases by 30% (remaining legs work harder)
          • Lost leg joints hang in a relaxed partially-folded position

        leg_idx: 0=FL, 1=FR, 2=RL, 3=RR
        """
        if leg_idx not in range(4):
            raise ValueError(f"leg_idx must be 0–3, got {leg_idx}")
        names = ["FL", "FR", "RL", "RR"]
        self._lost_limb = leg_idx
        logger.info("[SimBridge] Simulating limb loss: leg %d (%s)", leg_idx, names[leg_idx])

    def clear_limb_loss(self) -> None:
        """Restore all four legs to normal simulation."""
        self._lost_limb = None
        logger.info("[SimBridge] Limb loss simulation cleared — all legs nominal")

    @property
    def lost_limb(self) -> int | None:
        """Index of currently simulated lost limb, or None."""
        return self._lost_limb

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
    """
    Instantiate the correct bridge from environment variables.

    Priority:
      1. GO2_SIMULATION=true  → SimBridge  (software-only, no hardware)
      2. GO2_ROS2=true        → Ros2Bridge (requires rclpy + unitree_ros2)
      3. default              → RealBridge (CycloneDDS, physical Go2)
    """
    simulation = os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1", "yes")
    if simulation:
        logger.info("Creating SimBridge (GO2_SIMULATION=true)")
        return SimBridge()

    mujoco_mode = os.getenv("GO2_MUJOCO", "false").lower() in ("true", "1", "yes")
    if mujoco_mode:
        from cerberus.bridge.mujoco_bridge import MuJocoBridge
        logger.info("Creating MuJocoBridge (GO2_MUJOCO=true)")
        return MuJocoBridge()

    ros2 = os.getenv("GO2_ROS2", "false").lower() in ("true", "1", "yes")
    if ros2:
        from cerberus.bridge.ros2_bridge import Ros2Bridge
        logger.info("Creating Ros2Bridge (GO2_ROS2=true)")
        return Ros2Bridge()

    iface = os.getenv("GO2_NETWORK_INTERFACE", os.getenv("GO2_IFACE", "eth0"))
    logger.info("Creating RealBridge on interface '%s'", iface)
    return RealBridge(iface)
