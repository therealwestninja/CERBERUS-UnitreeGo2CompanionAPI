# 🐕 Go2 Platform

> **A production-grade, modular robotics platform and extensible API ecosystem for the Unitree Go2 quadruped robot** — built on FastAPI, ROS2, and a warm companion-app UI. Safety-first architecture, plugin system, fleet coordination, 200Hz physics simulation, and AI-powered behavior generation.

[![Tests](https://img.shields.io/badge/tests-111%20passing-brightgreen)](#testing)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#installation)
[![License](https://img.shields.io/badge/license-MIT-blue)](#license)
[![ROS2](https://img.shields.io/badge/ROS2-Kilted-orange)](#ros2-setup)

---

## Table of Contents

- [What Is This?](#what-is-this)
- [Features](#features)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage Guide](#usage-guide)
- [Configuration Guide](#configuration-guide)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [Plugin System](#plugin-system)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## What Is This?

Go2 Platform is **not a UI app**. It is a layered robotics **platform**:

```
UI (companion app)         ← warm, pet-like — zero robot logic
    ↓  REST + WebSocket
Backend Platform           ← authoritative: FSM, safety, missions, plugins
    ↓  asyncio event bus
Simulation / ROS2 Bridge   ← 200Hz physics sim or real ROS2 nodes
    ↓  Unitree SDK2
Go2 Hardware               ← 12-DOF quadruped, IMU, LiDAR, camera
```

Safety enforcement lives at the **platform layer** — never in the UI, never bypassed by the API. The E-STOP always works regardless of authentication state or load.

---

## Features

### 🛡️ Safety First (ISO/TS 15066 aligned)
- **Tri-redundant E-STOP**: UI button, REST POST, WebSocket command, automatic threshold breach, fleet broadcast
- **Reflex layer**: every command evaluated before execution — pitch/roll/force/battery/temp/obstacle/velocity
- **Human zone detection** blocks interaction commands when a person is nearby
- **Watchdog timer**: auto-safe if telemetry stream stops for > 2s
- **5-layer security**: schema validation → sanitization → allowlist → numeric bounds → rate limiting
- **HMAC-chained audit log** — tamper-evident event history, secrets never logged

### 🤖 Robot Control
- **11-state FSM** with validated transition table (OFFLINE → IDLE → STANDING → WALKING → NAVIGATING → INTERACTING → PERFORMING → PATROLLING → FAULT → ESTOP)
- **4 behavior policies**: smooth / agile / stable / adaptive (runtime-switchable)
- **12+ built-in behaviors**: sit, stand, stretch, head tilt, happy wag, roll over, paw shake, zoomies, play bow, follow, patrol, breathing
- **Body control**: height, roll, yaw, walk speed
- **BD Spot SDK pattern adapter** — familiar command vocabulary mapped to Go2

### 🎥 Physics Simulation
- **200Hz kinematic model** — trot gait, 12-DOF, realistic leg phase locking
- **Battery drain** (current × time) and **thermal model** (power in vs. natural cooling)
- **360° synthetic LiDAR** with configurable obstacle field
- **Simulated YOLO detections** with distance/confidence model
- Fully replaces ROS2 bridge — no hardware needed for dev or demo

### 🧩 Plugin System
- **Sandboxed execution** with permission-gated API surface
- Plugins can: register behaviors, add UI panels, add API routes, hook FSM events, read sensors
- **OTA updates**: SHA-256 verify → backup current → apply → auto-rollback on failure
- Max 10 plugins, 5s init timeout, per-plugin rate limiting

### 🌍 World Model
- **Object registry**: affordances, moods, force limits, 3D positions, versioned schema v2.0
- **Zones**: no-enter, slow, patrol, rest, geofence
- **Waypoints** for navigation missions
- **Import/Export** JSON with full schema validation and injection-safe sanitization

### 🐕 Companion UI
- Warm, friendly, pet-like aesthetic — Fraunces serif + DM Sans, cream/amber palette
- **6 tabs**: Home, Manual, Tricks, Objects, Missions, Settings
- **Minimal mode**: E-STOP + ARM only for phone-sized screens
- **Real-time wireframe robot** with joint animation, foot force colors, COM line
- **Joystick** with full touch support
- **Behavior grid** with category filter and running-state animations
- **AI behavior generator** (Claude API) — mood + object → motion parameters

---

## Quick Start

### Option 1: Browser Only (Zero Install)

```bash
# Open the companion UI — full simulation runs in browser, no server needed
open ui/index.html        # macOS
xdg-open ui/index.html    # Linux
start ui/index.html       # Windows
```

Full simulation, telemetry, behaviors, FunScript, AI generation — all work without Python or a robot.

---

### Option 2: Full Backend

```bash
pip install fastapi "uvicorn[standard]" pyyaml python-dotenv
cp .env.example .env
python -m uvicorn backend.api.server:create_app --factory --port 8080

# In another terminal:
open ui/index.html
# Settings tab → Connection → set API URL to http://localhost:8080
```

Try it immediately:
```bash
curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/v1/arm
curl -X POST http://localhost:8080/api/v1/command \
  -H "Content-Type: application/json" -d '{"action":"SIT"}'
curl -X POST http://localhost:8080/api/v1/estop
```

---

### Option 3: Docker

```bash
docker build -t go2-platform .
docker run -p 8080:8080 -e GO2_MODE=simulation go2-platform
# Open http://localhost:8080/docs for interactive API explorer
```

---

### Option 4: Hardware (Go2 + ROS2)

```bash
# Prerequisites: ROS2 Kilted, unitree_sdk2_python installed
source /opt/ros/kilted/setup.bash
cd ros2_ws && colcon build && source install/setup.bash
ros2 launch go2_control go2_system.launch.py use_sim:=false robot_ip:=192.168.12.1
GO2_MODE=hardware uvicorn backend.api.server:create_app --factory --port 8080
```

---

## Installation

### Requirements

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | Required |
| fastapi | 0.111+ | Web framework |
| uvicorn | 0.29+ | ASGI server |
| pydantic | 2.7+ | Schema validation |
| ROS2 Kilted | optional | Hardware mode |
| Unitree SDK2 | optional | Hardware mode |

### Install

```bash
git clone https://github.com/your-org/go2-platform.git
cd go2-platform

# Production
pip install -e .

# Development (includes testing tools)
pip install -e ".[dev]"

# With vision (YOLOv8 + MediaPipe)
pip install -e ".[vision]"

# With BLE
pip install -e ".[ble]"

cp .env.example .env
```

### Verify

```bash
python tests/test_platform.py
# Expected: 111/111 passed ✓
```

---

## Usage Guide

### Arming

The system requires explicit arming before any motion command. Pre-flight checks are automatic (battery > 10%, no active E-STOP):

```bash
curl -X POST http://localhost:8080/api/v1/arm
# {"ok": true, "msg": "Armed"}
```

### Motion Commands

```bash
# All motion commands follow the same pattern:
curl -X POST http://localhost:8080/api/v1/command \
  -H "Content-Type: application/json" \
  -d '{"action": "STAND"}'

# Available actions (requires armed):
# STAND, SIT, WALK, FOLLOW, NAVIGATE, INTERACT, PERFORM
# RUN_BEHAVIOR (+ "behavior_id": "zoomies")
# BODY_CTRL (+ "height": 0.35, "speed": 0.8)
# SET_POLICY (+ "policy": "AGILE")
```

### WebSocket Real-Time

```javascript
const ws = new WebSocket('ws://localhost:8080/ws');

ws.onopen = () => {
  ws.send(JSON.stringify({type:'command', data:{action:'ARM'}}));
  ws.send(JSON.stringify({type:'command', data:{action:'STAND'}}));
};

ws.onmessage = ({data}) => {
  const msg = JSON.parse(data);
  if (msg.type === 'telemetry') console.log('Battery:', msg.data.battery_pct+'%');
  if (msg.type === 'fsm')       console.log('State:', msg.data.state);
  if (msg.type === 'estop')     console.error('E-STOP triggered!');
};
```

### Running a Mission

```bash
# Create patrol mission
curl -X POST http://localhost:8080/api/v1/missions \
  -d '{"name":"Garden Patrol","type":"patrol","params":{"waypoints":["A","B"],"repeat":true}}'
# → {"ok":true,"mission":{"id":"abc12345",...}}

# Start it
curl -X POST http://localhost:8080/api/v1/missions/abc12345/start
```

### Registering Objects

```bash
curl -X POST http://localhost:8080/api/v1/objects \
  -d '{
    "id":"red_cushion","name":"Red Cushion","type":"soft_prop",
    "affordances":["mount_play","knead"],"moods":["playful","gentle"],
    "max_force_n":20,"pos":{"x":0.5,"y":0,"z":0.4},"contact_normal":[0,0,1]
  }'
```

---

## Configuration Guide

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GO2_MODE` | `simulation` | `simulation` \| `hardware` \| `hybrid` |
| `GO2_HOST` | `0.0.0.0` | Server bind address |
| `GO2_PORT` | `8080` | Server port |
| `GO2_LOG_LEVEL` | `info` | `debug` \| `info` \| `warn` \| `error` |
| `GO2_API_TOKEN` | *(empty)* | Bearer token — **set in production** |
| `GO2_ALLOWED_ORIGINS` | `*` | CORS origins (comma-separated) |
| `GO2_ROBOT_IP` | `192.168.12.1` | Go2 network address |
| `GO2_ROBOT_MODEL` | `edu` | `air` \| `pro` \| `edu` |
| `ANTHROPIC_API_KEY` | *(empty)* | AI behavior generation |

### Production Security

```bash
# Generate a secure token
python -c "import secrets; print(secrets.token_hex(32))"

# Set in .env
GO2_API_TOKEN=your-64-char-hex-token
GO2_ALLOWED_ORIGINS=https://your-dashboard.example.com
```

Then all API calls require:
```bash
curl -H "Authorization: Bearer your-token" http://localhost:8080/api/v1/arm
```
E-STOP never requires a token.

### Safety Thresholds (live update)

```bash
curl -X PATCH http://localhost:8080/api/v1/safety/config \
  -d '{"pitch_limit_deg":12,"force_limit_n":25,"temp_limit_c":70}'
```

| Threshold | Default | Range |
|-----------|---------|-------|
| `pitch_limit_deg` | 10° | 5–30° |
| `roll_limit_deg` | 10° | 5–30° |
| `force_limit_n` | 30N | 5–80N |
| `temp_limit_c` | 72°C | 50–90°C |
| `battery_min_pct` | 10% | 5–25% |
| `watchdog_s` | 2.0s | 0.1–10s |

---

## API Reference

Full interactive docs: `http://localhost:8080/docs` (simulation mode)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | — | Liveness probe |
| GET | `/api/v1/status` | — | Full platform status |
| GET | `/api/v1/telemetry` | — | Latest sensor data |
| GET | `/api/v1/fsm` | — | FSM state + transitions |
| GET | `/api/v1/safety` | — | Safety status |
| POST | `/api/v1/estop` | **never** | Emergency stop |
| POST | `/api/v1/estop/clear` | ✓ | Clear E-STOP |
| POST | `/api/v1/arm` | ✓ | Arm system |
| POST | `/api/v1/disarm` | ✓ | Disarm |
| POST | `/api/v1/command` | ✓ | Execute command |
| PATCH | `/api/v1/safety/config` | ✓ | Update limits |
| GET | `/api/v1/objects` | — | List objects |
| POST | `/api/v1/objects` | ✓ | Register object |
| PUT | `/api/v1/objects/{id}` | ✓ | Update object |
| DELETE | `/api/v1/objects/{id}` | ✓ | Remove object |
| GET | `/api/v1/world/export` | — | Export world |
| POST | `/api/v1/world/import` | ✓ | Import world |
| GET | `/api/v1/behaviors` | — | List behaviors |
| POST | `/api/v1/behaviors/{id}/run` | ✓ | Run behavior |
| POST | `/api/v1/behaviors/policy` | ✓ | Set policy |
| GET | `/api/v1/missions` | — | List missions |
| POST | `/api/v1/missions` | ✓ | Create mission |
| POST | `/api/v1/missions/{id}/start` | ✓ | Start mission |
| POST | `/api/v1/missions/stop` | ✓ | Stop mission |
| GET | `/api/v1/plugins` | — | List plugins |
| POST | `/api/v1/plugins/{name}/activate` | ✓ | Activate plugin |
| DELETE | `/api/v1/plugins/{name}` | ✓ | Unload plugin |
| GET | `/api/v1/security/audit` | ✓ | Audit log |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  ui/index.html  (single-file companion app)                  │
│  No robot logic — REST/WS consumer only                      │
└──────────────────────┬───────────────────────────────────────┘
                       │ REST + WebSocket (:8080)
┌──────────────────────▼───────────────────────────────────────┐
│  backend/api/server.py  (FastAPI)                            │
│  • Bearer auth  • Rate limiting  • Schema validation          │
│  • All mutations → PlatformCore.execute_command()            │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│  backend/core/platform.py  (PlatformCore — authoritative)    │
│  SafetyEnforcer    ← ALWAYS final authority, every command   │
│  AuthoritativeFSM  ← validated transition table              │
│  WorldModel        ← objects + zones + waypoints             │
│  BehaviorRegistry  ← 12 built-in + plugin-extensible         │
│  MissionSystem     ← patrol / follow / inspect / sequence    │
│  EventBus          ← internal async pub/sub                  │
└──────────┬───────────────────────┬───────────────────────────┘
           │ SIM                   │ HW
┌──────────▼──────────┐  ┌─────────▼──────────────────────────┐
│  simulation_engine  │  │  ros2_bridge/bridge_node.py         │
│  200Hz kinematics   │  │  ROS2 Lifecycle node                │
│  Battery + thermal  │  │  PD impedance: τ=Kp(q_des-q)-Kd·dq │
│  LiDAR + detection  │  │  Unitree SDK2 LowCmd (500Hz)        │
└─────────────────────┘  └────────────────────────────────────┘
```

---

## Plugin System

### Minimal Plugin

```
plugins/my_plugin/
  ├── manifest.json
  └── plugin.py
```

`manifest.json`:
```json
{
  "name": "my_plugin",
  "version": "1.0.0",
  "permissions": ["behaviors", "ui"],
  "entry_point": "plugin.py"
}
```

`plugin.py`:
```python
async def init(ctx):
    ctx.register_behavior({
        "id": "my_trick", "name": "My Trick",
        "category": "custom", "icon": "🎭", "duration_s": 2.0
    })
    ctx.register_ui_panel({"id":"my_panel","title":"My Plugin","icon":"🎭"})

def teardown(ctx): pass
```

Activate: `curl -X POST http://localhost:8080/api/v1/plugins/my_plugin/activate`

### Permissions

`ui` • `behaviors` • `api` • `fsm` • `sensors` • `world` • `missions`

---

## Testing

```bash
python tests/test_platform.py   # 111 tests, ~0.5s
make test                        # same via Makefile
```

Coverage: EventBus, SafetyEnforcer, AuthoritativeFSM, WorldModel, BehaviorRegistry, InputSanitizer, CommandValidator, RateLimiter, AuditLog, ObjectImportValidator, PluginManifest, SimulationKinematics, FleetManager, PlatformIntegration, Performance benchmarks.

---

## Troubleshooting

**Can't arm:** Check `GET /api/v1/safety` — look for active E-STOP or low battery. Clear with `POST /api/v1/estop/clear`.

**Safety trip loops:** Widen limit: `PATCH /api/v1/safety/config -d '{"pitch_limit_deg":15}'`

**WebSocket no telemetry:** Verify simulation mode is active: `GET /api/v1/status` → `platform.sim` should be `true`.

**Plugin not loading:** Validate `manifest.json` JSON syntax, check permissions list uses only `ui/behaviors/api/fsm/sensors/world/missions`.

**Rate limited:** Default 30 req/s — for batch testing use the `source` field to distribute across virtual clients.

**Go2 Air/Pro can't connect:** Requires custom firmware for SDK2 access. See [unitreerobotics community](https://github.com/unitreerobotics) for Air unlock procedure.

---

## FAQ

**Can I use this without a robot?** Yes — browser simulation works standalone with no Python.

**Does it support Go2 Air?** Air requires firmware modification for SDK2. EDU has native access. See hardware notes.

**Is ROS2 required?** Only for hardware mode. Simulation and UI work with just Python 3.11+.

**How do I generate AI behaviors?** Settings → AI Behavior Generator → enter Anthropic API key → pick mood + object → Generate → Apply.

**Is auth required?** Optional locally (empty `GO2_API_TOKEN`). Always enable in production — the robot can be armed by any unauthenticated client otherwise.

**Can I run multiple robots?** Yes — FleetManager coordinates N robots with broadcast commands and synchronized choreography.

---

## Roadmap

- [ ] SLAM-based real-time mapping with Nav2
- [ ] 3D robot model viewer (Three.js)
- [ ] WYSIWYG behavior / animation editor
- [ ] Voice command pipeline (wake word + STT)
- [ ] React Native mobile app with BLE direct control
- [ ] Gazebo physics simulation integration
- [ ] Computer vision: live YOLO on robot camera stream
- [ ] WebRTC ultra-low-latency video
- [ ] Plugin marketplace + community registry
- [ ] gRPC transport for time-critical paths
- [ ] OpenAPI auto-generated client SDKs

---

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md). Quick start:

```bash
git clone https://github.com/your-org/go2-platform.git && cd go2-platform
make dev && make test  # 111/111 should pass
```

---

## License

MIT License. See [LICENSE](../LICENSE).

*Go2 Platform is an independent community project, not affiliated with Unitree Robotics or Boston Dynamics.*
