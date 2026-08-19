.DEFAULT_GOAL := help
SHELL := /bin/bash

# Pinned interpreter: the system python3 on this box is 3.6 and cannot run the stack.
PYTHON ?= /usr/local/bin/python3.11
UV     ?= $(shell command -v uv 2>/dev/null || echo /home/ubuntu/.local/bin/uv)
VENV   := .venv
RUN    := $(UV) run --

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Create the venv and install all dependencies
	$(UV) sync --python $(PYTHON) --all-groups
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

.PHONY: dev
dev: ## Run the API with autoreload on :8000
	$(RUN) uvicorn apps.api.main:app --reload --port 8000

.PHONY: migrate
migrate: ## Apply all database migrations
	$(RUN) alembic upgrade head

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add jobs table"
	$(RUN) alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: ## Load demo fixtures into the local database
	$(RUN) python scripts/seed.py

.PHONY: test
test: ## Run the test suite
	$(RUN) pytest -q

.PHONY: lint
lint: ## Lint and type-check
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) mypy .

.PHONY: format
format: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

.PHONY: web-install
web-install: ## Install frontend dependencies (needs scripts/bootstrap.sh node)
	cd apps/web && PATH="$(CURDIR)/.tooling/bin:$$PATH" npm install --no-audit --no-fund

.PHONY: web-dev
web-dev: ## Vite dev server on :5173, proxying /api to :8000
	cd apps/web && PATH="$(CURDIR)/.tooling/bin:$$PATH" npm run dev

.PHONY: web-build
web-build: ## Build the UI so the API can serve it from :8000
	cd apps/web && PATH="$(CURDIR)/.tooling/bin:$$PATH" npm run build

.PHONY: web-check
web-check: ## Type-check the frontend
	cd apps/web && PATH="$(CURDIR)/.tooling/bin:$$PATH" npm run typecheck

.PHONY: mock-site
mock-site: ## Serve the local mock application site on :8001 (milestone M3)
	$(RUN) uvicorn services.browser.mock_site.app:app --port 8001

# --- Per-milestone test targets -------------------------------------------------
# Each milestone owns a set of test files, so a single step can be checked in
# isolation while reviewing. `make test` still runs everything.

M0_TESTS := tests/unit/test_schemas.py tests/unit/test_llm_client.py \
            tests/unit/test_logging_redaction.py tests/unit/test_audit.py \
            tests/integration/test_health.py tests/integration/test_migrations.py
M1_TESTS := tests/unit/test_latex.py tests/unit/test_extraction.py \
            tests/unit/test_parsing.py tests/unit/test_fact_validation.py \
            tests/integration/test_ingestion.py tests/integration/test_candidates_api.py
M2_TESTS := tests/unit/test_jobs_dedupe.py tests/unit/test_requirements.py \
            tests/unit/test_matching.py tests/integration/test_discovery_matching.py
M3_TESTS := tests/unit/test_patcher.py tests/integration/test_tailoring.py \
            tests/integration/test_mock_site.py
M4_TESTS := tests/integration/test_applications.py
M5_TESTS := tests/unit/test_browser_safety.py tests/integration/test_runner.py

.PHONY: test-m0
test-m0: ## M0 foundation: schemas, LLM seam, audit, logging, health, migrations
	$(RUN) pytest $(M0_TESTS) -v

.PHONY: test-m1
test-m1: ## M1 candidate intelligence: extraction, evidence, fact validation
	$(RUN) pytest $(M1_TESTS) -v

.PHONY: test-m2
test-m2: ## M2 discovery and matching: dedupe, requirements, scoring, injection
	$(RUN) pytest $(M2_TESTS) -v

.PHONY: test-m3
test-m3: ## M3 tailoring: AST, patcher, compile loop, mock site
	$(RUN) pytest $(M3_TESTS) -v

.PHONY: test-m4
test-m4: ## M4 applications: answer mapping, state machine, submit gate
	$(RUN) pytest $(M4_TESTS) -v

.PHONY: test-m5
test-m5: ## M5 browser: safety detection, runner against the mock site
	$(RUN) pytest $(M5_TESTS) -v

.PHONY: smoke-m0
smoke-m0: ## M0 end-to-end: fresh database from migrations, then verify /health
	rm -f data/career_agent.db data/career_agent.db-wal data/career_agent.db-shm
	$(RUN) alembic upgrade head
	$(RUN) python scripts/smoke_m0.py

.PHONY: check
check: lint test ## Everything CI would run

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
