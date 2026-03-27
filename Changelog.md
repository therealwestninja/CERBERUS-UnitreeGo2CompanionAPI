# CERBERUS — Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)  
Versioning: [Semantic Versioning](https://semver.org/)

---

## [2.0.0] — 2026-03-27

### Breaking Changes
- **Full architecture rewrite** — all existing code replaced
- Entry point is now `main.py`; `backend.api.server:create_app` remains but is optional
- Plugin contract changed: subclass `CERBERUSPlugin` (new base), not the old adapter pattern
- `requirements.txt` updated: added `aiortc`, `bleak`, `buttplug`, `dearpygui`; removed Node.js deps

### Added
- **Go2 WebRTC Adapter** (`cerberus/robot/go2_webrtc.py`) — full PRO/AIR transport via `aiortc`
  - SDP exchange via HTTP POST to robot's signaling endpoint (port 8082)
  - JSON sport commands over data channel (matches unitree_sdk2 API IDs)
  - Simulation mode with synthetic telemetry for development without hardware
  - Full sport API: move, stop, stand_up/down, set_body_height, set_euler, speed_level, dance, stretch, heart, wiggle_hips
- **Central Event Bus** (`cerberus/core/event_bus.py`)
  - 20+ typed `EventType` enums spanning robot, safety, peripheral, FunScript, bio, UI domains
  - Priority model: priority-1 bypasses queue (ESTOP, HR_CRITICAL)
  - Thread-safe `publish_sync()` for DPG / BLE callbacks
  - Queue depth monitoring and per-event stats
- **Safety Manager** (`cerberus/core/safety.py`)
  - Battery voltage monitoring with configurable cutoff
  - IMU tilt limit enforcement
  - Heart rate thresholds: soft alarm (180+), hard e-stop (200+), low HR e-stop (<40)
  - Watchdog: 3s robot telemetry timeout → auto e-stop
  - E-stop requires explicit `clear_estop()` — no auto-resume
- **Plugin Base** (`cerberus/core/plugin_base.py`)
  - Trust levels: CORE / TRUSTED / SANDBOX
  - Managed lifecycle: load → start → tick → stop → unload
  - Background task tracking with auto-cancel on unload
  - Plugin error isolation (crash does not propagate)
- **Runtime** (`cerberus/core/runtime.py`)
  - 30Hz deterministic tick loop with overrun detection
  - Priority scheduling: safety → robot → plugins → UI
  - Plugin registry with dynamic load/unload
  - Handles UI_COMMAND events (play/pause/stop/load_funscript/estop)
- **FunScript Player Plugin** (`plugins/funscript/funscript_player.py`)
  - Parses `.funscript` JSON files
  - Linear interpolation between keyframes at 30Hz
  - Maps `pos` → robot `vx`, body height, lateral sway
  - Emits `FUNSCRIPT_TICK` for peripheral plugins (Buttplug, Hismith)
  - Pauses on ESTOP; requires manual resume after clear
- **Buttplug.io Plugin** (`plugins/buttplug/buttplug_plugin.py`)
  - Connects to Intiface Central via WebSocket (protocol v4)
  - Drives VIBRATE, ROTATE, POSITION_WITH_DURATION output types
  - Auto-reconnect loop with 5s retry
  - E-stop: all devices stopped immediately (priority-1 handler)
- **Hismith Plugin** (`plugins/hismith/hismith_plugin.py`)
  - BLE GATT connection to Hismith sex machines
  - Auto-scans for "Hismith" / "BM-" BLE advertisers
  - Speed packet format: `[0xFE, speed_byte, 0xFF]`
  - Configurable max speed cap (safety limit)
  - Auto-reconnect on BLE drop
- **Samsung Galaxy Fit 2 Plugin** (`plugins/galaxy_fit2/galaxy_fit2_plugin.py`)
  - Standard BLE HR Service (0x180D) primary path
  - Samsung proprietary fallback (service 0x6217 / `_on_hr_proprietary`)
  - Rolling 3-sample median filter for noise rejection
  - Publishes `HEARTRATE_UPDATE` → SafetyManager acts automatically
  - Continues monitoring through ESTOP (does not disconnect)
- **Dear PyGui UI** (`ui/cerberus_ui.py`)
  - Native GPU-rendered operator interface
  - Utilitarian / industrial aesthetic (dark base, amber/cyan accents, monospace telemetry)
  - Panels: Connection Status, Telemetry, Bio, FunScript Player, Safety (E-Stop), Plugins, Runtime
  - Fully decoupled: reads UIState snapshots only, sends commands via UIBridge
  - File dialog for `.funscript` loading
  - E-stop button (red, prominent), Clear E-stop button (gated behind active ESTOP)
- **UI Bridge** (`ui/ui_bridge.py`)
  - Thread-safe state exchange between asyncio runtime and DPG render thread
  - Last-write-wins state snapshot — no backlog accumulation
  - `send_command()` → `bus.publish_sync()` — the only legal UI→runtime path
- **Test suite** (`tests/test_core.py`)
  - EventBus: subscribe, priority-1 bypass, exception isolation, unsubscribe
  - SafetyManager: estop trigger/clear, idempotency, battery violation, HR critical
  - FunScriptPlugin: file load, bad file handling
- **Configuration** (`config/cerberus.yaml`) — all settings in one place
- **Main entry point** (`main.py`) — runtime + UI + API wired together

### Changed
- Transport changed from CycloneDDS to WebRTC (PRO/AIR target)
- UI changed from HTML/JS to Dear PyGui (native, decoupled, GPU-rendered)
- Safety is now the first thing checked every tick (was last)
- Event bus replaces all direct cross-module calls

### Removed
- Node.js dependency (was used for old simulation runner)
- Cute/playful UI aesthetic
- `go2_platform` package name (now `cerberus`)

---

## [1.x] — Pre-rewrite

Initial scaffold: FastAPI backend, HTML UI, placeholder cerberus/ package, 
CycloneDDS-based robot communication stub, basic CI/CD workflows.
See git history for detail.
