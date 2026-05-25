# Why I built the layer **underneath** every AI coding agent

*(and how it ends up saving the same teams roughly 90% of their Claude / GPT bill)*

---

I have nothing against the AI coding agent space. Cursor, Claude Code, OpenHands, Aider, Continue, Cody, Copilot — they're all useful. They're also all racing toward the same finish line: a slightly smarter autocomplete that can occasionally file a PR.

Six months ago I started a project that deliberately runs in the opposite direction. It's called **ASIL** — Engineering Intelligence Infrastructure — and it's the layer those agents *should be sitting on top of*. The persistent, temporal, causal model of how a software system evolves, behaves, and fails.

This post is a tour of what it does, why no one else has built this exact composition, how it ends up saving a meaningful fraction of your LLM bill almost by accident, and what it looks like when you point it at your own codebase.

If you want the code first, it's open source at [github.com/rkstlohchab/ASIL](https://github.com/rkstlohchab/ASIL).

---

## The three blind spots every AI coding tool shares

I spent a year using every agent I could find. They all share three blind spots that nobody is racing to fix:

**1. No memory across sessions.** Ask Claude Code "how does authentication work in this repo?" today. Ask it the same question next Tuesday in a new chat window. It re-reads the codebase from scratch, re-runs the LLM, charges you again, gives you a slightly different answer.

**2. No model of time.** They can tell you *what* the code does today. They cannot tell you *what changed between the deployment at 14:17 and the incident alert at 14:23*. There's no temporal index. There's no concept of "this preceded that."

**3. No causality.** When prod breaks, a human reads dashboards, postmortems, deploys, logs, and assembles a causal chain. AI coding agents have zero access to that runtime substrate — and even if they did, they'd emit *plausible* causes (LLM hallucination), not *observable* ones.

These three gaps are not random. They're the same gap viewed from three angles: **there is no shared, persistent, evidence-weighted model of the system underneath the agents**. Every session is amnesia.

ASIL fills that gap. The agents become its clients.

---

## What ASIL actually is

One sentence:

> A persistent, temporal, causal knowledge graph of how your software system evolves, behaves, and fails — exposed to any AI agent via MCP, queryable from a CLI and a dashboard.

The hero query that defines v1:

> **"Why did this production incident happen?"** → reconstructed timeline, probable root cause with a confidence score, evidence list, causal chain visualisation, architecture-drift report.

It does *not* file PRs. It does *not* try to "be the agent." It tries to **be the truth layer** that the agents use.

---

## The four defensible pillars

| # | Pillar | What it means |
|---|---|---|
| 1 | **Temporal causality** | The graph tracks events and their relationships *over time*. `(:Deployment)-[:PRECEDED {confidence}]->(:Incident)` is a real edge type with real semantics. |
| 2 | **Execution replay** | Given an incident id, ASIL reconstructs what happened across services as a time-ordered, dependency-aware causal chain. Time-travel debugging for distributed systems. |
| 3 | **Confidence-scored reasoning** | Every conclusion ASIL emits ships with a `Confidence` object: score + evidence count + retrieval strength + causal strength + derivation list. Never stripped before returning to the user. |
| 4 | **Architecture drift detection** | Learn the *expected* dependency graph; flag undocumented coupling, decay, and anti-pattern growth as concrete `DriftEvent` nodes — *before* the PR merges. |

The combination is what's hard to copy. Most products do one of these. None of the popular tools do all four.

---

## What it can actually do (today, on my laptop)

Everything below works **right now** on a fresh checkout with `make up`. Receipts further down.

### 1. Ingest any codebase in 13 languages

```bash
uv run asil ingest /path/to/your/project --embed
```

ASIL parses Python, TypeScript, JavaScript, TSX, **Go, Ruby, Java, Rust, C, C++, PHP, Swift, Kotlin**, builds a Neo4j knowledge graph (Repo / File / Function / Class / Symbol / Commit / Author), and writes function-level vector embeddings to Qdrant. The whole thing is incremental and idempotent — re-running the same command updates rather than duplicates.

This is the parser registry from `_GENERIC_LANG_CONFIG`:

```
go      ✓  function_declaration, method_declaration, type_spec, import_spec
ruby    ✓  method, singleton_method, class, module, require/load
java    ✓  method_declaration, class_declaration, interface_declaration, ...
rust    ✓  function_item, struct_item, trait_item, impl_item, use_declaration
c/cpp   ✓  function_definition, struct_specifier, class_specifier, ...
php     ✓  function_definition, class_declaration, namespace_use_declaration
swift   ✓  function_declaration, class_declaration, import_declaration
kotlin  ✓  function_declaration, class_declaration, import_header
python  ✓  bespoke parser (decorators, docstrings)
js/ts   ✓  bespoke parser (arrow functions, named imports, JSX)
```

### 2. Ask questions, get cited answers with confidence

```bash
uv run asil ask "How does the LLM router pick a provider for a given tier?"
```

Behind the scenes: hybrid vector + graph retrieval, LLM synthesis, verifier pass that checks every claim against the cited evidence, confidence object assembled from retrieval strength + verifier flags + causal strength.

The output isn't an essay. It's **a structured answer + file:line citations + a per-claim ✓/✗ verifier report + a Confidence card**.

Ask the same question again 30 seconds later and ASIL recognises it from episodic memory. The second answer costs ~$0.0001 instead of ~$0.01.

That's the **money-saving fact** I'm coming back to in a minute.

### 3. Ingest incidents → derive observable causal chains

```bash
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil temporal link prod
uv run asil temporal causes INC-2026-04-12-payments-cascade
```

Three composable causal-edge strategies, each writing `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)`:

- **Temporal proximity**: exponential decay with 5-min half-life. Catches the obvious "deploy 7 minutes before incident."
- **Lagged correlation**: when a Deployment shipped code that the MetricShift's service runs, the deployment outranks the symptom. This is the cause-vs-symptom honesty fix from Phase 4 step 2.
- **Explicit reference**: a commit message or postmortem text mentioning the incident id gets a high-confidence edge with `strategy: "explicit_reference"`.

**Crucially: no LLM is ever asked "what caused this?"** The causes come from observable graph state. Every edge carries the derivation string ("Deployment deploy-8f2c1d4 on auth occurred 7.0min before the incident → confidence 0.379 (half-life 5min)"). The downstream LLM consumes the causal edges; it doesn't author them.

### 4. Replay any incident

```bash
uv run asil replay INC-2026-04-12-payments-cascade
```

Renders a Rich terminal report (or, in the dashboard, an interactive ReactFlow view): incident header → time-ordered timeline → ranked causal chain → service cascade (which services took collateral damage and in what order) → state diff (deployments during the window, metric before/after deltas) → confidence card.

### 5. Detect architecture drift

```bash
uv run asil drift baseline local:$(pwd) --output baseline.json
# ... a week, a refactor, three PRs later ...
uv run asil drift report local:$(pwd) --baseline baseline.json
```

Flags new dependency edges and boundary violations — *before* the PR merges. The dashboard's `/drift` page makes this clickable.

### 6. Bring in PRs, Slack, Jira, Linear

```bash
# Works without any tokens on any git repo — uses `gh` CLI or `git log` fallback.
uv run asil external github . --write

# Token-gated. Wire SLACK_BOT_TOKEN / JIRA_* / LINEAR_API_KEY in .env.
uv run asil external slack    --channel C-INCIDENTS --service payments --service auth --write
uv run asil external jira     --project INC --write
uv run asil external linear   --team ENG --write
```

The slack adapter extracts incident IDs ("INC-2026-04-12") and service mentions from the message body, so the graph automatically wires `(:ChatMessage)-[:DISCUSSES]->(:Incident)` and `(:ChatMessage)-[:MENTIONS]->(:Service)` edges. The Jira and Linear adapters do the same for ticket titles and descriptions.

### 7. Poll live Prometheus + Loki + Kubernetes

```bash
uv run asil adapters prometheus \
  --probe 'payments:p99_latency:histogram_quantile(0.99, ...)' \
  --probe 'auth:error_rate:sum(rate(http_5xx_total[1m]))' \
  --write
uv run asil adapters loki --service payments --service auth --write
uv run asil adapters k8s  --namespace prod --write
```

When Prometheus shows a metric ratio crossing the configured threshold (default 1.5x current vs baseline), ASIL emits a `MetricShift` event. Loki's most recent error log lines get clustered by a redacted signature (UUIDs / numbers / timestamps collapsed) and emitted as `LogSignature` nodes. K8s contributes `Deployment` + `Service` events.

These are all real adapters against real services. The docker-compose ships Prometheus + Loki + Grafana out of the box so you can play with them locally without a cluster.

### 8. Talk to it from any AI agent via 12 MCP tools

```
POST http://localhost:8000/mcp/call/<tool_name>
```

Twelve tools, JSON-schema'd, callable from Claude Code / Cursor / OpenHands / Aider / Cody / your own scripts:

`asil.search_code`, `asil.get_callers`, `asil.get_dependencies`, `asil.who_owns`, `asil.commit_history`, `asil.ask`, `asil.remember`, `asil.recall`, `asil.forget`, `asil.find_causes`, `asil.replay_incident`, `asil.drift_check`.

This is what I mean by "the layer underneath." Every modern AI coding tool speaks MCP. You wire ASIL once, and Claude Code (or Cursor, or Aider) gets all of the above as tools.

---

## The cost story — how it saves ~90% on a per-question basis

Here's the part I want to be honest about. ASIL doesn't make individual LLM calls cheaper. It makes **the second, third, and fourth time you ask the same question** essentially free.

### How the persistence works

Every conclusion ASIL ever produces is written to a Postgres row in `asil_memories`:

```sql
CREATE TABLE asil_memories (
  id              UUID PRIMARY KEY,
  repo_key        TEXT NOT NULL,
  question        TEXT NOT NULL,
  answer          TEXT NOT NULL,
  citations       JSONB NOT NULL,
  confidence      JSONB NOT NULL,
  cost_usd        DOUBLE PRECISION NOT NULL,
  model           TEXT,
  created_at      TIMESTAMPTZ DEFAULT now()
);
```

The question's vector goes into a Qdrant collection. The next time *anyone* asks a similar-enough question (cosine similarity above threshold), ASIL returns the cached answer instead of running the full pipeline.

The fresh cost is roughly `$0.005 – $0.02 per ask` on the cheap tier with verification. The cached cost is roughly `$0.0001` (one embedding to do the lookup). That's a **~99% saving per repeat ask**.

### The savings math

```
saved_usd ≈ memory_hits × (fresh_cost - cached_cost)
         ≈ memory_hits × ($0.01 - $0.0001)
         ≈ memory_hits × $0.0099
```

The new `asil cost summary` command computes this against your real ledger:

```
$ uv run asil cost summary
                       LLM spend, last 30 days
┌────────────────┬───────────┐
│ metric         │     value │
│ total spent    │   $0.0827 │
│ # of LLM calls │       127 │
│ avg / call     │ $0.000651 │
└────────────────┴───────────┘

     by provider              by tier
┌──────────┬─────────┐  ┌───────────┬─────────┐
│ openai   │ $0.0780 │  │ reasoning │ $0.0791 │
│ anthropic│ $0.0047 │  │ verify    │ $0.0036 │
└──────────┴─────────┘  └───────────┴─────────┘

         episodic memory savings
┌──────────────────────┬─────────┐
│ memories stored      │      41 │
│ fresh-only estimate  │   $0.41 │
│ with-memory estimate │ $0.0041 │
│ saved                │ $0.4059 │
│ savings %            │   99.0% │
└──────────────────────┴─────────┘
```

On my laptop, after a week of dogfooding ASIL against itself: **$0.08 spent, ~$0.41 worth of repeated queries deflected by memory.** That ratio scales linearly with team size — three engineers all asking "how does X work?" pay 3× without memory, 1× with it.

The `/cost` page in the dashboard renders the same numbers visually (daily-spend bars, per-provider breakdown, per-tier breakdown, savings card). This is what you'd put in a blog post or a budget review.

### The architecture choice that makes this work

The cost ledger lives in Postgres, not in process memory, on purpose. When you restart the API for any reason, the ledger persists. When you `make down && make up`, the ledger persists. When you re-clone the repo on a new laptop and `make bootstrap`, the *schema* is re-created but past entries are gone (because they were on the old machine). For a real deployment you point the same DSN at a managed Postgres and the history follows you forever.

The schema is `asil_costs (ts, provider, model, tier, profile, input_tokens, output_tokens, cost_usd)`. One row per LLM call. Aggregations happen in SQL, not in application code — so the dashboard's "daily spend" view is one query, not a fold over an in-memory list.

---

## Why nobody else has built this exact composition

I looked. Hard. Here's the field as of 2026-05-26:

| Class | Examples | What they do well | What they're missing |
|---|---|---|---|
| Coding agents | OpenHands, Aider, Continue, Cody, Cursor, Claude Code, Copilot | Edit code, file PRs, run tests | No persistent cross-session memory of *conclusions*; no temporal model; no causal reasoning |
| Code-graph tools | Sourcegraph, Codebase-Memory, Glean | Static code understanding — symbol resolution, callgraphs | Static only — no runtime events, no temporal edges, no causal scoring |
| Observability | Datadog, Grafana, Honeycomb, New Relic | Ingest metrics + traces + logs; humans build dashboards | No code model — they cannot tell you *which commit* caused the latency shift, only that latency shifted |
| AIOps RCA | MicroRCA, CausalRCA, vendor-internal RCA | Statistical anomaly detection on metric time series | Detached from the code graph; their "causes" are metric-level, not commit-level; no agent-facing API |
| GraphRAG | Neo4j GenAI, Microsoft GraphRAG | Vector + graph retrieval | Pure RAG — no temporal causality, no confidence calibration, no incident replay, no MCP surface |
| Memory products | Mem0, Letta, Zep | Episodic memory for LLM apps | Generic — not aware of code, runtime, or causality |

**ASIL is the composition no one else has shipped:** code graph + vector index + episodic memory + runtime event graph + observable causal linker + execution replay + drift detector + confidence-weighted reasoning + MCP surface, all in one product, with the explicit positioning of being the layer *under* coding agents rather than another agent.

Why hasn't someone else built this?

1. **It crosses too many disciplines.** Program analysis + temporal graphs + vector retrieval + causal inference + distributed-systems observability + evidence-weighted reasoning. Almost no single team has expertise across all of them.
2. **The market gravity pulls toward "agent edits code."** That's where the funding is (OpenHands' $18.8M Series A is the canonical example). Everyone competes for the same slot, leaving the infrastructure layer unattended.
3. **LLMs make people lazy about causality.** It's tempting to ask GPT "what caused this incident?" and ship the answer. ASIL refuses that — every causal edge must be derivable from observable graph state. That discipline is annoying to build, easy to skip, and load-bearing for trust.
4. **It's the unglamorous infrastructure work.** No viral demo of "ASIL files a PR for you." Just: when something goes wrong, you get the truth, with evidence, fast. That's a B2B-trust pitch, not a viral-demo pitch.

---

## The dashboard

8 pages, all backed by the same FastAPI gateway on `:8000`. All open source.

1. **Dashboard** — live counts (Repos / Files / Functions / Classes / Incidents / Deployments / Memories), indexed repo list, active LLM profile
2. **Ask** — question box → answer + file:line citations + per-claim verifier ✓/✗ + Confidence bar + memory hits
3. **Incidents** — every ingested postmortem; click for replay
4. **Incident replay** — timeline + ReactFlow causal graph + service cascade + state diff
5. **Causality** — interactive `find_causes` for any incident id
6. **Drift** — pick a repo, see new dependencies + boundary violations vs baseline
7. **Memory** — semantic search of every conclusion ASIL has ever reached
8. **Cost** — daily-spend bars, per-provider / per-tier splits, savings card
9. **MCP** — all 12 tools with their JSON schemas + a copy-paste Claude Code wiring snippet
10. **Health** — service status, auto-refresh every 5s

---

## The hard rules that make ASIL trustworthy

Six rules enforced in `CLAUDE.md` and the `.claude/skills/` directory. These are what keep the system from drifting into LLM-flavoured snake oil:

1. **All LLM calls go through `ModelRouter.call(tier=...)`.** Tier-routed, cost-bounded, swappable across `tight` / `balanced` / `generous` profiles via one env var.
2. **Every conclusion ships with a `Confidence` object.** Score + evidence count + retrieval strength + causal strength + derivation list. Never stripped before returning.
3. **Causality is observable, not predicted.** No LLM ever authors a `:PRECEDED` edge. Edges come from deterministic strategies, each writing its own `strategy` property.
4. **Deterministic pipelines over multi-agent debate.** One critique pass max. LangGraph is for state machines, not for agents arguing.
5. **Code namespace and runtime namespace are isolated.** Code nodes carry `repo_key`; runtime nodes carry `env_key`. No accidental cross-namespace joins.
6. **MERGE, never CREATE.** Every ingestor and every linker is idempotent.

---

## How to try it on your own repo (10 minutes, no tokens needed)

```bash
# 1. one-time setup
git clone https://github.com/rkstlohchab/ASIL
cd ASIL
make bootstrap          # uv sync + .env
make up                 # docker stack: neo4j, qdrant, postgres, redis, loki, prom, grafana
make web-install        # one-time: pnpm install for the dashboard

# 2. start the services
make api-dev            # FastAPI on :8000   (terminal A)
make web-dev            # Next.js on :3001   (terminal B)

# 3. ingest your code
uv run asil ingest /path/to/your/project --embed

# 4. ask questions (verified, cited, confidence-scored, cached for next time)
uv run asil ask "What is the main architecture pattern in this repo?"

# 5. see your money saved
uv run asil cost summary

# 6. open the dashboard
open http://localhost:3001
```

Add your GitHub PRs:

```bash
uv run asil external github /path/to/your/project --write
```

Add your Slack / Jira / Linear — just set the env vars and re-run the same command with the appropriate adapter. The CLI tells you which token is missing if you forgot one.

---

## What's still ahead

I've shipped Phases 0–7 plus most of the "Phase 3 step 3+" expansion (live adapters, external systems, multi-language). The roadmap has one stretch item left:

- **Phase 8** — *deterministic fix pipeline*. Given the causal chain from Phase 5, generate a patch, run it in a sandbox, file a PR. Constrained by the same observable evidence as the causal claim. The point is *not* "ASIL is now another coding agent" — it's "ASIL can ship the fix it already understands, with the same audit trail."

That's the only thing left to build before the v1 story is closed. Everything else is iteration on the existing layers — more languages, more adapters, more eval coverage, hosting the demo at a public URL.

---

## Why I'm building this in the open

Three reasons:

1. **The market is overcrowded at the wrong layer.** I'd rather work on the unglamorous slot than ship the 47th competitor to Cursor.
2. **Trust comes from evidence, not from confidence.** Every claim ASIL makes is auditable. That's a feature, and you can only sell it if people can see the code.
3. **The composition is the moat.** Individually each layer is solved. Combining them in this exact way — with confidence threading through every layer and causality coming from observable graph state, not LLM guesses — is the part nobody has shipped.

If any of this resonates — if you're an engineer who has watched a postmortem be reconstructed by hand for the tenth time, or paid the same OpenAI bill twice because your AI tool re-derived the same answer — give it a star, point it at your codebase, file an issue. The repo is at [github.com/rkstlohchab/ASIL](https://github.com/rkstlohchab/ASIL).

The agents you already use are about to get a lot smarter, because they're about to start asking ASIL the questions they couldn't answer alone.

---

*Built solo over 6 months. Python + FastAPI + Neo4j + Qdrant + Postgres + Tree-sitter + Next.js + Tailwind + ReactFlow. 13 source languages, 12 MCP tools, 234 unit + integration tests, ~6 hours from `git clone` to "ask my own codebase a question." MIT-licensed.*
