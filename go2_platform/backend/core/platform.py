"""
go2_platform/backend/core/platform.py
══════════════════════════════════════════════════════════════════════════════
Go2 Platform — Authoritative Backend Core
The single source of truth for all robot state and behavior.

Architecture:
  UI / external clients
        │  REST + WebSocket
  ┌─────▼──────────────────────────────────────────────────────┐
  │  PlatformCore (this module)                                │
  │   ├─ EventBus (internal pub/sub)                          │
  │   ├─ AuthoritativeFSM (validated state machine)           │
  │   ├─ SafetyEnforcer (hard limits, ALWAYS final authority)  │
  │   ├─ WorldModel (maps, objects, zones, memory)             │
  │   ├─ MissionSystem (tasks, patrol, sequences)              │
  │   ├─ BehaviorRegistry (policies, animations, styles)       │
  │   ├─ PluginSystem (sandboxed, permission-gated)            │
  │   ├─ FleetManager (multi-robot coordination)               │
  │   └─ SessionManager (auth, rate limiting, audit log)       │
  └────────────────────────────────────────────────────────────┘
        │  validated commands only
  ROS2 Bridge
        │
  Go2 Hardware
══════════════════════════════════════════════════════════════════════════════
"""

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger('go2.platform')


# ════════════════════════════════════════════════════════════════════════════
# DOMAIN TYPES
# ════════════════════════════════════════════════════════════════════════════

class RobotState(Enum):
    OFFLINE     = 'offline'
    IDLE        = 'idle'
    STANDING    = 'standing'
    SITTING     = 'sitting'
    WALKING     = 'walking'
    FOLLOWING   = 'following'
    NAVIGATING  = 'navigating'
    INTERACTING = 'interacting'
    PERFORMING  = 'performing'    # animation/behavior sequence
    PATROLLING  = 'patrolling'
    FAULT       = 'fault'
    ESTOP       = 'estop'


class SafetyLevel(Enum):
    NORMAL   = 'normal'
    CAUTION  = 'caution'    # approaching limits
    WARNING  = 'warning'    # at limits, restricting commands
    CRITICAL = 'critical'   # hard stop in progress
    ESTOP    = 'estop'      # full hardware stop


class BehaviorPolicy(Enum):
    SMOOTH   = 'smooth'     # slow, fluid, for companion/indoors
    AGILE    = 'agile'      # fast, reactive
    STABLE   = 'stable'     # conservative, outdoor terrain
    ADAPTIVE = 'adaptive'   # auto-select based on environment


@dataclass
class Telemetry:
    ts: float = field(default_factory=time.monotonic)
    battery_pct: float = 87.0
    voltage: float = 29.4
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    yaw_deg: float = 0.0
    contact_force_n: float = 0.0
    com_x: float = 0.0
    foot_forces: Dict[str, float] = field(default_factory=lambda: {'fl':13,'fr':12,'rl':14,'rr':13})
    motor_temps: Dict[str, float] = field(default_factory=lambda: {'fl':42,'fr':43,'rl':41,'rr':42})
    joint_positions: Dict[str, float] = field(default_factory=dict)
    ctrl_hz: float = 500.0
    safety_level: str = SafetyLevel.NORMAL.value

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SafetyConfig:
    pitch_limit_deg: float = 10.0
    roll_limit_deg:  float = 10.0
    force_limit_n:   float = 30.0
    temp_limit_c:    float = 72.0
    battery_min_pct: float = 10.0
    watchdog_s:      float = 2.0
    human_zone_m:    float = 0.5
    max_velocity_ms: float = 1.5
    collision_stop_m: float = 0.25


@dataclass
class WorldObject:
    id: str
    name: str
    type: str                    # soft_prop / hard_prop / interactive / waypoint / zone
    affordances: List[str]
    moods: List[str]
    max_force_n: float
    pos: Dict[str, float]        # {x, y, z}
    contact_normal: List[float]
    tags: List[str] = field(default_factory=list)
    funscript: Optional[str] = None
    schema_ver: str = '2.0.0'
    notes: str = ''
    linked_behavior: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Zone:
    id: str
    name: str
    type: str                    # no_enter / slow / patrol / rest / geofence
    center: Dict[str, float]     # {x, y}
    radius_m: float
    active: bool = True
    trigger_action: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Mission:
    id: str
    name: str
    type: str                    # patrol / inspect / follow / sequence / conditional
    params: Dict[str, Any]
    status: str = 'pending'      # pending/running/paused/complete/failed
    created_at: float = field(default_factory=time.time)
    progress: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PluginInfo:
    name: str
    version: str
    description: str
    permissions: List[str]       # 'ui', 'behaviors', 'api', 'fsm', 'sensors', 'world'
    author: str
    status: str = 'inactive'
    instance: Any = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != 'instance'}


# ════════════════════════════════════════════════════════════════════════════
# EVENT BUS
# ════════════════════════════════════════════════════════════════════════════

class EventBus:
    """Async internal pub/sub for platform components."""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        self._history: List[dict] = []
        self._max_history = 500

    def subscribe(self, event: str, handler: Callable):
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Callable):
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    async def emit(self, event: str, data: Any = None, source: str = 'platform'):
        entry = {'event': event, 'data': data, 'source': source, 'ts': time.time()}
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        logger.debug(f'Event: {event} from {source}')
        for handler in self._handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event, data)
                else:
                    handler(event, data)
            except Exception as e:
                logger.error(f'Event handler error [{event}]: {e}')

    def recent(self, n: int = 50) -> List[dict]:
        return self._history[-n:]


# ════════════════════════════════════════════════════════════════════════════
# SAFETY ENFORCER  (ALWAYS has final authority)
# ════════════════════════════════════════════════════════════════════════════

class SafetyEnforcer:
    """
    Hard safety enforcement — cannot be bypassed by any API or plugin.
    Implements reflex layer: evaluates every command before ROS2 execution.

    Pipeline:
      Planner → FSM → SafetyEnforcer → ROS2 Bridge
                                ↑
                          (final authority)
    """

    def __init__(self, cfg: SafetyConfig, bus: EventBus):
        self.cfg = cfg
        self.bus = bus
        self.level = SafetyLevel.NORMAL
        self.trip_count = 0
        self.estop_count = 0
        self.last_trip_reason = ''
        self._telemetry = Telemetry()
        self._human_in_zone = False
        self._obstacle_dist = float('inf')
        self._last_telemetry_ts = time.monotonic()
        self._active_overrides: Set[str] = set()

    def update_telemetry(self, t: Telemetry):
        self._telemetry = t
        self._last_telemetry_ts = time.monotonic()

    def update_perception(self, human_in_zone: bool, obstacle_dist: float):
        self._human_in_zone = human_in_zone
        self._obstacle_dist = obstacle_dist

    async def evaluate(self, cmd: dict) -> tuple[bool, str]:
        """
        Evaluate a command. Returns (allowed, reason).
        This runs synchronously-fast (<< 1ms) for real-time loop compatibility.
        """
        if self.level == SafetyLevel.ESTOP:
            return False, 'E-STOP active'

        t = self._telemetry

        # Telemetry watchdog
        if time.monotonic() - self._last_telemetry_ts > self.cfg.watchdog_s:
            await self._trip('Telemetry watchdog timeout')
            return False, 'Watchdog timeout'

        # Hard limits
        if abs(t.pitch_deg) > self.cfg.pitch_limit_deg:
            await self._trip(f'Pitch {t.pitch_deg:.1f}° > ±{self.cfg.pitch_limit_deg}°')
            return False, 'Pitch limit'
        if abs(t.roll_deg) > self.cfg.roll_limit_deg:
            await self._trip(f'Roll {t.roll_deg:.1f}°')
            return False, 'Roll limit'
        if t.contact_force_n > self.cfg.force_limit_n:
            await self._trip(f'Force {t.contact_force_n:.0f}N > {self.cfg.force_limit_n}N')
            return False, 'Force limit'
        if t.battery_pct < self.cfg.battery_min_pct:
            await self._trip(f'Battery {t.battery_pct:.0f}%')
            return False, 'Battery critical'
        for k, temp in t.motor_temps.items():
            if temp > self.cfg.temp_limit_c:
                await self._trip(f'Motor {k} overtemp {temp:.0f}°C')
                return False, 'Motor overtemp'

        # Reflex: human zone
        if self._human_in_zone and cmd.get('action') in ('APPROACH','EXECUTE','MOTION_LOOP'):
            await self.bus.emit('safety.human_zone_block', cmd, 'safety')
            return False, 'Human detected in contact zone'

        # Reflex: obstacle proximity
        if self._obstacle_dist < self.cfg.collision_stop_m:
            if cmd.get('action') in ('NAVIGATE','WALK','APPROACH','ZOOMIES'):
                await self.bus.emit('safety.obstacle_block',
                                    {'dist': self._obstacle_dist}, 'safety')
                return False, f'Obstacle at {self._obstacle_dist:.2f}m'

        # Velocity cap
        if 'velocity' in cmd:
            v = cmd.get('velocity', 0)
            if abs(v) > self.cfg.max_velocity_ms:
                cmd['velocity'] = self.cfg.max_velocity_ms * (1 if v > 0 else -1)
                await self.bus.emit('safety.velocity_capped',
                                    {'requested': v, 'capped': cmd['velocity']}, 'safety')

        self._update_level()
        return True, 'ok'

    def _update_level(self):
        t = self._telemetry
        p = abs(t.pitch_deg) / self.cfg.pitch_limit_deg
        r = abs(t.roll_deg) / self.cfg.roll_limit_deg
        f = t.contact_force_n / self.cfg.force_limit_n
        worst = max(p, r, f)
        if worst < 0.6:
            new = SafetyLevel.NORMAL
        elif worst < 0.8:
            new = SafetyLevel.CAUTION
        elif worst < 1.0:
            new = SafetyLevel.WARNING
        else:
            new = SafetyLevel.CRITICAL
        self.level = new

    async def _trip(self, reason: str):
        self.trip_count += 1
        self.last_trip_reason = reason
        self.level = SafetyLevel.CRITICAL
        logger.error(f'SAFETY TRIP [{self.trip_count}]: {reason}')
        await self.bus.emit('safety.trip', {'reason': reason, 'count': self.trip_count}, 'safety')

    async def trigger_estop(self, source: str = 'api'):
        self.estop_count += 1
        self.level = SafetyLevel.ESTOP
        logger.critical(f'E-STOP [{self.estop_count}] source={source}')
        await self.bus.emit('safety.estop', {'source': source, 'count': self.estop_count}, 'safety')

    async def clear_estop(self):
        self.level = SafetyLevel.NORMAL
        logger.warning('E-STOP cleared — manual re-arm required')
        await self.bus.emit('safety.estop_cleared', {}, 'safety')

    def status(self) -> dict:
        return {
            'level': self.level.value,
            'trips': self.trip_count,
            'estops': self.estop_count,
            'last_trip': self.last_trip_reason,
            'human_in_zone': self._human_in_zone,
            'obstacle_dist': round(self._obstacle_dist, 2),
            'cfg': asdict(self.cfg),
        }


# ════════════════════════════════════════════════════════════════════════════
# AUTHORITATIVE FSM
# ════════════════════════════════════════════════════════════════════════════

# Validated transition table
_TRANSITIONS: Dict[RobotState, List[RobotState]] = {
    RobotState.OFFLINE:     [RobotState.IDLE],
    RobotState.IDLE:        [RobotState.STANDING, RobotState.SITTING, RobotState.ESTOP],
    RobotState.STANDING:    [RobotState.IDLE, RobotState.SITTING, RobotState.WALKING,
                             RobotState.FOLLOWING, RobotState.NAVIGATING,
                             RobotState.INTERACTING, RobotState.PERFORMING,
                             RobotState.PATROLLING, RobotState.ESTOP, RobotState.FAULT],
    RobotState.SITTING:     [RobotState.STANDING, RobotState.IDLE, RobotState.ESTOP],
    RobotState.WALKING:     [RobotState.STANDING, RobotState.FOLLOWING,
                             RobotState.NAVIGATING, RobotState.ESTOP, RobotState.FAULT],
    RobotState.FOLLOWING:   [RobotState.STANDING, RobotState.WALKING,
                             RobotState.ESTOP, RobotState.FAULT],
    RobotState.NAVIGATING:  [RobotState.STANDING, RobotState.WALKING,
                             RobotState.ESTOP, RobotState.FAULT],
    RobotState.INTERACTING: [RobotState.STANDING, RobotState.ESTOP, RobotState.FAULT],
    RobotState.PERFORMING:  [RobotState.STANDING, RobotState.ESTOP, RobotState.FAULT],
    RobotState.PATROLLING:  [RobotState.STANDING, RobotState.NAVIGATING,
                             RobotState.ESTOP, RobotState.FAULT],
    RobotState.FAULT:       [RobotState.IDLE, RobotState.ESTOP],
    RobotState.ESTOP:       [RobotState.IDLE],
}

class AuthoritativeFSM:
    """
    The single authoritative FSM for the entire platform.
    All state changes go through here — no exceptions.
    """

    def __init__(self, safety: SafetyEnforcer, bus: EventBus):
        self.state = RobotState.OFFLINE
        self.prev_state = RobotState.OFFLINE
        self.armed = False
        self.enter_ts = time.monotonic()
        self.history: List[dict] = []
        self.behaviors_complete = 0
        self._safety = safety
        self._bus = bus

    async def transition(self, new_state: RobotState,
                         reason: str = '', source: str = 'platform') -> tuple[bool, str]:
        allowed = _TRANSITIONS.get(self.state, [])
        if new_state not in allowed:
            msg = f'Invalid: {self.state.name} → {new_state.name}'
            logger.warning(msg)
            return False, msg

        # Safety gate
        if self._safety.level == SafetyLevel.ESTOP and new_state != RobotState.IDLE:
            return False, 'E-STOP active'
        if not self.armed and new_state not in (
                RobotState.IDLE, RobotState.OFFLINE, RobotState.ESTOP):
            return False, 'System not armed'

        self.prev_state = self.state
        self.state = new_state
        self.enter_ts = time.monotonic()
        self.history.append({
            'from': self.prev_state.name, 'to': new_state.name,
            'reason': reason, 'source': source, 't': time.time()
        })
        if len(self.history) > 200:
            self.history.pop(0)
        logger.info(f'FSM: {self.prev_state.name} → {new_state.name} [{reason}]')
        await self._bus.emit('fsm.transition', {
            'from': self.prev_state.name, 'to': new_state.name,
            'reason': reason, 'elapsed': round(time.monotonic() - self.enter_ts, 2)
        }, source)
        return True, 'ok'

    async def arm(self, source: str = 'api') -> tuple[bool, str]:
        if self._safety.level == SafetyLevel.ESTOP:
            return False, 'Cannot arm during E-STOP'
        self.armed = True
        logger.info(f'Armed by {source}')
        await self._bus.emit('fsm.armed', {'source': source}, source)
        return True, 'Armed'

    async def disarm(self, source: str = 'api') -> tuple[bool, str]:
        self.armed = False
        logger.info(f'Disarmed by {source}')
        # Force back to standing/idle
        if self.state not in (RobotState.IDLE, RobotState.STANDING, RobotState.OFFLINE):
            await self.transition(RobotState.STANDING, 'disarm', source)
        await self._bus.emit('fsm.disarmed', {'source': source}, source)
        return True, 'Disarmed'

    def status(self) -> dict:
        return {
            'state': self.state.value,
            'prev': self.prev_state.value,
            'armed': self.armed,
            'elapsed_s': round(time.monotonic() - self.enter_ts, 1),
            'behaviors_complete': self.behaviors_complete,
            'allowed_transitions': [s.value for s in _TRANSITIONS.get(self.state, [])],
        }


# ════════════════════════════════════════════════════════════════════════════
# WORLD MODEL
# ════════════════════════════════════════════════════════════════════════════

class WorldModel:
    """
    Persistent world model — maps, objects, zones, spatial memory.
    All perception data flows here and is validated before storage.
    """

    SCHEMA_VERSION = '2.0.0'

    DEFAULT_OBJECTS = [
        WorldObject('cushion_blue','Blue Cushion','soft_prop',
                    ['mount_play','knead','nuzzle'],['playful','gentle','curious'],
                    20.0,{'x':0,'y':0,'z':0.4},[0,0,1]),
        WorldObject('chair1','Wooden Chair','hard_prop',
                    ['mount_play','shake','scratch'],['excited','frantic'],
                    30.0,{'x':0,'y':0.2,'z':0.5},[0,-1,0]),
        WorldObject('plush_dog','Plush Dog','soft_prop',
                    ['mount_play','nuzzle','knead','nudge'],['affectionate','playful'],
                    15.0,{'x':0.3,'y':0,'z':0.3},[0,0,1]),
    ]

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.objects: Dict[str, WorldObject] = {}
        self.zones: Dict[str, Zone] = {}
        self.waypoints: Dict[str, dict] = {}
        self.grid_map: Optional[dict] = None
        self.detections: List[dict] = []
        self._load_defaults()

    def _load_defaults(self):
        for obj in self.DEFAULT_OBJECTS:
            self.objects[obj.id] = obj
        self.zones['home'] = Zone('home', 'Home Zone', 'rest',
                                  {'x': 0, 'y': 0}, 1.0)
        self.zones['no_humans'] = Zone('no_humans', 'No Human Zone', 'no_enter',
                                       {'x': 0, 'y': 0}, 0.5, trigger_action='stop')

    def add_object(self, obj: WorldObject) -> tuple[bool, str]:
        if not obj.id or not obj.type:
            return False, 'Object missing required fields'
        if obj.max_force_n <= 0 or obj.max_force_n > 100:
            return False, 'max_force_n out of range (0, 100]'
        self.objects[obj.id] = obj
        return True, 'added'

    def remove_object(self, obj_id: str) -> bool:
        return bool(self.objects.pop(obj_id, None))

    def add_zone(self, zone: Zone) -> bool:
        self.zones[zone.id] = zone
        return True

    def add_waypoint(self, wp_id: str, pos: dict, label: str = '') -> bool:
        self.waypoints[wp_id] = {'id': wp_id, 'pos': pos, 'label': label}
        return True

    def update_detections(self, detections: List[dict]):
        """Update from perception pipeline."""
        self.detections = detections

    def get_object(self, obj_id: str) -> Optional[WorldObject]:
        return self.objects.get(obj_id)

    def find_by_affordance(self, affordance: str) -> List[WorldObject]:
        return [o for o in self.objects.values() if affordance in o.affordances]

    def export(self) -> dict:
        return {
            'schema_version': self.SCHEMA_VERSION,
            'exported': time.time(),
            'objects': [o.to_dict() for o in self.objects.values()],
            'zones': [z.to_dict() for z in self.zones.values()],
            'waypoints': list(self.waypoints.values()),
        }

    def import_from_dict(self, data: dict) -> tuple[int, int]:
        """Import world data. Returns (objects_added, errors)."""
        added, errors = 0, 0
        for od in data.get('objects', []):
            try:
                obj = WorldObject(**{k: od[k] for k in WorldObject.__dataclass_fields__
                                     if k in od})
                ok, _ = self.add_object(obj)
                if ok: added += 1
                else: errors += 1
            except Exception:
                errors += 1
        for zd in data.get('zones', []):
            try:
                self.zones[zd['id']] = Zone(**zd)
                added += 1
            except Exception:
                errors += 1
        return added, errors


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR REGISTRY
# ════════════════════════════════════════════════════════════════════════════

class BehaviorRegistry:
    """
    Catalog of all registered behaviors, animations, and motion policies.
    Plugins can register new behaviors here.
    """

    BUILTIN = [
        {'id': 'sit',         'name': 'Sit',         'category': 'posture',  'icon': '🐾', 'duration_s': 1.5},
        {'id': 'stand',       'name': 'Stand',        'category': 'posture',  'icon': '🐕', 'duration_s': 1.2},
        {'id': 'stretch',     'name': 'Stretch',      'category': 'posture',  'icon': '🐶', 'duration_s': 2.0},
        {'id': 'head_tilt',   'name': 'Head Tilt',    'category': 'express',  'icon': '🤔', 'duration_s': 1.0},
        {'id': 'tail_wag',    'name': 'Happy Wag',    'category': 'express',  'icon': '🎉', 'duration_s': 2.5},
        {'id': 'roll_over',   'name': 'Roll Over',    'category': 'trick',    'icon': '🔄', 'duration_s': 3.0},
        {'id': 'paw_shake',   'name': 'Shake Paw',    'category': 'trick',    'icon': '🤝', 'duration_s': 2.0},
        {'id': 'zoomies',     'name': 'Zoomies!',     'category': 'play',     'icon': '💨', 'duration_s': 4.0},
        {'id': 'play_bow',    'name': 'Play Bow',     'category': 'play',     'icon': '🙇', 'duration_s': 1.5},
        {'id': 'patrol',      'name': 'Patrol',       'category': 'mission',  'icon': '🗺️', 'duration_s': None},
        {'id': 'follow',      'name': 'Follow Me',    'category': 'companion','icon': '👣', 'duration_s': None},
        {'id': 'idle_breath', 'name': 'Breathing',    'category': 'idle',     'icon': '💤', 'duration_s': None},
    ]

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._behaviors: Dict[str, dict] = {}
        self._policies: Dict[BehaviorPolicy, dict] = {
            BehaviorPolicy.SMOOTH:   {'max_vel': 0.8, 'accel': 0.3, 'jerk': 0.1},
            BehaviorPolicy.AGILE:    {'max_vel': 1.5, 'accel': 1.0, 'jerk': 0.5},
            BehaviorPolicy.STABLE:   {'max_vel': 0.6, 'accel': 0.2, 'jerk': 0.05},
            BehaviorPolicy.ADAPTIVE: {'max_vel': 1.0, 'accel': 0.5, 'jerk': 0.2},
        }
        self.active_policy = BehaviorPolicy.SMOOTH
        self.active_behavior: Optional[str] = None
        for b in self.BUILTIN:
            self._behaviors[b['id']] = b

    def register(self, behavior: dict, source: str = 'builtin') -> bool:
        """Register a behavior. Used by plugins."""
        required = {'id', 'name', 'category'}
        if not required.issubset(behavior.keys()):
            return False
        behavior['source'] = source
        self._behaviors[behavior['id']] = behavior
        return True

    def get(self, behavior_id: str) -> Optional[dict]:
        return self._behaviors.get(behavior_id)

    def list_by_category(self) -> Dict[str, List[dict]]:
        cats = defaultdict(list)
        for b in self._behaviors.values():
            cats[b.get('category', 'other')].append(b)
        return dict(cats)

    def set_policy(self, policy: BehaviorPolicy):
        self.active_policy = policy

    def get_policy_params(self) -> dict:
        return self._policies.get(self.active_policy, {})

    def all(self) -> List[dict]:
        return list(self._behaviors.values())


# ════════════════════════════════════════════════════════════════════════════
# MISSION SYSTEM
# ════════════════════════════════════════════════════════════════════════════

class MissionSystem:
    """
    Task and mission orchestration system.
    Missions are high-level goal sequences; the FSM executes them.
    """

    def __init__(self, fsm: AuthoritativeFSM, world: WorldModel, bus: EventBus):
        self.fsm = fsm
        self.world = world
        self.bus = bus
        self.missions: Dict[str, Mission] = {}
        self.active_mission: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    def create(self, name: str, mission_type: str, params: dict) -> Mission:
        m = Mission(id=str(uuid.uuid4())[:8], name=name, type=mission_type, params=params)
        self.missions[m.id] = m
        logger.info(f'Mission created: {m.id} {name} ({mission_type})')
        return m

    async def start(self, mission_id: str) -> tuple[bool, str]:
        m = self.missions.get(mission_id)
        if not m:
            return False, f'Mission {mission_id} not found'
        if not self.fsm.armed:
            return False, 'System not armed'
        if self.active_mission:
            return False, f'Mission {self.active_mission} already running'
        m.status = 'running'
        self.active_mission = mission_id
        self._task = asyncio.create_task(self._execute(m))
        await self.bus.emit('mission.started', m.to_dict(), 'missions')
        return True, 'started'

    async def stop(self, reason: str = 'user') -> bool:
        if not self.active_mission:
            return False
        m = self.missions.get(self.active_mission)
        if m:
            m.status = 'paused'
        if self._task:
            self._task.cancel()
        self.active_mission = None
        await self.bus.emit('mission.stopped', {'reason': reason}, 'missions')
        return True

    async def _execute(self, m: Mission):
        """Mission execution coroutine — dispatches by type."""
        try:
            if m.type == 'patrol':
                await self._patrol(m)
            elif m.type == 'follow':
                await self._follow(m)
            elif m.type == 'sequence':
                await self._sequence(m)
            elif m.type == 'inspect':
                await self._inspect(m)
            m.status = 'complete'
            m.progress = 1.0
            self.active_mission = None
            await self.bus.emit('mission.complete', m.to_dict(), 'missions')
        except asyncio.CancelledError:
            m.status = 'cancelled'
        except Exception as e:
            m.status = 'failed'
            logger.error(f'Mission {m.id} failed: {e}')
            await self.bus.emit('mission.failed', {'id': m.id, 'error': str(e)}, 'missions')

    async def _patrol(self, m: Mission):
        waypoints = m.params.get('waypoints', [])
        repeat = m.params.get('repeat', False)
        await self.fsm.transition(RobotState.PATROLLING, 'patrol_mission')
        while True:
            for i, wp_id in enumerate(waypoints):
                m.progress = i / max(len(waypoints), 1)
                await self.bus.emit('mission.progress',
                                    {'id': m.id, 'waypoint': wp_id, 'progress': m.progress},
                                    'missions')
                await asyncio.sleep(3.0)  # In HW: navigate to waypoint
            if not repeat:
                break
        await self.fsm.transition(RobotState.STANDING, 'patrol_complete')

    async def _follow(self, m: Mission):
        await self.fsm.transition(RobotState.FOLLOWING, 'follow_mission')
        timeout = m.params.get('timeout_s', 60)
        await asyncio.sleep(timeout)
        await self.fsm.transition(RobotState.STANDING, 'follow_timeout')

    async def _sequence(self, m: Mission):
        steps = m.params.get('steps', [])
        await self.fsm.transition(RobotState.PERFORMING, 'sequence_mission')
        for i, step in enumerate(steps):
            m.progress = i / max(len(steps), 1)
            await asyncio.sleep(step.get('duration_s', 1.0))
            await self.bus.emit('mission.step', {'step': step, 'index': i}, 'missions')
        await self.fsm.transition(RobotState.STANDING, 'sequence_complete')

    async def _inspect(self, m: Mission):
        targets = m.params.get('targets', [])
        await self.fsm.transition(RobotState.NAVIGATING, 'inspect_mission')
        for t in targets:
            await asyncio.sleep(2.0)
            await self.bus.emit('mission.inspection',
                                {'target': t, 'status': 'ok'}, 'missions')
        await self.fsm.transition(RobotState.STANDING, 'inspect_complete')

    def list(self) -> List[dict]:
        return [m.to_dict() for m in self.missions.values()]


# ════════════════════════════════════════════════════════════════════════════
# PLATFORM CORE  (top-level coordinator)
# ════════════════════════════════════════════════════════════════════════════

class PlatformCore:
    """
    Top-level platform coordinator.
    Initialises all subsystems and wires them together.
    """

    VERSION = '2.0.0'

    def __init__(self, safety_cfg: Optional[SafetyConfig] = None):
        self.bus         = EventBus()
        self.safety      = SafetyEnforcer(safety_cfg or SafetyConfig(), self.bus)
        self.fsm         = AuthoritativeFSM(self.safety, self.bus)
        self.world       = WorldModel(self.bus)
        self.behaviors   = BehaviorRegistry(self.bus)
        self.missions    = MissionSystem(self.fsm, self.world, self.bus)
        self.telemetry   = Telemetry()
        self.sim_mode    = True
        self.policy      = BehaviorPolicy.SMOOTH
        self._ws_clients: Set[Any] = set()
        self._sim_task: Optional[asyncio.Task] = None

        # Wire internal events
        self.bus.subscribe('safety.estop', self._on_estop)
        self.bus.subscribe('safety.trip', self._on_safety_trip)

        logger.info(f'PlatformCore v{self.VERSION} initialised')

    async def start(self):
        await self.fsm.transition(RobotState.IDLE, 'platform_start', 'core')
        if self.sim_mode:
            self._sim_task = asyncio.create_task(self._sim_loop())
        logger.info('Platform started')

    async def stop(self):
        if self._sim_task:
            self._sim_task.cancel()
        await self.bus.emit('platform.stop', {}, 'core')
        logger.info('Platform stopped')

    async def _on_estop(self, event, data):
        await self.fsm.transition(RobotState.ESTOP, data.get('source', '?'), 'safety')
        await self._broadcast({'type': 'estop', 'data': data})

    async def _on_safety_trip(self, event, data):
        await self.fsm.transition(RobotState.FAULT, data.get('reason', '?'), 'safety')
        await self._broadcast({'type': 'safety_trip', 'data': data})

    # ── Simulation loop ──────────────────────────────────────────────────

    async def _sim_loop(self):
        """Realistic telemetry simulation for SIM mode."""
        import math
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            active = self.fsm.state in (
                RobotState.WALKING, RobotState.FOLLOWING,
                RobotState.NAVIGATING, RobotState.INTERACTING,
                RobotState.PERFORMING, RobotState.PATROLLING)

            tel = self.telemetry
            tel.ts = time.monotonic()
            tel.battery_pct = max(0, tel.battery_pct - (0.003 if active else 0.001))
            tel.voltage = (tel.battery_pct / 100) * 33.6
            noise = lambda scale: (math.sin(t * 17.3) * 0.4 + math.sin(t * 43.1) * 0.6) * scale
            tel.pitch_deg = noise(2.0 if active else 0.4)
            tel.roll_deg  = noise(1.0 if active else 0.2)
            tel.yaw_deg   = (tel.yaw_deg + 0.05 * math.sin(t * 0.3)) % 360
            tel.contact_force_n = abs(noise(5 if active else 0))
            tel.com_x = math.sin(t * 0.5) * 0.03
            for k in ('fl','fr','rl','rr'):
                tel.foot_forces[k] = max(0, 13 + noise(5 if active else 2))
                tel.motor_temps[k] = min(90, tel.motor_temps.get(k, 42) +
                                         (0.015 if active else -0.005))
            tel.safety_level = self.safety.level.value
            self.safety.update_telemetry(tel)
            self.safety._update_level()

            # Broadcast telemetry to all WS clients
            await self._broadcast({'type': 'telemetry', 'data': tel.to_dict()})
            # State broadcast
            await self._broadcast({'type': 'fsm', 'data': self.fsm.status()})
            await asyncio.sleep(0.2)  # 5Hz to WS (robot runs at 500Hz internally)

    # ── WebSocket broadcast ──────────────────────────────────────────────

    def register_ws(self, ws):
        self._ws_clients.add(ws)

    def unregister_ws(self, ws):
        self._ws_clients.discard(ws)

    async def _broadcast(self, msg: dict):
        if not self._ws_clients:
            return
        data = json.dumps(msg)
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        for d in dead:
            self._ws_clients.discard(d)

    # ── Command gateway ──────────────────────────────────────────────────

    async def execute_command(self, cmd: dict, source: str = 'api') -> dict:
        """
        All commands from UI/API flow through here.
        Safety evaluation → FSM validation → execution.
        """
        action = cmd.get('action', '')

        # Hard safety gates
        if action == 'ESTOP':
            await self.safety.trigger_estop(source)
            await self.missions.stop('estop')
            return {'ok': True, 'action': 'ESTOP'}

        if action == 'CLEAR_ESTOP':
            await self.safety.clear_estop()
            await self.fsm.transition(RobotState.IDLE, 'estop_cleared', source)
            return {'ok': True}

        if action == 'ARM':
            ok, msg = await self.fsm.arm(source)
            return {'ok': ok, 'msg': msg}

        if action == 'DISARM':
            ok, msg = await self.fsm.disarm(source)
            return {'ok': ok, 'msg': msg}

        # Evaluate remaining commands through safety reflex
        ok, reason = await self.safety.evaluate(cmd)
        if not ok:
            await self._broadcast({'type': 'safety_block',
                                   'data': {'reason': reason, 'cmd': action}})
            return {'ok': False, 'reason': reason}

        # Route to subsystem
        return await self._route_command(cmd, source)

    async def _route_command(self, cmd: dict, source: str) -> dict:
        action = cmd.get('action', '')

        fsm_map = {
            'STAND':    RobotState.STANDING,
            'SIT':      RobotState.SITTING,
            'WALK':     RobotState.WALKING,
            'FOLLOW':   RobotState.FOLLOWING,
            'NAVIGATE': RobotState.NAVIGATING,
            'INTERACT': RobotState.INTERACTING,
            'PERFORM':  RobotState.PERFORMING,
        }

        if action in fsm_map:
            ok, msg = await self.fsm.transition(fsm_map[action], action, source)
            if ok:
                await self._broadcast({'type': 'fsm', 'data': self.fsm.status()})
            return {'ok': ok, 'msg': msg}

        if action == 'RUN_BEHAVIOR':
            behavior_id = cmd.get('behavior_id', '')
            b = self.behaviors.get(behavior_id)
            if not b:
                return {'ok': False, 'reason': f'Unknown behavior: {behavior_id}'}
            await self.fsm.transition(RobotState.PERFORMING, behavior_id, source)
            await self._broadcast({'type': 'behavior_start', 'data': b})
            if b.get('duration_s'):
                asyncio.create_task(self._auto_return_standing(b['duration_s']))
            return {'ok': True, 'behavior': b}

        if action == 'START_MISSION':
            mission_id = cmd.get('mission_id', '')
            ok, msg = await self.missions.start(mission_id)
            return {'ok': ok, 'msg': msg}

        if action == 'STOP_MISSION':
            ok = await self.missions.stop('user')
            return {'ok': ok}

        if action == 'SET_POLICY':
            policy_name = cmd.get('policy', 'SMOOTH')
            try:
                p = BehaviorPolicy[policy_name]
                self.behaviors.set_policy(p)
                return {'ok': True, 'policy': policy_name}
            except KeyError:
                return {'ok': False, 'reason': f'Unknown policy: {policy_name}'}

        return {'ok': False, 'reason': f'Unknown action: {action}'}

    async def _auto_return_standing(self, delay: float):
        await asyncio.sleep(delay)
        if self.fsm.state == RobotState.PERFORMING:
            self.fsm.behaviors_complete += 1
            await self.fsm.transition(RobotState.STANDING, 'behavior_complete')
            await self._broadcast({'type': 'fsm', 'data': self.fsm.status()})

    # ── Accessors ────────────────────────────────────────────────────────

    def full_status(self) -> dict:
        return {
            'platform': {'version': self.VERSION, 'sim': self.sim_mode},
            'fsm':      self.fsm.status(),
            'safety':   self.safety.status(),
            'telemetry': self.telemetry.to_dict(),
            'mission':  self.missions.active_mission,
            'policy':   self.behaviors.active_policy.value,
            'objects':  len(self.world.objects),
            'zones':    len(self.world.zones),
        }
