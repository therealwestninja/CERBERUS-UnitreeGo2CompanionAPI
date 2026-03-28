"""
backend/api/server.py  — CERBERUS v3.1
=======================================
FastAPI server. Endpoints:

System
  GET  /health
  GET  /api/v1/state

Motion
  POST /api/v1/move
  POST /api/v1/stop
  POST /api/v1/emergency_stop
  POST /api/v1/stand

Mode
  POST /api/v1/mode

Config
  POST /api/v1/config/height
  POST /api/v1/config/euler
  POST /api/v1/config/speed
  POST /api/v1/config/foot_raise
  POST /api/v1/config/obstacle
  POST /api/v1/vui

Behavior
  POST /api/v1/behavior
  GET  /api/v1/behaviors

NLU  (NEW v3.1)
  POST /api/v1/nlu/command

Personality
  GET  /api/v1/personality

Data / Replay  (NEW v3.1)
  GET  /api/v1/sessions
  POST /api/v1/replay

Plugins
  GET  /api/v1/plugins
  POST /api/v1/plugins/load
  POST /api/v1/plugins/unload

WebSocket
  WS   /ws/telemetry          10 Hz push + inbound commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from cerberus.behavior.engine import BehaviorEngine, Priority
from cerberus.core.cognitive import CognitiveEngine, Goal, GoalType
from cerberus.hardware.bridge import (
    AVAILABLE_MODES, ConnectionState, Go2Bridge, RobotState,
)
from cerberus.learning.data_logger import DataLogger, SessionReplayer
from cerberus.nlu.interpreter import NLUAction, interpret
from cerberus.personality.model import PersonalityModel, Traits
from cerberus.plugins.manager import (
    PluginContext, PluginManager, PluginManifest, TrustLevel,
)
from cerberus.safety.gate import SafetyConfig, SafetyGate
from cerberus.utils.logging_config import configure_logging

logger = logging.getLogger(__name__)

# ── Globals ────────────────────────────────────────────────────────────── #
_bridge:      Optional[Go2Bridge]       = None
_behavior:    Optional[BehaviorEngine]  = None
_cognitive:   Optional[CognitiveEngine] = None
_personality: Optional[PersonalityModel] = None
_plugins:     Optional[PluginManager]   = None
_data_logger: Optional[DataLogger]      = None
_ws_clients:  set[WebSocket]            = set()


def _load_config() -> dict:
    path = os.getenv("CERBERUS_CONFIG", "config/cerberus.yaml")
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Config not found at %s — using defaults", path)
        return {}


# ── Lifespan ───────────────────────────────────────────────────────────── #
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge, _behavior, _cognitive, _personality, _plugins, _data_logger

    # Configure structured logging first
    log_level = os.getenv('LOG_LEVEL', 'INFO')
    configure_logging(level=log_level)

    cfg = _load_config()
    robot_cfg  = cfg.get("robot", {})
    beh_cfg    = cfg.get("behavior", {})
    pers_cfg   = cfg.get("personality", {})
    safety_raw = cfg.get("safety", {})

    safety = SafetyGate(SafetyConfig(**safety_raw) if safety_raw else None)
    _bridge = Go2Bridge.from_config(robot_cfg)
    try:
        await _bridge.connect()
        await _bridge.start_watchdog()
    except Exception as e:
        logger.error("Bridge connect failed: %s — simulation mode", e)

    traits = Traits(**pers_cfg.get("traits", {})) if "traits" in pers_cfg else None
    _personality = PersonalityModel(traits=traits, persistence_path=pers_cfg.get("persistence_path"))

    _behavior = BehaviorEngine(_bridge, tick_rate_hz=beh_cfg.get("tick_rate_hz", 10.0))
    await _behavior.start()

    if cfg.get("cognitive", {}).get("enabled", True):
        _cognitive = CognitiveEngine(_behavior, _personality)
        await _cognitive.start()

    if cfg.get("logging", {}).get("enabled", True):
        _data_logger = DataLogger(
            logs_dir=cfg.get("logging", {}).get("logs_dir", "logs"),
            max_mb=cfg.get("logging", {}).get("max_mb", 50.0),
        )
        _data_logger.attach_bridge(_bridge)

    _plugins = PluginManager(plugins_dir=cfg.get("plugins_dir", "plugins"))
    for manifest in _plugins.discover():
        if manifest.enabled:
            ctx = PluginContext(trust_level=manifest.trust_level,
                               _bridge=_bridge, _behavior=_behavior)
            try:
                await _plugins.load(manifest, ctx)
            except Exception as e:
                logger.error("Plugin '%s' load failed: %s", manifest.name, e)

    asyncio.create_task(_broadcaster())
    yield

    # Shutdown
    if _cognitive:   await _cognitive.stop()
    if _behavior:    await _behavior.stop()
    if _plugins:     await _plugins.unload_all()
    if _bridge:      await _bridge.disconnect()
    if _personality: _personality.save()
    if _data_logger: _data_logger.close()


# ── App ────────────────────────────────────────────────────────────────── #
app = FastAPI(
    title="CERBERUS — Unitree Go2 Companion API",
    description="Canine-Emulative Responsive Behavioral Engine & Reactive Utility System",
    version="3.1.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve web UI if it exists
_ui_path = os.path.join(os.path.dirname(__file__), "..", "..", "ui")
if os.path.isdir(_ui_path):
    app.mount("/ui", StaticFiles(directory=_ui_path, html=True), name="ui")


# ── Helpers ────────────────────────────────────────────────────────────── #
def _br() -> Go2Bridge:
    if _bridge is None: raise HTTPException(503, "Bridge not ready")
    return _bridge

def _be() -> BehaviorEngine:
    if _behavior is None: raise HTTPException(503, "Behavior engine not ready")
    return _behavior

def _state_json(state: RobotState) -> str:
    d: dict = {
        "timestamp":      state.timestamp,
        "connection":     state.connection_state.value,
        "position":       {"x": state.position_x, "y": state.position_y},
        "orientation":    {"yaw": state.yaw, "pitch": state.pitch, "roll": state.roll},
        "velocity":       {"vx": state.vx, "vy": state.vy, "vyaw": state.vyaw},
        "body_height":    state.body_height,
        "battery":        {"voltage": state.battery_voltage, "percent": state.battery_percent},
        "foot_force":     state.foot_force,
        "sport_mode":     state.sport_mode_active,
        "obstacle_avoid": state.obstacle_avoidance,
        "current_mode":   state.current_mode,
        "latency_ms":     state.latency_ms,
    }
    if _personality: d["personality"] = _personality.to_dict()
    if _behavior:    d["current_behavior"] = _behavior.current_behavior
    return json.dumps(d)

async def _broadcaster() -> None:
    while True:
        await asyncio.sleep(0.1)
        if not _ws_clients or _bridge is None: continue
        try:
            state = await _bridge.get_state()
            if _personality: _personality.tick()
            if _cognitive and _bridge: _cognitive.update_state(state)
            payload = _state_json(state)
            dead = set()
            for ws in _ws_clients:
                try: await ws.send_text(payload)
                except: dead.add(ws)
            _ws_clients -= dead
        except Exception as e:
            logger.debug("Broadcaster: %s", e)


# ── Request models ─────────────────────────────────────────────────────── #
class MoveReq(BaseModel):
    vx:   float = Field(0.0, ge=-1.5, le=1.5)
    vy:   float = Field(0.0, ge=-0.8, le=0.8)
    vyaw: float = Field(0.0, ge=-2.0, le=2.0)

class StandReq(BaseModel):
    action: str = Field("up", pattern="^(up|down)$")

class ModeReq(BaseModel):
    mode: str
    @field_validator("mode")
    @classmethod
    def _v(cls, v):
        if v not in AVAILABLE_MODES:
            raise ValueError(f"Unknown mode '{v}'. Valid: {sorted(AVAILABLE_MODES)}")
        return v

class HeightReq(BaseModel):
    height: float = Field(..., ge=0.3, le=0.5)

class EulerReq(BaseModel):
    roll:  float = Field(0.0, ge=-0.75, le=0.75)
    pitch: float = Field(0.0, ge=-0.75, le=0.75)
    yaw:   float = Field(0.0, ge=-1.5,  le=1.5)

class SpeedReq(BaseModel):
    level: int = Field(..., ge=-1, le=1)

class FootReq(BaseModel):
    height: float = Field(..., ge=-0.06, le=0.03)

class ObstacleReq(BaseModel):
    enabled: bool

class VUIReq(BaseModel):
    volume:     int = Field(50, ge=0, le=100)
    brightness: int = Field(50, ge=0, le=100)

class BehaviorReq(BaseModel):
    behavior: str
    params:   dict = Field(default_factory=dict)
    priority: int  = Field(50, ge=0, le=100)

class NLUReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)
    execute: bool = Field(True, description="Execute the interpreted actions immediately")
    llm_fallback: bool = Field(False, description="Allow LLM fallback if rule match fails")

class ReplayReq(BaseModel):
    session_file: str
    speed: float = Field(1.0, gt=0, le=10.0)
    actions_only: bool = True

class PluginLoadReq(BaseModel):
    manifest_path: str

class PluginUnloadReq(BaseModel):
    name: str


# ── Routes: System ─────────────────────────────────────────────────────── #
@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "bridge": _bridge.connected if _bridge else False, "version": "3.1.0"}

@app.get("/api/v1/state", tags=["Robot"])
async def get_state():
    return json.loads(_state_json(await _br().get_state()))


# ── Routes: Motion ─────────────────────────────────────────────────────── #
@app.post("/api/v1/move", tags=["Motion"])
async def move(r: MoveReq):
    if _data_logger: _data_logger.log_action("move", r.model_dump())
    await _br().move(r.vx, r.vy, r.vyaw)
    return {"status": "ok", **r.model_dump()}

@app.post("/api/v1/stop", tags=["Motion"])
async def stop():
    if _data_logger: _data_logger.log_action("stop", {})
    await _br().stop()
    return {"status": "stopped"}

@app.post("/api/v1/emergency_stop", tags=["Motion"])
async def emergency_stop():
    if _data_logger: _data_logger.log_event("emergency_stop")
    await _br().emergency_stop()
    if _behavior:
        await _behavior.enqueue("emergency_sit")
    return {"status": "emergency_stop_issued"}

@app.post("/api/v1/stand", tags=["Motion"])
async def stand(r: StandReq):
    if _data_logger: _data_logger.log_action("stand", r.model_dump())
    if r.action == "up": await _br().stand_up()
    else:                await _br().stand_down()
    return {"status": "ok", "action": r.action}


# ── Routes: Mode ───────────────────────────────────────────────────────── #
@app.post("/api/v1/mode", tags=["Mode"])
async def set_mode(r: ModeReq):
    if _data_logger: _data_logger.log_action("mode", r.model_dump())
    await _br().set_mode(r.mode)
    return {"status": "ok", "mode": r.mode}


# ── Routes: Config ─────────────────────────────────────────────────────── #
@app.post("/api/v1/config/height", tags=["Config"])
async def set_height(r: HeightReq):
    if _data_logger: _data_logger.log_action("set_body_height", r.model_dump())
    await _br().set_body_height(r.height)
    return {"status": "ok", "body_height": r.height}

@app.post("/api/v1/config/euler", tags=["Config"])
async def set_euler(r: EulerReq):
    await _br().set_euler(r.roll, r.pitch, r.yaw)
    return {"status": "ok", **r.model_dump()}

@app.post("/api/v1/config/speed", tags=["Config"])
async def set_speed(r: SpeedReq):
    await _br().set_speed_level(r.level)
    return {"status": "ok", "level": r.level}

@app.post("/api/v1/config/foot_raise", tags=["Config"])
async def set_foot_raise(r: FootReq):
    await _br().set_foot_raise_height(r.height)
    return {"status": "ok", "foot_raise_height": r.height}

@app.post("/api/v1/config/obstacle", tags=["Config"])
async def set_obstacle(r: ObstacleReq):
    await _br().set_obstacle_avoidance(r.enabled)
    return {"status": "ok", "obstacle_avoidance": r.enabled}

@app.post("/api/v1/vui", tags=["Config"])
async def set_vui(r: VUIReq):
    await _br().set_vui(r.volume, r.brightness)
    return {"status": "ok", "volume": r.volume, "brightness": r.brightness}


# ── Routes: Behavior ───────────────────────────────────────────────────── #
@app.post("/api/v1/behavior", tags=["Behavior"])
async def trigger_behavior(r: BehaviorReq):
    if _data_logger: _data_logger.log_action("behavior", r.model_dump())
    try:
        await _be().enqueue(r.behavior, r.params, Priority(r.priority))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "queued", "behavior": r.behavior}

@app.get("/api/v1/behaviors", tags=["Behavior"])
async def list_behaviors():
    b = _be()
    return {"behaviors": b.available_behaviors, "current": b.current_behavior, "history": b.history}


# ── Routes: NLU (NEW v3.1) ─────────────────────────────────────────────── #
@app.post("/api/v1/nlu/command", tags=["NLU"])
async def nlu_command(r: NLUReq):
    """
    Natural-language robot control.

    Parses the text, returns the interpreted actions.
    When execute=true (default), runs them immediately.

    Example:  {"text": "walk forward slowly"}
              → move(vx=0.2)
    """
    actions: list[NLUAction] = await interpret(
        r.text, llm_fallback=r.llm_fallback
    )
    if _data_logger:
        _data_logger.log_event("nlu_command", {"text": r.text, "actions": len(actions)})

    executed: list[dict] = []
    if r.execute and _bridge:
        for act in actions:
            try:
                await _dispatch_nlu_action(act)
                executed.append({"action": act.action_type, "params": act.params, "confidence": act.confidence})
            except Exception as e:
                logger.warning("NLU dispatch error: %s", e)

    return {
        "text":    r.text,
        "actions": [{"action_type": a.action_type, "params": a.params, "confidence": a.confidence} for a in actions],
        "executed": executed,
    }

async def _dispatch_nlu_action(act: NLUAction) -> None:
    if not _bridge: return
    match act.action_type:
        case "move":
            await _bridge.move(act.params.get("vx",0), act.params.get("vy",0), act.params.get("vyaw",0))
        case "stop":
            await _bridge.stop()
        case "emergency_stop":
            await _bridge.emergency_stop()
        case "mode":
            await _bridge.set_mode(act.params["mode"])
        case "behavior":
            if _behavior: await _behavior.enqueue(act.params.get("behavior","idle"))
        case "config":
            if "height" in act.params: await _bridge.set_body_height(act.params["height"])
        case "config_obstacle":
            await _bridge.set_obstacle_avoidance(bool(act.params.get("enabled", True)))
        case "vui":
            vol = act.params.get("volume", -1)
            bri = act.params.get("brightness", -1)
            await _bridge.set_vui(vol if vol >= 0 else 50, bri if bri >= 0 else 50)


# ── Routes: Personality ────────────────────────────────────────────────── #
@app.get("/api/v1/personality", tags=["Personality"])
async def get_personality():
    if not _personality: raise HTTPException(503, "Personality not ready")
    return _personality.to_dict()


# ── Routes: Data / Replay (NEW v3.1) ───────────────────────────────────── #
@app.get("/api/v1/sessions", tags=["Data"])
async def list_sessions():
    if not _data_logger: return {"sessions": []}
    return {"sessions": [str(p) for p in _data_logger.list_sessions()]}

@app.post("/api/v1/replay", tags=["Data"])
async def replay_session(r: ReplayReq):
    if not _bridge: raise HTTPException(503, "Bridge not ready")
    from pathlib import Path
    if not Path(r.session_file).exists():
        raise HTTPException(404, f"Session file not found: {r.session_file}")
    replayer = SessionReplayer(r.session_file)
    asyncio.create_task(replayer.replay(_bridge, speed=r.speed, actions_only=r.actions_only))
    return {"status": "replay_started", "file": r.session_file, "speed": r.speed}


# ── Routes: Plugins ────────────────────────────────────────────────────── #
@app.get("/api/v1/plugins", tags=["Plugins"])
async def list_plugins():
    if not _plugins: raise HTTPException(503, "Plugin manager not ready")
    return {"plugins": _plugins.status(), "loaded": _plugins.loaded}

@app.post("/api/v1/plugins/load", tags=["Plugins"])
async def load_plugin(r: PluginLoadReq):
    if not (_plugins and _bridge and _behavior): raise HTTPException(503, "Systems not ready")
    from pathlib import Path
    try:
        manifest = PluginManifest.from_yaml(Path(r.manifest_path))
        ctx = PluginContext(trust_level=manifest.trust_level, _bridge=_bridge, _behavior=_behavior)
        await _plugins.load(manifest, ctx)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"status": "loaded", "plugin": manifest.name}

@app.post("/api/v1/plugins/unload", tags=["Plugins"])
async def unload_plugin(r: PluginUnloadReq):
    if not _plugins: raise HTTPException(503, "Plugin manager not ready")
    try: await _plugins.unload(r.name)
    except Exception as e: raise HTTPException(400, str(e))
    return {"status": "unloaded", "plugin": r.name}


# ── WebSocket ──────────────────────────────────────────────────────────── #
@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    logger.info("WS connected (%d total)", len(_ws_clients))
    try:
        while True:
            data = await ws.receive_text()
            try:
                cmd = json.loads(data)
                await _handle_ws_cmd(cmd)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        logger.info("WS disconnected (%d remaining)", len(_ws_clients))

async def _handle_ws_cmd(cmd: dict) -> None:
    if not _bridge: return
    match cmd.get("action"):
        case "move":     await _bridge.move(cmd.get("vx",0), cmd.get("vy",0), cmd.get("vyaw",0))
        case "stop":     await _bridge.stop()
        case "emergency_stop": await _bridge.emergency_stop()
        case "mode":     await _bridge.set_mode(cmd["mode"])
        case "behavior":
            if _behavior: await _behavior.enqueue(cmd["behavior"], cmd.get("params",{}))
        case "nlu":
            actions = await interpret(cmd.get("text",""), llm_fallback=False)
            for act in actions:
                await _dispatch_nlu_action(act)


# ── Entry point ────────────────────────────────────────────────────────── #
def main() -> None:
    import uvicorn
    uvicorn.run(
        "backend.api.server:app",
        host=os.getenv("CERBERUS_HOST", "0.0.0.0"),
        port=int(os.getenv("CERBERUS_PORT", "8080")),
        reload=os.getenv("CERBERUS_DEV", "false").lower() == "true",
        log_level="info",
    )

if __name__ == "__main__":
    main()
