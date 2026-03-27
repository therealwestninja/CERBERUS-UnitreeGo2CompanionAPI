# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://img.shields.io/badge/CI-CD-blue)](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions)
[![Version](https://img.shields.io/badge/version-2.1.0-orange)](Changelog.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

CERBERUS is a **fully autonomous, adaptive, and intelligent quadrupedal robotics platform** for the **Unitree Go2**. It combines a **three-layer cognitive engine**, **digital anatomy model**, **perception pipeline**, **safety watchdog**, and a **sandboxed plugin ecosystem** into a single, research-grade system.

> **Simulation mode** — no robot required. Set `GO2_SIMULATION=true` and everything works.

---

## 🧠 Architecture

```
CERBERUS Engine (asyncio, 30–200Hz)
│
├── Safety Watchdog    ← Heartbeat, tilt, battery, E-stop
├── Behavior Engine    ← 3-layer BT: Reactive/Deliberative/Reflective
├── Digital Anatomy    ← 12-DOF kinematics, COM, fatigue, energy
├── Perception         ← YOLOv8 camera, sensor fusion (plugin)
├── Plugin System      ← Sandboxed, capability-gated, auto-discovered
│
└── Go2 Bridge
    ├── RealBridge     ← CycloneDDS / unitree_sdk2_python (real hardware)
    └── SimBridge      ← Full simulation (no hardware)
```

---

## 🚀 Quick Start

### Simulation (no robot required)

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt

cp .env.example .env
# Edit .env: GO2_SIMULATION=true

cerberus
# → http://localhost:8080
```

### Real Hardware

```bash
# 1. Build CycloneDDS 0.10.x
git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x
cd cyclonedds && mkdir build install && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=../install
cmake --build . --target install

# 2. Install unitree_sdk2_python
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
cd unitree_sdk2_python
export CYCLONEDDS_HOME=~/cyclonedds/install
pip install -e .

# 3. Configure CERBERUS
cp .env.example .env
# Set: GO2_SIMULATION=false
# Set: GO2_NETWORK_INTERFACE=eth0  ← your interface name

# 4. Connect to Go2
# Wire Ethernet, check: ping 192.168.123.161

# 5. Launch
cerberus
```

---

## ⚙️ Configuration (`.env`)

```bash
# Robot
GO2_SIMULATION=true            # true = no hardware needed
GO2_NETWORK_INTERFACE=eth0     # Ethernet interface to Go2 (real mode)

# API
GO2_API_HOST=0.0.0.0
GO2_API_PORT=8080

# Engine
CERBERUS_HZ=60                 # Tick rate (30–200)
HEARTBEAT_TIMEOUT=5.0          # Seconds before auto-stop

# Personality
PERSONALITY_ENERGY=0.7
PERSONALITY_FRIENDLINESS=0.8
PERSONALITY_CURIOSITY=0.6
PERSONALITY_LOYALTY=0.9
PERSONALITY_PLAYFULNESS=0.65

# Plugins
PLUGIN_DIRS=plugins            # Colon-separated list
PLUGIN_MAX_ERRORS=5            # Disable plugin after this many consecutive errors

# Logging
LOG_LEVEL=INFO
CERBERUS_AUDIT_LOG=logs/safety_audit.jsonl
```

---

## 📡 API Reference

### REST Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | System status, engine state |
| `GET` | `/state` | Full robot state snapshot |
| `GET` | `/stats` | Engine performance metrics |
| `GET` | `/anatomy` | Joints, COM, energy, fatigue |
| `GET` | `/behavior` | Cognitive engine status |
| `GET` | `/plugins` | Loaded plugin list |
| `GET` | `/safety/events` | Safety audit log |
| `POST` | `/safety/estop` | Trigger emergency stop |
| `POST` | `/safety/clear_estop` | Clear E-stop (sim only) |
| `POST` | `/motion/stand_up` | |
| `POST` | `/motion/stand_down` | |
| `POST` | `/motion/stop` | Stop all motion |
| `POST` | `/motion/move` | `{"vx", "vy", "vyaw"}` |
| `POST` | `/motion/sport_mode` | `{"mode": "hello"}` — all 17 modes |
| `POST` | `/motion/body_height` | `{"height": 0.05}` — relative offset |
| `POST` | `/motion/euler` | `{"roll", "pitch", "yaw"}` |
| `POST` | `/motion/gait` | `{"gait_id": 0–4}` |
| `POST` | `/motion/foot_raise` | `{"height": 0.0}` |
| `POST` | `/motion/speed_level` | `{"level": -1/0/1}` |
| `POST` | `/motion/continuous_gait` | `{"enabled": true}` |
| `POST` | `/led` | `{"r", "g", "b"}` |
| `POST` | `/volume` | `{"level": 0–100}` |
| `POST` | `/obstacle_avoidance` | `{"enabled": true}` |
| `POST` | `/behavior/goal` | `{"name", "priority", "params"}` |
| `POST` | `/plugins/{name}/enable` | |
| `POST` | `/plugins/{name}/disable` | |
| `DELETE` | `/plugins/{name}` | Unload plugin |

### WebSocket `/ws`

```python
import asyncio, websockets, json

async def main():
    async with websockets.connect("ws://localhost:8080/ws") as ws:
        # Receive state at 30Hz
        async for msg in ws:
            data = json.loads(msg)
            if data["type"] == "state":
                print(data["data"]["battery"])

        # Send commands
        await ws.send(json.dumps({"cmd": "move", "vx": 0.3, "vy": 0, "vyaw": 0}))
        await ws.send(json.dumps({"cmd": "sport_mode", "mode": "hello"}))
        await ws.send(json.dumps({"cmd": "estop"}))
```

### Python SDK Example

```python
import asyncio
from cerberus.bridge.go2_bridge import create_bridge, SportMode
from cerberus.core.engine import CerberusEngine
from cerberus.core.safety import SafetyWatchdog, SafetyLimits
from cerberus.cognitive.behavior_engine import BehaviorEngine

async def main():
    bridge   = create_bridge()     # SimBridge if GO2_SIMULATION=true
    watchdog = SafetyWatchdog(bridge, SafetyLimits())
    engine   = CerberusEngine(bridge, watchdog, target_hz=60)

    engine.behavior_engine = BehaviorEngine(bridge)

    await engine.start()

    # Motion
    await bridge.stand_up()
    await bridge.move(0.3, 0.0, 0.0)
    await asyncio.sleep(3)
    await bridge.stop_move()

    # Sport modes
    await bridge.execute_sport_mode(SportMode.HELLO)
    await bridge.execute_sport_mode(SportMode.DANCE1)

    await engine.stop()

asyncio.run(main())
```

---

## 🔌 Writing a Plugin

```python
# plugins/my_plugin/plugin.py
from cerberus.plugins.plugin_manager import CerberusPlugin, PluginManifest, TrustLevel

MANIFEST = PluginManifest(
    name        = "MyPlugin",
    version     = "1.0.0",
    author      = "You",
    description = "Does something cool",
    capabilities = ["read_state", "publish_events"],
    trust       = TrustLevel.COMMUNITY,
)

class MyPlugin(CerberusPlugin):
    MANIFEST = MANIFEST

    async def on_load(self):
        print("MyPlugin loaded!")

    async def on_tick(self, tick: int):
        if tick % 60 == 0:  # Once per second at 60Hz
            state = await self.get_state()       # Safe — capability checked
            await self.publish("my.event", {"battery": state.battery_percent})

    async def on_unload(self):
        print("MyPlugin unloaded")
```

Plugins are auto-discovered from `PLUGIN_DIRS`. No registration needed.

---

## 🧩 All 17 Sport Modes

```python
from cerberus.bridge.go2_bridge import SportMode

# Via API
requests.post("http://localhost:8080/motion/sport_mode", json={"mode": "hello"})

# Via bridge
await bridge.execute_sport_mode(SportMode.FRONT_FLIP)   # ⚠️ needs open space!
await bridge.execute_sport_mode(SportMode.DANCE1)
await bridge.execute_sport_mode(SportMode.FINGER_HEART)
```

All modes: `damp`, `balance_stand`, `stop_move`, `stand_up`, `stand_down`, `sit`, `rise_sit`, `hello`, `stretch`, `wallow`, `scrape`, `front_flip`, `front_jump`, `front_pounce`, `dance1`, `dance2`, `finger_heart`

---

## 🛡️ Safety

CERBERUS enforces safety at every layer:

- **Watchdog** at 50Hz: heartbeat timeout, tilt detection, battery monitoring
- **Hard E-stop**: cannot be bypassed — triggers motor damp
- **Velocity guardrails**: validated at API level AND bridge level
- **Plugin capability sandbox**: plugins cannot exceed declared permissions
- **Audit log**: every safety event written to `logs/safety_audit.jsonl`

```bash
# Trigger E-stop
curl -X POST http://localhost:8080/safety/estop

# View safety events
curl http://localhost:8080/safety/events
```

---

## 🧪 Testing

```bash
# Run full test suite
pytest tests/ -v

# Run specific test
pytest tests/test_all.py::test_sim_bridge_all_sport_modes -v

# With coverage
pytest tests/ --cov=cerberus --cov=backend --cov-report=html
```

---

## 📈 Contribution Guidelines

1. Fork the repository
2. Create a branch: `feature/my-feature` or `fix/my-fix`
3. Run tests before submitting: `pytest tests/ -v`
4. Lint: `ruff check .`
5. Submit a pull request with description

All PRs are automatically tested via GitHub Actions.

---

## 🌐 Roadmap

- MuJoCo physics simulation integration
- RL training pipeline (IsaacLab/MuJoCo environments)
- Voice/NLU commands (Whisper STT)
- ROS2 bridge (optional)
- Multi-agent coordination
- Mobile companion app

---

## 📜 License

MIT License — see [LICENSE](LICENSE)
