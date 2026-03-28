"""
cerberus/learning/rewards.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Locomotion reward functions for Go2 RL training.

All reward components return values in a normalised range suitable for PPO
training (roughly [-5, +5] per timestep at 50 Hz).

Literature:
  Kumar et al. 2021 — RMA: Rapid Motor Adaptation for Legged Robots
    Reward structure: velocity tracking + energy + stability + alive bonus
  Zhuang et al. 2023 — Robot Parkour Learning
    Adds: contact reward, smoothness penalty
  Lee et al. 2020 — Learning to Walk in Minutes Using Massively Parallel DRL
    Normalisation: exponential kernels for velocity tracking
  Miki et al. 2022 — Learning Robust Perceptive Locomotion (ETH Zurich)
    Adds: foot clearance, body orientation constraints
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class RewardWeights:
    """
    Per-component reward weights.  Scale these to balance the trade-off
    between velocity tracking and energy efficiency.

    Recommended starting values (from Lee et al. 2020):
        vx=2.0, vy=0.5, vyaw=0.5
        energy=0.01  (low — energy matters but tracking is primary)
        stability=0.5, smoothness=0.05
        contact=0.2, alive=0.25
        fall=-10.0
    """
    vx:         float = 2.0
    vy:         float = 0.5
    vyaw:       float = 0.5
    energy:     float = 0.01
    stability:  float = 0.5
    smoothness: float = 0.05
    contact:    float = 0.2
    alive:      float = 0.25
    fall:       float = -10.0


def _exp_kernel(error: float, sigma: float = 0.25) -> float:
    """
    Exponential reward kernel from Lee et al. 2020.

    Returns 1.0 when error=0, decays to near 0 for |error| >> sigma.
    Preferred over squared error because it is bounded and well-scaled
    for neural network training.

        r = exp(-error² / σ²)

    σ controls the tolerance: at |error| = σ, r ≈ 0.37.
    Tuned values for Go2:
        velocity: σ = 0.25 m/s  (comfortable tracking tolerance)
        yaw rate: σ = 0.25 rad/s
    """
    return math.exp(-(error ** 2) / (sigma ** 2))


def reward_velocity_tracking(
    vx: float, vy: float, vyaw: float,
    cmd_vx: float, cmd_vy: float, cmd_vyaw: float,
) -> dict[str, float]:
    """
    Velocity tracking reward — primary locomotion objective.

    Uses exponential kernels (Lee et al. 2020) for each velocity component.
    Returns values in [0, 1] per component (before weighting).

    Forward tracking (vx) uses a wider σ because the task cares more about
    the general direction than exact speed.
    Lateral (vy) and yaw (vyaw) use tighter σ for precise steering.
    """
    return {
        "vx":   _exp_kernel(vx   - cmd_vx,   sigma=0.25),
        "vy":   _exp_kernel(vy   - cmd_vy,   sigma=0.15),
        "vyaw": _exp_kernel(vyaw - cmd_vyaw, sigma=0.25),
    }


def reward_energy(
    torques: np.ndarray,
    joint_velocities: np.ndarray,
) -> float:
    """
    Energy efficiency penalty — power consumed by all 12 joints.

    Based on:  E = Σ |τᵢ × q̇ᵢ|  (instantaneous joint power)

    Normalised by dividing by (mass × g × max_speed × n_joints) so the
    penalty is roughly in the range [-1, 0] at normal locomotion.

    This is the primary efficiency objective from Kumar et al. 2021.
    Keep the weight small (0.005–0.05) so it doesn't dominate.
    """
    if len(torques) == 0 or len(joint_velocities) == 0:
        return 0.0
    n = min(len(torques), len(joint_velocities))
    power = np.sum(np.abs(torques[:n] * joint_velocities[:n]))
    # Normalise: Go2 typical power at trot ≈ 150 W → ~12.5 per joint
    return -float(power) / (12.5 * 12 + 1e-6)


def reward_stability(
    roll: float,
    pitch: float,
) -> float:
    """
    Body orientation stability penalty.

    Penalises body tilt to discourage falling and encourage an upright
    base, which also makes the proprioceptive sensor readings more useful.

    Returns in [-1, 0] (worst: π/2 tilt in both axes = -1).
    """
    max_tilt_sq = (math.pi / 2) ** 2
    tilt_sq = roll**2 + pitch**2
    return -min(1.0, tilt_sq / max_tilt_sq)


def reward_action_smoothness(
    action: np.ndarray,
    prev_action: np.ndarray,
) -> float:
    """
    Action smoothness penalty — discourages jerky or oscillating commands.

    Penalises large action deltas (Zhuang et al. 2023).
    Returns in [-1, 0] (worst: max action change every step = -1).
    """
    delta = action - prev_action
    return -float(np.sum(delta ** 2)) / (len(action) + 1e-6)


def reward_foot_contact(
    foot_forces: list[float],
    gait_phase: float,
    cmd_speed: float,
) -> float:
    """
    Foot contact reward — bonus for expected trot contact pattern.

    In trot gait, diagonal pairs (FL+RR, FR+RL) alternate between stance
    and swing.  At zero speed, all feet should be in stance.

    Returns in [0, 1] (1.0 = perfect contact pattern).
    This is a simplified heuristic; full contact reward would require
    explicit foot trajectory planning.
    """
    if cmd_speed < 0.05:
        # At standstill: reward all four feet in contact
        contact_count = sum(1 for f in foot_forces[:4] if f > 5.0)
        return contact_count / 4.0
    else:
        # During trot: reward alternating diagonal contact
        # FL+RR should be in contact when FR+RL are in swing and vice versa
        fl, fr, rl, rr = (f > 5.0 for f in foot_forces[:4])
        diag1 = fl and rr
        diag2 = fr and rl
        # Reward when exactly one diagonal pair is in contact
        if diag1 ^ diag2:
            return 1.0
        elif diag1 or diag2:
            return 0.5
        else:
            return 0.0


def compute_reward(
    data,           # mujoco.MjData
    model,          # mujoco.MjModel (unused currently — future foot pos)
    torques:    np.ndarray,
    action:     np.ndarray,
    prev_action:np.ndarray,
    cmd:        tuple[float, float, float],   # (vx_cmd, vy_cmd, vyaw_cmd)
    weights:    RewardWeights,
) -> tuple[float, dict[str, float]]:
    """
    Compute the full reward for one MuJoCo step.

    Returns (total_reward, breakdown_dict) where breakdown_dict
    contains each component for logging / curriculum diagnostics.
    """
    cmd_vx, cmd_vy, cmd_vyaw = cmd

    # ── Extract state from MuJoCo data ───────────────────────────────────────
    vx   = float(data.qvel[0]) if len(data.qvel) > 0 else 0.0
    vy   = float(data.qvel[1]) if len(data.qvel) > 1 else 0.0
    vyaw = float(data.qvel[5]) if len(data.qvel) > 5 else 0.0

    # IMU orientation
    if len(data.qpos) >= 7:
        w, x, y, z = data.qpos[3], data.qpos[4], data.qpos[5], data.qpos[6]
        roll  = math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y))
        pitch = math.asin(max(-1, min(1, 2*(w*y-z*x))))
    else:
        roll = pitch = 0.0

    joint_vel = (np.array(data.qvel[6:18], dtype=np.float32)
                 if len(data.qvel) >= 18 else np.zeros(12))

    # Foot forces (z component of contact wrench)
    foot_forces = [0.0] * 4
    for fi, fname in enumerate(["FL_foot", "FR_foot", "RL_foot", "RR_foot"]):
        try:
            bid = model.body(fname).id
            foot_forces[fi] = max(0.0, float(data.cfrc_ext[bid, 2]))
        except Exception:
            pass

    # ── Compute components ────────────────────────────────────────────────────
    vel_rewards = reward_velocity_tracking(vx, vy, vyaw, cmd_vx, cmd_vy, cmd_vyaw)
    r_vx        = vel_rewards["vx"]   * weights.vx
    r_vy        = vel_rewards["vy"]   * weights.vy
    r_vyaw      = vel_rewards["vyaw"] * weights.vyaw
    r_energy    = reward_energy(torques, joint_vel) * weights.energy
    r_stability = reward_stability(roll, pitch) * weights.stability
    r_smooth    = reward_action_smoothness(action, prev_action) * weights.smoothness
    r_contact   = reward_foot_contact(
        foot_forces,
        gait_phase=0.0,          # phase not tracked at reward level
        cmd_speed=abs(cmd_vx),
    ) * weights.contact
    r_alive     = weights.alive

    total = r_vx + r_vy + r_vyaw + r_energy + r_stability + r_smooth + r_contact + r_alive

    breakdown = {
        "r_vx":        round(r_vx, 4),
        "r_vy":        round(r_vy, 4),
        "r_vyaw":      round(r_vyaw, 4),
        "r_energy":    round(r_energy, 4),
        "r_stability": round(r_stability, 4),
        "r_smooth":    round(r_smooth, 4),
        "r_contact":   round(r_contact, 4),
        "r_alive":     round(r_alive, 4),
        "total":       round(total, 4),
        # Debug info
        "vx_actual":   round(vx, 3),
        "vy_actual":   round(vy, 3),
        "vyaw_actual": round(vyaw, 3),
        "vx_error":    round(abs(vx - cmd_vx), 3),
    }
    return total, breakdown
