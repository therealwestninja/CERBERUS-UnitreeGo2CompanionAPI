"""
go2_platform/backend/behavior_tree/behavior_tree.py
══════════════════════════════════════════════════════════════════════════════
Behavior Tree (BT) Implementation

Industry-standard BT library with:
  - Composite nodes: Sequence, Selector, Parallel, RandomSelector
  - Decorator nodes: Inverter, Succeeder, Repeater, UntilFail, Cooldown, Timeout
  - Leaf nodes: Action, Condition
  - Blackboard: typed shared memory across tree
  - Subtree support (modular composition)
  - Tick statistics and debug visualization

Tick return values:
  SUCCESS — node completed successfully
  FAILURE — node failed (triggers parent fallback in Selector)
  RUNNING — node is in progress (returns to parent with RUNNING)

Integration with platform:
  - BehaviorTree subscribes to FSM events via EventBus
  - Actions publish commands to PlatformCore via EventBus
  - Blackboard mirrors robot telemetry for condition nodes
  - BT tick runs at ~10Hz (deliberative, not real-time)

Architecture layer:
  BehaviorTree (10Hz deliberative) → FSM commands → PlatformCore → Safety
"""

import asyncio
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

log = logging.getLogger('go2.bt')


class BTStatus(Enum):
    SUCCESS = 'SUCCESS'
    FAILURE = 'FAILURE'
    RUNNING = 'RUNNING'


# ════════════════════════════════════════════════════════════════════════════
# BLACKBOARD — typed shared memory
# ════════════════════════════════════════════════════════════════════════════

class Blackboard:
    """
    Typed key-value shared memory for behavior tree nodes.
    Provides: set, get with default, existence check, clear, snapshot.
    Supports namespacing for subtrees: 'mission/current_waypoint'.
    """

    def __init__(self):
        self._data: Dict[str, Any] = {}
        self._write_log: List[Tuple[float, str, Any]] = []

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._write_log.append((time.monotonic(), key, value))
        if len(self._write_log) > 500:
            self._write_log.pop(0)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._data

    def delete(self, key: str) -> bool:
        return bool(self._data.pop(key, None))

    def clear_namespace(self, prefix: str):
        to_del = [k for k in self._data if k.startswith(prefix)]
        for k in to_del:
            del self._data[k]

    def snapshot(self) -> dict:
        return dict(self._data)

    def update_from_telemetry(self, tel: dict):
        """Mirror telemetry into blackboard for condition evaluation."""
        self.set('robot.battery_pct', tel.get('battery_pct', 100))
        self.set('robot.pitch_deg', tel.get('pitch_deg', 0))
        self.set('robot.roll_deg', tel.get('roll_deg', 0))
        self.set('robot.contact_force_n', tel.get('contact_force_n', 0))
        temps = tel.get('motor_temps', {})
        self.set('robot.max_temp', max(temps.values()) if temps else 0)
        self.set('robot.ts', tel.get('ts', time.monotonic()))

    def update_from_fsm(self, fsm: dict):
        self.set('robot.state', fsm.get('state', 'unknown'))
        self.set('robot.armed', fsm.get('armed', False))


# ════════════════════════════════════════════════════════════════════════════
# BASE NODE
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TickContext:
    blackboard: Blackboard
    platform:   Any      # PlatformCore reference
    bus:        Any      # EventBus reference
    dt_s:       float = 0.1  # time since last tick

    async def emit(self, event: str, data: dict):
        if self.bus:
            await self.bus.emit(event, data, 'behavior_tree')

    async def command(self, action: str, **params):
        """Send a command through PlatformCore."""
        if self.platform:
            cmd = {'action': action, **params}
            return await self.platform.execute_command(cmd, source='behavior_tree')
        return {'ok': False, 'reason': 'no platform'}


class BTNode(ABC):
    """Abstract base for all behavior tree nodes."""

    def __init__(self, name: str):
        self.name = name
        self._status = BTStatus.FAILURE
        self._tick_count = 0
        self._last_tick_t = 0.0
        self._total_running_s = 0.0

    @abstractmethod
    async def tick(self, ctx: TickContext) -> BTStatus:
        """Execute one tick. Return SUCCESS, FAILURE, or RUNNING."""

    async def reset(self):
        """Called when parent resets this subtree."""
        self._status = BTStatus.FAILURE

    @property
    def status(self) -> BTStatus:
        return self._status

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self.name!r})'

    def debug_str(self, indent: int = 0) -> str:
        sym = {'SUCCESS': '✓', 'FAILURE': '✗', 'RUNNING': '⟳'}.get(
            self._status.value, '?')
        return '  ' * indent + f'{sym} {self.__class__.__name__}: {self.name}'


# ════════════════════════════════════════════════════════════════════════════
# COMPOSITE NODES
# ════════════════════════════════════════════════════════════════════════════

class Sequence(BTNode):
    """
    AND node: ticks children left-to-right.
    Returns SUCCESS only if ALL children succeed.
    Returns FAILURE on first child failure.
    Returns RUNNING if current child returns RUNNING.
    """

    def __init__(self, name: str, children: List[BTNode]):
        super().__init__(name)
        self.children = children
        self._current_idx = 0

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        while self._current_idx < len(self.children):
            child = self.children[self._current_idx]
            status = await child.tick(ctx)
            if status == BTStatus.RUNNING:
                self._status = BTStatus.RUNNING
                return self._status
            if status == BTStatus.FAILURE:
                self._current_idx = 0
                self._status = BTStatus.FAILURE
                return self._status
            # SUCCESS: advance to next
            self._current_idx += 1

        self._current_idx = 0
        self._status = BTStatus.SUCCESS
        return self._status

    async def reset(self):
        self._current_idx = 0
        for child in self.children:
            await child.reset()

    def debug_str(self, indent: int = 0) -> str:
        lines = [super().debug_str(indent) + ' [SEQ]']
        for c in self.children:
            lines.append(c.debug_str(indent + 1))
        return '\n'.join(lines)


class Selector(BTNode):
    """
    OR node: ticks children left-to-right.
    Returns SUCCESS on first child success (fallback chain).
    Returns FAILURE only if ALL children fail.
    Returns RUNNING if current child returns RUNNING.
    """

    def __init__(self, name: str, children: List[BTNode]):
        super().__init__(name)
        self.children = children
        self._current_idx = 0

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        while self._current_idx < len(self.children):
            child = self.children[self._current_idx]
            status = await child.tick(ctx)
            if status == BTStatus.RUNNING:
                self._status = BTStatus.RUNNING
                return self._status
            if status == BTStatus.SUCCESS:
                self._current_idx = 0
                self._status = BTStatus.SUCCESS
                return self._status
            # FAILURE: try next child
            self._current_idx += 1

        self._current_idx = 0
        self._status = BTStatus.FAILURE
        return self._status

    async def reset(self):
        self._current_idx = 0
        for child in self.children:
            await child.reset()

    def debug_str(self, indent: int = 0) -> str:
        lines = [super().debug_str(indent) + ' [SEL]']
        for c in self.children:
            lines.append(c.debug_str(indent + 1))
        return '\n'.join(lines)


class Parallel(BTNode):
    """
    Ticks ALL children every tick regardless of status.
    policy='all': SUCCESS when all succeed
    policy='any': SUCCESS when any succeeds
    """

    def __init__(self, name: str, children: List[BTNode],
                 policy: str = 'all'):
        super().__init__(name)
        self.children = children
        self.policy = policy

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        results = []
        for child in self.children:
            results.append(await child.tick(ctx))

        successes = results.count(BTStatus.SUCCESS)
        failures  = results.count(BTStatus.FAILURE)

        if self.policy == 'any' and successes > 0:
            self._status = BTStatus.SUCCESS
        elif self.policy == 'all' and successes == len(self.children):
            self._status = BTStatus.SUCCESS
        elif failures == len(self.children):
            self._status = BTStatus.FAILURE
        else:
            self._status = BTStatus.RUNNING
        return self._status

    def debug_str(self, indent: int = 0) -> str:
        lines = [super().debug_str(indent) + f' [PAR:{self.policy}]']
        for c in self.children:
            lines.append(c.debug_str(indent + 1))
        return '\n'.join(lines)


class RandomSelector(BTNode):
    """Like Selector but shuffles children each reset (exploration)."""

    def __init__(self, name: str, children: List[BTNode]):
        super().__init__(name)
        self._orig = list(children)
        self.children = list(children)
        self._idx = 0

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        while self._idx < len(self.children):
            status = await self.children[self._idx].tick(ctx)
            if status != BTStatus.FAILURE:
                self._status = status
                if status == BTStatus.SUCCESS:
                    self._idx = 0
                return self._status
            self._idx += 1
        self._idx = 0
        self._status = BTStatus.FAILURE
        return self._status

    async def reset(self):
        self.children = list(self._orig)
        random.shuffle(self.children)
        self._idx = 0


# ════════════════════════════════════════════════════════════════════════════
# DECORATOR NODES
# ════════════════════════════════════════════════════════════════════════════

class Inverter(BTNode):
    """Inverts child status: SUCCESS↔FAILURE (RUNNING unchanged)."""
    def __init__(self, name: str, child: BTNode):
        super().__init__(name)
        self.child = child

    async def tick(self, ctx: TickContext) -> BTStatus:
        s = await self.child.tick(ctx)
        if s == BTStatus.SUCCESS: self._status = BTStatus.FAILURE
        elif s == BTStatus.FAILURE: self._status = BTStatus.SUCCESS
        else: self._status = s
        return self._status


class Succeeder(BTNode):
    """Always returns SUCCESS regardless of child result."""
    def __init__(self, name: str, child: BTNode):
        super().__init__(name)
        self.child = child

    async def tick(self, ctx: TickContext) -> BTStatus:
        await self.child.tick(ctx)
        self._status = BTStatus.SUCCESS
        return self._status


class Repeater(BTNode):
    """Repeat child N times (or infinitely if n=-1)."""
    def __init__(self, name: str, child: BTNode, n: int = -1):
        super().__init__(name)
        self.child = child
        self.n = n
        self._count = 0

    async def tick(self, ctx: TickContext) -> BTStatus:
        while self.n < 0 or self._count < self.n:
            s = await self.child.tick(ctx)
            if s == BTStatus.RUNNING:
                self._status = BTStatus.RUNNING
                return self._status
            if s == BTStatus.FAILURE:
                self._status = BTStatus.FAILURE
                return self._status
            self._count += 1
            await self.child.reset()
        self._count = 0
        self._status = BTStatus.SUCCESS
        return self._status


class Cooldown(BTNode):
    """Prevents child from ticking more than once per `cooldown_s` seconds."""
    def __init__(self, name: str, child: BTNode, cooldown_s: float):
        super().__init__(name)
        self.child = child
        self.cooldown_s = cooldown_s
        self._last_success_t = 0.0

    async def tick(self, ctx: TickContext) -> BTStatus:
        now = time.monotonic()
        if now - self._last_success_t < self.cooldown_s:
            self._status = BTStatus.FAILURE  # still cooling down
            return self._status
        s = await self.child.tick(ctx)
        if s == BTStatus.SUCCESS:
            self._last_success_t = now
        self._status = s
        return s


class Timeout(BTNode):
    """Returns FAILURE if child hasn't succeeded within `timeout_s`."""
    def __init__(self, name: str, child: BTNode, timeout_s: float):
        super().__init__(name)
        self.child = child
        self.timeout_s = timeout_s
        self._start_t: Optional[float] = None

    async def tick(self, ctx: TickContext) -> BTStatus:
        if self._start_t is None:
            self._start_t = time.monotonic()
        if time.monotonic() - self._start_t > self.timeout_s:
            self._start_t = None
            self._status = BTStatus.FAILURE
            return self._status
        s = await self.child.tick(ctx)
        if s != BTStatus.RUNNING:
            self._start_t = None
        self._status = s
        return s


# ════════════════════════════════════════════════════════════════════════════
# LEAF NODES
# ════════════════════════════════════════════════════════════════════════════

class Condition(BTNode):
    """
    Evaluates a predicate against the blackboard.
    Returns SUCCESS if predicate is True, FAILURE otherwise.
    """

    def __init__(self, name: str, predicate: Callable[[Blackboard], bool]):
        super().__init__(name)
        self._pred = predicate

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        try:
            result = self._pred(ctx.blackboard)
            self._status = BTStatus.SUCCESS if result else BTStatus.FAILURE
        except Exception as e:
            log.debug('Condition %r error: %s', self.name, e)
            self._status = BTStatus.FAILURE
        return self._status


class Action(BTNode):
    """
    Executes an async coroutine and maps its result to BT status.
    The coroutine receives TickContext and returns:
      True/dict → SUCCESS, False/None → FAILURE, 'running' → RUNNING
    """

    def __init__(self, name: str,
                 action: Callable[[TickContext], Any],
                 expected_duration_s: float = 0.0):
        super().__init__(name)
        self._action = action
        self._expected_dur = expected_duration_s
        self._start_t: Optional[float] = None

    async def tick(self, ctx: TickContext) -> BTStatus:
        self._tick_count += 1
        if self._start_t is None:
            self._start_t = time.monotonic()
        try:
            result = self._action(ctx)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            log.error('Action %r error: %s', self.name, e)
            self._start_t = None
            self._status = BTStatus.FAILURE
            return self._status

        if result == 'running' or result is None and self._expected_dur > 0:
            elapsed = time.monotonic() - self._start_t
            if elapsed < self._expected_dur:
                self._status = BTStatus.RUNNING
                return self._status

        self._start_t = None
        if result is False or result == 'failure':
            self._status = BTStatus.FAILURE
        else:
            self._status = BTStatus.SUCCESS
        return self._status

    async def reset(self):
        self._start_t = None


class Wait(BTNode):
    """Returns RUNNING for `duration_s` then SUCCESS."""
    def __init__(self, name: str, duration_s: float):
        super().__init__(name)
        self.duration_s = duration_s
        self._start: Optional[float] = None

    async def tick(self, ctx: TickContext) -> BTStatus:
        if self._start is None:
            self._start = time.monotonic()
        if time.monotonic() - self._start >= self.duration_s:
            self._start = None
            self._status = BTStatus.SUCCESS
        else:
            self._status = BTStatus.RUNNING
        return self._status

    async def reset(self):
        self._start = None


# ════════════════════════════════════════════════════════════════════════════
# BUILT-IN BEHAVIOR TREES for Go2
# ════════════════════════════════════════════════════════════════════════════

def build_companion_tree() -> BTNode:
    """
    Companion behavior tree.
    Priority (highest first):
    1. Handle emergency (obstacle, low battery, tilt)
    2. Execute active mission
    3. Follow human if visible
    4. Express idle behaviors (Utility-AI selected)
    """

    # ── Conditions ────────────────────────────────────────────────────────
    is_armed          = Condition('armed?',          lambda bb: bb.get('robot.armed', False))
    battery_ok        = Condition('battery_ok?',     lambda bb: bb.get('robot.battery_pct', 100) > 15)
    battery_critical  = Condition('battery_crit?',   lambda bb: bb.get('robot.battery_pct', 100) < 10)
    is_tilted         = Condition('tilted?',          lambda bb: abs(bb.get('robot.pitch_deg', 0)) > 8)
    has_mission       = Condition('has_mission?',     lambda bb: bb.has('mission.active'))
    human_visible     = Condition('human_visible?',   lambda bb: bb.get('perception.human_dist', 99) < 3.0)
    obstacle_close    = Condition('obstacle_close?',  lambda bb: bb.get('perception.obstacle_dist', 99) < 0.4)
    at_target         = Condition('at_target?',       lambda bb: bb.get('nav.at_target', False))
    is_idle           = Condition('idle?',            lambda bb: bb.get('robot.state', '') in ('idle','standing'))

    # ── Actions ───────────────────────────────────────────────────────────
    async def do_estop(ctx: TickContext):
        log.warning('BT: triggering E-STOP')
        return await ctx.command('ESTOP')

    async def do_obstacle_avoid(ctx: TickContext):
        log.info('BT: obstacle avoidance')
        return await ctx.command('STAND')

    async def do_sit(ctx: TickContext):
        log.info('BT: sitting (low battery)')
        return await ctx.command('SIT')

    async def do_run_mission(ctx: TickContext):
        mission_id = ctx.blackboard.get('mission.active')
        if mission_id:
            return await ctx.command('START_MISSION', mission_id=mission_id)
        return False

    async def do_follow(ctx: TickContext):
        return await ctx.command('FOLLOW')

    async def do_idle_behavior(ctx: TickContext):
        behaviors = ['tail_wag', 'head_tilt', 'idle_breath']
        import random as _r
        chosen = _r.choice(behaviors)
        ctx.blackboard.set('anim.requested', chosen)
        return await ctx.command('RUN_BEHAVIOR', behavior_id=chosen)

    estop_action    = Action('E-STOP', do_estop)
    obstacle_action = Action('obstacle_avoid', do_obstacle_avoid, expected_duration_s=2.0)
    sit_action      = Action('sit_down', do_sit, expected_duration_s=2.0)
    mission_action  = Action('run_mission', do_run_mission, expected_duration_s=30.0)
    follow_action   = Action('follow_human', do_follow, expected_duration_s=0.0)
    idle_action     = Action('idle_behavior', do_idle_behavior, expected_duration_s=3.0)

    # ── Emergency subtree ─────────────────────────────────────────────────
    emergency = Selector('emergency', [
        Sequence('estop_on_tilt', [is_tilted, estop_action]),
        Sequence('avoid_obstacle', [obstacle_close, obstacle_action]),
        Sequence('sit_low_battery', [battery_critical, sit_action]),
    ])

    # ── Mission subtree ────────────────────────────────────────────────────
    mission = Sequence('run_active_mission', [
        is_armed, battery_ok, has_mission, mission_action
    ])

    # ── Companion subtree ─────────────────────────────────────────────────
    companion = Sequence('companion_follow', [
        is_armed, battery_ok, human_visible, follow_action
    ])

    # ── Idle subtree ──────────────────────────────────────────────────────
    idle = Sequence('idle_expressions', [
        is_idle, Cooldown('cooldown_idle', idle_action, cooldown_s=5.0)
    ])

    # ── Root ──────────────────────────────────────────────────────────────
    root = Selector('root', [emergency, mission, companion, idle])
    return root


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR TREE RUNNER
# ════════════════════════════════════════════════════════════════════════════

class BehaviorTreeRunner:
    """
    Ticks the behavior tree at a configurable rate (default 10Hz).
    Integrates with PlatformCore via EventBus.
    Provides debug visualization and tick statistics.
    """

    DEFAULT_TICK_HZ = 10.0

    def __init__(self, platform=None, bus=None, tick_hz: float = DEFAULT_TICK_HZ):
        self.blackboard = Blackboard()
        self._platform  = platform
        self._bus       = bus
        self._tick_hz   = tick_hz
        self._tree:     Optional[BTNode] = None
        self._task:     Optional[asyncio.Task] = None
        self._ticks:    int = 0
        self._active    = False
        self._last_status = BTStatus.FAILURE

        # Subscribe to telemetry/FSM events for blackboard updates
        if bus:
            bus.subscribe('telemetry',      self._on_telemetry)
            bus.subscribe('fsm.transition', self._on_fsm)
            bus.subscribe('detections',     self._on_detections)

    def set_tree(self, tree: BTNode):
        self._tree = tree
        log.info('BT: tree set — %r', tree.name)

    async def start(self):
        if self._tree is None:
            self._tree = build_companion_tree()
            log.info('BT: using built-in companion tree')
        self._active = True
        self._task = asyncio.create_task(self._run())
        log.info('BT runner started at %.0fHz', self._tick_hz)

    async def stop(self):
        self._active = False
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass

    async def _run(self):
        dt = 1.0 / self._tick_hz
        while self._active:
            t0 = time.monotonic()
            await self._tick_once()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0, dt - elapsed))

    async def _tick_once(self):
        if self._tree is None:
            return
        self._ticks += 1
        ctx = TickContext(
            blackboard=self.blackboard,
            platform=self._platform,
            bus=self._bus,
            dt_s=1.0 / self._tick_hz,
        )
        try:
            status = await self._tree.tick(ctx)
            self._last_status = status
        except Exception as e:
            log.error('BT tick error: %s', e)

        # Broadcast debug info every 50 ticks
        if self._ticks % 50 == 0 and self._bus:
            await self._bus.emit('bt.tick_stats', self.status_dict(), 'behavior_tree')

    def _on_telemetry(self, event: str, data: dict):
        self.blackboard.update_from_telemetry(data)

    def _on_fsm(self, event: str, data: dict):
        self.blackboard.update_from_fsm(data)

    def _on_detections(self, event: str, data: dict):
        dets = data.get('detections', [])
        # Find nearest person
        person_dists = [d.get('dist_m', 99) for d in dets if d.get('label') == 'person']
        self.blackboard.set('perception.human_dist', min(person_dists) if person_dists else 99)
        # Nearest obstacle
        obs_dists = [d.get('dist_m', 99) for d in dets if d.get('label') != 'person']
        self.blackboard.set('perception.obstacle_dist', min(obs_dists) if obs_dists else 99)

    def status_dict(self) -> dict:
        return {
            'ticks': self._ticks,
            'hz': self._tick_hz,
            'active': self._active,
            'last_status': self._last_status.value if self._last_status else None,
            'tree': self._tree.name if self._tree else None,
            'blackboard_keys': list(self.blackboard.snapshot().keys()),
        }

    def debug_tree(self) -> str:
        return self._tree.debug_str() if self._tree else '(no tree)'
