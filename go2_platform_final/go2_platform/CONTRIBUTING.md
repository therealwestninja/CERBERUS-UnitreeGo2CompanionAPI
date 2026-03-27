# Contributing to Go2 Platform

Thank you for your interest in contributing! This guide covers how to set up your development environment, submit changes, and understand the project conventions.

---

## Development Setup

```bash
# 1. Fork and clone
git clone https://github.com/your-org/go2-platform.git
cd go2-platform

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dev dependencies
make dev
# Equivalent: pip install -e ".[dev,vision,ble]" && cp .env.example .env

# 4. Verify everything passes
make test
```

---

## Project Structure

```
go2_platform/
├── backend/core/     — Platform logic (platform.py, security.py, plugin_system.py)
├── backend/api/      — FastAPI server (server.py)
├── backend/sim/      — Simulation engine
├── backend/ros2_bridge/ — ROS2 bridge node
├── plugins/          — Plugin directory (auto-discovered)
├── ui/               — Single-file companion UI (index.html)
├── tests/            — Test suite
└── config/           — Configuration files
```

---

## Contribution Areas

### Bug Fixes
- Check open issues on GitHub
- Add a failing test that demonstrates the bug first
- Fix the bug, verify the test passes
- Submit a PR referencing the issue

### New Features
- Open an issue to discuss before implementing large features
- Keep the layered architecture intact: UI → API → Platform → ROS2 → Hardware
- Safety enforcement must remain in `SafetyEnforcer`, not the API or UI layer

### Plugins
Plugins are the preferred way to add new behaviors. See `plugins/examples/` for reference.
```python
# Minimal plugin: manifest.json + plugin.py
async def init(ctx):
    ctx.register_behavior({'id':'my_trick', 'name':'My Trick',
                           'category':'custom', 'icon':'🎭', 'duration_s':2.0})
```

### UI Improvements
- The UI (`ui/index.html`) is a single self-contained file for portability
- Follows the warm/friendly companion aesthetic — not tactical/industrial
- All robot logic must use `apiCmd()` — never add direct robot state to JS

---

## Code Standards

### Python
- Style: `ruff` (configured in `pyproject.toml`)
- Type hints on all public functions
- Docstrings on all public classes and non-trivial methods
- `asyncio` for I/O — no blocking calls in async context
- Never log secrets (`api_key`, `token`, `password`)

### Tests
- Test file: `tests/test_platform.py`
- New features must include tests
- Run: `make test` or `python tests/test_platform.py`
- Target: 100% of safety-critical paths covered

### Commit Messages
```
feat: add WiFi geofencing to ConnectivityNode
fix: correct hmac digest computation in AuditLog
test: add 5 tests for rate limiter edge cases
docs: update README quick start section
refactor: extract WorldModel export to separate method
```

---

## Pull Request Checklist

- [ ] Tests pass (`make test`)
- [ ] Lint passes (`make lint`)
- [ ] No secrets in code or commits
- [ ] Safety layer not bypassed (changes to `SafetyEnforcer` reviewed carefully)
- [ ] New API endpoints have auth guard (`Depends(require_auth)`)
- [ ] Changelog entry added (under `## Unreleased`)

---

## Safety-Critical Changes

Any changes to the following require extra review:
- `backend/core/platform.py` — FSM transition table
- `backend/core/security.py` — Command allowlist, sanitizer
- `SafetyEnforcer.evaluate()` — Reflex layer

---

## License

By contributing, you agree your contributions will be licensed under the MIT License.
