# Go2 Platform — Advanced Robotics Research Summary
# docs/research/ARCHITECTURE_RESEARCH.md

## 1. Autonomous Behavior Systems: Comparative Analysis

### 1.1 Finite State Machine (FSM) — Current Baseline

**Strengths:** Deterministic, easy to debug, predictable transitions, low overhead (~O(1) per tick).
**Weaknesses:** State explosion at scale (N² transitions for N states), brittle to unexpected inputs, poor composability.
**Verdict:** Suitable for *simple*, well-bounded robots. Our current `AuthoritativeFSM` (11 states) sits near the complexity ceiling where FSMs remain manageable.

**When to keep FSMs:** Top-level robot mode (IDLE/WALKING/ESTOP) where determinism matters most.

---

### 1.2 Behavior Trees (BT) — Recommended for Behavior Layer

Behavior Trees address FSM's composability problem with a recursive, hierarchical structure:

```
Root (Selector)
├── Sequence: "Handle Emergency"
│   ├── Condition: is_obstacle_close?
│   └── Action: emergency_stop
├── Sequence: "Execute Mission"
│   ├── Condition: is_armed?
│   ├── Condition: has_mission?
│   └── Action: run_mission
└── Action: idle_breath
```

**Tick semantics:** Each node returns SUCCESS | FAILURE | RUNNING.
**Compositors:** Sequence (AND semantics), Selector (OR semantics), Parallel, Decorator.

**Strengths:**
- O(log N) typical execution, modular composition, testable subtrees
- Reactive to environment changes (re-evaluated every tick)
- Graceful fallback chains via Selector nodes
- Industry standard: Unreal Engine, ROS2 BehaviorTree.CPP

**Weaknesses:** Memory tracking (blackboards needed), harder to reason about global state.

**Our design:** BTs handle the *behavior layer* (what the robot does), while the FSM handles *mode* (can the robot do it?). These compose cleanly.

---

### 1.3 GOAP (Goal-Oriented Action Planning)

GOAP maintains a world-state model and plans action sequences to satisfy goal conditions. Used in F.E.A.R., The Sims.

```python
WorldState = {"at_waypoint": False, "object_targeted": True, "armed": True}
Goal       = {"behavior_performed": True}
Actions    = [ApproachAction, AlignAction, EngageAction, PerformAction]
# Planner finds shortest valid sequence: Approach → Align → Engage → Perform
```

**Strengths:** Emergent, flexible behavior without handcrafted transitions. Excellent for NPC-like companion behavior.
**Weaknesses:** Planning is expensive (A* on state graph), hard to guarantee real-time bounds, state space explosion.
**Verdict:** Suitable for high-level mission planning (5-30s horizon). Not for real-time control loops.

---

### 1.4 Utility AI

Each possible action scored against current world state. Highest score wins.

```python
score(SIT)     = w1*battery_low + w2*human_nearby + w3*been_walking_long
score(FOLLOW)  = w1*human_visible + w2*(1-human_nearby) + w3*command_received
score(ZOOMIES) = w1*energy_high + w2*space_available + w3*excitement_level
```

**Strengths:** Smooth, continuous decision-making; naturally handles "almost equal" cases; easily tunable.
**Weaknesses:** Weight tuning is an art; emergent behaviors hard to predict; no guaranteed task completion.
**Verdict:** Excellent for companion/idle behaviors. Combine with BT for task completion + Utility for behavior selection.

---

### 1.5 Recommended Hybrid Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Strategic Layer (GOAP / Mission System, 10s+ horizon)      │
│  "What should the robot accomplish over the next minutes?"  │
├─────────────────────────────────────────────────────────────┤
│  Tactical Layer (Behavior Tree, 1-10s horizon)              │
│  "Which sequence of actions achieves the current goal?"     │
├─────────────────────────────────────────────────────────────┤
│  Reactive Layer (Utility AI, 100ms horizon)                 │
│  "Which behavior is most appropriate right now?"            │
├─────────────────────────────────────────────────────────────┤
│  Reflex Layer (SafetyEnforcer, <1ms, hard real-time)        │
│  "Is this command safe to execute?" — ALWAYS FINAL AUTH     │
└─────────────────────────────────────────────────────────────┘
```

State persistence: a **Blackboard** (typed key-value store) shared across all layers, providing world state without coupling.

---

## 2. Control Systems: PID vs MPC vs Impedance

### 2.1 PID (Proportional-Integral-Derivative)

```
u(t) = Kp*e(t) + Ki*∫e(t)dt + Kd*de/dt
```

**Current use:** Our `BridgeNode` uses PD impedance (PID without I term) for joint control.
**Tuning:** Ziegler-Nichols or auto-tune methods.
**Limitations:** Single-input/output, no constraint handling, no lookahead.
**Best for:** Individual joint position control, servo loops.

### 2.2 Model Predictive Control (MPC)

MPC solves an optimization problem at each timestep over a receding horizon:

```
minimize    Σ (x_k - x_ref)' Q (x_k - x_ref) + u_k' R u_k
subject to  x_{k+1} = f(x_k, u_k)       # dynamics model
            u_min ≤ u_k ≤ u_max           # actuator limits
            |pitch| ≤ pitch_max            # safety constraints
```

**Strengths:** Constraint-aware, multi-variable, handles actuator limits explicitly, optimal by construction.
**Weaknesses:** Computationally expensive (QP solver every timestep), model accuracy critical.
**Practical:** CasADi + OSQP solve 50Hz MPC in <2ms on Jetson NX for 10-joint, 20-step horizon.
**Best for:** Whole-body control, gait optimization, terrain adaptation.

### 2.3 Impedance Control (Our Current Approach)

```
τ = K_p(q_des - q) - K_d * dq/dt   # Spring-damper at joint level
```

**Physical meaning:** Makes joints behave like springs — compliant to external forces.
**Strengths:** Natural compliance for contact tasks, safe for interaction, simple implementation.
**Weaknesses:** No constraint awareness, no lookahead.
**Verdict:** Correct for our interaction use case. MPC would be added for gait optimization.

### 2.4 Recommended Stack

```
Strategic gait planner (MPC, 50Hz) → Footstep planner → Whole-body controller
  ↓
Joint impedance (PD, 500Hz) → Torque commands → Motors
  ↓
Safety reflex (1000Hz) → Hard stop if limits breached
```

---

## 3. Locomotion: Gait Stability

### 3.1 Gait Patterns

| Gait | Speed | Stability | Footfalls |
|------|-------|-----------|-----------|
| Static walk | Slow | High | Always 3+ feet down |
| Trot | Med | Med | Diagonal pairs |
| Pace | Med | Med | Lateral pairs |
| Bound | Fast | Low | Front/rear pairs |
| Gallop | Fast | Low | Asymmetric |

**Go2 default:** Trot (fast, natural quadruped gait). Our simulation implements trot with diagonal-pair phase locking.

### 3.2 Zero-Moment Point (ZMP)

ZMP is the point where the total ground reaction force effectively acts. Stability condition: ZMP must remain inside the support polygon.

```python
ZMP_x = (Σ m_i * (x_i * g - z_i * ẍ_i)) / (Σ m_i * (g - z̈_i))
```

**Our `in_support_polygon()` test** approximates this with COM projection — valid for quasi-static motion.

### 3.3 Terrain Adaptation

**Flat terrain:** Fixed gait parameters, static foot placement.
**Uneven terrain:** Height mapping from LiDAR, adaptive step height, foothold selection.
**Unknown terrain:** Foot force sensing + compliance to detect and adapt to surface properties.

**Recommended:** Go2 EDU's foot force sensors enable real terrain adaptation. Air/Pro use compliance tuning.

---

## 4. Perception Pipeline

### 4.1 SLAM (Simultaneous Localization and Mapping)

```
Input: LiDAR scans + IMU + odometry
Output: 3D map + robot pose (6-DOF)

Pipeline:
  Raw LiDAR → Point cloud filtering (voxel downsample)
            → Scan matching (ICP or NDT)
            → Loop closure detection (Scan context)
            → Graph optimization (g2o / GTSAM)
            → Dense map (OctoMap or NDT map)
```

**ROS2 packages:** `slam_toolbox` (2D), `cartographer` (2D/3D), `rtab_map` (RGB-D + LiDAR).
**Go2 setup:** L1 LiDAR + IMU → cartographer → nav2 costmap → A* planner.

### 4.2 Object Detection

```
Camera → Preprocessing (resize, normalize)
       → YOLOv8n (edge-optimized, 30fps on Jetson NX)
       → NMS + track (ByteTrack)
       → 3D projection (camera intrinsics + depth)
       → World model update
```

**Classes we care about:** person (safety), chair, cushion, plush, obstacle.
**Confidence thresholds:** person=0.7 (high recall for safety), objects=0.5.

### 4.3 Sensor Fusion

```
IMU (1000Hz)  ─┐
LiDAR (10Hz)  ─┤→ Extended Kalman Filter → Fused pose estimate
Camera (30Hz) ─┘
Odometry      ─┘
```

**EKF state:** [x, y, z, roll, pitch, yaw, vx, vy, vz, ωx, ωy, ωz]
**Prediction:** IMU propagation (1000Hz)
**Update:** LiDAR/camera correction (10-30Hz)

---

## 5. Reactive Systems Design

### 5.1 Event-Driven vs Reactive Streams vs Actor Model

| Model | Latency | Backpressure | Ordering | Best For |
|-------|---------|--------------|----------|----------|
| Event-driven (our EventBus) | Low | None | Loose | Internal platform events |
| Reactive streams (RxPY) | Very low | Natural | Strong | Sensor pipelines |
| Actor model (Erlang/Akka) | Low | Per-actor | Per-actor | Distributed robots |

**Our approach:** EventBus (event-driven) for platform events + asyncio tasks for concurrent pipelines. This is pragmatic for a single-robot system. For fleet, actor-per-robot with message passing scales better.

### 5.2 Real-Time Scheduling

**Hard real-time (safety reflex):** Must complete < 1ms. Implementation: C++ with `SCHED_FIFO`, memory-locked. In Python: 500Hz timer in a dedicated asyncio loop with `asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())`.

**Soft real-time (control loop):** Should complete < 2ms. Implementation: Python + asyncio + uvloop achieves this for our 500Hz loop.

**Best-effort (telemetry, UI):** 5-30Hz push, can be delayed. Standard asyncio is fine.

---

## 6. Human-Robot Interaction (HRI)

### 6.1 Context-Aware Responses

**State machine for proximity:**
```
FAR (>2m)    → idle animations, passive behavior
NEARBY (1-2m) → attention behaviors (head track, alert pose)
CLOSE (<1m)  → interactive mode (reduced speed, compliant joints)
CONTACT zone → BLOCK all motion commands, safety override
```

### 6.2 Multi-Modal Feedback

| Channel | Modality | Latency | Richness |
|---------|----------|---------|---------|
| LED (head) | Visual | <50ms | Low |
| Speaker (built-in) | Audio | <100ms | Med |
| Body pose | Kinesthetic | <200ms | High |
| App notification | Digital | ~1s | Low |
| API event | Programmatic | <10ms | Configurable |

### 6.3 Safety-Aware Interaction Zones

```
Zone 1: Danger   (< 0.25m)  → Hard stop, emergency posture
Zone 2: Caution  (0.25-0.5m) → Velocity cap at 0.3 m/s
Zone 3: Interact (0.5-1.0m)  → Normal but monitored
Zone 4: Nearby   (1.0-2.0m)  → Attention behaviors active
Zone 5: Far      (> 2.0m)   → Normal autonomous operation
```

---

## 7. AI Integration Strategy

### 7.1 Local vs Remote Inference

| Aspect | Local (Jetson) | Remote (Cloud) |
|--------|----------------|----------------|
| Latency | <10ms | 50-500ms |
| Privacy | Full | Data leaves robot |
| Cost | Hardware only | Ongoing API cost |
| Reliability | No connectivity needed | Network dependent |
| Model size | <7B parameters | Unlimited |

**Recommended:** Safety-critical inference (collision, human detection) local only. Creative/generative (behavior generation, conversation) can use remote API with local fallback.

### 7.2 Learning System Constraints

**Never learn from:** unsafe situations, rare failure modes, adversarial inputs.
**Always constrain:** learned policies must pass safety reflex layer — learning cannot override hard limits.
**Safe exploration:** In simulation only. Deploy only after validation.

---

## 8. Memory Safety Strategy

### 8.1 Language Recommendations by Layer

| Layer | Language | Reason |
|-------|----------|--------|
| Safety reflex | **Rust** | Zero-cost abstractions, no GC pauses, ownership prevents data races |
| Control loop | **C++ 20** | Deterministic timing, hardware access |
| Perception pipeline | **Python + C extensions** | ML library ecosystem |
| Platform/API | **Python** (asyncio) | Rapid development, plugin flexibility |
| UI | **JavaScript/TypeScript** | Browser compatibility |

### 8.2 Rust Safety Node Design (Recommended Future)

```rust
// Real-time safety monitor — no heap allocation in hot path
#[repr(C)]
struct SafetyState {
    pitch_limit:  f32,
    roll_limit:   f32,
    force_limit:  f32,
    estop_active: AtomicBool,
    trip_count:   AtomicU32,
}

fn evaluate_safety(state: &SafetyState, telemetry: &Telemetry) -> SafetyDecision {
    // Stack-only, no allocation, no GC, deterministic < 10µs
    if telemetry.pitch.abs() > state.pitch_limit {
        return SafetyDecision::Trip { reason: TripReason::Pitch };
    }
    SafetyDecision::Allow
}
```

**Benefits:** Borrow checker prevents use-after-free, no null pointers, no data races by construction.
**Interop:** Python via PyO3, C++ via `extern "C"` ABI, ROS2 via `r2r` crate.

---

## 9. Tradeoff Summary Table

| Decision | Option A | Option B | Our Choice | Reason |
|----------|----------|----------|------------|--------|
| Behavior system | Pure FSM | Hybrid BT+FSM | Hybrid | FSM for modes, BT for behaviors |
| Control | PID only | PID + MPC | PID now, MPC planned | MPC needs hardware tuning |
| Primary language | All Python | Python + Rust | Python + Rust safety node | Pragmatic now, safe later |
| Inference | Local only | Local + Remote | Both with fallback | Best of both |
| Event system | Polling | Event-driven | Event-driven (asyncio) | Low latency, async-native |
| Simulation | Separate tool | Integrated | Integrated | Developer experience |
| Auth | Always required | Optional | Optional (dev), required (prod) | Friction vs security balance |
