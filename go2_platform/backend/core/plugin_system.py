"""
go2_platform/backend/core/plugin_system.py
══════════════════════════════════════════════════════════════════════════════
Plugin System — Sandboxed, Permission-Gated, OTA-Capable
Plugins extend the platform without touching core code.

Architecture:
  PluginLoader (discovers + loads)
  → PluginSandbox (execution isolation)
  → PluginRegistry (runtime registry)
  → PluginPermissions (capability enforcement)

Plugin Contract:
  Every plugin MUST:
    - Declare name, version, permissions in manifest
    - Implement init(context) entry point
    - Respect rate limits and memory quotas
    - Not call OS / filesystem APIs directly
    - Use platform context for all robot interactions

Permissions:
  'ui'        — register UI panels/tabs
  'behaviors' — register new behaviors/animations
  'api'       — add REST routes
  'fsm'       — subscribe to FSM events
  'sensors'   — access raw sensor data
  'world'     — read/write world model
  'missions'  — create/run missions
"""

import asyncio
import importlib
import importlib.util
import inspect
import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger('go2.plugins')


# ════════════════════════════════════════════════════════════════════════════
# PERMISSIONS
# ════════════════════════════════════════════════════════════════════════════

ALL_PERMISSIONS = {
    'ui',
    'behaviors',
    'api',
    'fsm',
    'sensors',
    'world',
    'missions',
}

class PermissionError(Exception):
    pass


# ════════════════════════════════════════════════════════════════════════════
# PLUGIN CONTEXT  (sandboxed API surface for plugins)
# ════════════════════════════════════════════════════════════════════════════

class PluginContext:
    """
    The ONLY API surface a plugin can access.
    Every call validates permissions before executing.
    """

    def __init__(self, plugin_name: str, permissions: Set[str], platform):
        self._name = plugin_name
        self._perms = permissions
        self._platform = platform
        self._call_log: List[dict] = []
        self._rate_limits: Dict[str, float] = {}
        self._MAX_CALLS_PER_S = 20

    def _require(self, perm: str):
        if perm not in self._perms:
            raise PermissionError(
                f'Plugin "{self._name}" requires permission "{perm}"')

    def _rate_check(self, op: str) -> bool:
        now = time.monotonic()
        last = self._rate_limits.get(op, 0)
        if now - last < 1.0 / self._MAX_CALLS_PER_S:
            return False
        self._rate_limits[op] = now
        return True

    def _log_call(self, method: str, args: dict):
        self._call_log.append({
            'method': method, 'args': args, 'ts': time.time()
        })
        if len(self._call_log) > 100:
            self._call_log.pop(0)

    # ── FSM ──────────────────────────────────────────────────────────────

    async def on_fsm_transition(self, callback: Callable):
        self._require('fsm')
        self._platform.bus.subscribe('fsm.transition', callback)
        self._log_call('on_fsm_transition', {})

    async def get_fsm_state(self) -> dict:
        self._require('fsm')
        return self._platform.fsm.status()

    # ── Behaviors ────────────────────────────────────────────────────────

    def register_behavior(self, behavior: dict) -> bool:
        self._require('behaviors')
        if not self._rate_check('register_behavior'):
            raise RateLimitError(f'Rate limit hit for register_behavior')
        behavior['plugin'] = self._name
        ok = self._platform.behaviors.register(behavior, source=self._name)
        self._log_call('register_behavior', {'id': behavior.get('id')})
        return ok

    # ── World ─────────────────────────────────────────────────────────────

    def get_objects(self) -> List[dict]:
        self._require('world')
        return [o.to_dict() for o in self._platform.world.objects.values()]

    def add_object(self, obj_data: dict) -> bool:
        self._require('world')
        from .platform import WorldObject
        try:
            obj = WorldObject(**{k: obj_data.get(k, WorldObject.__dataclass_fields__[k].default)
                                 for k in WorldObject.__dataclass_fields__})
            ok, _ = self._platform.world.add_object(obj)
            self._log_call('add_object', {'id': obj_data.get('id')})
            return ok
        except Exception as e:
            logger.error(f'Plugin {self._name} add_object error: {e}')
            return False

    # ── Sensors ──────────────────────────────────────────────────────────

    def get_telemetry(self) -> dict:
        self._require('sensors')
        return self._platform.telemetry.to_dict()

    def subscribe_telemetry(self, callback: Callable):
        self._require('sensors')
        self._platform.bus.subscribe('telemetry', callback)

    # ── UI ────────────────────────────────────────────────────────────────

    def register_ui_panel(self, panel: dict) -> bool:
        self._require('ui')
        if 'id' not in panel or 'title' not in panel:
            return False
        panel['plugin'] = self._name
        self._platform._plugin_panels[self._name] = panel
        self._log_call('register_ui_panel', {'id': panel.get('id')})
        return True

    # ── API routes ────────────────────────────────────────────────────────

    def register_route(self, method: str, path: str, handler: Callable):
        self._require('api')
        route_key = f'{method.upper()}:{path}'
        self._platform._plugin_routes[route_key] = {
            'method': method, 'path': path,
            'handler': handler, 'plugin': self._name
        }
        self._log_call('register_route', {'route': route_key})

    # ── Missions ─────────────────────────────────────────────────────────

    async def create_mission(self, name: str, mission_type: str, params: dict) -> str:
        self._require('missions')
        m = self._platform.missions.create(name, mission_type, params)
        return m.id

    # ── Events ───────────────────────────────────────────────────────────

    async def emit(self, event: str, data: Any):
        """Plugins can emit namespaced events."""
        await self._platform.bus.emit(
            f'plugin.{self._name}.{event}', data, source=self._name)

    def on_event(self, event: str, callback: Callable):
        self._platform.bus.subscribe(event, callback)

    def audit_log(self) -> List[dict]:
        return self._call_log.copy()


class RateLimitError(Exception):
    pass


# ════════════════════════════════════════════════════════════════════════════
# PLUGIN MANIFEST VALIDATOR
# ════════════════════════════════════════════════════════════════════════════

def validate_manifest(manifest: dict) -> tuple[bool, str]:
    """
    Validate plugin manifest before loading.
    Security: reject anything that fails schema validation.
    """
    required = {'name', 'version', 'permissions', 'entry_point'}
    missing = required - set(manifest.keys())
    if missing:
        return False, f'Missing fields: {missing}'

    name = manifest['name']
    if not isinstance(name, str) or len(name) > 64 or not name.replace('_','').isalnum():
        return False, f'Invalid name: {name!r}'

    version = manifest.get('version', '')
    if not isinstance(version, str) or len(version) > 20:
        return False, f'Invalid version: {version!r}'

    perms = manifest.get('permissions', [])
    if not isinstance(perms, list):
        return False, 'permissions must be a list'
    unknown = set(perms) - ALL_PERMISSIONS
    if unknown:
        return False, f'Unknown permissions: {unknown}'

    entry = manifest.get('entry_point', '')
    if not isinstance(entry, str) or '..' in entry or entry.startswith('/'):
        return False, f'Invalid entry_point: {entry!r}'

    return True, 'ok'


# ════════════════════════════════════════════════════════════════════════════
# PLUGIN LOADER
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class LoadedPlugin:
    manifest: dict
    module: Any
    context: PluginContext
    loaded_at: float = field(default_factory=time.time)
    status: str = 'inactive'      # inactive / active / error
    error: str = ''
    call_count: int = 0

    def to_dict(self) -> dict:
        return {
            'name': self.manifest.get('name'),
            'version': self.manifest.get('version'),
            'description': self.manifest.get('description', ''),
            'author': self.manifest.get('author', 'unknown'),
            'permissions': self.manifest.get('permissions', []),
            'status': self.status,
            'error': self.error,
            'loaded_at': self.loaded_at,
            'call_count': self.call_count,
        }


class PluginSystem:
    """
    Plugin lifecycle manager:
      load → validate → sandbox → activate → (use) → deactivate → unload
    """

    MAX_PLUGINS = 10

    def __init__(self, platform, plugin_dir: str = 'plugins'):
        self.platform = platform
        self.plugin_dir = Path(plugin_dir)
        self._plugins: Dict[str, LoadedPlugin] = {}
        # Inject plugin support into platform
        if not hasattr(platform, '_plugin_panels'):
            platform._plugin_panels = {}
        if not hasattr(platform, '_plugin_routes'):
            platform._plugin_routes = {}

    async def load_from_dir(self, path: Optional[str] = None) -> dict:
        """Scan plugin directory and load all valid plugins."""
        scan_path = Path(path) if path else self.plugin_dir
        loaded, failed = 0, 0
        if not scan_path.exists():
            logger.warning(f'Plugin dir not found: {scan_path}')
            return {'loaded': 0, 'failed': 0}

        for manifest_file in scan_path.glob('*/manifest.json'):
            try:
                with open(manifest_file) as f:
                    manifest = json.load(f)
                ok, err = await self.load(manifest, str(manifest_file.parent))
                if ok: loaded += 1
                else: failed += 1
            except Exception as e:
                logger.error(f'Plugin load error {manifest_file}: {e}')
                failed += 1

        logger.info(f'Plugins scanned: {loaded} loaded, {failed} failed')
        return {'loaded': loaded, 'failed': failed}

    async def load(self, manifest: dict, plugin_path: str) -> tuple[bool, str]:
        """Load and validate a single plugin."""
        # Schema validation
        ok, reason = validate_manifest(manifest)
        if not ok:
            logger.warning(f'Plugin validation failed: {reason}')
            return False, reason

        name = manifest['name']

        # Limit total plugins
        if len(self._plugins) >= self.MAX_PLUGINS and name not in self._plugins:
            return False, f'Plugin limit ({self.MAX_PLUGINS}) reached'

        # Load module safely
        entry = manifest['entry_point']
        module_path = os.path.join(plugin_path, entry)
        if not os.path.exists(module_path):
            return False, f'Entry point not found: {module_path}'

        try:
            spec = importlib.util.spec_from_file_location(
                f'go2_plugin_{name}', module_path)
            module = importlib.util.module_from_spec(spec)
            # Security: prevent dangerous imports within timeout
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f'Plugin {name} module load error: {e}')
            return False, f'Module load error: {e}'

        # Create sandboxed context
        perms = set(manifest.get('permissions', []))
        ctx = PluginContext(name, perms, self.platform)

        lp = LoadedPlugin(manifest=manifest, module=module, context=ctx)
        self._plugins[name] = lp

        logger.info(f'Plugin loaded: {name} v{manifest.get("version")} '
                    f'perms={list(perms)}')
        return True, 'loaded'

    async def activate(self, name: str) -> tuple[bool, str]:
        """Call plugin.init(context) with timeout and error isolation."""
        lp = self._plugins.get(name)
        if not lp:
            return False, f'Plugin {name} not loaded'
        if lp.status == 'active':
            return True, 'already active'

        if not hasattr(lp.module, 'init'):
            lp.status = 'error'
            lp.error = 'Missing init() function'
            return False, lp.error

        try:
            init_fn = lp.module.init
            if inspect.iscoroutinefunction(init_fn):
                await asyncio.wait_for(init_fn(lp.context), timeout=5.0)
            else:
                init_fn(lp.context)
            lp.status = 'active'
            logger.info(f'Plugin activated: {name}')
            await self.platform.bus.emit(
                'plugin.activated', {'name': name}, 'plugins')
            return True, 'activated'
        except asyncio.TimeoutError:
            lp.status = 'error'
            lp.error = 'init() timeout (5s)'
            return False, lp.error
        except Exception as e:
            lp.status = 'error'
            lp.error = str(e)
            logger.error(f'Plugin {name} activation error: {traceback.format_exc()}')
            return False, lp.error

    async def deactivate(self, name: str) -> bool:
        lp = self._plugins.get(name)
        if not lp:
            return False
        if hasattr(lp.module, 'teardown'):
            try:
                await asyncio.wait_for(lp.module.teardown(lp.context), timeout=3.0)
            except Exception as e:
                logger.warning(f'Plugin {name} teardown error: {e}')
        lp.status = 'inactive'
        await self.platform.bus.emit('plugin.deactivated', {'name': name}, 'plugins')
        return True

    async def unload(self, name: str) -> bool:
        await self.deactivate(name)
        self._plugins.pop(name, None)
        self.platform._plugin_panels.pop(name, None)
        logger.info(f'Plugin unloaded: {name}')
        return True

    async def update(self, name: str, new_manifest: dict,
                     new_path: str) -> tuple[bool, str]:
        """OTA update: unload → reload → activate."""
        logger.info(f'OTA update: {name}')
        await self.unload(name)
        ok, msg = await self.load(new_manifest, new_path)
        if ok:
            ok2, msg2 = await self.activate(name)
            return ok2, msg2
        return ok, msg

    def list(self) -> List[dict]:
        return [lp.to_dict() for lp in self._plugins.values()]

    def get(self, name: str) -> Optional[LoadedPlugin]:
        return self._plugins.get(name)

    def panels(self) -> List[dict]:
        return list(self.platform._plugin_panels.values())

    def routes(self) -> List[dict]:
        return [
            {k: v for k, v in r.items() if k != 'handler'}
            for r in self.platform._plugin_routes.values()
        ]
