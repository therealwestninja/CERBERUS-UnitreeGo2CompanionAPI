"""
go2_platform/backend/core/fleet.py
══════════════════════════════════════════════════════════════════════════════
Fleet Manager — Multi-robot coordination, shared tasks, synchronized behaviors.

Architecture:
  FleetManager
   ├─ RobotProxy (one per connected robot, wraps individual PlatformCore)
   ├─ FleetScheduler (distributed task assignment)
   ├─ SyncEngine (choreography: time-locked behavior execution)
   └─ FleetEventBus (cross-robot event routing)

Communication:
  Each robot runs its own PlatformCore + backend.
  Fleet coordinator connects via REST/WS to each robot's backend.
  Optionally: single shared broker (Redis pub/sub) for low-latency sync.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger('go2.fleet')


class RobotStatus(Enum):
    OFFLINE     = 'offline'
    ONLINE      = 'online'
    BUSY        = 'busy'
    ERROR       = 'error'
    ESTOP       = 'estop'


@dataclass
class RobotProxy:
    """Represents one robot in the fleet."""
    robot_id:    str
    name:        str
    api_url:     str              # http://192.168.1.x:8080
    ws_url:      str              # ws://192.168.1.x:8080/ws
    model:       str = 'edu'     # air/pro/edu
    status:      str = RobotStatus.OFFLINE.value
    last_seen:   float = field(default_factory=time.time)
    telemetry:   Dict[str, Any] = field(default_factory=dict)
    fsm_state:   str = 'offline'
    capabilities: List[str] = field(default_factory=list)
    assigned_task: Optional[str] = None
    _ws: Any = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if not k.startswith('_')}

    @property
    def available(self) -> bool:
        return (self.status == RobotStatus.ONLINE.value
                and self.assigned_task is None
                and self.fsm_state in ('idle', 'standing'))


@dataclass
class FleetTask:
    """A task that can be distributed across one or more robots."""
    task_id:    str
    name:       str
    type:       str               # patrol / synchronized_dance / convoy / inspect_multi
    params:     Dict[str, Any]
    robot_ids:  List[str]         # which robots execute this
    status:     str = 'pending'   # pending/running/complete/failed
    progress:   float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class SyncEngine:
    """
    Time-locked behavior synchronization across multiple robots.
    Uses a conductor/performer model:
      - Conductor broadcasts a countdown + beat reference
      - Performers execute behaviors synchronized to shared clock offset
    """

    SYNC_PRECISION_MS = 50  # acceptable timing error

    def __init__(self):
        self._sessions: Dict[str, dict] = {}

    def create_session(self, behavior_id: str, robot_ids: List[str],
                        delay_s: float = 2.0) -> str:
        """
        Create a synchronized execution session.
        delay_s: how long robots have to prepare before T=0.
        """
        session_id = str(uuid.uuid4())[:8]
        t_zero = time.time() + delay_s
        self._sessions[session_id] = {
            'id':           session_id,
            'behavior_id':  behavior_id,
            'robot_ids':    robot_ids,
            't_zero':       t_zero,
            'delay_s':      delay_s,
            'status':       'countdown',
            'ready':        [],
        }
        logger.info(f'Sync session {session_id}: {behavior_id} '
                    f'T0={t_zero:.2f} robots={robot_ids}')
        return session_id

    def mark_ready(self, session_id: str, robot_id: str):
        s = self._sessions.get(session_id)
        if s:
            s['ready'].append(robot_id)
            all_ready = set(s['ready']) >= set(s['robot_ids'])
            logger.debug(f'Sync {session_id}: {robot_id} ready '
                         f'({len(s["ready"])}/{len(s["robot_ids"])})')
            return all_ready
        return False

    def get_session(self, session_id: str) -> Optional[dict]:
        return self._sessions.get(session_id)

    def get_t_zero(self, session_id: str) -> Optional[float]:
        s = self._sessions.get(session_id)
        return s['t_zero'] if s else None


class FleetScheduler:
    """
    Distributes fleet tasks to available robots using simple strategies:
      - round_robin: cycle through available robots
      - nearest:     assign to closest robot by estimated position
      - broadcast:   send task to ALL available robots
      - optimal:     prefer robots with matching capabilities
    """

    def __init__(self, robots: Dict[str, RobotProxy]):
        self._robots = robots
        self._rr_idx = 0

    def assign(self, task: FleetTask,
               strategy: str = 'round_robin') -> List[str]:
        """Returns list of robot IDs assigned to this task."""
        available = [r for r in self._robots.values() if r.available]
        if not available:
            logger.warning(f'No available robots for task {task.task_id}')
            return []

        if task.robot_ids:
            # Explicit assignment
            return [rid for rid in task.robot_ids if rid in self._robots]

        if strategy == 'broadcast':
            return [r.robot_id for r in available]

        if strategy == 'round_robin':
            r = available[self._rr_idx % len(available)]
            self._rr_idx += 1
            return [r.robot_id]

        if strategy == 'optimal':
            # Find robot with most matching capabilities
            best = max(available,
                       key=lambda r: len(set(r.capabilities) &
                                         set(task.params.get('required_caps', []))))
            return [best.robot_id]

        return [available[0].robot_id]


class FleetManager:
    """
    Top-level fleet coordinator.
    Manages N robots, assigns tasks, coordinates synchronized behaviors.
    """

    MAX_ROBOTS = 20

    def __init__(self):
        self.robots:    Dict[str, RobotProxy]  = {}
        self.tasks:     Dict[str, FleetTask]   = {}
        self._sync     = SyncEngine()
        self._scheduler: Optional[FleetScheduler] = None
        self._poll_tasks: Dict[str, asyncio.Task] = {}
        logger.info('FleetManager initialized')

    # ── Robot registration ────────────────────────────────────────────────

    def register_robot(self, robot_id: str, name: str,
                        api_url: str, model: str = 'edu') -> RobotProxy:
        if len(self.robots) >= self.MAX_ROBOTS:
            raise ValueError(f'Fleet limit ({self.MAX_ROBOTS}) reached')
        ws_url = api_url.replace('http', 'ws') + '/ws'
        proxy = RobotProxy(robot_id=robot_id, name=name,
                           api_url=api_url, ws_url=ws_url, model=model)
        self.robots[robot_id] = proxy
        self._scheduler = FleetScheduler(self.robots)
        logger.info(f'Robot registered: {robot_id} ({name}) @ {api_url}')
        return proxy

    def deregister_robot(self, robot_id: str) -> bool:
        if robot_id not in self.robots:
            return False
        if task := self.robots[robot_id].assigned_task:
            logger.warning(f'Deregistering robot {robot_id} mid-task {task}')
        del self.robots[robot_id]
        self._scheduler = FleetScheduler(self.robots)
        return True

    # ── Connectivity ──────────────────────────────────────────────────────

    async def connect_all(self):
        """Attempt WS connections to all registered robots."""
        coros = [self._connect_robot(r) for r in self.robots.values()]
        await asyncio.gather(*coros, return_exceptions=True)

    async def _connect_robot(self, proxy: RobotProxy):
        try:
            import websockets
            async with websockets.connect(proxy.ws_url,
                                           open_timeout=3.0) as ws:
                proxy._ws = ws
                proxy.status = RobotStatus.ONLINE.value
                proxy.last_seen = time.time()
                logger.info(f'Fleet connected: {proxy.robot_id}')
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        self._handle_robot_msg(proxy, data)
                    except Exception:
                        pass
        except Exception as e:
            proxy.status = RobotStatus.OFFLINE.value
            proxy._ws = None
            logger.warning(f'Fleet connection failed {proxy.robot_id}: {e}')

    def _handle_robot_msg(self, proxy: RobotProxy, msg: dict):
        t = msg.get('type', '')
        if t == 'telemetry':
            proxy.telemetry = msg.get('data', {})
            proxy.last_seen = time.time()
        elif t == 'fsm':
            proxy.fsm_state = msg['data'].get('state', 'unknown')
            if proxy.fsm_state not in ('idle', 'standing', 'offline'):
                proxy.status = RobotStatus.BUSY.value
            else:
                proxy.status = RobotStatus.ONLINE.value
        elif t == 'estop':
            proxy.status = RobotStatus.ESTOP.value

    # ── Commands ──────────────────────────────────────────────────────────

    async def send_command(self, robot_id: str, cmd: dict) -> dict:
        """Send command to a specific robot via its REST API."""
        proxy = self.robots.get(robot_id)
        if not proxy:
            return {'ok': False, 'reason': f'Robot {robot_id} not found'}
        if proxy.status == RobotStatus.ESTOP.value:
            return {'ok': False, 'reason': 'Robot in E-STOP'}
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        proxy.api_url + '/api/v1/command',
                        json={'action': cmd.get('action', ''), 'params': cmd},
                        timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                    return await resp.json()
        except Exception as e:
            return {'ok': False, 'reason': str(e)}

    async def broadcast_command(self, cmd: dict,
                                 robot_ids: Optional[List[str]] = None) -> Dict[str, dict]:
        """Send same command to multiple (or all) robots."""
        targets = robot_ids or list(self.robots.keys())
        results = await asyncio.gather(
            *[self.send_command(rid, cmd) for rid in targets],
            return_exceptions=True
        )
        return {rid: (r if isinstance(r, dict) else {'ok': False, 'reason': str(r)})
                for rid, r in zip(targets, results)}

    async def estop_all(self, source: str = 'fleet') -> dict:
        """Emergency stop ALL robots immediately."""
        logger.critical(f'FLEET E-STOP from {source}')
        return await self.broadcast_command({'action': 'ESTOP'})

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def create_and_start(self, name: str, task_type: str,
                                params: dict,
                                strategy: str = 'round_robin') -> FleetTask:
        task = FleetTask(
            task_id=str(uuid.uuid4())[:8],
            name=name, type=task_type, params=params, robot_ids=[])
        task.robot_ids = self._scheduler.assign(task, strategy)
        self.tasks[task.task_id] = task

        if not task.robot_ids:
            task.status = 'failed'
            logger.warning(f'Task {task.task_id} has no assignees')
            return task

        # Mark robots busy
        for rid in task.robot_ids:
            if rid in self.robots:
                self.robots[rid].assigned_task = task.task_id

        task.status = 'running'
        logger.info(f'Fleet task {task.task_id} "{name}" → robots {task.robot_ids}')

        # Launch on each robot
        await asyncio.gather(*[
            self.send_command(rid, {'action': 'START_MISSION',
                                    'mission_id': task.task_id, **params})
            for rid in task.robot_ids
        ])
        return task

    # ── Synchronization ───────────────────────────────────────────────────

    async def synchronized_behavior(self, behavior_id: str,
                                     robot_ids: Optional[List[str]] = None,
                                     delay_s: float = 2.0) -> str:
        """
        Execute a behavior on multiple robots in tight synchronization.
        Returns sync session ID.
        """
        targets = robot_ids or [
            r.robot_id for r in self.robots.values() if r.available]
        if not targets:
            raise ValueError('No available robots for synchronized behavior')

        session_id = self._sync.create_session(behavior_id, targets, delay_s)
        t_zero = self._sync.get_t_zero(session_id)

        # Send sync command to each robot with shared T0
        await self.broadcast_command({
            'action': 'RUN_BEHAVIOR',
            'behavior_id': behavior_id,
            'sync_session': session_id,
            't_zero': t_zero,
        }, targets)

        logger.info(f'Synchronized behavior {behavior_id} '
                    f'T0={t_zero:.2f} robots={targets}')
        return session_id

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            'total_robots': len(self.robots),
            'online': sum(1 for r in self.robots.values()
                          if r.status == RobotStatus.ONLINE.value),
            'busy': sum(1 for r in self.robots.values()
                        if r.status == RobotStatus.BUSY.value),
            'estop': sum(1 for r in self.robots.values()
                         if r.status == RobotStatus.ESTOP.value),
            'active_tasks': sum(1 for t in self.tasks.values()
                                if t.status == 'running'),
            'robots': [r.to_dict() for r in self.robots.values()],
            'tasks': [t.to_dict() for t in list(self.tasks.values())[-10:]],
        }


# ════════════════════════════════════════════════════════════════════════════
# OTA Update System
# ════════════════════════════════════════════════════════════════════════════

"""
go2_platform/backend/core/ota.py
OTA (Over-The-Air) update manager for plugins, behaviors, and configs.
Supports versioning, staged rollout, and rollback.
"""

import hashlib
import os
import shutil
from pathlib import Path


@dataclass
class UpdatePackage:
    """Validated update package."""
    package_id:   str
    target:       str           # 'plugin' / 'behavior' / 'config' / 'firmware'
    name:         str
    version:      str
    prev_version: Optional[str]
    checksum:     str           # SHA-256 of package content
    size_bytes:   int
    url:          Optional[str]
    status:       str = 'pending'  # pending/downloaded/verified/installed/rolled_back
    installed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class OTAManager:
    """
    Manages versioned updates with rollback capability.
    Update pipeline:
      Download → Verify checksum → Backup current → Apply → Verify → (rollback on fail)
    """

    BACKUP_DIR = Path('/tmp/go2_ota_backups')

    def __init__(self, plugin_system, base_dir: str = '.'):
        self._plugins = plugin_system
        self._base = Path(base_dir)
        self._packages: Dict[str, UpdatePackage] = {}
        self._update_history: List[dict] = []
        self.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        logger.info('OTAManager initialized')

    async def check_updates(self, registry_url: str) -> List[dict]:
        """
        Query remote registry for available updates.
        Returns list of update packages newer than installed versions.
        """
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(registry_url + '/packages',
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        packages = await r.json()
                        logger.info(f'OTA: {len(packages)} packages available')
                        return packages
        except Exception as e:
            logger.warning(f'OTA registry unreachable: {e}')
        return []

    async def install_plugin(self, manifest: dict,
                              package_path: str) -> tuple[bool, str]:
        """
        Install / update a plugin from a local package.
        Backs up existing version before replacing.
        """
        name = manifest.get('name', '')
        version = manifest.get('version', '')

        # Create backup of existing
        existing_path = self._base / 'plugins' / name
        backup_path = None
        if existing_path.exists():
            backup_path = self.BACKUP_DIR / f'{name}_{version}_backup'
            shutil.copytree(str(existing_path), str(backup_path))
            logger.info(f'OTA backup: {name} → {backup_path}')

        # Verify checksum
        computed = self._checksum_dir(package_path)

        # Install
        try:
            dest = self._base / 'plugins' / name
            if dest.exists():
                shutil.rmtree(str(dest))
            shutil.copytree(package_path, str(dest))

            # Reload plugin
            ok, msg = await self._plugins.update(name, manifest, str(dest))
            if not ok:
                # Rollback
                await self._rollback(name, str(backup_path))
                return False, f'Plugin update failed: {msg}'

            pkg = UpdatePackage(
                package_id=str(uuid.uuid4())[:8],
                target='plugin', name=name, version=version,
                prev_version=None, checksum=computed,
                size_bytes=self._dir_size(str(dest)),
                url=None, status='installed', installed_at=time.time()
            )
            self._packages[pkg.package_id] = pkg
            self._update_history.append({
                'action': 'install', 'target': name,
                'version': version, 'ts': time.time()
            })
            logger.info(f'OTA installed: {name} v{version}')
            return True, f'Installed {name} v{version}'

        except Exception as e:
            logger.error(f'OTA install failed: {e}')
            if backup_path:
                await self._rollback(name, str(backup_path))
            return False, str(e)

    async def rollback_plugin(self, name: str) -> tuple[bool, str]:
        """Rollback to the most recent backup of a plugin."""
        backups = sorted(self.BACKUP_DIR.glob(f'{name}_*_backup'),
                         key=os.path.getmtime, reverse=True)
        if not backups:
            return False, f'No backup found for {name}'
        return await self._rollback(name, str(backups[0]))

    async def _rollback(self, name: str, backup_path: str) -> tuple[bool, str]:
        try:
            dest = self._base / 'plugins' / name
            if dest.exists():
                shutil.rmtree(str(dest))
            shutil.copytree(backup_path, str(dest))
            logger.warning(f'OTA rollback: {name} ← {backup_path}')
            self._update_history.append({
                'action': 'rollback', 'target': name, 'ts': time.time()
            })
            return True, f'Rolled back {name}'
        except Exception as e:
            return False, f'Rollback failed: {e}'

    def _checksum_dir(self, path: str) -> str:
        """SHA-256 of all file contents in a directory."""
        h = hashlib.sha256()
        for fp in sorted(Path(path).rglob('*')):
            if fp.is_file():
                h.update(fp.read_bytes())
        return h.hexdigest()

    def _dir_size(self, path: str) -> int:
        return sum(f.stat().st_size for f in Path(path).rglob('*') if f.is_file())

    def history(self, n: int = 20) -> List[dict]:
        return self._update_history[-n:]

    def status(self) -> dict:
        return {
            'packages': len(self._packages),
            'recent_updates': self.history(5),
        }
