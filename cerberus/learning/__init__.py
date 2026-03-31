"""
cerberus/learning/__init__.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Reinforcement Learning — Locomotion Policy Training

Requires: pip install cerberus-go2[mujoco] stable-baselines3 gymnasium
"""

from cerberus.learning.environment import CerberusEnv, EnvConfig
from cerberus.learning.rewards import RewardWeights, compute_reward
from cerberus.learning.trainer import TrainingConfig, train_ppo, evaluate_policy

__all__ = [
    "CerberusEnv", "EnvConfig",
    "RewardWeights", "compute_reward",
    "TrainingConfig", "train_ppo", "evaluate_policy",
]
