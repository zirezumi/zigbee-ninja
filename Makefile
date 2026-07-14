VENV := .venv
PY   := $(VENV)/bin/python

venv: ## create venv and install collector in editable mode with dev deps
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e './collector[dev]'

api: ## run the collector on :8686
	$(PY) -m zigbee_ninja

test: ## run the collector test suite
	cd collector && ../$(VENV)/bin/pytest

lint: ## ruff over python sources
	$(VENV)/bin/ruff check collector tools

licenses: ## enforce the dependency license policy (DESIGN.md §16)
	$(PY) tools/license_check.py

web: ## frontend dev server (proxies /api to :8686)
	cd frontend && npm install --no-audit --no-fund && npm run dev

image: ## build the container image
	docker build -f deploy/Dockerfile -t zigbee-ninja:dev .

.PHONY: venv api test lint licenses web image
