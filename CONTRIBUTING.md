# Contributing to CERBERUS

Thank you for your interest in contributing! CERBERUS is a community-driven project and welcomes contributions of all kinds — bug fixes, new behaviors, plugins, documentation, and more.

---

## Getting Started

```bash
git clone https://github.com/therealwestninja/CERBERUS-UnitreeGo2CompanionAPI.git
cd CERBERUS-UnitreeGo2CompanionAPI
pip install -r requirements.txt
make test   # ensure baseline passes before you start
```

---

## Development Workflow

1. **Fork** the repository
2. **Create a branch** with a descriptive name:
   - `feature/voice-command-plugin`
   - `fix/safety-gate-tilt-threshold`
   - `docs/perception-pipeline-guide`
3. **Make your changes** — see guidelines below
4. **Run tests**: `make test`
5. **Run linter**: `make lint`
6. **Submit a Pull Request** with a clear description

---

## Code Standards

- Python 3.11+, type annotations required on all public functions
- Formatting: `ruff format` (line length 100)
- Linting: `ruff check` — all checks must pass
- Every new module needs at least one test in `tests/test_cerberus.py`
- Async-first: use `asyncio.to_thread()` for blocking SDK calls

---

## Adding a Behavior

```python
# In cerberus/behavior/engine.py or your plugin:
from cerberus.behavior.engine import BehaviorDescriptor, Priority

async def my_behavior(ctx: BehaviorContext) -> None:
    await ctx.bridge.set_mode("hello")
    await asyncio.sleep(2.0)

engine.register(BehaviorDescriptor(
    name="my_behavior",
    fn=my_behavior,
    priority=Priority.NORMAL,
    cooldown_s=5.0,
    description="Does something canine-like",
))
```

---

## Writing a Plugin

1. Create `plugins/my_plugin/plugin.yaml` (see `plugins/examples/hello_world/` for template)
2. Create `plugins/my_plugin/my_plugin.py` with a class exposing `on_load(ctx)` and `on_unload()`
3. Choose the minimum trust level required for your capabilities
4. Add a test that loads the plugin against a mock bridge

---

## Safety Requirements

Any contribution that touches the hardware bridge, safety gate, or motion commands **must**:

- Not add any bypass path around `SafetyGate`
- Not raise the default velocity limits above `max_vx=1.5, max_vy=0.8, max_vyaw=2.0`
- Not remove emergency stop functionality
- Include a test for the safety constraint

---

## Commit Messages

Use conventional commits:
- `feat: add voice command plugin`
- `fix: correct battery threshold for EDU+ variant`
- `docs: add perception pipeline guide`
- `test: add NLU edge case tests`
- `refactor: extract transport base class`

---

## Plugin Contributions

Great plugins to contribute:
- **VoicePlugin** — Whisper STT + TTS using Go2 speaker/mic
- **PersonFollowPlugin** — follow a detected person using perception pipeline
- **SLAMPlugin** — map building via go2_ros2_sdk RTAB-Map integration
- **MetricsPlugin** — Prometheus metrics endpoint
- **HealthCheckPlugin** — extended diagnostics and hardware self-test

---

## Questions?

Open an issue or start a discussion. All contributors are expected to follow the project's code of conduct: be respectful, constructive, and helpful.
