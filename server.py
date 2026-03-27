"""
CERBERUS Backend API Server
============================
Optional REST + WebSocket layer for external integrations.

Endpoints:
  GET  /health             — liveness probe
  GET  /state              — current UIState as JSON
  POST /command            — send UI_COMMAND event
  WS   /ws                 — subscribe to live state stream (30Hz push)

The server does NOT own the runtime or bus.  It receives them via
app.state after the runtime is started in main.py.

This is intentionally thin — the real logic lives in the runtime + plugins.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class CommandRequest(BaseModel):
    command: str
    payload: dict[str, Any] = {}


def create_app(bridge: Any = None, runtime: Any = None) -> FastAPI:
    """
    Factory function.  Pass the bridge and runtime from main.py.
    If called standalone (e.g. uvicorn direct), they are None and the
    server runs in a degraded state (returns empty state).
    """
    app = FastAPI(
        title   = "CERBERUS API",
        version = "2.0.0",
        docs_url = "/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins     = ["http://localhost:*", "http://127.0.0.1:*"],
        allow_methods     = ["GET", "POST"],
        allow_headers     = ["*"],
    )

    _bridge  = bridge
    _runtime = runtime
    _ws_clients: list[WebSocket] = []

    # ── HTTP endpoints ─────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "cerberus"}

    @app.get("/state")
    async def get_state() -> JSONResponse:
        if _bridge is None:
            return JSONResponse({"error": "bridge not initialised"}, status_code=503)
        state = _bridge.get_state()
        return JSONResponse(state.__dict__)

    @app.post("/command")
    async def post_command(req: CommandRequest) -> dict:
        if _bridge is None:
            return {"error": "bridge not initialised"}
        _bridge.send_command(req.command, **req.payload)
        return {"ok": True, "command": req.command}

    # ── WebSocket ──────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        _ws_clients.append(ws)
        logger.info("WS client connected (%d total)", len(_ws_clients))
        try:
            while True:
                if _bridge:
                    state = _bridge.get_state()
                    await ws.send_text(json.dumps(state.__dict__))

                # Also handle incoming commands from WS clients
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=0.033)
                    data = json.loads(raw)
                    cmd  = data.get("command")
                    if cmd and _bridge:
                        _bridge.send_command(cmd, **data.get("payload", {}))
                except TimeoutError:
                    pass

        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("WS error: %s", e)
        finally:
            _ws_clients.remove(ws)
            logger.info("WS client disconnected (%d remaining)", len(_ws_clients))

    return app


def main() -> None:
    """Entry point for 'go2-server' CLI command."""
    import uvicorn
    uvicorn.run(
        "backend.api.server:create_app",
        factory = True,
        host    = "0.0.0.0",
        port    = 8080,
        workers = 1,
        log_level = "info",
    )
