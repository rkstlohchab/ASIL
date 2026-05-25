.PHONY: help bootstrap up down restart logs status sync lint format typecheck test test-unit test-integration test-e2e reset-dbs seed clean web-install web-dev web-build api-dev

SHELL := /bin/bash

help:
	@echo "ASIL — developer targets"
	@echo ""
	@echo "  bootstrap      install uv deps, copy .env.example -> .env if missing"
	@echo "  up             start docker compose services in background"
	@echo "  down           stop docker compose services"
	@echo "  restart        down + up"
	@echo "  logs           tail logs from all services"
	@echo "  status         show docker compose service status"
	@echo "  sync           uv sync (install/lock workspace deps)"
	@echo ""
	@echo "  lint           ruff check"
	@echo "  format         ruff format"
	@echo "  typecheck      mypy across workspace"
	@echo "  test           run all tests"
	@echo "  test-unit      run unit tests only"
	@echo "  test-integration  run integration tests (requires 'make up')"
	@echo "  test-e2e       run end-to-end incident replay tests"
	@echo ""
	@echo "  reset-dbs      tear down + remove volumes (DESTRUCTIVE)"
	@echo "  seed           seed demo repo + demo incident into the graph"
	@echo "  clean          remove caches and build artifacts"
	@echo ""
	@echo "  api-dev        run FastAPI gateway on :8000 (reload)"
	@echo "  web-install    pnpm install inside apps/web"
	@echo "  web-dev        run Next.js dashboard on :3001 (requires api-dev)"
	@echo "  web-build      production build of the dashboard"

bootstrap:
	@command -v uv >/dev/null 2>&1 || { echo "uv not installed. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	@test -f .env || cp .env.example .env
	uv sync

up:
	docker compose up -d
	@echo ""
	@echo "Services starting. Endpoints:"
	@echo "  Neo4j browser   http://localhost:7474   (neo4j / asil_dev_password)"
	@echo "  Qdrant          http://localhost:6333/dashboard"
	@echo "  Postgres        localhost:5432          (asil / asil_dev_password / asil)"
	@echo "  Redis           localhost:6379"
	@echo "  Prometheus      http://localhost:9090"
	@echo "  Grafana         http://localhost:3000   (admin / asil_dev_password)"
	@echo "  Loki            http://localhost:3100"

down:
	docker compose down

restart: down up

logs:
	docker compose logs -f --tail=100

status:
	docker compose ps

sync:
	uv sync

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy packages apps

test: test-unit

test-unit:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v

test-e2e:
	uv run pytest tests/e2e -v

reset-dbs:
	@echo "WARNING: this will delete all data in Neo4j, Qdrant, Postgres, Redis, Loki, Prometheus, Grafana"
	@read -p "Continue? [y/N] " ans && [ "$$ans" = "y" ] || exit 1
	docker compose down -v

seed:
	uv run python scripts/seed_demo_repo.py
	uv run python scripts/seed_demo_incident.py

clean:
	rm -rf .ruff_cache .mypy_cache .pytest_cache **/__pycache__ **/*.egg-info dist build

api-dev:
	uv run uvicorn asil_api.main:app --reload --port 8000

web-install:
	cd apps/web && pnpm install

web-dev:
	cd apps/web && pnpm dev

web-build:
	cd apps/web && pnpm build
