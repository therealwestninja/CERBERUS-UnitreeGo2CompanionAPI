# CERBERUS | Canine-Emulative Responsive Behavioral Engine & Reactive Utility System

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://img.shields.io/badge/CI-CD-blue)](https://github.com/therealwestninja/UnitreeGo2CompanionAPI/actions)

CERBERUS is a **fully autonomous, adaptive, and intelligent quadrupedal robotics platform** that emulates canine behaviors while providing a **modular, extensible, and research-grade utility framework** for developers, researchers, and enthusiasts. It combines **cognitive intelligence, digital anatomy, learning, perception, and a reactive plugin ecosystem** into a single robust system.

---

## 🚀 Key Features

### Core Runtime Engine

* Deterministic tick-based loop (30–200Hz)
* Priority scheduling: safety → control → cognition → animation → UI
* Centralized event/state bus
* Full plugin lifecycle management

### Cognitive Architecture

* Reactive → Deliberative → Reflective behavior layers
* Goal prioritization and attention system
* Working memory and long-term memory models
* Adaptive decision-making based on environment and user

### Digital Anatomy

* Kinematic chain and joint constraints
* COM tracking and balance management
* Energy, fatigue, and stress-aware motion planning
* Integration with motion control and perception systems

### Perception System

* Sensor fusion: camera, LIDAR, IMU
* Semantic understanding of objects, scenes, and humans
* Context-aware decision-making for autonomous operation

### Learning & Adaptation

* Reinforcement learning for autonomous interactions
* Imitation learning for user-guided behavior
* Preference-based personalization
* Continuous adaptation over repeated experiences

### Behavior & Personality

* Behavior tree/hybrid engine with interrupt handling
* Personality traits, mood states, and behavior modulation
* Human-aware interaction shaping

### Plugin Ecosystem

* Sandboxed and capability-based plugins
* Dynamic load/unload with versioning
* Major systems are standalone plugins (Animation, Perception, Simulation, Debug)
* Example plugin repository for developers

### Simulation & Observability

* Real-time simulation for autonomous behavior and animation preview
* Debug overlays: FPS, sensor data, plugin states, active behaviors
* Event timeline, state inspector, and scenario testing
* Translation/localization packs integrated into simulation

### Safety & Reliability

* Fault-tolerant architecture, watchdogs, crash isolation
* Hard and soft safety constraints
* Plugin trust levels and audit logging
* Resource-aware decision-making

### Developer Tools & CI/CD

* Quick-start guides, CLI tools, and plugin templates
* CI/CD workflows: Node.js, Python package, testing, linting, security scanning
* Versioned core + plugins for reproducibility

---

##⚙️ Installation

**Clone the repository:**

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd UnitreeGo2CompanionAPI
```

**Install dependencies (Node.js + Python):**

```bash
npm install
pip install -r requirements.txt
```

**Run simulation & test environment:**

```bash
npm run start-simulation
```

---

## 📖 Usage

CERBERUS exposes a **reactive, event-driven API** to control the robot, monitor sensors, and interact with cognitive and personality systems.

**Example:**

```python
from cerberus import RobotDog

dog = RobotDog()
dog.load_plugin("Perception")
dog.start()

# Move autonomously while reacting to environment
dog.set_goal("explore_area")
dog.on("obstacle_detected", lambda data: dog.stop())
```

Developers can **add plugins dynamically**, create **behavior scripts**, or extend **simulation scenarios**.

---

## 🧩 Plugins

CERBERUS uses a **modular plugin ecosystem**:

* **Animation:** Autonomous and pre-scripted movement
* **Perception:** Sensor fusion, object detection, scene interpretation
* **Simulation:** Preview behaviors and interactions
* **Debug:** Overlays, logging, event timelines

**Create a new plugin:**

```bash
cerberus-cli create-plugin MyPlugin
```

---

## 📈 Contribution Guidelines

We welcome contributions!

1. Fork the repository
2. Create a new branch: `feature/awesome-plugin`
3. Commit changes with descriptive messages
4. Submit a pull request

**Testing & CI/CD:** All contributions are automatically tested via GitHub Actions.

---

## 🌐 Future Roadmap

* Multi-agent coordination (swarm behaviors)
* Predictive planning and risk assessment
* Voice/NLU commands
* Advanced personality evolution over time
* Integration with external AI modules for advanced perception and planning

---

## 🎯 Target Audience

* **Researchers:** Autonomous quadrupeds, robotics, AI behavior modeling
* **Developers:** Plugin and simulation development, system extension
* **Enthusiasts:** Realistic robotic companions, educational and experimental platforms

---

## 📜 License

MIT License – see [LICENSE](LICENSE)
