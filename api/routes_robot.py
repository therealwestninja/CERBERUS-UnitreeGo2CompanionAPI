from fastapi import APIRouter

router = APIRouter()

@router.get("/robot/state")
def get_state():
    return {"state": "unknown", "connected": False}

@router.post("/robot/cmd_vel")
def cmd_vel(x: float, y: float, yaw: float):
    return {"ok": True, "cmd": [x, y, yaw]}

@router.post("/robot/stop")
def stop():
    return {"ok": True}

@router.post("/robot/emergency_stop")
def estop():
    return {"ok": True, "estop": True}

@router.post("/robot/action")
def action(action: str, params: dict = {}):
    return {"ok": True, "action": action, "params": params}
