# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System
## Vision Document — v3.1.1

**Repository:** [CERBERUS-UnitreeGo2CompanionAPI](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI)
**Status:** Active Development — v3.1.1 · 129 Tests Passing
**Last Updated:** 2026-03-27

---

## 1. Project Overview

CERBERUS is a **cognitive-emulative engine**, a **body-aware motion system**, and a **reactive utility framework** for Unitree Go2 quadrupedal robotics. It merges AI-driven autonomy, physical realism, natural language control, learning, and human-aware interaction into a single extensible platform.

Three pillars:

| Pillar | Description |
|--------|-------------|
| **Mind** | Cognitive engine, NLU, goal planning, personality, learning |
| **Body** | All 17 native Go2 sport modes, safety-constrained motion, kinematics |
| **System** | Plugin ecosystem, REST/WebSocket API, web dashboard, CLI, data logging |

---

## 2. Vision Statement

To create a **self-aware, responsive, and emulative quadruped robotic system** that:

- Responds to plain English ("walk forward slowly", "do a finger heart", "go limp")
- Demonstrates realistic canine behaviors (greet, wag, stretch, patrol, alert)
- Learns and personalizes behavior over time via mood adaptation and session logging
- Provides a fully extensible platform through a sandboxed plugin ecosystem
- Serves developers, researchers, and enthusiasts with equal ease

---

## 3. Architecture

### 3.1 Transport Layer

| Transport | Models | Protocol | Install |
|-----------|--------|----------|---------|
| `mock` | — | In-memory | Nothing (default) |
| `dds` | Go2 EDU | CycloneDDS (Ethernet) | `pip install unitree_sdk2py` |
| `webrtc` | AIR / PRO / EDU | WebRTC (Wi-Fi) | `pip install go2_webrtc_connect` |

### 3.2 NLU Coverage (v3.1.1)

All 17 Go2 sport modes reachable via rule-based NLU (no LLM required):

| Phrase | Mode / Behavior |
|--------|----------------|
| "damp" / "go limp" / "safe park" | `damp` |
| "stand up" / "get up" | `stand_up` (behavior) |
| "lie down" / "stand down" | `stand_down` |
| "sit" / "sit down" | `sit` (behavior) |
| "rise from sit" / "rise sit" | `rise_sit` |
| "say hello" / "wave" / "greet" | `hello` (behavior) |
| "stretch" / "the robot looks tired" | `stretch` (behavior) |
| "wag your tail" / "wiggle" / "happy" | `wallow` (behavior) |
| "scrape" / "scrape the ground" | `scrape` |
| "front flip" / "do a flip" | `front_flip` |
| "jump" / "leap" | `front_jump` |
| "pounce" / "front pounce" | `front_pounce` |
| "dance" / "boogie" | `dance1/2` (behavior) |
| "finger heart" | `finger_heart` |
| "go limp" / "damp" | `damp` |
| "walk forward [slowly/at 0.8 m/s]" | `move(vx)` |
| "spin left" / "rotate right" | `move(vyaw)` — pure rotation |
| "go left" / "strafe right" | `move(vy)` — lateral strafe |
| "emergency stop" | `emergency_stop` |
| "turn obstacle avoidance on/off" | `config_obstacle` |
| "turn the lights on" / "dim the lights" | `vui(brightness)` |
| "volume up" / "turn it down" | `vui(volume)` |
| "set height to 45cm" | `config(height=0.45)` |
| "follow me" | `patrol` behavior |

### 3.3 Safety Gate

Every motion command validates against SafetyGate — no bypass except `emergency_stop()`:

| Guard | Standard (Air/Pro/EDU) | EDU+ (15000mAh) |
|-------|----------------------|-----------------|
| Battery warn | 22.0 V | 25.0 V |
| Battery block | 20.5 V | 23.5 V |
| Tilt warn | 20° | 20° |
| Tilt block | 40° | 40° |
| Max velocity | vx 1.5, vy 0.8, vyaw 2.0 m/s | same |
| Special motion cooldown | 3 s | 3 s |

Use `SafetyConfig.for_edu_plus()` for the 28.8V extended-battery variant.

### 3.4 Full API Surface

```
GET  /health                     Liveness probe
GET  /api/v1/state               Full robot state (JSON)
POST /api/v1/move                Velocity control {vx, vy, vyaw}
POST /api/v1/stop                Stop motion
POST /api/v1/emergency_stop      Hard damp (bypasses queue)
POST /api/v1/stand               {action: "up"|"down"}
POST /api/v1/mode                {mode: str} — 17 modes
POST /api/v1/config/height       {height: 0.3–0.5}
POST /api/v1/config/euler        {roll, pitch, yaw}
POST /api/v1/config/speed        {level: -1|0|1}
POST /api/v1/config/foot_raise   {height: -0.06–0.03}
POST /api/v1/config/obstacle     {enabled: bool}
POST /api/v1/vui                 {volume: 0-100, brightness: 0-100}
POST /api/v1/behavior            {behavior: str, params: {}}
GET  /api/v1/behaviors           List + execution history
POST /api/v1/nlu/command         {text: str, execute: bool, llm_fallback: bool}
GET  /api/v1/personality         Traits + mood + mood_label
GET  /api/v1/sessions            List recorded NDJSON sessions
POST /api/v1/replay              {session_file: str, speed: float}
GET  /api/v1/plugins             Plugin status
POST /api/v1/plugins/load        {manifest_path: str}
POST /api/v1/plugins/unload      {name: str}
WS   /ws/telemetry               10 Hz push + inbound commands
GET  /ui/                        Live web dashboard
```

---

## 4. Deliverables Status

| Deliverable | Status | Notes |
|-------------|--------|-------|
| Hardware bridge (DDS + WebRTC + Mock) | ✅ Complete | All 17 modes, auto-reconnect |
| Safety gate | ✅ Complete | Battery/tilt/velocity/cooldown, EDU+ variant |
| Cognitive engine (3-layer) | ✅ Complete | Reactive 20Hz, deliberative 1Hz, reflective |
| Behavior engine (10 behaviors) | ✅ Complete | Priority queue, cooldowns, history |
| Personality model | ✅ Complete | Traits + mood, JSON persistence |
| NLU interpreter (rule + LLM) | ✅ Complete | 17/17 modes reachable, 25+ command types |
| Data logger + replay | ✅ Complete | NDJSON + gzip, SessionReplayer |
| Plugin system (4-tier trust) | ✅ Complete | Auto-discovery, lifecycle hooks |
| REST + WebSocket API (21 endpoints) | ✅ Complete | Full Pydantic v2 validation |
| Web dashboard | ✅ Complete | D-pad, modes, NLU chat, telemetry |
| CLI tool | ✅ Complete | 10 subcommands, registered entry point |
| Simulation environment | ✅ Complete | Physics-lite, battery drain, events |
| Perception pipeline (YOLO/MediaPipe) | 🚧 Stub | Scaffold wired to WebRTC video/LIDAR |
| Test suite | ✅ 129 tests | 10 test classes, all passing |
| Documentation | ✅ Complete | README, Architecture, Vision, Changelog |
| CI/CD (GitHub Actions) | ✅ Complete | Python 3.11+3.12, lint, security, Docker |
| SLAM / Nav2 integration | 📋 v4.0 | |
| Reinforcement learning pipeline | 📋 v4.0 | |
| Voice interface (Whisper + TTS) | 📋 v4.0 | |

---

## 5. Community Projects Adopted

| Project | Stars | What CERBERUS borrowed |
|---------|-------|----------------------|
| `unitree_sdk2_python` (official) | — | DDS SportClient, VuiClient, ObstaclesAvoidClient, all mode names |
| `go2_webrtc_connect` (phospho-app) | — | WebRTC transport, AP/STA/remote connection, API IDs |
| `unitree_webrtc_connect` (legion1581) | — | WebRTC fallback, serial number discovery |
| `go2_robot` (URJC ROS2) | 285⭐ | Sport service mode list, config params |
| `unitree-go2-mcp-server` (lpigeon) | 67⭐ | NLU command interpretation pattern |
| `logging-mp` (Unitree official) | — | Multiprocess-safe structured logging design |
| Go2 EDU+ spec (2026 hardware) | — | 28.8V / 15000mAh battery thresholds |

---

## 6. Future Directions (v4.0+)

- **Vision pipeline** — YOLO v11 person tracking, obstacle mapping from LIDAR pointcloud
- **SLAM navigation** — `go2_ros2_sdk` / RTAB-Map autonomous mapping and waypoint following
- **Voice interface** — Whisper STT + Go2 built-in speaker TTS (EDU/Pro only)
- **World model** — leverage Unitree's `unifolm-world-model-action` (903⭐) architecture
- **RL training** — `unitree_rl_gym` / `unitree_mujoco` sim-to-real pipeline
- **Multi-agent** — fleet management for swarm behaviors
- **Personality evolution** — long-term trait drift from interaction history

---

## 7. Success Metrics

- All 17 sport modes reachable via NLU rules ✅
- SafetyGate zero-bypass policy enforced ✅
- 129 tests, all passing ✅
- Three hardware transports working (mock verified, DDS/WebRTC designed for real hardware)
- NLU rule interpreter covers all common commands without LLM
- Plugin ecosystem with trust enforcement end-to-end
