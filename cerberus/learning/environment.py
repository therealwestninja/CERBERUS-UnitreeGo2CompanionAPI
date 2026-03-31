"""
cerberus/learning/environment.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Gymnasium-compatible RL environment for Unitree Go2 locomotion.

Observation and action spaces are designed for sim-to-real transfer:
observations match the real robot's sensor suite, and actions are joint
position targets executed by a PD controller (matching the hardware SDK).

Literature:
  Kumar et al. 2021 — RMA: Rapid Motor Adaptation for Legged Robots
  Zhuang et al. 2023 — Robot Parkour Learning (Go2-adjacent architecture)
  Lee et al. 2020   — Learning to Walk in Minutes
  Miki et al. 2022  — Learning Robust Perceptive Locomotion (ETH Zurich)

Requires: pip install mujoco>=3.1.0 gymnasium numpy

Usage:
    from cerberus.learning.environment import CerberusEnv, EnvConfig
    import gymnasium as gym

    env = CerberusEnv(EnvConfig(target_vx=0.5, target_vy=0.0, target_vyaw=0.0))
    obs, info = env.reset()
    for _ in range(1000):
        action = env.action_space.sample()
        obs, reward, terminated, truncated, info = env.step(action)
        if terminated or truncated:
            obs, info = env.reset()
    env.close()
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional, SupportsFloat

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
    HAS_GYMNASIUM = True
except ImportError:
    HAS_GYMNASIUM = False
    # Provide a minimal stub so the module can be imported without gymnasium
    class gym:
        class Env:
            pass
    class spaces:
        @staticmethod
        def Box(*a, **kw): return None


# ── Physical constants ────────────────────────────────────────────────────────

ROBOT_MASS_KG    = 15.0
G                = 9.81
NOMINAL_HEIGHT   = 0.27    # m — standing height
FALL_HEIGHT      = 0.20    # m — below this = fallen

# Joint limits (rad) — from Go2 URDF
JOINT_LO = np.array([
    -1.047, -3.490, -0.524,   # FL: hip_ab, hip_flex, knee
    -1.047, -3.490, -0.524,   # FR
    -1.047, -3.490, -0.524,   # RL
    -1.047, -3.490, -0.524,   # RR
], dtype=np.float32)

JOINT_HI = np.array([
    1.047, 1.745, 4.189,
    1.047, 1.745, 4.189,
    1.047, 1.745, 4.189,
    1.047, 1.745, 4.189,
], dtype=np.float32)

# Nominal standing joint angles — action space centre
JOINT_NOMINAL = np.array([
    0.0, -0.67, 1.40,
    0.0, -0.67, 1.40,
    0.0, -0.67, 1.40,
    0.0, -0.67, 1.40,
], dtype=np.float32)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class EnvConfig:
    """
    Environment configuration — all parameters tunable without modifying code.

    The target velocity command is randomised during training (curriculum).
    Set target_* directly to train for a specific velocity profile.
    """

    # ── Command ──────────────────────────────────────────────────────────────
    target_vx:   float = 0.5    # m/s forward command
    target_vy:   float = 0.0    # m/s lateral command
    target_vyaw: float = 0.0    # rad/s yaw command

    # Randomise target velocity at each reset (curriculum learning)
    randomise_command:  bool  = True
    max_target_vx:      float = 1.0     # m/s
    max_target_vy:      float = 0.3     # m/s
    max_target_vyaw:    float = 0.5     # rad/s

    # ── Episode limits ────────────────────────────────────────────────────────
    episode_length_s:   float = 20.0   # seconds
    control_dt:         float = 0.02   # 50 Hz policy + 500 Hz physics

    # ── Reward weights (normalised to ~[-5, +5] range) ─────────────────────
    reward_vx_weight:       float = 2.0
    reward_vy_weight:       float = 0.5
    reward_vyaw_weight:     float = 0.5
    reward_energy_weight:   float = 0.01
    reward_stability_weight:float = 0.5
    reward_smoothness_weight:float= 0.05
    reward_contact_weight:  float = 0.2
    reward_alive_bonus:     float = 0.25
    reward_fall_penalty:    float = -10.0

    # ── Observation noise (sim-to-real gap modelling) ─────────────────────
    add_obs_noise:   bool  = True
    pos_noise:       float = 0.01   # rad   — joint position noise
    vel_noise:       float = 0.05   # rad/s — joint velocity noise
    imu_noise:       float = 0.01   # rad   — IMU angle noise

    # ── Physics ───────────────────────────────────────────────────────────
    physics_dt:   float = 0.002    # 500 Hz MuJoCo steps
    mujoco_model: str   = ""       # path to scene.xml (auto-detected if empty)


# ── Observation and action space sizes ───────────────────────────────────────

OBS_DIM = (
    3    # IMU: roll, pitch, yaw
  + 3    # IMU: acc_x, acc_y, acc_z
  + 3    # base linear velocity: vx, vy, vz
  + 3    # base angular velocity: roll_rate, pitch_rate, yaw_rate
  + 12   # joint positions (12 DOF)
  + 12   # joint velocities
  + 12   # joint torques (from previous step)
  + 4    # foot contact binary flags
  + 3    # velocity command: vx_cmd, vy_cmd, vyaw_cmd
  + 1    # body height
)  # = 56

ACT_DIM = 12   # joint position targets (relative to nominal)


# ── Environment ───────────────────────────────────────────────────────────────

class CerberusEnv(gym.Env):
    """
    Gymnasium environment for Unitree Go2 locomotion policy training.

    Observation space (56-dim):
        [0:3]   IMU orientation: roll, pitch, yaw (rad)
        [3:6]   IMU acceleration: ax, ay, az (m/s²)
        [6:9]   Base linear velocity: vx, vy, vz (m/s)
        [9:12]  Base angular velocity: ωx, ωy, ωz (rad/s)
        [12:24] Joint positions q (rad) — 12 DOF
        [24:36] Joint velocities q̇ (rad/s) — 12 DOF
        [36:48] Joint torques τ from previous step (N·m)
        [48:52] Foot contact flags (0/1) — FL, FR, RL, RR
        [52:55] Velocity command: vx_cmd, vy_cmd, vyaw_cmd
        [55]    Body height (m)

    Action space (12-dim):
        Relative joint position targets Δq added to nominal stance:
            q_target = JOINT_NOMINAL + action * ACTION_SCALE

        The PD controller applies:
            τ = Kp × (q_target - q) + Kd × (0 - q̇)

        This matches the hardware SDK's joint control mode, enabling
        zero-shot sim-to-real transfer of the policy.

    Reward (see rewards.py for full formulation):
        r = w_vx  × track(vx)
          + w_vy  × track(vy)
          + w_yaw × track(vyaw)
          - w_energy  × Σ|τᵢ × q̇ᵢ|
          - w_stab    × (roll² + pitch²)
          - w_smooth  × Σ|Δaction|²
          + w_contact × contact_bonus
          + alive_bonus
          [+ fall_penalty if fallen]
    """

    metadata = {"render_modes": ["human", "rgb_array"], "render_fps": 50}

    ACTION_SCALE = 0.25   # rad — max deviation from nominal stance per joint

    def __init__(
        self,
        config: Optional[EnvConfig] = None,
        render_mode: Optional[str]  = None,
    ):
        if not HAS_GYMNASIUM:
            raise ImportError(
                "gymnasium is not installed.\n"
                "Install:  pip install gymnasium"
            )

        self.cfg         = config or EnvConfig()
        self.render_mode = render_mode
        self._mj_model   = None
        self._mj_data    = None
        self._step_count = 0
        self._prev_action = np.zeros(ACT_DIM, dtype=np.float32)
        self._prev_torques= np.zeros(ACT_DIM, dtype=np.float32)
        self._episode_start_time = 0.0

        # Velocity command (may be randomised each reset)
        self._cmd_vx   = self.cfg.target_vx
        self._cmd_vy   = self.cfg.target_vy
        self._cmd_vyaw = self.cfg.target_vyaw

        # ── Spaces ────────────────────────────────────────────────────────────

        obs_lo = np.full(OBS_DIM, -np.inf, dtype=np.float32)
        obs_hi = np.full(OBS_DIM, +np.inf, dtype=np.float32)
        # Clip angles
        obs_lo[:6]  = -math.pi;  obs_hi[:6]  = math.pi
        # Foot contacts
        obs_lo[48:52] = 0.0;     obs_hi[48:52] = 1.0

        self.observation_space = spaces.Box(obs_lo, obs_hi, dtype=np.float32)
        self.action_space      = spaces.Box(
            low  = -1.0,
            high = +1.0,
            shape= (ACT_DIM,),
            dtype= np.float32,
        )

    # ── MuJoCo lifecycle ──────────────────────────────────────────────────────

    def _load_mujoco(self):
        try:
            import mujoco
        except ImportError as exc:
            raise ImportError(
                "mujoco is not installed.\n"
                "Install:  pip install mujoco>=3.1.0\n"
                "Model:    git clone https://github.com/unitreerobotics/unitree_mujoco"
            ) from exc

        if self.cfg.mujoco_model:
            model_path = self.cfg.mujoco_model
        else:
            # Auto-detect common locations
            import os
            from pathlib import Path
            candidates = [
                Path("cerberus/assets/go2_scene.xml"),
                Path("unitree_mujoco/unitree_robots/go2/scene.xml"),
                Path(os.path.expanduser("~")) / "unitree_mujoco/unitree_robots/go2/scene.xml",
            ]
            model_path = next((str(p) for p in candidates if p.exists()), None)
            if model_path is None:
                raise FileNotFoundError(
                    "Go2 MuJoCo model not found. Set EnvConfig(mujoco_model=...) or:\n"
                    "  git clone https://github.com/unitreerobotics/unitree_mujoco.git\n"
                    "  export CERBERUS_MUJOCO_MODEL=unitree_mujoco/unitree_robots/go2/scene.xml"
                )

        self._mj_model = mujoco.MjModel.from_xml_path(model_path)
        self._mj_data  = mujoco.MjData(self._mj_model)

    def _reset_physics(self):
        import mujoco
        mujoco.mj_resetData(self._mj_model, self._mj_data)
        d = self._mj_data
        d.qpos[2] = 0.35    # initial height
        # Set nominal joint angles
        for i, q in enumerate(JOINT_NOMINAL):
            idx = 7 + i
            if idx < len(d.qpos):
                d.qpos[idx] = q
        # Small random perturbation for robustness
        d.qpos[2]  += np.random.uniform(-0.02, 0.02)
        d.qpos[3:7] += np.random.uniform(-0.05, 0.05, 4)   # orientation noise
        mujoco.mj_forward(self._mj_model, self._mj_data)

    # ── Gymnasium API ─────────────────────────────────────────────────────────

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> tuple[np.ndarray, dict]:
        super().reset(seed=seed)

        if self._mj_model is None:
            self._load_mujoco()

        self._reset_physics()
        self._step_count       = 0
        self._prev_action[:]   = 0.0
        self._prev_torques[:]  = 0.0
        self._episode_start_time = time.monotonic()

        # Randomise velocity command for curriculum learning
        if self.cfg.randomise_command:
            rng = self.np_random
            speed = rng.uniform(0.0, self.cfg.max_target_vx)
            angle = rng.uniform(-math.pi, math.pi)
            self._cmd_vx   = speed * math.cos(angle)
            self._cmd_vy   = min(abs(self._cmd_vx) * 0.3,
                                 rng.uniform(0, self.cfg.max_target_vy))
            self._cmd_vyaw = rng.uniform(-self.cfg.max_target_vyaw,
                                          self.cfg.max_target_vyaw)
        else:
            self._cmd_vx   = self.cfg.target_vx
            self._cmd_vy   = self.cfg.target_vy
            self._cmd_vyaw = self.cfg.target_vyaw

        obs  = self._get_obs()
        info = {"cmd": (self._cmd_vx, self._cmd_vy, self._cmd_vyaw)}
        return obs, info

    def step(
        self, action: np.ndarray
    ) -> tuple[np.ndarray, SupportsFloat, bool, bool, dict]:
        import mujoco
        action = np.clip(action, -1.0, 1.0).astype(np.float32)
        self._step_count += 1

        # Compute joint position targets
        q_target = JOINT_NOMINAL + action * self.ACTION_SCALE
        q_target = np.clip(q_target, JOINT_LO, JOINT_HI)

        # PD controller parameters (Go2 actuator characteristics)
        KP = np.array([40, 40, 60] * 4, dtype=np.float32)
        KD = np.array([0.8, 0.8, 1.5] * 4, dtype=np.float32)

        # Sub-step at physics_dt inside each control_dt
        n_physics = max(1, round(self.cfg.control_dt / self.cfg.physics_dt))
        torques = np.zeros(ACT_DIM, dtype=np.float32)

        for _ in range(n_physics):
            d = self._mj_data
            q     = np.array(d.qpos[7:7+ACT_DIM], dtype=np.float32)
            dq    = np.array(d.qvel[6:6+ACT_DIM], dtype=np.float32)
            tau   = KP * (q_target - q) - KD * dq
            tau   = np.clip(tau, -23.7, 23.7)   # Go2 peak torque
            torques = tau

            if len(d.ctrl) >= ACT_DIM:
                d.ctrl[:ACT_DIM] = tau
            mujoco.mj_step(self._mj_model, d)

        self._prev_torques = torques.copy()

        # ── Reward ────────────────────────────────────────────────────────────
        from cerberus.learning.rewards import compute_reward, RewardWeights
        weights = RewardWeights(
            vx        = self.cfg.reward_vx_weight,
            vy        = self.cfg.reward_vy_weight,
            vyaw      = self.cfg.reward_vyaw_weight,
            energy    = self.cfg.reward_energy_weight,
            stability = self.cfg.reward_stability_weight,
            smoothness= self.cfg.reward_smoothness_weight,
            contact   = self.cfg.reward_contact_weight,
            alive     = self.cfg.reward_alive_bonus,
            fall      = self.cfg.reward_fall_penalty,
        )
        reward, reward_info = compute_reward(
            data      = self._mj_data,
            model     = self._mj_model,
            torques   = torques,
            action    = action,
            prev_action=self._prev_action,
            cmd       = (self._cmd_vx, self._cmd_vy, self._cmd_vyaw),
            weights   = weights,
        )
        self._prev_action = action.copy()

        # ── Termination ───────────────────────────────────────────────────────
        height     = float(self._mj_data.qpos[2])
        fallen     = height < FALL_HEIGHT
        time_limit = (time.monotonic() - self._episode_start_time) >= self.cfg.episode_length_s

        if fallen:
            reward += self.cfg.reward_fall_penalty

        obs  = self._get_obs()
        info = {
            "height": height,
            "reward_breakdown": reward_info,
            "cmd": (self._cmd_vx, self._cmd_vy, self._cmd_vyaw),
        }
        return obs, float(reward), fallen, time_limit, info

    def _get_obs(self) -> np.ndarray:
        """Assemble the 56-dim observation vector from MuJoCo state."""
        if self._mj_data is None:
            return np.zeros(OBS_DIM, dtype=np.float32)

        d  = self._mj_data

        # IMU orientation (quaternion → euler via qpos[3:7])
        if len(d.qpos) >= 7:
            w, x, y, z = d.qpos[3], d.qpos[4], d.qpos[5], d.qpos[6]
            roll  = math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))
            pitch = math.asin(max(-1, min(1, 2*(w*y-z*x))))
            yaw   = math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z))
        else:
            roll = pitch = yaw = 0.0

        imu_acc = np.array(d.qacc[:3], dtype=np.float32) if len(d.qacc) >= 3 else np.zeros(3)
        base_vel= np.array(d.qvel[:3], dtype=np.float32) if len(d.qvel) >= 3 else np.zeros(3)
        base_ang= np.array(d.qvel[3:6], dtype=np.float32) if len(d.qvel) >= 6 else np.zeros(3)

        # Joint state
        nj = min(ACT_DIM, max(0, len(d.qpos) - 7))
        q  = np.array(d.qpos[7:7+nj], dtype=np.float32)
        dq = np.array(d.qvel[6:6+nj], dtype=np.float32)
        q  = np.pad(q,  (0, ACT_DIM - nj))
        dq = np.pad(dq, (0, ACT_DIM - nj))

        # Foot contacts (1 = in contact, 0 = in swing)
        contacts = np.zeros(4, dtype=np.float32)
        foot_names = ["FL_foot", "FR_foot", "RL_foot", "RR_foot"]
        for fi, fname in enumerate(foot_names):
            try:
                bid = self._mj_model.body(fname).id
                contacts[fi] = 1.0 if d.cfrc_ext[bid, 2] > 1.0 else 0.0
            except Exception:
                contacts[fi] = 1.0   # assume contact if body not found

        obs = np.concatenate([
            [roll, pitch, yaw],          # [0:3]
            imu_acc,                     # [3:6]
            base_vel,                    # [6:9]
            base_ang,                    # [9:12]
            q,                           # [12:24]
            dq,                          # [24:36]
            self._prev_torques,          # [36:48]
            contacts,                    # [48:52]
            [self._cmd_vx, self._cmd_vy, self._cmd_vyaw],  # [52:55]
            [d.qpos[2] if len(d.qpos) > 2 else NOMINAL_HEIGHT],  # [55]
        ]).astype(np.float32)

        # Add observation noise for sim-to-real robustness
        if self.cfg.add_obs_noise and self.np_random is not None:
            rng = self.np_random
            obs[:3]   += rng.normal(0, self.cfg.imu_noise,  3).astype(np.float32)
            obs[12:24] += rng.normal(0, self.cfg.pos_noise, 12).astype(np.float32)
            obs[24:36] += rng.normal(0, self.cfg.vel_noise, 12).astype(np.float32)

        return obs

    def render(self) -> Optional[np.ndarray]:
        if self.render_mode == "rgb_array" and self._mj_model is not None:
            try:
                import mujoco
                renderer = mujoco.Renderer(self._mj_model, height=480, width=640)
                renderer.update_scene(self._mj_data)
                return renderer.render()
            except Exception:
                pass
        return None

    def close(self):
        self._mj_model = None
        self._mj_data  = None
