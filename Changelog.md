# CERBERUS Changelog

All notable changes to CERBERUS are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/)

---

## [2.8.0] — 2026-03-28 (Session 7 — Hardware Deployment & RL Pipeline)

### Priority
This release focuses on **getting the robot running on real hardware** —
resolving every blocking deployment gap, providing a complete hardware
validation workflow, and adding a production-ready RL training pipeline.

### Added

- **`.env.example`** — complete annotated config for every env var across all
  bridge modes, safety thresholds, voice NLU, and MuJoCo settings.

- **`scripts/hardware_check.py`** — hardware preflight script.
  `python scripts/hardware_check.py --iface eth0` before first launch.
  Validates: Python ≥3.11, package importable, env vars, network interface UP,
  Go2 ping (192.168.123.161), unitree_sdk2py installed, DDS init on interface,
  state topic live, foot forces/IMU/battery in range, optional motion sequence.
  Exit codes: 0=pass  1=fail (do not start)  2=warnings only.

- **`scripts/systemd/cerberus.service`** — production systemd unit.
  `KillSignal=SIGTERM` (personality saved), `EnvironmentFile`, `Restart=on-failure`,
  `IPAddressAllow=192.168.123.0/24`, `NoNewPrivileges=true`.

- **`cerberus/learning/` — RL training pipeline**
  - `environment.py` — `CerberusEnv(gym.Env)` 56-obs/12-act Gymnasium env.
    Observations: IMU, joint pos/vel/torque, foot contacts, cmd, height.
    Actions: relative joint position targets via PD controller (matches SDK
    joint mode → zero-shot sim-to-real). 3-stage velocity curriculum.
    Observation noise for sim-to-real gap. Literature: Kumar 2021, Lee 2020.
  - `rewards.py` — 8-component locomotion reward suite:
    exp velocity kernels (σ=0.25), energy penalty (Σ|τq̇|), stability,
    smoothness, foot-contact bonus, alive bonus, fall penalty.
    Based on RMA / ETH Zurich / Lee et al. formulations.
  - `trainer.py` — PPO via Stable-Baselines3: curriculum callback (0.3→0.7→1.0 m/s),
    EvalCallback + CheckpointCallback, TensorBoard logging, ONNX export for
    on-robot inference (`ort.InferenceSession("policy.onnx").run(...)`).

- **`tests/test_integration.py`** — 38 full-stack HTTP integration tests:
  dashboard, probes (version from metadata), motion/safety/limb-loss/plugin
  flows, error handling, WebSocket broadcast wiring.

- **`tests/test_learning.py`** — 47 RL infrastructure tests:
  reward functions (pure math), env/trainer graceful-degradation, deployment
  file validation (.env.example, systemd KillSignal).

### Fixed
- `test_move_missing_fields_422` — MoveCmd has default values; test corrected to /led
- `TestWebSocketBroadcast` EventBus attribute `_subs` (not `_subscribers`)
- `test_env_config_customisable` — removed invalid `n_envs` kwarg to `EnvConfig`

### Changed
- Version `2.7.0` → `2.8.0`
- `pyproject.toml` — added `[mujoco]` and `[voice]` optional dep groups
- `cerberus/learning/__init__.py` — populated from empty stub
- Total tests: 475 → 522 (+47)

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

---

## [2.7.0] — 2026-03-28 (Session 6 — Limb Loss, MuJoCo, Voice NLU)

### Research

**Limb-loss gait compensation** — grounded in:
- *Canines (veterinary)*: Dickerson et al. 2015; Kirpensteijn et al. 1999;
  Torres et al. 2022 — tripod dogs shift ~40% of missing-limb load to the
  diagonal partner, ~30% to each remaining limb; body tilts toward the
  support triangle centroid via lateral trunk flexion.
- *Equines*: Garcia-Lopez 2022; Back et al. 1995 — fore-limb loss produces
  a nose-up rocking pattern; hind-limb loss a nose-down pelvic-drop.
- *Insects/ants*: Grabowska et al. 2012 — after middle-leg loss, adjacent
  legs extend stance phase; alternating tripod is maintained as far as possible.
- *Spiders*: Parry 1957; Wilson 1967 — universal constraint: never lift two
  adjacent legs simultaneously; asymmetric coupling preserves COM projection.
- *Universal principle*: COM projection must always fall within the support polygon.

### Added

- **`plugins/limb_loss_recovery/plugin.py`** — LimbLossRecoveryPlugin v1.0
  - `LimbDetector` — 90-sample rolling window; dead_fraction > 0.80 for
    SUSPECT_TICKS(30) + CONFIRM_TICKS(60) = 1.5 s confirms limb loss.
    Normal trot swing phase produces ~0.35 dead fraction — well below threshold.
  - `TRIPOD_TABLE` — per-leg (FL/FR/RL/RR) pitch/roll bias derived from exact
    support-triangle centroid geometry at nominal stance height (0.27 m):
    - Missing front leg: +13.1° pitch (nose-up, lean back)
    - Missing rear leg:  −13.1° pitch (nose-down, lean forward)
    - Missing left leg:  −6.8° roll  (lean right toward remaining feet)
    - Missing right leg: +6.8° roll  (lean left)
  - Exponential body-orientation ramp (12% per tick → ~0.5 s to target)
  - Yaw-drift PID (P=0.30, I=0.10) correcting asymmetric ground reaction force
  - Gait re-enforced every tick at priority 120 (highest — overrides stair/terrain)
  - `declare_limb_loss(leg)` — immediate manual override via API
  - `clear_limb_loss()` — restore 4-leg operation
  - LED amber during recovery; restored on clear

- **SimBridge limb-loss physics** (`cerberus/bridge/go2_bridge.py`)
  - `simulate_limb_loss(leg_idx)` — marks one leg non-functional
  - `clear_limb_loss()` — restores all legs
  - In `_sim_loop`:
    - Lost leg: near-zero force (just noise)
    - Remaining legs: +40% diagonal partner, +30% others (biomechanical ratios)
    - Velocity capped at tripod safe speed (0.14 m/s)
    - Yaw drift: left-side missing → negative vyaw; right-side → positive
    - Lost leg joints: near-zero torques, folded position
    - Battery drain +30% (remaining legs compensate)
    - Body pitch/roll bias toward missing leg

- **REST endpoints** (4 new)
  - `GET  /limb_loss` — status, tripod params, per-leg dead fractions
  - `POST /limb_loss/declare {leg}` — manual override
  - `POST /limb_loss/clear` — restore normal operation
  - `POST /sim/limb_loss {leg}` — simulation injection (sim mode only)

- **`cerberus/bridge/mujoco_bridge.py`** — MuJoCo Physics Bridge
  - `MuJocoBridge` — full `BridgeBase` implementation using MuJoCo rigid-body physics
  - Hopf-oscillator CPG (Ijspeert 2008) — 4-oscillator trot with diagonal coupling
    (FL+RR in-phase, FR+RL in-phase, adjacent pairs anti-phase)
  - PD joint controller: KP=40–60, KD=0.8–1.5 mapped to Go2 actuator stiffness
  - 500 Hz physics thread → 30 Hz state readback to async event loop
  - Contact forces from `cfrc_ext` on foot bodies (not approximated)
  - Joint states, quaternion→euler IMU, battery drain from actual joint power
  - Graceful: no mujoco installed → clear RuntimeError at connect(), never at import
  - `create_bridge()`: `GO2_MUJOCO=true` routes to MuJocoBridge
  - Model search: `CERBERUS_MUJOCO_MODEL` env var → default paths → error with install instructions

- **`plugins/voice_nlu/plugin.py`** — VoiceNLU Plugin v1.0
  - `VoiceRecorder` — VAD + sounddevice capture (50 ms chunks, RMS threshold)
  - `TrotCPG.freeze()` equivalent — hold silence detection (1.2 s after speech)
  - 16 intent patterns (regex) covering: stop, sit, stand, lie down, come here,
    hello, dance, stretch, wallow, scrape, finger_heart, balance_stand, explore,
    rise_sit, pounce, front_flip
  - E-stop intent routes directly to `watchdog.trigger_estop()` bypassing goal queue
  - All other intents → `behavior_engine.push_goal(name, priority)`
  - Whisper model pre-loaded in background thread at `on_load()` to reduce latency
  - File mode: `POST /voice/transcribe {path}` — transcribe without microphone
  - Microphone mode: `POST /voice/listen/start` / `/stop`
  - Graceful: sounddevice/Whisper absent → warning at load, error only at use

- **REST endpoints** (4 new)
  - `GET  /voice` — status, model, listening state, last command
  - `POST /voice/listen/start`
  - `POST /voice/listen/stop`
  - `POST /voice/transcribe {path}`

- **Limb loss test suite** (`tests/test_limb_loss.py`) — 46 tests
  - `TestLimbDetector` (6) — normal trot never false-triggers, dead leg detection,
    confirm ticks gating, all four legs detectable, auto-recovery, snapshot
  - `TestTripodGeometry` (10) — pitch direction for front/rear loss, roll direction
    for left/right loss, yaw correction sign, magnitude symmetry, velocity limits,
    body height offset, foot raise height
  - `TestLimbLossPlugin` (13) — manual declare, all four legs, unknown leg error,
    double-declare blocked, watchdog limits tightened/restored, auto-detection
    from force window, estop safety, orientation ramp
  - `TestSimBridgeLimbLoss` (11) — lost-leg near-zero force, remaining leg elevation,
    yaw drift direction (left missing → left yaw, right missing → right yaw),
    joint torques near zero, folded joint positions, battery drain increase
  - `TestLimbLossAPI` (6) — REST endpoint smoke tests

### Changed
- Package version `2.6.0` → `2.7.0`
- `create_bridge()` extended: `GO2_MUJOCO=true` → MuJocoBridge (priority 2, after SimBridge)
- WebSocket broadcast: `voice.*` topics forwarded as `{type: "voice", data: ...}`
- WebSocket broadcast: `limb_loss.*` topics forwarded
- Total REST endpoints: 42 → 50 (+8)
- Total tests: 316 → 362 (+46 limb-loss)
- Bridge implementations: 3 → 4 (SimBridge, RealBridge, Ros2Bridge, MuJocoBridge)
- Plugin count: 4 → 6 (+ limb_loss_recovery, voice_nlu)

---

## [2.6.0] — 2026-03-28 (Session 5)

### Added

- **`cerberus/bridge/ros2_bridge.py`** — ROS 2 Bridge (`GO2_ROS2=true`)
  - `Ros2Bridge` implementing the full `BridgeBase` interface
  - REP-103 coordinate conversion (y-axis mirror, roll sign flip)
  - Full publisher map: `/cmd_vel`, `/body_height`, `/body_euler`, `/speed_level`,
    `/foot_raise_height`, `/gait_mode`, `/sport_mode`, `/estop` (latched),
    `/led_color`, `/volume`, `/obstacle_avoidance`
  - Subscriber map: unitree_go/SportModeState → RobotState, BatteryState
  - `MultiThreadedExecutor` in a daemon thread; async-safe via `run_in_executor`
  - Graceful import failure: `rclpy` absent → clean `RuntimeError` at `connect()`
    time, never at import time — the rest of CERBERUS always importable
  - `create_bridge()` factory: `GO2_ROS2=true` routes to `Ros2Bridge`

- **`GET /stair`** — StairClimber plugin status endpoint
  - FSM state, direction, score, step count, snag count, adaptive foot-raise,
    sensor window snapshot, recovery phase

- **`POST /stair/tune`** — Runtime threshold adjustment
  - `StairTuneCmd` Pydantic model: 14 optional fields covering all stair-detection
    thresholds (`asym_variance_min`, `confirm_ticks`, etc.) and all snag-detector
    thresholds (`force_spike_ratio`, `stall_fraction_threshold`, etc.)
  - Returns the full updated threshold set; no plugin reload required

- **`cerberus/__init__.__version__`** — Single-source version (closes KI-07)
  - Reads from `importlib.metadata` (installed dist-info) at runtime
  - Falls back to parsing `pyproject.toml` when running from source without install
  - `backend/main.py` imports `__version__` — FastAPI constructor, `/health`
    response, and root endpoint are all driven from the single source

- **Test suite redesign** — +104 tests (212 → 316, closes KI-06)
  - `tests/conftest.py` — shared async fixtures: `sim_bridge`, `bare_engine`,
    `engine_be`, `full_engine`, `terrain_plugin`, `stair_plugin`, `payload_plugin`,
    `MockWebSocket`, `dead_ws`, `session_store`, `tmp_session_path`
  - `tests/test_plugin_compatibility.py` (20 tests) — hook priority ordering,
    execution order within a tick, stair-overrides-terrain gait, combined payload+stair
    limit intersection, event bus topic isolation across all three plugins, error
    isolation (crashing plugin doesn't disable siblings), hook cleanup on unload,
    dynamic-load priority, enable/disable cycle
  - `tests/test_plugin_safety.py` (38 tests) — trust-level taxonomy invariants,
    UNTRUSTED plugin blocked from all control capabilities, COMMUNITY plugin blocked
    from TRUSTED-only capabilities, manifest-declared-only capabilities enforced,
    E-stop propagation to all plugin behaviors, safety limit invariants (plugins
    may only tighten, never widen), sandbox module isolation in `sys.modules`,
    reloading produces fresh instance, watchdog heartbeat/tilt/battery gating
  - `tests/test_infrastructure.py` (46 tests) — session store first-boot defaults,
    load/save/personality-evolution cycle, bounds clamping, loyalty invariance,
    atomic write verification, corrupt-file fallback, lifetime stats accumulation,
    v1→v2 schema migration; WebSocket manager add/remove/broadcast/dead-client-cleanup/
    broadcast_json; `/health`, `/ready`, `/session` endpoint contracts

### Fixed

- **KI-05 — RealBridge DDS auto-reconnect**
  - `_cmd()` helper wraps all 17 SDK calls with success tracking via
    `_mark_command_result(success)`
  - Five consecutive failures trigger `_reconnect_loop()` as an `asyncio.Task`
  - Back-off schedule: 1 → 2 → 4 → 8 → 16 → 32 → 60 s (capped)
  - Live state updates (DDS subscription callbacks) reset the stale counter
  - `disconnect()` cancels any active reconnect task on clean shutdown
  - All RealBridge methods (`stand_up`, `stand_down`, `move`, `stop_move`,
    `set_body_height`, `set_euler`, `switch_gait`, `set_foot_raise_height`,
    `set_continuous_gait`, `execute_sport_mode`, `emergency_stop`,
    `set_obstacle_avoidance`, `set_led`, `set_volume`) migrated to `_cmd()`

- **`UndercarriagePayload.on_tick` — missing E-stop gate (safety regression)**
  - All five autonomous behaviors (ground_scout, belly_contact, thermal_rest,
    object_nudge, substrate_scan) continued executing when `state.estop_active`
    was True, because `on_tick` had no top-level estop check.
  - Added: `if state.estop_active: await self._abort_behavior(); return`
  - Caught by `test_plugin_safety.py::TestEstopPropagation`

- **`PluginManager.unload_plugin` — sys.modules cleanup by wrong key**
  - Used manifest name (`TerrainArbiter`) for `sys.modules` lookup, but modules
    were registered under the file-path-derived name (`cerberus_plugin_terrain_arbiter_plugin`)
  - Fixed: derive `unique_name` from `Path(record.module_path)` using the same
    formula as `load_from_file()` — `cerberus_plugin_{parent.name}_{stem}`
  - Caught by `test_plugin_safety.py::TestPluginSandboxIsolation`

- **`FastAPI(version=__version__)` syntax error**
  - Partial replacement from previous session left `__version__` as a bare
    positional argument, causing `SyntaxError` and failing all 34 API tests

- **`create_bridge()` did not support ROS 2 mode**
  - Added `GO2_ROS2=true` branch routing to `Ros2Bridge`

- **`SimBridge` class header lost during RealBridge replacement**
  - Python-level file manipulation consumed the `class SimBridge(BridgeBase):` header
  - Restored; all 316 tests pass

### Changed
- Package version `2.5.0` → `2.6.0`
- `pyproject.toml` version bumped
- `Readme.md` — KI-05, KI-06, KI-07 marked ~~resolved~~; Coming Soon table updated;
  architecture diagram adds `Ros2Bridge`; project structure reflects new test files
- `create_bridge()` docstring updated with three-way priority table
- Total test count: 212 → 316 (+104)
- Total REST endpoints: 39 → 42 (`/stair`, `/stair/tune`, `/session` added)
- Total bridge implementations: 2 → 3 (`SimBridge`, `RealBridge`, `Ros2Bridge`)

---

## [2.5.0] — 2026-03-28 (Session 4)

### Added

- **`GET /health`** — Liveness probe; always 200 while the process is alive.
  Auth-exempt so container orchestrators and load balancers reach it without
  an API key.
- **`GET /ready`** — Readiness probe; returns 503 until `engine.state == RUNNING`.
  Auth-exempt for the same reason.
- **`GET /session`** — Current session number, uptime, `PersonalityTraits`, lifetime
  accumulated stats, and the last saved file snapshot.
- **`cerberus/core/auth.py`** — Auth middleware now exempts `/health` and `/ready`
  paths regardless of `CERBERUS_API_KEY` configuration.
- **`WebSocketManager`** (`backend/main.py`) — Centralised WS client registry with
  `add()`, `remove()`, `broadcast()`, `broadcast_json()`, and `count` property.
  Replaces three independent dead-client-cleanup patterns that previously lived
  in separate `_broadcast_*` closures. All EventBus → WS forwarding now routes
  through a single `broadcast_json(type, data)` call.
- **Session store save on shutdown** — `lifespan` now calls `_store.save(engine.behavior_engine)`
  before `engine.stop()` on every clean shutdown (`SIGTERM` / `SIGINT`).
- **`Readme.md` — Known Issues, In-Development, and Coming Soon sections** added
  at the top of the document with 8 numbered known issues (KI-01 through KI-08),
  a partial-implementation table, and a prioritised roadmap with target versions.

### Fixed

- **`TorqueWindow` false positive on startup** — `_mean` was initialised to `[0.0]*12`,
  so the very first non-zero joint torque reading (e.g., 5 N·m nominal) produced
  a ratio of `5.0 / max(0.5, 0.0) = 10.0`, immediately exceeding the 3.2×
  spike threshold and triggering a spurious `TORQUE_SPIKE` snag event on the
  first `on_tick()` call after `_enter_stair()`. Fixed by initialising
  `_mean` to `[4.0]*12` (realistic static-load estimate).

- **`SnagDetector` delta always zero** — `FootForceWindow.update(forces)` sets
  `self._prev = forces` (current tick), but `delta(forces)` was called *after*
  `update()`, computing `forces - forces = 0`. The force-spike channel therefore
  never fired regardless of how large the spike was. Fixed by computing
  `deltas = force_win.delta(forces)` **before** calling `force_win.update(forces)`.

- **`StairClimberPlugin.HOOK_PRIORITY = 70`** — The comment said "runs after
  TerrainArbiter" but in the engine's ascending-sort hook order, priority 70
  actually runs *before* priority 100 (TerrainArbiter default). This meant
  StairClimber was issuing stair-gait commands that TerrainArbiter immediately
  overrode on the same tick. Changed to `HOOK_PRIORITY = 110`.

- **`PluginManager._register_hook_for_record()` ignores `HOOK_PRIORITY`** —
  The method hardcoded `priority=200` for all plugins, making the `HOOK_PRIORITY`
  class attribute a no-op. Fixed to read `getattr(cls, "HOOK_PRIORITY", 100)`.

- **Orphaned `_ws_clients` list and `_broadcast_state` function** — After
  `WebSocketManager` was introduced in the previous session, the old list and
  standalone broadcast function were left in place. `main.py` still referenced
  `_ws_clients` in the WS endpoint and in a global scope that was never cleaned
  up. All references removed; WS endpoint migrated to `ws_manager.add/remove`.

- **Recovery body-height calls used absolute values** — `_tick_recovery` passed
  `state.body_height + 0.004` (absolute ~0.274 m) to `bridge.set_body_height()`,
  which in the Go2 SDK and SimBridge is a *relative offset* API. SimBridge would
  have computed `0.27 + 0.274 = 0.544 m`. Changed to `set_body_height(RECOVERY_BODY_LIFT_M)`
  (+4 mm offset) and `set_body_height(0.0)` (neutral restore).

- **Velocity stall test off-by-one** — `test_velocity_stall_detected` called
  `det.update()` a 4th time expecting an event, but the event fires on the Nth
  stall tick (inside the loop) and the subsequent call hits the refractory
  cooldown. Fixed to capture the last result *from inside* the stall loop and
  to feed enough ticks (15 max) for the upper-70th-percentile baseline to clear
  the detection threshold.

### Changed
- Package version `2.4.0` → `2.5.0`
- `Readme.md` version badge updated; architecture diagram updated with hook priorities
- `_session_store` placeholder singleton removed; `ws_manager` is now the only
  WebSocket singleton
- Total test count: 150 → 212 (+62 new stair climber tests fully passing)

---

## [2.4.0] — 2026-03-28 (Session 3)

### Added

- **`cerberus/anatomy/payload.py`** — Undercarriage Payload Physics Model
  - `PayloadConfig` dataclass: material, mass, thickness, dimensions, COM offset, sensor flags
  - `PayloadMaterial` enum: SILICONE, RIGID_PLATE, FOAM, MESH with friction/compliance properties
  - `PayloadCompensator`: derives all corrected operating limits from payload geometry
    - `adjusted_safety_limits()` — velocity (mass penalty), yaw (inertia), tilt (belly-drag angle), min height
    - `recommended_standing_height_m` — maintains `desired_clearance_m` above payload contact
    - `foot_raise_adjustment_m()` — additional foot raise for swing-phase clearance
    - `recommended_gait_id()` — 0–3 based on total system mass
    - `combined_com()` — composite COM for robot + payload in world frame
    - `infer_contact()` — contact state from body height + foot-force redistribution
  - `ContactState` enum: NO_CONTACT / APPROACHING / CONTACT / PRESSED / DRAGGING
  - `CombinedCOM` dataclass with delta_x/y/z shift relative to bare robot

- **`plugins/undercarriage_payload/plugin.py`** — UndercarriagePayloadPlugin
  - `attach()` / `detach()` — live payload registration, limit adjustment, gait/height commands
  - Continuous ground-clearance monitoring and drag detection every tick
  - Drag abort: `stop_move()` + `payload.drag_warning` event on lateral motion while in contact
  - Five autonomous belly-interaction behaviors with 4-phase FSMs:
    - `ground_scout` — 3mm hover traverse for terrain texture reading
    - `belly_contact` — controlled touchdown, configurable hold, compliant ascent
    - `thermal_rest` — full stand_down on silicone pad, LED amber, periodic telemetry
    - `object_nudge` — lower/advance/retreat cycle using high-friction silicone
    - `substrate_scan` — boustrophedon belly sweep producing a `ScanTile` map
  - Autonomous trigger evaluation (curiosity, boredom, playfulness thresholds)
  - E-stop / drag passthrough aborts all behaviors with safe height restore
  - All behaviors emit typed EventBus topics forwarded to WebSocket clients

- **`cerberus/anatomy/kinematics.py`** — DigitalAnatomy payload integration
  - `attach_payload(config)` / `detach_payload()` — register payload compensator
  - `update()` uses `combined_com()` for COM tracking when payload attached
  - Idle power raised by payload mass × g × 0.05 W/N holding cost
  - `status()` includes `payload_attached` flag and full compensator dict

- **`cerberus/core/auth.py`** — API key authentication (`MISSING_SAFETY`)
  - `require_api_key` FastAPI dependency — applied globally to ALL endpoints
  - Key lookup: `X-CERBERUS-Key` header (REST) / `?api_key=` query param (WS)
  - `secrets.compare_digest` — timing-attack-safe comparison
  - Simulation mode: key optional, startup proceeds with warning
  - Real-hardware mode: `CERBERUS_API_KEY` unset → startup `RuntimeError`

- **`backend/main.py`** — New payload REST surface (9 endpoints)
  - `GET  /payload` — status, contact state, compensator values
  - `POST /payload/attach` — validated `PayloadAttachCmd` Pydantic model
  - `POST /payload/detach`
  - `POST /payload/behavior/{ground_scout,belly_contact,thermal_rest,object_nudge,substrate_scan}`
  - All payload EventBus topics (9 topics) forwarded to WebSocket clients
  - `app = FastAPI(dependencies=[Depends(require_api_key)])` — global auth gate

- **`cerberus/plugins/plugin_manager.py`** — Base class and capability expansion
  - `CerberusPlugin.bridge` property — returns `self.engine.bridge` (clean shorthand)
  - New capability-gated wrappers: `set_body_height`, `switch_gait`, `set_foot_raise_height`,
    `set_speed_level`, `execute_sport_mode`, `set_led`
  - New capabilities: `control_gait`, `modify_safety_limits` added to `ALL_CAPABILITIES`
  - `COMMUNITY` trust level now includes `control_gait`
  - `discover_and_load()` now skips `__pycache__` and other dunder/dot directories
  - `discover_and_load()` checks `plugin_file.exists()` before attempting load

- **`tests/test_payload.py`** — 54 new tests
  - `TestPayloadConfig` (5) — geometry, COM auto-computation, material properties
  - `TestPayloadCompensatorGeometry` (6) — contact height, standing height, foot raise scaling
  - `TestSafetyLimitAdjustment` (10) — all limits tightened, never relaxed, 5° tilt floor
  - `TestCombinedCOM` (4) — COM lowered, bounded, lateral symmetry, gait ID scaling
  - `TestContactInference` (7) — NO_CONTACT / APPROACHING / CONTACT / DRAGGING / clearance sign
  - `TestDigitalAnatomyPayload` (7) — attach/detach, status, COM, energy cost
  - `TestUndercarriagePayloadPlugin` (13) — lifecycle, all 5 behavior triggers,
    concurrent behavior rejection, limits tightened/restored
  - API smoke tests (2)

### Fixed
- `PluginManager.discover_and_load()`: `__pycache__` treated as plugin directory
  caused spurious `No such file or directory` errors on every startup; fixed with
  leading-underscore / dot directory skip + `plugin_file.exists()` guard
- `CerberusPlugin`: no `bridge` property meant TRUSTED plugins had to use the
  verbose `self.engine.bridge` path; added property and 6 new capability-gated wrappers

### Changed
- Package version `2.3.0` → `2.4.0`
- Total test count: 96 → 150 (+54)

---



### Added
- **SimBridge sensor realism** (`cerberus/bridge/go2_bridge.py`)
  - `_sim_loop()` fully rewritten with physically-motivated models
  - Foot forces: trotting diagonal pattern (FL/RR ↔ FR/RL alternation), dynamic load scaling with speed, per-foot Gaussian noise
  - Joint torques: hip flexor 50% of foot load × 0.213m lever, knee 30%, hip abductor 15%
  - Joint positions: nominal trot pose with gait-phase oscillation
  - IMU pitch: integrates proportionally to commanded vx with damping
  - IMU roll: responds to vy + vyaw with damping
  - Battery: speed-proportional drain (moving ≈ 3× faster than idle)
  - TerrainArbiter now classifies terrain correctly in simulation
- **GoalQueue deliberative consumer** (`cerberus/cognitive/behavior_engine.py`)
  - `_execute_goal()` dispatcher: 16 sport mode goals + move, move_timed, height, stop, explore
  - `pending_goal` key injected into BT context every tick via `goals.peek()`
  - `goal_dispatch` BT node: highest-priority goal node added to deliberative layer above greet/explore
  - Goals are always popped in `finally` block — unknown goals can't stall the queue
  - `move_timed` duration clamped to [0.1, 30.0] seconds
- **WebSocket input validation** (`backend/main.py`)
  - `_handle_ws_command()` fully rewritten with explicit type coercion and range clamping
  - All numeric fields use `_float(key, default, lo, hi)` helper — strings/None return error, not crash
  - E-stop check added to move, sport_mode, body_height paths
  - `led` command added to WS surface (was REST-only)
  - Error responses include `"cmd"` field for client-side disambiguation
- **CORS security hardening** (`backend/main.py`)
  - Default origins restricted to `localhost:3000`, `localhost:5173`, `127.0.0.1:3000`
  - `allow_methods` restricted from `["*"]` to `["GET", "POST", "DELETE"]`
  - `allow_headers` restricted from `["*"]` to `["Content-Type", "Authorization"]`
  - Operators set `CORS_ORIGINS=*` explicitly for open access

### Fixed
- `math` and `random` missing from SimBridge module imports after `_sim_loop` rewrite

### Tests
- 12 new tests: goal queue consumer (5), SimBridge sensor realism (4), WS validation (3)
- Total: 96 passing
## [2.2.0] — 2026-03-28

### Added
- **TerrainArbiter Plugin** (`plugins/terrain_arbiter/plugin.py`)
  - Proprioceptive terrain classification from foot-force + IMU data
  - Six terrain classes: FLAT, ROUGH, SOFT, INCLINE_UP, INCLINE_DOWN, LATERAL_SLOPE
  - Rolling 60-sample `SensorWindow` with force variance, front/rear asymmetry, IMU means
  - Rule-based `TerrainClassifier` with configurable thresholds
  - `TransitionDebouncer` (hold_ticks=15) prevents gait thrashing on transient noise
  - Automatic `switch_gait()` + `set_foot_raise_height()` dispatch on terrain change
  - `GaitProfile` map covering all six terrain classes
  - Runtime `tune()` API for threshold adjustment without reload
  - Publishes `terrain.classification` events to EventBus
  - Compatible with SimBridge and RealBridge
- `GET /terrain` REST endpoint exposing TerrainArbiter plugin status
- WebSocket forwards `terrain.classification` events to UI clients in real time
- `tests/test_terrain_arbiter.py` — 40 tests covering SensorWindow, TerrainClassifier, TransitionDebouncer, GaitMap, and full plugin integration

### Fixed
- **Patch 1** — `RealBridge._run_sync()`: `asyncio.get_event_loop()` deprecated/raises in Python 3.10+/3.12; replaced with `asyncio.get_running_loop()`
- **Patch 2** — `SafetyWatchdog` battery state machine: BATTERY_WARN/LOW branches were unreachable once level > NOMINAL; fixed priority ordering + 2% hysteresis on recovery transition
- **Patch 3** — `CerberusEngine._loop()`: exception exit did not call `stop()`, leaving watchdog task and bridge connection open (resource leak); added `asyncio.ensure_future(self.stop())`
- **Patch 4** — `PluginManager.load_plugin_class()`: plugins loaded after startup (via API) were never registered as engine tick hooks; extracted `_register_hook_for_record()` called at load time; `register_with_engine()` is now idempotent
- **Patch 5** — `BehaviorEngine._build_tree()`: `__import__()` inside greeter lambda re-evaluated on every BT tick; replaced with `from ... import SportMode as _SportMode` captured at tree-construction time
- **Patch 6** — `PluginManager.load_from_file()`: two independent bugs in the sandboxed loader:
  - `module.__name__` override after `module_from_spec` caused `"loader for X cannot handle Y"` from spec loader
  - Not adding module to `sys.modules` before `exec_module` caused Python 3.10+ `@dataclass` to crash (`'NoneType' has no __dict__`)
  - Fix: pass unique name to `spec_from_file_location`; register in `sys.modules[unique_name]` pre-exec; clean up on unload
- **Missing dev dep** — `asgi-lifespan>=2.1.0` added to `[dev]` extras; its absence caused all 19 API integration tests to error on fresh install

### Changed
- Package version bumped `2.1.0` → `2.2.0`
- `pyproject.toml` `[dev]` extras: added `asgi-lifespan>=2.1.0`

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

