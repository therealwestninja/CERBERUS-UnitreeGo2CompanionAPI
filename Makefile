.PHONY: install dev test test-cov lint docker-build clean help

install:
	pip install -r requirements.txt

dev:
	GO2_SIMULATION=true uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload --log-level debug

sim:
	python main.py --simulation

test:
	GO2_SIMULATION=true pytest tests/ -v --asyncio-mode=auto

test-cov:
	GO2_SIMULATION=true pytest tests/ -v --asyncio-mode=auto \
	  --cov=cerberus --cov=backend --cov=plugins \
	  --cov-report=term-missing --cov-report=html

lint:
	ruff check cerberus/ backend/ plugins/ tests/ main.py

docker-build:
	docker build -t cerberus-go2:3.2.0 .

docker-sim:
	docker run --rm -p 8080:8080 -e GO2_SIMULATION=true cerberus-go2:3.2.0

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage dist build *.egg-info

help:
	@echo "CERBERUS v3.2.0 — Make targets:"
	@echo "  install     Install Python dependencies"
	@echo "  dev         Run dev server with hot-reload (simulation)"
	@echo "  sim         Run full platform in simulation mode"
	@echo "  test        Run test suite (32 tests)"
	@echo "  test-cov    Run tests with coverage report"
	@echo "  docker-build Build Docker image"
	@echo "  docker-sim  Run Docker container in simulation"
	@echo "  clean       Remove build artifacts"
