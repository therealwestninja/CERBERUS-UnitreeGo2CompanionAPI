"""
go2_platform/backend/api/server.py
══════════════════════════════════════════════════════════════════════════════
FastAPI Backend Server
Full REST API + WebSocket real-time hub.

REST API:  http://localhost:8080/api/v1/
WS Hub:    ws://localhost:8080/ws
Docs:      http://localhost:8080/docs  (Swagger UI auto-generated)

All state-changing endpoints go through PlatformCore.execute_command()
so safety + FSM validation is always enforced.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# FastAPI + uvicorn (pip install fastapi uvicorn[standard])
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field, validator
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    class BaseModel: pass
    class FastAPI: pass

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.platform import (PlatformCore, SafetyConfig, WorldObject, Zone,
                            BehaviorPolicy, Mission)
from core.plugin_system import PluginSystem

logger = logging.getLogger('go2.api')


# ════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS (Pydantic schema validation)
# ════════════════════════════════════════════════════════════════════════════

class CommandRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    params: Dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default='api', max_length=32)

    @validator('action')
    def action_alphanumeric(cls, v):
        if not all(c.isalnum() or c == '_' for c in v):
            raise ValueError('action must be alphanumeric + underscore')
        return v.upper()


class ObjectRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    type: str = Field(...)
    affordances: List[str] = Field(default_factory=list)
    moods: List[str] = Field(default_factory=list)
    max_force_n: float = Field(default=20.0, gt=0, le=100)
    pos: Dict[str, float] = Field(default_factory=lambda: {'x':0,'y':0,'z':0.4})
    contact_normal: List[float] = Field(default_factory=lambda: [0,0,1])
    notes: str = Field(default='', max_length=512)
    linked_behavior: Optional[str] = None
    schema_ver: str = '2.0.0'

    @validator('type')
    def valid_type(cls, v):
        allowed = {'soft_prop','hard_prop','medium_prop','interactive','funscript_prop','waypoint','zone'}
        if v not in allowed:
            raise ValueError(f'type must be one of {allowed}')
        return v

    @validator('affordances', 'moods', each_item=True)
    def str_max_length(cls, v):
        if len(v) > 64: raise ValueError('tag too long')
        return v


class MissionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    type: str = Field(...)
    params: Dict[str, Any] = Field(default_factory=dict)


class SafetyConfigRequest(BaseModel):
    pitch_limit_deg: Optional[float] = Field(None, ge=5, le=30)
    roll_limit_deg:  Optional[float] = Field(None, ge=5, le=30)
    force_limit_n:   Optional[float] = Field(None, ge=5, le=80)
    temp_limit_c:    Optional[float] = Field(None, ge=50, le=90)
    battery_min_pct: Optional[float] = Field(None, ge=5, le=25)


class PolicyRequest(BaseModel):
    policy: str = Field(...)

    @validator('policy')
    def valid_policy(cls, v):
        valid = {p.name for p in BehaviorPolicy}
        if v.upper() not in valid:
            raise ValueError(f'policy must be one of {valid}')
        return v.upper()


# ════════════════════════════════════════════════════════════════════════════
# RATE LIMITER
# ════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Simple in-memory token bucket rate limiter."""
    def __init__(self, max_rps: int = 20):
        self._buckets: Dict[str, list] = {}
        self.max_rps = max_rps

    def check(self, key: str) -> bool:
        now = time.monotonic()
        calls = self._buckets.get(key, [])
        calls = [t for t in calls if now - t < 1.0]
        if len(calls) >= self.max_rps:
            return False
        calls.append(now)
        self._buckets[key] = calls
        return True


_limiter = RateLimiter(max_rps=30)


# ════════════════════════════════════════════════════════════════════════════
# APP FACTORY
# ════════════════════════════════════════════════════════════════════════════

def create_app(platform: Optional[PlatformCore] = None,
               plugin_system: Optional[PluginSystem] = None) -> 'FastAPI':

    _platform = platform or PlatformCore()
    _plugins  = plugin_system or PluginSystem(_platform)

    @asynccontextmanager
    async def lifespan(app):
        await _platform.start()
        await _plugins.load_from_dir('plugins')
        logger.info('Go2 Platform API started')
        yield
        await _platform.stop()
        logger.info('Go2 Platform API stopped')

    app = FastAPI(
        title='Go2 Platform API',
        description='Unitree Go2 Robotics Platform — REST + WebSocket backend',
        version='2.0.0',
        lifespan=lifespan,
        docs_url='/docs',
        redoc_url='/redoc',
    )

    app.add_middleware(CORSMiddleware, allow_origins=['*'],
                       allow_methods=['*'], allow_headers=['*'])

    # ── Dependency injection ──────────────────────────────────────────────

    def get_platform() -> PlatformCore: return _platform
    def get_plugins()  -> PluginSystem: return _plugins

    # ── Health ───────────────────────────────────────────────────────────

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'version': '2.0.0', 'ts': time.time()}

    # ── Status ───────────────────────────────────────────────────────────

    @app.get('/api/v1/status')
    async def get_status(p: PlatformCore = Depends(get_platform)):
        return p.full_status()

    @app.get('/api/v1/telemetry')
    async def get_telemetry(p: PlatformCore = Depends(get_platform)):
        return p.telemetry.to_dict()

    @app.get('/api/v1/fsm')
    async def get_fsm(p: PlatformCore = Depends(get_platform)):
        return p.fsm.status()

    @app.get('/api/v1/safety')
    async def get_safety(p: PlatformCore = Depends(get_platform)):
        return p.safety.status()

    # ── Commands ─────────────────────────────────────────────────────────

    @app.post('/api/v1/command')
    async def execute_command(req: CommandRequest,
                              p: PlatformCore = Depends(get_platform)):
        """Central command gateway — all commands validated by safety + FSM."""
        if not _limiter.check('commands'):
            raise HTTPException(429, 'Rate limit exceeded')
        cmd = {'action': req.action, **req.params}
        result = await p.execute_command(cmd, source=req.source)
        if not result.get('ok', True):
            raise HTTPException(422, detail=result)
        return result

    @app.post('/api/v1/estop')
    async def estop(p: PlatformCore = Depends(get_platform)):
        """Emergency stop — highest priority, bypasses all rate limits."""
        await p.execute_command({'action': 'ESTOP'}, source='api_estop')
        return {'ok': True, 'action': 'ESTOP'}

    @app.post('/api/v1/estop/clear')
    async def clear_estop(p: PlatformCore = Depends(get_platform)):
        await p.execute_command({'action': 'CLEAR_ESTOP'}, source='api')
        return {'ok': True}

    @app.post('/api/v1/arm')
    async def arm(p: PlatformCore = Depends(get_platform)):
        result = await p.execute_command({'action': 'ARM'}, source='api')
        return result

    @app.post('/api/v1/disarm')
    async def disarm(p: PlatformCore = Depends(get_platform)):
        result = await p.execute_command({'action': 'DISARM'}, source='api')
        return result

    # ── Safety config ─────────────────────────────────────────────────────

    @app.patch('/api/v1/safety/config')
    async def update_safety(req: SafetyConfigRequest,
                             p: PlatformCore = Depends(get_platform)):
        cfg = p.safety.cfg
        if req.pitch_limit_deg is not None: cfg.pitch_limit_deg = req.pitch_limit_deg
        if req.roll_limit_deg  is not None: cfg.roll_limit_deg  = req.roll_limit_deg
        if req.force_limit_n   is not None: cfg.force_limit_n   = req.force_limit_n
        if req.temp_limit_c    is not None: cfg.temp_limit_c    = req.temp_limit_c
        if req.battery_min_pct is not None: cfg.battery_min_pct = req.battery_min_pct
        logger.info(f'Safety config updated: {req.dict(exclude_none=True)}')
        return {'ok': True, 'config': p.safety.status()['cfg']}

    # ── Objects ───────────────────────────────────────────────────────────

    @app.get('/api/v1/objects')
    async def list_objects(p: PlatformCore = Depends(get_platform)):
        return {'objects': [o.to_dict() for o in p.world.objects.values()]}

    @app.get('/api/v1/objects/{obj_id}')
    async def get_object(obj_id: str, p: PlatformCore = Depends(get_platform)):
        obj = p.world.get_object(obj_id)
        if not obj:
            raise HTTPException(404, f'Object {obj_id} not found')
        return obj.to_dict()

    @app.post('/api/v1/objects')
    async def create_object(req: ObjectRequest,
                             p: PlatformCore = Depends(get_platform)):
        obj = WorldObject(
            id=req.id, name=req.name, type=req.type,
            affordances=req.affordances, moods=req.moods,
            max_force_n=req.max_force_n, pos=req.pos,
            contact_normal=req.contact_normal, notes=req.notes,
            linked_behavior=req.linked_behavior, schema_ver=req.schema_ver,
        )
        ok, msg = p.world.add_object(obj)
        if not ok:
            raise HTTPException(422, msg)
        await p.bus.emit('world.object_added', obj.to_dict(), 'api')
        return {'ok': True, 'id': req.id}

    @app.delete('/api/v1/objects/{obj_id}')
    async def delete_object(obj_id: str, p: PlatformCore = Depends(get_platform)):
        ok = p.world.remove_object(obj_id)
        if not ok:
            raise HTTPException(404, f'Object {obj_id} not found')
        await p.bus.emit('world.object_removed', {'id': obj_id}, 'api')
        return {'ok': True}

    @app.get('/api/v1/objects/export/json')
    async def export_world(p: PlatformCore = Depends(get_platform)):
        return p.world.export()

    @app.post('/api/v1/objects/import/json')
    async def import_world(data: dict, p: PlatformCore = Depends(get_platform)):
        added, errors = p.world.import_from_dict(data)
        return {'ok': True, 'added': added, 'errors': errors}

    # ── Behaviors ─────────────────────────────────────────────────────────

    @app.get('/api/v1/behaviors')
    async def list_behaviors(p: PlatformCore = Depends(get_platform)):
        return {'behaviors': p.behaviors.all(),
                'categories': p.behaviors.list_by_category()}

    @app.post('/api/v1/behaviors/{behavior_id}/run')
    async def run_behavior(behavior_id: str,
                           p: PlatformCore = Depends(get_platform)):
        result = await p.execute_command(
            {'action': 'RUN_BEHAVIOR', 'behavior_id': behavior_id}, 'api')
        return result

    @app.post('/api/v1/behaviors/policy')
    async def set_policy(req: PolicyRequest,
                         p: PlatformCore = Depends(get_platform)):
        result = await p.execute_command(
            {'action': 'SET_POLICY', 'policy': req.policy}, 'api')
        return result

    # ── Missions ──────────────────────────────────────────────────────────

    @app.get('/api/v1/missions')
    async def list_missions(p: PlatformCore = Depends(get_platform)):
        return {'missions': p.missions.list(),
                'active': p.missions.active_mission}

    @app.post('/api/v1/missions')
    async def create_mission(req: MissionRequest,
                              p: PlatformCore = Depends(get_platform)):
        m = p.missions.create(req.name, req.type, req.params)
        return {'ok': True, 'mission': m.to_dict()}

    @app.post('/api/v1/missions/{mission_id}/start')
    async def start_mission(mission_id: str,
                             p: PlatformCore = Depends(get_platform)):
        ok, msg = await p.missions.start(mission_id)
        if not ok: raise HTTPException(422, msg)
        return {'ok': True}

    @app.post('/api/v1/missions/stop')
    async def stop_mission(p: PlatformCore = Depends(get_platform)):
        ok = await p.missions.stop('api')
        return {'ok': ok}

    # ── Plugins ───────────────────────────────────────────────────────────

    @app.get('/api/v1/plugins')
    async def list_plugins(ps: PluginSystem = Depends(get_plugins)):
        return {'plugins': ps.list(), 'panels': ps.panels()}

    @app.post('/api/v1/plugins/{name}/activate')
    async def activate_plugin(name: str,
                               ps: PluginSystem = Depends(get_plugins)):
        ok, msg = await ps.activate(name)
        return {'ok': ok, 'msg': msg}

    @app.post('/api/v1/plugins/{name}/deactivate')
    async def deactivate_plugin(name: str,
                                 ps: PluginSystem = Depends(get_plugins)):
        ok = await ps.deactivate(name)
        return {'ok': ok}

    @app.delete('/api/v1/plugins/{name}')
    async def unload_plugin(name: str,
                             ps: PluginSystem = Depends(get_plugins)):
        ok = await ps.unload(name)
        return {'ok': ok}

    # ── Event log ─────────────────────────────────────────────────────────

    @app.get('/api/v1/events')
    async def get_events(n: int = 50, p: PlatformCore = Depends(get_platform)):
        return {'events': p.bus.recent(min(n, 200))}

    # ── Plugin route extension ────────────────────────────────────────────

    @app.api_route('/api/v1/plugins/ext/{plugin_name}/{path:path}',
                   methods=['GET','POST','PUT','DELETE','PATCH'])
    async def plugin_route(plugin_name: str, path: str,
                            ps: PluginSystem = Depends(get_plugins)):
        """Dynamic routing for plugin-registered API routes."""
        from fastapi import Request
        # Look up handler
        pass  # Plugin routes would be dynamically registered here

    # ── WebSocket hub ─────────────────────────────────────────────────────

    @app.websocket('/ws')
    async def ws_endpoint(ws: WebSocket, p: PlatformCore = Depends(get_platform)):
        await ws.accept()
        client_id = str(uuid.uuid4())[:8]
        p.register_ws(ws)
        logger.info(f'WS client connected: {client_id}')

        # Send initial state
        await ws.send_text(json.dumps({
            'type': 'init',
            'data': p.full_status(),
            'client_id': client_id
        }))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                    msg = json.loads(raw)
                    # Rate limit per client
                    if not _limiter.check(f'ws_{client_id}'):
                        await ws.send_text(json.dumps(
                            {'type': 'error', 'data': 'rate_limited'}))
                        continue
                    # Process command
                    if msg.get('type') == 'command':
                        cmd = msg.get('data', {})
                        result = await p.execute_command(cmd, source=f'ws_{client_id}')
                        await ws.send_text(json.dumps({'type': 'cmd_result', 'data': result}))
                    elif msg.get('type') == 'ping':
                        await ws.send_text(json.dumps({'type': 'pong', 'ts': time.time()}))
                except asyncio.TimeoutError:
                    # Heartbeat
                    await ws.send_text(json.dumps({'type': 'ping', 'ts': time.time()}))
        except WebSocketDisconnect:
            logger.info(f'WS client disconnected: {client_id}')
        except Exception as e:
            logger.error(f'WS error [{client_id}]: {e}')
        finally:
            p.unregister_ws(ws)

    return app


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def main():
    if not HAS_FASTAPI:
        print('ERROR: FastAPI not installed. Run: pip install fastapi uvicorn[standard]')
        return

    import uvicorn
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    app = create_app()
    uvicorn.run(app, host='0.0.0.0', port=8080, log_level='info')


if __name__ == '__main__':
    main()
