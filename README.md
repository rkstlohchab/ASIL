# ASIL — Engineering Intelligence Infrastructure

> A persistent, temporal, causal understanding of how a software system evolves, behaves, and fails — exposed to coding agents via MCP.

ASIL is **not** a coding assistant, an autonomous coder, or an "AI OS." Those spaces are crowded. ASIL is the layer underneath them: the engineering knowledge graph that knows what changed, what broke, when, why, and how confident the answer is.

The hero query that defines v1:

> **"Why did this production incident happen?"**
> → reconstructed timeline, probable root cause with confidence score, evidence list, causal chain, architecture-drift report.

## Four defensible pillars

1. **Temporal causality** — `(:Deployment)-[:PRECEDED]->(:Incident)`, `(:MetricShift)-[:CORRELATED_WITH]->(:Commit)`.
2. **Execution replay** — time-travel debugging across services.
3. **Confidence-scored reasoning** — every conclusion ships with score + evidence + derivation.
4. **Architecture drift detection** — learn expected boundaries; flag undocumented coupling.

See [PLAN.md](PLAN.md) for the full architecture, roadmap, and rationale.

## Quickstart

Prereqs: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12+.

```bash
make bootstrap   # uv sync + create .env from template
make up          # start Neo4j, Qdrant, Postgres, Redis, Loki, Prometheus, Grafana
make status      # confirm services healthy
```

Endpoints after `make up`:

| Service | URL | Creds |
|---|---|---|
| Neo4j browser | http://localhost:7474 | `neo4j` / `asil_dev_password` |
| Qdrant | http://localhost:6333/dashboard | — |
| Postgres | `localhost:5432` | `asil` / `asil_dev_password` / db `asil` |
| Redis | `localhost:6379` | — |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | `admin` / `asil_dev_password` |
| Loki | http://localhost:3100 | — |

## Project layout

```
apps/         # FastAPI gateway, Typer CLI, Arq worker, Next.js dashboard (deferred)
packages/     # asil_core, asil_ingest, asil_memory, asil_temporal, asil_replay,
              # asil_drift, asil_reasoning, asil_infra, asil_eval
infrastructure/  # docker, k8s (later), terraform (later)
research/     # papers, design decisions, postmortem corpus
scripts/      # bootstrap, seed, reset
tests/        # unit, integration, e2e
```

## Status

**Phase 0 + 1 + 2 ✅ done (2026-05-20 → 2026-05-24).** ASIL now ingests any repo, builds a queryable knowledge graph + semantic vector index, answers natural-language questions with file:line citations, verifies each claim against its citations, and persists every conclusion as episodic memory that subsequent runs recall automatically.

Try it:

```bash
make up
uv run asil ingest . --embed             # parse + graph + embed the current repo
uv run asil ask "How does the LLM router pick a provider for a given tier?"
# ↑ runs verifier; downgrades Confidence on any unsupported claim
uv run asil ask "How does the LLM router pick a provider for a given tier?"
# ↑ second run surfaces the prior conclusion from episodic memory
uv run asil memory list
uv run asil eval recall asil_self --repo "local:$(pwd)"
```

Currently at **Phase 3 — Infra Bridge.** K8s / Prometheus / Loki adapters feed runtime events (Deployment, MetricShift, LogSignature) into the graph — the data foundation for Phase 4's temporal causality engine, which is the moat.

See [PLAN.md](PLAN.md#phased-roadmap-solo-12-months) for the full roadmap, [docs/phase-0-testing.md](docs/phase-0-testing.md), and [docs/phase-1-testing.md](docs/phase-1-testing.md).

## For contributors (and AI coding agents)

- **Starting from any agent (Antigravity, Cursor, OpenHands, Aider, Cody, etc.):** read [AGENTS.md](AGENTS.md) — the tool-agnostic entry point.
- **Claude Code specifically:** [CLAUDE.md](CLAUDE.md) is auto-loaded; [.claude/skills/](.claude/skills/) auto-apply (`asil-llm-call`, `asil-confidence`, `asil-positioning`, `asil-phase-gate`); [.claude/commands/](.claude/commands/) expose `/phase` (status) and `/check-tier` (scan for hardcoded model names).
- Personal Claude Code overrides go in `.claude/settings.local.json` (gitignored).
