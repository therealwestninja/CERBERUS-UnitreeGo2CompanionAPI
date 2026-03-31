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
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

from cerberus import __version__
from cerberus.bridge.go2_bridge import create_bridge, SportMode
from cerberus.core.auth import require_api_key, auth_enabled
from cerberus.cognitive.session_store import SessionStore
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

# ── WebSocket Manager ─────────────────────────────────────────────────────────

class WebSocketManager:
    """
    Centralised WebSocket client registry with atomic dead-client cleanup.

    Replaces the duplicated dead-client-removal pattern that previously
    appeared independently in every EventBus broadcast callback.
    """

    def __init__(self):
        self._clients: list[WebSocket] = []

    def add(self, ws: WebSocket) -> None:
        self._clients.append(ws)

    def remove(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, msg: str) -> None:
        """Send text to all clients; silently drop any that have disconnected."""
        if not self._clients:
            return
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)

    async def broadcast_json(self, type_: str, data: Any) -> None:
        """Convenience wrapper — serialises canonical WS envelope and broadcasts."""
        await self.broadcast(json.dumps(_ws_envelope(type_, data)))

    @property
    def count(self) -> int:
        return len(self._clients)


# ── Global singletons ─────────────────────────────────────────────────────────
bridge: Any = None
engine: CerberusEngine | None = None
watchdog: SafetyWatchdog | None = None
plugin_manager: PluginManager | None = None
ws_manager = WebSocketManager()
_ws_seq = 0


def _ws_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_ws_seq() -> int:
    global _ws_seq
    _ws_seq += 1
    return _ws_seq


def _ws_envelope(type_: str, data: Any | None = None, **extra) -> dict:
    msg = {
        "type": type_,
        "ts": _ws_now(),
        "seq": _next_ws_seq(),
    }
    if data is not None:
        msg["data"] = data
    msg.update(extra)
    return msg


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, engine, watchdog, plugin_manager

    # ── Session store — load persisted personality ────────────────────────────
    _store = SessionStore()
    saved_traits, saved_stats = _store.load()

    bridge   = create_bridge()
    limits   = SafetyLimits(
        heartbeat_timeout_s=float(os.getenv("HEARTBEAT_TIMEOUT", "5.0"))
    )
    watchdog = SafetyWatchdog(bridge, limits)
    engine   = CerberusEngine(bridge, watchdog,
                               target_hz=float(os.getenv("CERBERUS_HZ", "60")))

    # Attach subsystems — personality from session store if available,
    # otherwise fall back to env vars (first-boot behaviour).
    env_personality = PersonalityTraits(
        energy       = float(os.getenv("PERSONALITY_ENERGY",       str(saved_traits.energy))),
        friendliness = float(os.getenv("PERSONALITY_FRIENDLINESS", str(saved_traits.friendliness))),
        curiosity    = float(os.getenv("PERSONALITY_CURIOSITY",    str(saved_traits.curiosity))),
        loyalty      = float(os.getenv("PERSONALITY_LOYALTY",      str(saved_traits.loyalty))),
        playfulness  = float(os.getenv("PERSONALITY_PLAYFULNESS",  str(saved_traits.playfulness))),
    )
    engine.behavior_engine = BehaviorEngine(bridge, env_personality)
    engine.behavior_engine._session_stats = saved_stats
    engine.anatomy = DigitalAnatomy()

    # Plugin system
    plugin_dirs = os.getenv("PLUGIN_DIRS", "plugins").split(":")
    plugin_manager = PluginManager(engine, plugin_dirs)
    await plugin_manager.discover_and_load()
    plugin_manager.register_with_engine()

    # ── EventBus → WebSocket forwarding (single broadcast path) ──────────────
    async def _on_state_update(state) -> None:
        await ws_manager.broadcast_json("state", state.to_dict())

    async def _on_terrain(data) -> None:
        await ws_manager.broadcast_json("terrain", data)

    async def _on_stair(data) -> None:
        await ws_manager.broadcast_json("stair", data)

    async def _on_payload(data) -> None:
        await ws_manager.broadcast_json("payload", data)

    engine.bus.subscribe("state.update", _on_state_update)
    engine.bus.subscribe("terrain.classification", _on_terrain)

    for _stair_topic in ("stair.status", "stair.detected", "stair.exited"):
        engine.bus.subscribe(_stair_topic, _on_stair)
    # Forward voice events to WebSocket
    async def _on_voice(data) -> None:
        await ws_manager.broadcast_json("voice", data)
    for _vt in ("voice.transcript", "voice.intent",
                "voice.listening_started", "voice.listening_stopped"):
        engine.bus.subscribe(_vt, _on_voice)


    for _pt in (
        "payload.contact", "payload.behavior", "payload.drag_warning",
        "payload.scan_result", "payload.attached", "payload.detached",
        "payload.scout_sample", "payload.contact_hold", "payload.thermal_rest",
    ):
        engine.bus.subscribe(_pt, _on_payload)

    # Forward limb-loss events to WebSocket clients
    async def _on_limb_loss(data) -> None:
        await ws_manager.broadcast_json("limb_loss", data)

    for _lt in ("limb_loss.status", "limb_loss.detected", "limb_loss.cleared"):
        engine.bus.subscribe(_lt, _on_limb_loss)

    await engine.start()
    logger.info("CERBERUS API ready — session #%d", saved_stats.session_number)

    yield

    logger.info("CERBERUS API shutting down")
    # Persist personality evolution before stopping
    if engine.behavior_engine is not None:
        _store.save(engine.behavior_engine)
    await engine.stop()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CERBERUS — Unitree Go2 Companion API",
    version=__version__,
    description="Cognitive, adaptive, canine-emulative companion system for the Unitree Go2",
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)],
)

# Serve the React dashboard from backend/static/
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        """Serve the CERBERUS real-time dashboard."""
        html = (_STATIC_DIR / "dashboard.html").read_text()
        return HTMLResponse(html)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-CERBERUS-Key"],
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


class PayloadAttachCmd(BaseModel):
    name:              str   = "undercarriage_payload"
    description:       str   = "Silicone substructure"
    material:          str   = "silicone"
    mass_kg:           float = Field(1.5,  ge=0.1, le=10.0)
    thickness_m:       float = Field(0.05, ge=0.005, le=0.15)
    length_m:          float = Field(0.30, ge=0.05, le=0.60)
    width_m:           float = Field(0.20, ge=0.05, le=0.40)
    desired_clearance_m: float = Field(0.025, ge=0.005, le=0.10)
    has_tactile_sensor:  bool = True
    has_thermal_sensor:  bool = False


class BehaviorTriggerCmd(BaseModel):
    duration_s:    float | None = None
    hold_s:        float | None = None
    nudge_speed:   float | None = None
    nudge_dist_m:  float | None = None
    cols:          int   | None = None
    col_width_m:   float | None = None
    row_len_m:     float | None = None


class StairTuneCmd(BaseModel):
    asym_variance_min:    float | None = Field(None, gt=0)
    dir_changes_min:      int   | None = Field(None, ge=0)
    dir_changes_max:      int   | None = Field(None, ge=1)
    pitch_range_min_rad:  float | None = Field(None, gt=0)
    peak_asym_min:        float | None = Field(None, gt=0)
    diagonal_alt_min:     float | None = Field(None, ge=0)
    min_speed_ms:         float | None = Field(None, ge=0)
    confirm_ticks:        int   | None = Field(None, ge=1)
    exit_ticks:           int   | None = Field(None, ge=1)
    force_spike_ratio:        float | None = Field(None, gt=1)
    force_delta_min_n:        float | None = Field(None, gt=0)
    stall_fraction_threshold: float | None = Field(None, gt=0, lt=1)
    torque_spike_ratio:       float | None = Field(None, gt=1)
    stall_confirm_ticks:      int   | None = Field(None, ge=1)


@app.get("/health")
@app.get("/api/v1/system/health")
async def health():
    return {"status": "healthy", "service": "CERBERUS", "version": __version__}


@app.get("/ready")
@app.get("/api/v1/system/ready")
async def ready():
    if engine is None or engine.state.value not in ("running",):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready",
                     "reason": "engine not running" if engine else "engine not initialised"},
        )
    return {"status": "ready", "engine_hz": round(engine.stats.tick_hz, 1)}


@app.get("/session")
@app.get("/api/v1/session")
async def get_session():
    eng = _require_engine()
    be  = eng.behavior_engine
    if be is None:
        raise HTTPException(404, "Behavior engine not loaded")
    store = SessionStore()
    saved = store.read_file()
    return {
        "session_number":    be._session_stats.session_number,
        "uptime_s":          round(be._session_stats.uptime_s, 1),
        "stats":             be._session_stats.to_dict(),
        "current_personality": be.personality.to_dict(),
        "last_saved":        saved,
    }


@app.get("/")
@app.get("/api/v1/system/info")
async def root():
    eng = _require_engine()
    return {
        "service": "CERBERUS",
        "version": __version__,
        "engine_state": eng.state.value,
        "simulation": os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1"),
        "safety_level": watchdog.safety_level.value if watchdog else "unknown",
        "estop_active": watchdog.estop_active if watchdog else False,
    }

@app.get("/state")
@app.get("/api/v1/robot/state")
async def get_state():
    _require_engine()
    state = await bridge.get_state()
    return state.to_dict()

@app.get("/stats")
@app.get("/api/v1/robot/stats")
async def get_stats():
    eng = _require_engine()
    return eng.stats.to_dict()

@app.get("/anatomy")
@app.get("/api/v1/robot/anatomy")
async def get_anatomy():
    eng = _require_engine()
    if eng.anatomy is None:
        raise HTTPException(404, "Anatomy subsystem not loaded")
    return eng.anatomy.status()

@app.get("/terrain")
@app.get("/api/v1/robot/terrain")
async def get_terrain():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    plugins = {p["name"]: p for p in plugin_manager.list_plugins()}
    terrain_plugin = plugins.get("terrain_arbiter") or plugins.get("TerrainArbiter")
    if terrain_plugin is None:
        raise HTTPException(404, "TerrainArbiter plugin not loaded")
    return terrain_plugin


def _require_stair_plugin():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    for rec in plugin_manager._plugins.values():
        if rec.plugin.__class__.__name__ == "StairClimberPlugin":
            return rec.plugin
    raise HTTPException(404, "StairClimberPlugin not loaded. Ensure plugins/stair_climber/ is in PLUGIN_DIRS.")


@app.get("/stair")
@app.get("/api/v1/robot/stair")
async def get_stair():
    plugin = _require_stair_plugin()
    return plugin.status()


@app.post("/stair/tune")
async def tune_stair(cmd: StairTuneCmd):
    plugin = _require_stair_plugin()
    updates = {k: v for k, v in cmd.dict().items() if v is not None}
    if not updates:
        raise HTTPException(422, "No threshold values supplied — nothing to tune")
    result = plugin.tune(**updates)
    return ok(result)


class LimbDeclareCmd(BaseModel):
    leg: str = Field(..., description="Leg to declare lost: FL, FR, RL, or RR")


class SimLimbLossCmd(BaseModel):
    leg: str | None = Field(None, description="Leg to simulate lost (null to clear)")


def _require_limb_loss_plugin():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    for rec in plugin_manager._plugins.values():
        if rec.plugin.__class__.__name__ == "LimbLossRecoveryPlugin":
            return rec.plugin
    raise HTTPException(404, "LimbLossRecoveryPlugin not loaded. Ensure plugins/limb_loss_recovery/ is in PLUGIN_DIRS.")


@app.get("/limb_loss")
@app.get("/api/v1/robot/limb_loss")
async def get_limb_loss():
    plugin = _require_limb_loss_plugin()
    return plugin.status()


@app.post("/limb_loss/declare")
async def declare_limb_loss(cmd: LimbDeclareCmd):
    _require_engine()
    plugin = _require_limb_loss_plugin()
    result = await plugin.declare_limb_loss(cmd.leg)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return ok(result)


@app.post("/limb_loss/clear")
async def clear_limb_loss():
    _require_engine()
    plugin = _require_limb_loss_plugin()
    result = await plugin.clear_limb_loss()
    if "error" in result:
        raise HTTPException(409, result["error"])
    return ok(result)


@app.post("/sim/limb_loss")
async def sim_limb_loss(cmd: SimLimbLossCmd):
    if os.getenv("GO2_SIMULATION", "false").lower() not in ("true", "1", "yes"):
        raise HTTPException(409, "Simulation limb-loss injection requires GO2_SIMULATION=true")

    from cerberus.bridge.go2_bridge import SimBridge
    if not isinstance(bridge, SimBridge):
        raise HTTPException(409, "Active bridge is not a SimBridge")

    if cmd.leg is None:
        bridge.clear_limb_loss()
        return ok({"simulated_lost_leg": None})

    leg = cmd.leg.upper().strip()
    leg_map = {"FL": 0, "FR": 1, "RL": 2, "RR": 3}
    if leg not in leg_map:
        raise HTTPException(422, f"Unknown leg '{leg}'. Valid: FL, FR, RL, RR")

    bridge.simulate_limb_loss(leg_map[leg])
    return ok({"simulated_lost_leg": leg})

class VoiceTranscribeCmd(BaseModel):
    path: str = Field(..., description="Path to audio file (wav/mp3/m4a/flac)")


def _require_voice_plugin():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    for rec in plugin_manager._plugins.values():
        if rec.plugin.__class__.__name__ == "VoiceNLUPlugin":
            return rec.plugin
    raise HTTPException(404, "VoiceNLUPlugin not loaded. Ensure plugins/voice_nlu/ is in PLUGIN_DIRS.")


@app.get("/voice")
@app.get("/api/v1/robot/voice")
async def get_voice():
    plugin = _require_voice_plugin()
    return plugin.status()


@app.post("/voice/listen/start")
async def voice_listen_start():
    _require_engine()
    plugin = _require_voice_plugin()
    result = await plugin.start_listening()
    if "error" in result:
        raise HTTPException(409, result["error"])
    return ok(result)


@app.post("/voice/listen/stop")
async def voice_listen_stop():
    plugin = _require_voice_plugin()
    result = await plugin.stop_listening()
    return ok(result)


@app.post("/voice/transcribe")
async def voice_transcribe(cmd: VoiceTranscribeCmd):
    _require_engine()
    plugin = _require_voice_plugin()
    result = await plugin.transcribe_file(cmd.path)
    if "error" in result:
        raise HTTPException(422, result["error"])
    return result


def _require_payload_plugin():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    for rec in plugin_manager._plugins.values():
        inst = rec.plugin
        if inst is not None and inst.__class__.__name__ == "UndercarriagePayloadPlugin":
            return inst
    raise HTTPException(404, "UndercarriagePayloadPlugin not loaded. Load it first: plugin_dirs must include plugins/undercarriage_payload")


@app.get("/payload")
@app.get("/api/v1/robot/payload")
async def get_payload():
    plugin = _require_payload_plugin()
    return plugin.status()


@app.get("/behavior")
@app.get("/api/v1/robot/behavior")
async def get_behavior():
    eng = _require_engine()
    if eng.behavior_engine is None:
        raise HTTPException(404, "Behavior engine not loaded")
    return eng.behavior_engine.status()

@app.get("/plugins")
@app.get("/api/v1/plugins")
async def list_plugins():
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    return plugin_manager.list_plugins()

@app.get("/safety/events")
@app.get("/api/v1/safety/events")
async def safety_events(n: int = 50):
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    return watchdog.get_recent_events(n)


@app.post("/safety/estop")
@app.post("/api/v1/safety/estop")
async def trigger_estop():
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    await watchdog.trigger_estop("API manual trigger")
    return ok({"estop_active": True})

@app.post("/safety/clear_estop")
@app.post("/api/v1/safety/clear_estop")
async def clear_estop():
    if watchdog is None:
        raise HTTPException(503, "Watchdog not ready")
    success = await watchdog.clear_estop()
    if not success:
        raise HTTPException(403, "E-stop clearance only allowed in simulation mode")
    return ok({"estop_active": False})


@app.post("/motion/stand_up")
@app.post("/api/v1/robot/motion/stand_up")
async def stand_up():
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    ok_result = await bridge.stand_up()
    if not ok_result:
        raise HTTPException(500, "stand_up command failed")
    return ok()

@app.post("/motion/stand_down")
@app.post("/api/v1/robot/motion/stand_down")
async def stand_down():
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.stand_down()
    return ok()

@app.post("/motion/stop")
@app.post("/api/v1/robot/motion/stop")
async def stop_motion():
    _require_engine()
    watchdog.ping_heartbeat()
    await bridge.stop_move()
    return ok()

@app.post("/motion/move")
@app.post("/api/v1/robot/motion/move")
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
@app.post("/api/v1/robot/motion/body_height")
async def body_height(cmd: BodyHeightCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.set_body_height(cmd.height)
    return ok({"height_offset": cmd.height})

@app.post("/motion/euler")
@app.post("/api/v1/robot/motion/euler")
async def set_euler(cmd: EulerCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    await bridge.set_euler(cmd.roll, cmd.pitch, cmd.yaw)
    return ok()

@app.post("/motion/gait")
@app.post("/api/v1/robot/motion/gait")
async def switch_gait(cmd: GaitCmd):
    _require_engine()
    _require_no_estop()
    await bridge.switch_gait(cmd.gait_id)
    return ok({"gait_id": cmd.gait_id})

@app.post("/motion/foot_raise")
@app.post("/api/v1/robot/motion/foot_raise")
async def foot_raise(cmd: FootRaiseCmd):
    _require_engine()
    _require_no_estop()
    await bridge.set_foot_raise_height(cmd.height)
    return ok()

@app.post("/motion/speed_level")
@app.post("/api/v1/robot/motion/speed_level")
async def speed_level(cmd: SpeedCmd):
    _require_engine()
    _require_no_estop()
    await bridge.set_speed_level(cmd.level)
    return ok({"level": cmd.level})

@app.post("/motion/continuous_gait")
@app.post("/api/v1/robot/motion/continuous_gait")
async def continuous_gait(cmd: ContinuousGaitCmd):
    _require_engine()
    await bridge.set_continuous_gait(cmd.enabled)
    return ok({"enabled": cmd.enabled})

@app.post("/motion/sport_mode")
@app.post("/api/v1/robot/motion/sport_mode")
async def sport_mode(cmd: SportModeCmd):
    _require_engine()
    _require_no_estop()
    watchdog.ping_heartbeat()
    result = await bridge.execute_sport_mode(cmd.mode)
    if not result:
        raise HTTPException(500, f"Sport mode '{cmd.mode.value}' failed")
    return ok({"mode": cmd.mode.value})


@app.post("/led")
@app.post("/api/v1/robot/led")
async def set_led(cmd: LEDCmd):
    _require_engine()
    await bridge.set_led(cmd.r, cmd.g, cmd.b)
    return ok({"rgb": [cmd.r, cmd.g, cmd.b]})

@app.post("/volume")
@app.post("/api/v1/robot/audio/volume")
async def set_volume(cmd: VolumeCmd):
    _require_engine()
    await bridge.set_volume(cmd.level)
    return ok({"level": cmd.level})

@app.post("/obstacle_avoidance")
@app.post("/api/v1/robot/navigation/obstacle_avoidance")
async def obstacle_avoidance(cmd: ObstacleCmd):
    _require_engine()
    await bridge.set_obstacle_avoidance(cmd.enabled)
    return ok({"enabled": cmd.enabled})


@app.post("/behavior/goal")
@app.post("/api/v1/behavior/goal")
async def push_goal(cmd: GoalCmd):
    eng = _require_engine()
    if eng.behavior_engine is None:
        raise HTTPException(404, "Behavior engine not loaded")
    eng.behavior_engine.push_goal(cmd.name, cmd.priority, **cmd.params)
    return ok({"goal": cmd.name, "priority": cmd.priority})


@app.post("/plugins/{name}/enable")
@app.post("/api/v1/plugins/{name}/enable")
async def enable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.enable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()

@app.post("/plugins/{name}/disable")
@app.post("/api/v1/plugins/{name}/disable")
async def disable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.disable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()

@app.delete("/plugins/{name}")
@app.delete("/api/v1/plugins/{name}")
async def unload_plugin(name: str):
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    success = await plugin_manager.unload_plugin(name)
    if not success:
        raise HTTPException(404, f"Plugin '{name}' not found")
    return ok()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_manager.add(ws)
    logger.info("WebSocket client connected (total: %d)", ws_manager.count)
    try:
        if bridge:
            state = await bridge.get_state()
            await ws.send_text(json.dumps(_ws_envelope("state", state.to_dict())))

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                data = json.loads(msg)
                await _handle_ws_command(ws, data)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps(_ws_envelope("ping")))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        ws_manager.remove(ws)
        logger.info("WebSocket client disconnected (total: %d)", ws_manager.count)


async def _handle_ws_command(ws: WebSocket, data: dict) -> None:
    cmd = data.get("cmd")

    async def _err(msg: str) -> None:
        await ws.send_text(json.dumps(_ws_envelope(
            "error",
            {"cmd": cmd, "message": msg}
        )))

    async def _ack(status: str = "accepted", **payload) -> None:
        await ws.send_text(json.dumps(_ws_envelope(
            "command_ack",
            {"cmd": cmd, "status": status, **payload}
        )))

    def _float(key: str, default: float = 0.0, lo: float = -1e9, hi: float = 1e9) -> float | None:
        try:
            v = float(data.get(key, default))
            return max(lo, min(hi, v))
        except (TypeError, ValueError):
            return None

    if cmd == "move":
        if watchdog and watchdog.estop_active:
            await _err("E-stop active")
            return
        vx   = _float("vx",   0.0, -1.5, 1.5)
        vy   = _float("vy",   0.0, -0.8, 0.8)
        vyaw = _float("vyaw", 0.0, -2.0, 2.0)
        if vx is None or vy is None or vyaw is None:
            await _err("move: vx, vy, vyaw must be numbers")
            return
        watchdog.ping_heartbeat()
        await bridge.move(vx, vy, vyaw)
        await _ack(vx=vx, vy=vy, vyaw=vyaw)

    elif cmd == "stop":
        if watchdog:
            watchdog.ping_heartbeat()
        await bridge.stop_move()
        await _ack()

    elif cmd == "estop":
        if watchdog:
            await watchdog.trigger_estop("WebSocket client")
        await _ack(estop_active=True)

    elif cmd == "sport_mode":
        if watchdog and watchdog.estop_active:
            await _err("E-stop active")
            return
        mode_str = data.get("mode", "")
        if not isinstance(mode_str, str):
            await _err("sport_mode: 'mode' must be a string")
            return
        try:
            mode = SportMode(mode_str)
        except ValueError:
            await _err(f"Unknown sport mode: '{mode_str}'")
            return
        if watchdog:
            watchdog.ping_heartbeat()
        await bridge.execute_sport_mode(mode)
        await _ack(mode=mode.value)

    elif cmd == "body_height":
        if watchdog and watchdog.estop_active:
            await _err("E-stop active")
            return
        height = _float("height", 0.0, -0.1, 0.1)
        if height is None:
            await _err("body_height: 'height' must be a number in [-0.1, 0.1]")
            return
        await bridge.set_body_height(height)
        await _ack(height=height)

    elif cmd == "led":
        r = _float("r", 0, 0, 255)
        g = _float("g", 0, 0, 255)
        b = _float("b", 0, 0, 255)
        if r is None or g is None or b is None:
            await _err("led: r, g, b must be integers 0–255")
            return
        await bridge.set_led(int(r), int(g), int(b))
        await _ack(rgb=[int(r), int(g), int(b)])

    elif cmd == "subscribe":
        await _ack(status="subscribed")

    else:
        await _err(f"Unknown command: '{cmd}'")


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
