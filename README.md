# Go2 Platform — Production Robotics Platform

**Version:** 2.0.0 | **Stack:** Python 3.11 · FastAPI · ROS2 Kilted · HTML5 UI

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          UI LAYER  (client only)                        │
│  go2_platform/ui/index.html                                             │
│   ├─ 6 tabs: Home / Manual / Tricks / Objects / Missions / Settings     │
│   ├─ Minimal mode (E-STOP + ARM only)                                   │
│   ├─ Real-time wireframe robot + telemetry                              │
│   ├─ Joystick + body control                                            │
│   └─ REST fetch + WebSocket consumer ONLY — zero robot logic            │
└──────────────┬──────────────────────────────────────────────────────────┘
               │  REST  /api/v1/*       (HTTPS in production)
               │  WS    /ws             (commands + telemetry stream)
┌──────────────▼──────────────────────────────────────────────────────────┐
│                      BACKEND PLATFORM  (authoritative)                  │
│  backend/api/server.py  (FastAPI)                                       │
│   ├─ REST: commands, objects, behaviors, missions, plugins, safety      │
│   ├─ WS hub: telemetry push, FSM events, detection overlays             │
│   └─ SecurityManager: rate limit → sanitize → validate → audit         │
│                                                                         │
│  backend/core/platform.py  (PlatformCore)                               │
│   ├─ EventBus          internal pub/sub                                 │
│   ├─ AuthoritativeFSM  validated state machine (11 states)              │
│   ├─ SafetyEnforcer    tri-redundant, ALWAYS final authority            │
│   ├─ WorldModel        objects, zones, waypoints, memory                │
│   ├─ BehaviorRegistry  12+ built-in + plugin-extensible                 │
│   └─ MissionSystem     patrol/follow/inspect/sequence                  │
│                                                                         │
│  backend/core/plugin_system.py  (PluginSystem)                          │
│   ├─ Manifest validation → sandboxed load → permission-gated context   │
│   └─ OTA: download → verify → backup → apply → rollback                │
│                                                                         │
│  backend/core/fleet_and_ota.py  (FleetManager)                          │
│   ├─ N robots, shared tasks, sync choreography                         │
│   └─ OTA updates with SHA-256 chain verification                       │
│                                                                         │
│  backend/core/security.py  (SecurityManager)                            │
│   ├─ InputSanitizer: HTML/SQL/path-traversal/null-byte rejection       │
│   ├─ CommandValidator: allowlist, numeric bounds, ID format            │
│   ├─ RateLimiter: token buckets per (client, endpoint)                 │
│   └─ AuditLog: HMAC-chained tamper-evident event log                  │
└──────────────┬──────────────────────────────────────────────────────────┘
               │  asyncio event bus  /  direct calls
┌──────────────▼──────────────────────────────────────────────────────────┐
│                  SIMULATION ENGINE  /  ROS2 BRIDGE                      │
│                                                                         │
│  SIM MODE: backend/sim/simulation_engine.py                             │
│   ├─ 200Hz kinematic simulation (12-DOF trot gait)                     │
│   ├─ Battery drain, thermal model, foot forces                         │
│   ├─ Synthetic LiDAR (360°) + obstacle field                           │
│   ├─ Simulated object detection (YOLO proxy)                           │
│   └─ Feeds realistic Telemetry directly to SafetyEnforcer              │
│                                                                         │
│  HW MODE: backend/ros2_bridge/bridge_node.py                           │
│   ├─ ROS2 Lifecycle node                                               │
│   ├─ Hardware abstraction: Go2 Air/Pro/EDU differences                 │
│   ├─ PD impedance control: τ = Kp(q_des−q) − Kd·dq                   │
│   └─ Unitree SDK2 LowCmd (12 joints, 500Hz)                           │
└──────────────┬──────────────────────────────────────────────────────────┘
               │  ROS2 topics / Unitree SDK2
┌──────────────▼──────────────────────────────────────────────────────────┐
│                        UNITREE GO2 HARDWARE                             │
│   12× Joints · IMU · LiDAR · RealSense Camera · Foot Force (EDU)       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow

```
UI click "SIT"
  → POST /api/v1/command  {action: "SIT"}
  → SecurityManager.validate_command()     ← schema + sanitize + rate check
  → PlatformCore.execute_command()
  → SafetyEnforcer.evaluate()              ← pitch/roll/force/battery check
  → AuthoritativeFSM.transition(SITTING)   ← validates allowed transitions
  → EventBus.emit('fsm.transition')
  → [SIM] SimEngine.set_velocity(0,0,0)
  → [HW]  BridgeNode._publish_lowcmd(SIT_POSE)
  → Robot moves
  → Telemetry → SafetyEnforcer → WS broadcast → UI updates
```

---

## File Structure

```
go2_platform/
├── backend/
│   ├── api/
│   │   └── server.py            FastAPI REST + WebSocket hub
│   ├── core/
│   │   ├── platform.py          PlatformCore — all authoritative logic
│   │   ├── plugin_system.py     Sandboxed plugin lifecycle + OTA
│   │   ├── fleet_and_ota.py     Fleet manager + OTA update system
│   │   └── security.py          Input validation + audit log
│   ├── ros2_bridge/
│   │   └── bridge_node.py       ROS2 ↔ Platform bridge (Lifecycle node)
│   └── sim/
│       └── simulation_engine.py 200Hz physics simulation
├── plugins/
│   └── examples/
│       └── plugins_examples.py  FunScript, Fleet, Navigation plugins
├── ui/
│   └── index.html               Single-file UI (no framework, no build step)
├── tests/
│   └── test_platform.py         111-test suite (all passing)
├── config/
│   └── platform_config.yaml     Runtime configuration
└── docs/
    └── README.md                This file
```

---

## Quick Start

### Browser-Only (Simulation, no install)

```bash
open ui/index.html
```

Full simulation runs in-browser. No server, no robot required.

---

### Full Backend (Python)

```bash
pip install fastapi uvicorn[standard] pyyaml aiohttp websockets
cd go2_platform
python -m uvicorn backend.api.server:create_app --factory --port 8080
# Open ui/index.html → Settings → set API URL to http://localhost:8080
```

---

### With ROS2 Hardware Bridge

```bash
# Prerequisites: ROS2 Kilted + unitree_ros2
pip install rclpy
ros2 run go2_platform bridge_node

# Launch full system:
ros2 launch go2_control go2_system.launch.py use_sim:=false robot_ip:=192.168.12.1
```

---

## REST API Reference

All state-changing endpoints go through `PlatformCore.execute_command()`.
Safety + FSM validation is enforced below the API layer.

### Core

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/health` | Health check |
| GET  | `/api/v1/status` | Full platform status |
| GET  | `/api/v1/telemetry` | Latest telemetry |
| GET  | `/api/v1/fsm` | FSM state |
| GET  | `/api/v1/safety` | Safety status |
| POST | `/api/v1/command` | Execute command (validated) |
| POST | `/api/v1/estop` | Emergency stop (bypass rate limit) |
| POST | `/api/v1/estop/clear` | Clear E-STOP |
| POST | `/api/v1/arm` | Arm system |
| POST | `/api/v1/disarm` | Disarm system |
| PATCH| `/api/v1/safety/config` | Update safety thresholds |

### Objects

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/v1/objects` | List all objects |
| GET  | `/api/v1/objects/{id}` | Get one object |
| POST | `/api/v1/objects` | Register object (validated) |
| DELETE | `/api/v1/objects/{id}` | Remove object |
| GET  | `/api/v1/objects/export/json` | Export registry |
| POST | `/api/v1/objects/import/json` | Import registry |

### Behaviors & Missions

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/v1/behaviors` | List behaviors |
| POST | `/api/v1/behaviors/{id}/run` | Execute behavior |
| POST | `/api/v1/behaviors/policy` | Set motion policy |
| GET  | `/api/v1/missions` | List missions |
| POST | `/api/v1/missions` | Create mission |
| POST | `/api/v1/missions/{id}/start` | Start mission |
| POST | `/api/v1/missions/stop` | Stop active mission |

### Plugins

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/v1/plugins` | List plugins + panels |
| POST | `/api/v1/plugins/{name}/activate` | Activate plugin |
| POST | `/api/v1/plugins/{name}/deactivate` | Deactivate plugin |
| DELETE | `/api/v1/plugins/{name}` | Unload plugin |

---

## WebSocket Events

Connect: `ws://localhost:8080/ws`

### Server → Client

| Type | Frequency | Description |
|------|-----------|-------------|
| `init` | Once | Full initial state |
| `telemetry` | 5Hz | Battery, IMU, forces, temps |
| `fsm` | On change | FSM state transition |
| `estop` | On trigger | E-STOP event |
| `safety_trip` | On trip | Safety limit breach |
| `behavior_start` | On start | Behavior execution started |
| `detections` | 2Hz | Object detection results |
| `lidar` | 5Hz | LiDAR scan (90 points) |
| `mission.progress` | On update | Mission progress |

### Client → Server

```json
{"type": "command", "data": {"action": "SIT"}}
{"type": "command", "data": {"action": "RUN_BEHAVIOR", "behavior_id": "tail_wag"}}
{"type": "ping"}
```

---

## FSM State Machine

```
OFFLINE → IDLE → STANDING ⇔ SITTING
                      ↓
               WALKING ⇔ FOLLOWING
                      ↓
                  NAVIGATING
                      ↓
               INTERACTING / PERFORMING / PATROLLING
                      ↓
                  FAULT → IDLE
                  ESTOP → IDLE (after clear)
```

All transitions validated. Requires `armed=True` for motion states.
Safety override immediately transitions to FAULT/IDLE.

---

## Safety Architecture (ISO/TS 15066)

```
Planner → FSM → SafetyEnforcer → ROS2 Bridge → Hardware
                      ↑
              (ALWAYS final authority)
```

### Reflex layer checks (every command):
- Pitch > ±10° → TRIP
- Roll > ±10° → TRIP
- Contact force > 30N → TRIP
- Battery < 10% → TRIP
- Motor temp > 72°C → TRIP
- Heartbeat timeout > 2s → TRIP
- Human in zone → BLOCK motion
- Obstacle < 0.25m → BLOCK navigation
- Velocity > 1.5 m/s → CLAMP

### E-STOP channels:
1. UI button
2. REST POST `/api/v1/estop`
3. WebSocket `{type: "command", data: {action: "ESTOP"}}`
4. Safety automatic (threshold breach)
5. Fleet manager broadcast
6. BLE controller byte 0x01
7. Geofence WiFi signal loss

---

## Plugin System

```python
# manifest.json
{
  "name": "my_plugin",
  "version": "1.0.0",
  "permissions": ["behaviors", "ui", "fsm"],
  "entry_point": "plugin.py"
}

# plugin.py
async def init(ctx):
    ctx.register_behavior({
        "id": "my_trick", "name": "My Trick",
        "category": "custom", "icon": "🎭", "duration_s": 2.0
    })
    ctx.register_ui_panel({"id": "my_panel", "title": "My Plugin", "icon": "🎭"})
    await ctx.on_fsm_transition(my_callback)

def teardown(ctx):
    pass
```

Permissions: `ui` · `behaviors` · `api` · `fsm` · `sensors` · `world` · `missions`

---

## Security Model

All inputs pass through 4 layers:

1. **Schema validation** — Pydantic models on all API endpoints
2. **Sanitization** — HTML/SQL/path traversal/null-byte rejection
3. **Allowlist** — Only 23 explicitly defined actions permitted
4. **Numeric bounds** — All safety-critical values clamped before execution
5. **Rate limiting** — Token buckets per (client, endpoint)
6. **Audit log** — HMAC-chained tamper-evident event log

Secrets (API keys, tokens) are never logged.

---

## Testing

```bash
python tests/test_platform.py
# 111 tests: EventBus, Safety, FSM, WorldModel, Security, Plugins,
#            Simulation math, Fleet, Platform integration, Performance
```

Coverage:
- Safety monitor (15 tests)
- FSM transitions (11 tests)
- World model CRUD (9 tests)
- Input sanitizer (9 tests)
- Command validator (10 tests)
- Rate limiter (4 tests)
- Audit log (5 tests)
- Object import validator (7 tests)
- Plugin manifest validator (6 tests)
- Simulation kinematics (5 tests)
- Fleet + sync (6 tests)
- Platform integration (5 tests)
- Performance benchmarks (4 tests)

---

## Changelog vs Baseline (v1.0)

| Category | v1.0 | v2.0 |
|----------|------|------|
| Architecture | UI app with robot logic | Platform: UI→API→Backend→ROS2 |
| Safety | Client-side checks | Backend SafetyEnforcer, always authoritative |
| FSM | Dashboard state | AuthoritativeFSM with validated transitions |
| Security | None | 5-layer: sanitize+allowlist+bounds+ratelimit+audit |
| Plugins | None | First-class: sandboxed, permission-gated, OTA |
| Simulation | Simple JS loop | 200Hz kinematic engine, thermal+battery models |
| Fleet | Single robot | N robots, sync choreography, broadcast E-STOP |
| OTA | None | Checksum-verified updates with rollback |
| Tests | 74 tests | 111 tests (+security, +fleet, +integration) |
| UI aesthetic | Industrial dark HUD | Warm, friendly, pet-like companion |
| World model | JSON objects only | Objects + zones + waypoints + memory |
| Missions | FSM sequences | patrol/follow/inspect/sequence + conditional |

---

## Hardware Notes

### Go2 Air (SDK unlock required)
Flash custom firmware for SDK2 Ethernet access.
No foot force sensors; use pressure pads externally.

### Go2 Pro
Same as Air firmware requirements; higher motor speed.

### Go2 EDU (recommended)
SDK2 native. Jetson Orin NX onboard. Foot force sensors. RealSense D435i.
Run all backend nodes directly on robot.

---

## License
MIT · Unitree SDK2: Unitree License · ROS2: Apache-2.0
