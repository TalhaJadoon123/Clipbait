SHELL := /bin/bash
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
WEB := apps/web
PORT ?= 8000

.DEFAULT_GOAL := help

.PHONY: help install install-all venv web web-dev serve test lint fmt docker-up docker-down clean

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*##"; printf "\nClipbait targets:\n\n"} \
		/^[a-zA-Z_-]+:.*##/ {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install Clipbait in editable mode
	$(BIN)/pip install -e .

install-all: venv ## Editable install with Whisper, vision, live chat and dev tools
	$(BIN)/pip install -e ".[all,dev]"

web: ## Build the SPA into apps/web/dist (committed)
	cd $(WEB) && npm install --no-audit --no-fund && npm run build

web-dev: ## Vite dev server on :5173, proxying the API to :8000
	cd $(WEB) && npm run dev

serve: ## Run the dashboard + API
	$(BIN)/clipbait serve --host 127.0.0.1 --port $(PORT)

test: ## Run the test suite
	$(BIN)/python -m pytest

lint: ## Ruff check
	$(BIN)/ruff check src tests

fmt: ## Ruff autofix + format
	$(BIN)/ruff check --fix src tests
	$(BIN)/ruff format src tests

doctor: ## Report which optional capabilities are available
	$(BIN)/clipbait doctor

docker-up: ## Build and start the container stack
	docker compose up --build -d

docker-down: ## Stop the container stack
	docker compose down

clean: ## Remove caches and local build artefacts (keeps data/ and dist)
	rm -rf .pytest_cache .ruff_cache
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf src/*.egg-info