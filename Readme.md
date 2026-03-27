# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI/CD](https://img.shields.io/badge/CI-CD-blue)](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688)](https://fastapi.tiangolo.com)

CERBERUS is a **fully autonomous, adaptive, and intelligent quadrupedal robotics platform** for the Unitree Go2. It merges **cognitive intelligence, digital anatomy, learning, perception, and a reactive plugin ecosystem** into a single robust system — and it actually talks to the robot.

---

## 🚀 Quick Start

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt

# Simulation / CI (default — no hardware needed)
python -m backend.api.server
```

Open `http://localhost:8080/docs` for interactive API documentation.

---

## 🤖 Connecting to Real Hardware

### Go2 EDU — Wired Ethernet (DDS)

```bash
pip install unitree_sdk2py
# Edit config/cerberus.yaml:
#   robot:
#     transport: dds
#     network_interface: eth0
python -m backend.api.server
```

> Requires `cyclonedds==0.10.2`. See [unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python) for build instructions.

### Go2 AIR / PRO / EDU — Wi-Fi (WebRTC, no jailbreak)

```bash
pip install go2_webrtc_connect
# Edit config/cerberus.yaml:
#   robot:
#     transport: webrtc
#     webrtc_method: local_sta
#     robot_ip: 192.168.8.1     # or use serial_number for auto-discovery
python -m backend.api.server
```

> Works with all models out of the box. Disconnect the Unitree Go app before connecting.

---

## ⚙️ Installation

```bash
# Core dependencies
pip install -r requirements.txt

# Optional: DDS (Go2 EDU wired)
pip install unitree_sdk2py

# Optional: WebRTC Wi-Fi (all models)
pip install go2_webrtc_connect

# Optional: Vision
pip install ultralytics mediapipe opencv-python

# Optional: Audio (Go2 Pro/EDU only)
pip install PyAudio SpeechRecognition
```

---

## 📖 Usage

### Python API

```python
from cerberus.hardware.go2_bridge import Go2Bridge

# Simulation
bridge = Go2Bridge.from_config({"transport": "mock"})
await bridge.connect()

# Walk forward
await bridge.move(0.5, 0.0, 0.0)

# Perform a greeting behavior
from cerberus.behavior.engine import BehaviorEngine
engine = BehaviorEngine(bridge)
await engine.start()
await engine.enqueue("greet")

# Robot state
state = await bridge.get_state()
print(f"Battery: {state.battery_voltage}V  Mode: {state.current_mode}")
```

### REST API

```bash
# Get full state
curl http://localhost:8080/api/v1/state

# Walk forward 0.5 m/s
curl -X POST http://localhost:8080/api/v1/move \
     -H "Content-Type: application/json" \
     -d '{"vx": 0.5, "vy": 0.0, "vyaw": 0.0}'

# Perform greeting
curl -X POST http://localhost:8080/api/v1/mode \
     -H "Content-Type: application/json" \
     -d '{"mode": "hello"}'

# Trigger a behavior
curl -X POST http://localhost:8080/api/v1/behavior \
     -H "Content-Type: application/json" \
     -d '{"behavior": "greet"}'

# Emergency stop
curl -X POST http://localhost:8080/api/v1/emergency_stop
```

### WebSocket Telemetry

```javascript
const ws = new WebSocket("ws://localhost:8080/ws/telemetry");
ws.onmessage = (event) => {
  const state = JSON.parse(event.data);
  console.log(state.battery, state.current_behavior);
};

// Send commands inbound
ws.send(JSON.stringify({ action: "move", vx: 0.3, vy: 0.0, vyaw: 0.0 }));
ws.send(JSON.stringify({ action: "behavior", behavior: "greet" }));
```

---

## 🧠 Cognitive Architecture

```
Layer 3 — Reflective    Personality (mood, traits)
              ↓                modulates
Layer 2 — Deliberative  Goal planner (1 Hz)
              ↓                schedules
     BehaviorEngine            queues and executes
              ↓
Layer 1 — Reactive      Safety monitor (20 Hz) — emergency override
              ↓
       Go2Bridge + SafetyGate → Hardware (DDS / WebRTC)
```

---

## 🛡️ Safety

All motion commands are validated by `SafetyGate` before reaching hardware:

| Guard | Threshold | Action |
|-------|-----------|--------|
| Battery warn | < 22.0 V | Log warning |
| Battery block | < 20.5 V | Block all motion |
| Tilt warn | > 20° | Log warning |
| Tilt block | > 40° | Block all motion |
| Velocity hard limit | vx > ±1.5 m/s | Clamp + reject |
| Special motion cooldown | 3 s (configurable) | Queue reject |

`/api/v1/emergency_stop` **always** bypasses the queue and issues `Damp` immediately.

---

## 🎯 Available Modes

All 17 native Go2 sport modes:

| Mode | Description |
|------|-------------|
| `damp` | Joints go limp (safe park) |
| `balance_stand` | Default standing balance |
| `stop_move` | Stop walking, hold position |
| `stand_up` | Rise to standing |
| `stand_down` | Lower to lying |
| `sit` | Sit down |
| `rise_sit` | Rise from sitting |
| `hello` | Wave greeting gesture |
| `stretch` | Full-body stretch |
| `wallow` | Rolling / wag motion |
| `scrape` | Paw-scrape gesture |
| `front_flip` | Front flip ⚠️ |
| `front_jump` | Forward jump ⚠️ |
| `front_pounce` | Pounce ⚠️ |
| `dance1` | Dance routine 1 |
| `dance2` | Dance routine 2 |
| `finger_heart` | Finger heart pose |

> ⚠️ High-energy modes have a 3-second cooldown enforced by SafetyGate.

---

## 🧩 Plugins

Create a plugin in two files:

**`plugins/my_plugin/plugin.yaml`:**
```yaml
name: MyPlugin
version: 1.0.0
author: Your Name
description: Does something useful
entry_point: plugins.my_plugin.my_plugin:MyPlugin
capabilities:
  - motion
  - perception
trust_level: trusted
enabled: true
```

**`plugins/my_plugin/my_plugin.py`:**
```python
class MyPlugin:
    async def on_load(self, context) -> None:
        # context.bridge  — Go2Bridge
        # context.behavior_engine  — BehaviorEngine
        print("MyPlugin loaded!")

    async def on_unload(self) -> None:
        print("MyPlugin unloaded!")
```

See `plugins/examples/hello_world/` for a complete example.

---

## 🔑 Key Features

### Core Runtime Engine
- Deterministic tick-based loop (10–50 Hz configurable)
- Priority scheduling: safety → control → cognition → animation → UI
- Centralized event/state bus via WebSocket
- Full plugin lifecycle management

### Cognitive Architecture
- Reactive → Deliberative → Reflective behavior layers
- Goal prioritization and attention system
- Working memory and long-term memory models
- Adaptive decision-making based on environment and user

### Perception System *(stub — v4.0)*
- Sensor fusion: camera, LIDAR, IMU
- Semantic understanding of objects, scenes, and humans
- Context-aware decision-making

### Learning & Adaptation *(stub — v4.0)*
- Reinforcement learning for autonomous interactions
- Imitation learning for user-guided behavior
- Preference-based personalization via personality persistence

### Safety & Reliability
- Fault-tolerant architecture, watchdogs, crash isolation
- Hard and soft safety constraints (battery, tilt, velocity)
- Plugin trust levels and audit logging
- Emergency stop always bypasses queue

---

## 🧪 Testing

```bash
# Run full test suite
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=cerberus --cov=backend --cov-report=term-missing

# Run specific module
pytest tests/test_cerberus.py::TestSafetyGate -v
```

---

## 🌐 Future Roadmap

- Multi-agent coordination (swarm behaviors)
- Predictive planning and risk assessment
- Voice/NLU commands (Whisper + LLM)
- Advanced personality evolution over time
- Vision pipeline (YOLO v11 + MediaPipe)
- SLAM navigation with go2_ros2_sdk integration

---

## 🤝 Contributing

1. Fork the repository
2. Create a branch: `git checkout -b feature/awesome-plugin`
3. Commit with descriptive messages
4. Run tests: `pytest tests/ -v`
5. Submit a pull request

See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

---

## 📜 License

MIT License — see [LICENSE](LICENSE)

---

## 🙏 Acknowledgements

Built on the shoulders of the Go2 open-source community:

- [unitreerobotics/unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python) — official Python DDS SDK
- [phospho-app/go2_webrtc_connect](https://github.com/phospho-app/go2_webrtc_connect) — WebRTC driver
- [legion1581/unitree_webrtc_connect](https://github.com/legion1581/unitree_webrtc_connect) — original WebRTC driver
- [Unitree-Go2-Robot/go2_robot](https://github.com/Unitree-Go2-Robot/go2_robot) — ROS2 Go2 integration
- [abizovnuralem/go2_ros2_sdk](https://github.com/abizovnuralem/go2_ros2_sdk) — Go2 ROS2 SDK
- [tfoldi/go2-webrtc](https://github.com/tfoldi/go2-webrtc) — original WebRTC research
