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
apps/         # FastAPI gateway, Typer CLI, Arq worker, Next.js dashboard (Phase 7)
packages/     # asil_core, asil_ingest, asil_memory, asil_temporal, asil_replay,
              # asil_drift, asil_reasoning, asil_infra, asil_eval
infrastructure/  # docker, k8s (later), terraform (later)
research/     # papers, design decisions, postmortem corpus (5 incidents)
scripts/      # bootstrap, seed, reset
tests/        # unit, integration, e2e
```

## Status

**Phases 0 – 7 ✅ done. Phase 3 step 3 ✅ done. Multi-language ✅ done.** The engine + the dashboard + live infrastructure adapters + external-system adapters are all shipped. Phase 8 (deterministic fix pipeline) is the only remaining stretch item.

ASIL ingests any **Python / JS / TS / TSX / Go / Ruby / Java / Rust / C / C++ / PHP / Swift / Kotlin** repo, builds a queryable knowledge graph + semantic vector index, answers natural-language questions with file:line citations and a confidence score (verified against the citations), persists every conclusion as episodic memory that subsequent sessions recall automatically (so the same question doesn't pay LLM cost twice — backed by a Postgres ledger that survives restarts), ingests postmortems as runtime events (Service / Deployment / MetricShift / LogSignature / Incident), polls **live Prometheus + Loki + Kubernetes** for runtime events, polls **GitHub pull requests + Slack messages + Jira / Linear tickets** for human context, **derives observable causal edges `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)` across three composable strategies — temporal proximity, lagged correlation, explicit reference — with no LLM hallucination, every claim auditable**, replays incidents as a timeline + cascade + state diff, detects architecture drift against a stored baseline, exposes everything through 12 MCP tools that any agent can call, and ships a Next.js dashboard for visual exploration.

Try it:

```bash
make up                                            # docker stack
make api-dev                                       # FastAPI on :8000
make web-install && make web-dev                   # dashboard on :3001 (first time)

uv run asil ingest . --embed                       # parse + graph + embed the current repo
uv run asil ask "How does the LLM router pick a provider for a given tier?"
# ↑ runs verifier; downgrades Confidence on any unsupported claim
uv run asil ask "How does the LLM router pick a provider for a given tier?"
# ↑ second run surfaces the prior conclusion from episodic memory
uv run asil memory stats
uv run asil eval recall asil_self --repo "local:$(pwd)"

# Phase 3: ingest a postmortem and walk the runtime timeline
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil events list --service payments --env prod

# Phase 4 — THE MOAT: derive observable causal edges from the timeline
uv run asil temporal link prod
uv run asil temporal causes INC-2026-04-12-payments-cascade
# ↑ ranked (:Cause)-[:PRECEDED]->(:Incident) with confidence + derivation

# Phase 5: full incident replay
uv run asil replay INC-2026-04-12-payments-cascade

# Phase 6: architecture drift
uv run asil drift baseline local:$(pwd) --output baseline.json
uv run asil drift report   local:$(pwd) --baseline baseline.json
```

Then open the dashboard at <http://localhost:3001> — Dashboard / Ask / Incidents / Causality / Drift / Memory / MCP tools / Health, all wired to the same FastAPI gateway.

See [PLAN.md](PLAN.md#phased-roadmap-solo-12-months) for the full roadmap, [docs/why-asil.md](docs/why-asil.md) for the long-form "what / why / how / what's unique" explainer, [docs/asil-in-five-minutes.md](docs/asil-in-five-minutes.md) for the five-minute layperson version, and [docs/phase-0-testing.md](docs/phase-0-testing.md) / [docs/phase-1-testing.md](docs/phase-1-testing.md) for local validation guides.

## For contributors (and AI coding agents)

- **Starting from any agent (Antigravity, Cursor, OpenHands, Aider, Cody, etc.):** read [AGENTS.md](AGENTS.md) — the tool-agnostic entry point.
- **Claude Code specifically:** [CLAUDE.md](CLAUDE.md) is auto-loaded; [.claude/skills/](.claude/skills/) auto-apply (`asil-llm-call`, `asil-confidence`, `asil-positioning`, `asil-phase-gate`); [.claude/commands/](.claude/commands/) expose `/phase` (status) and `/check-tier` (scan for hardcoded model names).
- Personal Claude Code overrides go in `.claude/settings.local.json` (gitignored).
