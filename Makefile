# CERBERUS — Makefile  (v3.1.0)
# ════════════════════════════════════════════════════════════════════════
.PHONY: install dev test lint format docker-build docker-run clean help

# ── Variables ────────────────────────────────────────────────────────────
PYTHON   := python3
PIP      := pip3
IMAGE    := cerberus-go2:3.1.0
PORT     := 8080

# ── Install ──────────────────────────────────────────────────────────────
install:
	$(PIP) install -r requirements.txt

install-all:
	$(PIP) install -e ".[all]"

# ── Development server ────────────────────────────────────────────────────
dev:
	CERBERUS_DEV=true uvicorn backend.api.server:app \
	  --host 0.0.0.0 --port $(PORT) --reload --log-level debug

# ── Tests ─────────────────────────────────────────────────────────────────
test:
	pytest tests/ -v --asyncio-mode=auto

test-cov:
	pytest tests/ -v --asyncio-mode=auto \
	  --cov=cerberus --cov=backend \
	  --cov-report=term-missing --cov-report=html

test-fast:
	pytest tests/ -q --asyncio-mode=auto -x

# ── Code quality ──────────────────────────────────────────────────────────
lint:
	ruff check cerberus/ backend/ tests/

format:
	ruff format cerberus/ backend/ tests/

# ── Docker ────────────────────────────────────────────────────────────────
docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p $(PORT):$(PORT) \
	  --env-file .env \
	  -v $(PWD)/logs:/app/logs \
	  -v $(PWD)/config:/app/config:ro \
	  $(IMAGE)

docker-compose-up:
	docker compose up --build

# ── Utilities ─────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage dist build *.egg-info

# ── Help ──────────────────────────────────────────────────────────────────
help:
	@echo "CERBERUS v3.1.0 — Make targets:"
	@echo "  install       Install core Python dependencies"
	@echo "  install-all   Install all optional extras"
	@echo "  dev           Run development server (hot-reload)"
	@echo "  test          Run full test suite"
	@echo "  test-cov      Run tests with coverage report"
	@echo "  lint          Run ruff linter"
	@echo "  format        Run ruff formatter"
	@echo "  docker-build  Build Docker image"
	@echo "  docker-run    Run Docker container"
	@echo "  clean         Remove build/cache artefacts"
