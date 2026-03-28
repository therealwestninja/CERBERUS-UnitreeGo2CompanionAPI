"""
CERBERUS — Canine-Emulative Responsive Behavioral Engine & Reactive Utility System
Unitree Go2 Companion API  v3.1

Public API surface:
    from cerberus import Go2Bridge, RobotState, BehaviorEngine
    from cerberus import SafetyGate, SafetyConfig
    from cerberus import PersonalityModel
    from cerberus import CognitiveEngine
    from cerberus import interpret          # NLU
    from cerberus import DataLogger
"""

from cerberus.hardware.bridge import Go2Bridge, RobotState, AVAILABLE_MODES, ConnectionState
from cerberus.safety.gate import SafetyGate, SafetyConfig
from cerberus.behavior.engine import BehaviorEngine, BehaviorDescriptor, Priority
from cerberus.personality.model import PersonalityModel, Traits, Mood
from cerberus.core.cognitive import CognitiveEngine, Goal, GoalType
from cerberus.nlu.interpreter import interpret, rule_interpret, NLUAction
from cerberus.learning.data_logger import DataLogger, SessionReplayer
from cerberus.plugins.manager import PluginManager, PluginManifest, PluginContext, TrustLevel

__version__ = "3.1.0"
__all__ = [
    # Hardware
    "Go2Bridge", "RobotState", "AVAILABLE_MODES", "ConnectionState",
    # Safety
    "SafetyGate", "SafetyConfig",
    # Behavior
    "BehaviorEngine", "BehaviorDescriptor", "Priority",
    # Personality
    "PersonalityModel", "Traits", "Mood",
    # Cognitive
    "CognitiveEngine", "Goal", "GoalType",
    # NLU
    "interpret", "rule_interpret", "NLUAction",
    # Learning
    "DataLogger", "SessionReplayer",
    # Plugins
    "PluginManager", "PluginManifest", "PluginContext", "TrustLevel",
    # Meta
    "__version__",
]
