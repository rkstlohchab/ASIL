# ASIL — Engineering Intelligence Infrastructure

**Plan version:** v2 (revised 2026-05-19 after positioning + scope feedback)
**Target repo:** `/Users/raksithlochabb/Documents/GitHub/ASIL` (currently empty)

---

## Context

You're building **engineering intelligence infrastructure** — a persistent, queryable, *temporal* understanding of how a software system evolves, behaves, and fails. Not a coding assistant. Not another autonomous coder. Not "AI OS." Those are crowded commodity spaces.

The defensible position is *underneath* coding agents: the layer that knows what changed, what broke, when, why, and how confident we are in that answer. Coding agents (OpenHands, Cursor, Claude Code, Aider) become **clients** of ASIL — they query its knowledge graph and incident replay via MCP. ASIL itself is not trying to file PRs as its hero feature; it's trying to **explain reality** with evidence and confidence.

**The moat is temporal causality** — almost nobody builds this deeply. Current AI coding tools understand code *statically*. ASIL understands how the system *evolved* — which deployment preceded which incident, which config change correlated with which metric shift, which commit drifted the architecture from its prior boundaries.

### Why this is hard (and resume-defining)

Combines program analysis, temporal graphs, vector retrieval, causal inference, distributed-systems observability, evidence-weighted reasoning, and continual learning. The hard parts are the *temporal* and *causal* parts — and they're hard precisely because nobody has solved them yet.

### Validated against 2026 landscape

Web research today (2026-05-19) confirms / updates the stack:

- **LangGraph v0.4** still leads stateful orchestration, but the 2026 trend is **fewer agents, better state machines** — deterministic pipelines beat chatty multi-agent. Use LangGraph for state + checkpoints, not for "agents debating."
- **Tree-sitter is the standard.** GitHub archived Stack Graphs in Sept 2025. Pair Tree-sitter with **SCIP** (Sourcegraph) for cross-repo symbol resolution.
- **Codebase-Memory** (Tree-sitter → SQLite knowledge graph → MCP tools) hit 900+ stars in 4 weeks and showed ~10× lower token cost vs file-based exploration. Build on this pattern for the structural code layer.
- **Hybrid vector + graph beats pure GraphRAG** on latency in production. Qdrant + Neo4j, not GraphRAG alone.
- **Memory**: Mem0 for episodic (lightweight), graph for relational, vector for semantic. Letta/Zep are alternatives if Mem0 doesn't scale.
- **Primary model**: Claude Opus 4.7 (SWE-bench Pro leader, lowest hallucination). GPT-5.5 fallback for long-horizon work. DeepSeek V4 as cost-tier default during the tight-budget build.
- **Reference for infra agents**: KubeIntellect paper — supervisor + domain-aligned K8s adapters.
- **Crowded spaces to avoid**: OpenHands (72k stars, $18.8M Series A), Aider, Continue, Cody. They're all "agent edits code." Don't compete. Sit underneath.

### What we're shipping

**Confirmed scope**: 12+ months solo, startup-track, tight budget now but architecture must scale to generous. **Full Phase 0–6** including temporal causality + execution replay + architecture drift detection. Autonomous PR-filing is explicitly deprioritized to "stretch" — that's commodity work and not where the moat lives.

The **hero demo** that defines v1 done:

> **"Why did this production incident happen?"**
> Input: a service + a timestamp.
> Output:
> - Reconstructed incident timeline (events, deploys, config changes, metric shifts, traffic spikes).
> - Probable root cause with **confidence score** (e.g., 78%).
> - Evidence list: latency spike 3 min after deploy, correlated error logs, dependency graph overlap, similar historical incident.
> - Causal chain visualization: which change propagated to which service via which dependency edge.
> - Architecture drift report: did this incident violate previously-stable boundaries?

That single capability requires every layer (structural graph, temporal graph, memory, infra ingestion, causality engine, replay engine, confidence scoring) to actually work. Ship that and the project is publishable, resume-defining, and startup-worthy on its own.

**Hard constraint for solo + 12 months**: strict phase gates. Do not start Phase N+1 until Phase N has a recorded demo and a written design doc in `research/`. Solo founders die by drift — the gates are the antidote.

---

## Differentiation (read before building)

Every feature decision gets weighed against:

> *Does this move us toward temporal/causal/evidence-weighted understanding, or are we drifting into commodity coding-agent territory?*

If the answer is the second one, defer it.

ASIL's four defensible pillars:

1. **Temporal causality** — the system tracks *events and their relationships over time*, not just static structure. `(:Deployment)-[:PRECEDED]->(:Incident)`, `(:MetricShift)-[:CORRELATED_WITH]->(:Commit)`.
2. **Execution replay** — given an incident, reconstruct what happened across services as a time-ordered, dependency-aware causal chain. "Time-travel debugging for distributed systems."
3. **Confidence-scored reasoning** — every conclusion ships with a score, evidence count, retrieval strength, and causal confidence. Enterprise-ready answers, not LLM hallucinations.
4. **Architecture drift detection** — ASIL learns the *expected* architecture and flags undocumented coupling, decay, and anti-pattern growth.

Things ASIL is explicitly *not*:
- Not an "AI OS." Not branded that way.
- Not an autonomous coder. PR-filing is stretch, not core.
- Not a chat UI. CLI + API + MCP for the first 4–5 months. UI comes after the engine works.
- Not a multi-agent debate framework. Deterministic pipelines wherever possible.

---

## Architecture (target)

```
                ┌─────────────────────────────────────────┐
                │  Clients: CLI / Coding agents (OpenHands,│
                │  Cursor, Claude Code) via MCP / REST    │
                └────────────────────┬────────────────────┘
                                     │
                ┌────────────────────▼────────────────────┐
                │       ASIL API Gateway (FastAPI)        │
                └────────────────────┬────────────────────┘
                                     │
   ┌──────────────┬──────────────────┼──────────────────┬─────────────────┐
   ▼              ▼                  ▼                  ▼                 ▼
┌────────┐  ┌──────────┐    ┌─────────────────┐  ┌─────────────┐  ┌──────────────┐
│Ingest  │  │Temporal  │    │ Reasoning       │  │ Replay      │  │ Infra Bridge │
│Workers │  │Causality │    │ Pipeline        │  │ Engine      │  │ (K8s/Logs/   │
│        │  │Engine    │    │ (Retrieve→Graph │  │ (Incident   │  │  Prom)       │
│        │  │          │    │  →Temporal→     │  │  Timeline)  │  │              │
│        │  │          │    │  Reason→Verify) │  │             │  │              │
└───┬────┘  └────┬─────┘    └────────┬────────┘  └──────┬──────┘  └──────┬───────┘
    │            │                   │                  │                │
    └────────────┴───────────────────┴──────────────────┴────────────────┘
                                     │
   ┌─────────────────────────────────▼─────────────────────────────────┐
   │  Storage layer                                                     │
   │  • Neo4j           (structural + temporal causality graph)         │
   │  • Qdrant          (semantic embeddings of code+docs+incidents)    │
   │  • Postgres        (metadata, audit log, cost ledger, confidence)  │
   │  • Redis           (cache, pipeline state, pub/sub)                │
   │  • Object storage  (raw logs, traces, large artifacts)             │
   └────────────────────────────────────────────────────────────────────┘
```

Six subsystems, built strictly in order:

1. **Ingestion + Structural Graph** — Tree-sitter parser, SCIP indexer, embeddings, code/dependency graph.
2. **Persistent Memory + Confidence Scoring** — vector + graph + episodic, with every stored conclusion carrying evidence + scores.
3. **Infra Bridge** — K8s, Prometheus, Loki adapters feed *events* (deployments, config changes, metric shifts, alerts) into the graph.
4. **Temporal Causality Engine** — the moat. Causal edges, time-ordered queries, correlation scoring.
5. **Execution Replay Engine** — incident timeline reconstruction, cascade visualization, before/after state diff.
6. **Architecture Drift Detection** — baseline architecture model, deviation alerts.

Multi-agent orchestration and any autonomous-execution work sit *on top of* this stack as a deferred Phase 7+ — and even there, the design is **deterministic pipelines first, agents only where genuinely needed**.

---

## Graph schema (the heart of the system)

The graph is where the moat lives. Schema must be designed deliberately.

### Structural nodes (Phase 1)

`Repo`, `File`, `Module`, `Class`, `Function`, `Symbol`, `Commit`, `Author`, `PR`

Structural edges:
`CONTAINS`, `IMPORTS`, `CALLS`, `DEFINED_IN`, `MODIFIED_BY`, `AUTHORED_BY`, `MERGED_FROM`

### Runtime / temporal nodes (Phase 3–4)

`Service`, `Deployment`, `ConfigChange`, `Event`, `MetricShift`, `Alert`, `Incident`, `TrafficSpike`, `LogSignature`, `Trace`

Each has `timestamp` (and `start_ts`/`end_ts` where relevant), `source` (which adapter produced it), and `confidence` (how reliable the observation is).

### Causal edges (Phase 4 — the moat)

```
(:Deployment)-[:PRECEDED {delta_seconds, confidence}]->(:Incident)
(:MetricShift)-[:CORRELATED_WITH {pearson_r, lag_seconds}]->(:Commit)
(:ConfigChange)-[:AFFECTED]->(:Service)
(:Service)-[:DEPENDS_ON {observed_at, strength}]->(:Service)
(:Incident)-[:CASCADED_TO {hop, path}]->(:Service)
(:Commit)-[:SHIPPED_IN]->(:Deployment)
(:LogSignature)-[:EMITTED_BY]->(:Service)
(:Alert)-[:TRIGGERED_BY]->(:MetricShift)
```

Every causal edge stores **how we computed the link** (`derivation`: `"temporal_proximity"`, `"co_occurrence"`, `"explicit_reference"`, etc.) and a **confidence score**. This is what makes the answers explainable.

### Drift baseline (Phase 6)

`ExpectedDependency`, `ArchitectureBoundary`, `DriftEvent`

```
(:Service)-[:VIOLATES]->(:ArchitectureBoundary)
(:DriftEvent)-[:OBSERVED_IN]->(:Service)
```

---

## Confidence scoring (cross-cutting from Phase 2)

Every conclusion ASIL emits — every retrieval, every causal claim, every root-cause hypothesis — carries:

```python
@dataclass
class Confidence:
    score: float                # 0.0–1.0 overall
    evidence_count: int         # how many independent supports
    retrieval_strength: float   # avg similarity of supporting chunks
    causal_confidence: float    # strength of any causal edges used
    derivation: list[str]       # human-readable list of supports
```

Stored in Postgres alongside the conclusion. Surfaced in every API response. **Never** strip it before showing the user.

This is what turns ASIL from "another LLM that might be lying" into "enterprise-trustable infrastructure." It's also the thing your eventual paper/blog post hangs on.

---

## Reasoning as a deterministic pipeline (not an agent debate)

The hero query (`"why did this incident happen?"`) runs through a **fixed pipeline**, not a free-form agent loop:

```
1. Retrieve         — vector search for related code, docs, prior incidents
2. Graph expand     — 1–2 hops from candidate nodes in structural graph
3. Temporal correlate — find events within ±N minutes of incident timestamp
4. Causal score     — compute strength of each candidate causal edge
5. Reason           — LLM synthesizes from the (now-narrow, structured) evidence
6. Verify           — second LLM pass checks claims against evidence; flags any unsupported
7. Score            — assemble Confidence object
8. Respond          — structured JSON + natural-language explanation
```

LangGraph manages state and checkpoints between steps. Each step is a node; each node is **deterministic except for the LLM call inside it**. Cost is bounded. Behavior is debuggable. You can replay a query against an older graph snapshot.

Specialized roles (DebugAgent, InfraAgent, DocAgent) become **pipelines parameterized by role**, not autonomous chat participants. Multi-agent debate is reserved for the rare cases where it provably helps (e.g., one critique pass).

---

## Tech stack (locked choices with rationale)

| Layer | Choice | Why |
|---|---|---|
| Language (backend) | **Python 3.12+** | AI ecosystem, async maturity |
| API | **FastAPI** + Uvicorn | Standard, fast, type-safe |
| Pipeline orchestration | **LangGraph v0.4** | Stateful, checkpointable; used for *pipelines*, not agent debates |
| Structured agents | **Pydantic AI** | Type-safe deterministic role pipelines |
| Claude SDK | **Claude Agent SDK** | Auto-compaction when calling Anthropic |
| Code parsing | **Tree-sitter** | Multi-language standard |
| Semantic code index | **SCIP** (Sourcegraph) | Cross-repo symbol resolution |
| Code-graph pattern | **Codebase-Memory** style | Tree-sitter → graph → MCP tools |
| Graph DB | **Neo4j Community** | Cypher, native temporal indexes, free tier |
| Vector DB | **Qdrant** | Fast, self-hostable, hybrid search |
| Episodic memory | **Mem0** | Light, swappable to Zep/Letta later |
| Relational DB | **PostgreSQL 16** | Metadata, audit log, cost ledger, confidence rows |
| Cache + pub/sub | **Redis** | Standard |
| Embeddings | **BGE-large** (self-host, tight) → **Voyage-3-code** (generous) | Tier-swappable |
| Primary LLM (generous) | **Claude Opus 4.7** | SWE-bench Pro leader, low hallucination |
| Fallback LLM | **GPT-5.5** | Long-horizon autonomous tasks |
| Cost-tier LLM (tight default) | **DeepSeek V4** | Open-weight, cheap |
| Tool protocol | **MCP** | Lets Cursor/Claude Code/Cody *call* ASIL — central to positioning |
| K8s client | **kubernetes-asyncio** | Async-native |
| Log adapter | **loki-client** | Standard |
| Metrics adapter | **prometheus-api-client** | Standard |
| Observability (self) | **OpenTelemetry** + Grafana + Prometheus + Loki | Dogfood the stack we ingest |
| Container | **Docker** + Docker Compose (dev) → K8s (prod) | Standard |
| Worker queue | **Arq** (Redis-backed) | Lighter than Celery |
| CLI | **Typer** | Rich CLI is the primary UX for months 1–5 |
| Frontend (deferred) | **Next.js 15** + TS + Tailwind + ReactFlow | After engine ships |

**Explicitly NOT using:**
- Stack Graphs — archived Sept 2025.
- Pure GraphRAG — oversold, hybrid beats it.
- Celery — overweight for this scope.
- LlamaIndex as core — utility only; LangGraph is the orchestrator.
- AutoGen v0.4 — fine framework, but its multi-agent emphasis pulls in the wrong direction for this product.

### LLM provider abstraction (cross-cutting, built in Phase 0)

You confirmed: **budget is tight now, but the system must scale cleanly to generous later.** That makes the LLM layer a first-class architectural concern.

Build in `packages/asil_core/llm.py`:

```python
class LLMProvider(Protocol):
    async def complete(self, messages, tools=None, **kw) -> Response: ...
    async def embed(self, texts) -> list[Vector]: ...

class ModelRouter:
    """Tier-based routing: pick the cheapest model that satisfies the call's
    'tier' (reasoning, classify, summarize, embed, verify)."""
```

Three configured profiles, swappable via `ASIL_LLM_PROFILE`:

| Profile | Reasoning | Classify/Summarize | Verify | Embedding | Approx $/M tokens |
|---|---|---|---|---|---|
| `tight` (default during build) | DeepSeek V4 | Qwen-Coder | DeepSeek V4 | BGE-large (self-hosted) | ~$1–3 |
| `balanced` | Claude Sonnet 4.6 | DeepSeek V4 | Sonnet 4.6 | Voyage-3-code | ~$10–20 |
| `generous` (hero demo / prod) | Claude Opus 4.7 | Sonnet 4.6 | Opus 4.7 | Voyage-3-code | ~$50–100 |

**Cost guard:** every LLM call records token + $ cost to Postgres. Grafana panel shows daily spend. If `ASIL_DAILY_BUDGET_USD` is exceeded, router auto-downgrades the profile until reset. Run nightly batch jobs always on `tight`.

**Every prompt site is tier-tagged**: `router.call(tier="reasoning", ...)`, never `claude.messages.create(model="opus-4-7", ...)`. Get this right in Phase 0 or pay for it forever.

---

## Project structure

```
ASIL/
├── README.md
├── pyproject.toml                 # uv-managed monorepo
├── docker-compose.yml             # neo4j, qdrant, postgres, redis, loki, prom, grafana
├── .env.example
├── Makefile                       # bootstrap / up / down / test / lint / seed
│
├── apps/
│   ├── api/                       # FastAPI gateway
│   │   ├── main.py
│   │   ├── routes/
│   │   └── mcp_server.py          # exposes ASIL tools over MCP
│   ├── cli/                       # primary UX for months 1–5
│   │   └── asil.py                # `asil ingest`, `asil ask`, `asil replay`, ...
│   ├── worker/                    # ingestion jobs (Arq)
│   └── web/                       # Next.js dashboard — deferred until engine ships
│
├── packages/
│   ├── asil_core/                 # shared types, config, LLM router, Confidence dataclass
│   │   ├── llm.py
│   │   ├── confidence.py
│   │   └── config.py
│   ├── asil_ingest/               # cloners, parsers, indexers
│   │   ├── treesitter_parser.py
│   │   ├── scip_indexer.py
│   │   └── graph_builder.py
│   ├── asil_memory/               # vector + graph + episodic
│   │   ├── vector_store.py
│   │   ├── graph_store.py
│   │   ├── episodic.py
│   │   └── hybrid_retriever.py
│   ├── asil_temporal/             # THE MOAT
│   │   ├── event_ingestor.py
│   │   ├── causal_linker.py       # temporal proximity, correlation scoring
│   │   ├── time_window.py
│   │   └── causality_query.py
│   ├── asil_replay/               # incident reconstruction
│   │   ├── timeline.py
│   │   ├── cascade.py
│   │   └── state_diff.py
│   ├── asil_drift/                # architecture drift detection
│   │   ├── baseline.py
│   │   └── detector.py
│   ├── asil_reasoning/            # the deterministic pipeline
│   │   ├── pipeline.py            # Retrieve→Graph→Temporal→Reason→Verify→Score
│   │   ├── verifier.py
│   │   └── scorer.py
│   ├── asil_infra/                # K8s, Prometheus, Loki adapters
│   │   ├── k8s_adapter.py
│   │   ├── loki_adapter.py
│   │   └── prom_adapter.py
│   └── asil_eval/                 # benchmarks + harness
│
├── infrastructure/
│   ├── docker/
│   ├── k8s/                       # helm charts — Phase 4+
│   └── terraform/                 # Phase 5+
│
├── research/                      # papers, design docs, eval reports
│   ├── papers.md
│   ├── design-decisions.md
│   ├── benchmarks.md
│   └── postmortems/               # public incidents used for eval
│
├── scripts/
│   ├── bootstrap.sh
│   ├── seed_demo_repo.py
│   ├── seed_demo_incident.py      # replay a real public postmortem into the graph
│   └── reset_dbs.sh
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/                       # full incident-replay end-to-end
```

Monorepo via `uv` workspaces. `apps/*` are deployables; `packages/asil_*` are libraries.

---

## Phased roadmap (solo, 12+ months)

Each phase ends with a **demoable artifact** and a written design doc in `research/`. **No UI work until Phase 6 or later** — CLI is the UX. Strict gates.

| Phase | Solo duration | Cumulative | Status |
|---|---|---|---|
| 0 — Foundation | 2 weeks | M1 | ✅ DONE 2026-05-20 |
| 1 — Repo Intelligence (structural) | 6 weeks | M2 | ✅ DONE 2026-05-23 (Python + JS/TS/TSX; Go + diff-aware re-index deferred to Phase 1.x polish) |
| 2 — Memory + Confidence Scoring | 4 weeks | M3 | ✅ DONE 2026-05-24 (Verifier, canonical Scorer, EpisodicStore, memory MCP tools) |
| 3 — Infra Bridge (event ingestion) | 6 weeks | M5 | ✅ DONE 2026-05-25 (asil_infra models, postmortem ingestor, InfraAdapter protocol, FileAdapter, K8s/Prom/Loki stubs). |
| 4 — **Temporal Causality Engine** | 8 weeks | M7 | ✅ DONE 2026-05-25 (THE MOAT: temporal-proximity + lagged-correlation + explicit-reference; 3-strategy composable causal linker; `asil.find_causes` MCP tool). |
| 5 — **Execution Replay + Hero Demo** | 8 weeks | M9 | ✅ DONE 2026-05-25 (ReplayEngine, state diff, `asil replay <id>` with 6 panels, `asil.replay_incident` MCP tool). |
| 6 — Architecture Drift Detection | 6 weeks | M10 | ✅ DONE 2026-05-25 (BaselineLearner, DriftDetector, boundary rules, `asil drift baseline/report`, `asil.drift_check` MCP tool). |
| 7 (stretch) — Minimal UI + MCP polish | 6 weeks | M11–12 | ⬜ |
| 8 (stretch) — Deterministic fix pipeline (PRs) | open | post-launch | ⬜ |
| Buffer / launch / writeup | rolling | M12 | — |

Phases 4 and 5 are the moat. Everything before them is necessary plumbing; everything after is upside. Solo timelines below assume 15–20 hrs/week sustained.

### Phase 0 — Foundation (Weeks 1–2) ✅ DONE 2026-05-20

**Goal:** dev environment that someone else can `git clone && make bootstrap` and have running locally.

- [x] `pyproject.toml` with `uv` workspaces (root as virtual workspace coordinator).
- [x] `docker-compose.yml`: Neo4j, Qdrant, Postgres, Redis, Loki, Prometheus, Grafana.
- [x] `Makefile`: `bootstrap`, `up`, `down`, `lint`, `format` (with `ruff --fix`), `test`, `seed`.
- [x] FastAPI skeleton with `/health`, `/llm/ping`, `/mcp/info`, `/mcp/tools`, structured logging.
- [x] **`packages/asil_core/llm/`** — LLMProvider + EmbeddingProvider protocols, ModelRouter with tier-routed dispatch, InMemoryCostLedger, budget-guard fallback, three profiles (tight / balanced / generous), `tight` auto-falls-back DeepSeek → OpenAI gpt-4o-mini → Mock.
- [x] **`asil_core.Confidence` dataclass** with score / evidence_count / retrieval_strength / causal_confidence / derivation.
- [x] **`apps/cli/asil_cli/main.py`** — Typer CLI: `asil status`, `asil llm ping`, `asil llm profile`.
- [x] CI: GitHub Actions running ruff + format check + mypy (continue-on-error) + pytest.
- [x] `.env.example` with Anthropic, OpenAI, Voyage, DeepSeek, GitHub PAT keys and `ASIL_LLM_PROFILE=tight` default + `ASIL_DAILY_BUDGET_USD` guard.
- [x] MCP server skeleton in `apps/api/asil_api/mcp_server.py` (tools list empty in Phase 0).
- [x] 10 unit tests (Confidence validation + ModelRouter dispatch / ledger / budget downgrade / embed).
- [x] Claude Code project architecture: [CLAUDE.md](CLAUDE.md), [.claude/settings.json](.claude/settings.json), four skills (`asil-llm-call`, `asil-confidence`, `asil-positioning`, `asil-phase-gate`), two slash commands (`/phase`, `/check-tier`).
- [x] [docs/phase-0-testing.md](docs/phase-0-testing.md) — step-by-step local validation guide.

**Demo (passed 2026-05-20):** `make up` brings up 7 services, `curl /health` returns `status: "ok"` with all backends reachable, `curl /llm/ping` returns a real `gpt-4o-mini` response with cost `~$7e-06` logged via the cost ledger, `uvicorn` boots and serves `/docs`.

**Known deferrals (intentional, not blockers):**
- No `/metrics` Prometheus exporter on the FastAPI app yet → Prometheus polls and gets 404s every 15s. Harmless. Add `prometheus-fastapi-instrumentator` in Phase 1 (or sooner if the noise bothers you).
- mypy is on `continue-on-error: true` in CI — tightens in Phase 1.
- No design doc in `research/` yet (Phase 0 demo is itself simple enough that PLAN.md + the testing guide cover it).

### Phase 1 — Repo Intelligence / Structural Graph (Weeks 3–8) ✅ DONE 2026-05-23

**Goal:** point ASIL at a real GitHub repo, get answerable questions about its static architecture.

**Substep status:**

- [x] **1.1 Tree-sitter parser (Python)** — `packages/asil_ingest/asil_ingest/treesitter_parser.py`. Permissive parsing, qualified names computed inside the parser, errors recorded not raised.
- [x] **1.2 Repo cloner + `asil ingest <spec>`** — shallow clone for remote, walk with ignore list, parse-only stats. Demoed on `tiangolo/fastapi` (1118 files, 4294 functions).
- [x] **1.3 Neo4j graph builder** — Repo/File/Function/Class/Symbol nodes + CONTAINS edges, calls/imports kept as JSON for Phase 1.6 resolution. `asil graph stats / clear / neighbors / query`.
- [x] **1.4 Qdrant embeddings + semantic search** — function-level chunks via ModelRouter.embed; `asil ingest --embed`, `asil vector stats / search / clear`. Chunk identity == graph node identity.
- [x] **1.5 Hybrid retriever + `asil ask`** — vector top-K → graph expand 1 hop → dedupe → rank. Every answer carries a Confidence object. System prompt enforces file:line citation on every claim.
- [x] **1.6 Lightweight call-edge resolver** — promotes `calls_json` text refs to real `:CALLS` edges via 5 heuristics (exact, self_method, same_module, import_alias, import_member). Auto-runs after ingest; standalone `asil graph resolve-calls`. **Full SCIP integration deferred** — current resolver covers ~14% of all call sites in the ASIL repo (215/1510); the remaining 86% are stdlib/3rd-party calls our index doesn't contain, which is the expected regime. SCIP becomes useful when we hit cross-language polyglot repos in Phase 4.
- [x] **1.9 MCP tool surface** — `apps/api/asil_api/mcp_tools.py` ships 6 tools (`asil.search_code`, `asil.get_callers`, `asil.get_dependencies`, `asil.who_owns`, `asil.commit_history`, `asil.ask`) over HTTP at `POST /mcp/call/{tool}`. JSON Schemas at `GET /mcp/tools`. Native stdio MCP transport deferred to Phase 7 polish.
- [x] **1.10 Eval harness** — `packages/asil_eval/` with a 10-pair hand-curated Q&A corpus (`asil_self`) + `asil eval recall` CLI. Phase 1 baseline: recall@1 = 60%, recall@5 = 80%. Note: misses the 80% recall@3 bar from the original plan; the gap is exactly what re-ranking + Phase 2's verifier are designed to close. Documenting the baseline honestly rather than tuning the corpus to hide it.

**Phase 1.x polish (deferred — not blocking Phase 2):**

- [x] **1.8 JS/TS/TSX parsers** — per-language dispatch in `TreeSitterParser`, shared `_parse_js_family` extractor, module-name convention (repo-relative with `/` → `.`), 20 unit tests. Validated on 50-file React Native repo (149 functions, 891 call sites).
- [ ] **1.7 Incremental re-index** — `git fetch` + diff-aware re-parse of only changed files + prune removed files from graph/vectors. Today re-ingest is a full re-scan; cheap on small repos, painful on large ones.
- [ ] **1.8b Go parser** — the parser uses a node shim that lifts trivially to other grammars. Worth doing carefully as a focused commit.
- [ ] **1.6 Full SCIP** — promote the remaining 86% of call sites by running `scip-python` / `scip-typescript` and ingesting the protobuf. Becomes important for cross-file symbol resolution in big repos.

**Demo (passed 2026-05-23):** `asil ingest .` on the ASIL repo itself: 43 files, 289 functions, 63 classes, 352 vector writes, 215 resolved call edges. `asil ask "How does the LLM router pick a provider for a given tier?"` returns `ModelRouter._provider` + `ModelRouter.call` with correct file:line citations and a 0.586 confidence score. End-to-end cost ~$0.0005 per query on `tight` profile.

**Critical files:**
- [packages/asil_ingest/asil_ingest/treesitter_parser.py](packages/asil_ingest/asil_ingest/treesitter_parser.py) — entry point for all code understanding.
- [packages/asil_ingest/asil_ingest/graph_builder.py](packages/asil_ingest/asil_ingest/graph_builder.py) — schema defining downstream contracts.
- [packages/asil_ingest/asil_ingest/embedder.py](packages/asil_ingest/asil_ingest/embedder.py) — AST-aligned chunking.
- [packages/asil_ingest/asil_ingest/call_resolver.py](packages/asil_ingest/asil_ingest/call_resolver.py) — `:CALLS` edge promotion.
- [packages/asil_memory/asil_memory/hybrid_retriever.py](packages/asil_memory/asil_memory/hybrid_retriever.py) — unified read path.
- [apps/api/asil_api/mcp_tools.py](apps/api/asil_api/mcp_tools.py) — public tool surface.
- [packages/asil_eval/asil_eval/recall.py](packages/asil_eval/asil_eval/recall.py) — regression harness.
- [docs/phase-1-testing.md](docs/phase-1-testing.md) — end-to-end validation guide.

### Phase 2 — Persistent Memory + Confidence Scoring (Weeks 9–12)

**Goal:** ASIL remembers across sessions, and every answer ships with a confidence score + evidence list.

- **Episodic store** via Mem0.
- **Memory write path**: every conclusion is stored with full provenance (which graph nodes / chunks it relied on, plus the LLM trace).
- **Confidence scorer** (`packages/asil_reasoning/scorer.py`): computes the `Confidence` dataclass from retrieval similarity, evidence count, and (later) causal strength.
- **Verifier pass** (`packages/asil_reasoning/verifier.py`): second LLM call that checks every claim in the answer against the cited evidence; flags unsupported claims and downgrades confidence accordingly.
- **CLI**: every `asil ask` answer now ends with a confidence block showing score + derivation list.
- **MCP tools**: `asil.remember`, `asil.recall`, `asil.forget`.

**Demo:** ask the same question on day 1 and day 7; the answer reflects the new state, references the prior conclusion, and shows confidence shifting as evidence accumulates.

### Phase 3 — Infra Bridge / Event Ingestion (Weeks 13–18)

**Goal:** the graph now contains *runtime* events, not just static code. **This is the data foundation for the moat.**

- **K8s adapter** (`packages/asil_infra/k8s_adapter.py`): poll cluster state → ingest `Deployment`, `Service`, `Pod`, `ConfigMap` nodes with edges to the code services they run.
- **Prometheus adapter**: scrape key metrics per service; on change-point detection emit `MetricShift` nodes (don't store raw points — too noisy).
- **Loki adapter**: stream logs, extract error signatures (`LogSignature` nodes), link to service nodes.
- **Event normalization**: every ingested observation becomes a node with `timestamp`, `source`, `confidence`.
- **Local cluster**: kind/k3d; seed with a 4-service demo app that you can intentionally break.
- **Real postmortem ingestion**: pick 3–5 public postmortems (Kubernetes incidents, public SaaS RCAs) and write ingest scripts that reconstruct them as event sequences in the graph. These become the eval corpus.

**Demo:** `asil events --service payments --since "1h ago"` shows a time-ordered list of deployments, metric shifts, and log signatures linked to the payments service.

### Phase 4 — Temporal Causality Engine (Weeks 19–26) — THE MOAT

**Goal:** the graph doesn't just *contain* events; it *reasons* about which caused which.

- **Causal linker** (`packages/asil_temporal/causal_linker.py`):
  - Temporal proximity: events within ±N minutes of an incident get candidate `PRECEDED` edges with `delta_seconds` and a decaying-with-distance confidence.
  - Co-occurrence: deployments that consistently precede the same metric shifts get reinforced edges.
  - Explicit reference: a commit message mentioning an incident ID creates a high-confidence edge.
- **Correlation scoring**: for paired time series (e.g., deployment timestamps vs latency series), compute lagged Pearson / cross-correlation and store as edge property.
- **Causality query API**: `causality.find_causes(incident_id, lookback="6h")` returns ranked candidate causes with derivations.
- **Time-windowed graph queries** (`packages/asil_temporal/time_window.py`): "as of timestamp T" — every Cypher query can be temporally scoped.
- **CLI**: `asil ask --temporal "what changed before the payments latency spike at 14:23?"`.
- **Eval harness** (`packages/asil_eval`): on the 3–5 ingested postmortems, measure recall@5 of the correct root cause.

**Demo:** trigger a controlled failure in the kind cluster (e.g., bad ConfigMap rollout); `asil ask "why is the cart service erroring?"` returns the ConfigChange node as the top cause with ~70%+ confidence and a chain of evidence.

This phase is the project. Everything before it is enabling; everything after is application.

### Phase 5 — Execution Replay Engine + Hero Demo (Weeks 27–34)

**Goal:** "time-travel debugging for distributed systems." Ship the v1 hero demo.

- **Timeline builder** (`packages/asil_replay/timeline.py`): given an incident, materialize a time-ordered, dependency-aware timeline of all related events.
- **Cascade reconstruction** (`packages/asil_replay/cascade.py`): traverse `CASCADED_TO` and `DEPENDS_ON` edges to show how a failure propagated.
- **State diff** (`packages/asil_replay/state_diff.py`): "what did the architecture look like before and after?" — uses time-windowed graph queries from Phase 4.
- **Reasoning pipeline** (`packages/asil_reasoning/pipeline.py`): the full 8-step deterministic pipeline (Retrieve → Graph expand → Temporal correlate → Causal score → Reason → Verify → Score → Respond), implemented as a LangGraph state machine.
- **CLI**: `asil replay <incident-id>` produces a rich terminal report (timeline, causal chain, confidence, evidence).
- **Recording**: capture a polished demo video on `generous` profile for the public devlog.

**Demo (hero):** "Why did the payment service fail after the auth deployment at 14:17?" → reconstructed timeline, probable root cause, 78% confidence, evidence list, cascade visualization, before/after architecture diff. This is the moment the project is publishable.

### Phase 6 — Architecture Drift Detection (Weeks 35–40)

**Goal:** ASIL learns what the architecture *should* look like and flags drift.

- **Baseline learner** (`packages/asil_drift/baseline.py`): from N weeks of structural-graph history, learn expected dependencies, expected boundaries, expected ownership patterns.
- **Drift detector** (`packages/asil_drift/detector.py`): on each ingestion, compare current graph to baseline; emit `DriftEvent` nodes for new undocumented dependencies, boundary violations, anti-pattern growth.
- **CLI**: `asil drift report` shows current deviations with severity.
- **MCP**: `asil.drift_check(service)` for coding agents to query before merging changes.

**Demo:** introduce a boundary-violating import in a PR against the demo repo; `asil drift report` flags it with rationale ("auth service now depends on payment internals; this edge was absent in 8 of 8 prior weeks").

### Phase 1.8b — Multi-language parser expansion ✅ DONE 2026-05-26

Adds Tree-sitter parsers for Go, Ruby, Java, Rust, C, C++, PHP, Swift,
Kotlin via a single configurable extractor (`_GENERIC_LANG_CONFIG` in
[treesitter_parser.py](packages/asil_ingest/asil_ingest/treesitter_parser.py)).
Python and the JS family keep their bespoke extractors; everything else
uses the generic one. 11 new unit tests pin behaviour per language.
Acknowledged gaps: no docstring / decorator extraction in the generic
path, no nested-local-function recursion.

### Phase 3 step 3 — Live infrastructure adapters ✅ DONE 2026-05-26

The three Phase 3 stubs (Prometheus / Loki / K8s) are now real:

- **PrometheusAdapter**: polls (service, metric, promql) probes against
  any Prometheus endpoint; emits `MetricShift` whenever current / baseline
  ratio crosses `shift_threshold` (default 1.5x). Health-probes
  `/-/ready` so unreachable endpoints surface as `NotConfiguredError`.
- **LokiAdapter**: queries `/loki/api/v1/query_range`, redacts UUIDs /
  hex / numbers / ISO timestamps so the same root error message
  clusters into one `LogSignature`, caps signature length at 200 chars.
- **K8sAdapter**: walks `list_namespaced_service` + `list_namespaced_deployment`
  via `kubernetes-asyncio`. Requires a kubeconfig; falls back to in-cluster
  config when none is provided.

CLI: `asil adapters {prometheus, loki, k8s}` with `--write` to MERGE
results into Neo4j. 10 unit tests (HTTP mocked) + 2 integration tests
that round-trip against the docker-compose stack.

### Phase 7.5 — External-system adapters (PR / Slack / Tickets) ✅ DONE 2026-05-26

Brings the systems engineers actually live in into ASIL's graph:

- **GitHubAdapter**: zero-token by default — uses `gh` CLI when
  authenticated, falls back to `git log --merges` so any local repo
  works. Repo key inferred from origin remote.
- **SlackAdapter**: polls `conversations.history`, extracts incident
  IDs and service mentions from message text. Token-gated on
  `SLACK_BOT_TOKEN`.
- **JiraAdapter**: polls Jira REST v3 for updated tickets; flattens
  ADF descriptions for incident-id extraction. Token-gated on
  `JIRA_BASE_URL` / `JIRA_USER_EMAIL` / `JIRA_API_TOKEN`.
- **LinearAdapter**: single GraphQL query per poll. Token-gated on
  `LINEAR_API_KEY`.

New graph types: `PullRequest`, `ChatMessage`, `Ticket`. Five new edges:
`:AUTHORED_BY`, `:MERGES`, `:DISCUSSES`, `:MENTIONS`, `:LINKS_TO`,
`:ASSIGNED_TO`. CLI: `asil external {github, slack, jira, linear}`. 12
new unit tests.

### Phase 7.6 — Postgres cost ledger + savings dashboard ✅ DONE 2026-05-26

Replaces the in-memory cost ledger so spend history survives the
lifetime of the data (not just one Python process). New
`PostgresCostLedger` (`asil_costs` table), `from_settings_or_none()`
helper that auto-wires from `ModelRouter.from_env()` with a transparent
fall-back to in-memory when Postgres isn't reachable. New
`savings_vs_no_memory(memory_count, fresh, cached)` method estimates
total $ saved by episodic-memory recalls. CLI: `asil cost summary` +
`asil cost daily`. API: `GET /dashboard/cost`. Dashboard page: `/cost`.

### Phase 7 — Minimal UI + MCP polish ✅ DONE 2026-05-25

- [x] **7.1 Next.js dashboard** — `apps/web/` is a Next.js 15 + Tailwind 3 + ReactFlow app served on port 3001. Eight pages: Dashboard / Ask / Incidents / Incident replay (with ReactFlow causal graph) / Causality / Drift / Memory / MCP catalog / Health. Type-safe API client in `src/lib/api.ts`, shared components (`Card`, `StatTile`, `ConfidenceBar`, `Sidebar`, `CausalFlow`).
- [x] **7.2 CORS + UI-facing REST endpoints** — FastAPI now exposes `/dashboard/stats` (aggregated counts for code + runtime + repos + envs + memory + LLM profile) and `/incidents` (every Incident node, newest first, with affected services). CORS allows `http://localhost:3001`.
- [x] **7.3 MCP catalog page** — `/mcp` page hits `GET /mcp/tools` and renders the 12 tools with collapsible JSON-schema previews and a Claude Code wiring snippet. Native stdio MCP transport still pending.
- [ ] **7.4 Hosted public demo** — one polished postmortem replay at a public URL. Pending hosting decisions.

**Demo:** `make api-dev` + `make web-dev` → open `http://localhost:3001` → dashboard shows live counts from Neo4j + Qdrant + Postgres; `/ask` runs the full hybrid-retrieval + verifier pipeline; `/incidents/<id>` renders the timeline + ReactFlow causal chain + state diff; `/causality` lets you trigger `asil.find_causes` interactively; `/drift` runs `asil.drift_check` on any indexed repo.

### Phase 8 — Deterministic fix pipeline ✅ DONE 2026-05-26

The constrained autonomous coder. Not free-form code generation — the
patch generator only runs when there is a Phase-5 causal chain to act
on, and the LLM is given a narrow slice of context (incident summary +
top causes + implicated files) and an explicit instruction to emit a
minimal `git apply`-compatible unified diff.

What ships in `packages/asil_fix/`:

- **PatchGenerator** ([patch_generator.py](packages/asil_fix/asil_fix/patch_generator.py)):
  loads a `ReplayResult` for the incident; gathers code context from the
  cause props (`file_path`, `service_name -> Service.file_paths`);
  prompts via `ModelRouter.call(tier="reasoning")`; parses the diff out
  of either a fenced block or bare unified-diff text; computes an
  aggregate confidence bounded by the *weakest* component (cause-vs-
  symptom honesty — a strong replay built on a weak cause stays weak).
- **LocalSandbox** ([sandbox.py](packages/asil_fix/asil_fix/sandbox.py)):
  copies the repo to a `tempfile.TemporaryDirectory`, runs `git apply
  --check` before the real apply, then executes a configurable test
  command with a wall-clock timeout. Returns one of `not_run`,
  `apply_failed`, `tests_passed`, `tests_failed`, `timeout`,
  `sandbox_error`. Never raises — sandboxes are the boundary between
  "LLM said something" and "we did something."
- **NoOpSandbox**: returns `not_run` cleanly so `asil fix propose` can
  still flow through the audit path.
- **AuditLog** ([audit.py](packages/asil_fix/asil_fix/audit.py)):
  Postgres-backed `asil_fix_audit` table — one wide row per proposal
  with the diff, causal chain, sandbox stdout/stderr tail, model + cost,
  and an aggregate `FixOutcome` (`proposed` / `accepted` / `rejected` /
  `inconclusive`). Two-gate classifier: tests must pass *and* confidence
  must be above the configured floor for `accepted`.

CLI surface (`asil fix`):

- `propose <incident_id>` — read-only; shows the diff + confidence
  breakdown + derivation.
- `run <incident_id>` — full propose → sandbox → audit pipeline.
- `list [--incident-id ID]` — recent audit rows.

MCP tool: `asil.propose_fix` (read-only by default; opt-in to record).

17 new unit tests in [tests/unit/test_fix_pipeline.py](tests/unit/test_fix_pipeline.py)
pin the diff extractor (fenced + bare formats), the confidence-min
aggregation, the generator's "no replay" / "no causal chain" guard
branches, an end-to-end happy path against a real `tmp_path` repo, the
oversize-file truncation, the four SandboxOutcome branches against a
real git repo + `git apply` round-trip, and the audit-log `FixOutcome`
classifier truth table.

Nothing in this phase pushes, merges, or notifies. The proposal + sandbox
result is the artifact a human (or a future orchestrator) decides on.

### Phase 7 — Minimal UI + MCP polish ✅ DONE 2026-05-25

---

## Research reading list (parallel with phases)

**Required before Phase 2:**
- MemGPT / Letta — virtual context paging.
- Codebase-Memory (2026 ArXiv) — Tree-sitter knowledge graphs.
- Generative Agents (Park et al.) — episodic memory architecture.

**Required before Phase 4 (most important set):**
- Causal inference textbook chapters: Pearl (Causality), Hernán & Robins (What If) — focus on causal graphs, lagged correlation, confounders.
- Change-point detection papers (PELT, BOCPD) — for `MetricShift` boundaries.
- AIOps papers on incident root-cause analysis (e.g., GrayHat, MicroRCA, CausalRCA).

**Required before Phase 5:**
- KubeIntellect paper — supervisor + domain-aligned K8s agents.
- A public postmortem corpus (danluu/post-mortems, k8s.io issues, GitLab/Cloudflare/AWS public RCAs). Pick 3–5 to ingest as eval data.

**Required before Phase 8 (stretch only):**
- SWE-agent + SWE-bench Verified — execution + critique loops.
- Sandbox isolation tradeoffs (gVisor / Firecracker / DinD).

Write notes into `research/papers.md` as you go. This doc becomes part of the project's defensibility — and likely the basis for a workshop paper.

---

## Evaluation & verification

The project is **only credible if it's measured.** Build the harness in [packages/asil_eval/](packages/asil_eval/) starting Phase 1.

| Phase | Eval | Pass bar |
|---|---|---|
| 1 | Code-search accuracy on 50 hand-labeled Q&A pairs | top-3 recall ≥ 80% |
| 2 | Confidence calibration: high-confidence answers right ≥ 90% of the time | ECE < 0.1 |
| 3 | Event-ingestion completeness on 3 ingested postmortems | ≥ 95% events captured |
| 4 | Causal recall@5 on the postmortem corpus | ≥ 60% correct cause in top 5 |
| 5 | End-to-end hero query on 5 held-out incidents | ≥ 50% correct root cause with ≥ 60% confidence |
| 6 | Drift detection precision on synthetic boundary violations | precision ≥ 80% at recall ≥ 70% |

End-to-end verification each phase:
- `make e2e` runs the full pipeline against the seeded demo repo + a recorded incident.
- Grafana dashboard shows tokens/query, $/query, latency, accuracy over time — ASIL is observable about itself.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Scope creep — easy to start "improving" agents | Phase gates with demoable artifacts; UI work explicitly banned until Phase 7. |
| Drift toward "coding assistant" positioning | Every devlog leads with temporal causality / replay, not PR-filing. PR work is Phase 8 stretch. |
| LLM cost spirals | `tight` profile default; cost guard auto-downgrades; nightly batches always tight. |
| Graph schema churn breaking downstream | Freeze v1 schema after Phase 1 demo; versioned migrations (Alembic-style) thereafter. |
| Causal linker emits garbage | Eval harness from Phase 1; never ship a causal claim without confidence + derivation. |
| Single dev can't ship all 8 phases | Phase 0–5 alone is publishable + resume-defining. Phase 6 is the polish. Phase 7–8 are upside. |
| Real K8s clusters cost money | kind/k3d locally; cloud only for one polished demo. |
| Build-in-public exposes bad early demos | Devlog cadence is weekly, not daily; only post demos that pass their phase's eval bar. |

---

## What to do this week (Phase 0, Week 1)

1. Create the project skeleton inside `/Users/raksithlochabb/Documents/GitHub/ASIL` (`uv init`, monorepo layout above, `git init`, public GitHub repo).
2. Stand up `docker-compose.yml` with Neo4j + Qdrant + Postgres + Redis. Verify all reachable via `make up` + `/health`.
3. **Build `packages/asil_core/llm.py` first** — LLMProvider protocol, ModelRouter, cost recorder, `Confidence` dataclass. Register DeepSeek V4 + BGE-large as the `tight` profile.
4. Skeleton `apps/cli/asil.py` with Typer; expose `asil status`, `asil llm ping`.
5. Open `research/papers.md`; read this week: **MemGPT, Codebase-Memory (2026), Generative Agents, the Pearl causality intro chapter.** Take structured notes.
6. Pick the demo repo for Phase 1 (recommendation: `litestar-org/litestar` or `tiangolo/fastapi` — mid-sized Python OSS you already understand).
7. Pick the postmortem corpus for Phases 3–5 (recommendation: start scanning danluu/post-mortems, GitLab incident postmortems, Cloudflare public RCAs — shortlist 5 candidates).
8. Start the public devlog. Frame: *"Building engineering intelligence infrastructure — the persistent, temporal, causal layer underneath coding agents."* Never lead with "AI OS" or "autonomous coder."

---

## Confirmed constraints

- **Timeline:** 12+ months, startup-track. Phase 0–6 in scope; 7–8 are upside.
- **Team:** solo. Buffer time and phase gates are mandatory.
- **Budget:** tight during build (`tight` profile by default); generous is a one-line config flip for the hero demo and benchmark runs.
- **MVP cut:** Phase 5 hero demo (incident root cause with timeline + confidence + evidence) is the v1 ship target. PR-filing autonomy is explicitly Phase 8, *stretch only*. Multi-agent debate is explicitly out of scope — deterministic pipelines win.
- **Positioning:** Engineering Intelligence Infrastructure. Not AI OS. Not autonomous coder. Not chatbot.
