.PHONY: help install dev run test test-live lint fmt openapi docker clean

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	pip install -r requirements.txt

dev:  ## Install runtime + development dependencies
	pip install -r requirements-dev.txt

run:  ## Start the API with auto-reload on http://127.0.0.1:8000
	uvicorn app.main:app --reload --port 8000

test:  ## Run the offline test suite with coverage
	pytest --cov=app --cov-report=term-missing

test-live:  ## Run contract tests against the live portal (needs credentials)
	pytest -m live -v

lint:  ## Check formatting and lint rules
	ruff check app tests scripts
	ruff format --check app tests scripts

fmt:  ## Auto-format the codebase
	ruff format app tests scripts
	ruff check --fix app tests scripts

openapi:  ## Regenerate openapi.json from the code
	python -m scripts.export_openapi

docker:  ## Build the container image
	docker build -t flock-energy-api:latest .

clean:  ## Remove caches and coverage artefacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
