"""
tests/conftest.py
━━━━━━━━━━━━━━━━
Shared pytest fixtures for the CERBERUS test suite.

Provides reusable async fixtures at three levels of completeness:

  sim_bridge      — connected SimBridge only
  bare_engine     — engine + watchdog + bridge, no subsystems
  engine_be       — bare_engine + BehaviorEngine (no plugins)
  full_engine     — engine_be + DigitalAnatomy + PluginManager
                    (auto-discovers plugins from plugins/ directory)

All engine fixtures yield the engine in RUNNING state and ensure
engine.stop() is called after each test via the fixture teardown.

Plugin fixtures (terrain_plugin, stair_plugin, payload_plugin) provide
individual plugin instances wired to a bare_engine, allowing isolated
unit tests without the full plugin manager discovery overhead.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure simulation mode for all tests
os.environ.setdefault("GO2_SIMULATION", "true")


# ─────────────────────────────────────────────────────────────────────────────
# Bridge
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sim_bridge():
    from cerberus.bridge.go2_bridge import SimBridge
    b = SimBridge()
    await b.connect()
    yield b
    await b.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Engines
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def bare_engine():
    """Engine + watchdog + bridge only.  No behavior engine, no plugins."""
    from cerberus.bridge.go2_bridge import SimBridge
    from cerberus.core.engine import CerberusEngine
    from cerberus.core.safety import SafetyWatchdog, SafetyLimits

    bridge   = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng      = CerberusEngine(bridge, watchdog, target_hz=60)
    eng.watchdog = watchdog

    await bridge.connect()
    yield eng
    await eng.stop() if eng.state.value != "stopped" else None
    await bridge.disconnect()


@pytest_asyncio.fixture
async def engine_be(bare_engine):
    """bare_engine + BehaviorEngine attached."""
    from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
    bare_engine.behavior_engine = BehaviorEngine(
        bare_engine.bridge,
        PersonalityTraits(energy=0.7, friendliness=0.8,
                          curiosity=0.6, loyalty=0.9, playfulness=0.65),
    )
    yield bare_engine


@pytest_asyncio.fixture
async def full_engine(tmp_path):
    """
    Full engine: bridge + watchdog + behavior engine + anatomy + plugin manager.
    Plugin manager discovers all plugins from the plugins/ directory.
    Engine is NOT started (avoids the tick loop in unit tests).
    """
    from cerberus.bridge.go2_bridge import SimBridge
    from cerberus.core.engine import CerberusEngine
    from cerberus.core.safety import SafetyWatchdog, SafetyLimits
    from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
    from cerberus.anatomy.kinematics import DigitalAnatomy
    from cerberus.plugins.plugin_manager import PluginManager

    bridge   = SimBridge()
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    eng      = CerberusEngine(bridge, watchdog, target_hz=60)
    eng.watchdog = watchdog

    await bridge.connect()

    eng.behavior_engine = BehaviorEngine(bridge, PersonalityTraits())
    eng.anatomy         = DigitalAnatomy()

    pm = PluginManager(eng, ["plugins"])
    await pm.discover_and_load()
    pm.register_with_engine()

    yield eng, pm

    for name in list(pm._plugins.keys()):
        await pm.unload_plugin(name)
    await bridge.disconnect()


# ─────────────────────────────────────────────────────────────────────────────
# Individual plugin fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def terrain_plugin(bare_engine):
    from plugins.terrain_arbiter.plugin import TerrainArbiter
    p = TerrainArbiter(bare_engine)
    await p.on_load()
    yield p
    await p.on_unload()


@pytest_asyncio.fixture
async def stair_plugin(bare_engine):
    from plugins.stair_climber.plugin import StairClimberPlugin
    p = StairClimberPlugin(bare_engine)
    await p.on_load()
    yield p
    await p.on_unload()


@pytest_asyncio.fixture
async def payload_plugin(bare_engine):
    from plugins.undercarriage_payload.plugin import UndercarriagePayloadPlugin
    p = UndercarriagePayloadPlugin(bare_engine)
    await p.on_load()
    yield p
    await p.on_unload()


# ─────────────────────────────────────────────────────────────────────────────
# Session store (temp file)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_session_path(tmp_path):
    """Temporary path for session store — never touches logs/ in real dirs."""
    return tmp_path / "test_session.json"


@pytest.fixture
def session_store(tmp_session_path):
    from cerberus.cognitive.session_store import SessionStore
    return SessionStore(path=tmp_session_path)


# ─────────────────────────────────────────────────────────────────────────────
# Mock WebSocket helper
# ─────────────────────────────────────────────────────────────────────────────

class MockWebSocket:
    """Minimal WebSocket stand-in for WebSocketManager tests."""

    def __init__(self, fail_on_send: bool = False):
        self.sent: list[str] = []
        self._fail = fail_on_send

    async def send_text(self, msg: str) -> None:
        if self._fail:
            raise RuntimeError("simulated disconnect")
        self.sent.append(msg)


@pytest.fixture
def mock_ws():
    return MockWebSocket()


@pytest.fixture
def dead_ws():
    return MockWebSocket(fail_on_send=True)
