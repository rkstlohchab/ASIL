# Why ASIL — what it is, what it does, why nothing else does this

> A persistent, temporal, causal understanding of how a software system evolves, behaves, and fails — exposed to coding agents via MCP.

This is the long-form companion to [README.md](../README.md) and [docs/asil-in-five-minutes.md](asil-in-five-minutes.md). Read it once to understand what ASIL is for, what it actually does today, and why no other tool in the 2026 landscape occupies the same slot.

---

## 1. The problem ASIL solves

Modern software is built and operated by a fleet of AI coding agents — Cursor, Claude Code, OpenHands, Aider, Continue, Cody, Copilot. They write code, file PRs, run tests, even merge. They share three blind spots:

1. **They have no memory across sessions.** Ask Claude "how does auth work?" today, then again next Tuesday in a new chat — it re-reads the entire codebase from scratch, calls the LLM again, pays the cost again. There is no shared institutional memory.
2. **They understand code statically, not temporally.** They can show you *what* the code does today. They cannot tell you *what changed between Tuesday 14:17 and the incident at 14:25*. They have no model of "this deployment preceded this metric shift preceded this incident."
3. **They cannot reason about why systems fail.** When production breaks, a human reads dashboards, postmortems, deploys, and logs, then assembles a causal chain. Coding agents have zero access to that runtime substrate — and even if they did, they would emit *plausible-sounding* causes (LLM hallucination) instead of *observable* ones.

The result: every AI tool in the coding space is a slightly better autocomplete. None of them sit on a persistent, queryable, evidence-weighted model of *how the system actually evolves and behaves over time*.

**That layer is what ASIL is.**

---

## 2. What ASIL is (one sentence)

**Engineering Intelligence Infrastructure** — the persistent, temporal, causal knowledge graph that sits *underneath* coding agents and answers the questions they cannot.

Three things ASIL is **not**:

- ❌ Not a coding assistant. It does not write code or file PRs (Phase 8 stretch).
- ❌ Not an "AI OS" or autonomous coder. Those are crowded commodity spaces.
- ❌ Not a chatbot. It is a knowledge layer with CLI + REST + MCP + (now) dashboard surfaces.

---

## 3. The hero query

Everything ASIL does serves one question:

> **"Why did this production incident happen?"**

The answer ships with:

- A reconstructed timeline (events, deploys, config changes, metric shifts, traffic spikes).
- A probable root cause with a **confidence score**.
- An evidence list (which deploy, which commit, which metric, with timestamps).
- A causal chain visualisation (which change propagated to which service via which dependency edge).
- An architecture-drift report (did this incident violate previously-stable boundaries?).

That single capability requires every layer of the system — graph + vector + temporal + causal + memory + verification — to actually work. Shipping it is what made ASIL publishable.

---

## 4. The four defensible pillars

These four properties are what make ASIL hard to copy:

| # | Pillar | What it means | Where it lives |
|---|---|---|---|
| 1 | **Temporal causality** | The graph tracks events and their relationships *over time*, not just static structure. `(:Deployment)-[:PRECEDED {confidence}]->(:Incident)` is a real edge type with real semantics. | `packages/asil_temporal/` |
| 2 | **Execution replay** | Given an incident, reconstruct what happened across services as a time-ordered, dependency-aware causal chain. "Time-travel debugging for distributed systems." | `packages/asil_replay/` |
| 3 | **Confidence-scored reasoning** | Every conclusion ships with a `Confidence` dataclass: score, evidence count, retrieval strength, causal strength, derivation list. No hallucinations dressed up as facts. | `packages/asil_reasoning/`, surfaced in every API response. |
| 4 | **Architecture drift detection** | Learn the *expected* dependency graph; flag undocumented coupling, decay, and anti-pattern growth as concrete `DriftEvent` nodes. | `packages/asil_drift/` |

---

## 5. How ASIL is built (phase by phase)

The codebase is organised as a Python monorepo under `uv` workspaces. Each phase ships a demoable artifact and a design doc. Status as of 2026-05-25:

| Phase | What | Status |
|---|---|---|
| 0 | Foundation — Docker stack (Neo4j + Qdrant + Postgres + Redis + Loki + Prom + Grafana), `ModelRouter` tier abstraction, `Confidence` dataclass, Typer CLI skeleton, FastAPI skeleton, OpenTelemetry hooks | ✅ Done |
| 1 | Repo Intelligence — Tree-sitter parsers (Python / JS / TS / TSX), call resolver, Neo4j structural graph (Repo/File/Function/Class/Symbol/Commit), Qdrant function-level embeddings, hybrid retriever (vector + graph expand + re-rank), 12 MCP tools, eval harness with hand-curated Q&A corpus | ✅ Done |
| 2 | Memory + Confidence — `EpisodicStore` (Postgres source of truth + Qdrant for semantic recall), Verifier (second LLM pass checks every claim against citations), canonical Scorer | ✅ Done |
| 3 | Infra Bridge — runtime-event namespace under `env_key` (Service / Deployment / MetricShift / LogSignature / Incident), YAML postmortem ingestor, `InfraAdapter` protocol with `FileAdapter` implementation; K8s / Prom / Loki stubs ready | ✅ Done |
| 4 | **Temporal Causality Engine — THE MOAT.** `TemporalLinker` derives `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)` from three composable observable strategies: temporal proximity (exponential decay with 5-min half-life), lagged correlation (deploy touches code that the metric's service runs), explicit reference (commit/postmortem text names the incident). No LLM emits causes — they are derived from graph state, always auditable. | ✅ Done |
| 5 | Execution Replay — `ReplayEngine` assembles timeline + service cascade + state diff (deployments-during, metric deltas) per incident; CLI `asil replay <id>` and MCP `asil.replay_incident` | ✅ Done |
| 6 | Architecture Drift — `BaselineSnapshot` + `DriftDetector` emit `DriftEvent` nodes for new dependencies + boundary violations; CLI `asil drift baseline / report` | ✅ Done |
| 7 | **Minimal UI + MCP polish — DONE TODAY.** Next.js 15 dashboard (Tailwind + ReactFlow) on port 3001, talks to FastAPI gateway, 8 pages: Dashboard / Ask / Incidents / Incident replay / Causality / Drift / Memory / MCP catalog / Health. CORS-wired, type-safe API client. | ✅ Done |
| 8 | Deterministic fix pipeline — sandbox executor + patch generator constrained by the Phase 5 causal chain | ⬜ Stretch |

---

## 6. What ASIL can actually do *right now*

### Codebase questions, answered with citations and confidence

```bash
uv run asil ingest .                  # parse the current repo
uv run asil ask "How does the LLM router pick a provider for a given tier?"
```

Returns: an answer, a candidate list (file:line citations), a verifier report (each claim ✓ supported / ✗ unsupported), and a confidence breakdown. The second time you ask, the same answer is recalled from episodic memory at ~1/100th the cost.

### Incident causality, derived from observable evidence

```bash
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil temporal link prod
uv run asil temporal causes INC-2026-04-12-payments-cascade
```

Returns a ranked list of `(:Cause)-[:PRECEDED]->(:Incident)` candidates, each with:

- The cause kind (Deployment, MetricShift, ConfigChange, …)
- A confidence score (0.0–1.0)
- The exact time delta before the incident
- The strategy that produced the edge (`temporal_proximity`, `lagged_correlation`, `explicit_reference`)
- A human-readable derivation string

This is **observable causality**: every edge is derivable from graph state, no LLM gets to "decide" what caused what.

### Full incident replay

```bash
uv run asil replay INC-2026-04-12-payments-cascade
```

Renders a Rich terminal report (or, in the dashboard, an interactive ReactFlow view) with:

- The incident header
- A time-ordered event timeline
- The ranked causal chain
- The service cascade (which services took collateral damage and in what order)
- A state diff (deployments during the window, metric before/after deltas)
- A confidence card

### Architecture drift

```bash
uv run asil drift baseline local:$(pwd) --output baseline.json
# ... a week later, after some refactors ...
uv run asil drift report local:$(pwd) --baseline baseline.json
```

Flags new dependency edges and boundary violations that weren't in the baseline — *before* the PR merges.

### MCP surface for any agent

```
POST http://localhost:8000/mcp/call/<tool_name>
```

12 tools, JSON-schema'd, callable from Claude Code / Cursor / OpenHands / Aider / Cody / your own scripts:

`asil.search_code`, `asil.get_callers`, `asil.get_dependencies`, `asil.who_owns`, `asil.commit_history`, `asil.ask`, `asil.remember`, `asil.recall`, `asil.forget`, `asil.find_causes`, `asil.replay_incident`, `asil.drift_check`.

### Persistent memory across sessions (the API-call saver)

Every conclusion ASIL ever reaches is persisted to Postgres with full provenance (question, answer, citations, model, cost, confidence). The corresponding question vector goes into Qdrant. The next `asil ask` first embeds the new question (~$0.0001) and searches memory — a hit returns the cached answer instead of re-running the full pipeline (~$0.01). Across a team or across many sessions on the same codebase, the savings compound.

| Scenario | Without ASIL memory | With ASIL memory |
|---|---|---|
| Same question asked 5× across sessions | 5 × $0.01 = **$0.05** | 1 × $0.01 + 4 × $0.0001 = **$0.0104** |
| 100 questions asked twice each | 200 × $0.01 = **$2.00** | 100 × $0.01 + 100 × $0.0001 = **$1.01** |
| Team of 3 on the same codebase | 3× everything | Shared memory store — 1× cost |

---

## 7. Why nothing else does this

The 2026 AI-coding landscape is dense, but the slot ASIL occupies is genuinely empty. Here's the field:

| Class | Examples | What they do | What they don't do |
|---|---|---|---|
| Coding agents | OpenHands, Aider, Continue, Cody, Cursor, Claude Code, Copilot | Edit code, file PRs, run tests | No persistent cross-session memory of conclusions; no temporal model; no causal reasoning over runtime |
| Code-graph tools | Sourcegraph, Codebase-Memory, Glean | Static code understanding — symbol resolution, callgraphs | Static only — no runtime events, no temporal edges, no causal scoring |
| Observability platforms | Datadog, Grafana, Honeycomb, New Relic | Ingest metrics + traces + logs; humans dashboard them | No code model — they cannot tell you *which commit* caused the latency shift, only that latency shifted |
| AIOps RCA tools | MicroRCA, CausalRCA, Grayhat, vendor-internal | Statistical anomaly detection on metric time series | Detached from the code graph; their "causes" are metric-level, not commit-level; no agent-facing API |
| GraphRAG products | Neo4j GenAI, Microsoft GraphRAG | Vector + graph retrieval | Pure RAG — no temporal causality, no confidence calibration, no incident replay, no MCP surface |
| Memory products | Mem0, Letta, Zep | Episodic memory for LLM apps | Generic — not aware of code, runtime, or causality |

**ASIL is the composition no one else has shipped:** code graph + vector index + episodic memory + runtime event graph + observable causal linker + execution replay + drift detector + confidence-weighted reasoning + MCP surface, all in one product, with the explicit positioning of being the layer *under* coding agents rather than another agent.

Why hasn't someone else built this? Four reasons:

1. **It crosses too many disciplines.** Program analysis + temporal graphs + vector retrieval + causal inference + distributed-systems observability + evidence-weighted reasoning. Almost no single team has expertise across all of them — they specialise.
2. **The market gravity pulls toward "agent edits code."** That's where the funding is (OpenHands' $18.8M Series A is the canonical example). Everyone competes for the same slot, leaving the infrastructure layer unattended.
3. **LLMs make people lazy about causality.** It's tempting to ask GPT "what caused this incident?" and ship the answer. ASIL refuses that — every causal edge must be derivable from observable graph state. That discipline is annoying to build, easy to skip, and load-bearing for trust.
4. **It's the unglamorous infrastructure work.** No demo of "ASIL files a PR for you." Just: when something goes wrong, you get the truth, with evidence, fast. That's a B2B-trust pitch, not a viral-demo pitch.

---

## 8. Architecture (one diagram)

```
                ┌─────────────────────────────────────────┐
                │  Clients: Next.js dashboard (Phase 7),  │
                │  Typer CLI, any coding agent via MCP    │
                └────────────────────┬────────────────────┘
                                     │ HTTP + MCP
                ┌────────────────────▼────────────────────┐
                │       ASIL API Gateway (FastAPI)        │
                │  /health  /dashboard/stats  /incidents  │
                │  /mcp/tools  /mcp/call/{tool}           │
                └────────────────────┬────────────────────┘
                                     │
   ┌──────────────┬──────────────────┼──────────────────┬─────────────────┐
   ▼              ▼                  ▼                  ▼                 ▼
┌────────┐  ┌──────────┐    ┌─────────────────┐  ┌─────────────┐  ┌──────────────┐
│Ingest  │  │Temporal  │    │ Reasoning       │  │ Replay      │  │ Infra Bridge │
│(parser,│  │(linker,  │    │ (retriever +    │  │ (timeline + │  │ (postmortem  │
│ embed) │  │ scorer)  │    │  verifier +     │  │  cascade +  │  │  + adapters) │
│        │  │          │    │  scorer)        │  │  state diff)│  │              │
└───┬────┘  └────┬─────┘    └────────┬────────┘  └──────┬──────┘  └──────┬───────┘
    │            │                   │                  │                │
    └────────────┴───────────────────┴──────────────────┴────────────────┘
                                     │
   ┌─────────────────────────────────▼─────────────────────────────────┐
   │  Storage layer                                                     │
   │  • Neo4j     — code namespace (repo_key) + runtime namespace       │
   │                (env_key) + causal :PRECEDED edges                  │
   │  • Qdrant    — function-level embeddings + memory question vectors │
   │  • Postgres  — episodic memory (source of truth), cost ledger      │
   │  • Redis     — cache, pipeline state                               │
   │  • Loki/Prom/Grafana  — observability of ASIL itself               │
   └────────────────────────────────────────────────────────────────────┘
```

---

## 9. The hard rules that make ASIL trustworthy

These rules are enforced in `CLAUDE.md` and the `.claude/skills/` directory, and they're what keep the system from drifting into LLM-flavoured snake oil:

1. **All LLM calls go through `ModelRouter.call(tier=...)`.** No hardcoded model names. Tier-routed, cost-bounded, swappable across profiles (tight / balanced / generous).
2. **Every conclusion ships with a `Confidence` object.** Score + evidence count + retrieval strength + causal strength + derivation list. Never stripped before returning.
3. **Causality is observable, not predicted.** No LLM ever authors a `:PRECEDED` edge. Edges are derived from graph state by deterministic strategies, each writing its own `strategy` property on the edge.
4. **Deterministic pipelines over multi-agent debate.** One critique pass max. LangGraph is for state machines, not for agents arguing.
5. **Phase gates are real.** Each phase ships a demo + a design doc before the next phase starts.
6. **Code namespace and runtime namespace are isolated.** Code nodes carry `repo_key`; runtime nodes carry `env_key`. No accidental joins.
7. **MERGE, never CREATE.** Re-running any ingestor or linker is idempotent.
8. **Every edge carries provenance.** `derivation` and `strategy` props on `:PRECEDED`; full citation chain on every memory; `source` on every runtime event.

---

## 10. How to try it on your own codebase

```bash
# 1. one-time setup
make bootstrap          # uv sync + .env
make up                 # docker stack
make web-install        # pnpm install for the dashboard

# 2. start the services
make api-dev            # FastAPI on :8000   (terminal A)
make web-dev            # Next.js on :3001   (terminal B)

# 3. ingest your code
uv run asil ingest /path/to/your/project --embed

# 4. ask questions
uv run asil ask "What is the main architecture pattern in this repo?"
#   ↑ verified, cited, confidence-scored. Cached for next time.

# 5. open the dashboard
open http://localhost:3001
```

The dashboard has eight pages — Dashboard, Ask, Incidents, Causality, Drift, Memory, MCP, Health — each backed by either a `/dashboard/*` REST endpoint or an `/mcp/call/{tool}` invocation.

---

## 11. Where this goes next

- **Phase 8 (stretch)** — deterministic fix pipeline. Given the causal chain from Phase 5, generate a patch, run it in a sandbox, file a PR. Constrained by the same observable evidence as the causal claim. The point is *not* "ASIL is now another coding agent" — it's "ASIL can ship the fix it already understands, with the same audit trail."
- **Phase 3 step 3+** — live K8s / Prometheus / Loki adapters that stream into the runtime namespace continuously. The stubs are already in place under `packages/asil_infra/`; what's left is the actual cluster I/O.
- **Hosted public demo** — one polished postmortem replay running at a public URL, so reviewers can click through without setting up Docker.

But the engine is done. Everything from here is upside.
