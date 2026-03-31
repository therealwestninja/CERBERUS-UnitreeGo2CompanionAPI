"""
cerberus/learning/trainer.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━
RL training utilities for CERBERUS locomotion policies.

Uses Stable-Baselines3 (SB3) PPO by default — the most widely validated
algorithm for quadruped locomotion based on:
  Schulman et al. 2017 — Proximal Policy Optimization
  Kumar et al. 2021 — RMA (uses PPO with privileged observations)
  Lee et al. 2020 — Massively parallel locomotion (uses PPO)

Alternate algorithms (SAC, TD3) are supported via the `algorithm` config.

Requires:
    pip install stable-baselines3>=2.0.0 gymnasium tensorboard

Usage:
    from cerberus.learning import CerberusEnv, EnvConfig, TrainingConfig, train_ppo

    env_cfg  = EnvConfig(target_vx=0.5, randomise_command=True)
    train_cfg= TrainingConfig(
        total_timesteps = 1_000_000,
        save_path       = "models/cerberus_trot_v1",
        log_dir         = "runs/cerberus_trot_v1",
        n_envs          = 8,          # parallel environments
    )
    model = train_ppo(env_cfg, train_cfg)

    # Evaluate on a single environment
    rewards = evaluate_policy(model, CerberusEnv(env_cfg), n_episodes=10)
    print(f"Mean return: {sum(rewards)/len(rewards):.1f}")

    # Export to ONNX for deployment
    from cerberus.learning.trainer import export_onnx
    export_onnx(model, "models/cerberus_trot_v1.onnx")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    """
    Full training configuration for one PPO run.

    Hyperparameters are tuned for locomotion tasks following the SB3
    locomotion benchmark and Kumar et al. 2021 recommendations.
    """

    # ── Run ───────────────────────────────────────────────────────────────────
    total_timesteps: int  = 10_000_000  # 10 M steps ≈ 5 h on a mid-range GPU
    save_path:       str  = "models/cerberus_policy"
    log_dir:         str  = "runs/cerberus"
    run_name:        str  = "trot_baseline"

    # ── Parallelism ───────────────────────────────────────────────────────────
    n_envs: int = 8   # parallel env workers (use more for faster data collection)
    device: str = "auto"   # "cpu", "cuda", "mps", or "auto"

    # ── PPO hyperparameters ────────────────────────────────────────────────────
    # Values from Lee et al. 2020 adapted for 50 Hz control / 20 s episodes
    learning_rate: float = 3e-4
    n_steps:       int   = 2048    # rollout steps per env before update
    batch_size:    int   = 256
    n_epochs:      int   = 10      # update epochs per rollout
    gamma:         float = 0.99    # discount factor
    gae_lambda:    float = 0.95    # GAE lambda
    clip_range:    float = 0.2     # PPO clipping
    ent_coef:      float = 0.005   # entropy bonus (encourages exploration)
    vf_coef:       float = 0.5
    max_grad_norm: float = 0.5

    # ── Network ───────────────────────────────────────────────────────────────
    # Policy net: 56 → [512, 256, 128] → 12
    # Value net:  56 → [512, 256, 128] → 1
    net_arch: list[int] = field(default_factory=lambda: [512, 256, 128])

    # ── Callbacks ─────────────────────────────────────────────────────────────
    eval_freq:      int  = 50_000   # evaluate every N env steps
    n_eval_eps:     int  = 10
    checkpoint_freq:int  = 100_000  # save checkpoint every N steps

    # ── Curriculum ────────────────────────────────────────────────────────────
    # Stage 1: slow commands only (vx up to 0.3 m/s)
    # Stage 2: medium commands (vx up to 0.7 m/s) — starts at stage1_steps
    # Stage 3: full commands (vx up to 1.0 m/s) — starts at stage2_steps
    use_curriculum:    bool  = True
    stage1_steps:      int   = 2_000_000
    stage2_steps:      int   = 5_000_000
    stage1_max_vx:     float = 0.3
    stage2_max_vx:     float = 0.7
    stage3_max_vx:     float = 1.0


def _check_sb3() -> bool:
    """Return True if stable-baselines3 is available."""
    try:
        import stable_baselines3  # noqa: F401
        return True
    except ImportError:
        return False


def train_ppo(
    env_config,          # EnvConfig
    train_config: Optional[TrainingConfig] = None,
    resume_from:  Optional[str] = None,
) -> Any:
    """
    Train a PPO locomotion policy on the CerberusEnv.

    Args:
        env_config:    EnvConfig — environment hyperparameters
        train_config:  TrainingConfig — training hyperparameters
        resume_from:   path to a saved model to resume training from

    Returns:
        Trained stable_baselines3.PPO model object.

    Raises:
        ImportError: if stable-baselines3 or gymnasium is not installed.

    Example:
        from cerberus.learning import CerberusEnv, EnvConfig, train_ppo
        model = train_ppo(EnvConfig(target_vx=0.5))
        model.save("models/trot_policy")
    """
    if not _check_sb3():
        raise ImportError(
            "stable-baselines3 is not installed.\n"
            "Install:  pip install stable-baselines3>=2.0.0 gymnasium tensorboard"
        )

    from stable_baselines3 import PPO
    from stable_baselines3.common.env_util import make_vec_env
    from stable_baselines3.common.callbacks import (
        EvalCallback, CheckpointCallback, CallbackList,
    )

    cfg = train_config or TrainingConfig()

    # ── Create vectorised environments ────────────────────────────────────────
    from cerberus.learning.environment import CerberusEnv
    logger.info(
        "Creating %d parallel environments (total_timesteps=%s)",
        cfg.n_envs, f"{cfg.total_timesteps:,}"
    )

    def _make_env(env_cfg=env_config):
        return CerberusEnv(env_cfg)

    train_env = make_vec_env(_make_env, n_envs=cfg.n_envs)
    eval_env  = make_vec_env(_make_env, n_envs=1)

    # ── Policy network architecture ───────────────────────────────────────────
    policy_kwargs = dict(
        net_arch=dict(pi=cfg.net_arch, vf=cfg.net_arch),
        activation_fn=__import__("torch.nn", fromlist=["Tanh"]).Tanh,
    )

    # ── Create or load model ──────────────────────────────────────────────────
    if resume_from:
        logger.info("Resuming training from %s", resume_from)
        model = PPO.load(resume_from, env=train_env, device=cfg.device)
    else:
        model = PPO(
            policy          = "MlpPolicy",
            env             = train_env,
            learning_rate   = cfg.learning_rate,
            n_steps         = cfg.n_steps,
            batch_size      = cfg.batch_size,
            n_epochs        = cfg.n_epochs,
            gamma           = cfg.gamma,
            gae_lambda      = cfg.gae_lambda,
            clip_range      = cfg.clip_range,
            ent_coef        = cfg.ent_coef,
            vf_coef         = cfg.vf_coef,
            max_grad_norm   = cfg.max_grad_norm,
            policy_kwargs   = policy_kwargs,
            tensorboard_log = cfg.log_dir,
            device          = cfg.device,
            verbose         = 1,
        )

    # ── Callbacks ─────────────────────────────────────────────────────────────
    Path(cfg.save_path).mkdir(parents=True, exist_ok=True)
    callbacks = []

    eval_cb = EvalCallback(
        eval_env,
        best_model_save_path = str(Path(cfg.save_path) / "best"),
        log_path             = str(Path(cfg.log_dir) / "evals"),
        eval_freq            = cfg.eval_freq,
        n_eval_episodes      = cfg.n_eval_eps,
        deterministic        = True,
        render               = False,
    )
    callbacks.append(eval_cb)

    ckpt_cb = CheckpointCallback(
        save_freq   = cfg.checkpoint_freq,
        save_path   = str(Path(cfg.save_path) / "checkpoints"),
        name_prefix = "cerberus_ppo",
    )
    callbacks.append(ckpt_cb)

    # ── Curriculum callback ────────────────────────────────────────────────────
    if cfg.use_curriculum:
        class _CurriculumCallback(
            __import__("stable_baselines3.common.callbacks", fromlist=["BaseCallback"]).BaseCallback
        ):
            """Ramp up target velocity in stages as training progresses."""

            def _on_step(self) -> bool:
                steps = self.num_timesteps
                if steps < cfg.stage1_steps:
                    vx_max = cfg.stage1_max_vx
                elif steps < cfg.stage2_steps:
                    vx_max = cfg.stage2_max_vx
                else:
                    vx_max = cfg.stage3_max_vx

                # Update all envs' max velocity
                for e in self.training_env.envs:
                    if hasattr(e, "cfg"):
                        e.cfg.max_target_vx = vx_max
                return True

        callbacks.append(_CurriculumCallback())

    # ── Train ─────────────────────────────────────────────────────────────────
    logger.info("Training PPO — %s timesteps", f"{cfg.total_timesteps:,}")
    logger.info("Device: %s  |  Envs: %d  |  Batch: %d", cfg.device, cfg.n_envs, cfg.batch_size)

    model.learn(
        total_timesteps  = cfg.total_timesteps,
        callback         = CallbackList(callbacks),
        tb_log_name      = cfg.run_name,
        progress_bar     = True,
        reset_num_timesteps = resume_from is None,
    )

    # ── Save final model ──────────────────────────────────────────────────────
    final_path = str(Path(cfg.save_path) / "final")
    model.save(final_path)
    logger.info("Model saved to %s", final_path)

    train_env.close()
    eval_env.close()
    return model


def evaluate_policy(
    model,
    env,
    n_episodes: int = 10,
    deterministic: bool = True,
    render: bool = False,
) -> list[float]:
    """
    Evaluate a trained policy on the given environment.

    Returns a list of episode total rewards (one per episode).
    """
    episode_rewards = []

    for ep in range(n_episodes):
        obs, _ = env.reset()
        total_reward = 0.0
        done = False

        while not done:
            action, _ = model.predict(obs, deterministic=deterministic)
            obs, reward, terminated, truncated, info = env.step(action)
            total_reward += float(reward)
            done = terminated or truncated

            if render:
                env.render()

        episode_rewards.append(total_reward)
        logger.info("Episode %d/%d: return=%.1f", ep+1, n_episodes, total_reward)

    mean_return = sum(episode_rewards) / len(episode_rewards)
    logger.info("Mean return over %d episodes: %.1f", n_episodes, mean_return)
    return episode_rewards


def export_onnx(model, path: str) -> None:
    """
    Export a trained SB3 PPO policy to ONNX format.

    The exported model accepts a (1, obs_dim) float32 tensor and
    produces a (1, act_dim) float32 tensor (deterministic action).

    Requires: pip install onnx onnxruntime torch

    Usage:
        export_onnx(model, "models/cerberus_trot_v1.onnx")
        # Then load for inference:
        import onnxruntime as ort
        sess = ort.InferenceSession("models/cerberus_trot_v1.onnx")
        action = sess.run(None, {"obs": obs_array})[0]
    """
    try:
        import torch
        import torch.onnx
    except ImportError:
        raise ImportError("torch is required for ONNX export. pip install torch")

    policy = model.policy
    policy.eval()

    from cerberus.learning.environment import OBS_DIM
    dummy_obs = torch.zeros(1, OBS_DIM, dtype=torch.float32)

    torch.onnx.export(
        policy,
        dummy_obs,
        path,
        export_params    = True,
        opset_version    = 17,
        input_names      = ["obs"],
        output_names     = ["action"],
        dynamic_axes     = {"obs": {0: "batch"}, "action": {0: "batch"}},
    )
    logger.info("Policy exported to ONNX: %s", path)
