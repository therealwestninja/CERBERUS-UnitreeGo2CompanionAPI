"""
backend/main.py
━━━━━━━━━━━━━━
CERBERUS FastAPI Application

REST endpoints + WebSocket state streaming.
Startup/shutdown managed via asyncio lifespan.

Endpoints:
  GET  /                        — Health + engine status
  GET  /state                   — Current robot state snapshot
  GET  /stats                   — Engine stats (Hz, tick count, uptime)
  GET  /anatomy                 — Kinematics, joints, COM, energy
  GET  /behavior                — Cognitive engine status
  GET  /plugins                 — Plugin list
  GET  /safety/events           — Recent safety audit events
  POST /safety/estop            — Trigger emergency stop
  POST /safety/clear_estop      — Clear E-stop (sim only)
  POST /motion/stand_up
  POST /motion/stand_down
  POST /motion/stop
  POST /motion/move             — {vx, vy, vyaw}
  POST /motion/body_height      — {height}
  POST /motion/euler            — {roll, pitch, yaw}
  POST /motion/gait             — {gait_id}
  POST /motion/foot_raise       — {height}
  POST /motion/speed_level      — {level}
  POST /motion/continuous_gait  — {enabled}
  POST /motion/sport_mode       — {mode}
  POST /led                     — {r, g, b}
  POST /volume                  — {level}
  POST /obstacle_avoidance      — {enabled}
  POST /behavior/goal           — {name, priority, params}
  POST /plugins/{name}/enable
  POST /plugins/{name}/disable
  DELETE /plugins/{name}        — Unload plugin
  WS   /ws                      — Real-time state stream (30Hz)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from cerberus.bridge.go2_bridge import create_bridge, SportMode
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
from cerberus.anatomy.kinematics import DigitalAnatomy
from cerberus.plugins.plugin_manager import PluginManager

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

# ── Global singletons ─────────────────────────────────────────────────────────
bridge: Any = None
engine: CerberusEngine | None = None
watchdog: SafetyWatchdog | None = None
plugin_manager: PluginManager | None = None
_ws_clients: list[WebSocket] = []


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, engine, watchdog, plugin_manager

    bridge   = create_bridge()
    limits   = SafetyLimits(
        heartbeat_timeout_s=float(os.getenv("HEARTBEAT_TIMEOUT", "5.0"))
    )
    watchdog = SafetyWatchdog(bridge, limits)
    engine   = CerberusEngine(bridge, watchdog,
                               target_hz=float(os.getenv("CERBERUS_HZ", "60")))

    # Attach subsystems
    engine.behavior_engine = BehaviorEngine(bridge, PersonalityTraits(
        energy       = float(os.getenv("PERSONALITY_ENERGY",       "0.7")),
        friendliness = float(os.getenv("PERSONALITY_FRIENDLINESS", "0.8")),
        curiosity    = float(os.getenv("PERSONALITY_CURIOSITY",    "0.6")),
        loyalty      = float(os.getenv("PERSONALITY_LOYALTY",      "0.9")),
        playfulness  = float(os.getenv("PERSONALITY_PLAYFULNESS",  "0.65")),
    ))
    engine.anatomy = DigitalAnatomy()

    # Plugin system
    plugin_dirs = os.getenv("PLUGIN_DIRS", "plugins").split(":")
    plugin_manager = PluginManager(engine, plugin_dirs)
    await plugin_manager.discover_and_load()
    plugin_manager.register_with_engine()

    # Subscribe state updates to WebSocket broadcast
    engine.bus.subscribe("state.update", _broadcast_state)

    await engine.start()
    logger.info("CERBERUS API ready")

    yield

    logger.info("CERBERUS API shutting down")
    await engine.stop()


async def _broadcast_state(state) -> None:
    if not _ws_clients:
        return
    payload = json.dumps({"type": "state", "data": state.to_dict()})
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CERBERUS — Unitree Go2 Companion API",
    version="2.1.0",
    description="Cognitive, adaptive, canine-emulative companion system for the Unitree Go2",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_engine() -> CerberusEngine:
    if engine is None:
        raise HTTPException(503, "Engine not initialized")
    return engine

def _require_no_estop() -> None:
    if watchdog and watchdog.estop_active:
        raise HTTPException(503, "Emergency stop active — clear E-stop first")

def ok(data: dict | None = None) -> dict:
    return {"ok": True, **(data or {})}


# ── Pydantic models ───────────────────────────────────────────────────────────

class MoveCmd(BaseModel):
    vx:   float = Field(0.0, ge=-1.5, le=1.5)
    vy:   float = Field(0.0, ge=-0.8, le=0.8)
    vyaw: float = Field(0.0, ge=-2.0, le=2.0)

class BodyHeightCmd(BaseModel):
    height: float = Field(0.0, ge=-0.1, le=0.1, description="Relative offset from default (m)")

class EulerCmd(BaseModel):
    roll:  float = Field(0.0, ge=-0.75, le=0.75)
    pitch: float = Field(0.0, ge=-0.75, le=0.75)
    yaw:   float = Field(0.0, ge=-1.5,  le=1.5)

class GaitCmd(BaseModel):
    gait_id: int = Field(..., ge=0, le=4)

class FootRaiseCmd(BaseModel):
    height: float = Field(0.0, ge=-0.06, le=0.03)

class SpeedCmd(BaseModel):
    level: int = Field(..., ge=-1, le=1)

class ContinuousGaitCmd(BaseModel):
    enabled: bool

class SportModeCmd(BaseModel):
    mode: SportMode

class LEDCmd(BaseModel):
    r: int = Field(..., ge=0, le=255)
    g: int = Field(..., ge=0, le=255)
    b: int = Field(..., ge=0, le=255)

class VolumeCmd(BaseModel):
    level: int = Field(..., ge=0, le=100)

class ObstacleCmd(BaseModel):
    enabled: bool

class GoalCmd(BaseModel):
    name:     str
    priority: float = Field(0.5, ge=0.0, le=1.0)
    params:   dict = Field(default_factory=dict)


# ── Status endpoints ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    eng = _require_engine()
    return {
        "service": "CERBERUS",
        "version": "2.1.0",
        "engine_state": eng.state.value,
        "simulation": os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1"),
        "safety_level": watchdog.safety_level.value if watchdog else "unknown",
        "estop_active": watchdog.estop_active if watchdog else False,
    }

@app.get("/state")
async def get_state():
    _require_engine()
    state = await bridge.get_state()
    return state.to_dict()

@app.get("/stats")
async def get_stats():
    eng = _require_engine()
    return eng.stats.to_dict()

@app.get("/anatomy")
async def get_anatomy():
    eng = _require_engine()
    if eng.anatomy is None:
        raise HTTPException(404, "Anatomy subsystem not loaded")
    return eng.anatomy.status()

@app.get("/behavior")
async def get_behavior():
    eng = _require_engine()
    if eng.behavior_engine is None:
        raise HTTPException(404, "Behavior engine not loaded")
    return eng.behavior_engine.status()

@app.get("/plugins")
async def list_plugins():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    return plugin_manager.list_plugins()

@app.get("/safety/events")
async def safety_events(n: int = 50):
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    return watchdog.get_recent_events(n)


# ── Safety ────────────────────────────────────────────────────────────────────

@app.post("/safety/estop")
async def trigger_estop():
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    await watchdog.trigger_estop("API manual trigger")
    return ok({"estop_active": True})

@app.post("/safety/clear_estop")
async def clear_estop():
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    success = await watchdog.clear_estop()
    if not success:
        raise HTTPException(403, "E-stop clearance only allowed in simulation mode")
    return ok({"estop_active": False})


# ── Motion ────────────────────────────────────────────────────────────────────

@app.post("/motion/stand_up")
async def stand_up():
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    ok_result = await bridge.stand_up()
    if not ok_result:
        raise HTTPException(500, "stand_up command failed")
    return ok()

@app.post("/motion/stand_down")
async def stand_down():
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.stand_down()
    return ok()

@app.post("/motion/stop")
async def stop_motion():
    _require_engine()
    watchdog.ping_heartbeat()
    await bridge.stop_move()
    return ok()

@app.post("/motion/move")
async def move(cmd: MoveCmd):
    _require_engine()
    _require_no_estop()
    ok_v, reason = watchdog.validate_velocity(cmd.vx, cmd.vy, cmd.vyaw)
    if not ok_v:
        raise HTTPException(422, f"Velocity validation failed: {reason}")
    watchdog.ping_heartbeat()
    result = await bridge.move(cmd.vx, cmd.vy, cmd.vyaw)
    return ok({"vx": cmd.vx, "vy": cmd.vy, "vyaw": cmd.vyaw, "sent": result})

@app.post("/motion/body_height")
async def body_height(cmd: BodyHeightCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.set_body_height(cmd.height)
    return ok({"height_offset": cmd.height})

@app.post("/motion/euler")
async def set_euler(cmd: EulerCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.set_euler(cmd.roll, cmd.pitch, cmd.yaw)
    return ok()

@app.post("/motion/gait")
async def switch_gait(cmd: GaitCmd):
    _require_engine()
    _require_no_estop()
    await bridge.switch_gait(cmd.gait_id)
    return ok({"gait_id": cmd.gait_id})

@app.post("/motion/foot_raise")
async def foot_raise(cmd: FootRaiseCmd):
    _require_engine()
    _require_no_estop()
    await bridge.set_foot_raise_height(cmd.height)
    return ok()

@app.post("/motion/speed_level")
async def speed_level(cmd: SpeedCmd):
    _require_engine()
    _require_no_estop()
    await bridge.set_speed_level(cmd.level)
    return ok({"level": cmd.level})

@app.post("/motion/continuous_gait")
async def continuous_gait(cmd: ContinuousGaitCmd):
    _require_engine()
    await bridge.set_continuous_gait(cmd.enabled)
    return ok({"enabled": cmd.enabled})

@app.post("/motion/sport_mode")
async def sport_mode(cmd: SportModeCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    result = await bridge.execute_sport_mode(cmd.mode)
    if not result:
        raise HTTPException(500, f"Sport mode '{cmd.mode.value}' failed")
    return ok({"mode": cmd.mode.value})


# ── Peripherals ───────────────────────────────────────────────────────────────

@app.post("/led")
async def set_led(cmd: LEDCmd):
    _require_engine()
    await bridge.set_led(cmd.r, cmd.g, cmd.b)
    return ok({"rgb": [cmd.r, cmd.g, cmd.b]})

@app.post("/volume")
async def set_volume(cmd: VolumeCmd):
    _require_engine()
    await bridge.set_volume(cmd.level)
    return ok({"level": cmd.level})

@app.post("/obstacle_avoidance")
async def obstacle_avoidance(cmd: ObstacleCmd):
    _require_engine()
    await bridge.set_obstacle_avoidance(cmd.enabled)
    return ok({"enabled": cmd.enabled})


# ── Behavior / Cognition ──────────────────────────────────────────────────────

@app.post("/behavior/goal")
async def push_goal(cmd: GoalCmd):
    eng = _require_engine()
    if eng.behavior_engine is None:
        raise HTTPException(404, "Behavior engine not loaded")
    eng.behavior_engine.push_goal(cmd.name, cmd.priority, **cmd.params)
    return ok({"goal": cmd.name, "priority": cmd.priority})


# ── Plugin management ─────────────────────────────────────────────────────────

@app.post("/plugins/{name}/enable")
async def enable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.enable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()

@app.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.disable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()

@app.delete("/plugins/{name}")
async def unload_plugin(name: str):
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    success = await plugin_manager.unload_plugin(name)
    if not success:
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    logger.info("WebSocket client connected (total: %d)", len(_ws_clients))
    try:
        # Send immediate state on connect
        if bridge:
            state = await bridge.get_state()
            await ws.send_text(json.dumps({"type": "state", "data": state.to_dict()}))

        # Keep connection alive and handle incoming commands
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                data = json.loads(msg)
                await _handle_ws_command(ws, data)
            except asyncio.TimeoutError:
                # Send ping
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        logger.info("WebSocket client disconnected (total: %d)", len(_ws_clients))


async def _handle_ws_command(ws: WebSocket, data: dict) -> None:
    """Handle incoming WS commands from clients."""
    cmd = data.get("cmd")
    if cmd == "move":
        if watchdog and not watchdog.estop_active:
            watchdog.ping_heartbeat()
            await bridge.move(
                data.get("vx", 0.0),
                data.get("vy", 0.0),
                data.get("vyaw", 0.0),
            )
    elif cmd == "stop":
        await bridge.stop_move()
    elif cmd == "estop":
        if watchdog:
            await watchdog.trigger_estop("WebSocket client")
    elif cmd == "sport_mode":
        if watchdog and not watchdog.estop_active:
            try:
                mode = SportMode(data.get("mode", "stop_move"))
                await bridge.execute_sport_mode(mode)
            except ValueError:
                await ws.send_text(json.dumps({"type": "error", "msg": "invalid sport mode"}))
    elif cmd == "subscribe":
        pass  # All clients get state broadcasts automatically
    else:
        await ws.send_text(json.dumps({"type": "error", "msg": f"unknown command: {cmd}"}))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("GO2_API_HOST", "0.0.0.0"),
        port=int(os.getenv("GO2_API_PORT", "8080")),
        reload=os.getenv("DEV_RELOAD", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )

if __name__ == "__main__":
    main()
