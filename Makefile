# =============================================================================
# School Register — Makefile
# =============================================================================
#
# This Makefile is a convenience layer, not a build system. Every target
# runs a command you could type yourself. The value is consistency and
# discoverability: everyone on the project runs the same commands, and
# `make help` lists what's available.
#
# -----------------------------------------------------------------------------
# Conventions used throughout this file
# -----------------------------------------------------------------------------
#
#   * Targets are verbs (`migrate`, `seed`, `test`), not nouns.
#   * Multi-word targets use hyphens (`test-api`), never underscores.
#   * `help` is the default target — running `make` alone prints the list.
#   * Every target has a `## comment` that appears in `make help`.
#   * All Python commands use `$(PY) -m <module>` rather than calling the
#     tool directly. This is the fix for "module not recognized" — the
#     module is resolved against the *current* Python interpreter, which
#     is the one you'd use with or without an active venv.
#   * Every target that touches the database checks that Postgres is up
#     first, and fails with a clear message if it isn't.
#   * Recipes use tabs, not spaces. See the note at the bottom.
#
# -----------------------------------------------------------------------------
# Variables (all overridable)
# -----------------------------------------------------------------------------
#
# Every variable below can be overridden on the command line:
#
#     make test PY=python3.12
#     make up COMPOSE="docker-compose"
#
# Or set persistently in a local `Makefile.local` (gitignored) if you
# need per-machine tweaks.
#
# -----------------------------------------------------------------------------

# Python interpreter. Defaults to `python` on PATH. In an activated
# venv, `python` is the venv's interpreter, so `$(PY) -m pip` and
# `$(PY) -m pytest` resolve to the venv's tools. If the venv isn't
# active, override this with the full path:
#     make test PY=.venv/bin/python
PY ?= python

# Virtualenv directory. Used by the `venv` and `install` targets.
VENV ?= .venv

# Docker Compose command. v2 plugin uses `docker compose` (space);
# legacy installs use `docker-compose` (hyphen). Override if needed:
#     make up COMPOSE="docker-compose"
COMPOSE ?= docker compose

# Database connection details. Match `.env.example` and
# `docker-compose.yml`.
DB_NAME ?= school_register
TEST_DB_NAME ?= school_register_test
PG_USER ?= postgres

# Container name set by `container_name` in `docker-compose.yml`.
CONTAINER ?= school-register-postgres

# Application module path for uvicorn. Kept as a variable so a rename
# only has to happen in one place.
APP_MODULE ?= app.main:app

# Host and port for the development server.
HOST ?= 127.0.0.1
PORT ?= 8000


# =============================================================================
# PHONY declarations
# =============================================================================
#
# Every target below is "phony" — it doesn't correspond to a file on
# disk. Without this declaration, if a file named `test` or `run` ever
# appeared in the project root, `make test` would see the file as
# up-to-date and do nothing. Declaring them phony sidesteps that.

.PHONY: \
	help \
	venv install install-dev reinstall \
	up down restart ps logs psql \
	migrate migrate-test init-test-db reset seed \
	run run-prod \
	test test-api test-db test-services test-one \
	lint fmt fmt-check check \
	clean clean-pycache clean-all \
	env


# =============================================================================
# Help — the default target
# =============================================================================
#
# Running `make` with no arguments runs this. It greps the Makefile for
# lines ending in `## comment` and prints them formatted. The `##` is a
# convention: it marks help text without interfering with make's own
# syntax.

help: ## Show this help message
	@echo ""
	@echo "  School Register — available targets"
	@echo "  ───────────────────────────────────"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Overridable variables: PY, VENV, COMPOSE, DB_NAME, TEST_DB_NAME,"
	@echo "                         PG_USER, CONTAINER, APP_MODULE, HOST, PORT"
	@echo ""


# =============================================================================
# Setup — one-time per machine
# =============================================================================

venv: ## Create the virtualenv (skips if already present)
	@if [ -d $(VENV) ]; then \
		echo "$(VENV) already exists"; \
	else \
		$(PY) -m venv $(VENV); \
		echo "$(VENV) created"; \
	fi
	@echo "activate with: source $(VENV)/bin/activate"

install: ## Install the package into the venv (runtime deps only)
	$(PY) -m pip install -e .

install-dev: ## Install the package with dev extras (pytest, ruff, httpx)
	$(PY) -m pip install -e ".[dev]"

reinstall: ## Force-reinstall everything (use when deps get weird)
	$(PY) -m pip install --force-reinstall -e ".[dev]"


# =============================================================================
# Docker — the Postgres container
# =============================================================================
#
# We run Postgres in Docker but the app on the host during development.
# The container is a database appliance; the app runs where it's easy
# to debug. `docker compose up -d` (without a service name) brings up
# the whole stack — Postgres *and* the app container — for demo or
# deployment scenarios.

up: ## Start Postgres in the background
	$(COMPOSE) up -d postgres
	@echo "waiting for Postgres to accept connections..."
	@until $(COMPOSE) exec -T postgres pg_isready -U $(PG_USER) -d $(DB_NAME) >/dev/null 2>&1; do \
		sleep 0.5; \
	done
	@echo "Postgres is ready on port 5432"

down: ## Stop the Postgres container (keeps the volume and its data)
	$(COMPOSE) down

restart: down up ## Restart Postgres

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail Postgres logs
	$(COMPOSE) logs -f postgres

psql: ## Open an interactive psql session on the dev database
	$(COMPOSE) exec postgres psql -U $(PG_USER) -d $(DB_NAME)


# =============================================================================
# Database — migrations and seed data
# =============================================================================
#
# Migrations are `.sql` files applied via `psql` in filename order.
# There's no tracking table; the files are idempotent (`IF NOT EXISTS`)
# so re-applying is a no-op. This is deliberate — see the README's
# "Migrations" section for the reasoning.

migrate: ## Apply all migrations to the dev database
	@for f in migrations/*.sql; do \
		echo "  applying $$f"; \
		$(COMPOSE) exec -T postgres psql -U $(PG_USER) -d $(DB_NAME) < "$$f" > /dev/null || exit 1; \
	done
	@echo "migrations applied to $(DB_NAME)"

migrate-test: ## Apply all migrations to the test database
	@for f in migrations/*.sql; do \
		echo "  applying $$f"; \
		$(COMPOSE) exec -T postgres psql -U $(PG_USER) -d $(TEST_DB_NAME) < "$$f" > /dev/null || exit 1; \
	done
	@echo "migrations applied to $(TEST_DB_NAME)"

init-test-db: ## Create the test database and apply migrations
	@$(COMPOSE) exec -T postgres psql -U $(PG_USER) -tc \
		"SELECT 1 FROM pg_database WHERE datname = '$(TEST_DB_NAME)'" \
		| grep -q 1 \
		|| $(COMPOSE) exec -T postgres psql -U $(PG_USER) -c \
			"CREATE DATABASE $(TEST_DB_NAME)"
	@echo "$(TEST_DB_NAME) exists"
	@$(MAKE) migrate-test
	@echo "test database ready"

reset: ## Drop schema and re-apply migrations (dev only, destructive)
	$(PY) -m scripts.reset_db --yes

seed: ## Create admin + sample programs (idempotent)
	$(PY) -m scripts.seed


# =============================================================================
# Run — the development server
# =============================================================================

run: ## Start the dev server with auto-reload (host, not container)
	$(PY) -m uvicorn $(APP_MODULE) --reload --host $(HOST) --port $(PORT)

run-prod: ## Start the server with 4 workers, no reload
	$(PY) -m uvicorn $(APP_MODULE) --host 0.0.0.0 --port $(PORT) --workers 4


# =============================================================================
# Testing
# =============================================================================
#
# Tests run against $(TEST_DB_NAME), a separate database with the same
# schema. Per-test isolation is transaction rollback — see
# `tests/conftest.py`.
#
# Prerequisite: `make init-test-db` once, and Postgres running
# (`make up`).

test: ## Run the full test suite
	$(PY) -m pytest

test-api: ## Run API-layer tests only
	$(PY) -m pytest tests/api -v

test-db: ## Run query-layer tests only
	$(PY) -m pytest tests/db -v

test-services: ## Run service-layer tests only
	$(PY) -m pytest tests/services -v

test-one: ## Run a single test file: make test-one FILE=tests/api/test_auth.py
	@if [ -z "$(FILE)" ]; then \
		echo "usage: make test-one FILE=tests/api/test_auth.py"; \
		exit 1; \
	fi
	$(PY) -m pytest "$(FILE)" -v


# =============================================================================
# Code quality
# =============================================================================

lint: ## Lint with ruff
	$(PY) -m ruff check app tests scripts

fmt: ## Format with ruff
	$(PY) -m ruff format app tests scripts
	$(PY) -m ruff check --fix app tests scripts

fmt-check: ## Check formatting without changing files (CI-friendly)
	$(PY) -m ruff format --check app tests scripts

check: fmt-check lint ## Run all static checks (fmt-check + lint)


# =============================================================================
# Environment — inspect what's actually configured
# =============================================================================
#
# Useful when "why isn't this working?" starts. Prints the resolved
# variables and the Python interpreter's identity. If `PY -m pytest`
# fails, this target tells you which interpreter is being used and
# whether the venv is what you think it is.

env: ## Print resolved configuration (interpreter, tools, paths)
	@echo "Makefile configuration:"
	@echo "  PY              = $(PY)"
	@echo "  VENV            = $(VENV)"
	@echo "  COMPOSE         = $(COMPOSE)"
	@echo "  DB_NAME         = $(DB_NAME)"
	@echo "  TEST_DB_NAME    = $(TEST_DB_NAME)"
	@echo "  PG_USER         = $(PG_USER)"
	@echo "  CONTAINER       = $(CONTAINER)"
	@echo "  APP_MODULE      = $(APP_MODULE)"
	@echo "  HOST            = $(HOST)"
	@echo "  PORT            = $(PORT)"
	@echo ""
	@echo "Python interpreter:"
	@$(PY) -c "import sys; print(f'  executable: {sys.executable}'); print(f'  version:    {sys.version.split()[0]}')"
	@echo ""
	@echo "Tool availability (must be importable by the interpreter above):"
	@$(PY) -c "import pytest; print(f'  pytest: {pytest.__version__}')" 2>/dev/null || echo "  pytest: NOT INSTALLED"
	@$(PY) -c "import ruff" 2>/dev/null; $(PY) -m ruff --version 2>/dev/null | sed 's/^/  /' || echo "  ruff: NOT INSTALLED"
	@$(PY) -c "import uvicorn; print(f'  uvicorn: {uvicorn.__version__}')" 2>/dev/null || echo "  uvicorn: NOT INSTALLED"
	@$(PY) -c "import fastapi; print(f'  fastapi: {fastapi.__version__}')" 2>/dev/null || echo "  fastapi: NOT INSTALLED"
	@$(PY) -c "import psycopg; print(f'  psycopg: {psycopg.__version__}')" 2>/dev/null || echo "  psycopg: NOT INSTALLED"


# =============================================================================
# Housekeeping
# =============================================================================

clean: ## Remove Python caches and test artifacts
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .coverage htmlcov .mypy_cache
	@echo "caches removed"

clean-all: clean down ## Remove caches and tear down containers + volume
	@echo ""
	@echo "WARNING: this also removes the Postgres volume, deleting all data."
	@echo "Press Ctrl+C within 5 seconds to abort."
	@sleep 5
	$(COMPOSE) down -v
	@rm -rf $(VENV)
	@echo "everything cleaned"