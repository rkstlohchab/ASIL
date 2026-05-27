# ASIL — Engineering Intelligence Infrastructure

> A persistent, temporal, causal understanding of how a software system evolves, behaves, and fails — exposed to coding agents via MCP.

ASIL is **not** a coding assistant, an autonomous coder, or an "AI OS." Those spaces are crowded. ASIL is the layer underneath them: the engineering knowledge graph that knows *what* changed, *what* broke, *when*, *why*, and *how confident* the answer is.

The hero query that defines v1:

> **"Why did this production incident happen?"**
> → reconstructed timeline, probable root cause with confidence score, evidence list, causal chain, architecture-drift report.

![ASIL demo — 90-second tour](docs/assets/asil-demo.gif)

*90-second tour: ingest → ask (fresh + cached) → cost summary → incident replay → constrained fix → CI scan. Regenerate locally with `make demo-auto` piped through asciinema — see [How to record this demo](#how-to-record-this-demo) below.*

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
- [Cross-IDE memory handoff (the two-command flow)](#cross-ide-memory-handoff-the-two-command-flow)
- [Multi-team setup](#multi-team-setup)
- [Deleting memories](#deleting-memories)
- [Wiring ASIL into your AI agent (MCP)](#wiring-asil-into-your-ai-agent-mcp)
- [How memory saves you API calls](#how-memory-saves-you-api-calls)
- [Inspecting the data inside Docker](#inspecting-the-data-inside-docker)
- [Measuring real savings on your codebase](#measuring-real-savings-on-your-codebase)
- [Status](#status)
- [Project layout](#project-layout)
- [For contributors and AI coding agents](#for-contributors-and-ai-coding-agents)
- [How to record this demo](#how-to-record-this-demo)

---

## What ASIL does

Point it at any codebase (Python / JS / TS / TSX / Go / Ruby / Java / Rust / C / C++ / PHP / Swift / Kotlin) and ASIL will:

1. **Build a knowledge graph** of files, functions, classes, imports, call edges (Neo4j) + a semantic vector index of function bodies (Qdrant).
2. **Answer natural-language questions** about the code, with file:line citations and a confidence score. Every claim is verified against its cited evidence.
3. **Remember every conclusion** in an episodic store (Postgres) so the next session — yours or anyone else's — recalls the cached answer instead of re-running the full pipeline. The cache short-circuit is real: when a question's embedding matches a prior one above the threshold, ASIL returns the stored answer and **skips the reasoning + verifier LLM calls entirely**. Whether this saves real money on your codebase depends on how often near-duplicate questions get asked; see [docs/measuring-savings.md](docs/measuring-savings.md) for the A/B protocol that turns "depends" into a number.
4. **Hand context across IDEs and agents.** Run `asil context export` in your Claude Code session to ingest the whole conversation — questions, prose, **files edited, commands run, final task list** — into ASIL. Then run `asil context import cursor` (or `claude-code`, `aider`, `openhands`, `prompt`) on the new agent to wire it up. Cross-IDE memory works whether the other tool speaks MCP or not.
5. **Ingest production incidents** as postmortems → derive `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)` edges across three observable strategies (no LLM hallucination, every edge auditable).
6. **Replay any incident** as a time-ordered timeline + service cascade + state diff (deployments-during, metric-before/after) + ranked causal chain.
7. **Detect architecture drift** between your current dependency structure and a stored baseline — before the PR merges.
8. **Pull live data** from Prometheus / Loki / Kubernetes (metrics, logs, deployments) and external systems (GitHub PRs, Slack messages, Jira / Linear tickets).
9. **Propose code fixes** constrained by the Phase-5 causal chain — read-only by default, optional sandbox apply + test run, every attempt audited to Postgres with the diff + sandbox output + outcome (`accepted` / `rejected` / `inconclusive` / `proposed`).
10. **Share memory across a team** — point multiple ASIL installs at the same Postgres, gate access with per-team API keys (`asil team create`), and every memory carries `user_id` + `machine_id` + `origin_agent` so cross-team recalls render *"originally answered on 2026-05-26 by `alice` via claude-code on `workstation-7`"*.
11. **Expose all of the above as 14 MCP tools** so Claude Code / Cursor / OpenHands / Aider / Cody can call ASIL programmatically. Every `asil.ask` response carries a `provenance` block — calling agents can render *"Recalled from ASIL — proceed with full research?"* before showing the cached answer.

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
- Episodic-memory write (so the next session can recall this without re-running the full pipeline).

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
- **Memory savings card** — counts cache-hits (questions answered from prior conclusions) and compares the average measured fresh-ask cost against the average recall-hit cost on your ledger. Numbers are whatever your codebase actually produced, not estimates. See [docs/measuring-savings.md](docs/measuring-savings.md) for the A/B protocol that makes the number defensible.

**Use it for:** seeing your actual spend, deciding whether the cache threshold is set sensibly, sharing real numbers in writing.

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
uv run asil memory stats                              # per-repo conclusion counts + totals
uv run asil memory stats --dedupe-rate                # write-time dedupe ratios from asil_memory_writes
uv run asil memory stats --by-agent --by-source       # writes broken out by origin agent + transcript source
uv run asil memory stats --top-recalled 10            # memories with the most cache hits
uv run asil memory list                               # newest conclusions across all repos
uv run asil memory recall --limit 10                  # like `list` but for review
uv run asil memory recall --repo "local:/path"        # scoped to one repo
uv run asil memory show <memory_id>                   # full record incl. citations + provenance
uv run asil memory forget <memory_id>                 # delete one memory
uv run asil memory forget-session <session-uuid>      # delete every memory from one ingested session
uv run asil memory clear <repo_key>                   # nuke every memory for one repo
uv run asil memory clear-all                          # nuke EVERY memory across the whole store (prompts)
```

### `context` — one-command cross-IDE handoff (Phase 9)

```bash
uv run asil context export                            # auto-detect cwd, ingest last 2h of Claude Code session
uv run asil context export --since 1d                 # bigger window
uv run asil context export --file /tmp/ctx.md         # also write a portable markdown bundle (for non-MCP agents)

uv run asil context import claude-code                # print ~/.claude/settings.json MCP snippet
uv run asil context import cursor                     # print ~/.cursor/mcp.json snippet
uv run asil context import openhands                  # print config.toml snippet
uv run asil context import mcp                        # generic MCP HTTP wiring
uv run asil context import prompt                     # paste-able markdown summary (works with any LLM/IDE)
uv run asil context import prompt --about "auth flow" # topic-scoped recall, capped at --limit
```

See [Cross-IDE memory handoff (the two-command flow)](#cross-ide-memory-handoff-the-two-command-flow) for the end-to-end walkthrough.

### `ingest-transcripts` — pull AI session transcripts into memory (Phase 9.3 / 9.4)

```bash
uv run asil ingest-transcripts claude-code                          # all Claude Code sessions, all projects
uv run asil ingest-transcripts claude-code --since 1h               # last hour only
uv run asil ingest-transcripts claude-code --project /path/to/repo  # scope to one repo
uv run asil ingest-transcripts claude-code --session <uuid>         # one specific session
uv run asil ingest-transcripts claude-code --dry-run                # preview, no writes

uv run asil ingest-transcripts cursor                               # Cursor's workspaceStorage SQLite
uv run asil ingest-transcripts cursor --workspace <ws-id>           # one workspace

uv run asil ingest-transcripts generic-jsonl \
  --path ~/.aider/chat.jsonl --source aider-transcript \
  --role-key role --text-key content --user-label user --assistant-label assistant
```

Every chunk that lands carries **prose + Actions taken (files edited, commands run, sub-agents) + Final task list** so future agents recall the full implementation context, not just the conversation.

### `watch` — keep memory fresh automatically (Phase 9.4)

```bash
uv run asil watch claude-code,cursor                  # poll every 30s, ingest new turns
uv run asil watch claude-code --interval 60           # custom interval
uv run asil watch claude-code --iterations 5          # exit after N polls (testing)
```

Long-running. SIGINT/SIGTERM exit cleanly. Write-time dedupe folds re-ingested turns so it's safe to leave running.

### `team` — multi-team API keys (Phase 9.5)

```bash
uv run asil team create startup-dev --name "Startup Dev"   # mints raw key, shown ONCE
uv run asil team list                                       # status table
uv run asil team rotate-key startup-dev                     # mint new key, invalidate old
uv run asil team revoke startup-dev                         # mark revoked
```

API-key auth gates `/mcp/*` and `/dashboard/*` once teams exist. Set `ASIL_AUTH_DISABLE=true` for local dev to bypass.

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

The savings card reads real numbers off the ledger: it counts cache-hits (questions where the recall path returned a stored answer), averages the actual recorded cost of fresh asks vs recall-hit asks, and multiplies. If you've never run a cache hit yet, the card will tell you so — no fabricated savings %. For a defensible end-to-end measurement, run the A/B protocol in [docs/measuring-savings.md](docs/measuring-savings.md).

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

### `scan` — SonarQube-style CI entry point

```bash
# Default: read the graph + saved baseline, print findings to the terminal, exit 0/1 based on the gate.
uv run asil scan

# Full CI invocation: PR comment + SARIF for code scanning + JSON for archival + strict gate.
uv run asil scan \
  --baseline asil-baseline.json \
  --gate normal \
  --sarif asil.sarif \
  --pr-comment asil-pr-comment.md \
  --json asil.json
```

What it does:

1. Connects to the local Neo4j graph (no LLM call — scan is cheap).
2. Runs the Phase-6 drift detector against the baseline JSON if one is provided (empty baseline → every observed dependency reads as new).
3. Queries the Phase-4 causal links for incidents in the last `--incident-lookback-hours` (default 168h). Each incident's top causal chain is emitted as a `note` finding so the reviewer sees "the incident last Tuesday had a deploy of code in this PR as its top cause."
4. Aggregates findings into a `ScanReport` with severity counts.
5. Applies the quality gate: `strict` (fail on warning+), `normal` (fail on error+, default), `lenient` (fail on critical only), `none` (always pass).
6. Emits whichever output formats you asked for. Exit code is `0` if the gate passed, `1` if it failed, `2` if ASIL itself crashed.

Outputs:

- **SARIF 2.1.0** (`--sarif`): the standard CI tools speak. GitHub code scanning, SonarQube, Semgrep — anything that consumes SARIF will surface ASIL's findings in the same UI as your other linters.
- **GitHub-flavored markdown PR comment** (`--pr-comment`): a single comment with a pass/fail badge, severity counts, and one `<details>` block per tier listing the findings.
- **JSON** (`--json`): the full `ScanReport` for archival or custom dashboards.
- **Terminal table** (default): human-readable for local invocation.

### CI templates

**GitHub Actions** — drop [.github/workflows/asil-scan.yml](.github/workflows/asil-scan.yml) into any repo. It spins Neo4j + Qdrant + Postgres as service containers, ingests the PR's code, runs `asil scan`, posts the PR comment (edits the prior one in place on subsequent runs), uploads the SARIF to GitHub code scanning, and fails the workflow on a gate failure. No external ASIL server required.

**pre-commit** — the repo ships [.pre-commit-hooks.yaml](.pre-commit-hooks.yaml) so any project using `pre-commit` can wire ASIL into its hook stack:

```yaml
# .pre-commit-config.yaml in your repo
repos:
  - repo: https://github.com/rkstlohchab/ASIL
    rev: main
    hooks:
      - id: asil-scan          # gate normal, runs on pre-push
      # or for warnings-and-above:
      - id: asil-scan-strict
```

### `fix` — constrained fix pipeline (Phase 8)

```bash
# Read-only — generate a patch from the causal chain, show the diff, exit.
uv run asil fix propose INC-2026-04-12-payments-cascade

# Same, but also persist the proposal to the audit log even though no sandbox ran.
uv run asil fix propose INC-... --record

# Full pipeline: propose -> ephemeral sandbox -> git apply -> run tests -> audit.
uv run asil fix run INC-2026-04-12-payments-cascade
uv run asil fix run INC-... --test-command "uv run pytest tests/unit -q" --timeout 120
uv run asil fix run INC-... --confidence-gate 0.7

# Browse the audit log.
uv run asil fix list                              # most recent across all incidents
uv run asil fix list --incident-id INC-...        # one incident only
```

What happens:

1. Load the Phase-5 `ReplayResult` for the incident. Refuse if there is no causal chain (the moat must run first).
2. Gather code context from the top causes' `file_path` / `service_name -> Service.file_paths` props. Cap at 4 files × 2000 chars by default.
3. LLM call via `ModelRouter.call(tier="reasoning")` with a constrained prompt — emit ONLY a minimal unified diff for the implicated files.
4. Parse the diff (fenced or bare). Compute aggregate confidence = `min(top_cause_confidence, replay_confidence)`.
5. For `run`: copy the repo to a temp dir, `git apply --check` then `git apply`, execute the test command with a wall-clock timeout, capture stdout/stderr tails.
6. Persist to Postgres `asil_fix_audit`. Aggregate `FixOutcome` is `accepted` (tests passed + confidence ≥ gate), `rejected` (tests failed or apply failed), `inconclusive` (timeout or low confidence), or `proposed` (no sandbox).

Nothing is pushed, nothing is merged. The proposal + sandbox result is the artifact you (or an orchestrator) decide on.

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

## Cross-IDE memory handoff (the two-command flow)

The headline Phase 9 feature: end a long session in one IDE, pick up the *full* context — questions, prose, files edited, commands run, completed tasks — in any other IDE or any LLM provider.

### The flow

```bash
# In the IDE where you just did real work (e.g. Claude Code):
uv run asil context export                # auto-detects cwd + recent transcript, ingests into ASIL

# In the next IDE (Cursor / Antigravity / Aider / OpenHands / anything):
uv run asil context import cursor         # prints the MCP wiring snippet to paste
# …or, for agents that don't speak MCP:
uv run asil context import prompt         # prints a paste-able markdown context summary
```

That's it. The cache short-circuit in `asil.ask` does the rest — the new agent calls `asil.ask` (directly or via MCP), gets a `provenance.is_cached=true` response with the cached answer plus a preamble like:

> *Recalled from ASIL — originally answered on 2026-05-26 by `alice` via claude-code on `workstation-7` (similarity 0.94). Reasoning + verifier LLM calls were skipped. Proceed with full research?*

### What "context" actually contains

Each Q/A pair stored carries three sections in the response body:

1. **Prose** — the assistant's narrative explanation (text blocks only; thinking + tool noise stripped).
2. **Actions taken in this turn** — bulleted summary derived from the tool_use calls:
   - **Edited:** comma-separated relative file paths
   - **Wrote:** new files created
   - **Read:** files read (capped at 10, with `+N more`)
   - **Ran:** Bash commands with their `description` field
   - **Sub-agents:** sub-agent spawns (subagent_type + description)
3. **Final task list** — the *last* TodoWrite state of the turn, rendered with ✅ / ⏳ / ⬜ icons.

So when a future agent recalls a memory, it sees both *what was discussed* and *what was implemented* — not just the chat.

### Keeping memory fresh automatically

Instead of running `asil context export` manually, let a watcher do it:

```bash
uv run asil watch claude-code,cursor --interval 30
```

Polls each agent's transcript directory every 30s, ingests new turns, dedupes against existing memories. Leave it running in a tmux pane.

### Going offline (no shared Postgres available)

If you want to take context to a machine that can't reach your Postgres (a different network, a flight), use the portable bundle:

```bash
uv run asil context export --file /tmp/handoff.md   # writes self-contained markdown
# move handoff.md to the other machine, paste into the new agent's prompt
```

The bundle includes every Q/A pair with the full Actions/Tasks sections — no DB lookup needed.

---

## Multi-team setup

Multiple machines pointed at the same Postgres = shared memory. To gate it properly:

### Create a team and mint its first API key

```bash
uv run asil team create startup-dev --name "Startup Dev Team"
# The raw API key is printed in a yellow panel exactly ONCE.
# Format: asil_<team-id>_<24-char-secret>
# Store it in your secret manager immediately.
```

### Configure clients

```bash
# Server: turn auth ON (it's on by default if `ASIL_AUTH_DISABLE` is unset)
export ASIL_TEAM_API_KEY=asil_startup-dev_xxx...

# Clients: pass the key as Bearer auth
curl -sS http://localhost:8000/mcp/call/asil.ask \
  -H "Authorization: Bearer $ASIL_TEAM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"question":"..."}}'
```

MCP clients (Claude Code, Cursor, OpenHands) typically accept an `Authorization` header in their MCP server config — see [Wiring ASIL into your AI agent (MCP)](#wiring-asil-into-your-ai-agent-mcp).

### Local development

```bash
export ASIL_AUTH_DISABLE=true        # bypasses the middleware entirely
# every request gets team_id='default'; no key required
```

This is what `make api-dev` assumes. Unset the env var to flip auth back on.

### Rotating + revoking keys

```bash
uv run asil team rotate-key startup-dev   # mint new key, old one stops working
uv run asil team revoke startup-dev       # mark revoked; auth returns 401 immediately
uv run asil team list                     # status table
```

### What teams scope

Every `asil_memories` row, every `asil_costs` row, every `asil_memory_writes` row carries a `team_id`. Different team keys → isolated memory pools. Same key on two machines → shared pool.

The local-dev default `team_id='default'` is what gets stamped when auth is bypassed.

---

## Deleting memories

Four levels of granularity, smallest blast radius first:

```bash
uv run asil memory forget <memory_id>             # one memory (Postgres row + Qdrant point)
uv run asil memory forget-session <session-uuid>  # everything from one ingested session
uv run asil memory clear <repo_key>               # everything for one repo
uv run asil memory clear-all                      # nuke EVERYTHING (prompts for y/n)
```

`forget-session` is the one you'll use most — it matches both `origin_session_id` (memories *written* during a session) and `metadata.original_session_id` (memories *ingested from* a session's transcript), so undoing an `asil context export` is one command.

The session UUID for a Claude Code session is the `.jsonl` filename under `~/.claude/projects/<encoded-cwd>/`. The web `/memory` page's "Top recalled" panel also surfaces `origin_session_id` per row.

All four commands prompt for confirmation by default; pass `--yes` to skip.

---

## Wiring ASIL into your AI agent (MCP)

All 12 tools are exposed at:

```text
POST http://localhost:8000/mcp/call/<tool_name>
Body: {"arguments": { ... }}
```

The full tool list (catalog at `GET /mcp/tools` — 14 tools):

| Tool | What it does |
|---|---|
| `asil.search_code` | Hybrid semantic + graph search → ranked functions/classes with citations |
| `asil.get_callers` | Every function calling a given qualified name (1-hop) |
| `asil.get_dependencies` | Inverse — functions that the target calls |
| `asil.who_owns` | Containing file + last-commit author (git blame) |
| `asil.commit_history` | Recent commits touching a file |
| `asil.ask` | Full reasoning pipeline with cache short-circuit. On a high-similarity recall hit, returns the cached answer + provenance preamble and skips the reasoning + verifier LLM calls. Accepts `client_id` / `session_id` / `cache_threshold`. |
| `asil.full_research` | Same args as `asil.ask` but forces `cache_threshold=1.01` so the cache never fires. Wire to a "Proceed with full research" button. |
| `asil.remember` | Explicitly persist an out-of-band conclusion (opts out of dedupe — intentional insert). |
| `asil.recall` | Semantic search over past conclusions; each hit carries a `provenance` block (`originated_by_user`, `originated_via_agent`, `originated_on_machine`, `created_at`, similarity, ready-to-render `preamble`). |
| `asil.forget` | Delete a memory |
| `asil.find_causes` | Ranked causal candidates for an incident |
| `asil.replay_incident` | Timeline + cascade + state diff + confidence |
| `asil.drift_check` | New dependencies + boundary violations vs baseline |
| `asil.propose_fix` | Generate a constrained patch from a Phase-5 causal chain (read-only by default) |

**Every response from `asil.ask` / `asil.recall` carries a `provenance` block** — calling agents should render the `preamble` to the user before showing the cached answer. The preamble looks like:

```text
Recalled from ASIL — originally answered on 2026-05-26 by alice via claude-code
on workstation-7 (similarity 0.94). Reasoning + verifier LLM calls were skipped.
Proceed with full research?
```

The simplest "fast tour" of all this from any IDE: run `asil context import <your-ide>` and paste the snippet it prints.

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

If you have ASIL running locally already, the easiest way to get the exact snippet for any IDE is:

```bash
uv run asil context import cursor       # or claude-code / openhands / aider / mcp
```

### Authentication

When `ASIL_AUTH_DISABLE` is not set on the server, every request to `/mcp/*` or `/dashboard/*` must carry `Authorization: Bearer <team-api-key>`. See [Multi-team setup](#multi-team-setup) for key creation.

---

## How memory saves you API calls

Every conclusion ASIL produces goes into Postgres (`asil_memories`) plus a Qdrant point keyed by the question vector. The next `asil ask` (yours or any agent's via MCP) first embeds the new question and searches memory. If the top hit's similarity is above the cache threshold (default `0.92`), ASIL returns the stored answer directly and **skips the reasoning + verifier LLM calls entirely**. The only cost on a hit is the embedding call used to do the lookup. Below the threshold, ASIL still runs the full pipeline but injects the prior conclusions into the prompt as context.

Whether this saves money on **your** codebase is an empirical question, not a marketing one. It depends on:

- How often you ask the same (or near-duplicate) questions.
- The cache-similarity threshold you choose — higher = fewer hits, but more confident hits.
- Your LLM profile — a `tight` profile has cheaper fresh asks, so the absolute saving per hit is smaller.

The answer for your repo is whatever your cost ledger says it is. The savings card in the dashboard reads real numbers off the ledger — it counts actual cache-hits (the `recall_hits` counter on each memory row), averages the recorded cost of fresh asks vs recall-hit asks, and multiplies. If you've never had a cache hit yet, the card says so honestly rather than fabricating a percentage.

```bash
uv run asil cost summary                 # totals + by-provider + by-tier + measured savings
uv run asil cost daily --days 14         # daily-spend bar chart
uv run asil memory stats --dedupe-rate   # write-time dedupe ratio (how often near-duplicates folded)
uv run asil memory stats --top-recalled  # most-used memories — questions your team keeps asking
```

The ledger schema (`asil_costs`) is one row per LLM call: `ts, provider, model, tier, profile, input_tokens, output_tokens, cost_usd, team_id`. Cache-hits show up as a single `tier=embed` row instead of the usual reasoning + verify + embed triplet. Aggregations happen in SQL (`asil_core.llm.PostgresCostLedger.aggregates`).

For a *defensible* end-to-end measurement, see [Measuring real savings on your codebase](#measuring-real-savings-on-your-codebase) below.

---

## Inspecting the data inside Docker

Everything ASIL stores lives in one of three Docker services. Nothing is hidden; nothing is in process memory.

| Store | Open it | What's there |
|---|---|---|
| **Neo4j** | <http://localhost:7474> · `neo4j` / `asil_dev_password` | The code graph + runtime-event graph. `(:Repo)-[:CONTAINS]->(:File)`, `(:Function)-[:CALLS]->(:Function)`, `(:Cause)-[:PRECEDED]->(:Incident)` with `confidence` + `strategy` props. |
| **Qdrant** | <http://localhost:6333/dashboard> | Two collections: `asil_code` (one point per function/class body) and `asil_memories` (one point per stored conclusion, vector = question embedding). |
| **Postgres** | `psql postgresql://asil:asil_dev_password@localhost:5432/asil` | The episodic store + cost ledger + fix audit log + teams table. Key tables: `asil_memories`, `asil_costs`, `asil_memory_writes`, `asil_fix_audit`, `asil_teams`. |

Starter Cypher / SQL queries and a full walkthrough for each store are in **[docs/inspecting-the-graph.md](docs/inspecting-the-graph.md)**. CLI shortcuts:

```bash
uv run asil graph stats                          # node counts per label
uv run asil graph query "MATCH (f:Function) RETURN f LIMIT 5"
uv run asil vector stats                         # Qdrant collection size + dim
uv run asil memory stats --by-source --by-agent  # Postgres slice
```

---

## Measuring real savings on your codebase

If you're going to claim that ASIL's memory layer saves money, the number has to come from your own ledger, not from an estimate. The repo ships an A/B benchmark:

1. Pick a fixed list of representative questions (the seed corpus at [research/savings-benchmark.yaml](research/savings-benchmark.yaml) has 20 about ASIL itself).
2. Take a timestamp marker on `asil_costs`.
3. Run every question with `--no-recall --no-remember`. Sum the cost since the marker — that's your **cold cost**.
4. Run every question again with full recall enabled. Sum since the next marker — that's your **warm cost**.
5. Real saving = `(cold − warm) / cold`. Token saving = same with `input_tokens + output_tokens`.

The full protocol, including edge cases (mock providers, profile mismatch, prior memories) is in **[docs/measuring-savings.md](docs/measuring-savings.md)**. That's the document to follow if you want a percentage you can put in writing.

---

## Status

**Phases 0 – 9 ✅ done.** The engine + the dashboard + live infrastructure adapters + external-system adapters + the constrained fix pipeline + the cross-IDE memory broker (cache short-circuit, provenance preamble, transcript ingesters for Claude Code / Cursor / generic JSONL, watch daemon, multi-team auth, observability dashboard) are all shipped. No remaining stretch items on the roadmap.

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
  asil_fix/         Constrained patch generator + sandbox + audit log (Phase 8)

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

---

## How to record this demo

The GIF at the top of this README is regenerated from `make demo-auto` piped through asciinema. Replace it any time you change the CLI surface — the recording is deterministic given the same backing data.

**One-time install:**

```bash
brew install asciinema agg          # asciinema records; agg renders to GIF
```

**Record + render:**

```bash
asciinema rec docs/assets/asil-demo.cast
# inside the recording shell:
make demo-auto
# wait for "Tour complete", then exit with Ctrl+D
agg --speed 1.5 docs/assets/asil-demo.cast docs/assets/asil-demo.gif
```

`make demo-auto` is the same script that runs interactively as `make demo` — it just uses fixed pauses (2s short, 4s long) so the recording flows without you having to hit ENTER. Total length is ~90 seconds, GIF size lands around 2–4 MB, which embeds cleanly in Medium / GitHub.

**Knobs:**

```bash
./scripts/record_demo.sh --auto --short 3 --long 6            # slower pacing
./scripts/record_demo.sh --auto --incident INC-...            # different incident
./scripts/record_demo.sh --auto --question "how does X work?" # different ask
```

**For the dashboard half (stills or video, not in the GIF):**

- ReactFlow causal graph: navigate to `http://localhost:3001/incidents/INC-2026-04-12-payments-cascade`. Cmd+Shift+4 for a crop, or Cmd+Shift+5 → "Record Selected Portion" for a clip.
- `/cost` page is the screenshot for budget-review / blog-post savings claims.
- For a polished single-frame terminal still (front-of-deck quality): `brew install charmbracelet/tap/freeze`, then `freeze --output cost.png "uv run asil cost summary"`.
