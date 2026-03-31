from fastapi import FastAPI
from api.routes_health import router as health_router
from api.routes_robot import router as robot_router
from api.routes_plugins import router as plugin_router
from api.routes_system import router as system_router
from api.ws import router as ws_router

app = FastAPI(title="CERBERUS Companion API")

app.include_router(health_router, prefix="/api/v1")
app.include_router(robot_router, prefix="/api/v1")
app.include_router(plugin_router, prefix="/api/v1")
app.include_router(system_router, prefix="/api/v1")
app.include_router(ws_router)

@app.get("/")
def root():
    return {"status": "CERBERUS API online"}
