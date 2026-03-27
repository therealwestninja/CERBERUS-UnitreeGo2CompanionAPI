# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System
## Vision Document — v3.0

**Repository:** [CERBERUS-UnitreeGo2CompanionAPI](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI)
**Status:** Active Development — v3.0.0
**Last Updated:** 2026-03-27

---

## 1. Project Overview

CERBERUS is **more than a control API**: it is a **cognitive-emulative engine**, a **body-aware motion system**, and a **reactive utility framework** for Unitree Go2 quadrupedal robotics. Its mission is to merge **AI-driven autonomy, physical realism, learning capability, and human-aware interaction** into a single platform.

Three pillars:

| Pillar | Description |
|--------|-------------|
| **Mind** | Cognitive processing, goal-oriented behavior, learning, and environmental awareness |
| **Body** | Realistic digital anatomy, kinematics, energy modeling, and stability-aware motion |
| **System** | Robust plugin-based architecture with observability, simulation, safety, and CI/CD |

---

## 2. Vision Statement

To create a **self-aware, responsive, and emulative quadruped robotic system** that:

- Demonstrates **realistic canine-like behaviors** while interacting intelligently with users and environments
- Learns, adapts, and personalizes behavior over time
- Offers a **fully extensible platform** through a plugin ecosystem
- Provides **research-grade observability, simulation, and developer tools**
- Serves as a **modular framework** for further experimentation in AI, robotics, and autonomous systems

---

## 3. Core Objectives

1. **Intelligent Autonomy** — Layered decision-making: reactive (20 Hz safety loop), deliberative (1 Hz goal planning), reflective (personality modulation)
2. **Embodiment & Physical Realism** — Accurate kinematics, COM stability, energy/fatigue modeling, and digital anatomy
3. **Perception & Understanding** — Sensor fusion and semantic interpretation for objects, environments, and humans
4. **Learning & Adaptation** — Reinforcement, imitation, and preference-based learning pipelines
5. **Behavior & Personality** — Behavior trees/hybrid engines, mood and personality states, human-aware modulation
6. **Extensibility & Plugins** — Modular, sandboxed plugin ecosystem with manifest, versioning, and 4-tier trust model
7. **Simulation & Observability** — Real-time simulation, debug overlays, event timelines, state inspection, and scenario testing
8. **Safety, Ethics & Reliability** — Fault-tolerant systems, watchdogs, fallback modes, plugin trust levels, and resource-aware behavior
9. **Developer Experience** — Quick-start guides, CLI, plugin templates, CI/CD pipelines, and testing workflows

---

## 4. Architecture

### 4.1 Transport Layer (NEW v3.0)

CERBERUS now supports **three hardware transports**, selected via `config/cerberus.yaml`:

| Transport | Models | Protocol | Notes |
|-----------|--------|----------|-------|
| `mock` | — | In-memory | Default; CI/simulation; no hardware needed |
| `dds` | Go2 EDU | CycloneDDS (Ethernet) | Requires `unitree_sdk2_python` |
| `webrtc` | AIR / PRO / EDU | WebRTC (Wi-Fi) | Requires `go2_webrtc_connect`; no jailbreak |

All transports expose an identical `Go2Bridge` interface — application code is **transport-agnostic**.

### 4.2 Safety Gate (ENHANCED v3.0)

Every motion command passes through `SafetyGate` before reaching hardware:

- **Battery guard** — warn at 22 V, block motion at 20.5 V
- **Tilt guard** — throttle at 20°, hard block at 40° IMU tilt
- **Velocity clamp** — hard limits: vx ±1.5 m/s, vy ±0.8 m/s, vyaw ±2.0 rad/s
- **Special-motion cooldown** — configurable per-mode (default 3 s for flips/dances)
- **Violation audit log** — all safety blocks logged with counter

### 4.3 Cognitive Architecture (NEW v3.0)

Three-layer decision system:

```
Layer 3 — Reflective    Personality (mood, traits) → modulates goal selection
Layer 2 — Deliberative  Goal planner (1 Hz)        → schedules BehaviorEngine calls
Layer 1 — Reactive      Safety monitor (20 Hz)     → overrides with emergency_sit
```

### 4.4 Behavior Engine

Priority-queued async executor. 10 built-in canine behaviors, all wired to Go2 SDK:

| Behavior | Description | Maps to |
|----------|-------------|---------|
| `idle` | Balance stand | `balance_stand` |
| `sit` | Sit down | `sit` |
| `greet` | Head tilt + hello gesture | `euler` + `hello` |
| `stretch` | Full body stretch | `stretch` |
| `dance` | Random dance1/dance2 | `dance1`/`dance2` |
| `patrol` | Square walk loop | `move()` sequence |
| `wag` | Tail-wag emulation | `wallow` |
| `alert` | Attentive posture | height + euler |
| `emergency_sit` | Hard-stop + damp | `emergency_stop()` |

### 4.5 Personality Model

```
Traits (stable)          Mood (dynamic, decays)
─────────────────────    ─────────────────────────────
sociability  0–1         valence  -1 (negative) → +1 (positive)
playfulness  0–1         arousal   0 (calm)      →  1 (excited)
energy       0–1
curiosity    0–1
```

Mood events: `on_interaction()`, `on_battery_low()`, `on_obstacle()`, `on_task_success()`.
Persists across restarts via JSON.

### 4.6 Plugin System (ENHANCED v3.0)

4-tier trust model:

| Trust Level | Capabilities |
|-------------|-------------|
| `core` | motion + perception + vui + config + admin |
| `trusted` | motion + perception + vui |
| `community` | perception (read-only) |
| `untrusted` | no hardware access |

Plugins are auto-discovered from `plugins/` via `plugin.yaml` manifests.

### 4.7 API Surface (NEW v3.0)

Full REST + WebSocket API via FastAPI:

```
GET  /health                     Liveness probe
GET  /api/v1/state               Full robot state snapshot
POST /api/v1/move                Velocity control (vx, vy, vyaw)
POST /api/v1/stop                Stop motion
POST /api/v1/emergency_stop      Hard damp
POST /api/v1/stand               stand_up / stand_down
POST /api/v1/mode                Set named sport mode (17 modes)
POST /api/v1/config/height       Body height [0.3–0.5 m]
POST /api/v1/config/euler        Roll/pitch/yaw posture
POST /api/v1/config/speed        Speed level [-1, 0, 1]
POST /api/v1/config/foot_raise   Foot raise height
POST /api/v1/config/obstacle     Toggle obstacle avoidance
POST /api/v1/vui                 Volume + LED brightness
POST /api/v1/behavior            Trigger named behavior
GET  /api/v1/behaviors           List + history
GET  /api/v1/personality         Traits + mood
GET  /api/v1/plugins             Plugin status
POST /api/v1/plugins/load        Load plugin by manifest path
POST /api/v1/plugins/unload      Unload named plugin

WS   /ws/telemetry               10 Hz state stream; accepts inbound commands
```

---

## 5. Supported Go2 Modes (Complete List)

All 17 native sport modes from `unitree_sdk2_python` / `go2_robot` ROS2 service:

`damp` · `balance_stand` · `stop_move` · `stand_up` · `stand_down` · `sit` · `rise_sit` · `hello` · `stretch` · `wallow` · `scrape` · `front_flip` · `front_jump` · `front_pounce` · `dance1` · `dance2` · `finger_heart`

---

## 6. Community Integrations Adopted (v3.0)

| Project | What we borrowed |
|---------|-----------------|
| `unitree_sdk2_python` (official) | DDS SportClient, VuiClient, ObstaclesAvoidClient |
| `go2_webrtc_connect` (phospho-app) | WebRTC transport, connection methods, audio channel |
| `unitree_webrtc_connect` (legion1581) | Fallback WebRTC, serial discovery |
| `go2_robot` ROS2 (URJC) | Complete mode list, config params, service API design |
| `go2_ros2_sdk` (abizovnuralem) | SLAM/nav patterns, camera streaming design |

---

## 7. Deliverables Status

| Deliverable | Status |
|-------------|--------|
| Architecture diagrams | 📋 Planned |
| Core runtime engine | ✅ Complete (`cerberus/core/`) |
| Cognitive system | ✅ Complete (`cerberus/core/cognitive.py`) |
| Hardware bridge (mock + DDS + WebRTC) | ✅ Complete (`cerberus/hardware/go2_bridge.py`) |
| Safety gate | ✅ Complete (`cerberus/safety/gate.py`) |
| Behavior engine (10 behaviors) | ✅ Complete (`cerberus/behavior/engine.py`) |
| Personality system | ✅ Complete (`cerberus/personality/model.py`) |
| Plugin system | ✅ Complete (`cerberus/plugins/manager.py`) |
| REST + WebSocket API | ✅ Complete (`backend/api/server.py`) |
| Configuration system | ✅ Complete (`config/cerberus.yaml`) |
| Test suite (~50 tests) | ✅ Complete (`tests/test_cerberus.py`) |
| Perception pipeline | 🚧 Stub (camera, LIDAR, object detection) |
| Learning system | 🚧 Stub |
| Simulation environment | 🚧 Partial |
| Data logging / replay | 📋 Planned |
| CLI tools | 📋 Planned |
| UI dashboard | 🚧 Partial |

---

## 8. Future Directions (v4.0+)

- **Multi-agent coordination** — swarm behaviors, multi-robot fleet manager
- **Predictive world modeling** — planning and risk assessment
- **Voice/NLU integration** — whisper STT + LLM command parsing
- **Advanced personality evolution** — long-term trait drift from experience
- **Vision pipeline** — YOLO v11 integration for real-time object/person detection
- **SLAM navigation** — autonomous map building and waypoint navigation
- **Architecture diagrams** — system architecture as code (Mermaid)

---

## 9. Target Audience

| Audience | Use Case |
|----------|----------|
| **Researchers** | Autonomous systems, robotics, AI behavior modeling |
| **Developers** | Plugin development, system extension, testing AI interactions |
| **Enthusiasts** | Realistic robotic companions, educational and experimental use |

---

## 10. Success Metrics

- Stable runtime on both simulation and real Go2 hardware (all 3 transports)
- SafetyGate zero-bypass policy (no command reaches hardware without validation)
- Robust plugin ecosystem with sandboxing, versioning, and trust enforcement
- Demonstrated adaptive, autonomous, and human-aware behavior
- Test coverage ≥ 80% (core modules)
- Community adoption: forks, plugin contributions, research citations

---

## 11. Conclusion

**CERBERUS v3.0** delivers the complete **mind–body–system** triad envisioned in the original specification:

- **Mind**: Three-layer cognitive engine + personality + learning hooks
- **Body**: Unified Go2 bridge (DDS + WebRTC) with full 17-mode support, safety gate, and canine behavior library
- **System**: Plugin ecosystem with trust enforcement, REST/WebSocket API, comprehensive tests, and CI/CD

It is **modular, extensible, research-grade, and developer-friendly** — and it actually talks to the robot.
