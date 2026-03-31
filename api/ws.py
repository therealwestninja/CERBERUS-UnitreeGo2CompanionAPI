from fastapi import APIRouter, WebSocket

router = APIRouter()

@router.websocket("/ws/telemetry")
async def telemetry_ws(ws: WebSocket):
    await ws.accept()
    while True:
        await ws.send_json({"type": "heartbeat"})
