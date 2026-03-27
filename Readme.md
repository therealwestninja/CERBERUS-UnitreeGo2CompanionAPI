# CERBERUS
### Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI/actions)

**CERBERUS** is a real-time orchestration platform for the Unitree Go2 PRO/AIR quadruped robot. It controls the robot, drives peripheral haptic/mechanical devices from its motion, monitors the operator via a wearable bio-sensor, and renders a native operator interface — all through a typed async event bus.

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────────┐
│                         Event Bus                               │
│   Priority 1 (ESTOP / HR_CRITICAL)  →  synchronous dispatch    │
│   Priority 2–8  →  asyncio queue                                │
│   Priority 9    →  UI cosmetic                                  │
└───────┬──────────────────────────────────────┬──────────────────┘
        │                                      │
  ┌─────▼──────┐   ┌──────────┐   ┌───────────▼────────────────┐
  │  Safety    │   │ Runtime  │   │         Plugins             │
  │  Manager  │   │  30Hz    │   │  FunScript  Buttplug        │
  │  (CORE)   │   │  tick    │   │  Hismith    GalaxyFit2      │
  └─────┬──────┘   └────┬─────┘   └───────────┬────────────────┘
        │               │                     │
  ┌─────▼───────────────▼─────────────────────▼────────────────┐
  │                  Go2 WebRTC Adapter                         │
  │          (RTCPeerConnection + data channel)                 │
  └─────────────────────────────────┬──────────────────────────┘
                                    │ WebRTC
                              ┌─────▼─────┐
                              │  Go2 PRO  │
                              │  / AIR    │
                              └───────────┘
```

**Robot is master.** All peripheral plugins subscribe to robot state and FunScript timeline events. They never issue commands back to the robot.

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Unitree Go2 PRO or AIR (connected via Wi-Fi, default IP `192.168.123.1`)
- [Intiface Central](https://intiface.com/central/) running locally (for Buttplug devices)

### 2. Install

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt
```

### 3. Configure

Copy and edit the config:
```bash
cp config/cerberus.yaml config/cerberus.local.yaml
# Edit: robot.ip, device addresses, safety thresholds
```

### 4. Run

```bash
# With hardware + UI
python main.py

# Simulation mode (no robot/BLE needed)
python main.py --simulation

# Headless server
python main.py --no-ui
```

---

## Peripheral Plugins

| Plugin | Hardware | Notes |
|--------|----------|-------|
| **FunScript** | — | Replays `.funscript` timeline files as robot choreography |
| **Buttplug.io** | Any Intiface-compatible device | Requires Intiface Central running locally |
| **Hismith** | Hismith sex machines | BLE auto-scan; speed driven by FunScript position |
| **Samsung Galaxy Fit 2** | Galaxy Fit 2 wearable | HR monitoring; triggers e-stop at critical thresholds |

All plugins are sandboxed with trust levels.  
Only GalaxyFit2 has `CORE` trust (needed for e-stop authority).

---

## Safety System

Three-tier model:

| Condition | Action |
|-----------|--------|
| Battery < 21V | Soft violation warning |
| Battery < 20V | Hard e-stop |
| IMU tilt > 45° | Soft violation → recovery attempt |
| HR > 180 bpm | Interaction pause, operator alert |
| HR > 200 bpm | **Hard e-stop** |
| HR < 40 bpm (active wearable) | **Hard e-stop** |
| Robot telemetry dropout > 3s | **Watchdog e-stop** |

**E-stop requires explicit operator clearance** — no automatic resume.

---

## FunScript

CERBERUS natively replays `.funscript` files. Position (0–100) is mapped to:

- Forward velocity: 0 → 0 m/s, 100 → 0.4 m/s
- Body height offset: centered, ±0.05m
- Lateral sway: proportional to velocity change

The same `FUNSCRIPT_TICK` events drive Buttplug vibration and Hismith speed simultaneously.

---

## Developer Guide

### Plugin API

```python
from cerberus.core.plugin_base import CERBERUSPlugin, PluginManifest, PluginTrustLevel
from cerberus.core.event_bus import Event, EventType

MANIFEST = PluginManifest(
    name="MyPlugin",
    version="1.0.0",
    description="...",
    author="You",
    trust_level=PluginTrustLevel.SANDBOX,
)

class MyPlugin(CERBERUSPlugin):
    def __init__(self):
        super().__init__(MANIFEST)

    async def on_load(self, config):
        self.bus.subscribe(EventType.FUNSCRIPT_TICK, self.on_tick)

    async def on_start(self): ...
    async def on_stop(self):  ...
    async def on_unload(self): ...

    async def on_tick(self, event: Event):
        pos = event.data["position"]   # 0.0 – 1.0
        # do something with pos
```

Register it in `main.py`:
```python
await runtime.load_plugin(MyPlugin(), config={...})
```

### Running Tests

```bash
pytest tests/ -v
```

### REST API

When running, the API is available at `http://localhost:8080`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness check |
| `/state` | GET | Full system state JSON |
| `/command` | POST | Send command (estop, play, pause, etc.) |
| `/ws` | WebSocket | Live state stream at ~30Hz |

---

## Project Structure

```
CERBERUS-UnitreeGo2CompanionAPI/
├── main.py                     Entry point
├── cerberus/
│   ├── core/
│   │   ├── event_bus.py        Central async event bus
│   │   ├── plugin_base.py      Plugin ABC + trust levels
│   │   ├── safety.py           Safety manager + watchdog
│   │   └── runtime.py          30Hz tick loop + plugin registry
│   └── robot/
│       └── go2_webrtc.py       Go2 PRO/AIR WebRTC adapter
├── plugins/
│   ├── buttplug/               Intiface Central integration
│   ├── funscript/              FunScript timeline player
│   ├── galaxy_fit2/            Samsung Galaxy Fit 2 BLE
│   └── hismith/                Hismith BLE machine control
├── ui/
│   ├── cerberus_ui.py          Dear PyGui operator interface
│   └── ui_bridge.py            Thread-safe runtime ↔ UI bridge
├── backend/api/server.py       FastAPI REST/WebSocket server
├── config/cerberus.yaml        All configuration
├── tests/test_core.py          Core test suite
├── Vision_Document.md
├── Changelog.md
└── requirements.txt
```

---

## License

MIT — see [LICENSE](LICENSE)
