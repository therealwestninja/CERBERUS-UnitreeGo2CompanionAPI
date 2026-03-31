# go2_platform/Makefile
.PHONY: help install dev test lint format clean run docker-build docker-run

PYTHON := python3
PORT   := 8080

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	pip install -e .

dev: ## Install dev dependencies
	pip install -e ".[dev,vision,ble]"
	cp -n .env.example .env || true

test: ## Run full test suite
	python -m pytest tests/ -v --tb=short

test-fast: ## Run tests without slow integration tests
	python -m pytest tests/ -v -m "not slow" --tb=short

lint: ## Lint with ruff
	python -m ruff check .

format: ## Auto-format with ruff
	python -m ruff format .

run: ## Start the platform server (simulation mode)
	GO2_MODE=simulation uvicorn backend.api.server:create_app \
		--factory --host 0.0.0.0 --port $(PORT) --reload

run-hw: ## Start the platform server (hardware mode)
	GO2_MODE=hardware uvicorn backend.api.server:create_app \
		--factory --host 0.0.0.0 --port $(PORT)

open-ui: ## Open companion UI in browser
	python3 -c "import webbrowser; webbrowser.open('ui/index.html')"

clean: ## Remove Python caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache dist build *.egg-info

docker-build: ## Build Docker image
	docker build -t go2-platform:latest .

docker-run: ## Run in Docker (simulation)
	docker run -p 8080:8080 -e GO2_MODE=simulation go2-platform:latest

ros2-launch: ## Launch full ROS2 system (requires ROS2 + sourced environment)
	ros2 launch ros2_ws/src/go2_control/launch/go2_system.launch.py

ros2-test: ## Run ROS2 package tests
	cd ros2_ws && colcon test --packages-select go2_control && colcon test-result --verbose

docs: ## Generate API docs (requires server running)
	@echo "API docs available at: http://localhost:$(PORT)/docs"
	@echo "ReDoc available at:    http://localhost:$(PORT)/redoc"
