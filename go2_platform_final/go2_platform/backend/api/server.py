"""
go2_platform/backend/api/server.py  v2.0
FastAPI server — all endpoints including i18n, animation, BT, observability.
E-STOP never requires auth. All other mutations require Bearer token if GO2_API_TOKEN is set.
"""
import asyncio, json, logging, os, sys, time, uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError: pass

try:
    from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                          HTTPException, Depends, Security, status, Query)
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import PlainTextResponse, JSONResponse
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
    from pydantic import BaseModel, Field, field_validator
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    class BaseModel: pass
    class FastAPI: pass

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path: sys.path.insert(0, _ROOT)

from core.platform import WorldObject, Zone, BehaviorPolicy
from core.plugin_system import PluginSystem
from core.security import SecurityManager

log = logging.getLogger('go2.api')
logging.basicConfig(
    level=os.getenv('GO2_LOG_LEVEL','info').upper(),
    format='%(asctime)s %(name)s %(levelname)s: %(message)s',
)

_API_TOKEN = os.getenv('GO2_API_TOKEN','').strip()
_ORIGINS   = [o.strip() for o in os.getenv('GO2_ALLOWED_ORIGINS','*').split(',')]
_MODE      = os.getenv('GO2_MODE','simulation')
_DOCS      = '/docs' if _MODE != 'hardware' else None


# ── Pydantic models ────────────────────────────────────────────────────────

class CommandRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    params: Dict[str,Any] = Field(default_factory=dict)
    source: str = Field(default='api', max_length=32)
    @field_validator('action')
    @classmethod
    def safe_action(cls,v):
        v=v.strip().upper()
        if not all(c.isalnum() or c=='_' for c in v):
            raise ValueError('alphanumeric+underscore only')
        return v

class ObjectRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    type: str
    affordances: List[str] = Field(default_factory=list)
    moods:       List[str] = Field(default_factory=list)
    max_force_n: float = Field(default=20.0, gt=0, le=100)
    pos: Dict[str,float] = Field(default_factory=lambda:{'x':0,'y':0,'z':0.4})
    contact_normal: List[float] = Field(default_factory=lambda:[0,0,1])
    notes: str = Field(default='', max_length=512)
    linked_behavior: Optional[str] = None
    schema_ver: str = '2.0.0'
    @field_validator('type')
    @classmethod
    def valid_type(cls,v):
        allowed={'soft_prop','hard_prop','medium_prop','interactive','funscript_prop','waypoint','zone'}
        if v not in allowed: raise ValueError(f'type must be one of {sorted(allowed)}')
        return v
    @field_validator('affordances','moods',mode='before')
    @classmethod
    def cap_tags(cls,v): return [str(s)[:64] for s in (v or []) if isinstance(s,str)][:32]

class MissionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    type: str
    params: Dict[str,Any] = Field(default_factory=dict)
    @field_validator('type')
    @classmethod
    def valid_type(cls,v):
        allowed={'patrol','follow','inspect','sequence','conditional'}
        if v not in allowed: raise ValueError(f'must be one of {sorted(allowed)}')
        return v

class SafetyConfigRequest(BaseModel):
    pitch_limit_deg: Optional[float] = Field(None, ge=5, le=30)
    roll_limit_deg:  Optional[float] = Field(None, ge=5, le=30)
    force_limit_n:   Optional[float] = Field(None, ge=5, le=80)
    temp_limit_c:    Optional[float] = Field(None, ge=50, le=90)
    battery_min_pct: Optional[float] = Field(None, ge=5, le=25)
    watchdog_s:      Optional[float] = Field(None, ge=0.1, le=10)

class PolicyRequest(BaseModel):
    policy: str
    @field_validator('policy')
    @classmethod
    def valid_policy(cls,v):
        valid={p.name for p in BehaviorPolicy}
        if v.upper() not in valid: raise ValueError(f'must be one of {sorted(valid)}')
        return v.upper()

class AnimationLoadRequest(BaseModel):
    clip_id: str = Field(..., min_length=1, max_length=64)
    name:    str = Field(default='', max_length=128)
    format:  Optional[str] = Field(None)
    data:    Any = Field(...)   # JSON/string animation data

class AnimationPlayRequest(BaseModel):
    clip_id: Optional[str] = None
    speed:   float = Field(default=1.0, ge=0.1, le=10.0)
    loop:    Optional[bool] = None


# ── Auth & rate limit ──────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

def require_auth(creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer)):
    if not _API_TOKEN: return
    if not creds or creds.credentials != _API_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail='Invalid or missing Bearer token',
                            headers={'WWW-Authenticate':'Bearer'})

class _Lim:
    def __init__(self, rps=30):
        self._b: Dict[str,list]={}; self.rps=rps
    def check(self, key, burst=None):
        limit=burst or self.rps; now=time.monotonic()
        calls=[t for t in self._b.get(key,[]) if now-t<1.0]
        if len(calls)>=limit: return False
        calls.append(now); self._b[key]=calls; return True
_lim = _Lim(30)


# ── App factory ────────────────────────────────────────────────────────────

def create_app(runtime=None):
    """
    Factory. Pass a PlatformRuntime for testing; None creates a fresh one.
    Usage: uvicorn backend.api.server:create_app --factory
    """
    if runtime is None:
        from core.integration import PlatformRuntime
        runtime = PlatformRuntime()

    _p   = runtime.platform
    _ps  = runtime.plugins
    _sec = SecurityManager()
    _i18n = runtime.i18n
    _anim = runtime.anim_player
    _areg = runtime.anim_registry
    _bt   = runtime.bt_runner
    _met  = runtime.metrics
    _hlt  = runtime.health
    _trc  = runtime.tracer

    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        _hlt.register_platform_checks(_p)
        log.info('Go2 API v2.0 — mode=%s auth=%s docs=%s',
                 _MODE, 'on' if _API_TOKEN else 'off(dev)', _DOCS or 'off')
        yield
        await runtime.stop()

    app = FastAPI(title='Go2 Platform API', version='2.0.0',
                  lifespan=lifespan, docs_url=_DOCS,
                  redoc_url='/redoc' if _DOCS else None)

    if _ORIGINS==['*']: log.warning('CORS wildcard active — restrict in production')
    app.add_middleware(CORSMiddleware, allow_origins=_ORIGINS,
                       allow_credentials=(_ORIGINS!=['*']),
                       allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
                       allow_headers=['Authorization','Content-Type','Accept-Language'])

    def P(): return _p
    def PS(): return _ps
    def SEC(): return _sec

    # ── System ────────────────────────────────────────────────────────────
    @app.get('/health')
    async def health():
        return {'status':'ok','version':'2.0.0','mode':_MODE,'ts':time.time()}

    @app.get('/api/v1/status')
    async def full_status():
        return runtime.full_status()

    @app.get('/api/v1/telemetry')
    async def telemetry(p=Depends(P)):
        return p.telemetry.to_dict()

    @app.get('/api/v1/fsm')
    async def fsm(p=Depends(P)):
        return p.fsm.status()

    @app.get('/api/v1/safety')
    async def safety(p=Depends(P)):
        return p.safety.status()

    @app.get('/api/v1/events')
    async def events(n:int=50, p=Depends(P)):
        return {'events':p.bus.recent(min(n,200))}

    # ── E-STOP: NEVER requires auth ───────────────────────────────────────
    @app.post('/api/v1/estop', tags=['safety'])
    async def estop(p=Depends(P)):
        """Emergency stop — no auth, no rate limit, always responds."""
        await p.execute_command({'action':'ESTOP'}, source='api')
        _met.counter('go2_estop_total').inc()
        return {'ok':True,'action':'ESTOP','ts':time.time()}

    # ── Auth-protected control ────────────────────────────────────────────
    @app.post('/api/v1/estop/clear')
    async def clear_estop(p=Depends(P), _=Depends(require_auth)):
        return await p.execute_command({'action':'CLEAR_ESTOP'})

    @app.post('/api/v1/arm')
    async def arm(p=Depends(P), _=Depends(require_auth)):
        return await p.execute_command({'action':'ARM'}, source='api')

    @app.post('/api/v1/disarm')
    async def disarm(p=Depends(P), _=Depends(require_auth)):
        return await p.execute_command({'action':'DISARM'}, source='api')

    @app.post('/api/v1/command')
    async def command(req:CommandRequest, p=Depends(P), sec=Depends(SEC), _=Depends(require_auth)):
        if not _lim.check('cmd'): raise HTTPException(429,'Rate limit exceeded')
        t0 = time.monotonic()
        clean, valid, reason = sec.validate_command(
            {'action':req.action,**req.params}, client_id='api', armed=p.fsm.armed)
        if not valid: raise HTTPException(422, detail={'error':reason})
        result = await p.execute_command(clean, source=req.source)
        _met.histogram('go2_command_latency_s').observe(time.monotonic()-t0)
        _met.counter('go2_commands_total').inc()
        if result.get('ok') is False: raise HTTPException(422, detail=result)
        return result

    # ── Safety config ──────────────────────────────────────────────────────
    @app.patch('/api/v1/safety/config')
    async def safety_config(req:SafetyConfigRequest, p=Depends(P), _=Depends(require_auth)):
        cfg=p.safety.cfg; updates={}
        for k,v in req.model_dump(exclude_none=True).items():
            setattr(cfg,k,v); updates[k]=v
        await p.bus.emit('safety.config_updated', updates, 'api')
        return {'ok':True,'updated':updates,'config':p.safety.status()['cfg']}

    # ── Objects ────────────────────────────────────────────────────────────
    @app.get('/api/v1/objects')
    async def list_objects(p=Depends(P)):
        return {'objects':[o.to_dict() for o in p.world.objects.values()],
                'count':len(p.world.objects)}

    @app.get('/api/v1/objects/{obj_id}')
    async def get_object(obj_id:str, p=Depends(P)):
        obj=p.world.get_object(obj_id)
        if not obj: raise HTTPException(404,f'Object not found: {obj_id!r}')
        return obj.to_dict()

    @app.post('/api/v1/objects', status_code=201)
    async def create_object(req:ObjectRequest, p=Depends(P), sec=Depends(SEC), _=Depends(require_auth)):
        valid_objs,errors=sec.validate_import({'objects':[req.model_dump()]},'api')
        if not valid_objs: raise HTTPException(422,detail={'errors':errors})
        obj=WorldObject(id=req.id,name=req.name,type=req.type,affordances=req.affordances,
                        moods=req.moods,max_force_n=req.max_force_n,pos=req.pos,
                        contact_normal=req.contact_normal,notes=req.notes,
                        linked_behavior=req.linked_behavior,schema_ver=req.schema_ver)
        ok,msg=p.world.add_object(obj)
        if not ok: raise HTTPException(422,detail=msg)
        await p.bus.emit('world.object_added',obj.to_dict(),'api')
        return {'ok':True,'id':req.id}

    @app.put('/api/v1/objects/{obj_id}')
    async def update_object(obj_id:str,req:ObjectRequest,p=Depends(P),_=Depends(require_auth)):
        if obj_id not in p.world.objects: raise HTTPException(404,f'Not found: {obj_id!r}')
        obj=WorldObject(id=obj_id,name=req.name,type=req.type,affordances=req.affordances,
                        moods=req.moods,max_force_n=req.max_force_n,pos=req.pos,
                        contact_normal=req.contact_normal,notes=req.notes,
                        linked_behavior=req.linked_behavior,schema_ver=req.schema_ver)
        p.world.objects[obj_id]=obj
        return {'ok':True,'id':obj_id}

    @app.delete('/api/v1/objects/{obj_id}')
    async def delete_object(obj_id:str,p=Depends(P),_=Depends(require_auth)):
        if not p.world.remove_object(obj_id): raise HTTPException(404,f'Not found: {obj_id!r}')
        return {'ok':True}

    @app.get('/api/v1/world/export')
    async def export_world(p=Depends(P)):
        return p.world.export()

    @app.post('/api/v1/world/import')
    async def import_world(data:Dict[str,Any],p=Depends(P),sec=Depends(SEC),_=Depends(require_auth)):
        if not _lim.check('import',burst=3): raise HTTPException(429,'Import rate limited')
        valid_objs,errors=sec.validate_import(data,'api')
        added,errs2=p.world.import_from_dict({'objects':valid_objs})
        return {'ok':True,'added':added,'validation_errors':errors,'import_errors':errs2}

    # ── Zones ──────────────────────────────────────────────────────────────
    @app.get('/api/v1/zones')
    async def list_zones(p=Depends(P)):
        return {'zones':[z.to_dict() for z in p.world.zones.values()]}

    @app.get('/api/v1/waypoints')
    async def list_waypoints(p=Depends(P)):
        return {'waypoints':list(p.world.waypoints.values())}

    # ── Behaviors ──────────────────────────────────────────────────────────
    @app.get('/api/v1/behaviors')
    async def list_behaviors(p=Depends(P)):
        return {'behaviors':p.behaviors.all(),
                'categories':p.behaviors.list_by_category(),
                'active_policy':p.behaviors.active_policy.value}

    @app.post('/api/v1/behaviors/{behavior_id}/run')
    async def run_behavior(behavior_id:str,p=Depends(P),_=Depends(require_auth)):
        b=p.behaviors.get(behavior_id)
        if not b: raise HTTPException(404,f'Behavior not found: {behavior_id!r}')
        result=await p.execute_command({'action':'RUN_BEHAVIOR','behavior_id':behavior_id},'api')
        # Also trigger animation if registered
        await _anim.__class__  # no-op; anim_state_machine handles via events
        if result.get('ok') is False: raise HTTPException(422,detail=result)
        return result

    @app.post('/api/v1/behaviors/policy')
    async def set_policy(req:PolicyRequest,p=Depends(P),_=Depends(require_auth)):
        return await p.execute_command({'action':'SET_POLICY','policy':req.policy},'api')

    # ── Missions ───────────────────────────────────────────────────────────
    @app.get('/api/v1/missions')
    async def list_missions(p=Depends(P)):
        return {'missions':p.missions.list(),'active':p.missions.active_mission}

    @app.post('/api/v1/missions', status_code=201)
    async def create_mission(req:MissionRequest,p=Depends(P),_=Depends(require_auth)):
        m=p.missions.create(req.name,req.type,req.params)
        return {'ok':True,'mission':m.to_dict()}

    @app.post('/api/v1/missions/{mission_id}/start')
    async def start_mission(mission_id:str,p=Depends(P),_=Depends(require_auth)):
        ok,msg=await p.missions.start(mission_id)
        if not ok: raise HTTPException(422,detail=msg)
        return {'ok':True}

    @app.post('/api/v1/missions/stop')
    async def stop_mission(p=Depends(P),_=Depends(require_auth)):
        return {'ok':await p.missions.stop('api')}

    @app.get('/api/v1/missions/{mission_id}')
    async def get_mission(mission_id:str,p=Depends(P)):
        m=p.missions.missions.get(mission_id)
        if not m: raise HTTPException(404,f'Mission {mission_id!r} not found')
        return m.to_dict()

    # ── i18n ───────────────────────────────────────────────────────────────
    @app.get('/api/v1/i18n/locales', tags=['i18n'])
    async def i18n_locales():
        return {'locales':_i18n.available_locales(),'current':_i18n.locale}

    @app.get('/api/v1/i18n/locale', tags=['i18n'])
    async def i18n_current():
        return {'locale':_i18n.locale,'name':_i18n.locale_name,
                'flag':_i18n.locale_flag,'rtl':_i18n.is_rtl}

    @app.post('/api/v1/i18n/locale/{code}', tags=['i18n'])
    async def i18n_set(code:str, p=Depends(P)):
        ok=_i18n.set_locale(code)
        if not ok: raise HTTPException(400,detail={
            'error':'unsupported_locale',
            'supported':[l['code'] for l in _i18n.available_locales()]})
        await p.bus.emit('i18n.locale_changed',{'locale':code,'name':_i18n.locale_name},'i18n')
        return {'ok':True,'locale':code,'name':_i18n.locale_name}

    @app.get('/api/v1/i18n/translations/{code}', tags=['i18n'])
    async def i18n_translations(code:str):
        pack=_i18n.export_locale(code)
        if not pack: raise HTTPException(404,f'Locale {code!r} not found')
        return pack

    @app.get('/api/v1/i18n/coverage', tags=['i18n'])
    async def i18n_coverage():
        return _i18n.coverage_report()

    @app.get('/api/v1/i18n/translate', tags=['i18n'])
    async def i18n_translate(
        key: str = Query(...), locale: Optional[str] = Query(None)):
        return {'key':key,'locale':locale or _i18n.locale,'text':_i18n.translate(key,locale)}

    # ── Animation ──────────────────────────────────────────────────────────
    @app.get('/api/v1/animations', tags=['animation'])
    async def list_animations():
        return {'animations':_areg.list(),'count':len(_areg.list())}

    @app.get('/api/v1/animations/player', tags=['animation'])
    async def player_status():
        return _anim.status()

    @app.post('/api/v1/animations/load', tags=['animation'])
    async def load_animation(req:AnimationLoadRequest, _=Depends(require_auth)):
        try:
            clip=_areg.load_from_data(req.data,req.clip_id,req.name or req.clip_id,req.format)
            return {'ok':True,'clip':clip.to_dict()}
        except Exception as e:
            raise HTTPException(422,detail=str(e))

    @app.post('/api/v1/animations/{clip_id}/play', tags=['animation'])
    async def play_animation(clip_id:str, req:AnimationPlayRequest=AnimationPlayRequest(),
                              _=Depends(require_auth)):
        clip=_areg.get(clip_id)
        if not clip: raise HTTPException(404,f'Clip {clip_id!r} not found')
        if req.loop is not None: clip.loop=req.loop
        await _anim.load(clip)
        await _anim.play(speed=req.speed)
        return {'ok':True,'clip':clip_id,'speed':req.speed}

    @app.post('/api/v1/animations/pause', tags=['animation'])
    async def pause_animation(_=Depends(require_auth)):
        await _anim.pause(); return {'ok':True}

    @app.post('/api/v1/animations/stop', tags=['animation'])
    async def stop_animation(_=Depends(require_auth)):
        await _anim.stop(); return {'ok':True}

    @app.delete('/api/v1/animations/{clip_id}', tags=['animation'])
    async def remove_animation(clip_id:str, _=Depends(require_auth)):
        ok=_areg.remove(clip_id)
        if not ok: raise HTTPException(404,f'Clip {clip_id!r} not found')
        return {'ok':True}

    @app.get('/api/v1/animations/{clip_id}/export/funscript', tags=['animation'])
    async def export_funscript(clip_id:str):
        from animation.animation_system import AnimationLoader
        clip=_areg.get(clip_id)
        if not clip: raise HTTPException(404,f'Clip {clip_id!r} not found')
        return AnimationLoader.to_funscript(clip)

    @app.get('/api/v1/animations/{clip_id}/export/json', tags=['animation'])
    async def export_anim_json(clip_id:str):
        from animation.animation_system import AnimationLoader
        clip=_areg.get(clip_id)
        if not clip: raise HTTPException(404,f'Clip {clip_id!r} not found')
        return AnimationLoader.to_go2_json(clip)

    # ── Behavior tree ──────────────────────────────────────────────────────
    @app.get('/api/v1/bt/status', tags=['behavior_tree'])
    async def bt_status():
        return _bt.status_dict()

    @app.get('/api/v1/bt/blackboard', tags=['behavior_tree'])
    async def bt_blackboard(_=Depends(require_auth)):
        return _bt.blackboard.snapshot()

    @app.post('/api/v1/bt/blackboard', tags=['behavior_tree'])
    async def bt_set_blackboard(data:Dict[str,Any], _=Depends(require_auth)):
        for k,v in data.items():
            if not k.startswith('_'):  # protect internal keys
                _bt.blackboard.set(k, v)
        return {'ok':True,'set':list(data.keys())}

    @app.get('/api/v1/bt/debug', tags=['behavior_tree'])
    async def bt_debug():
        return {'tree': _bt.debug_tree()}

    @app.post('/api/v1/bt/start', tags=['behavior_tree'])
    async def bt_start(_=Depends(require_auth)):
        if not _bt._active: await _bt.start()
        return {'ok':True}

    @app.post('/api/v1/bt/stop', tags=['behavior_tree'])
    async def bt_stop(_=Depends(require_auth)):
        await _bt.stop(); return {'ok':True}

    # ── Plugins ────────────────────────────────────────────────────────────
    @app.get('/api/v1/plugins')
    async def list_plugins(ps=Depends(PS)):
        return {'plugins':ps.list(),'panels':ps.panels(),'routes':ps.routes()}

    @app.post('/api/v1/plugins/{name}/activate')
    async def activate_plugin(name:str,ps=Depends(PS),_=Depends(require_auth)):
        ok,msg=await ps.activate(name)
        if not ok: raise HTTPException(422,detail=msg)
        return {'ok':True,'name':name}

    @app.post('/api/v1/plugins/{name}/deactivate')
    async def deactivate_plugin(name:str,ps=Depends(PS),_=Depends(require_auth)):
        return {'ok':await ps.deactivate(name)}

    @app.delete('/api/v1/plugins/{name}')
    async def unload_plugin(name:str,ps=Depends(PS),_=Depends(require_auth)):
        return {'ok':await ps.unload(name)}

    # ── Observability ──────────────────────────────────────────────────────
    @app.get('/api/v1/metrics', tags=['observability'], response_class=PlainTextResponse)
    async def metrics_prom():
        return PlainTextResponse(_met.prometheus_text(),
                                  media_type='text/plain; version=0.0.4; charset=utf-8')

    @app.get('/api/v1/metrics/json', tags=['observability'])
    async def metrics_json():
        return _met.to_dict()

    @app.get('/api/v1/health', tags=['observability'])
    async def health_agg():
        result=await _hlt.aggregate()
        code=200 if result['status']=='ok' else 503
        return JSONResponse(result,status_code=code)

    @app.get('/api/v1/health/live', tags=['observability'])
    async def live(): return {'status':'ok','ts':time.time()}

    @app.get('/api/v1/health/ready', tags=['observability'])
    async def ready():
        result=await _hlt.aggregate()
        code=200 if result['status']!='down' else 503
        return JSONResponse({'ready':code==200,**result},status_code=code)

    @app.get('/api/v1/traces', tags=['observability'])
    async def traces(n:int=20, _=Depends(require_auth)):
        return {'traces':_trc.recent_traces(min(n,100)),'active':_trc.active_spans()}

    # ── Security/audit ─────────────────────────────────────────────────────
    @app.get('/api/v1/security/audit')
    async def audit(n:int=50, sec=Depends(SEC), _=Depends(require_auth)):
        return {'entries':sec.audit.recent(min(n,200)),
                'chain_valid':sec.audit.verify_chain()}

    @app.get('/api/v1/security/status')
    async def sec_status(sec=Depends(SEC), _=Depends(require_auth)):
        return sec.status()

    # ── WebSocket hub ──────────────────────────────────────────────────────
    @app.websocket('/ws')
    async def ws_hub(ws:WebSocket, p=Depends(P)):
        await ws.accept()
        cid = str(uuid.uuid4())[:8]
        if _API_TOKEN:
            token=ws.query_params.get('token','')
            if token!=_API_TOKEN:
                await ws.send_text(json.dumps({'type':'error','data':{'code':'unauthorized'}}))
                await ws.close(code=4001); return

        p.register_ws(ws)
        _met.gauge('go2_ws_clients').inc()
        _met.counter('go2_ws_connections_total').inc()
        try:
            await ws.send_text(json.dumps({'type':'init','client_id':cid,
                'data':runtime.full_status()}))
        except Exception:
            p.unregister_ws(ws); _met.gauge('go2_ws_clients').dec(); return

        try:
            while True:
                try:
                    raw=await asyncio.wait_for(ws.receive_text(),timeout=30.0)
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({'type':'ping','ts':time.time()})); continue

                if not _lim.check(f'ws:{cid}'):
                    await ws.send_text(json.dumps({'type':'error','data':{'code':'rate_limited'}}))
                    continue
                try: msg=json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({'type':'error','data':{'code':'invalid_json'}}))
                    continue

                t=msg.get('type','')
                if t=='ping':
                    await ws.send_text(json.dumps({'type':'pong','ts':time.time()}))
                elif t=='command':
                    result=await p.execute_command(msg.get('data',{}),source=f'ws:{cid}')
                    await ws.send_text(json.dumps({'type':'cmd_result','data':result}))
                elif t=='locale':
                    code=msg.get('data',{}).get('locale','en')
                    ok=_i18n.set_locale(code)
                    await ws.send_text(json.dumps({'type':'locale_changed',
                        'data':{'ok':ok,'locale':_i18n.locale,'name':_i18n.locale_name}}))
                elif t=='subscribe':
                    await ws.send_text(json.dumps({'type':'subscribed','topics':msg.get('topics',[])}))
                else:
                    await ws.send_text(json.dumps({'type':'error','data':{'code':'unknown_type','received':t}}))
        except WebSocketDisconnect:
            log.info('WS disconnected: %s', cid)
        except Exception as e:
            log.error('WS error %s: %s', cid, e)
        finally:
            p.unregister_ws(ws)
            _met.gauge('go2_ws_clients').dec()

    return app


def main():
    if not HAS_FASTAPI: print('pip install fastapi uvicorn[standard]'); return
    import uvicorn
    uvicorn.run(create_app(), host=os.getenv('GO2_HOST','0.0.0.0'),
                port=int(os.getenv('GO2_PORT','8080')),
                log_level=os.getenv('GO2_LOG_LEVEL','info'))

if __name__=='__main__': main()
