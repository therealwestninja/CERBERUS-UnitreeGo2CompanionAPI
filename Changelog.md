# Changelog

All notable changes to CERBERUS are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) · Versioning: [SemVer](https://semver.org/)

---

## [3.1.1] — 2026-03-27

### Fixed

**NLU — 100% Go2 mode coverage via rules (no LLM needed)**
- Added `damp` patterns: "damp", "go limp", "safe park"
- Added `front_pounce` patterns: "pounce", "front pounce", "lunge"
- Added `rise_sit` patterns: "rise from sit", "rise sit", "get up from sitting"
- Fixed spin vs strafe: "spin left/right" now produces pure rotation (vyaw only)
- Fixed pattern priority: `rise_sit` checked before `sit`
- Added obstacle control: "turn obstacle avoidance on/off"
- Added light control: "turn the lights on/off", "dim the lights"
- Added volume control: "volume up/down", "louder/quieter"
- Added spin: "spin left/right", "rotate left/right", "yaw left/right"
- Added follow: "follow me", "come here"
- Added wag: "wag your tail", "happy", "wiggle"
- Height unit auto-detect: "height 45cm" and "height 0.45m" → 0.45 m
- **All 17 Go2 sport modes now reachable via NLU rules**

**Server**
- `_dispatch_nlu_action()` handles `config_obstacle` action type
- VUI dispatch uses `-1` sentinel to preserve unchanged values
- `configure_logging()` wired into lifespan startup

**Package**
- `cerberus/__init__.py` populated: 25 exported symbols, `__version__ = "3.1.0"`
- `tests/conftest.py` added — `asyncio_mode=auto` global, no CLI flag needed
- `requirements.txt` — added `aiofiles>=23.2.0` (FastAPI StaticFiles dependency)

### Tests
- 36 new tests added → **129 total, all passing**
  - `TestNLUExpanded` (20): all new NLU pattern categories
  - `TestNLUAPIExpanded` (4): API-level NLU endpoint
  - `TestCLI` (5): CLI argument parsing
  - `TestPackageAPI` (6): public API exports and `__version__`

---

## [3.1.0] — 2026-03-27

### Added

**NLU** (`cerberus/nlu/interpreter.py`) — rule engine + LLM fallback; `POST /api/v1/nlu/command`

**Data Logger** (`cerberus/learning/data_logger.py`) — NDJSON + gzip recording, `SessionReplayer`

**Web Dashboard** (`ui/index.html`) — D-pad, modes, NLU chat, live telemetry, keyboard shortcuts

**CLI** (`cerberus/cli.py`) — `cerberus serve|status|move|stop|mode|behavior|nlu|sessions|replay|plugins`

**Simulation** (`cerberus/simulation/simulator.py`) — physics-lite mock for CI/dev

**Perception stub** (`cerberus/perception/pipeline.py`) — YOLO v11 + LIDAR scaffold

**Logging** (`cerberus/utils/logging_config.py`) — coloured structured logger

**Infrastructure** — `docker-compose.yml`, `.gitignore`, `CONTRIBUTING.md`, `LICENSE`, `docs/architecture.md`

### Changed
- Battery thresholds updated to real Go2 hardware specs
- `SafetyConfig.for_edu_plus()` factory for 28.8V / 15000mAh variant
- `pyproject.toml` v3.1.0 with CLI entry point and optional extras
- `.env.example` cleaned (removed stale BLE haptic refs)
- 93 tests across 10 classes

---

## [3.0.0] — 2026-03-20

Full hardware bridge rewrite: Go2Bridge (DDS + WebRTC + Mock), SafetyGate,
BehaviorEngine (10 canine behaviors), CognitiveEngine, PersonalityModel,
PluginManager (4-tier trust), FastAPI server (18 endpoints + WebSocket). 62 tests.

---

## [2.0.0] — 2025-12-01

FastAPI scaffold, plugin skeleton, WebSocket stub, CI/CD, Dockerfile, Makefile.

---

## [1.0.0] — 2025-09-15

Initial project structure and Vision Document.
