# CERBERUS — Vision Document
## Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

**Version:** 2.0  
**Updated:** 2026-03-27  
**Repository:** [CERBERUS-UnitreeGo2CompanionAPI](https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI)

---

## 1. Project Overview

CERBERUS is an **intelligent, autonomous, and extensible quadrupedal robotics platform** for the Unitree Go2 PRO/AIR. It combines a real-time motion system, a reactive plugin ecosystem, peripheral device orchestration, bio-signal safety integration, and an operator-grade interface into a single coherent platform.

**Core pillars:**

| Pillar | Description |
|--------|-------------|
| **Mind** | Event-driven cognitive loop; goal-oriented behavior; learning pipeline |
| **Body** | WebRTC-native Go2 control; kinematic awareness; energy/fatigue modeling |
| **System** | Plugin architecture; safety manager; bio-safety integration; CI/CD |

---

## 2. Vision Statement

A **self-aware, responsive quadruped system** that:

- Demonstrates realistic canine-like behaviors while interacting intelligently with users and environments
- Acts as **master orchestrator** for peripheral devices (haptics, machines, wearables), driving them from robot motion and timeline data
- Learns, adapts, and personalises behavior over time
- Provides **research-grade observability, simulation, and developer tools**
- Operates safely under continuous bio-signal monitoring from a wearable operator device

---

## 3. Architecture

### 3.1 Transport Layer

Target hardware: **Unitree Go2 PRO/AIR** via WebRTC bridge (port 8082).

```
Go2 Robot  ←→  WebRTC Data Channel  ←→  Go2WebRTCAdapter  ←→  SportController
```

- SDP exchange via HTTP POST to robot's signaling endpoint
- JSON sport commands sent over data channel (matches unitree_sdk2 sport API IDs)
- Simulation mode available for development without hardware

> EDU owners using CycloneDDS: replace `Go2WebRTCAdapter` with `unitree_sdk2_python.SportClient`. The event bus contract is identical.

### 3.2 Event Bus

All subsystems communicate exclusively through a typed `EventBus`:

- **Priority-1 events** (ESTOP, HR_CRITICAL) bypass the queue and dispatch synchronously
- **Priority 2–8** go through an `asyncio.Queue` (FIFO within priority)
- **Priority 9** = UI / cosmetic only

No direct cross-subsystem function calls. Every action is an event.

### 3.3 Runtime Tick Loop

30Hz deterministic loop. Priority order per tick:

```
Safety checks → Robot state poll → Cognition/behavior → Plugin ticks → UI push
```

If `ESTOP_TRIGGERED`, the tick loop skips steps 2–4 until cleared.

### 3.4 Plugin Ecosystem

Every peripheral integration is a `CERBERUSPlugin` subclass:

| Plugin | Trust Level | Transport | Drives |
|--------|-------------|-----------|--------|
| FunScript | TRUSTED | local file | robot motion + peripheral events |
| Buttplug | SANDBOX | WebSocket (Intiface) | vibration / linear / rotation devices |
| Hismith | SANDBOX | BLE GATT | stroke machine speed |
| GalaxyFit2 | CORE | BLE GATT | safety system (read-only HR → e-stop) |

**Robot is master.** All peripheral plugins subscribe to `FUNSCRIPT_TICK` and `ROBOT_MOTION_UPDATE` events. They never command the robot.

### 3.5 Safety System

Three-tier safety model:

1. **Hard e-stop** (priority-1 event): immediate motor DAMP, zero all peripheral outputs
2. **Soft violation** (priority-2 event): logged, operator alerted, interaction modulated
3. **Watchdog**: if robot telemetry drops for 3+ seconds → auto e-stop

Bio-signal integration:
| Condition | Response |
|-----------|----------|
| HR > 180 bpm | HEARTRATE_ALARM → pause interaction |
| HR > 200 bpm | ESTOP_TRIGGERED → hard stop |
| HR < 40 bpm (while wearable active) | ESTOP_TRIGGERED → assumed emergency |
| Wearable disconnect | Warning logged, monitoring suspended (no auto-stop) |

### 3.6 UI Layer

**Dear PyGui** running on the main thread.  
The `UIBridge` provides the only legal cross-thread interface:

- `push_state()` — asyncio thread → UIBridge (last-write-wins snapshot)
- `get_state()` → UIBridge → DPG render thread (thread-safe copy)
- `send_command()` → DPG thread → `bus.publish_sync()` → asyncio

The UI **never calls robot code directly**. No `from cerberus.robot import ...` in any UI file.

Design language: **utilitarian / industrial** — dark panels, monospace telemetry, amber/cyan status indicators. Not playful; not tactical.

---

## 4. Peripheral Plugin Specifications

### 4.1 FunScript Player

- Parses `.funscript` JSON (`{version, inverted, range, actions: [{at_ms, pos}]}`)
- Linear interpolation between keyframes at 30Hz
- `pos` (0–100) → `vx` (0–0.4 m/s), body height offset (±0.05m), sway (vy)
- Emits `FUNSCRIPT_TICK` — Buttplug and Hismith subscribe to this

### 4.2 Buttplug.io

- Connects to **Intiface Central** (local WebSocket, default `ws://127.0.0.1:12345`)
- Protocol v4 via `buttplug` PyPI package
- Supports: VIBRATE, ROTATE, POSITION_WITH_DURATION output types
- `FUNSCRIPT_TICK.position` → vibration intensity
- `FUNSCRIPT_TICK.velocity` → rotation speed
- E-stop: `device.stop()` called on all devices immediately

### 4.3 Hismith

- BLE GATT: Service `0x FFF0`, Control char `0xFFF2`
- Speed packet: `[0xFE, speed_byte, 0xFF]` where `speed_byte` = 0–0x64
- Auto-scans for devices advertising "Hismith" or "BM-" prefix
- Configurable max speed cap (default 80%) for safety
- Auto-reconnects on BLE drop

### 4.4 Samsung Galaxy Fit 2

- Standard BLE Heart Rate Service (0x180D) in Accessory Mode
- Samsung proprietary fallback (service 0x6217) for older firmware
- Rolling 3-sample median filter for noise rejection
- Single channel: HR bpm → SafetyManager → estop or alarm events
- Does **not** disconnect on e-stop — continues monitoring so operator can clear

---

## 5. Core Objectives (updated)

1. **WebRTC-native Go2 control** for PRO/AIR with clean EDU upgrade path
2. **Peripheral orchestration**: robot-as-master event model
3. **Bio-signal safety**: wearable-integrated hard and soft limits
4. **FunScript choreography**: timeline playback drives robot + peripherals
5. **Decoupled UI**: Dear PyGui with thread-safe bridge — never rewrites robot code
6. **Sandboxed plugins**: trust levels enforce capability boundaries
7. **Simulation mode**: full functionality without hardware

---

## 6. Future Directions

- Multi-agent coordination (swarm mode, leader-follower)
- Predictive world modeling and risk assessment
- Voice / NLU commands (whisper.cpp onboard)
- Advanced personality evolution over time
- ROS2 bridge for research use (EDU + Orin Nano)
- Additional peripheral plugins: LovenseConnect, TheHandy API
- OTA firmware management
- Record robot motion → export as FunScript

---

## 7. Success Metrics

| Metric | Target |
|--------|--------|
| Runtime stability | < 1% tick overrun at 30Hz over 1h session |
| E-stop latency | < 16ms from trigger to robot DAMP |
| BLE reconnect | Automatic within 10s of device drop |
| HR → e-stop latency | < 100ms from wearable notification to robot halt |
| Plugin isolation | Plugin crash does not affect runtime or other plugins |
| UI frame rate | ≥ 60fps with all panels active |
