# ASIL — Engineering Intelligence Infrastructure

> A persistent, temporal, causal understanding of how a software system evolves, behaves, and fails — exposed to coding agents via MCP.

ASIL is **not** a coding assistant, an autonomous coder, or an "AI OS." Those spaces are crowded. ASIL is the layer underneath them: the engineering knowledge graph that knows *what* changed, *what* broke, *when*, *why*, and *how confident* the answer is.

The hero query that defines v1:

> **"Why did this production incident happen?"**
> → reconstructed timeline, probable root cause with confidence score, evidence list, causal chain, architecture-drift report.

For the long-form positioning + competitive landscape, see [docs/medium-blog-post.md](docs/medium-blog-post.md). For the layperson version, [docs/asil-in-five-minutes.md](docs/asil-in-five-minutes.md).

---

## Table of contents

- [What ASIL does](#what-asil-does)
- [Four defensible pillars](#four-defensible-pillars)
- [Prerequisites](#prerequisites)
- [First-time setup (10 minutes)](#first-time-setup-10-minutes)
- [Daily workflow](#daily-workflow)
- [The dashboard, page by page](#the-dashboard-page-by-page)
- [Every CLI command, explained](#every-cli-command-explained)
- [Wiring ASIL into your AI agent (MCP)](#wiring-asil-into-your-ai-agent-mcp)
- [How memory saves you API calls](#how-memory-saves-you-api-calls)
- [Status](#status)
- [Project layout](#project-layout)
- [For contributors and AI coding agents](#for-contributors-and-ai-coding-agents)

---

## What ASIL does

Point it at any codebase (Python / JS / TS / TSX / Go / Ruby / Java / Rust / C / C++ / PHP / Swift / Kotlin) and ASIL will:

1. **Build a knowledge graph** of files, functions, classes, imports, call edges (Neo4j) + a semantic vector index of function bodies (Qdrant).
2. **Answer natural-language questions** about the code, with file:line citations and a confidence score. Every claim is verified against its cited evidence.
3. **Remember every conclusion** in an episodic store (Postgres) so the next session — yours or anyone else's — recalls the cached answer at ~$0.0001 instead of paying the full ~$0.01 again.
4. **Ingest production incidents** as postmortems → derive `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)` edges across three observable strategies (no LLM hallucination, every edge auditable).
5. **Replay any incident** as a time-ordered timeline + service cascade + state diff (deployments-during, metric-before/after) + ranked causal chain.
6. **Detect architecture drift** between your current dependency structure and a stored baseline — before the PR merges.
7. **Pull live data** from Prometheus / Loki / Kubernetes (metrics, logs, deployments) and external systems (GitHub PRs, Slack messages, Jira / Linear tickets).
8. **Expose all of the above as MCP tools** so Claude Code / Cursor / OpenHands / Aider / Cody can call ASIL programmatically.

---

## Four defensible pillars

1. **Temporal causality** — `(:Deployment)-[:PRECEDED]->(:Incident)`, `(:MetricShift)-[:CORRELATED_WITH]->(:Commit)`. Real edges with real semantics, derived from graph state.
2. **Execution replay** — time-travel debugging across services. Timeline, cascade, state diff.
3. **Confidence-scored reasoning** — every conclusion ships with score + evidence count + retrieval strength + causal strength + derivation list.
4. **Architecture drift detection** — learn expected boundaries; flag undocumented coupling before it ships.

See [PLAN.md](PLAN.md) for the full architecture, roadmap, and rationale.

---

## Prerequisites

- **Docker** (for Neo4j, Qdrant, Postgres, Redis, Prometheus, Loki, Grafana).
- **uv** ([astral.sh/uv](https://docs.astral.sh/uv/)) — Python package manager and workspace runner.
- **Python 3.12+** (uv will install one if you don't have it).
- **pnpm** + **Node.js 20+** (for the dashboard. Skip if you only use the CLI.).
- Optional: **`gh` CLI**, authenticated — gives the GitHub adapter richer metadata than the `git log` fallback.

---

## First-time setup (10 minutes)

```bash
git clone https://github.com/rkstlohchab/ASIL
cd ASIL
make bootstrap                 # uv sync + create .env from template
make up                        # start docker services
make web-install               # one-time: pnpm install for the dashboard
```

What each step does:

| Command | What it does | When to re-run |
|---|---|---|
| `make bootstrap` | Runs `uv sync` (installs all Python deps in `.venv`) and copies `.env.example` → `.env` if missing. | First time + after pulling new deps. |
| `make up` | `docker compose up -d`. Starts Neo4j (`:7474` / `:7687`), Qdrant (`:6333`), Postgres (`:5432`), Redis (`:6379`), Prometheus (`:9090`), Loki (`:3100`), Grafana (`:3000`). | First time + after `make down` or reboot. |
| `make web-install` | `cd apps/web && pnpm install`. Installs Next.js + Tailwind + ReactFlow. | First time + after pulling new web deps. |

Backing-service endpoints:

| Service | URL | Credentials |
|---|---|---|
| Neo4j browser | <http://localhost:7474> | `neo4j` / `asil_dev_password` |
| Qdrant dashboard | <http://localhost:6333/dashboard> | — |
| Postgres | `localhost:5432` | `asil` / `asil_dev_password` / db `asil` |
| Redis | `localhost:6379` | — |
| Prometheus | <http://localhost:9090> | — |
| Grafana | <http://localhost:3000> | `admin` / `asil_dev_password` |
| Loki | <http://localhost:3100> | — |

Verify everything is up:

```bash
uv run asil status             # service health table for all 7 services
```

---

## Daily workflow

You'll want two terminals open whenever you use the dashboard.

```bash
# Terminal A — the API gateway (port 8000)
make api-dev

# Terminal B — the Next.js dashboard (port 3001)
make web-dev

# Terminal C — your normal shell for the CLI commands below
```

Then ingest a codebase:

```bash
uv run asil ingest /path/to/your/project --embed
```

And open <http://localhost:3001>.

---

## The dashboard, page by page

10 pages, all backed by the FastAPI gateway on `:8000`. Sidebar lives on the left.

### 1. `/` — Dashboard

Live counts straight from the graph: how many repos / files / functions / classes / incidents / deployments / memories. Lists every indexed repo with its commit SHA and indexed-at timestamp. Shows the active LLM profile (`tight` / `balanced` / `generous`) so you can confirm you're on the right cost tier.

**Use it for:** at-a-glance "is ASIL working and what does it know about me right now."

### 2. `/ask` — Ask

The most-used page. Type a question, optionally scope it to one repo, click Ask. ASIL runs the full pipeline:

- Hybrid retrieval (vector + graph expansion).
- LLM synthesis on the retrieved evidence.
- Verifier pass — second LLM call that checks every claim against the cited code.
- Confidence object assembled from retrieval strength, evidence count, verifier flags.
- Episodic-memory write (so the next session recalls this for ~$0.0001).

The right-hand pane shows the Confidence card and any memory hits from prior runs.

**Use it for:** "where is X handled?", "how does Y work?", "what are the callers of Z?".

### 3. `/incidents` — Incidents

Lists every Incident node in the runtime namespace, newest first. Severity badge, affected services, detected-at timestamp. Click any row to open the replay.

**Use it for:** browsing your postmortem history.

### 4. `/incidents/[id]` — Incident replay

The hero page. For a single incident, ASIL renders:

- **Causal chain** (top of page): a **ReactFlow** graph of `(:Cause)-[:PRECEDED]->(:Incident)` candidates with confidence colour-coding (green ≥70%, yellow ≥40%, grey below), arrows labelled with the strategy (proximity / lagged-correlation / explicit-reference) and the time delta.
- **Timeline**: every runtime event (Deployment / MetricShift / LogSignature) leading up to and including the incident, in chronological order.
- **Top causes (ranked)**: same data as the ReactFlow graph but listed with derivation strings ("Deployment deploy-8f2c1d4 on auth occurred 7.0min before the incident → confidence 0.379 (half-life 5min)").
- **Service cascade**: which services took collateral damage and in what order.
- **State diff**: deployments-during-the-window + metric before/after deltas.
- **Confidence card**: score + evidence + derivation list for the overall replay conclusion.

**Use it for:** the postmortem reconstruction. This is what an SRE would build by hand; ASIL does it from observable graph state.

### 5. `/causality` — Causality (interactive)

Type any incident ID, hit "Find causes." Surfaces the ranked `(:Cause)-[:PRECEDED]->(:Incident)` candidates with confidence + strategy + derivation + raw cause props. Identical data to the replay page's causal chain, but standalone so you can quickly explore "which strategy ranked this one highest?"

**Use it for:** debugging the causal linker; understanding why a given cause won the ranking.

### 6. `/drift` — Architecture drift

Pick a repo, click "Check drift." Returns `DriftEvent` rows: new dependencies, removed dependencies, boundary violations, severity badges.

**Use it for:** PR review. Before merging, see what new coupling the change introduces relative to the stored baseline.

### 7. `/memory` — Episodic memory

Type a query. ASIL embeds the query, searches the `asil_memories` Qdrant collection, and returns prior conclusions ranked by semantic similarity. Each hit shows the original question, the cached answer, repo key, model used, $ cost, confidence, and timestamp.

**Use it for:** "did I already figure this out?", auditing the institutional knowledge ASIL has built up, debugging weird recall behaviour.

### 8. `/cost` — Cost + savings

Pulled straight from the Postgres ledger (`asil_costs`):

- Total spent over the selected window (7/14/30/60/90 days).
- Daily-spend bars (relative height = relative cost).
- Per-provider split (openai / anthropic / deepseek / ...).
- Per-tier split (reasoning / classify / summarize / verify / embed).
- **Memory savings card** with the math: `fresh_cost - cached_cost` × memory_count = total $ saved, plus the percentage. This is the screenshot you'd put in a blog post.

**Use it for:** budget reviews, blog posts, justifying the persistence layer to your boss.

### 9. `/mcp` — MCP tool catalog

Lists all 12 tools ASIL exposes over MCP. Each tool is expandable to show its JSON schema. Includes a copy-paste Claude Code wiring snippet so you can plug ASIL into Claude Code in 30 seconds.

**Use it for:** wiring ASIL into your AI agent of choice.

### 10. `/health` — Health

Auto-refreshes every 5s. Shows the status of each backing service (Neo4j / Qdrant / Prometheus / Loki) and the active LLM profile.

**Use it for:** confirming `make up` actually brought everything up.

---

## Every CLI command, explained

Run `uv run asil --help` for the canonical list. Sections below walk through each top-level command and what it does in production.

### Setup + health

```bash
uv run asil status                 # health of Neo4j / Qdrant / Postgres / Redis / Prom / Loki
uv run asil llm profile            # show active profile (tight / balanced / generous) and tier -> provider map
uv run asil llm ping --tier reasoning   # smoke-test the configured LLM with a real call
```

### `ingest` — index a codebase

```bash
uv run asil ingest /path/to/repo               # parse + graph only (no embeddings, no LLM cost)
uv run asil ingest /path/to/repo --embed       # also chunk + embed function bodies into Qdrant
uv run asil ingest github.com/org/name --embed # clone from a remote URL, then index
```

What happens:

1. Cloner resolves the spec to a local path (clones if remote).
2. Tree-sitter parses every supported file → `ParsedFile` records with functions / classes / imports / calls.
3. `GraphBuilder` MERGEs nodes + edges into Neo4j (`Repo`, `File`, `Function`, `Class`, `Symbol`, `Commit`, `Author`, plus `:CONTAINS / :IMPORTS / :CALLS / :DEFINED_IN`).
4. Call resolver promotes imports + qualified names into `:CALLS` edges where possible.
5. With `--embed`: AST-aligned chunker writes one Qdrant point per function / class.

Re-running is idempotent — same MERGE keys, no duplicates.

### `ask` — ask the codebase a question

```bash
uv run asil ask "How does authentication work?"
uv run asil ask "How does authentication work?" --repo "local:/abs/path"
uv run asil ask "..." --no-recall                # disable memory lookup for this call
uv run asil ask "..." --no-remember              # don't persist the conclusion afterwards
```

What happens:

1. Embed the question; check episodic memory first. If a strong match exists, return that.
2. Otherwise: hybrid retrieval (vector + graph expand 1-2 hops + re-rank).
3. LLM synthesis on retrieved evidence.
4. Verifier (second LLM pass) — checks every claim against cited snippets.
5. Build `Confidence` from retrieval strength, evidence count, verifier flags.
6. Persist to Postgres + Qdrant for the next session.

### `graph` — Neo4j helpers

```bash
uv run asil graph stats                          # node counts per label
uv run asil graph stats --repo "local:/path"     # scoped to one repo
uv run asil graph neighbors my.module.Class      # 1-hop neighbourhood of a qualified name
uv run asil graph query "MATCH (f:Function) RETURN f LIMIT 5"   # ad-hoc Cypher
```

### `vector` — Qdrant helpers

```bash
uv run asil vector stats                         # collection size, vector dim
uv run asil vector search "router selects model" # top-K semantic matches
```

### `memory` — episodic memory

```bash
uv run asil memory stats                         # per-repo conclusion counts + totals
uv run asil memory list                          # newest conclusions across all repos
uv run asil memory recall --limit 10             # like `list` but for review
uv run asil memory recall --repo "local:/path"   # scoped to one repo
uv run asil memory show <memory_id>              # full record incl. citations + provenance
uv run asil memory forget <memory_id>            # delete one
```

### `eval` — eval harness

```bash
uv run asil eval recall asil_self --repo "local:$(pwd)"    # recall@k for the hand-curated Q&A corpus
```

### `cost` — LLM cost + savings (Phase 7.6)

```bash
uv run asil cost summary                         # total spent + by-provider + by-tier + savings card
uv run asil cost summary --days 7                # change the window
uv run asil cost daily                           # daily-spend bar chart in the terminal
uv run asil cost daily --days 30
```

The savings card estimates `memory_count × (fresh_cost - cached_cost)`. Defaults: $0.01 per fresh ask, $0.0001 per recall.

### `postmortem` — ingest incident YAMLs (Phase 3)

```bash
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil postmortem ingest your-own.yaml      # any YAML matching the runtime-event schema
```

Drops `Service / Deployment / MetricShift / LogSignature / Incident` nodes into the runtime namespace under `env_key=prod` (configurable).

### `events` — list runtime events (Phase 3)

```bash
uv run asil events list --service payments --env prod         # chronological event list
uv run asil events list --service payments --env prod --since "1h ago"
```

### `temporal` — causal engine (Phase 4 — THE MOAT)

```bash
uv run asil temporal link prod                                # derive :PRECEDED edges for every Incident in 'prod'
uv run asil temporal link prod --half-life 600                # change the proximity decay (seconds)
uv run asil temporal causes INC-2026-04-12-payments-cascade   # ranked top causes for one incident
uv run asil temporal causes INC-... --limit 10
```

Three strategies compose: `temporal_proximity` (exponential decay), `lagged_correlation` (deploy touches code the metric's service runs), `explicit_reference` (commit / postmortem text names the incident). Each writes its own `strategy` property on the edge.

### `replay` — full incident reconstruction (Phase 5)

```bash
uv run asil replay INC-2026-04-12-payments-cascade            # Rich terminal report
uv run asil replay INC-2026-04-12-payments-cascade --json     # machine-readable
```

Six panels: header → timeline → ranked causes → service cascade → state diff → confidence.

### `drift` — architecture drift (Phase 6)

```bash
# Snapshot the current dependency structure as a baseline:
uv run asil drift baseline local:$(pwd) --output baseline.json

# ... a week passes, code changes ...

# Compare current graph to the baseline:
uv run asil drift report local:$(pwd) --baseline baseline.json
```

Emits one `DriftEvent` per new dependency / removed dependency / boundary violation.

### `adapters` — live infra (Phase 3 step 3)

```bash
# Prometheus — emit MetricShift when ratio crosses threshold
uv run asil adapters prometheus \
  --probe 'payments:p99_latency:histogram_quantile(0.99, ...)' \
  --probe 'auth:error_rate:sum(rate(http_5xx_total[1m]))' \
  --threshold 1.5 \
  --write

# Loki — cluster recent error logs into LogSignatures
uv run asil adapters loki --service payments --service auth --lookback 300 --write

# Kubernetes — Service + Deployment events from a cluster
uv run asil adapters k8s --kubeconfig ~/.kube/config --namespace prod --write
```

`--write` MERGEs the result straight into Neo4j. Without it, dry-run only.

### `external` — PRs, Slack, Jira, Linear (Phase 7.5)

```bash
# GitHub — works without any token (uses `gh` CLI or `git log` fallback)
uv run asil external github . --write
uv run asil external github . --since-days 90 --limit 100 --write

# Slack — requires SLACK_BOT_TOKEN
uv run asil external slack \
  --channel C-INCIDENTS \
  --service payments --service auth \
  --lookback-hours 48 \
  --write

# Jira — requires JIRA_BASE_URL / JIRA_USER_EMAIL / JIRA_API_TOKEN
uv run asil external jira --project INC --project ENG --write

# Linear — requires LINEAR_API_KEY
uv run asil external linear --team ENG --limit 100 --write
```

Each token-gated adapter raises a clear `NotConfiguredError` until the right env var is set in `.env`.

---

## Wiring ASIL into your AI agent (MCP)

All 12 tools are exposed at:

```text
POST http://localhost:8000/mcp/call/<tool_name>
Body: {"arguments": { ... }}
```

The full tool list (catalog at `GET /mcp/tools`):

| Tool | What it does |
|---|---|
| `asil.search_code` | Hybrid semantic + graph search → ranked functions/classes with citations |
| `asil.get_callers` | Every function calling a given qualified name (1-hop) |
| `asil.get_dependencies` | Inverse — functions that the target calls |
| `asil.who_owns` | Containing file + last-commit author (git blame) |
| `asil.commit_history` | Recent commits touching a file |
| `asil.ask` | Full reasoning pipeline (retrieve → verify → score → answer) |
| `asil.remember` | Explicitly persist a conclusion |
| `asil.recall` | Search past conclusions semantically |
| `asil.forget` | Delete a memory |
| `asil.find_causes` | Ranked causal candidates for an incident |
| `asil.replay_incident` | Timeline + cascade + state diff + confidence |
| `asil.drift_check` | New dependencies + boundary violations vs baseline |

### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "asil": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Restart Claude Code. Tools appear under `mcp__asil__*` automatically.

### Cursor / Aider / OpenHands

Use the same HTTP base URL. Any MCP client can speak to `/mcp/tools` (list) and `/mcp/call/{tool}` (invoke).

---

## How memory saves you API calls

Every conclusion ASIL produces goes into Postgres (`asil_memories`) plus a Qdrant point keyed by the question vector. The next `asil ask` (yours or any agent's) first embeds the new question and searches memory — a hit returns the cached answer instead of re-running the full pipeline.

```text
fresh ask  : ~$0.005 – $0.02   (retrieval + LLM + verifier)
cached ask : ~$0.0001          (one embedding lookup)
saved per cached ask : ~99%
```

Three engineers asking the same question across three sessions pays 3× without memory, 1× with it. On a real codebase with 100+ recurring questions, that's the difference between a $50/month and a $5/month bill.

Track it live:

```bash
uv run asil cost summary
```

Or visit `/cost` in the dashboard for the same numbers as bar charts.

The ledger schema (`asil_costs`) is one row per LLM call: `ts, provider, model, tier, profile, input_tokens, output_tokens, cost_usd`. Aggregations happen in SQL (`asil_core.llm.PostgresCostLedger.aggregates`).

---

## Status

**Phases 0 – 7 ✅ done. Phase 3 step 3 ✅ done. Multi-language ✅ done.** The engine + the dashboard + live infrastructure adapters + external-system adapters are all shipped. Phase 8 (deterministic fix pipeline) is the only remaining stretch item.

See [PLAN.md](PLAN.md#phased-roadmap-solo-12-months) for the per-phase roadmap, [docs/medium-blog-post.md](docs/medium-blog-post.md) for the long-form positioning post, [docs/why-asil.md](docs/why-asil.md) for the reference explainer, [docs/asil-in-five-minutes.md](docs/asil-in-five-minutes.md) for the five-minute version, and [docs/phase-0-testing.md](docs/phase-0-testing.md) / [docs/phase-1-testing.md](docs/phase-1-testing.md) for local validation guides.

---

## Project layout

```text
apps/
  api/        FastAPI gateway + MCP HTTP server + UI REST endpoints
  cli/        Typer CLI — primary UX for Phases 1-6
  worker/     Arq worker for ingestion jobs (deferred polish)
  web/        Next.js 15 + Tailwind + ReactFlow dashboard (port 3001)

packages/
  asil_core/        LLM router, Confidence, config, logging, PostgresCostLedger
  asil_ingest/      Tree-sitter parsers (13 langs), cloner, embedder, graph builder
  asil_memory/      GraphStore (Neo4j) + VectorStore (Qdrant) + EpisodicStore (Postgres)
  asil_reasoning/   Verifier (second-pass) + canonical Scorer
  asil_eval/        Recall harness + Q&A corpus (asil_self)
  asil_infra/       Postmortem ingestor + InfraAdapter (Prom / Loki / K8s / File)
                    + external adapters (GitHub / Slack / Jira / Linear)
  asil_temporal/    Composite causal linker (proximity + lagged + explicit) — THE MOAT
  asil_replay/      Timeline + cascade + state diff (Phase 5)
  asil_drift/       Baseline snapshot + drift detector (Phase 6)

infrastructure/  docker, k8s (later), terraform (later)
research/        papers, design docs, 5 postmortems for eval corpus
scripts/         bootstrap, seed, reset
tests/           unit / integration / e2e
docs/            human-facing guides (testing, why-asil, blog post)
```

---

## For contributors and AI coding agents

- **Starting from any agent (Antigravity, Cursor, OpenHands, Aider, Cody, ...):** read [AGENTS.md](AGENTS.md) — the tool-agnostic entry point.
- **Claude Code specifically:** [CLAUDE.md](CLAUDE.md) is auto-loaded; [.claude/skills/](.claude/skills/) auto-apply (`asil-llm-call`, `asil-confidence`, `asil-positioning`, `asil-phase-gate`, `asil-graph-schema`, `asil-runtime-events`, `asil-temporal-causality`, `asil-memory`, `asil-mcp-tool`, `asil-eval-corpus`); [.claude/commands/](.claude/commands/) expose `/phase` (status), `/eval` (regression harness), and `/check-tier` (scan for hardcoded model names).
- Personal Claude Code overrides go in `.claude/settings.local.json` (gitignored).

Devloop:

```bash
make bootstrap          # uv sync + create .env
make up                 # docker stack
make down               # stop docker stack
make test               # unit tests
make test-integration   # integration tests (requires `make up`)
make lint               # ruff check
make format             # ruff format + ruff check --fix
make typecheck          # mypy across workspace
make reset-dbs          # DESTRUCTIVE — wipes docker volumes

make api-dev            # FastAPI on :8000 (reload)
make web-install        # one-time: pnpm install for the dashboard
make web-dev            # Next.js dashboard on :3001
make web-build          # production build of the dashboard
```

Status check during a Claude Code session: run `/phase` to see what's done and what's next. Run `/eval` to run the regression harness.
