# Changelog

All notable changes to CERBERUS are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [3.0.0] — 2026-03-27

### Added

**Hardware Transport Layer (major)**
- `cerberus/hardware/go2_bridge.py` — unified `Go2Bridge` with three transport backends:
  - `_DDSTransport` — wraps `unitree_sdk2_python` over CycloneDDS (Go2 EDU wired)
  - `_WebRTCTransport` — wraps `go2_webrtc_connect` / `unitree_webrtc_connect` for Wi-Fi (AIR/PRO/EDU, no jailbreak)
  - `_MockTransport` — in-memory stub for CI and simulation mode
- All 17 Go2 native sport modes mapped in both DDS method names and WebRTC API IDs
- `RobotState` dataclass with position, velocity, IMU, battery, foot force, and status flags
- Auto-reconnect watchdog background task
- `add_state_listener()` callback hook for event-driven integrations
- `Go2Bridge.from_config()` factory — transport selected from `config/cerberus.yaml`

**Safety Gate (major)**
- `cerberus/safety/gate.py` — `SafetyGate` with full constraint enforcement:
  - Battery guard: warn at 22 V, hard block at 20.5 V
  - Tilt guard: warn at ~20°, hard block at ~40° IMU tilt
  - Velocity hard limits: vx ±1.5 m/s, vy ±0.8 m/s, vyaw ±2.0 rad/s
  - Special-motion cooldown timer (configurable, default 3 s)
  - Violation audit counter
  - `allow_move()`, `allow_mode()`, `check_config()` interface
- `SafetyConfig` dataclass for full customisation via YAML

**Cognitive Engine (new)**
- `cerberus/core/cognitive.py` — three-layer decision system:
  - Reactive loop (20 Hz) — emergency override for battery-critical and tilt-alert
  - Deliberative loop (1 Hz) — goal selection, behavior scheduling
  - Reflective modulation — personality traits adjust greet/explore thresholds
- `WorkingMemory` dataclass: human-seen, obstacle, battery, tilt, idle timer
- `Goal` / `GoalType` system: IDLE, EXPLORE, GREET, PATROL, CHARGE, USER_CUSTOM
- External stimulus hooks: `notify_human_detected()`, `notify_obstacle()`, `update_from_state()`

**Behavior Engine (enhanced)**
- `cerberus/behavior/engine.py` — `BehaviorEngine` with 10 built-in canine behaviors:
  - `idle`, `sit`, `stand`, `greet` (head tilt + hello), `stretch`
  - `dance` (random dance1/dance2), `patrol` (configurable square loop)
  - `wag` (wallow motion), `alert` (raised posture), `emergency_sit`
- Priority-queued async execution (`asyncio.PriorityQueue`)
- Per-behavior cooldown enforcement
- Execution history (last 50 entries)
- `BehaviorDescriptor` registration API for custom plugin behaviors

**Personality Model (new)**
- `cerberus/personality/model.py` — traits + dynamic mood system:
  - Traits: `sociability`, `playfulness`, `energy`, `curiosity` (0–1, stable)
  - Mood: `valence` (-1 → +1) and `arousal` (0 → 1), exponential decay
  - Events: `on_interaction()`, `on_battery_low()`, `on_obstacle()`, `on_task_success()`
  - `mood_label` property: excited / content / calm / neutral / anxious / distressed
  - JSON persistence across restarts

**Plugin System (enhanced)**
- `cerberus/plugins/manager.py` — 4-tier trust enforcement:
  - `core` / `trusted` / `community` / `untrusted` trust levels
  - Capability check against trust level on load
  - `PluginContext` exposes `bridge` and `behavior_engine` only to permitted trust levels
  - Auto-discovery from `plugins/` via `plugin.yaml` manifests
  - Dynamic load/unload with proper lifecycle (`on_load` / `on_unload`)

**REST + WebSocket API (major expansion)**
- `backend/api/server.py` — FastAPI server with full CERBERUS surface:
  - 18 REST endpoints covering motion, mode, config, behavior, personality, plugins
  - `/ws/telemetry` WebSocket: 10 Hz state push + inbound command handling
  - Full `asynccontextmanager` lifespan: bridge → personality → behavior → cognitive → plugins → broadcaster
  - CORS middleware, Pydantic v2 validation with range constraints on all inputs
  - `emergency_stop` always bypasses safety queue for immediate hardware response

**Configuration**
- `config/cerberus.yaml` — fully documented configuration reference
- Transport, safety thresholds, behavior tick rate, cognitive enable, personality traits, plugins dir

**Tests**
- `tests/test_cerberus.py` — ~50 tests across 6 test classes:
  - `TestSafetyGate` — battery, tilt, velocity, mode cooldown, config validation
  - `TestGo2Bridge` — connect, move/clamp, stop, all 17 modes, euler clamp, listeners
  - `TestBehaviorEngine` — registration, enqueue, execution, cooldown, history cap
  - `TestPersonalityModel` — trait clamp, mood decay, events, persistence, `to_dict`
  - `TestAPIEndpoints` — all 15 REST endpoints (mock bridge)
  - `TestAvailableModes` — completeness check for all 17 modes

**Plugin Examples**
- `plugins/examples/hello_world/` — complete plugin template with `plugin.yaml` manifest

### Changed

- `requirements.txt` — added `numpy`, properly commented optional deps (DDS, WebRTC, Vision, Audio, BLE)
- `pyproject.toml` — project name, version bumped to 3.0.0
- All existing module stubs wired to real hardware bridge and safety gate

### Fixed

- `Go2Bridge` no longer exposes raw transport to application code (encapsulation)
- All mode names normalised to `snake_case` consistently across bridge, behavior engine, and API
- WebSocket broadcaster no longer raises on dead client connections

---

## [2.0.0] — 2025-12-01

### Added
- FastAPI backend scaffold with `uvicorn` + `pydantic v2`
- Plugin system skeleton with manifest loading
- WebSocket telemetry endpoint
- CI/CD workflows: Node.js, Python package, linting, security scan
- Dockerfile and Makefile
- Initial behavior engine stub
- Initial cognitive engine stub

### Changed
- Migrated from Flask to FastAPI
- Replaced `requirements.txt` with `pyproject.toml` as source of truth

---

## [1.0.0] — 2025-09-15

### Added
- Initial project structure: `cerberus/`, `backend/`, `tests/`, `config/`, `docs/`, `ui/`
- Vision Document
- README
- MIT License
- `.gitignore`, `.env.example`
- Contributing guidelines
