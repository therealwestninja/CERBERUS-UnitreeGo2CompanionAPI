"""
cerberus/bridge/mujoco_bridge.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS MuJoCo Physics Bridge

Replaces SimBridge's hand-rolled force approximations with full MuJoCo
rigid-body physics for the Unitree Go2.

Requires:
    pip install mujoco>=3.1.0
    # Get the Go2 MuJoCo model from unitree_mujoco:
    git clone https://github.com/unitreerobotics/unitree_mujoco.git
    # Set CERBERUS_MUJOCO_MODEL=path/to/go2/scene.xml

Activation:
    GO2_MUJOCO=true  (in .env)
    GO2_SIMULATION=true  (still required — MuJoCo is a sim bridge)
    CERBERUS_MUJOCO_MODEL=path/to/go2_scene.xml  (optional, auto-detected)

Fallback:
    If mujoco is not installed or the model is not found, create_bridge()
    falls back to SimBridge with a warning (never a hard crash).

Architecture
────────────
The bridge runs a MuJoCo step loop in a background thread (MuJoCo is
not async-safe).  Communication uses Python queues:

    asyncio event loop       threading.Thread
    ─────────────────        ─────────────────
    move(vx, vy, vyaw)  →   _cmd_queue  →  CPG target update
    get_state()         ←   _state_copy ←  physics readback

CPG (Central Pattern Generator) — Ijspeert 2008
────────────────────────────────────────────────
A Hopf oscillator drives each leg at the commanded gait frequency.
Diagonal pairs (FL+RR, FR+RL) are coupled in-phase (trot pattern).
Adjacent pairs are anti-phase.

For each leg i:
    dθᵢ/dt = ω + Σⱼ kᵢⱼ sin(θⱼ − θᵢ − φᵢⱼ)
    q_hip_flex(i) = q₀ + A_swing × cos(θᵢ)
    q_knee(i)     = q₀ + A_knee  × cos(θᵢ + π/4)

where:
    ω      = 2π × gait_frequency (Hz)
    kᵢⱼ   = coupling strength (default 5.0)
    φᵢⱼ   = desired phase offset (0 or π for trot)
    A_*    = amplitude scaled by commanded forward speed

Joint PD controller maps CPG output to actuator torques:
    τᵢ = Kp × (q_target - q_actual) + Kd × (dq_target - dq_actual)

where Kp/Kd are tuned to the Go2 actuator characteristics.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty
from typing import Callable, List, Optional

from cerberus.bridge.go2_bridge import BridgeBase, RobotState, SportMode

logger = logging.getLogger(__name__)

# ── Go2 physical constants ────────────────────────────────────────────────────

ROBOT_MASS_KG    = 15.0
G                = 9.81
STATIC_LOAD_N    = ROBOT_MASS_KG * G / 4   # ≈ 36.75 N

# Joint indices within the 12-DOF array
LEG_JOINT_BASE   = [0, 3, 6, 9]            # [FL, FR, RL, RR] × 3
LEG_NAMES        = ["FL", "FR", "RL", "RR"]

# Nominal stance joint angles (radians) — from URDF standing pose
Q0_HIP_AB   =  0.00
Q0_HIP_FLEX = -0.67
Q0_KNEE     =  1.40

# PD gains — tuned to Go2 actuator stiffness
KP_HIP_AB   =  40.0
KD_HIP_AB   =   0.8
KP_HIP_FLEX =  40.0
KD_HIP_FLEX =   0.8
KP_KNEE     =  60.0
KD_KNEE     =   1.5

# CPG parameters
GAIT_FREQ_HZ    = 2.2     # Hz — Go2 trot cadence at 0.5 m/s
SWING_AMPLITUDE = 0.35    # rad peak-to-peak hip_flex oscillation
KNEE_AMPLITUDE  = 0.30    # rad peak-to-peak knee oscillation
CPG_COUPLING    = 5.0     # inter-leg coupling strength

# Trot diagonal phase offsets: FL+RR in-phase (0), FR+RL in-phase (0),
#  FL+FR anti-phase (π), etc.
# Matrix φᵢⱼ — desired phase of leg i relative to leg j
#  Leg order: 0=FL, 1=FR, 2=RL, 3=RR
TROT_PHASE_OFFSET = [
    [0.0,        math.pi, math.pi, 0.0      ],  # FL row
    [math.pi,    0.0,     0.0,     math.pi  ],  # FR row
    [math.pi,    0.0,     0.0,     math.pi  ],  # RL row
    [0.0,        math.pi, math.pi, 0.0      ],  # RR row
]

# Environment variable for model path
MODEL_ENV_VAR  = "CERBERUS_MUJOCO_MODEL"
DEFAULT_SEARCH = [
    Path("cerberus/assets/go2_scene.xml"),
    Path("unitree_mujoco/unitree_robots/go2/scene.xml"),
    Path(os.path.expanduser("~")) / "unitree_mujoco/unitree_robots/go2/scene.xml",
]


# ─────────────────────────────────────────────────────────────────────────────
# CPG  (Central Pattern Generator)
# ─────────────────────────────────────────────────────────────────────────────

class TrotCPG:
    """
    Four-oscillator Hopf CPG for the Go2 trot gait.

    Each oscillator has a phase θᵢ ∈ [0, 2π).
    Phase is advanced by ω (commanded) + coupling correction per step.
    Output maps phase → target joint angles for hip_flex and knee.
    """

    def __init__(self):
        # Initial phases: FL and RR at 0, FR and RL at π (trot)
        self._theta = [0.0, math.pi, math.pi, 0.0]
        self._omega  = 2 * math.pi * GAIT_FREQ_HZ   # rad/s
        self._speed  = 0.0    # commanded |vx| for amplitude scaling

    def set_velocity(self, vx: float, vy: float, vyaw: float) -> None:
        speed = math.hypot(vx, vy)
        # Gait frequency scales with speed (faster trot)
        hz = GAIT_FREQ_HZ * max(0.4, speed / 0.5)
        self._omega = 2 * math.pi * hz
        self._speed = speed

    def step(self, dt: float) -> list[list[float]]:
        """
        Advance CPG by dt seconds.
        Returns list of (hip_ab, hip_flex, knee) target angles per leg.
        """
        n = 4
        dtheta = [0.0] * n

        for i in range(n):
            coupling = 0.0
            for j in range(n):
                if i != j:
                    phi_ij  = TROT_PHASE_OFFSET[i][j]
                    coupling += CPG_COUPLING * math.sin(
                        self._theta[j] - self._theta[i] - phi_ij
                    )
            dtheta[i] = self._omega + coupling

        # Euler integration
        for i in range(n):
            self._theta[i] = (self._theta[i] + dtheta[i] * dt) % (2 * math.pi)

        # Map phases to joint targets
        amp = min(1.0, self._speed / 0.4) if self._speed > 0.02 else 0.05
        targets = []
        for i in range(n):
            th  = self._theta[i]
            hip_flex = Q0_HIP_FLEX + SWING_AMPLITUDE * amp * math.cos(th)
            knee     = Q0_KNEE     + KNEE_AMPLITUDE  * amp * math.cos(th + math.pi / 4)
            targets.append([Q0_HIP_AB, hip_flex, knee])
        return targets

    def freeze(self) -> list[list[float]]:
        """Target for standing still — nominal pose."""
        return [[Q0_HIP_AB, Q0_HIP_FLEX, Q0_KNEE]] * 4


# ─────────────────────────────────────────────────────────────────────────────
# MuJoCo bridge
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Command:
    kind:  str          # "move" | "stop" | "sport" | "height" | "euler"
    args:  tuple = ()


class MuJocoBridge(BridgeBase):
    """
    High-fidelity physics bridge using MuJoCo for the Unitree Go2.

    On connect(), loads the Go2 MuJoCo model and starts a background
    physics thread that steps the simulation at ~500 Hz, applying CPG-
    derived joint torques and reading back sensor data into a shared
    RobotState.

    If mujoco is not installed or the model file is not found, connect()
    raises RuntimeError with clear installation instructions.
    """

    SIM_DT      = 0.002   # MuJoCo integration step (s) — 500 Hz
    READ_DT     = 0.033   # State readback period (s) — ~30 Hz
    MAX_TORQUE  = 23.7    # Nm — Go2 peak joint torque

    def __init__(self, model_path: Optional[str] = None):
        self._model_path = model_path or os.getenv(MODEL_ENV_VAR)
        self._model      = None    # mujoco.MjModel
        self._data       = None    # mujoco.MjData
        self._cpg        = TrotCPG()
        self._cmd_queue: Queue = Queue(maxsize=8)
        self._state      = RobotState()
        self._connected  = False
        self._thread: Optional[threading.Thread] = None
        self._lock       = threading.Lock()
        self._state_callbacks: list[Callable[[RobotState], None]] = []

        # Live command state (read by physics thread)
        self._cmd_vx:   float = 0.0
        self._cmd_vy:   float = 0.0
        self._cmd_vyaw: float = 0.0
        self._standing:  bool = False
        self._sport_mode: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._connected:
            return

        # Locate and load the model
        model_path = self._resolve_model_path()

        try:
            import mujoco  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mujoco is not installed.\n"
                "Install it:  pip install mujoco>=3.1.0\n"
                "Then get the Go2 model:\n"
                "  git clone https://github.com/unitreerobotics/unitree_mujoco.git\n"
                "  export CERBERUS_MUJOCO_MODEL="
                "unitree_mujoco/unitree_robots/go2/scene.xml"
            ) from exc

        try:
            self._model = mujoco.MjModel.from_xml_path(str(model_path))
            self._data  = mujoco.MjData(self._model)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load MuJoCo model from {model_path}: {exc}\n"
                "Set CERBERUS_MUJOCO_MODEL to a valid Go2 scene.xml path."
            ) from exc

        # Start the physics thread
        self._connected = True
        self._thread = threading.Thread(
            target=self._physics_loop,
            daemon=True,
            name="cerberus_mujoco_physics",
        )
        self._thread.start()
        logger.info(
            "MuJoCo bridge connected — model: %s  nq=%d  nu=%d",
            model_path, self._model.nq, self._model.nu
        )

    async def disconnect(self) -> None:
        self._connected = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("MuJoCo bridge disconnected")

    def _resolve_model_path(self) -> Path:
        """Search for the Go2 MuJoCo scene file."""
        if self._model_path:
            p = Path(self._model_path)
            if p.exists():
                return p
            raise RuntimeError(
                f"CERBERUS_MUJOCO_MODEL path does not exist: {self._model_path}"
            )
        for candidate in DEFAULT_SEARCH:
            if candidate.exists():
                logger.info("Found MuJoCo model: %s", candidate)
                return candidate
        raise RuntimeError(
            "Could not find Go2 MuJoCo model.\n"
            "Set CERBERUS_MUJOCO_MODEL=path/to/go2_scene.xml\n"
            "or clone: https://github.com/unitreerobotics/unitree_mujoco.git"
        )

    # ── Physics thread ────────────────────────────────────────────────────────

    def _physics_loop(self) -> None:
        """
        Main MuJoCo physics loop — runs in background thread.

        Steps the simulation at SIM_DT, applies CPG-derived PD torques,
        and writes sensor data back to self._state at READ_DT intervals.
        """
        import mujoco
        m = self._model
        d = self._data

        # Reset to standing pose
        self._stand_pose(d)
        mujoco.mj_forward(m, d)

        read_accumulator = 0.0

        while self._connected:
            t0 = time.monotonic()

            # Advance CPG and compute joint targets
            self._cpg.set_velocity(self._cmd_vx, self._cmd_vy, self._cmd_vyaw)
            if abs(self._cmd_vx) + abs(self._cmd_vy) + abs(self._cmd_vyaw) < 0.01:
                targets = self._cpg.freeze()
            else:
                targets = self._cpg.step(self.SIM_DT)

            # PD torque control for all 12 joints
            self._apply_pd_torques(m, d, targets)

            # Step physics
            mujoco.mj_step(m, d)

            # Readback at ~30 Hz
            read_accumulator += self.SIM_DT
            if read_accumulator >= self.READ_DT:
                read_accumulator = 0.0
                self._read_state(m, d)

            # Sleep to maintain real-time
            elapsed = time.monotonic() - t0
            sleep_t = self.SIM_DT - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _stand_pose(self, d) -> None:
        """Reset MuJoCo data to Go2 standing pose."""
        try:
            import mujoco
            mujoco.mj_resetData(self._model, d)
            # Set body height (z position of the root body)
            d.qpos[2] = 0.35   # typical Go2 standing height
            # Set each leg's joint angles to nominal stance
            for leg_idx in range(4):
                base = 7 + leg_idx * 3   # 7 = 3 pos + 4 quat
                if base + 2 < len(d.qpos):
                    d.qpos[base + 0] = Q0_HIP_AB
                    d.qpos[base + 1] = Q0_HIP_FLEX
                    d.qpos[base + 2] = Q0_KNEE
        except Exception as exc:
            logger.debug("Stand pose init error: %s", exc)

    def _apply_pd_torques(self, m, d, targets: list[list[float]]) -> None:
        """Compute and apply PD torques for all 12 joints."""
        KP = [KP_HIP_AB,   KP_HIP_FLEX, KP_KNEE]
        KD = [KD_HIP_AB,   KD_HIP_FLEX, KD_KNEE]

        for leg_idx in range(4):
            base_q = 7 + leg_idx * 3   # joint position index in qpos
            base_v = 6 + leg_idx * 3   # joint velocity index in qvel
            base_u = leg_idx * 3       # actuator index in ctrl

            for j in range(3):
                q_target  = targets[leg_idx][j]
                dq_target = 0.0   # target velocity (we don't command acceleration)

                q_actual  = d.qpos[base_q + j] if base_q + j < len(d.qpos) else 0.0
                dq_actual = d.qvel[base_v + j] if base_v + j < len(d.qvel) else 0.0

                torque = (KP[j] * (q_target - q_actual)
                          + KD[j] * (dq_target - dq_actual))
                torque = max(-self.MAX_TORQUE, min(self.MAX_TORQUE, torque))

                if base_u + j < len(d.ctrl):
                    d.ctrl[base_u + j] = torque

    def _read_state(self, m, d) -> None:
        """Read MuJoCo state and write to self._state (thread-safe via copy)."""
        try:
            s = RobotState()
            s.timestamp = time.time()

            # Base position and velocity (qpos[0:7], qvel[0:6])
            if len(d.qpos) >= 7:
                # quat → euler (roll, pitch, yaw)
                w, x, y, z = d.qpos[3], d.qpos[4], d.qpos[5], d.qpos[6]
                s.roll  = math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))
                s.pitch = math.asin(max(-1.0, min(1.0, 2*(w*y-z*x))))
                s.yaw   = math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))
                s.body_height = float(d.qpos[2])

            if len(d.qvel) >= 6:
                s.velocity_x   = float(d.qvel[0])
                s.velocity_y   = float(d.qvel[1])
                s.velocity_yaw = float(d.qvel[5])
                s.imu_acc_x    = float(d.qacc[0]) if len(d.qacc) > 0 else 0.0
                s.imu_acc_y    = float(d.qacc[1]) if len(d.qacc) > 1 else 0.0
                s.imu_acc_z    = float(d.qacc[2]) if len(d.qacc) > 2 else -G

            # Joint states (12 DOF after the 7 root DOF)
            njoints = min(12, max(0, len(d.qpos) - 7))
            s.joint_positions  = [float(d.qpos[7 + i]) for i in range(njoints)]
            s.joint_velocities = [float(d.qvel[6 + i]) for i in range(min(njoints, len(d.qvel) - 6))]
            s.joint_torques    = [float(d.ctrl[i])      for i in range(min(njoints, len(d.ctrl)))]

            # Foot contact forces — from cfrc_ext on foot bodies
            # (cfrc_ext is a 6D wrench per body; we use z-component as normal force)
            foot_forces = [0.0] * 4
            foot_body_names = ["FL_foot", "FR_foot", "RL_foot", "RR_foot"]
            for fi, fname in enumerate(foot_body_names):
                try:
                    bid = m.body(fname).id
                    # cfrc_ext[bid, 2] = z-component of contact force
                    foot_forces[fi] = max(0.0, float(d.cfrc_ext[bid, 2]))
                except Exception:
                    # If body not found, estimate from static load
                    foot_forces[fi] = STATIC_LOAD_N
            s.foot_force = foot_forces

            # Battery — drain based on joint power consumption
            joint_power = sum(abs(t * v)
                              for t, v in zip(s.joint_torques, s.joint_velocities[:12]))
            drain = (30.0 + joint_power * 0.01) * (self.READ_DT / 3600.0)
            s.battery_percent = max(0.0, self._state.battery_percent - drain)
            s.battery_voltage  = 24.0 * (s.battery_percent / 100.0) + 10.0

            with self._lock:
                self._state = s

            for cb in self._state_callbacks:
                try:
                    cb(s)
                except Exception:
                    pass

        except Exception as exc:
            logger.debug("MuJoCo state readback error: %s", exc)

    # ── BridgeBase interface ──────────────────────────────────────────────────

    async def get_state(self) -> RobotState:
        with self._lock:
            return self._state

    async def stand_up(self) -> bool:
        self._standing = True
        self._cmd_vx = self._cmd_vy = self._cmd_vyaw = 0.0
        self._state.mode = "standing"
        return True

    async def stand_down(self) -> bool:
        self._standing = False
        self._state.mode = "lying"
        return True

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        self._cmd_vx   = max(-1.5, min(1.5, vx))
        self._cmd_vy   = max(-0.8, min(0.8, vy))
        self._cmd_vyaw = max(-2.0, min(2.0, vyaw))
        self._state.velocity_x   = self._cmd_vx
        self._state.velocity_y   = self._cmd_vy
        self._state.velocity_yaw = self._cmd_vyaw
        self._state.mode = "trotting"
        return True

    async def stop_move(self) -> bool:
        self._cmd_vx = self._cmd_vy = self._cmd_vyaw = 0.0
        self._state.velocity_x = self._state.velocity_y = self._state.velocity_yaw = 0.0
        self._state.mode = "standing"
        return True

    async def set_body_height(self, height: float) -> bool:
        # Adjust MuJoCo root z position relative to nominal
        if self._data is not None:
            self._data.qpos[2] = 0.35 + height
        return True

    async def set_speed_level(self, level: int) -> bool:
        return True   # handled by CPG amplitude scaling

    async def set_euler(self, roll: float, pitch: float, yaw: float) -> bool:
        # Apply body orientation by modifying quaternion in qpos
        if self._data is not None:
            try:
                import mujoco
                euler = [roll, pitch, yaw]
                quat  = [0.0] * 4
                mujoco.mju_euler2Quat(quat, euler, "xyz")
                self._data.qpos[3:7] = quat
            except Exception as exc:
                logger.debug("set_euler error: %s", exc)
        return True

    async def switch_gait(self, gait_id: int) -> bool:
        # Adjust CPG frequency — lower gait_id = faster
        hz_map = {0: 2.2, 1: 1.6, 2: 1.2, 3: 0.8}
        import importlib; cpg_mod = __import__(__name__, fromlist=["TrotCPG"])
        global GAIT_FREQ_HZ
        GAIT_FREQ_HZ = hz_map.get(gait_id, 2.2)
        return True

    async def set_foot_raise_height(self, height: float) -> bool:
        return True   # not yet modelled in CPG

    async def set_continuous_gait(self, enabled: bool) -> bool:
        return True

    async def execute_sport_mode(self, mode: SportMode) -> bool:
        self._sport_mode = mode.value
        self._state.mode = mode.value
        logger.info("[MuJoCo] Sport mode: %s", mode.value)
        return True

    async def emergency_stop(self) -> bool:
        await self.stop_move()
        self._state.estop_active = True
        return True

    async def set_obstacle_avoidance(self, enabled: bool) -> bool:
        self._state.obstacle_avoidance = enabled
        return True

    async def set_led(self, r: int, g: int, b: int) -> bool:
        return True   # no MuJoCo LED model

    async def set_volume(self, level: int) -> bool:
        return True


# ── Factory helper ────────────────────────────────────────────────────────────

def create_mujoco_bridge() -> MuJocoBridge:
    """
    Create a MuJocoBridge.  Called by create_bridge() when GO2_MUJOCO=true.
    """
    model_path = os.getenv(MODEL_ENV_VAR)
    return MuJocoBridge(model_path=model_path)
