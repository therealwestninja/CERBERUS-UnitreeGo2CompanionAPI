# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-129%20passing-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Version](https://img.shields.io/badge/version-3.1.1-orange)](Changelog.md)
[![CI](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions/workflows/ci.yml/badge.svg)](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions)

CERBERUS is an autonomous, adaptive, and intelligent companion platform for the Unitree Go2. It understands plain English, executes canine behaviors, enforces safety constraints, records sessions for learning, and streams live telemetry to a web dashboard — and it actually talks to the robot.

> **"walk forward slowly"** → walks · **"do a finger heart"** → finger_heart · **"the robot looks tired"** → stretches · **"emergency stop"** → hard damp

---

## Quick Start

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt

cerberus serve          # start server (simulation mode — no hardware needed)
```

Open **http://localhost:8080/ui/** for the live dashboard, or **http://localhost:8080/docs** for the API.

---

## Connect Real Hardware

**Go2 AIR / PRO / EDU — Wi-Fi (no jailbreak):**
```bash
pip install go2_webrtc_connect
```
`config/cerberus.yaml`:
```yaml
robot:
  transport: webrtc
  webrtc_method: local_sta
  robot_ip: 192.168.8.1    # or use serial_number:
  # serial_number: B42D2000XXXXXXXX
```

**Go2 EDU — Wired Ethernet (CycloneDDS):**
```bash
pip install unitree_sdk2py
```
`config/cerberus.yaml`:
```yaml
robot:
  transport: dds
  network_interface: eth0
```

> Close the Unitree Go app before connecting CERBERUS via WebRTC.

---

## Natural Language Control

No API key needed — the rule engine covers all 17 Go2 modes:

```bash
cerberus nlu "walk forward slowly"           # move(vx=0.2)
cerberus nlu "do a finger heart"             # finger_heart mode
cerberus nlu "the robot looks tired"         # stretch behavior
cerberus nlu "spin left"                     # rotate in place
cerberus nlu "turn obstacle avoidance on"    # enable obstacle avoidance
cerberus nlu "emergency stop"                # hard damp

# or via REST:
curl -X POST http://localhost:8080/api/v1/nlu/command \
  -H "Content-Type: application/json" \
  -d '{"text": "greet the visitors", "execute": true}'
```

**All 17 sport modes reachable via rules:**
`damp` · `balance_stand` · `stop_move` · `stand_up` · `stand_down` · `sit` · `rise_sit` ·
`hello` · `stretch` · `wallow` · `scrape` · `front_flip` · `front_jump` · `front_pounce` ·
`dance1` · `dance2` · `finger_heart`

For LLM-powered interpretation of unusual phrasing, set `CERBERUS_OPENAI_API_KEY` in `.env`.

---

## CLI

```bash
cerberus status                          # robot state snapshot
cerberus move 0.5 0.0 0.0               # walk forward 0.5 m/s
cerberus move 0.0 0.0 0.8               # spin left
cerberus stop                            # stop motion
cerberus mode hello                      # wave greeting
cerberus behavior greet                  # full greeting sequence
cerberus nlu "dance for me"             # NLU command
cerberus sessions                        # list recorded sessions
cerberus replay logs/session.ndjson.gz  # replay at 1× speed
cerberus plugins list                    # loaded plugins
```

---

## Safety

Every command passes through `SafetyGate` — **no bypass except emergency_stop()**:

| Guard | Standard (Air/Pro/EDU) | EDU+ (15000 mAh) |
|-------|----------------------|-----------------|
| Battery warn | < 22.0 V | < 25.0 V |
| Battery block | < 20.5 V | < 23.5 V |
| Tilt block | > 40° | > 40° |
| Velocity limits | vx ±1.5, vy ±0.8, vyaw ±2.0 m/s | same |
| Special motion cooldown | 3 s | 3 s |

```python
from cerberus import SafetyConfig
cfg = SafetyConfig.for_edu_plus()   # 28.8V / 15000mAh variant
```

---

## Python API

```python
from cerberus import Go2Bridge, BehaviorEngine, interpret

bridge = Go2Bridge.from_config({"transport": "mock"})
await bridge.connect()

# Direct hardware control
await bridge.move(0.5, 0.0, 0.0)          # walk forward
await bridge.set_mode("hello")             # wave
await bridge.emergency_stop()              # hard damp

# Behavior engine
engine = BehaviorEngine(bridge)
await engine.start()
await engine.enqueue("greet")              # full greeting sequence

# Natural language
actions = await interpret("spin left and then sit")
for action in actions:
    print(action)   # NLUAction(move, {vyaw: 0.8}, conf=0.92)

# State
state = await bridge.get_state()
print(f"Battery: {state.battery_voltage:.1f}V  Mode: {state.current_mode}")
```

---

## Architecture

```
User text / REST / WebSocket / Dashboard / CLI
             ↓
       NLU Interpreter          rule-based (0ms) → LLM fallback
             ↓
       Cognitive Engine         Reactive (20Hz) │ Deliberate (1Hz) │ Reflective
             ↓
       Behavior Engine          priority-queued async executor
             ↓
    Go2Bridge + SafetyGate      transport-agnostic, safety-enforced
             ↓
   Mock │ DDS │ WebRTC          → Unitree Go2
```

---

## Plugins

```yaml
# plugins/my_plugin/plugin.yaml
name: MyPlugin
version: 1.0.0
entry_point: plugins.my_plugin.my_plugin:MyPlugin
capabilities: [motion, perception]
trust_level: trusted    # core | trusted | community | untrusted
enabled: true
```

```python
class MyPlugin:
    async def on_load(self, context) -> None:
        bridge = context.bridge           # Go2Bridge
        engine = context.behavior_engine  # BehaviorEngine

    async def on_unload(self) -> None: ...
```

See `plugins/examples/hello_world/` for a complete template.

---

## Testing

```bash
make test           # 129 tests
make test-cov       # with HTML coverage report
make lint           # ruff linter
make format         # ruff formatter
```

---

## Docker

```bash
make docker-build
make docker-run

# or single command:
docker compose up --build
```

---

## Installation

```bash
pip install -r requirements.txt         # core

# Optional extras:
pip install go2_webrtc_connect          # Wi-Fi all models
pip install unitree_sdk2py              # EDU wired
pip install ultralytics mediapipe       # vision
pip install PyAudio SpeechRecognition   # audio (Pro/EDU)
```

---

## License

MIT — see [LICENSE](LICENSE)

---

## Acknowledgements

- [unitreerobotics/unitree_sdk2_python](https://github.com/unitreerobotics/unitree_sdk2_python) — DDS SDK
- [phospho-app/go2_webrtc_connect](https://github.com/phospho-app/go2_webrtc_connect) — WebRTC driver
- [Unitree-Go2-Robot/go2_robot](https://github.com/Unitree-Go2-Robot/go2_robot) — ROS2 sport mode list
- [lpigeon/unitree-go2-mcp-server](https://github.com/lpigeon/unitree-go2-mcp-server) — NLU pattern inspiration
- [unitreerobotics/logging-mp](https://github.com/unitreerobotics/logging-mp) — structured logging design
