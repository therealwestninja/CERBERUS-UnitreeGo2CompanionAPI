"""
cerberus/plugins/plugin_manager.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CERBERUS Plugin System

Features:
  • Sandboxed importlib-based plugin loading
  • Capability manifest (what the plugin is allowed to do)
  • Trust levels: TRUSTED / COMMUNITY / UNTRUSTED
  • Dynamic load / unload with versioning
  • Audit log of plugin actions
  • Plugin error isolation — one crash doesn't kill the engine

Plugin structure (minimal):
    # my_plugin/plugin.py
    from cerberus.plugins.base import CerberusPlugin, PluginManifest

    MANIFEST = PluginManifest(
        name="MyPlugin",
        version="1.0.0",
        author="You",
        capabilities=["read_state"],
    )

    class MyPlugin(CerberusPlugin):
        async def on_load(self): ...
        async def on_unload(self): ...
        async def on_tick(self, tick: int): ...
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from cerberus.core.engine import CerberusEngine

logger = logging.getLogger(__name__)


# ── Trust levels ──────────────────────────────────────────────────────────────

class TrustLevel(str, Enum):
    TRUSTED    = "trusted"    # Core CERBERUS plugins — full capability access
    COMMUNITY  = "community"  # Verified community plugins — read_state + limited control
    UNTRUSTED  = "untrusted"  # Unknown origin — read_state only


# ── Capability set ────────────────────────────────────────────────────────────

ALL_CAPABILITIES = {
    "read_state",        # Read robot sensor state
    "control_motion",    # Send movement commands (move, stop, body_height)
    "control_gait",      # Gait mode / foot-raise / speed-level commands
    "control_led",       # LED / visual
    "control_audio",     # Speaker / volume
    "execute_sport",     # Sport mode commands
    "access_memory",     # Read/write working memory
    "publish_events",    # Publish to event bus
    "access_network",    # Outbound HTTP/WebSocket
    "access_filesystem", # Read/write local files
    "modify_safety_limits", # Adjust watchdog SafetyLimits at runtime (TRUSTED only)
    "low_level_control", # Direct joint commands (requires TRUSTED)
}

TRUST_CAPABILITY_MAP = {
    TrustLevel.TRUSTED:    ALL_CAPABILITIES,
    TrustLevel.COMMUNITY:  {"read_state", "control_motion", "control_gait",
                             "control_led", "control_audio", "execute_sport",
                             "publish_events"},
    TrustLevel.UNTRUSTED:  {"read_state"},
}


# ── Plugin manifest ────────────────────────────────────────────────────────────

@dataclass
class PluginManifest:
    name:         str
    version:      str
    author:       str = "Unknown"
    description:  str = ""
    capabilities: list[str] = field(default_factory=list)
    min_cerberus: str = "2.0.0"
    trust:        TrustLevel = TrustLevel.COMMUNITY

    def validate_capabilities(self) -> list[str]:
        """Return list of denied capabilities given trust level."""
        allowed = TRUST_CAPABILITY_MAP.get(self.trust, set())
        return [c for c in self.capabilities if c not in allowed]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "capabilities": self.capabilities,
            "trust": self.trust.value,
        }


# ── Plugin base class ─────────────────────────────────────────────────────────

class CerberusPlugin:
    """All CERBERUS plugins must subclass this."""

    MANIFEST: PluginManifest  # Must be set on the class

    def __init__(self, engine: "CerberusEngine"):
        self.engine = engine
        self._enabled = True
        self._error_count = 0
        self._loaded_at = time.time()

    async def on_load(self) -> None:
        """Called when the plugin is loaded. Set up resources here."""

    async def on_unload(self) -> None:
        """Called before unload. Clean up resources here."""

    async def on_tick(self, tick: int) -> None:
        """Called every engine tick. Keep this fast."""

    async def on_event(self, topic: str, payload: Any) -> None:
        """Called when a subscribed event fires."""

    # ── Convenience property ─────────────────────────────────────────────────

    @property
    def bridge(self):
        """Direct reference to the engine's bridge (read-only shorthand)."""
        return self.engine.bridge

    # ── Sandboxed capability check ────────────────────────────────────────────

    def _require_capability(self, cap: str) -> None:
        if cap not in self.MANIFEST.capabilities:
            raise PermissionError(
                f"Plugin '{self.MANIFEST.name}' tried to use capability '{cap}' "
                f"which is not declared in its manifest."
            )
        allowed = TRUST_CAPABILITY_MAP.get(self.MANIFEST.trust, set())
        if cap not in allowed:
            raise PermissionError(
                f"Plugin '{self.MANIFEST.name}' (trust={self.MANIFEST.trust.value}) "
                f"cannot use capability '{cap}'."
            )

    # ── Safe bridge wrappers — motion ─────────────────────────────────────────

    async def move(self, vx: float, vy: float, vyaw: float) -> bool:
        self._require_capability("control_motion")
        return await self.engine.bridge.move(vx, vy, vyaw)

    async def stop(self) -> bool:
        self._require_capability("control_motion")
        return await self.engine.bridge.stop_move()

    async def set_body_height(self, height: float) -> bool:
        self._require_capability("control_motion")
        return await self.engine.bridge.set_body_height(height)

    async def get_state(self):
        self._require_capability("read_state")
        return await self.engine.bridge.get_state()

    # ── Safe bridge wrappers — gait ───────────────────────────────────────────

    async def switch_gait(self, gait_id: int) -> bool:
        self._require_capability("control_gait")
        return await self.engine.bridge.switch_gait(gait_id)

    async def set_foot_raise_height(self, height: float) -> bool:
        self._require_capability("control_gait")
        return await self.engine.bridge.set_foot_raise_height(height)

    async def set_speed_level(self, level: int) -> bool:
        self._require_capability("control_gait")
        return await self.engine.bridge.set_speed_level(level)

    # ── Safe bridge wrappers — sport / LED ────────────────────────────────────

    async def execute_sport_mode(self, mode) -> bool:
        self._require_capability("execute_sport")
        return await self.engine.bridge.execute_sport_mode(mode)

    async def set_led(self, r: int, g: int, b: int) -> bool:
        self._require_capability("control_led")
        return await self.engine.bridge.set_led(r, g, b)

    # ── EventBus ──────────────────────────────────────────────────────────────

    async def publish(self, topic: str, payload: Any) -> None:
        self._require_capability("publish_events")
        await self.engine.bus.publish(topic, payload)

    # ── Memory ────────────────────────────────────────────────────────────────

    def read_memory(self, key: str, default=None):
        self._require_capability("access_memory")
        return self.engine.behavior_engine.memory.get(key, default)

    def write_memory(self, key: str, value: Any, ttl_s: float = 30.0) -> None:
        self._require_capability("access_memory")
        self.engine.behavior_engine.memory.set(key, value, ttl_s)

    def status(self) -> dict:
        return {
            "name": self.MANIFEST.name,
            "version": self.MANIFEST.version,
            "enabled": self._enabled,
            "error_count": self._error_count,
            "uptime_s": round(time.time() - self._loaded_at, 1),
        }


# ── Plugin record ─────────────────────────────────────────────────────────────

@dataclass
class PluginRecord:
    plugin: CerberusPlugin
    manifest: PluginManifest
    module_path: str
    loaded_at: float = field(default_factory=time.time)
    error_count: int = 0


# ── Plugin Manager ────────────────────────────────────────────────────────────

class PluginManager:
    """
    Manages the full lifecycle of CERBERUS plugins.

    Usage:
        pm = PluginManager(engine, plugin_dirs=["plugins"])
        await pm.discover_and_load()
        pm.register_with_engine()
    """

    def __init__(self, engine: "CerberusEngine", plugin_dirs: list[str] | None = None):
        self.engine = engine
        self._dirs = [Path(d) for d in (plugin_dirs or ["plugins"])]
        self._plugins: dict[str, PluginRecord] = {}
        self._max_errors = int(os.getenv("PLUGIN_MAX_ERRORS", "5"))

    # ── Discovery ────────────────────────────────────────────────────────────

    async def discover_and_load(self) -> int:
        """Scan plugin directories and load all valid plugins. Returns count loaded."""
        loaded = 0
        for plugin_dir in self._dirs:
            if not plugin_dir.exists():
                logger.debug("Plugin dir %s not found, skipping", plugin_dir)
                continue
            for candidate in sorted(plugin_dir.iterdir()):
                # Skip hidden dirs, __pycache__, __init__, and other dunder dirs
                if candidate.name.startswith("_") or candidate.name.startswith("."):
                    continue
                plugin_file = candidate / "plugin.py" if candidate.is_dir() else candidate
                if plugin_file.suffix != ".py" or plugin_file.name.startswith("_"):
                    continue
                if not plugin_file.exists():
                    continue
                try:
                    if await self.load_from_file(plugin_file):
                        loaded += 1
                except Exception as exc:
                    logger.error("Failed to load plugin from %s: %s", plugin_file, exc)
        logger.info("Plugin discovery complete: %d plugin(s) loaded", loaded)
        return loaded

    async def load_from_file(self, path: Path) -> bool:
        """Load a single plugin file. Returns True on success."""
        # Build a unique module name from the parent directory + stem so that
        # two plugins both named 'plugin.py' in different directories don't
        # collide. The name MUST be passed to spec_from_file_location — setting
        # module.__name__ after creation causes the loader to reject it.
        unique_name = f"cerberus_plugin_{path.parent.name}_{path.stem}"
        spec = importlib.util.spec_from_file_location(unique_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create spec for {path}")

        module = importlib.util.module_from_spec(spec)
        # Register under the unique name BEFORE exec_module.
        # Python's @dataclass decorator (and TYPE_CHECKING) resolve
        # cls.__module__ via sys.modules at class-definition time — if the
        # module isn't registered, @dataclass raises AttributeError on NoneType.
        # The unique_name prefix keeps this sandboxed from the real import tree.
        sys.modules[unique_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            # Clean up on load failure
            sys.modules.pop(unique_name, None)
            raise

        # Find CerberusPlugin subclass
        plugin_cls = None
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, CerberusPlugin)
                    and attr is not CerberusPlugin and hasattr(attr, "MANIFEST")):
                plugin_cls = attr
                break

        if plugin_cls is None:
            logger.warning("No CerberusPlugin subclass with MANIFEST found in %s", path)
            return False

        manifest = plugin_cls.MANIFEST
        return await self.load_plugin_class(plugin_cls, manifest, str(path))

    async def load_plugin_class(
        self, cls: type, manifest: PluginManifest, path: str
    ) -> bool:
        """Instantiate, validate, and register a plugin class."""
        name = manifest.name

        if name in self._plugins:
            logger.warning("Plugin '%s' already loaded — unload first", name)
            return False

        # Capability validation
        denied = manifest.validate_capabilities()
        if denied:
            logger.error(
                "Plugin '%s' requests denied capabilities %s (trust=%s) — rejected",
                name, denied, manifest.trust.value
            )
            return False

        plugin = cls(self.engine)
        try:
            await plugin.on_load()
        except Exception as exc:
            logger.error("Plugin '%s' on_load() failed: %s", name, exc)
            return False

        record = PluginRecord(plugin=plugin, manifest=manifest, module_path=path)
        self._plugins[name] = record

        # Subscribe to engine events if the plugin handles them
        if hasattr(plugin, "on_event"):
            for topic in getattr(manifest, "subscribed_topics", []):
                self.engine.bus.subscribe(topic, lambda p, t=topic: plugin.on_event(t, p))

        # Register engine tick hook immediately — this handles plugins loaded
        # after the initial register_with_engine() call (e.g. via POST /plugins).
        # (Bug: without this, dynamically loaded plugins are never ticked.)
        self._register_hook_for_record(name, record)

        logger.info("Plugin loaded: %s v%s [trust=%s]", name, manifest.version, manifest.trust.value)
        return True

    def _register_hook_for_record(self, name: str, record: "PluginRecord") -> None:
        """Register a single plugin's tick hook with the engine."""
        hook_name  = f"plugin_{name}"
        max_errors = self._max_errors

        # Respect the plugin class's HOOK_PRIORITY attribute.
        # Lower values run earlier in the tick (same as engine hook ordering).
        # Default 100 keeps the historical behaviour.
        priority = getattr(record.plugin.__class__, "HOOK_PRIORITY", 100)

        async def _hook(tick: int, rec: PluginRecord = record) -> None:
            if not rec.plugin._enabled:
                return
            try:
                await rec.plugin.on_tick(tick)
            except Exception as exc:
                rec.error_count += 1
                rec.plugin._error_count += 1
                logger.error("Plugin '%s' tick error (%d/%d): %s",
                             rec.manifest.name, rec.error_count, max_errors, exc)
                if rec.error_count >= max_errors:
                    logger.error("Plugin '%s' exceeded error limit — disabling", rec.manifest.name)
                    rec.plugin._enabled = False

        self.engine.register_hook(hook_name, _hook, priority=priority)

    async def unload_plugin(self, name: str) -> bool:
        """Gracefully unload a plugin by name."""
        record = self._plugins.get(name)
        if record is None:
            logger.warning("Cannot unload '%s' — not loaded", name)
            return False
        try:
            await record.plugin.on_unload()
        except Exception as exc:
            logger.error("Plugin '%s' on_unload() error: %s", name, exc)
        del self._plugins[name]
        self.engine.unregister_hook(f"plugin_{name}")

        # Remove from sys.modules to allow clean re-load.
        # The unique_name was built from the *file path* (parent dir + stem),
        # not the manifest name — so derive it the same way.
        path = Path(record.module_path)
        unique_name = f"cerberus_plugin_{path.parent.name}_{path.stem}"
        sys.modules.pop(unique_name, None)

        logger.info("Plugin unloaded: %s", name)
        return True

    # ── Engine integration ────────────────────────────────────────────────────

    def register_with_engine(self) -> None:
        """Register all loaded plugins as engine tick hooks.
        
        Safe to call multiple times — register_hook on an already-registered
        name is a no-op because load_plugin_class() now calls
        _register_hook_for_record() at load time. This method remains for
        explicit bulk registration at startup.
        """
        for name, record in self._plugins.items():
            hook_name = f"plugin_{name}"
            # Skip if already registered (idempotent)
            if any(h.name == hook_name for h in self.engine._plugin_hooks):
                continue
            self._register_hook_for_record(name, record)

    # ── Control ───────────────────────────────────────────────────────────────

    def enable(self, name: str) -> bool:
        rec = self._plugins.get(name)
        if rec:
            rec.plugin._enabled = True
            return True
        return False

    def disable(self, name: str) -> bool:
        rec = self._plugins.get(name)
        if rec:
            rec.plugin._enabled = False
            return True
        return False

    # ── Status ────────────────────────────────────────────────────────────────

    def list_plugins(self) -> list[dict]:
        return [
            {**rec.manifest.to_dict(), **rec.plugin.status(),
             "module_path": rec.module_path}
            for rec in self._plugins.values()
        ]
