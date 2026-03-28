# Changelog

---

## [3.2.0] — 2026-03-28  ← CURRENT (full restoration)

### Restored from Ver_7 / ver_7_1 (features lost in sessions 1–5)

**Event Bus** (`cerberus/core/event_bus.py`) — exact Ver_7 source
- Typed `EventType` enum (30 event types)
- Priority-1 events bypass queue — synchronous dispatch for E-STOP / HR alarms
- `publish_sync()` for thread-safe calls from BLE callbacks and UI thread
- Queue depth monitoring, per-event stats
- Module singleton (`get_bus()`) shared across all subsystems

**Safety System** — dual-layer, both restored
- `cerberus/core/safety.py` (Ver_7) — `SafetyManager` subscribing to bus events: battery voltage, IMU tilt, HR monitoring, watchdog
- `cerberus/core/safety_watchdog.py` (ver_7_1) — `SafetyWatchdog` with heartbeat timeout, tilt/battery guardrails, velocity validation, JSONL audit log

**CerberusEngine** (`cerberus/core/engine.py`) — exact ver_7_1 source
- Deterministic async tick loop (10–200Hz configurable)
- Plugin hook registry with priority ordering
- Pause/resume support
- Overrun detection and reporting
- EngineState FSM (STOPPED/STARTING/RUNNING/PAUSED/ERROR)
- Per-subsystem tick scheduling (cognition, perception, anatomy, learning, plugins, UI)

**Plugin Base** (`cerberus/core/plugin_base.py`) — exact Ver_7 source
- `CERBERUSPlugin` ABC with full lifecycle: `on_load → on_start → on_tick → on_stop → on_unload`
- `PluginTrustLevel` (CORE/TRUSTED/SANDBOX)
- Background task management (`_spawn()`)
- Auto-cancel on unload
- Bus event emission helper (`_emit()`)

**Plugin Manager** (`cerberus/plugins/plugin_manager.py`) — exact ver_7_1 source
- Capability manifest with trust enforcement (`TRUSTED/COMMUNITY/UNTRUSTED`)
- Sandboxed API wrappers (capability check before every call)
- Dynamic load from file (`plugins/*/plugin.py`)
- Error isolation — per-plugin error count, auto-disable at threshold
- Engine hook registration

**BehaviorEngine** (`cerberus/cognitive/behavior_engine.py`) — exact ver_7_1 source
- Full behavior tree: `Selector`, `Sequence`, `Condition`, `Action` nodes
- `WorkingMemory` — TTL-based key-value store (capacity 256)
- `GoalQueue` — priority-sorted, deadline-aware
- `PersonalityTraits` (energy, friendliness, curiosity, loyalty, playfulness)
- `MoodState` enum with valence/arousal properties
- 3-layer cognitive tree (reactive / deliberative / reflective)
- `on_human_detected()`, `on_obstacle_detected()`, `push_goal()`

**Digital Anatomy** (`cerberus/anatomy/kinematics.py`) — exact ver_7_1 source
- 12-DOF joint model (FL/FR/RL/RR × hip_ab, hip_flex, knee)
- Forward kinematics per leg (`forward_kinematics()`)
- Support polygon (convex hull of contact feet)
- Stability margin (min distance from projected COM to polygon edge)
- `EnergyModel` — per-joint power, consumed Wh, estimated runtime
- Per-joint fatigue accumulation with recovery

**All 4 BLE Plugins** — exact Ver_7 sources
- `plugins/buttplug/buttplug_plugin.py` — Intiface Central WebSocket, VIBRATE/ROTATE/POSITION from FUNSCRIPT_TICK
- `plugins/funscript/funscript_plugin.py` — .funscript replay → robot motion + FUNSCRIPT_TICK events
- `plugins/hismith/hismith_plugin.py` — BLE GATT (0xFFF0/0xFFF2), speed packet [0xFE, speed, 0xFF]
- `plugins/galaxy_fit2/galaxy_fit2_plugin.py` — BLE HR (standard 0x180D + Samsung 0x6217 fallback), HR→SafetyManager

**Backend** — restored dual-server architecture
- `backend/main.py` — full FastAPI app with lifespan, all 25+ endpoints, WebSocket
- `backend/api/server.py` — `create_app(bridge, runtime)` factory pattern (restores original Dockerfile CMD)

**Dockerfile** — restored `CMD ["uvicorn", "backend.api.server:create_app", "--factory", ...]`

**Config** — restored from Ver_7
- `config/cerberus.yaml` — with buttplug, hismith, galaxy_fit2, funscript plugin sections
- `.env.example` — with `INTIFACE_URL`, `HISMITH_ADDRESS`, `GALAXYFIT2_ADDRESS`

**UI** — 1771-line companion UI restored from Ver_5/Ver_6

### Tests
- 32 tests across 6 files (bridge, event_bus, safety, engine, kinematics, funscript)

---

## [3.1.1] — 2026-03-27 (previous session — superseded)
NLU 17/17 mode coverage, 129 tests — but wrong architecture.

## [3.0.0] — 2026-03-20
Initial rewrite — began regression of features.

## [2.0.0] — 2025-12-01
FastAPI scaffold.

## [1.0.0] — 2025-09-15
Initial structure and Vision Document.
