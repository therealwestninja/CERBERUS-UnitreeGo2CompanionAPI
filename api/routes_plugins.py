from fastapi import APIRouter

router = APIRouter()

@router.get("/plugins")
def list_plugins():
    return {"plugins": []}

@router.post("/plugins/{plugin_id}/execute")
def execute_plugin(plugin_id: str, payload: dict):
    return {"plugin": plugin_id, "status": "ok"}
