# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System
## Vision Document — v2.1.0

**Repository:** [CERBERUS-UnitreeGo2CompanionAPI](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI)
**Last Updated:** March 2026
**Status:** Active Development — v2.1.0

---

## 1. Project Overview

CERBERUS is **more than a control API**: it is a **cognitive-emulative engine**, a **body-aware motion system**, and a **reactive utility framework** for the Unitree Go2 quadruped robot. Its mission is to merge **AI-driven autonomy, physical realism, learning capability, and human-aware interaction** into a single platform.

Three pillars:

- **Mind:** Cognitive processing, behavior trees, goal-oriented planning, learning, and environmental awareness.
- **Body:** Digital anatomy with accurate kinematics, energy/fatigue modeling, COM tracking, and stability analysis.
- **System:** Robust, plugin-based architecture with DDS communication, safety watchdogs, simulation support, and CI/CD.

---

## 2. Vision Statement

To create a **self-aware, responsive, and emulative quadruped robotic system** that:

- Demonstrates **realistic canine-like behaviors** while interacting intelligently with users and environments.
- Learns, adapts, and personalizes behavior over time.
- Offers a **fully extensible platform** through a sandboxed, capability-based plugin ecosystem.
- Provides **research-grade observability, simulation, and developer tools**.
- Is **safe by design** — safety constraints cannot be overridden by any plugin or user command.

---

## 3. SDK Integration Architecture (v2.1.0)

### 3.1 Communication Layer

CERBERUS communicates with the Go2 using the **Unitree SDK2 Python (`unitree_sdk2_python`) DDS layer** — **not** HTTP or direct TCP/IP.

```
CERBERUS Engine
      │
      ▼
 Go2Bridge (cerberus/bridge/go2_bridge.py)
      │
      ▼
 CycloneDDS (pub/sub over Ethernet)
      │
      ├── SportClient.Init()         ← High-level motion commands
      ├── LowLevelCmd publisher      ← Direct joint control (TRUSTED plugins only)
      └── SportModeState subscriber  ← State feedback at ~50Hz
```

**Network setup:** Connect your computer to the Go2's Ethernet port (192.168.123.x). Set your interface:
```bash
export GO2_NETWORK_INTERFACE=eth0   # or enp2s0, etc.
```

### 3.2 Simulation Mode

When `GO2_SIMULATION=true`, CERBERUS uses `SimBridge` — a full behavioral simulation with:
- Realistic state drift (IMU noise, battery drain)
- All sport modes available (logged to console)
- Drop-in replacement for `RealBridge`
- Optional MuJoCo physics integration

### 3.3 Available Sport Modes (All 17)

| Mode | Description |
|------|-------------|
| `damp` | Motor damp — passive mode |
| `balance_stand` | Stand with active balance |
| `stop_move` | Stop all motion |
| `stand_up` | Stand from sitting/lying |
| `stand_down` | Lie down |
| `sit` | Sit like a dog |
| `rise_sit` | Rise from sit |
| `hello` | Wave hello |
| `stretch` | Stretch routine |
| `wallow` | Roll around |
| `scrape` | Scrape paw |
| `front_flip` | Front flip (requires open space!) |
| `front_jump` | Jump forward |
| `front_pounce` | Pounce |
| `dance1` | Dance routine 1 |
| `dance2` | Dance routine 2 |
| `finger_heart` | Finger heart gesture |

### 3.4 Motion Configuration Parameters

| Parameter | Range | Description |
|-----------|-------|-------------|
| `body_height` | [-0.1, +0.1] | Relative height offset from default (m) |
| `speed_level` | [-1, 0, 1] | Speed range multiplier |
| `euler` (roll) | [-0.75, 0.75] rad | Body orientation |
| `euler` (pitch) | [-0.75, 0.75] rad | |
| `euler` (yaw) | [-1.5, 1.5] rad | |
| `foot_raise_height` | [-0.06, 0.03] | Foot lift during gait (m) |
| `switch_gait` | [0–4] | Gait type selector |
| `move` vx | [-1.5, 1.5] m/s | Forward/back velocity |
| `move` vy | [-0.8, 0.8] m/s | Lateral velocity |
| `move` vyaw | [-2.0, 2.0] rad/s | Rotational velocity |

---

## 4. Core Objectives

| # | Objective | Status (v2.1.0) |
|---|-----------|-----------------|
| 1 | Intelligent Autonomy — reactive/deliberative/reflective layers | ✅ Implemented |
| 2 | Embodiment & Physical Realism — kinematics, COM, fatigue | ✅ Implemented |
| 3 | Perception & Understanding — sensor fusion, YOLO integration | ✅ Plugin ready |
| 4 | Learning & Adaptation — RL/imitation hooks | 🔄 Scaffold (Phase 3) |
| 5 | Behavior & Personality — traits, mood, behavior trees | ✅ Implemented |
| 6 | Plugin Ecosystem — sandboxed, capability-based | ✅ Implemented |
| 7 | Simulation & Observability — SimBridge, debug endpoints | ✅ Implemented |
| 8 | Safety, Reliability — watchdog, E-stop, tilt detection | ✅ Implemented |
| 9 | Developer Experience — FastAPI, WebSocket, tests, CI | ✅ Implemented |

---

## 5. System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    CERBERUS Engine                       │
│   Tick loop 30–200Hz | Priority scheduling              │
│                                                          │
│  1. Safety Watchdog  ← HIGHEST PRIORITY                 │
│  2. Bridge Dispatch  ← DDS commands to Go2              │
│  3. Behavior Engine  ← 3-layer cognitive system         │
│  4. Perception       ← YOLO, IMU, sensor fusion         │
│  5. Anatomy          ← Kinematics, COM, fatigue         │
│  6. Learning         ← RL / imitation (low rate)        │
│  7. Plugins          ← Sandboxed, capability-gated      │
│  8. UI Broadcast     ← WebSocket state stream           │
└──────────────────────┬──────────────────────────────────┘
                       │
              ┌────────┴────────┐
              │   Go2 Bridge    │
              │  RealBridge (DDS)│
              │  SimBridge (mock)│
              └────────┬────────┘
                       │ CycloneDDS
                       ▼
                   Unitree Go2
```

### 5.1 Safety System (Non-Bypassable)

The safety watchdog runs as a separate asyncio task at 50Hz and enforces:

- **Heartbeat timeout:** Auto-stops motion if no command received in 5s (configurable)
- **Tilt detection:** E-stop if roll or pitch exceeds 30° (fall detection)
- **Battery critical:** E-stop at 4% remaining
- **Battery low:** Warning at 15%, reduced speed mode at 8%
- **Velocity guardrails:** Hard clamp + reject commands over limits
- **Audit log:** All safety events written to `logs/safety_audit.jsonl`
- **E-stop:** One-way in real mode — requires service restart

### 5.2 Plugin Trust Levels

| Trust Level | Capabilities |
|-------------|-------------|
| `TRUSTED` | Full access (all capabilities) |
| `COMMUNITY` | `read_state`, motion, LED, audio, sport modes, events |
| `UNTRUSTED` | `read_state` only |

Plugins that request capabilities beyond their trust level are **rejected at load time**.

---

## 6. Feature Details

### 6.1 Behavior Tree Structure

```
Root (Selector)
├── Reactive Layer (Sequence)
│   ├── Condition: estop_clear
│   └── Selector: obstacle_avoid
│       ├── Condition: no_obstacle
│       └── Action: stop_for_obstacle
├── Deliberative Layer (Selector)
│   ├── Sequence: greet_human
│   │   ├── Condition: human_detected
│   │   ├── Condition: not_greeted_recently
│   │   └── Action: hello_wave
│   └── Selector: explore_or_idle
│       ├── Sequence: explore
│       └── Action: idle_stand
└── Reflective Layer (Sequence)
    └── Selector: boredom
        ├── Condition: not_bored
        └── Action: play_behavior
```

### 6.2 Personality System

Five traits (Big Five-inspired, tuned for canine behavior):

- `energy` — how active and initiating
- `friendliness` — how much it seeks interaction
- `curiosity` — response to novel stimuli
- `loyalty` — follows the operator
- `playfulness` — frequency of play behaviors

Traits are configurable via `.env` (`PERSONALITY_ENERGY=0.7` etc.) and modulated by mood state in real time.

### 6.3 Digital Anatomy Model

- 12-DOF joint model (FL/FR/RL/RR × hip_ab/hip_flex/knee)
- Forward kinematics from Go2 URDF link lengths
- Center of Mass tracking + support polygon convex hull
- Stability margin (minimum COM-to-edge distance)
- Per-joint fatigue accumulator (intensity × velocity × time)
- Energy model — idle power + joint power → remaining runtime estimate

---

## 7. API Quick Reference

### REST

```bash
# Status
GET  /              # System status
GET  /state         # Full robot state
GET  /stats         # Engine performance
GET  /anatomy       # Joints, COM, energy
GET  /behavior      # Cognitive status

# Motion
POST /motion/stand_up
POST /motion/stand_down
POST /motion/stop
POST /motion/move          {"vx": 0.3, "vy": 0.0, "vyaw": 0.0}
POST /motion/sport_mode    {"mode": "hello"}
POST /motion/body_height   {"height": 0.05}
POST /motion/euler         {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
POST /motion/gait          {"gait_id": 2}
POST /motion/speed_level   {"level": 1}

# Safety
POST /safety/estop
POST /safety/clear_estop   # Simulation only

# Peripherals
POST /led            {"r": 255, "g": 0, "b": 0}
POST /volume         {"level": 50}
POST /obstacle_avoidance  {"enabled": true}

# Cognitive
POST /behavior/goal  {"name": "greet_user", "priority": 0.8}

# Plugins
GET  /plugins
POST /plugins/{name}/enable
POST /plugins/{name}/disable
DELETE /plugins/{name}
```

### WebSocket (`ws://host:8080/ws`)

```json
// Send — motion commands
{"cmd": "move", "vx": 0.3, "vy": 0.0, "vyaw": 0.0}
{"cmd": "stop"}
{"cmd": "estop"}
{"cmd": "sport_mode", "mode": "hello"}

// Receive — state broadcast at 30Hz
{"type": "state", "data": { ... full robot state ... }}
{"type": "ping"}
{"type": "error", "msg": "..."}
```

---

## 8. Installation

### Quick Start (Simulation)

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt

cp .env.example .env
# Set GO2_SIMULATION=true in .env

cerberus
# API available at http://localhost:8080
```

### Real Hardware

```bash
# 1. Install CycloneDDS
git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x
cd cyclonedds && mkdir build install && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=../install && cmake --build . --target install

# 2. Install unitree_sdk2_python
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
export CYCLONEDDS_HOME=~/cyclonedds/install
pip install -e .

# 3. Configure
cp .env.example .env
# Set GO2_SIMULATION=false
# Set GO2_NETWORK_INTERFACE=eth0  (your interface)

# 4. Run
cerberus
```

---

## 9. Community Projects Referenced

| Project | Usage |
|---------|-------|
| `unitreerobotics/unitree_sdk2_python` | DDS bridge, SportClient, state subscription |
| `Unitree-Go2-Robot/go2_robot` | Sport mode list, body configuration services |
| `unitreerobotics/unitree_rl_gym` | RL training environment reference |
| `unitreerobotics/unitree_mujoco` | MuJoCo simulation (future integration) |
| `unitreerobotics/teleimager` | Camera streaming reference |

---

## 10. Future Directions

- **Multi-agent coordination** — swarm behaviors via DDS group topics
- **Predictive world modeling** — MuJoCo-backed planning
- **Voice/NLU commands** — Whisper STT + LLM intent parsing
- **Advanced personality evolution** — traits drift over interaction history
- **MuJoCo simulation integration** — full physics with sim-to-real transfer
- **ROS2 bridge** — optional ROS2 topic exposure for research workflows
- **Mobile companion app** — Flutter UI connecting to WebSocket API

---

## 11. Success Metrics

- Stable runtime at target Hz in both simulation and hardware
- All 17 sport modes functional with safety validation
- Plugin ecosystem with sandboxing, trust levels, and error isolation
- Demonstrated adaptive, human-aware behavior in simulation
- Safe, resource-aware, watchdog-protected operation
- Test suite with ≥ 90% coverage of critical paths

---

## 12. Conclusion

CERBERUS v2.1.0 delivers on the core Vision: a unified **mind + body + system** platform for the Unitree Go2. The DDS communication layer, full sport mode coverage, three-layer behavior architecture, capability-sandboxed plugin system, and non-bypassable safety watchdog form a solid foundation for research-grade experimentation and companion robotics development.
