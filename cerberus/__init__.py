"""CERBERUS — Canine-Emulative Responsive Behavioral Engine & Reactive Utility System"""

# Single-source version — defined once here, consumed everywhere else.
# importlib.metadata reads from the installed package (pyproject.toml → dist-info),
# so there is no separate version string to keep in sync after a release.
try:
    from importlib.metadata import version as _meta_version
    __version__: str = _meta_version("cerberus-go2")
except Exception:
    # Not installed (running from source without `pip install -e .`)
    # Fall back to pyproject.toml parse so development runs are still labelled.
    try:
        import re as _re
        import pathlib as _pl
        _toml = (_pl.Path(__file__).parent.parent / "pyproject.toml").read_text()
        _m    = _re.search(r'^version\s*=\s*"([^"]+)"', _toml, _re.MULTILINE)
        __version__ = _m.group(1) if _m else "unknown"
    except Exception:
        __version__ = "unknown"

from cerberus.bridge.go2_bridge import create_bridge, SimBridge, RealBridge, SportMode, RobotState
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
from cerberus.anatomy.kinematics import DigitalAnatomy

__all__ = [
    "__version__",
    "create_bridge", "SimBridge", "RealBridge", "SportMode", "RobotState",
    "CerberusEngine", "SafetyWatchdog", "SafetyLimits",
    "BehaviorEngine", "PersonalityTraits",
    "DigitalAnatomy",
]
