# CLAUDE.md — ASIL project conventions

This file orients Claude Code (and human contributors) when working in this repo. Read it before changing anything non-trivial.

If you're a non-Claude agent (Antigravity, Cursor, OpenHands, Aider, Cody), start at [AGENTS.md](AGENTS.md) — it points to the same rules in a tool-agnostic way.

The full plan is in [PLAN.md](PLAN.md). This file extracts only the rules that affect day-to-day work.

---

## What ASIL is

**Engineering Intelligence Infrastructure.** A persistent, temporal, causal understanding layer that sits *underneath* coding agents (OpenHands, Cursor, Claude Code, Aider). It is not a coding assistant, AI OS, autonomous coder, or chatbot. See [.claude/skills/asil-positioning.md](.claude/skills/asil-positioning.md).

The v1 hero query: *"Why did this production incident happen?"* → reconstructed timeline, root cause with confidence score, evidence list, causal chain, drift report.

---

## Hard rules

These are violations to surface in review, not preferences:

1. **All LLM calls go through `ModelRouter.call(tier=...)`.** No hardcoded model names. See [.claude/skills/asil-llm-call.md](.claude/skills/asil-llm-call.md).
2. **Every conclusion ships with a `Confidence` object.** Never strip it before returning to the user. See [.claude/skills/asil-confidence.md](.claude/skills/asil-confidence.md).
3. **Deterministic pipelines over multi-agent debate.** LangGraph is for state machines and checkpointing, not for agents arguing. One critique pass max.
4. **No frontend / Next.js work until Phase 7.** CLI (Typer) is the UX. Trying to build a dashboard now is the most common drift failure mode.
5. **Phase gates are real.** Do not start Phase N+1 until Phase N has a demo video + design doc in `research/`. See [.claude/skills/asil-phase-gate.md](.claude/skills/asil-phase-gate.md).
6. **Never read `os.environ` directly.** Go through `asil_core.get_settings()`.
7. **Never `pip install` outside `uv`.** This is a `uv` workspace. Add deps with `uv add` against the right workspace member.

---

## Current phase

**Phase 3 — Infra Bridge** (Phase 0 + 1 + 2 ✅ done; Phase 3 step 1 ✅). See PLAN.md for the full roadmap. Phase 3 step 1 shipped the `asil_infra` package, the runtime-event schema (Service/Deployment/MetricShift/LogSignature/Incident as a parallel namespace under `env_key`), a postmortem YAML loader+ingestor, and the `asil postmortem ingest` / `asil events` CLI commands. Step 2+ is live K8s/Prom/Loki adapters feeding the same schema.

To check status during a session, run `/phase`. To run the regression harness, run `/eval`.

---

## Layout

```
apps/api/     FastAPI gateway + MCP HTTP server (Phase 1: tools live; stdio MCP is Phase 7)
apps/cli/     Typer CLI — primary UX for Phases 1–5
apps/worker/  Arq worker for ingestion jobs (Phase 1.x polish)
apps/web/     Next.js dashboard — DO NOT TOUCH UNTIL PHASE 7

packages/asil_core/        ✅ LLM router, Confidence, config, logging (Phase 0)
packages/asil_ingest/      ✅ Tree-sitter parser, repo cloner, embedder, graph builder, call resolver (Phase 1)
packages/asil_memory/      ✅ GraphStore (Neo4j) + VectorStore (Qdrant) + HybridRetriever (Phase 1); Mem0 episodic (Phase 2)
packages/asil_eval/        ✅ recall harness + Q&A corpus (`asil_self`) (Phase 1)
packages/asil_reasoning/   ✅ verifier + canonical scorer (Phase 2)
packages/asil_infra/       ◐ Phase 3 step 1: runtime-event models + postmortem ingestor.
                              K8s / Prometheus / Loki adapters land next.
packages/asil_temporal/    THE MOAT — causal linker, time-windowed queries (Phase 4)
packages/asil_replay/      incident timeline, cascade, state diff (Phase 5)
packages/asil_drift/       architecture drift detector (Phase 6)

infrastructure/  docker, k8s, terraform
research/        papers, design docs, postmortem corpus
scripts/         bootstrap, seed, reset
tests/           unit / integration / e2e
docs/            human-facing guides (phase-0/1-testing, runbooks)
```

Packages without `pyproject.toml` yet (asil_temporal, asil_replay, asil_drift) get added to `[tool.uv.workspace] members` in the root `pyproject.toml` when their first code lands.

---

## Conventions

### Language & tooling
- Python 3.12+, async-first.
- Type hints required. `mypy --strict` (continue-on-error in CI for Phase 0; tightens in Phase 1).
- `ruff` for lint + format.
- `pytest` with `asyncio_mode = "auto"`.
- Structured logging via `from asil_core import get_logger`.
- Settings via `from asil_core import get_settings`.

### Naming
- Packages: `asil_<area>` (e.g., `asil_temporal`).
- Apps: `apps/<name>/asil_<name>/...` (e.g., `apps/api/asil_api/main.py`).
- Cypher node labels: PascalCase singular (`Service`, `Deployment`).
- Cypher edge types: SCREAMING_SNAKE (`PRECEDED`, `CASCADED_TO`).

### Imports
- Internal: absolute (`from asil_core.llm import ModelRouter`).
- No barrel imports across package boundaries — go through each package's `__init__.py`.

### Tests
- Unit tests **never** hit external services. Use mocks (`MockLLMProvider`, `MockEmbeddingProvider`).
- Integration tests in `tests/integration/` require `make up`.
- E2E in `tests/e2e/` is the full incident-replay pipeline.

### Comments
- Default: don't write them. Names should do the work.
- Exception: when the WHY is non-obvious — a hidden invariant, a workaround for a specific bug, behavior that would surprise a reader.
- Never reference issues / tasks / PR numbers in code. That belongs in PR descriptions.

---

## Devloop cheat-sheet

```bash
make bootstrap                  # one-time: uv sync + .env
make up                         # start docker services
make down                       # stop docker services
make test                       # unit tests
make test-integration           # integration (requires make up)
make lint                       # ruff check
make format                     # ruff format
make typecheck                  # mypy
make reset-dbs                  # DESTRUCTIVE — wipes docker volumes

uv run asil status              # service health table
uv run asil llm profile         # active tier → provider mapping
uv run asil llm ping --tier reasoning
uv run uvicorn asil_api.main:app --reload  # start API on :8000
```

---

## What to do when stuck

- Plan-level questions: see [PLAN.md](PLAN.md).
- Positioning / wording: see [.claude/skills/asil-positioning.md](.claude/skills/asil-positioning.md).
- LLM call patterns: see [.claude/skills/asil-llm-call.md](.claude/skills/asil-llm-call.md).
- Confidence object usage: see [.claude/skills/asil-confidence.md](.claude/skills/asil-confidence.md).
- "Can I start Phase N work yet?": run `/phase` or see [.claude/skills/asil-phase-gate.md](.claude/skills/asil-phase-gate.md).
- Phase 0 local validation: see [docs/phase-0-testing.md](docs/phase-0-testing.md).

---

## When you change things

- Update **PLAN.md** if the change affects phase scope, eval bar, or positioning.
- Update **this file** if the change is a new convention or hard rule.
- Update relevant skill in `.claude/skills/` if the change refines a workflow rule.
- Run `make lint test` before committing.
- Use conventional commit prefixes: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`.

Personal Claude Code overrides go in `.claude/settings.local.json` (gitignored). Don't commit personal hooks or permissions to `.claude/settings.json` — that file is shared.
