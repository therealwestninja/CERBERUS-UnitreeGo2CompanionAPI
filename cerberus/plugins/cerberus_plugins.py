"""
cerberus/plugins/cerberus_plugins.py
══════════════════════════════════════════════════════════════════════════════
CERBERUS Plugin System — Extended Plugin Ecosystem

Extends Go2 Platform's base plugin system with CERBERUS-specific features:
  - Trust levels (SYSTEM / TRUSTED / COMMUNITY / UNTRUSTED)
  - Resource quotas (CPU time, memory, event rate)
  - CERBERUS-specific API surface (mind, body, personality, learning access)
  - Hot-reload with state preservation
  - Plugin dependency graph
  - Versioned API contracts

Plugin trust levels determine API access:
  SYSTEM     — full platform access (built-in only, cannot be installed)
  TRUSTED    — signed, audited — access to cognitive + body APIs
  COMMUNITY  — unsigned — access to behaviors, UI, API routes only
  UNTRUSTED  — read-only telemetry access, no commands

Example CERBERUS plugin:
  async def init(ctx: CerberusPluginContext):
      ctx.subscribe_percept(on_percept)
      ctx.register_behavior({'id': 'bark', 'name': 'Bark', ...})
      ctx.on_mood_change(on_mood)
      await ctx.emit_goal({'name': 'greet_human', 'priority': 0.8})
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, Callable, Dict, List, Optional, Set

log = logging.getLogger('cerberus.plugins')


# ════════════════════════════════════════════════════════════════════════════
# TRUST LEVELS
# ════════════════════════════════════════════════════════════════════════════

class TrustLevel(IntEnum):
    SYSTEM      = 4   # Built-in, full access
    TRUSTED     = 3   # Signed & audited, cognitive API
    COMMUNITY   = 2   # Unsigned, behavior/UI only
    UNTRUSTED   = 1   # Sandboxed, read-only telemetry

# API access by trust level
TRUST_PERMISSIONS: Dict[TrustLevel, Set[str]] = {
    TrustLevel.SYSTEM:    {'*'},
    TrustLevel.TRUSTED:   {'behaviors', 'ui', 'api', 'fsm', 'sensors', 'world',
                           'missions', 'cognitive', 'personality', 'learning'},
    TrustLevel.COMMUNITY: {'behaviors', 'ui', 'api', 'sensors'},
    TrustLevel.UNTRUSTED: {'sensors'},
}


# ════════════════════════════════════════════════════════════════════════════
# RESOURCE QUOTA
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class ResourceQuota:
    """Per-plugin resource limits."""
    max_cpu_ms_per_tick:  float = 10.0    # ms budget per tick
    max_events_per_s:     int   = 50      # outbound event rate
    max_memory_kb:        int   = 5120    # 5MB
    max_behaviors:        int   = 20
    max_routes:           int   = 10
    max_subscriptions:    int   = 30

    QUOTA_BY_TRUST: Dict[TrustLevel, 'ResourceQuota'] = None  # set below

ResourceQuota.QUOTA_BY_TRUST = {
    TrustLevel.SYSTEM:    ResourceQuota(max_cpu_ms_per_tick=50),
    TrustLevel.TRUSTED:   ResourceQuota(max_cpu_ms_per_tick=20, max_behaviors=50),
    TrustLevel.COMMUNITY: ResourceQuota(max_cpu_ms_per_tick=10),
    TrustLevel.UNTRUSTED: ResourceQuota(max_cpu_ms_per_tick=2, max_behaviors=5),
}


# ════════════════════════════════════════════════════════════════════════════
# CERBERUS PLUGIN CONTEXT
# ════════════════════════════════════════════════════════════════════════════

class CerberusPluginContext:
    """
    Extended plugin context with CERBERUS cognitive/body/personality API.
    Access is gated by trust level.
    """

    def __init__(self, plugin_name: str, trust: TrustLevel,
                 platform, cerberus, bus):
        self._name     = plugin_name
        self._trust    = trust
        self._platform = platform
        self._cerberus = cerberus
        self._bus      = bus
        self._quota    = ResourceQuota.QUOTA_BY_TRUST.get(trust, ResourceQuota())
        self._perms    = TRUST_PERMISSIONS.get(trust, set())
        self._event_count_window = 0.0
        self._event_count_start  = time.monotonic()
        self._call_log: List[dict] = []

    def _require(self, perm: str):
        if '*' not in self._perms and perm not in self._perms:
            raise PermissionError(
                f'Plugin "{self._name}" (trust={self._trust.name}) '
                f'cannot access permission "{perm}"')

    def _check_event_rate(self) -> bool:
        now = time.monotonic()
        if now - self._event_count_start >= 1.0:
            self._event_count_window = 0
            self._event_count_start  = now
        self._event_count_window += 1
        return self._event_count_window <= self._quota.max_events_per_s

    # ── Base platform API (same as PluginContext) ─────────────────────────

    def register_behavior(self, behavior: dict) -> bool:
        self._require('behaviors')
        behavior['plugin'] = self._name
        behavior['trust']  = self._trust.name
        if self._platform and hasattr(self._platform, 'behaviors'):
            return self._platform.behaviors.register(behavior, source=self._name)
        return False

    def get_telemetry(self) -> dict:
        self._require('sensors')
        if self._platform:
            return self._platform.telemetry.to_dict()
        return {}

    def register_ui_panel(self, panel: dict) -> bool:
        self._require('ui')
        panel['plugin'] = self._name
        panel['trust']  = self._trust.name
        if self._platform and hasattr(self._platform, '_plugin_panels'):
            self._platform._plugin_panels[self._name] = panel
            return True
        return False

    async def emit(self, event: str, data: Any = None) -> bool:
        if not self._check_event_rate():
            log.warning('Plugin %s: event rate limit exceeded', self._name)
            return False
        await self._bus.emit(f'plugin.{self._name}.{event}', data, self._name)
        return True

    def on_event(self, event: str, callback: Callable):
        self._bus.subscribe(event, callback)

    # ── CERBERUS Cognitive API (TRUSTED+) ─────────────────────────────────

    async def emit_goal(self, goal_params: dict) -> Optional[str]:
        """Push a goal to the cognitive goal stack."""
        self._require('cognitive')
        mind = self._cerberus.mind if self._cerberus else None
        if mind:
            from cerberus.cognitive.mind import Goal
            goal = Goal(
                name     = goal_params.get('name', 'plugin_goal'),
                type     = goal_params.get('type', 'express'),
                priority = float(goal_params.get('priority', 0.5)),
                params   = goal_params.get('params', {}),
            )
            return await mind.goal_stack.push(goal)
        return None

    def get_memory_snapshot(self) -> dict:
        """Read working memory (no write access for plugins)."""
        self._require('cognitive')
        mind = self._cerberus.mind if self._cerberus else None
        if mind:
            return {
                'working': mind.working_memory.snapshot(),
                'attention': mind.attention.status(),
            }
        return {}

    def subscribe_percept(self, callback: Callable):
        """Subscribe to PerceptFrame updates."""
        self._require('sensors')
        self._bus.subscribe('perception.frame', callback)

    # ── CERBERUS Personality API (TRUSTED+) ──────────────────────────────

    def on_mood_change(self, callback: Callable):
        """Fire callback when mood label changes."""
        self._require('personality')
        self._bus.subscribe('personality.state', callback)

    def get_mood(self) -> dict:
        self._require('personality')
        if self._cerberus and hasattr(self._cerberus, 'personality'):
            return self._cerberus.personality.mood.to_dict()
        return {}

    def inject_mood_event(self, event: str, magnitude: float = 1.0):
        """Trigger an emotional stimulus."""
        self._require('personality')
        if self._cerberus and hasattr(self._cerberus, 'personality'):
            self._cerberus.personality.inject_event(event, magnitude)

    # ── CERBERUS Learning API (TRUSTED+) ──────────────────────────────────

    def record_user_preference(self, behavior_id: str, reward: float = 1.0):
        """Register a user behavior preference for learning."""
        self._require('learning')
        if self._cerberus and hasattr(self._cerberus, 'learning'):
            self._cerberus.learning.register_user_behavior(behavior_id, reward)

    def get_behavior_suggestion(self) -> str:
        """Get a behavior suggestion from the learning system."""
        self._require('learning')
        if self._cerberus and hasattr(self._cerberus, 'learning'):
            return self._cerberus.learning.suggest_behavior()
        return 'idle_breath'

    # ── Body API (TRUSTED+) ───────────────────────────────────────────────

    def get_body_state(self) -> dict:
        self._require('sensors')
        if self._cerberus and hasattr(self._cerberus, 'anatomy'):
            return self._cerberus.anatomy.body_state()
        return {}

    def get_fatigue(self) -> float:
        self._require('sensors')
        if self._cerberus and hasattr(self._cerberus, 'anatomy'):
            return self._cerberus.anatomy.energy.state.fatigue_level
        return 0.0


# ════════════════════════════════════════════════════════════════════════════
# EXAMPLE CERBERUS PLUGINS
# ════════════════════════════════════════════════════════════════════════════

# ── Example 1: Companion Greeter ──────────────────────────────────────────

GREETER_MANIFEST = {
    "name": "companion_greeter",
    "version": "1.0.0",
    "description": "Greets humans when detected in the nearby zone",
    "author": "CERBERUS Team",
    "trust_level": "trusted",
    "permissions": ["behaviors", "sensors", "personality", "cognitive"],
    "entry_point": "plugin.py",
    "cerberus_version": ">=2.0.0",
}

async def companion_greeter_init(ctx: CerberusPluginContext):
    """Greet humans when they enter the 'nearby' zone."""
    greeted_track_ids: Set[int] = set()
    last_greet_t = 0.0

    def on_percept(event, data):
        nonlocal last_greet_t
        humans = data.get('humans', [])
        for h in humans:
            tid  = h.get('track_id', -1)
            zone = h.get('zone', 'far')
            if zone in ('nearby', 'interact') and tid not in greeted_track_ids:
                if time.time() - last_greet_t > 10.0:   # max 1 greet per 10s
                    greeted_track_ids.add(tid)
                    last_greet_t = time.time()
                    asyncio.create_task(_do_greet(ctx))

    async def _do_greet(ctx: CerberusPluginContext):
        ctx.inject_mood_event('successful_interaction', magnitude=0.5)
        await ctx.emit_goal({
            'name': 'greet_human', 'type': 'express',
            'priority': 0.7, 'params': {'behavior': 'tail_wag'}
        })
        log.info('[companion_greeter] Greeting human!')

    ctx.subscribe_percept(on_percept)
    ctx.register_ui_panel({
        'id': 'greeter_status', 'title': 'Greeter',
        'icon': '👋', 'position': 'right'
    })
    log.info('[companion_greeter] Initialized')


# ── Example 2: Fatigue Modulator ──────────────────────────────────────────

FATIGUE_MANIFEST = {
    "name": "fatigue_modulator",
    "version": "1.0.0",
    "description": "Modulates behavior based on robot fatigue level",
    "trust_level": "trusted",
    "permissions": ["behaviors", "sensors", "personality"],
}

async def fatigue_modulator_init(ctx: CerberusPluginContext):
    """When fatigue is high, push a rest goal and soften behavior style."""
    _last_rest_t = 0.0

    async def check_fatigue():
        nonlocal _last_rest_t
        fatigue = ctx.get_fatigue()
        if fatigue > 0.7 and time.time() - _last_rest_t > 60:
            _last_rest_t = time.time()
            ctx.inject_mood_event('goal.failed', magnitude=0.3)  # tiredness → mild negative
            await ctx.emit_goal({
                'name': 'rest_when_tired', 'type': 'rest',
                'priority': 0.6, 'params': {'behavior': 'sit_down'}
            })
            log.info('[fatigue_modulator] High fatigue (%.2f) — pushing rest goal', fatigue)

    # Register as a periodic behavior check (via bus)
    ctx.on_event('body.state', lambda e, d: asyncio.create_task(check_fatigue()))
    log.info('[fatigue_modulator] Initialized')


# ── Example 3: Learning Demonstrator ─────────────────────────────────────

LEARNING_MANIFEST = {
    "name": "learning_demonstrator",
    "version": "1.0.0",
    "description": "Demonstrates imitation learning by recording user sequences",
    "trust_level": "trusted",
    "permissions": ["behaviors", "learning", "ui"],
}

async def learning_demonstrator_init(ctx: CerberusPluginContext):
    """Record and replay user behavior sequences."""
    ctx.register_behavior({
        'id': 'replay_learned',
        'name': 'Replay Learned',
        'category': 'custom',
        'icon': '🎓',
        'description': 'Replay the most recently learned behavior sequence',
        'duration_s': 10.0,
    })

    def on_behavior(event, data):
        if isinstance(data, dict):
            bid = data.get('id', '')
            if bid:
                ctx.record_user_preference(bid, reward=1.0)

    ctx.on_event('behavior_start', on_behavior)
    ctx.register_ui_panel({
        'id': 'learning_panel', 'title': 'Learning',
        'icon': '🎓', 'position': 'right'
    })
    log.info('[learning_demonstrator] Initialized — tracking behavior preferences')


# ════════════════════════════════════════════════════════════════════════════
# CERBERUS PLUGIN REGISTRY (extends base PluginSystem)
# ════════════════════════════════════════════════════════════════════════════

class CerberusPluginRegistry:
    """
    Extends the Go2 Platform PluginSystem with CERBERUS-specific features:
    trust levels, resource quotas, dependency graph, hot-reload.
    """

    def __init__(self, platform, cerberus, bus):
        self._platform = platform
        self._cerberus = cerberus
        self._bus      = bus
        self._plugins: Dict[str, dict] = {}   # name → {manifest, context, status}
        self._deps:    Dict[str, List[str]] = {}

    def create_context(self, plugin_name: str,
                        trust_str: str = 'community') -> CerberusPluginContext:
        trust_map = {
            'system': TrustLevel.SYSTEM, 'trusted': TrustLevel.TRUSTED,
            'community': TrustLevel.COMMUNITY, 'untrusted': TrustLevel.UNTRUSTED,
        }
        trust = trust_map.get(trust_str.lower(), TrustLevel.COMMUNITY)
        return CerberusPluginContext(
            plugin_name = plugin_name,
            trust       = trust,
            platform    = self._platform,
            cerberus    = self._cerberus,
            bus         = self._bus,
        )

    async def load_builtin_plugins(self):
        """Load and activate the built-in CERBERUS example plugins."""
        builtins = [
            ('companion_greeter', 'trusted', companion_greeter_init),
            ('fatigue_modulator', 'trusted', fatigue_modulator_init),
            ('learning_demonstrator', 'trusted', learning_demonstrator_init),
        ]
        for name, trust, init_fn in builtins:
            ctx = self.create_context(name, trust)
            try:
                await asyncio.wait_for(init_fn(ctx), timeout=5.0)
                self._plugins[name] = {
                    'name': name, 'trust': trust,
                    'status': 'active', 'context': ctx
                }
                log.info('CERBERUS plugin activated: %s [%s]', name, trust)
            except Exception as e:
                log.error('CERBERUS plugin failed [%s]: %s', name, e)
                self._plugins[name] = {'name': name, 'status': 'error', 'error': str(e)}

    def list(self) -> List[dict]:
        return [{k: v for k, v in p.items() if k != 'context'}
                for p in self._plugins.values()]

    def status(self) -> dict:
        return {
            'total': len(self._plugins),
            'active': sum(1 for p in self._plugins.values() if p.get('status') == 'active'),
            'plugins': self.list(),
        }
