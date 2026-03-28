"""
cerberus/plugins/manager.py  — CERBERUS v3.1
==============================================
Plugin Manager: dynamic load/unload, 4-tier trust, capability gating.

Plugin Manifest (plugin.yaml)
------------------------------
  name: MyPlugin
  version: 1.0.0
  author: Dev
  description: Does something useful
  entry_point: my_plugin:MyPlugin         # module:Class
  capabilities: [motion, perception]
  trust_level: trusted                    # core | trusted | community | untrusted
  enabled: true
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


class TrustLevel(str, Enum):
    CORE      = "core"
    TRUSTED   = "trusted"
    COMMUNITY = "community"
    UNTRUSTED = "untrusted"


_CAPS: dict[TrustLevel, set[str]] = {
    TrustLevel.CORE:      {"motion","perception","vui","config","admin"},
    TrustLevel.TRUSTED:   {"motion","perception","vui"},
    TrustLevel.COMMUNITY: {"perception"},
    TrustLevel.UNTRUSTED: set(),
}


@dataclass
class PluginManifest:
    name:         str
    version:      str
    entry_point:  str
    author:       str          = "Unknown"
    description:  str          = ""
    capabilities: list[str]    = field(default_factory=list)
    trust_level:  TrustLevel   = TrustLevel.COMMUNITY
    enabled:      bool         = True

    @classmethod
    def from_yaml(cls, path: Path) -> "PluginManifest":
        raw = yaml.safe_load(path.read_text())
        raw["trust_level"] = TrustLevel(raw.get("trust_level","community"))
        raw.setdefault("capabilities", [])
        return cls(**{k:v for k,v in raw.items() if k in cls.__dataclass_fields__})


@dataclass
class PluginRecord:
    manifest:  PluginManifest
    instance:  Any
    loaded_at: float = field(default_factory=time.time)


@dataclass
class PluginContext:
    trust_level: TrustLevel
    _bridge:     Any = field(repr=False)
    _behavior:   Any = field(repr=False)

    def can(self, cap: str) -> bool:
        return cap in _CAPS[self.trust_level]

    @property
    def bridge(self) -> Any:
        if not (self.can("motion") or self.can("perception")):
            raise PermissionError("Plugin lacks hardware access")
        return self._bridge

    @property
    def behavior_engine(self) -> Any:
        if not self.can("motion"):
            raise PermissionError("Plugin lacks motion capability")
        return self._behavior


class PluginError(RuntimeError): pass


class PluginManager:
    def __init__(self, plugins_dir: str | Path = "plugins") -> None:
        self._dir     = Path(plugins_dir)
        self._plugins: dict[str, PluginRecord] = {}

    def discover(self) -> list[PluginManifest]:
        manifests = []
        for p in self._dir.rglob("plugin.yaml"):
            try:
                manifests.append(PluginManifest.from_yaml(p))
            except Exception as e:
                logger.warning("Bad manifest %s: %s", p, e)
        return manifests

    async def load(self, manifest: PluginManifest, ctx: PluginContext) -> None:
        name = manifest.name
        if name in self._plugins:
            raise PluginError(f"Plugin '{name}' already loaded")
        if not manifest.enabled:
            return
        denied = set(manifest.capabilities) - _CAPS[manifest.trust_level]
        if denied:
            raise PluginError(f"Plugin '{name}' requests disallowed capabilities: {denied}")
        try:
            mod_s, cls_s = manifest.entry_point.rsplit(":", 1)
            mod  = importlib.import_module(mod_s)
            inst = getattr(mod, cls_s)()
        except Exception as e:
            raise PluginError(f"Import failed for '{name}': {e}") from e
        try:
            await inst.on_load(ctx)
        except Exception as e:
            raise PluginError(f"on_load failed for '{name}': {e}") from e
        self._plugins[name] = PluginRecord(manifest=manifest, instance=inst)
        logger.info("Loaded plugin '%s' v%s [%s]", name, manifest.version, manifest.trust_level)

    async def unload(self, name: str) -> None:
        rec = self._plugins.get(name)
        if not rec: raise PluginError(f"Plugin '{name}' not loaded")
        try: await rec.instance.on_unload()
        except Exception as e: logger.warning("Plugin '%s' on_unload error: %s", name, e)
        del self._plugins[name]
        logger.info("Unloaded plugin '%s'", name)

    async def unload_all(self) -> None:
        for name in list(self._plugins): await self.unload(name)

    @property
    def loaded(self) -> list[str]:
        return list(self._plugins)

    def status(self) -> list[dict]:
        return [
            {"name": r.manifest.name, "version": r.manifest.version,
             "trust": r.manifest.trust_level, "loaded_at": r.loaded_at,
             "capabilities": r.manifest.capabilities}
            for r in self._plugins.values()
        ]
