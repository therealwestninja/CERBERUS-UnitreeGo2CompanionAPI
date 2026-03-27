"""
backend/api/server.py
=====================
CERBERUS FastAPI server.

Endpoints
---------
GET  /health                    liveness probe
GET  /api/v1/state              full robot state snapshot
POST /api/v1/move               velocity control
POST /api/v1/stop               stop motion
POST /api/v1/emergency_stop     hard damp (bypasses queue)
POST /api/v1/stand              stand_up / stand_down
POST /api/v1/mode               set named sport mode
POST /api/v1/config/height      set body height
POST /api/v1/config/euler       set euler angles
POST /api/v1/config/speed       set speed level
POST /api/v1/config/foot_raise  set foot raise height
POST /api/v1/config/obstacle    toggle obstacle avoidance
POST /api/v1/vui                set volume / LED brightness
POST /api/v1/behavior           trigger a named behavior
GET  /api/v1/behaviors          list available behaviors
GET  /api/v1/personality        current personality + mood
POST /api/v1/plugins/load       load a plugin by manifest path
POST /api/v1/plugins/unload     unload a named plugin
GET  /api/v1/plugins            list loaded plugins

WS   /ws/telemetry              real-time state at 10 Hz
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from cerberus.behavior.engine import BehaviorEngine, Priority
from cerberus.core.cognitive import CognitiveEngine, Goal, GoalType
from cerberus.hardware.go2_bridge import (
    AVAILABLE_MODES,
    ConnectionState,
    Go2Bridge,
    RobotState,
)
from cerberus.personality.model import PersonalityModel, Traits
from cerberus.plugins.manager import (
    PluginContext,
    PluginManager,
    PluginManifest,
    TrustLevel,
)
from cerberus.safety.gate import SafetyConfig, SafetyGate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application-level singletons (created in lifespan)
# ---------------------------------------------------------------------------

_bridge:    Optional[Go2Bridge]      = None
_behavior:  Optional[BehaviorEngine] = None
_cognitive: Optional[CognitiveEngine] = None
_personality: Optional[PersonalityModel] = None
_plugins:   Optional[PluginManager]  = None
_ws_clients: set[WebSocket] = set()


def _load_config() -> dict:
    config_path = os.getenv("CERBERUS_CONFIG", "config/cerberus.yaml")
    try:
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Config file not found at %s — using defaults", config_path)
        return {}


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge, _behavior, _cognitive, _personality, _plugins

    cfg = _load_config()
    robot_cfg      = cfg.get("robot", {})
    behavior_cfg   = cfg.get("behavior", {})
    personality_cfg= cfg.get("personality", {})
    safety_cfg_raw = cfg.get("safety", {})
    plugins_dir    = cfg.get("plugins_dir", "plugins")

    # Safety gate
    safety = SafetyGate(SafetyConfig(**safety_cfg_raw) if safety_cfg_raw else None)

    # Hardware bridge
    _bridge = Go2Bridge.from_config(robot_cfg)
    try:
        await _bridge.connect()
        await _bridge.start_watchdog()
        logger.info("Robot bridge connected")
    except Exception as exc:
        logger.error("Bridge connect failed: %s — running in simulation mode", exc)

    # Personality
    traits = Traits(**personality_cfg.get("traits", {})) if "traits" in personality_cfg else None
    _personality = PersonalityModel(traits=traits,
                                    persistence_path=personality_cfg.get("persistence_path"))

    # Behavior engine
    _behavior = BehaviorEngine(_bridge, tick_rate_hz=behavior_cfg.get("tick_rate_hz", 10.0))
    await _behavior.start()

    # Cognitive engine
    _cognitive = CognitiveEngine(_behavior, _personality)
    if cfg.get("cognitive", {}).get("enabled", True):
        await _cognitive.start()

    # Plugin manager
    _plugins = PluginManager(plugins_dir=plugins_dir)
    for manifest in _plugins.discover():
        if manifest.enabled:
            ctx = PluginContext(
                trust_level=manifest.trust_level,
                _bridge=_bridge,
                _behavior=_behavior,
            )
            try:
                await _plugins.load(manifest, ctx)
            except Exception as exc:
                logger.error("Auto-load plugin '%s' failed: %s", manifest.name, exc)

    # State broadcaster
    asyncio.create_task(_state_broadcaster())

    yield  # ← server runs here

    # Shutdown
    logger.info("CERBERUS shutting down…")
    if _cognitive:
        await _cognitive.stop()
    if _behavior:
        await _behavior.stop()
    if _plugins:
        await _plugins.unload_all()
    if _bridge:
        await _bridge.disconnect()
    if _personality:
        _personality.save()


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CERBERUS — Unitree Go2 Companion API",
    description=(
        "Canine-Emulative Responsive Behavioral Engine & Reactive Utility System.\n\n"
        "Full robot control, behavior, cognitive, personality, and plugin API."
    ),
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_bridge() -> Go2Bridge:
    if _bridge is None:
        raise HTTPException(503, "Robot bridge not initialised")
    return _bridge


def _get_behavior() -> BehaviorEngine:
    if _behavior is None:
        raise HTTPException(503, "Behavior engine not ready")
    return _behavior


async def _state_broadcaster() -> None:
    """Push state snapshots to all connected WebSocket clients at 10 Hz."""
    while True:
        await asyncio.sleep(0.1)
        if not _ws_clients or _bridge is None:
            continue
        try:
            state = await _bridge.get_state()
            if _personality:
                _personality.tick()
                if _cognitive:
                    _cognitive.update_from_state(state)
            payload = _state_to_json(state)
            dead: set[WebSocket] = set()
            for ws in _ws_clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            _ws_clients -= dead
        except Exception as exc:
            logger.debug("Broadcaster error: %s", exc)


def _state_to_json(state: RobotState) -> str:
    d = {
        "timestamp":        state.timestamp,
        "connection":       state.connection_state.value,
        "position":         {"x": state.position_x, "y": state.position_y},
        "orientation":      {"yaw": state.yaw, "pitch": state.pitch, "roll": state.roll},
        "velocity":         {"vx": state.vx, "vy": state.vy, "vyaw": state.vyaw},
        "body_height":      state.body_height,
        "battery":          {"voltage": state.battery_voltage, "percent": state.battery_percent},
        "foot_force":       state.foot_force,
        "sport_mode":       state.sport_mode_active,
        "obstacle_avoid":   state.obstacle_avoidance,
        "current_mode":     state.current_mode,
        "latency_ms":       state.latency_ms,
    }
    if _personality:
        d["personality"] = _personality.to_dict()
    if _behavior:
        d["current_behavior"] = _behavior.current_behavior
    return json.dumps(d)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MoveRequest(BaseModel):
    vx:   float = Field(0.0, ge=-1.5, le=1.5,  description="Forward velocity m/s")
    vy:   float = Field(0.0, ge=-0.8, le=0.8,  description="Lateral velocity m/s")
    vyaw: float = Field(0.0, ge=-2.0, le=2.0,  description="Yaw rate rad/s")

class StandRequest(BaseModel):
    action: str = Field("up", pattern="^(up|down)$")

class ModeRequest(BaseModel):
    mode: str

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in AVAILABLE_MODES:
            raise ValueError(f"Unknown mode '{v}'. Valid: {sorted(AVAILABLE_MODES)}")
        return v

class BodyHeightRequest(BaseModel):
    height: float = Field(..., ge=0.3, le=0.5, description="Body height in metres")

class EulerRequest(BaseModel):
    roll:  float = Field(0.0, ge=-0.75, le=0.75)
    pitch: float = Field(0.0, ge=-0.75, le=0.75)
    yaw:   float = Field(0.0, ge=-1.5,  le=1.5)

class SpeedRequest(BaseModel):
    level: int = Field(..., ge=-1, le=1)

class FootRaiseRequest(BaseModel):
    height: float = Field(..., ge=-0.06, le=0.03)

class ObstacleRequest(BaseModel):
    enabled: bool

class VUIRequest(BaseModel):
    volume:     int = Field(50, ge=0, le=100)
    brightness: int = Field(50, ge=0, le=100)

class BehaviorRequest(BaseModel):
    behavior: str
    params:   dict = Field(default_factory=dict)
    priority: int  = Field(50, ge=0, le=100)

class PluginLoadRequest(BaseModel):
    manifest_path: str

class PluginUnloadRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Routes — System
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health():
    return {
        "status":     "ok",
        "bridge":     _bridge.connected if _bridge else False,
        "version":    "3.0.0",
    }


@app.get("/api/v1/state", tags=["Robot"])
async def get_state():
    state = await _get_bridge().get_state()
    return json.loads(_state_to_json(state))


# ---------------------------------------------------------------------------
# Routes — Motion
# ---------------------------------------------------------------------------

@app.post("/api/v1/move", tags=["Motion"])
async def move(req: MoveRequest):
    await _get_bridge().move(req.vx, req.vy, req.vyaw)
    return {"status": "ok", "vx": req.vx, "vy": req.vy, "vyaw": req.vyaw}


@app.post("/api/v1/stop", tags=["Motion"])
async def stop():
    await _get_bridge().stop()
    return {"status": "stopped"}


@app.post("/api/v1/emergency_stop", tags=["Motion"])
async def emergency_stop():
    await _get_bridge().emergency_stop()
    if _behavior:
        await _behavior.enqueue("emergency_sit")
    return {"status": "emergency_stop_issued"}


@app.post("/api/v1/stand", tags=["Motion"])
async def stand(req: StandRequest):
    if req.action == "up":
        await _get_bridge().stand_up()
    else:
        await _get_bridge().stand_down()
    return {"status": "ok", "action": req.action}


# ---------------------------------------------------------------------------
# Routes — Mode
# ---------------------------------------------------------------------------

@app.post("/api/v1/mode", tags=["Mode"])
async def set_mode(req: ModeRequest):
    await _get_bridge().set_mode(req.mode)
    return {"status": "ok", "mode": req.mode}


# ---------------------------------------------------------------------------
# Routes — Configuration
# ---------------------------------------------------------------------------

@app.post("/api/v1/config/height", tags=["Config"])
async def set_body_height(req: BodyHeightRequest):
    await _get_bridge().set_body_height(req.height)
    return {"status": "ok", "body_height": req.height}


@app.post("/api/v1/config/euler", tags=["Config"])
async def set_euler(req: EulerRequest):
    await _get_bridge().set_euler(req.roll, req.pitch, req.yaw)
    return {"status": "ok", **req.model_dump()}


@app.post("/api/v1/config/speed", tags=["Config"])
async def set_speed(req: SpeedRequest):
    await _get_bridge().set_speed_level(req.level)
    return {"status": "ok", "level": req.level}


@app.post("/api/v1/config/foot_raise", tags=["Config"])
async def set_foot_raise(req: FootRaiseRequest):
    await _get_bridge().set_foot_raise_height(req.height)
    return {"status": "ok", "foot_raise_height": req.height}


@app.post("/api/v1/config/obstacle", tags=["Config"])
async def set_obstacle_avoidance(req: ObstacleRequest):
    await _get_bridge().set_obstacle_avoidance(req.enabled)
    return {"status": "ok", "obstacle_avoidance": req.enabled}


@app.post("/api/v1/vui", tags=["Config"])
async def set_vui(req: VUIRequest):
    await _get_bridge().set_vui(req.volume, req.brightness)
    return {"status": "ok", "volume": req.volume, "brightness": req.brightness}


# ---------------------------------------------------------------------------
# Routes — Behavior
# ---------------------------------------------------------------------------

@app.post("/api/v1/behavior", tags=["Behavior"])
async def trigger_behavior(req: BehaviorRequest):
    beh = _get_behavior()
    try:
        await beh.enqueue(req.behavior, req.params, Priority(req.priority))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "queued", "behavior": req.behavior}


@app.get("/api/v1/behaviors", tags=["Behavior"])
async def list_behaviors():
    beh = _get_behavior()
    return {
        "behaviors": beh.available_behaviors,
        "current":   beh.current_behavior,
        "history":   beh.history,
    }


# ---------------------------------------------------------------------------
# Routes — Personality
# ---------------------------------------------------------------------------

@app.get("/api/v1/personality", tags=["Personality"])
async def get_personality():
    if not _personality:
        raise HTTPException(503, "Personality system not ready")
    return _personality.to_dict()


# ---------------------------------------------------------------------------
# Routes — Plugins
# ---------------------------------------------------------------------------

@app.get("/api/v1/plugins", tags=["Plugins"])
async def list_plugins():
    if not _plugins:
        raise HTTPException(503, "Plugin manager not ready")
    return {"plugins": _plugins.status(), "loaded": _plugins.loaded}


@app.post("/api/v1/plugins/load", tags=["Plugins"])
async def load_plugin(req: PluginLoadRequest):
    if not _plugins or not _bridge or not _behavior:
        raise HTTPException(503, "Systems not ready")
    from pathlib import Path
    try:
        manifest = PluginManifest.from_yaml(Path(req.manifest_path))
        ctx = PluginContext(
            trust_level=manifest.trust_level,
            _bridge=_bridge,
            _behavior=_behavior,
        )
        await _plugins.load(manifest, ctx)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"status": "loaded", "plugin": manifest.name}


@app.post("/api/v1/plugins/unload", tags=["Plugins"])
async def unload_plugin(req: PluginUnloadRequest):
    if not _plugins:
        raise HTTPException(503, "Plugin manager not ready")
    try:
        await _plugins.unload(req.name)
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"status": "unloaded", "plugin": req.name}


# ---------------------------------------------------------------------------
# WebSocket telemetry
# ---------------------------------------------------------------------------

@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    logger.info("WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            # Keep connection alive; clients can also send commands
            data = await websocket.receive_text()
            try:
                cmd = json.loads(data)
                await _handle_ws_command(cmd, websocket)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
        logger.info("WS client disconnected (%d remaining)", len(_ws_clients))


async def _handle_ws_command(cmd: dict, ws: WebSocket) -> None:
    """Handle inbound commands from WS clients."""
    action = cmd.get("action")
    if not _bridge:
        return
    match action:
        case "move":
            await _bridge.move(
                cmd.get("vx", 0.0),
                cmd.get("vy", 0.0),
                cmd.get("vyaw", 0.0),
            )
        case "stop":
            await _bridge.stop()
        case "emergency_stop":
            await _bridge.emergency_stop()
        case "mode":
            if "mode" in cmd:
                await _bridge.set_mode(cmd["mode"])
        case "behavior":
            if _behavior and "behavior" in cmd:
                await _behavior.enqueue(cmd["behavior"], cmd.get("params", {}))
        case _:
            logger.debug("Unknown WS command: %s", action)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
