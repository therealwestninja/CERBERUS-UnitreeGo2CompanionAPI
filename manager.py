"""
cerberus/plugins/manager.py
============================
Plugin Manager — dynamic load/unload of sandboxed CERBERUS plugins.

Plugin Manifest (plugin.yaml)
------------------------------
  name: MyPlugin
  version: 1.2.0
  author: Jane Dev
  description: Does something useful
  entry_point: my_plugin.main:MyPlugin
  capabilities:
    - motion          # can issue move() calls
    - perception      # can read sensor data
    - vui             # can control LEDs / volume
  trust_level: community   # core | trusted | community | untrusted

Trust levels
------------
  core        full access
  trusted     motion + perception + vui
  community   perception only (read-only)
  untrusted   sandboxed; no hardware access
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    CORE       = "core"
    TRUSTED    = "trusted"
    COMMUNITY  = "community"
    UNTRUSTED  = "untrusted"


_CAPABILITY_BY_TRUST: dict[TrustLevel, set[str]] = {
    TrustLevel.CORE:      {"motion", "perception", "vui", "config", "admin"},
    TrustLevel.TRUSTED:   {"motion", "perception", "vui"},
    TrustLevel.COMMUNITY: {"perception"},
    TrustLevel.UNTRUSTED: set(),
}


@dataclass
class PluginManifest:
    name:         str
    version:      str
    entry_point:  str                    # module:ClassName
    author:       str         = "Unknown"
    description:  str         = ""
    capabilities: list[str]   = field(default_factory=list)
    trust_level:  TrustLevel  = TrustLevel.COMMUNITY
    enabled:      bool        = True

    @classmethod
    def from_yaml(cls, path: Path) -> "PluginManifest":
        raw = yaml.safe_load(path.read_text())
        raw["trust_level"] = TrustLevel(raw.get("trust_level", "community"))
        raw.setdefault("capabilities", [])
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


@dataclass
class PluginRecord:
    manifest:   PluginManifest
    instance:   Any
    loaded_at:  float = field(default_factory=time.time)
    error:      Optional[str] = None


class PluginError(RuntimeError):
    pass


class PluginManager:
    """
    Loads, validates, and manages plugin lifecycle.

    Plugins must expose a class (named in manifest.entry_point) with:
        async def on_load(self, context: PluginContext) -> None
        async def on_unload(self) -> None
    """

    def __init__(self, plugins_dir: str | Path = "plugins") -> None:
        self._dir     = Path(plugins_dir)
        self._plugins: dict[str, PluginRecord] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[PluginManifest]:
        """Scan plugins dir and return manifests for all valid plugins."""
        manifests: list[PluginManifest] = []
        for manifest_path in self._dir.rglob("plugin.yaml"):
            try:
                manifests.append(PluginManifest.from_yaml(manifest_path))
            except Exception as exc:
                logger.warning("Bad plugin manifest %s: %s", manifest_path, exc)
        return manifests

    # ------------------------------------------------------------------
    # Load / Unload
    # ------------------------------------------------------------------

    async def load(self, manifest: PluginManifest, context: "PluginContext") -> None:
        name = manifest.name
        if name in self._plugins:
            raise PluginError(f"Plugin '{name}' is already loaded")

        if not manifest.enabled:
            logger.info("Plugin '%s' is disabled — skipping", name)
            return

        # Capability validation
        allowed = _CAPABILITY_BY_TRUST[manifest.trust_level]
        denied  = set(manifest.capabilities) - allowed
        if denied:
            raise PluginError(
                f"Plugin '{name}' requests capabilities {denied} "
                f"not permitted for trust_level='{manifest.trust_level}'"
            )

        # Import
        try:
            module_path, class_name = manifest.entry_point.rsplit(":", 1)
            mod  = importlib.import_module(module_path)
            cls  = getattr(mod, class_name)
            inst = cls()
        except Exception as exc:
            raise PluginError(f"Failed to import plugin '{name}': {exc}") from exc

        # Initialise
        try:
            await inst.on_load(context)
        except Exception as exc:
            raise PluginError(f"Plugin '{name}' on_load failed: {exc}") from exc

        self._plugins[name] = PluginRecord(manifest=manifest, instance=inst)
        logger.info("Loaded plugin '%s' v%s [%s]",
                    name, manifest.version, manifest.trust_level)

    async def unload(self, name: str) -> None:
        record = self._plugins.get(name)
        if not record:
            raise PluginError(f"Plugin '{name}' is not loaded")
        try:
            await record.instance.on_unload()
        except Exception as exc:
            logger.warning("Plugin '%s' on_unload error: %s", name, exc)
        del self._plugins[name]
        logger.info("Unloaded plugin '%s'", name)

    async def unload_all(self) -> None:
        for name in list(self._plugins):
            await self.unload(name)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> list[str]:
        return list(self._plugins.keys())

    def get_record(self, name: str) -> Optional[PluginRecord]:
        return self._plugins.get(name)

    def status(self) -> list[dict]:
        return [
            {
                "name":       r.manifest.name,
                "version":    r.manifest.version,
                "trust":      r.manifest.trust_level,
                "loaded_at":  r.loaded_at,
                "capabilities": r.manifest.capabilities,
            }
            for r in self._plugins.values()
        ]


@dataclass
class PluginContext:
    """
    Capability-filtered context passed to each plugin's on_load.
    The plugin can only access methods its trust level allows.
    """
    trust_level: TrustLevel
    _bridge:     Any = field(repr=False)
    _behavior:   Any = field(repr=False)

    def can(self, capability: str) -> bool:
        return capability in _CAPABILITY_BY_TRUST[self.trust_level]

    @property
    def bridge(self) -> Any:
        if not self.can("motion") and not self.can("perception"):
            raise PermissionError("Plugin does not have hardware access")
        return self._bridge

    @property
    def behavior_engine(self) -> Any:
        if not self.can("motion"):
            raise PermissionError("Plugin does not have motion capability")
        return self._behavior
