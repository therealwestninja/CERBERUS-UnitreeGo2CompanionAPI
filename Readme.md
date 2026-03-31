# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://img.shields.io/badge/CI-CD-blue)](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions)
[![Version](https://img.shields.io/badge/version-2.8.0-orange)](Changelog.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-522%20passing-brightgreen)](tests/)

CERBERUS is a **fully autonomous, adaptive, and intelligent quadrupedal robotics platform** for the **Unitree Go2**. It combines a **three-layer cognitive engine**, **digital anatomy model**, **safety watchdog**, **session-persistent personality**, and a **sandboxed plugin ecosystem** into a single, research-grade system.

**Backend (You are here →)** https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI  
**Interface:** https://github.com/therealwestninja/CERBERUS-UnitreeGo2Companion_Web-Interface  
**Plugins:** https://github.com/therealwestninja/Sweetie-Bot-Plugins_for_CERBERUS-API

> **Simulation mode** — no robot required. Use the "LocalHost.html" web-based interface simulator. Ideal for product demonstrtions, script and plugin development and testing.
> **Real-time control** — Unitree GO2 robot required. Use the "PythonServer.html" web-based interface controller to guide the robot, issue commands, and drive the robot autonomously . 

---

## ⚠️ Known Issues, In-Development Features & Coming Soon

> This section is updated with every release.
> **🔴 Known Issues** require operator awareness before deploying on real hardware.
> **🟡 In Development** features are partially implemented with known limitations.
> **🟢 Coming Soon** items are confirmed planned and actively being built.

---

### 🔴 Known Issues

| # | Area | Issue | Workaround |
|---|------|--------|------------|
| KI-01 | **StairClimber — velocity stall sensitivity** | The velocity stall channel uses an upper-70th-percentile baseline that requires ~5 sustained low-speed ticks (~0.08 s at 60 Hz) before triggering. Very brief stalls may be missed; the force-spike and torque-spike channels compensate in most cases. | Reduce `stall_confirm_ticks` via `plugin.tune(stall_confirm_ticks=2)` at runtime if response is too slow for your stair geometry. |
| KI-02 | **`set_body_height` is a relative offset** | The Go2 SDK and `SimBridge` treat `set_body_height(h)` as a **relative offset** from the default 0.27 m standing height, not an absolute value. The payload and stair plugins correctly use offsets. Third-party plugins calling this with an absolute value will misbehave silently. | Always pass a delta: `bridge.set_body_height(+0.04)` to raise 4 cm. Document this in every plugin that calls it. |
| KI-03 | **Session store not saved on SIGKILL** | Personality evolution is saved on clean shutdown only (`SIGTERM` / `SIGINT`). A hard kill, power loss, or OOM event loses the current session's delta. The safety audit log is unaffected (written per-event). | Use `systemd` with `KillSignal=SIGTERM` or a container `STOPSIGNAL SIGTERM`. |
| KI-04 | **TerrainArbiter + StairClimber gait conflict on hot-reload** | StairClimber runs at hook priority 110 (after TerrainArbiter at 100) and re-commands stair gait every active tick to override terrain selections. If TerrainArbiter is unloaded and reloaded mid-session while stair mode is active, it re-registers at priority 100 and the override continues to work — but the transition may produce one spurious gait command. | Avoid reloading TerrainArbiter while a stair traversal is in progress. Wait for `stair.active = false` first. |
| ~~KI-05~~ | ~~RealBridge DDS reconnection not implemented~~ | **✅ Resolved in v2.6.0.** `RealBridge` now includes automatic reconnection with exponential back-off (1→2→4→8→16→32→60 s cap). Five consecutive command failures trigger the reconnect loop; successful reconnection resets all state. | — |
| ~~KI-06~~ | ~~Test suite coverage gap~~ | **✅ Resolved in v2.6.0.** 316 tests across 7 files now cover plugin compatibility, trust-level sandboxing, session store (evolution, atomic write, schema migration), WebSocket manager, and health/ready/session endpoint contracts. | — |
| ~~KI-07~~ | ~~Version string hardcoded in three places~~ | **✅ Resolved in v2.6.0.** `cerberus/__init__.__version__` reads from `importlib.metadata` at runtime. All three former literal strings now import and use `__version__`. | — |
| KI-08 | **SimBridge sensor realism is approximate** | `SimBridge._sim_loop()` implements physically motivated but non-physics-based foot force, joint torque, and IMU models. Detection thresholds calibrated in simulation — especially the force-spike channel — will need re-tuning on real hardware because actual leg loading differs. | Use `plugin.tune(force_spike_ratio=X)` at runtime after initial hardware testing. All stair and snag thresholds are tunable without plugin reload. |

---

### 🟡 In Development (partial — use with caution)

| Feature | Status | Known Limitation |
|---------|--------|-----------------|
| ~~Comprehensive test suite~~ | ~~Active~~ | ✅ Completed v2.6.0: 362 tests, 8 test files, plugin compat/safety/session/WS/health all covered. |
| **Undercarriage payload tactile mapping** | Functional in simulation only | `substrate_scan` builds a `ScanTile` map via foot-force redistribution inference. On real hardware, fidelity depends on actual payload sensor hardware. No sensor driver is included. |
| **Personality evolution** | Session delta only | Evolution is intentionally small (~0.002 per trait per session). Long-term drift over months of operation is untested. Traits are clamped to [0.05, 0.98]. |
| **Stair step counter** | Best-effort estimate | Inferred from asymmetry direction changes in the detection window. Short steps or slow traversal may under-count. Not used in any safety decision. |
| **Swagger UI authentication** | Workaround required | The built-in `/docs` UI does not auto-inject `X-CERBERUS-Key`. Use the "Authorize" button in Swagger, `curl -H "X-CERBERUS-Key: $KEY"`, or the `?api_key=` query param for WebSocket until a UI-level fix is added. |

---

### 🟢 Coming Soon

| Feature | Priority | Target Version |
|---------|----------|----------------|
| ~~Comprehensive test suite redesign~~ | ~~P0~~ | ✅ Completed in v2.6.0 |
| ~~MuJoCo physics simulation~~ | ~~P1~~ | ✅ Completed in v2.7.0 — CPG trot controller, contact forces |
| ~~ROS 2 bridge~~ | ~~P1~~ | ✅ Completed in v2.6.0 (stub — full impl requires ROS2 env) |
| ~~RealBridge auto-reconnect~~ | ~~P2~~ | ✅ Completed in v2.6.0 |
| ~~Single-source version string~~ | ~~P2~~ | ✅ Completed in v2.6.0 |
| **Voice / NLU command interface** | P1 | v2.7.0 — Whisper STT → intent parser → GoalQueue injection |
| ~~React dashboard UI~~ | ~~P2~~ | ✅ Completed in v2.7.0 — see `GET /dashboard` |
| ~~Hardware deployment scripts~~ | ~~P1~~ | ✅ Completed in v2.8.0 — `.env.example`, `hardware_check.py`, systemd service |
| ~~RL training pipeline~~ | ~~P3~~ | ✅ Completed in v2.8.0 — `cerberus/learning/` Gymnasium env + PPO trainer |
| **Reinforcement learning pipeline** | P3 | v2.8.0 — IsaacLab/MuJoCo sim-to-real for gait and stair policies |
| **Multi-agent DDS coordination** | P3 | v2.9.0 — Swarm topics for coordinated multi-Go2 deployments |
| **Mobile companion app** | P4 | v3.0.0 — Flutter WebSocket client, gamepad support |
| **Predictive world model** | P4 | v3.0.0 — Short-horizon planning from sensor history |

---

## 🧠 Architecture

```
CERBERUS Engine (asyncio, 30–200 Hz)
│
├── Safety Watchdog      ← Heartbeat, tilt, battery, E-stop (50 Hz)
├── Behavior Engine      ← 3-layer BT: Reactive / Deliberative / Reflective
│   └── Session Store    ← Personality evolution persisted across restarts
├── Digital Anatomy      ← 12-DOF kinematics, COM, fatigue, energy
│   └── Payload Model    ← COM offset, clearance, adaptive safety limits
├── Plugin System        ← Sandboxed, capability-gated, priority-ordered
│   ├── TerrainArbiter   ← Terrain classification + gait adaptation   [priority 100]
│   ├── StairClimber     ← Stair detection, traversal, snag recovery   [priority 110]
│   ├── Undercarriage    ← Payload management + belly behaviors        [priority 120]
│   ├── LimbLossRecovery ← Tripod gait + orientation comp (biomech-derived) [priority 120]
│   └── VoiceNLU         ← Whisper STT → intent parser → GoalQueue
│
└── Go2 Bridge
    ├── RealBridge       ← CycloneDDS / unitree_sdk2_python (auto-reconnect)
    ├── Ros2Bridge       ← ROS 2 Humble+ / rclpy / unitree_ros2  [GO2_ROS2=true]
    ├── MuJocoBridge     ← High-fidelity physics, CPG trot, contact forces [GO2_MUJOCO=true]
    └── SimBridge        ← Behavioural simulation w/ limb-loss model
```

---

## 🚀 Quick Start

### Simulation (no robot required)

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -e ".[dev]"

cp .env.example .env
# Ensure GO2_SIMULATION=true in .env

cerberus
# REST API → http://localhost:8080
# Swagger UI → http://localhost:8080/docs
```

### Real Hardware

```bash
# 1. Build CycloneDDS 0.10.x
git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x
cd cyclonedds && mkdir build install && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=../install && cmake --build . --target install

# 2. Install unitree_sdk2_python
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
export CYCLONEDDS_HOME=~/cyclonedds/install && pip install -e .

# 3. Generate API key (required in real-hardware mode)
python -c "import secrets; print(secrets.token_hex(32))"
# Add to .env: CERBERUS_API_KEY=<output>

# 4. Configure and launch
cp .env.example .env
# Edit: GO2_SIMULATION=false, GO2_NETWORK_INTERFACE=eth0
ping 192.168.123.161   # confirm Go2 reachable
cerberus
```

---

## ⚙️ Configuration (`.env`)

```bash
# Robot
GO2_SIMULATION=true
GO2_NETWORK_INTERFACE=eth0

# Security — required in real-hardware mode
CERBERUS_API_KEY=

# API server
GO2_API_HOST=0.0.0.0
GO2_API_PORT=8080
CORS_ORIGINS=http://localhost:3000,http://localhost:5173

# Engine
CERBERUS_HZ=60
HEARTBEAT_TIMEOUT=5.0

# Personality (overrides loaded session on startup)
PERSONALITY_ENERGY=0.7
PERSONALITY_FRIENDLINESS=0.8
PERSONALITY_CURIOSITY=0.6
PERSONALITY_LOYALTY=0.9
PERSONALITY_PLAYFULNESS=0.65

# Session persistence
CERBERUS_SESSION_FILE=logs/personality_session.json

# Plugins
PLUGIN_DIRS=plugins
PLUGIN_MAX_ERRORS=5

# Logging
LOG_LEVEL=INFO
CERBERUS_AUDIT_LOG=logs/safety_audit.jsonl
```

---

## 📡 API Reference

All endpoints except `/health` and `/ready` require `X-CERBERUS-Key` header
(REST) or `?api_key=` query param (WebSocket) when `CERBERUS_API_KEY` is set.

### System & Probes

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | ❌ | Liveness probe — always 200 if process alive |
| `GET /ready` | ❌ | Readiness probe — 503 until engine RUNNING |
| `GET /` | ✅ | Service status, engine state, simulation flag |
| `GET /state` | ✅ | Full robot state snapshot |
| `GET /stats` | ✅ | Engine Hz, tick count, uptime, overruns |
| `GET /session` | ✅ | Session number, uptime, personality, lifetime stats |
| `GET /anatomy` | ✅ | Joints, COM, stability, energy, fatigue |
| `GET /behavior` | ✅ | Active behavior, mood, goals, personality |
| `GET /terrain` | ✅ | TerrainArbiter status |
| `GET /payload` | ✅ | Payload config, contact state |
| `GET /plugins` | ✅ | All loaded plugins |
| `GET /safety/events` | ✅ | Last 50 safety audit events |

### Motion

```
POST /safety/estop              — Hard E-stop (irreversible on real hardware)
POST /safety/clear_estop        — Re-arm (simulation only)
POST /motion/stand_up
POST /motion/stand_down
POST /motion/stop
POST /motion/move               {vx, vy, vyaw}
POST /motion/sport_mode         {mode}  — 17 named modes
POST /motion/body_height        {height}  — relative offset (m)
POST /motion/euler              {roll, pitch, yaw}
POST /motion/gait               {gait_id: 0–3}
POST /motion/foot_raise         {height}
POST /motion/speed_level        {level: -1/0/1}
POST /motion/continuous_gait    {enabled}
```

### Payload

```
POST /payload/attach                    — mount payload, auto-compensate limits
POST /payload/detach                    — remove, restore original limits
POST /payload/behavior/ground_scout     {duration_s}
POST /payload/behavior/belly_contact    {hold_s}
POST /payload/behavior/thermal_rest     {duration_s}
POST /payload/behavior/object_nudge     {nudge_speed, nudge_dist_m}
POST /payload/behavior/substrate_scan   {cols, col_width_m, row_len_m}
```

### Peripherals & Behavior

```
POST /led                       {r, g, b}
POST /volume                    {level}
POST /obstacle_avoidance        {enabled}
POST /behavior/goal             {name, priority, params}
POST /plugins/{name}/enable
POST /plugins/{name}/disable
DELETE /plugins/{name}
WS   /ws                        — state at 30 Hz + command channel
```

---

## 🔌 Writing a Plugin

```python
# plugins/my_plugin/plugin.py
from cerberus.plugins.plugin_manager import CerberusPlugin, PluginManifest, TrustLevel

class MyPlugin(CerberusPlugin):

    MANIFEST = PluginManifest(
        name         = "my_plugin",
        version      = "1.0.0",
        author       = "You",
        description  = "Does something cool",
        capabilities = {"read_state", "publish_events"},
        trust        = TrustLevel.COMMUNITY,
    )

    HOOK_PRIORITY = 90   # lower = runs earlier; default 100

    async def on_load(self)  -> None: ...
    async def on_unload(self)-> None: ...

    async def on_tick(self, tick: int) -> None:
        if tick % 60 == 0:
            state = await self.get_state()
            await self.publish("my.event", {"battery": state.battery_percent})
```

Plugins are auto-discovered from all `PLUGIN_DIRS` at startup. No registration required.

| Trust | Capabilities |
|-------|-------------|
| `TRUSTED` | All, including `modify_safety_limits` and `low_level_control` |
| `COMMUNITY` | Motion, gait, LED, audio, sport modes, events |
| `UNTRUSTED` | `read_state` only |

---

## 🛡️ Safety Architecture

```
Operator input
  → API validation (Pydantic)
  → Safety watchdog (50 Hz: heartbeat, tilt, battery)
  → Bridge velocity clamping
  → Payload limit compensation (if attached)
  → Stair limit tightening (if active)
  → Hardware firmware limits
```

All safety events are appended to `logs/safety_audit.jsonl` with a full
robot-state snapshot. The E-stop is one-way on real hardware.

---

## 🎭 All 17 Sport Modes

`damp` `balance_stand` `stop_move` `stand_up` `stand_down` `sit` `rise_sit`
`hello` `stretch` `wallow` `scrape` `front_flip`⚠️ `front_jump`⚠️
`front_pounce`⚠️ `dance1` `dance2` `finger_heart`

⚠️ Requires ~2 m clear space. Never use indoors or on slippery surfaces.

---

## 🧪 Testing

```bash
pytest tests/ -v                          # all 212 tests
pytest tests/test_stair_climber.py  -v   # 62 — stair + snag compensation
pytest tests/test_payload.py        -v   # 54 — payload physics + behaviors
pytest tests/test_all.py            -v   # 56 — bridge, engine, API, plugins
pytest tests/test_terrain_arbiter.py -v  # 40 — terrain classification

pytest tests/ --cov=cerberus --cov=backend --cov=plugins \
              --cov-report=html
```

---

## 📦 Project Structure

```
CERBERUS/
├── cerberus/
│   ├── anatomy/
│   │   ├── kinematics.py        12-DOF model, COM, stability, energy
│   │   └── payload.py           Undercarriage payload physics
│   ├── bridge/
│   │   ├── go2_bridge.py        RealBridge (DDS+reconnect) + SimBridge
│   │   └── ros2_bridge.py       ROS 2 Humble+ bridge [GO2_ROS2=true]
│   ├── cognitive/
│   │   ├── behavior_engine.py   3-layer BT, personality, mood, goal queue
│   │   └── session_store.py     Personality persistence + evolution
│   ├── core/
│   │   ├── auth.py              API key middleware
│   │   ├── engine.py            Async tick loop, EventBus, hooks
│   │   └── safety.py            Watchdog, E-stop, audit log
│   └── plugins/
│       └── plugin_manager.py    Sandboxed loader, capability system
├── backend/
│   └── main.py                  FastAPI (39 endpoints + WebSocket)
├── plugins/
│   ├── terrain_arbiter/         Terrain classification + gait adaptation
│   ├── stair_climber/           Stair detection + snag recovery
│   ├── undercarriage_payload/   Payload management + belly behaviors
│   └── examples/perception_plugin/
├── tests/
│   ├── conftest.py              Shared fixtures
│   ├── test_all.py              56 tests — bridge, engine, API
│   ├── test_terrain_arbiter.py  40 tests
│   ├── test_payload.py          54 tests
│   ├── test_stair_climber.py    62 tests
│   ├── test_plugin_compat.py    20 tests — multi-plugin coexistence
│   ├── test_plugin_safety.py    38 tests — trust sandboxing
│   └── test_infrastructure.py   46 tests — session, WS, health
└── logs/
    └── safety_audit.jsonl
```

---

## 📈 Contributing

1. Fork → branch (`feature/x` or `fix/y`)
2. `pytest tests/ -v` — all tests must pass
3. `ruff check .` — linting must be clean
4. Update `Changelog.md`
5. Open a pull request — CI runs automatically

---

## 📜 License

MIT License — see [LICENSE](LICENSE)

---

*CERBERUS v2.8.0 · 522 tests · 50 REST endpoints · 4 bridges · 5 plugins · Python 3.11+*
