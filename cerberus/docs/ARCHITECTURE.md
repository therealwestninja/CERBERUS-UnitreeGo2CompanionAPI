# CERBERUS Architecture Documentation
# cerberus/docs/ARCHITECTURE.md

## Overview

CERBERUS (Canine-Emulative Responsive Behavioral Engine & Reactive Utility System)
is a cognitive-physical-computational stack layered on top of Go2 Platform.

---

## System Layers

```
╔══════════════════════════════════════════════════════════════════════════╗
║  UI LAYER  (ui/index.html — warm companion UI)                           ║
║  API-only consumer. No robot logic. i18n, animation studio, BT viz.     ║
╠══════════════════════════════════════════════════════════════════════════╣
║  APPLICATION LAYER  (Go2 Platform REST/WS API)                           ║
║  FastAPI + CERBERUS route extension                                      ║
║  Auth / rate limiting / schema validation                                ║
╠══════════════════════════════════════════════════════════════════════════╣
║  CERBERUS COGNITIVE LAYER  (10Hz deliberative)                           ║
║  ┌─────────────────┐ ┌──────────────────┐ ┌────────────────────────┐   ║
║  │  CognitiveMind  │ │ PersonalityEngine│ │    LearningSystem      │   ║
║  │  WorkingMemory  │ │  MoodState(AV)   │ │  ReinforcementLearner  │   ║
║  │  EpisodicMemory │ │  PersonalityTraits│ │  ImitationLearner     │   ║
║  │  GoalStack      │ │  BehaviorMod     │ │  PreferenceLearner     │   ║
║  │  AttentionSystem│ └──────────────────┘ └────────────────────────┘   ║
║  └─────────────────┘                                                     ║
╠══════════════════════════════════════════════════════════════════════════╣
║  PERCEPTION LAYER  (~10Hz)                                               ║
║  ┌─────────────────┐ ┌──────────────────┐ ┌────────────────────────┐   ║
║  │  ObjectTracker  │ │  SpatialMapper   │ │   SceneClassifier      │   ║
║  │  (IoU matching) │ │  (log-odds grid) │ │   (heuristic rules)    │   ║
║  └─────────────────┘ └──────────────────┘ └────────────────────────┘   ║
╠══════════════════════════════════════════════════════════════════════════╣
║  BEHAVIOR LAYER  (10Hz BT tick)                                          ║
║  BehaviorTree: Selector → Sequence → Condition → Action                 ║
║  Blackboard ↔ CognitiveMind (goal activation)                           ║
║  PersonalityEngine → BehaviorModulation → speed/expressiveness          ║
╠══════════════════════════════════════════════════════════════════════════╣
║  PLATFORM CORE LAYER  (authoritative state)                              ║
║  AuthoritativeFSM │ SafetyEnforcer │ WorldModel │ MissionSystem          ║
╠══════════════════════════════════════════════════════════════════════════╣
║  DIGITAL ANATOMY LAYER  (500Hz)                                          ║
║  ┌──────────────┐ ┌──────────────────┐ ┌──────────────────────────┐    ║
║  │  JointModel  │ │   EnergyModel    │ │    StabilityModel        │    ║
║  │  12-DOF kine │ │ battery+fatigue  │ │  ZMP + support polygon   │    ║
║  │  thermal+wear│ │ velocity cap     │ │  tip-over prediction     │    ║
║  └──────────────┘ └──────────────────┘ └──────────────────────────┘    ║
╠══════════════════════════════════════════════════════════════════════════╣
║  SAFETY LAYER  (1kHz, NEVER bypassed)                                   ║
║  SafetyEnforcer: pitch/roll/force/battery/temp/obstacle/human           ║
╠══════════════════════════════════════════════════════════════════════════╣
║  CONTROL / SIMULATION LAYER  (500Hz)                                     ║
║  SimEngine (200Hz kinematics) │ ROS2Bridge (Unitree SDK2)                ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## Runtime Tick Scheduling

```
Priority 0 — SAFETY    1000Hz  ← NEVER deferred
Priority 1 — CONTROL    500Hz  ← Joint torque, SimEngine
Priority 2 — COGNITION   10Hz  ← BT, GoalStack, Mind tick
Priority 3 — ANIMATION   50Hz  ← AnimationPlayer, blending
Priority 4 — LEARNING     1Hz  ← Q-table updates, preference save
Priority 5 — TELEMETRY    5Hz  ← WS push, DataLogger flush
```

Each priority runs in its own asyncio task. Safety never yields.
Under CPU pressure, LEARNING and TELEMETRY are starved first; SAFETY never is.

---

## Cognitive Mind — Data Flow

```
PerceptionPipeline
    │ detections, humans, scene
    ▼
CognitiveMind (10Hz tick)
    │
    ├── AttentionSystem.attend(target, salience)
    │       └── most_salient() → "look at" person
    │
    ├── WorkingMemory.store(detection, importance=conf)
    │       └── Capacity-limited (9 items), recency-weighted
    │
    ├── EpisodicMemory.record(event, valence)
    │       └── 5000-episode circular buffer
    │
    ├── GoalStack.push(goal) / complete_active()
    │       └── Priority-ordered, preemption, deadline urgency
    │
    └── EventBus.emit('cognition.active_goal', goal)
              │
              ▼
        BehaviorTreeRunner
              │ action: run_behavior / follow / explore
              ▼
        PlatformCore.execute_command()
              │
              ▼
        SafetyEnforcer → ROS2Bridge → Go2 Hardware
```

---

## Personality Engine — Mood Circumplex

```
     HIGH AROUSAL
          │
  ANXIOUS │ EXCITED ←── joy events, behaviors, social
          │          
──────────┼────────── valence
NEGATIVE  │  POSITIVE
          │
  BORED   │ CONTENT ←── goal completion, rest
          │
     LOW AROUSAL

Current mood decays toward baseline (a=0, v=0.3) over time.
Traits modulate sensitivity:
  high neuroticism → negative events hit harder
  high extraversion → positive events amplify more
```

---

## Learning System — Decision Flow

```
State = (robot_state_bucket, mood_valence_bucket, fatigue_bucket, time_bucket)

1. State observed → Q-table lookup
2. ε-greedy selection: explore (random) or exploit (best Q-value)
3. Action executed (behavior)
4. Reward observed (goal±, safety trip−, user interaction+)
5. Q(s,a) ← Q(s,a) + α[r + γ·max Q(s',·) - Q(s,a)]
6. Preference weight updated: w(behavior) += 0.05 * reward
7. Every 5 min: save to /tmp/cerberus_learning.json

User feedback → PreferenceLearner (stronger signal than autonomous)
User sequences → ImitationLearner (captured and replayed)
Combined suggestion = RL_Q × 0.6 + preference_weight × 0.4
```

---

## Digital Anatomy — Energy & Fatigue Model

```
Battery:
  drain_mah_per_s = current_a / 3600
  current_a = base_current[state] + mechanical_current
  voltage = 19.0 + (battery_pct/100) × 14.4

Fatigue accumulation:
  fatigue += FATIGUE_RATE × power_W × dt_s
  FATIGUE_RATE = 2.5e-6 per Joule (~2% per minute walking at full load)
  
Recovery during rest:
  fatigue -= RECOVERY_RATE × dt_s
  RECOVERY_RATE = 0.0005 per second (~5% per minute at rest)

Velocity cap from fatigue:
  cap = max(0.4, 1.0 - fatigue × 0.6)
  → fresh robot:  1.5 m/s × 1.0 = 1.5 m/s
  → severe fatigue: 1.5 × 0.4 = 0.6 m/s

Motor thermal:
  dT = THERMAL_R × P - COOL_RATE × (T - T_ambient)
  THERMAL_R = 0.15 °C/W, COOL_RATE = 0.08 °C/s per °C above ambient
```

---

## Safety Architecture (Complete)

```
Planner → GoalStack → BehaviorTree → PlatformCore.execute_command()
                                              │
                                     SafetyEnforcer.evaluate()
                                     (every command, every tick)
                                              │
                               ┌─────────────┼─────────────┐
                               │             │             │
                          Reflex Gate   Watchdog     DigitalAnatomy
                          pitch/roll    timeout      tip-over risk
                          force/temp    monitor      energy critical
                          obstacle      1kHz         500Hz
                          human zone    │             │
                               │        └─────────────┘
                               │ if ANY trip:
                               └──→ E-STOP → FSM:ESTOP → motors off
```

---

## Plugin Trust Architecture

```
Plugin manifest → TrustLevel assessment
     │
     ├── SYSTEM     (built-in only)  → all permissions
     │
     ├── TRUSTED    (signed+audited) → + cognitive, personality, learning
     │                                  max_cpu=20ms, max_events=50/s
     │
     ├── COMMUNITY  (unsigned)       → behaviors, ui, api, sensors
     │                                  max_cpu=10ms, max_events=20/s
     │
     └── UNTRUSTED  (sandbox)        → sensors only (read-only telemetry)
                                       max_cpu=2ms, max_events=5/s

API surface per trust level:
  UNTRUSTED:  get_telemetry()
  COMMUNITY:  + register_behavior(), register_ui_panel(), register_route()
  TRUSTED:    + emit_goal(), get_memory_snapshot(), on_mood_change(),
                inject_mood_event(), record_user_preference(), get_body_state()
  SYSTEM:     + all internal platform APIs
```

---

## Data Flow Summary

```
Hardware/Sim → Telemetry → SafetyEnforcer + DigitalAnatomy + PerceptionPipeline
                                │
                         EventBus.emit(*)
                                │
              ┌─────────────────┼─────────────────────────────┐
              │                 │                             │
       CognitiveMind     PersonalityEngine             LearningSystem
       (attention,         (mood update,               (Q-table update,
        goals,              trait modulation)           preference record)
        memory)
              │
        BehaviorTreeRunner (10Hz)
              │
        PlatformCore.execute_command()
              │
        SafetyEnforcer (1kHz gate)
              │
        ROS2Bridge / SimEngine
              │
        Go2 Hardware / Simulation
```

---

## File Structure (CERBERUS)

```
cerberus/
├── __init__.py              Cerberus facade (one-line integration)
├── runtime.py               CerberusRuntime, TickScheduler, SystemEventBus,
│                            WatchdogMonitor, SubsystemRegistry
├── cognitive/
│   ├── mind.py              WorkingMemory, EpisodicMemory, SemanticMemory,
│                            GoalStack, AttentionSystem, CognitiveMind
├── body/
│   ├── anatomy.py           JointModel, EnergyModel, StabilityModel,
│                            DigitalAnatomy
├── personality/
│   ├── engine.py            PersonalityTraits, MoodState, BehaviorModulation,
│                            PersonalityEngine
├── learning/
│   ├── adaptation.py        ReinforcementLearner, ImitationLearner,
│                            PreferenceLearner, LearningSystem
├── perception/
│   ├── pipeline.py          ObjectTracker, SpatialMapper, SceneClassifier,
│                            HumanState, PerceptFrame, PerceptionPipeline
├── plugins/
│   ├── cerberus_plugins.py  TrustLevel, ResourceQuota, CerberusPluginContext,
│                            example plugins (greeter, fatigue, learning)
├── data/
│   ├── logging_pipeline.py  DataLogger, ScenarioReplayer, DatasetExporter
├── api/
│   ├── cerberus_routes.py   35+ REST endpoints for all CERBERUS systems
├── cli/
│   ├── cerberus_cli.py      Full developer CLI with all CERBERUS commands
└── docs/
    └── ARCHITECTURE.md      This document
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Asyncio throughout | Single-threaded concurrency — no lock contention, deterministic tick order |
| Priority-per-subsystem | Safety (1kHz) vs deliberation (10Hz) run at correct rates without coupling |
| EventBus for loose coupling | Subsystems don't import each other — they communicate via events |
| Tabular Q-learning | No PyTorch dependency, interpretable, fast, bounded memory |
| Russell's circumplex for mood | Scientifically grounded, continuous, maps cleanly to behavior parameters |
| ZMP for stability | Hardware-validated stability criterion, no physics engine required |
| Trust-tiered plugins | Defense in depth — community plugins can't crash the cognitive layer |
| JSON episodic memory | No database dependency, disk-serializable, ML-exportable |

---

## Future Extensions

- **SLAM integration**: cartographer → Nav2 → semantic map overlay in UI
- **Voice/NLU**: wake word + STT → GoalStack.push() with natural language goals
- **Multi-agent**: FleetManager + CERBERUS per robot + shared SemanticMemory
- **Neural policy**: Replace tabular Q with small MLP policy (PyTorch, <1MB)
- **Personality evolution**: Long-term trait drift via reinforcement of adaptive behaviors
- **Predictive world model**: MPC-style forward simulation in GoalStack planning
- **Swarm choreography**: Synchronized CERBERUS instances via Bluetooth mesh
