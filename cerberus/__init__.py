"""CERBERUS — Canine-Emulative Responsive Behavioral Engine & Reactive Utility System"""

__version__ = "2.1.0"

from cerberus.bridge.go2_bridge import create_bridge, SimBridge, RealBridge, SportMode, RobotState
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
from cerberus.anatomy.kinematics import DigitalAnatomy

__all__ = [
    "create_bridge", "SimBridge", "RealBridge", "SportMode", "RobotState",
    "CerberusEngine", "SafetyWatchdog", "SafetyLimits",
    "BehaviorEngine", "PersonalityTraits",
    "DigitalAnatomy",
]
