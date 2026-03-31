"""
backend/main.py
CERBERUS FastAPI Application
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import inspect
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cerberus import __version__
from cerberus.bridge.go2_bridge import create_bridge, SportMode
from cerberus.core.auth import require_api_key
from cerberus.cognitive.session_store import SessionStore
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.cognitive.behavior_engine import BehaviorEngine, PersonalityTraits
from cerberus.anatomy.kinematics import DigitalAnatomy
from cerberus.plugins.plugin_manager import PluginManager

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class WebSocketManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    def add(self, ws: WebSocket) -> None:
        self._clients.append(ws)

    def remove(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, msg: str) -> None:
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
        await self.broadcast(json.dumps(_ws_envelope(type_, data)))

    @property
    def count(self) -> int:
        return len(self._clients)


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
    msg = {"type": type_, "ts": _ws_now(), "seq": _next_ws_seq()}
    if data is not None:
        msg["data"] = data
    msg.update(extra)
    return msg


def _plugin_actions(plugin: Any) -> list[dict]:
    actions: list[dict] = []
    seen: set[str] = set()
    for attr_name in dir(plugin):
        if attr_name.startswith("_"):
            continue
        attr = getattr(plugin, attr_name, None)
        if not callable(attr):
            continue
        if attr_name in {"execute", "handle_execute"}:
            sig = str(inspect.signature(attr)) if callable(attr) else "()"
            actions.append({"name": attr_name, "dispatch": "generic", "signature": sig})
            seen.add(attr_name)
            continue
        if attr_name.startswith("execute_"):
            action_name = attr_name.removeprefix("execute_")
            sig = str(inspect.signature(attr))
            actions.append({"name": action_name, "dispatch": attr_name, "signature": sig})
            seen.add(action_name)
            continue
        if attr_name in {"start", "stop", "status", "enable", "disable"}:
            sig = str(inspect.signature(attr))
            actions.append({"name": attr_name, "dispatch": attr_name, "signature": sig})
            seen.add(attr_name)
    return sorted(actions, key=lambda a: a["name"])


def _plugin_descriptor(name: str, rec: Any) -> dict:
    plugin = rec.plugin
    manifest = getattr(plugin, "MANIFEST", None)
    descriptor = {
        "name": name,
        "class_name": plugin.__class__.__name__,
        "enabled": getattr(rec, "enabled", True),
        "actions": _plugin_actions(plugin),
    }
    if manifest is not None:
        descriptor["manifest"] = {
            "name": getattr(manifest, "name", None),
            "version": getattr(manifest, "version", None),
            "author": getattr(manifest, "author", None),
            "description": getattr(manifest, "description", None),
            "capabilities": sorted(list(getattr(manifest, "capabilities", []) or [])),
            "trust": str(getattr(manifest, "trust", None).value if getattr(manifest, "trust", None) is not None and hasattr(getattr(manifest, "trust", None), "value") else getattr(manifest, "trust", None)),
        }
    return descriptor


def _plugin_catalog() -> list[dict]:
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    out: list[dict] = []
    for name, rec in plugin_manager._plugins.items():
        out.append(_plugin_descriptor(name, rec))
    return sorted(out, key=lambda p: p["name"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bridge, engine, watchdog, plugin_manager
    _store = SessionStore()
    saved_traits, saved_stats = _store.load()
    bridge = create_bridge()
    limits = SafetyLimits(heartbeat_timeout_s=float(os.getenv("HEARTBEAT_TIMEOUT", "5.0")))
    watchdog = SafetyWatchdog(bridge, limits)
    engine = CerberusEngine(bridge, watchdog, target_hz=float(os.getenv("CERBERUS_HZ", "60")))
    env_personality = PersonalityTraits(
        energy=float(os.getenv("PERSONALITY_ENERGY", str(saved_traits.energy))),
        friendliness=float(os.getenv("PERSONALITY_FRIENDLINESS", str(saved_traits.friendliness))),
        curiosity=float(os.getenv("PERSONALITY_CURIOSITY", str(saved_traits.curiosity))),
        loyalty=float(os.getenv("PERSONALITY_LOYALTY", str(saved_traits.loyalty))),
        playfulness=float(os.getenv("PERSONALITY_PLAYFULNESS", str(saved_traits.playfulness))),
    )
    engine.behavior_engine = BehaviorEngine(bridge, env_personality)
    engine.behavior_engine._session_stats = saved_stats
    engine.anatomy = DigitalAnatomy()
    plugin_dirs = os.getenv("PLUGIN_DIRS", "plugins").split(":")
    plugin_manager = PluginManager(engine, plugin_dirs)
    await plugin_manager.discover_and_load()
    plugin_manager.register_with_engine()
    await engine.start()
    logger.info("CERBERUS API ready — session #%d", saved_stats.session_number)
    yield
    logger.info("CERBERUS API shutting down")
    if engine.behavior_engine is not None:
        _store.save(engine.behavior_engine)
    await engine.stop()


app = FastAPI(title="CERBERUS — Unitree Go2 Companion API", version=__version__, description="Cognitive, adaptive, canine-emulative companion system for the Unitree Go2", lifespan=lifespan, dependencies=[Depends(require_api_key)])

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return HTMLResponse((_STATIC_DIR / "dashboard.html").read_text())

app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000").split(","), allow_credentials=True, allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Authorization", "X-CERBERUS-Key"])


def _require_engine() -> CerberusEngine:
    if engine is None:
        raise HTTPException(503, "Engine not initialized")
    return engine


def _require_no_estop() -> None:
    if watchdog and watchdog.estop_active:
        raise HTTPException(503, "Emergency stop active — clear E-stop first")


def ok(data: dict | None = None) -> dict:
    return {"ok": True, **(data or {})}


class PluginExecuteCmd(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)
    request_id: str | None = None


def _plugin_registry_record(name: str):
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    rec = plugin_manager._plugins.get(name)
    if rec is not None:
        return rec
    for key, value in plugin_manager._plugins.items():
        manifest = getattr(value.plugin, "MANIFEST", None)
        manifest_name = getattr(manifest, "name", None)
        if key == name or manifest_name == name or value.plugin.__class__.__name__ == name:
            return value
    raise HTTPException(404, f"Plugin '{name}' not found")


async def _ws_plugin_status(name: str, phase: str, payload: dict) -> None:
    await ws_manager.broadcast_json("plugin_status", {"plugin": name, "phase": phase, **payload})


async def _execute_plugin_action(name: str, cmd: PluginExecuteCmd) -> dict:
    rec = _plugin_registry_record(name)
    plugin = rec.plugin
    if hasattr(rec, "enabled") and not rec.enabled:
        raise HTTPException(409, f"Plugin '{name}' is disabled")
    action = cmd.action.strip()
    if not action:
        raise HTTPException(422, "Plugin execute action cannot be empty")
    candidate_names = [action, f"execute_{action}", "execute", "handle_execute"]
    method = None
    selected_name = None
    for candidate in candidate_names:
        attr = getattr(plugin, candidate, None)
        if callable(attr):
            method = attr
            selected_name = candidate
            break
    if method is None:
        raise HTTPException(404, f"Plugin '{name}' does not expose executable action '{action}'")
    robot_affecting_actions = {"move", "stand_up", "stand_down", "sport_mode", "body_height", "gait", "led"}
    if action in robot_affecting_actions:
        _require_no_estop()
        if watchdog:
            watchdog.ping_heartbeat()
    await _ws_plugin_status(name, "started", {"action": action, "request_id": cmd.request_id})
    try:
        if selected_name in ("execute", "handle_execute"):
            result = method(action=action, params=cmd.params, request_id=cmd.request_id)
        else:
            result = method(**cmd.params)
        if asyncio.iscoroutine(result):
            result = await asyncio.wait_for(result, timeout=float(os.getenv("PLUGIN_EXECUTE_TIMEOUT_S", "15")))
        normalized = result if isinstance(result, dict) else {"value": result}
        await _ws_plugin_status(name, "completed", {"action": action, "request_id": cmd.request_id, "ok": True})
        return {"ok": True, "result": normalized}
    except asyncio.TimeoutError:
        await _ws_plugin_status(name, "failed", {"action": action, "request_id": cmd.request_id, "ok": False, "error": "timeout"})
        return {"ok": False, "error": {"code": "timeout", "message": f"Plugin '{name}' action '{action}' timed out"}}
    except HTTPException:
        raise
    except Exception as exc:
        await _ws_plugin_status(name, "failed", {"action": action, "request_id": cmd.request_id, "ok": False, "error": str(exc)})
        return {"ok": False, "error": {"code": "execution_error", "message": str(exc)}}


@app.get("/health")
@app.get("/api/v1/system/health")
async def health():
    return {"status": "healthy", "service": "CERBERUS", "version": __version__}


@app.get("/ready")
@app.get("/api/v1/system/ready")
async def ready():
    if engine is None or engine.state.value not in ("running",):
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "engine not running" if engine else "engine not initialised"})
    return {"status": "ready", "engine_hz": round(engine.stats.tick_hz, 1)}


@app.get("/")
@app.get("/api/v1/system/info")
async def root():
    eng = _require_engine()
    return {"service": "CERBERUS", "version": __version__, "engine_state": eng.state.value, "simulation": os.getenv("GO2_SIMULATION", "false").lower() in ("true", "1"), "safety_level": watchdog.safety_level.value if watchdog else "unknown", "estop_active": watchdog.estop_active if watchdog else False}


@app.get("/plugins")
@app.get("/api/v1/plugins")
async def list_plugins():
    return _plugin_catalog()


@app.get("/api/v1/plugins/catalog")
async def plugin_catalog():
    return {"plugins": _plugin_catalog()}


@app.get("/api/v1/plugins/{name}")
async def plugin_details(name: str):
    rec = _plugin_registry_record(name)
    resolved = getattr(getattr(rec.plugin, "MANIFEST", None), "name", None) or name
    return _plugin_descriptor(resolved, rec)


@app.post("/plugins/{name}/enable")
@app.post("/api/v1/plugins/{name}/enable")
async def enable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.enable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    await _ws_plugin_status(name, "enabled", {"ok": True})
    return ok()


@app.post("/plugins/{name}/disable")
@app.post("/api/v1/plugins/{name}/disable")
async def disable_plugin(name: str):
    if plugin_manager is None or not plugin_manager.disable(name):
        raise HTTPException(404, f"Plugin '{name}' not found")
    await _ws_plugin_status(name, "disabled", {"ok": True})
    return ok()


@app.delete("/plugins/{name}")
@app.delete("/api/v1/plugins/{name}")
async def unload_plugin(name: str):
    if plugin_manager is None:
        raise HTTPException(503, "Plugin manager not ready")
    success = await plugin_manager.unload_plugin(name)
    if not success:
        raise HTTPException(404, f"Plugin '{name}' not found")
    await _ws_plugin_status(name, "unloaded", {"ok": True})
    return ok()


@app.post("/api/v1/plugins/{name}/execute")
async def execute_plugin(name: str, cmd: PluginExecuteCmd):
    return await _execute_plugin_action(name, cmd)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_manager.add(ws)
    try:
        await ws.send_text(json.dumps(_ws_envelope("plugin_catalog", {"plugins": _plugin_catalog()})))
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                data = json.loads(msg)
                await _handle_ws_command(ws, data)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps(_ws_envelope("ping")))
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.remove(ws)


async def _handle_ws_command(ws: WebSocket, data: dict) -> None:
    cmd = data.get("cmd")

    async def _err(msg: str) -> None:
        await ws.send_text(json.dumps(_ws_envelope("error", {"cmd": cmd, "message": msg})))

    if cmd == "subscribe":
        await ws.send_text(json.dumps(_ws_envelope("command_ack", {"cmd": cmd, "status": "subscribed"})))
    elif cmd == "plugin_execute":
        name = data.get("name")
        action = data.get("action")
        params = data.get("params") or {}
        request_id = data.get("request_id")
        if not isinstance(name, str) or not isinstance(action, str):
            await _err("plugin_execute requires string 'name' and 'action'")
            return
        result = await _execute_plugin_action(name, PluginExecuteCmd(action=action, params=params if isinstance(params, dict) else {}, request_id=request_id if isinstance(request_id, str) else None))
        await ws.send_text(json.dumps(_ws_envelope("plugin_execute_result", {"plugin": name, "action": action, **result})))
    elif cmd == "plugin_catalog":
        await ws.send_text(json.dumps(_ws_envelope("plugin_catalog", {"plugins": _plugin_catalog()})))
    else:
        await ws.send_text(json.dumps(_ws_envelope("command_ack", {"cmd": cmd, "status": "ignored"})))


def main():
    import uvicorn
    uvicorn.run("backend.main:app", host=os.getenv("GO2_API_HOST", "0.0.0.0"), port=int(os.getenv("GO2_API_PORT", "8080")), reload=os.getenv("DEV_RELOAD", "false").lower() == "true", log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
