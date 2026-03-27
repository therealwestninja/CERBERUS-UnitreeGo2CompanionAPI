# CERBERUS Changelog

All notable changes to CERBERUS are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/)

---

## [2.1.0] — 2026-03-27

### 🔴 Breaking Changes
- `cerberus/bridge/go2_bridge.py` rewritten — now uses CycloneDDS via `unitree_sdk2_python`, not HTTP
- `GO2_IP` env var replaced by `GO2_NETWORK_INTERFACE` (e.g. `eth0`)
- Plugin base class now requires `MANIFEST` class attribute with `PluginManifest`

### Added
- **Complete DDS bridge** (`cerberus/bridge/go2_bridge.py`)
  - `RealBridge` — CycloneDDS via `unitree_sdk2_python` SportClient
  - `SimBridge` — full behavioral simulation with state drift (no hardware required)
  - All **17 sport modes** mapped: damp, balance_stand, stop_move, stand_up, stand_down, sit, rise_sit, hello, stretch, wallow, scrape, front_flip, front_jump, front_pounce, dance1, dance2, finger_heart
  - LED, volume, and obstacle avoidance control
  - `RobotState` dataclass with full state snapshot + `to_dict()`
  - `create_bridge()` factory respecting `GO2_SIMULATION` env var

- **Safety Watchdog** (`cerberus/core/safety.py`)
  - Heartbeat timeout (default 5s) → automatic `stop_move()`
  - Tilt / fall detection (roll/pitch > 30°) → E-stop
  - Battery critical (< 4%) → E-stop
  - Battery warning levels (15%, 8%) with level transitions
  - Velocity and body-height guardrail validators
  - JSONL audit log at `logs/safety_audit.jsonl`
  - E-stop: one-way in real mode, clearable in simulation

- **Core Engine** (`cerberus/core/engine.py`)
  - Deterministic asyncio tick loop at 30–200Hz (configurable via `CERBERUS_HZ`)
  - Priority scheduling: safety → control → cognition → perception → anatomy → learning → plugins → UI
  - Centralized `EventBus` (async pub/sub, typed topics)
  - Engine states: STOPPED / STARTING / RUNNING / PAUSED / ERROR / SHUTDOWN
  - Live `EngineStats` (Hz, dt, overrun count, uptime)
  - `register_hook()` / `unregister_hook()` for plugin tick callbacks
  - Tick overrun detection and logging

- **Behavior Engine** (`cerberus/cognitive/behavior_engine.py`)
  - Three-layer architecture: Reactive → Deliberative → Reflective
  - Behavior tree nodes: Selector, Sequence, Condition, Action
  - `PersonalityTraits` (energy, friendliness, curiosity, loyalty, playfulness)
  - `MoodState` enum with valence/arousal properties
  - `WorkingMemory` — TTL-based key-value store (capacity-limited)
  - `GoalQueue` — priority-sorted, expiry-aware goal stack
  - Boredom accumulator → auto-play behavior trigger
  - Human detection → automatic greeting behavior
  - Configurable via `PERSONALITY_*` env vars

- **Digital Anatomy** (`cerberus/anatomy/kinematics.py`)
  - 12-DOF joint model with names, limits, velocity, torque
  - Forward kinematics using Go2 URDF link lengths (L_HIP=0.0955, L_THIGH=0.213, L_CALF=0.213)
  - Per-joint fatigue accumulator (intensity × velocity × time)
  - `support_polygon()` — convex hull of contact feet
  - `stability_margin()` — COM-to-edge distance
  - `EnergyModel` — idle power + joint power → remaining runtime estimate
  - COM tracking from foot positions

- **Plugin System** (`cerberus/plugins/plugin_manager.py`)
  - `TrustLevel`: TRUSTED / COMMUNITY / UNTRUSTED
  - Capability manifests: 10 named capabilities, trust-gated
  - Dynamic load/unload via `importlib` (sandboxed namespace)
  - Auto-discovery from plugin directories
  - `CerberusPlugin` base class with safe capability-checked bridge wrappers
  - Auto-disable after `PLUGIN_MAX_ERRORS` consecutive failures
  - Plugin enable/disable without unloading

- **FastAPI Backend** (`backend/main.py`)
  - Full REST API (24 endpoints) — motion, safety, behavior, peripherals, plugins
  - WebSocket `/ws` — state broadcast at 30Hz + incoming command handler
  - Pydantic v2 models with field validation (ranges, types)
  - `asynccontextmanager` lifespan (clean startup/shutdown)
  - CORS middleware with configurable origins
  - All velocity/pose commands go through safety watchdog before bridge
  - Simulation info surfaced in root endpoint

- **Example Plugin** (`plugins/examples/perception_plugin/plugin.py`)
  - YOLOv8 object detection on Go2 front camera
  - Human detection → behavior engine callback
  - Obstacle detection → behavior engine callback
  - Graceful fallback when ultralytics/opencv not installed

- **Test Suite** (`tests/test_all.py`)
  - `test_bridge.py` — SimBridge unit tests (all sport modes, estop, LED, state)
  - `test_engine.py` — Engine tick loop, pause/resume, event bus, safety watchdog
  - `test_api.py` — Full REST endpoint coverage with httpx AsyncClient
  - `test_plugins.py` — Plugin load/unload, capability sandboxing, error isolation

- **Updated docs**
  - `Vision_Document.md` — Implementation status table, SDK architecture diagram, API quick reference
  - `Changelog.md` — This file
  - `Readme.md` — Updated install (CycloneDDS), quickstart, network setup

### Changed
- `pyproject.toml`: renamed package `go2-platform` → `cerberus-go2`, version `2.0.0` → `2.1.0`
- `pyproject.toml`: moved `numpy` to core deps (required by SDK and anatomy model)
- `requirements.txt`: added `numpy`, detailed CycloneDDS install instructions
- All motion commands now call `watchdog.ping_heartbeat()` before dispatch
- Bridge move() enforces velocity clamping at the bridge layer in addition to API validation

### Fixed
- Bridge was using non-existent HTTP interface — now uses correct DDS channel
- Sport mode commands were missing Unitree SDK2 method bindings
- Plugin loader was not sandboxing module namespace — now uses isolated `module_from_spec`
- Engine had no safety check before dispatching motion commands
- No E-stop mechanism existed — added both API endpoint and watchdog auto-trigger
- `pyproject.toml` build-backend had typo (`setuptools.backends.legacy:build`)

---

## [2.0.0] — 2026-01-15

### Added
- Initial project scaffold
- Directory structure: cerberus/, backend/, config/, plugins/examples/, tests/, ui/, docs/
- FastAPI + uvicorn foundation
- `requirements.txt` with core dependencies
- `pyproject.toml` build configuration
- `.env.example` configuration template
- GitHub Actions CI workflows
- `Readme.md` and `Vision_Document.md`
- `CONTRIBUTING.md`
- `Dockerfile`
- `Makefile`

---

## [Unreleased]

### Planned
- MuJoCo physics simulation integration (unitree_mujoco)
- Reinforcement learning agent (IsaacLab/MuJoCo environments)
- Imitation learning pipeline (unitree_lerobot reference)
- Voice/NLU command interface (Whisper STT)
- ROS2 bridge (unitree_ros2 reference)
- Multi-agent coordination (swarm DDS topics)
- Predictive world model for planning
- Mobile companion app (Flutter WebSocket client)
- Personality evolution over session history
