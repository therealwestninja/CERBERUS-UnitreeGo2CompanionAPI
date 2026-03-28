# CERBERUS Architecture Reference

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          CERBERUS v3.1                                   │
│                                                                           │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Server (port 8080)                      │   │
│  │  REST  /api/v1/*  │  WebSocket /ws/telemetry  │  UI /ui/          │   │
│  └──────────────┬───────────────────────────────────────────────────┘   │
│                 │                                                         │
│  ┌──────────────▼────────────────────────────────────────────────────┐  │
│  │                       NLU Interpreter                              │  │
│  │  Rule engine (regex, 0ms) → LLM fallback (OpenAI-compatible)      │  │
│  └──────────────┬────────────────────────────────────────────────────┘  │
│                 │                                                         │
│  ┌──────────────▼────────────────────────────────────────────────────┐  │
│  │                    Cognitive Engine                                │  │
│  │                                                                    │  │
│  │  Layer 3 – Reflective   PersonalityModel                          │  │
│  │            ↓            traits (sociability, playfulness…)        │  │
│  │            ↓            mood (valence, arousal) with decay        │  │
│  │                                                                    │  │
│  │  Layer 2 – Deliberative CognitiveEngine (1 Hz)                    │  │
│  │            ↓            Goals: IDLE / EXPLORE / GREET / PATROL    │  │
│  │            ↓            Working memory, idle timer, interaction   │  │
│  │                                                                    │  │
│  │  Layer 1 – Reactive     Safety monitor (20 Hz)                    │  │
│  │            ↓            Battery-critical → emergency_sit          │  │
│  │            ↓            Tilt-alert → idle                         │  │
│  └──────────────┬────────────────────────────────────────────────────┘  │
│                 │                                                         │
│  ┌──────────────▼────────────────────────────────────────────────────┐  │
│  │                    Behavior Engine                                 │  │
│  │  Priority queue  (asyncio.PriorityQueue)                          │  │
│  │  Tick rate: configurable (default 10 Hz)                          │  │
│  │                                                                    │  │
│  │  Built-in behaviors:                                               │  │
│  │  idle · sit · stand · greet · stretch · dance                     │  │
│  │  patrol · wag · alert · emergency_sit                             │  │
│  │  + Plugin-registered behaviors                                    │  │
│  └──────────────┬────────────────────────────────────────────────────┘  │
│                 │                                                         │
│  ┌──────────────▼────────────────────────────────────────────────────┐  │
│  │                    Safety Gate                                     │  │
│  │  battery_warn_v, battery_critical_v                               │  │
│  │  tilt_warn_rad, tilt_block_rad                                    │  │
│  │  max_vx/vy/vyaw, special_motion_cooldown                         │  │
│  │  BYPASS: emergency_stop() only                                    │  │
│  └──────────────┬────────────────────────────────────────────────────┘  │
│                 │                                                         │
│  ┌──────────────▼────────────────────────────────────────────────────┐  │
│  │                    Go2Bridge                                       │  │
│  │  Transport-agnostic. from_config() selects:                       │  │
│  │                                                                    │  │
│  │  ┌─────────────┐  ┌──────────────────┐  ┌────────────────────┐  │  │
│  │  │ _MockTransp. │  │  _DDSTransport   │  │ _WebRTCTransport   │  │  │
│  │  │ In-memory    │  │  unitree_sdk2py  │  │ go2_webrtc_connect │  │  │
│  │  │ CI / dev     │  │  Go2 EDU wired   │  │ AIR/PRO/EDU Wi-Fi  │  │  │
│  │  └─────────────┘  └──────────────────┘  └────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                           │
│  Side systems:                                                            │
│  ┌────────────────┐  ┌───────────────────┐  ┌────────────────────────┐ │
│  │ DataLogger     │  │ PerceptionPipeline│  │ PluginManager          │ │
│  │ NDJSON+gzip    │  │ YOLO / MediaPipe  │  │ 4-tier trust model     │ │
│  │ Replay system  │  │ LIDAR pointcloud  │  │ Auto-discovery         │ │
│  └────────────────┘  └───────────────────┘  └────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Module Reference

| Module | Path | Responsibility |
|--------|------|----------------|
| Bridge | `cerberus/hardware/bridge.py` | Hardware abstraction, transport selection |
| Safety | `cerberus/safety/gate.py` | All motion constraint enforcement |
| Behavior | `cerberus/behavior/engine.py` | Priority-queued async behavior executor |
| Cognitive | `cerberus/core/cognitive.py` | 3-layer goal/decision engine |
| Personality | `cerberus/personality/model.py` | Trait + mood model with persistence |
| NLU | `cerberus/nlu/interpreter.py` | Rule + LLM natural language parsing |
| Logger | `cerberus/learning/data_logger.py` | Session recording and replay |
| Perception | `cerberus/perception/pipeline.py` | Camera/LIDAR processing (v4 stub) |
| Simulation | `cerberus/simulation/simulator.py` | Mock sensor/physics for development |
| Plugins | `cerberus/plugins/manager.py` | Plugin lifecycle + trust enforcement |
| Server | `backend/api/server.py` | FastAPI REST + WebSocket server |
| CLI | `cerberus/cli.py` | Command-line interface |

---

## Transport Selection

```yaml
# config/cerberus.yaml
robot:
  transport: mock    # no hardware
  transport: dds     # pip install unitree_sdk2py   (Go2 EDU wired)
  transport: webrtc  # pip install go2_webrtc_connect  (all models, Wi-Fi)
```

---

## Plugin Trust Levels

| Level | Capabilities | Use for |
|-------|-------------|---------|
| `core` | motion + perception + vui + config + admin | Internal core modules |
| `trusted` | motion + perception + vui | Vetted community plugins |
| `community` | perception (read-only) | Analytics, monitoring plugins |
| `untrusted` | none | UI widgets, notification plugins |

---

## WebSocket Protocol

**Server → client** (10 Hz push):
```json
{
  "timestamp": 1711540800.0,
  "connection": "connected",
  "position": {"x": 0.0, "y": 0.0},
  "orientation": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
  "velocity": {"vx": 0.0, "vy": 0.0, "vyaw": 0.0},
  "body_height": 0.38,
  "battery": {"voltage": 25.1, "percent": 85.0},
  "foot_force": [12.1, 11.8, 13.2, 12.5],
  "current_mode": "balance_stand",
  "current_behavior": null,
  "personality": {
    "traits": {"sociability": 0.7, ...},
    "mood": {"valence": 0.32, "arousal": 0.41},
    "mood_label": "calm"
  }
}
```

**Client → server** (inbound commands):
```json
{"action": "move", "vx": 0.5, "vy": 0.0, "vyaw": 0.0}
{"action": "stop"}
{"action": "emergency_stop"}
{"action": "mode", "mode": "hello"}
{"action": "behavior", "behavior": "greet", "params": {}}
{"action": "nlu", "text": "walk forward slowly"}
```

---

## Battery Reference

| Model | Nominal | Warn | Block |
|-------|---------|------|-------|
| Standard Air/Pro/EDU (8000 mAh) | ~25.2 V | 22.0 V | 20.5 V |
| EDU+ extended (15000 mAh) | ~28.8 V | 25.0 V | 23.5 V |

Use `SafetyConfig.for_edu_plus()` for the extended battery variant.
