# Why I built the layer **underneath** every AI coding agent

*(and how it ends up saving the same teams roughly 90% of their Claude / GPT bill)*

---

I have nothing against the AI coding agent space. Cursor, Claude Code, OpenHands, Aider, Continue, Cody, Copilot — they're all useful. They're also all racing toward the same finish line: a slightly smarter autocomplete that can occasionally file a PR.

Six months ago I started a project that deliberately runs in the opposite direction. It's called **ASIL** — Engineering Intelligence Infrastructure — and it's the layer those agents *should be sitting on top of*. The persistent, temporal, causal model of how a software system evolves, behaves, and fails.

This post is a tour of what it does, why no one else has built this exact composition, how it ends up saving a meaningful fraction of your LLM bill almost by accident, and what it looks like when you point it at your own codebase. The code is open source at [github.com/rkstlohchab/ASIL](https://github.com/rkstlohchab/ASIL).

---

## The three blind spots every AI coding tool shares

I spent a year using every agent I could find. They all share three blind spots that nobody is racing to fix.

**First, no memory across sessions.** Ask Claude Code "how does authentication work in this repo?" today. Ask it the same question next Tuesday in a new chat window. It re-reads the codebase from scratch, re-runs the LLM, charges you again, and gives you a slightly different answer.

**Second, no model of time.** They can tell you *what* the code does today. They cannot tell you *what changed between the deployment at 14:17 and the incident alert at 14:23*. There's no temporal index. There's no concept of "this preceded that."

**Third, no causality.** When prod breaks, a human reads dashboards, postmortems, deploys, logs, and assembles a causal chain. AI coding agents have zero access to that runtime substrate — and even if they did, they'd emit *plausible* causes (LLM hallucination), not *observable* ones.

These three gaps are not random. They're the same gap viewed from three angles: **there is no shared, persistent, evidence-weighted model of the system underneath the agents**. Every session is amnesia.

ASIL fills that gap. The agents become its clients.

---

## What ASIL actually is

One sentence:

> A persistent, temporal, causal knowledge graph of how your software system evolves, behaves, and fails — exposed to any AI agent via MCP, queryable from a CLI and a dashboard.

The hero query that defines v1:

> **"Why did this production incident happen?"** → reconstructed timeline, probable root cause with a confidence score, evidence list, causal chain visualisation, architecture-drift report.

It does *not* file PRs as its hero feature. It does *not* try to "be the agent." It tries to **be the truth layer** that the agents use. When it *does* propose a fix (Phase 8, more below), it does so constrained by the observable causal chain — not from a free-form "go fix the bug" prompt.

---

## The four defensible pillars

The combination is what's hard to copy. Most products do one of these. None of the popular tools do all four.

**Temporal causality.** The graph tracks events and their relationships *over time*. `(:Deployment)-[:PRECEDED {confidence}]->(:Incident)` is a real edge type with real semantics, derived from observable graph state.

**Execution replay.** Given an incident id, ASIL reconstructs what happened across services as a time-ordered, dependency-aware causal chain. Time-travel debugging for distributed systems.

**Confidence-scored reasoning.** Every conclusion ASIL emits ships with a `Confidence` object: score, evidence count, retrieval strength, causal strength, derivation list. Never stripped before returning to the user.

**Architecture drift detection.** Learn the *expected* dependency graph; flag undocumented coupling, decay, and anti-pattern growth as concrete `DriftEvent` nodes — *before* the PR merges.

---

## What it can actually do today, on my laptop

Everything below works **right now** on a fresh checkout with `make up`. Receipts further down.

### Ingest any codebase in 13 languages

```
uv run asil ingest /path/to/your/project --embed
```

ASIL parses Python, TypeScript, JavaScript, TSX, Go, Ruby, Java, Rust, C, C++, PHP, Swift, and Kotlin. It builds a Neo4j knowledge graph (Repo, File, Function, Class, Symbol, Commit, Author) and writes function-level vector embeddings to Qdrant. The whole thing is incremental and idempotent — re-running the same command updates rather than duplicates.

### Ask questions, get cited answers with confidence

```
uv run asil ask "How does the LLM router pick a provider for a given tier?"
```

Behind the scenes: hybrid vector + graph retrieval, LLM synthesis, verifier pass that checks every claim against the cited evidence, confidence object assembled from retrieval strength + evidence count + verifier flags.

The output isn't an essay. It's a structured answer plus file:line citations plus a per-claim ✓/✗ verifier report plus a Confidence card.

Ask the same question again 30 seconds later and ASIL recognises it from episodic memory. The second answer costs roughly $0.0001 instead of roughly $0.01. That's the **money-saving fact** I'm coming back to in a minute.

### Ingest incidents → derive observable causal chains

```
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil temporal link prod
uv run asil temporal causes INC-2026-04-12-payments-cascade
```

Three composable causal-edge strategies, each writing `(:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)`.

The first strategy is **temporal proximity** — exponential decay with a 5-minute half-life. Catches the obvious "deploy 7 minutes before incident."

The second is **lagged correlation** — when a Deployment shipped code that the MetricShift's service runs, the deployment outranks the symptom. This is the cause-vs-symptom honesty fix.

The third is **explicit reference** — a commit message or postmortem text mentioning the incident id gets a high-confidence edge with `strategy: "explicit_reference"`.

**Crucially: no LLM is ever asked "what caused this?"** The causes come from observable graph state. Every edge carries the derivation string ("Deployment deploy-8f2c1d4 on auth occurred 7.0min before the incident → confidence 0.379, half-life 5min"). The downstream LLM consumes the causal edges; it doesn't author them.

### Replay any incident

```
uv run asil replay INC-2026-04-12-payments-cascade
```

Renders a Rich terminal report (or, in the dashboard, an interactive ReactFlow view): incident header → time-ordered timeline → ranked causal chain → service cascade (which services took collateral damage and in what order) → state diff (deployments during the window, metric before/after deltas) → confidence card.

### Detect architecture drift

```
uv run asil drift baseline local:$(pwd) --output baseline.json
# ... a week, a refactor, three PRs later ...
uv run asil drift report local:$(pwd) --baseline baseline.json
```

Flags new dependency edges and boundary violations — *before* the PR merges. The dashboard's `/drift` page makes this clickable.

### Bring in PRs, Slack, Jira, Linear

```
# Works without any tokens on any git repo — uses `gh` CLI or `git log` fallback.
uv run asil external github . --write

# Token-gated. Wire SLACK_BOT_TOKEN / JIRA_* / LINEAR_API_KEY in .env.
uv run asil external slack    --channel C-INCIDENTS --service payments --write
uv run asil external jira     --project INC --write
uv run asil external linear   --team ENG --write
```

The Slack adapter extracts incident IDs ("INC-2026-04-12") and service mentions from the message body, so the graph automatically wires `(:ChatMessage)-[:DISCUSSES]->(:Incident)` and `(:ChatMessage)-[:MENTIONS]->(:Service)` edges. The Jira and Linear adapters do the same for ticket titles and descriptions.

### Poll live Prometheus, Loki, and Kubernetes

```
uv run asil adapters prometheus \
  --probe 'payments:p99_latency:histogram_quantile(0.99, ...)' \
  --probe 'auth:error_rate:sum(rate(http_5xx_total[1m]))' \
  --write
uv run asil adapters loki --service payments --service auth --write
uv run asil adapters k8s  --namespace prod --write
```

When Prometheus shows a metric ratio crossing the configured threshold (default 1.5x current vs baseline), ASIL emits a `MetricShift` event. Loki's most recent error log lines get clustered by a redacted signature (UUIDs, numbers, timestamps collapsed) and emitted as `LogSignature` nodes. K8s contributes `Deployment` and `Service` events.

These are all real adapters against real services. The docker-compose ships Prometheus and Loki and Grafana out of the box so you can play with them locally without a cluster.

### Talk to it from any AI agent via 13 MCP tools

```
POST http://localhost:8000/mcp/call/<tool_name>
```

Thirteen tools, JSON-schema'd, callable from Claude Code, Cursor, OpenHands, Aider, Cody, or your own scripts: `asil.search_code`, `asil.get_callers`, `asil.get_dependencies`, `asil.who_owns`, `asil.commit_history`, `asil.ask`, `asil.remember`, `asil.recall`, `asil.forget`, `asil.find_causes`, `asil.replay_incident`, `asil.drift_check`, `asil.propose_fix`.

This is what I mean by "the layer underneath." Every modern AI coding tool speaks MCP. You wire ASIL once, and Claude Code (or Cursor, or Aider) gets all of the above as tools.

---

## Phase 8: the constrained fix pipeline

Phase 8 is the one part of ASIL that touches code. I deferred it for months on purpose, because it's the part everyone *expects* an "AI engineering" tool to do — and everyone gets it wrong by doing it free-form. ASIL's Phase 8 is the version that respects the rest of the system.

The contract is simple. The patch generator only runs when there is a Phase 5 causal chain to act on. The LLM gets a narrow slice of context: the incident summary, the top causal candidates with their strategy and confidence and derivation, and the specific functions or files implicated by those causes. It does NOT get the whole repo. It is NOT asked "what should we fix?" in the abstract.

The instruction is "here is observable evidence that X caused Y; emit a minimal unified diff that addresses X." The diff is then validated with `git apply --check` before any sandbox ever sees it. The sandbox is an ephemeral local directory that applies the patch, runs the configured test command, and reports tests-passed / tests-failed / apply-failed / timeout. Every attempt is logged to a Postgres audit table with the causal chain, the diff, the sandbox stdout/stderr tail, the LLM cost, and the aggregate outcome.

Nothing is pushed. Nothing is merged. The proposal plus the sandbox result is the artifact a human (or a higher-level orchestrator) decides on. Read-only by default: `asil fix propose <incident_id>` shows the diff and exits. Opt in to actually run the sandbox with `asil fix run <incident_id>`.

The confidence-score handling is the part I'm proudest of. The proposal's overall confidence is bounded by the *weakest* component — the minimum of the top cause's confidence and the replay's confidence. A high-confidence replay built on a 30% cause does NOT inherit the replay's 90%. The weakest link bounds the proposal, which is exactly the cause-vs-symptom honesty we built the causal linker to preserve.

This is what separates ASIL's fix pipeline from a generic coding agent. The LLM is the *executor* of a hypothesis the deterministic causal linker already settled on, not the *author* of the hypothesis.

In practice:

```
$ uv run asil fix propose INC-2026-04-12-payments-cascade
       incident  INC-2026-04-12-payments-cascade
        summary  patches auth/redis_pool.py
     confidence  0.412
 affected files  auth/redis_pool.py
          model  gpt-4o-mini
           cost  $0.000412

derivation
  • patch generator constrained to top-5 causal candidates
  • top cause: Deployment (strategy=temporal_proximity, confidence=0.412)
  • context: 1 file(s), 1432 chars
  • model=gpt-4o-mini provider=openai cost=$0.000412

proposed diff
  --- a/auth/redis_pool.py
  +++ b/auth/redis_pool.py
  @@ -42,7 +42,8 @@
   def get_connection():
  -    return pool.get()
  +    # TODO Phase 8: pool exhaustion linked to deploy-8f2c1d4
  +    return pool.get(timeout=POOL_GET_TIMEOUT_SECONDS)
```

When you're ready, `asil fix run INC-...` does the same thing plus copies the repo to a temp directory, applies the diff with `git apply`, runs `make test`, and audits the outcome.

---

## ASIL as your PR gatekeeper — the SonarQube-shaped surface, with real causal signal

Once people see the dashboard, the first question is always: "OK but how do I get this on every pull request?" Static-analysis tools sit in that slot today — you commit, the CI step runs, the bot comments on the PR with what it found, the gate either passes or fails. That's the shape engineers already trust. ASIL fills that slot for engineering-intelligence findings: causal links from production, architecture drift, boundary violations. **It is the gatekeeper, not a feeder for one.**

So I built `asil scan` — one command. It's the same role a code-quality scanner plays in your pipeline; the difference is what's actually being scanned.

The scan path is intentionally cheap. It never touches the reasoning LLM. Every signal comes from observable graph state or a saved baseline, which means a scan completes in seconds and costs essentially nothing per run. You can wire it as a `pre-push` hook without slowing anyone down.

```
uv run asil scan \
  --baseline asil-baseline.json \
  --gate normal \
  --sarif asil.sarif \
  --pr-comment asil-pr-comment.md \
  --json asil.json
```

What it does, in order: connects to the local Neo4j graph; runs the Phase-6 drift detector against the baseline JSON; queries the Phase-4 causal links for incidents in the last 168 hours so each incident's top causal chain becomes a `note` finding ("by the way, the auth-cascade incident last Tuesday had a deploy of code touched by this PR as its top cause"); aggregates everything into a `ScanReport` with severity counts; applies the quality gate; and emits whichever output formats CI asked for.

Four gate levels, same shape as every linter you've ever used. `strict` fails on warning and above, `normal` (the default) fails on error and above, `lenient` only on critical, `none` always passes. Exit code is `0` if the gate passed, `1` if it failed, `2` if ASIL itself crashed — so any CI tool's failure handling Just Works.

The output the team actually reads is the **GitHub-flavored markdown PR comment** — pass/fail badge, severity counts, one collapsible `<details>` block per tier, edited in place on subsequent pushes. That's the SonarQube-style sticky comment, generated by ASIL itself; no external server, no third-party UI required.

Two additional formats round it out. **SARIF 2.1.0** is the universal CI-scanner output format — emitting it makes ASIL's findings show up in GitHub's native code-scanning tab alongside CodeQL and friends. Not because we're feeding another tool; because SARIF is the format CI runners expect, and supporting it costs nothing. **JSON** is the full `ScanReport` for archival, custom dashboards, or your own grafana panels.

The repo ships a full GitHub Action workflow you can drop into any project. It spins Neo4j + Qdrant + Postgres as service containers (the entire ASIL backend, ephemeral per CI run, no external server required), ingests the PR's code, runs the scan, edits-or-posts the PR comment in place on subsequent runs, uploads the SARIF to GitHub code scanning, and fails the workflow on a gate failure. Roughly 100 lines of YAML, copy-paste ready.

For the `pre-commit` framework, the repo ships a `.pre-commit-hooks.yaml` with `asil-scan` and `asil-scan-strict` ids — three lines in your project's `.pre-commit-config.yaml` and the gate runs locally on every push.

This is the role a code-quality scanner plays in a modern stack, owned end-to-end by ASIL. A critical finding from `asil scan` looks like *"the auth service shipped a deploy that preceded an incident touching this code 7 minutes later — confidence 0.412, strategy temporal_proximity+lagged_correlation."* No other PR-gate tool ships that finding because no other PR-gate tool has the temporal/causal graph underneath it. That's the moat — and now it's in your pull-request flow.

---

## The "is it offline?" question, answered honestly

Every ASIL install is its own world. There is no central server. There is no telemetry. Your graph, your memories, your cost ledger, your audit log — all on your machine.

The whole docker-compose stack runs locally: Neo4j, Qdrant, Postgres, Redis, Prometheus, Loki, Grafana. Tree-sitter parsing, the graph builder, the vector store, the episodic memory, the causal linker, the replay engine, the drift detector, the fix sandbox, the audit log — all local Python, zero network. Embeddings can be 100% local too (the `tight` profile uses BGE-large via sentence-transformers).

There is one network dependency in the default config: the reasoning LLM. That's the call that goes to OpenAI / Anthropic / DeepSeek. The `ModelRouter` already supports swapping providers via a `LLMProvider` Protocol; adding an `OllamaProvider` for fully-offline reasoning is roughly 50 lines and on my short list.

External adapters (GitHub PRs, Slack, Jira, Linear, live K8s + Prom + Loki) are optional and token-gated. They only run when you configure them, and they only reach the systems you point them at.

If your security review needs "no data leaves the host except the LLM call," you're already there. If it needs "no data leaves the host at all," you swap the LLM tier to a local provider and you're done.

---

## The cost story — how it saves around 90% on a per-question basis

Here's the part I want to be honest about. ASIL doesn't make individual LLM calls cheaper. It makes the second, third, and fourth time you ask the same question essentially free.

### How the persistence works

Every conclusion ASIL ever produces is written to a Postgres row in `asil_memories`. Question, answer, citations, confidence, model, cost, timestamp. The question's vector goes into a Qdrant collection. The next time *anyone* asks a similar-enough question (cosine similarity above threshold), ASIL returns the cached answer instead of running the full pipeline.

The fresh cost is roughly $0.005 to $0.02 per ask on the cheap tier with verification. The cached cost is roughly $0.0001 (one embedding to do the lookup). That's a roughly 99% saving per repeat ask.

### The savings math

```
saved_usd ≈ memory_hits × (fresh_cost - cached_cost)
         ≈ memory_hits × ($0.01 - $0.0001)
         ≈ memory_hits × $0.0099
```

The `asil cost summary` command computes this against your real ledger and prints something like:

```
       LLM spend, last 30 days
       total spent       $0.0827
       # of LLM calls    127
       avg / call        $0.000651

       by provider
         openai          $0.0780
         anthropic       $0.0047

       by tier
         reasoning       $0.0791
         verify          $0.0036

       episodic memory savings
         memories stored        41
         fresh-only estimate    $0.41
         with-memory estimate   $0.0041
         saved                  $0.4059
         savings %              99.0%
```

On my laptop, after a week of dogfooding ASIL against itself: $0.08 spent, roughly $0.41 worth of repeated queries deflected by memory. That ratio scales linearly with team size — three engineers all asking "how does X work?" pay 3× without memory, 1× with it.

The `/cost` page in the dashboard renders the same numbers visually (daily-spend bars, per-provider breakdown, per-tier breakdown, savings card). This is what you'd put in a blog post or a budget review.

### The architecture choice that makes this work

The cost ledger lives in Postgres, not in process memory, on purpose. When you restart the API for any reason, the ledger persists. When you `make down && make up`, the ledger persists. When you re-clone the repo on a new laptop, the schema is re-created but past entries are gone (because they were on the old machine). For a real deployment you point the same DSN at a managed Postgres and the history follows you forever.

The schema is one row per LLM call: timestamp, provider, model, tier, profile, input_tokens, output_tokens, cost_usd. Aggregations happen in SQL, not in application code — so the dashboard's "daily spend" view is one query, not a fold over an in-memory list.

---

## Why nobody else has built this exact composition

I looked. Hard. Here's the field as of late 2026.

**Coding agents** — OpenHands, Aider, Continue, Cody, Cursor, Claude Code, Copilot — are good at editing code, filing PRs, running tests. They have no persistent cross-session memory of *conclusions*, no temporal model, no causal reasoning.

**Code-graph tools** — Sourcegraph, Codebase-Memory, Glean — are good at static code understanding: symbol resolution, callgraphs. Static only. No runtime events, no temporal edges, no causal scoring.

**Observability platforms** — Datadog, Grafana, Honeycomb, New Relic — ingest metrics and traces and logs; humans build dashboards on top. No code model — they can tell you that latency shifted, not *which commit* caused the shift.

**AIOps RCA tools** — MicroRCA, CausalRCA, vendor-internal RCA — do statistical anomaly detection on metric time series. Detached from the code graph; their "causes" are metric-level, not commit-level; no agent-facing API.

**GraphRAG products** — Neo4j GenAI, Microsoft GraphRAG — do vector + graph retrieval. Pure RAG. No temporal causality, no confidence calibration, no incident replay, no MCP surface.

**Memory products** — Mem0, Letta, Zep — implement episodic memory for LLM apps. Generic — not aware of code, runtime, or causality.

**ASIL is the composition no one else has shipped.** Code graph plus vector index plus episodic memory plus runtime event graph plus observable causal linker plus execution replay plus drift detector plus constrained fix pipeline plus confidence-weighted reasoning plus MCP surface — all in one product, with the explicit positioning of being the layer *under* coding agents rather than another agent.

Why hasn't someone else built this? Four reasons.

**It crosses too many disciplines.** Program analysis, temporal graphs, vector retrieval, causal inference, distributed-systems observability, evidence-weighted reasoning, sandbox execution. Almost no single team has expertise across all of them.

**The market gravity pulls toward "agent edits code."** That's where the funding is (OpenHands' $18.8M Series A is the canonical example). Everyone competes for the same slot, leaving the infrastructure layer unattended.

**LLMs make people lazy about causality.** It's tempting to ask GPT "what caused this incident?" and ship the answer. ASIL refuses that — every causal edge must be derivable from observable graph state. That discipline is annoying to build, easy to skip, and load-bearing for trust.

**It's the unglamorous infrastructure work.** No viral demo of "ASIL files a PR for you." Just: when something goes wrong, you get the truth, with evidence, fast. That's a B2B-trust pitch, not a viral-demo pitch.

---

## The dashboard

Ten pages, all backed by the same FastAPI gateway on port 8000. All open source.

The `/` Dashboard page shows live counts of repos, files, functions, classes, incidents, deployments, and memories — plus the indexed-repo list and the active LLM profile.

The `/ask` page is the most-used. Question box, then answer with file:line citations, per-claim verifier ✓/✗, a Confidence bar, and any memory hits from prior sessions.

The `/incidents` page lists every postmortem ASIL has ingested, newest first.

Clicking an incident takes you to `/incidents/[id]`, which renders the full reconstruction: a ReactFlow causal graph with causes orbiting the incident (line thickness and colour encoding confidence), the time-ordered timeline, the ranked causal chain with derivation strings, the service cascade, and the state diff.

The `/causality` page lets you trigger `asil.find_causes` interactively for any incident id.

The `/drift` page shows new dependencies and boundary violations against a stored baseline.

The `/memory` page is a semantic search of every conclusion ASIL has ever reached.

The `/cost` page renders the daily-spend bars, per-provider and per-tier splits, and the savings card.

The `/mcp` page lists all 13 tools with their JSON schemas and a copy-paste Claude Code wiring snippet.

The `/health` page auto-refreshes every 5 seconds and shows the status of each backing service.

---

## The hard rules that make ASIL trustworthy

Six rules enforced in `CLAUDE.md` and the `.claude/skills/` directory. These are what keep the system from drifting into LLM-flavoured snake oil.

**One — all LLM calls go through `ModelRouter.call(tier=...)`.** Tier-routed, cost-bounded, swappable across `tight` / `balanced` / `generous` profiles via one env var.

**Two — every conclusion ships with a `Confidence` object.** Score, evidence count, retrieval strength, causal strength, derivation list. Never stripped before returning.

**Three — causality is observable, not predicted.** No LLM ever authors a `:PRECEDED` edge. Edges come from deterministic strategies, each writing its own `strategy` property.

**Four — deterministic pipelines over multi-agent debate.** One critique pass max. LangGraph is for state machines, not for agents arguing.

**Five — code namespace and runtime namespace are isolated.** Code nodes carry `repo_key`; runtime nodes carry `env_key`. No accidental cross-namespace joins.

**Six — MERGE, never CREATE.** Every ingestor and every linker is idempotent.

---

## How to try it on your own repo (10 minutes, no tokens needed)

```
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

Want PR context? `uv run asil external github /path/to/your/project --write`.

Want Slack, Jira, Linear? Set the env vars (`SLACK_BOT_TOKEN`, `JIRA_*`, `LINEAR_API_KEY`) in `.env` and re-run the appropriate `asil external ...` command. The CLI tells you which token is missing if you forgot one.

Want to try the fix pipeline? Ingest one of the bundled postmortems, run `asil temporal link prod`, then `asil fix propose INC-2026-04-12-payments-cascade`. The diff plus confidence breakdown will land in your terminal.

Want to try the CI scan? `uv run asil scan --pr-comment -` prints the same markdown a GitHub Action would post on a PR — gate badge, severity counts, the lot. Drop the bundled `.github/workflows/asil-scan.yml` into any project to wire it in for real.

---

## What's still ahead

I've shipped the whole stack now — Phases 0 through 8 plus the CI integration. There is no remaining "stretch" item on the roadmap. The shape of the next six months is iteration, not new pillars.

**Local-LLM provider.** The reasoning tier currently defaults to a cloud LLM. An `OllamaProvider` for fully-offline reasoning is roughly 50 lines and slots into the existing `LLMProvider` Protocol — no other layer needs to change.

**Hosted public demo.** One polished postmortem replay at a public URL, so reviewers can click through without setting up Docker.

**Higher-order orchestrator.** Right now `asil fix run` is a one-shot CLI invocation. The natural next layer is something that runs it on every new incident, files a draft PR when confidence is above gate, and pings a human reviewer.

**More languages and adapters.** Each new Tree-sitter grammar (Scala, Haskell, Elixir, Dart, Zig) is a small `_GENERIC_LANG_CONFIG` entry away. Each new external adapter (PagerDuty, Notion, Confluence) follows the same `poll(env_key)` contract.

**Eval expansion.** The current postmortem corpus is five incidents. Doubling it tightens the regression bar on the causal linker.

**More scan rule sources.** Phase-7.5's GitHub PR adapter could feed `asil scan` with "this PR touches a file mentioned in three open Linear tickets." The wiring is already there; it's a query away.

---

## Why I'm building this in the open

Three reasons.

**The market is overcrowded at the wrong layer.** I'd rather work on the unglamorous slot than ship the 47th competitor to Cursor.

**Trust comes from evidence, not from confidence.** Every claim ASIL makes is auditable — the fix proposals, where the proposed diff and the causal chain and the sandbox stdout and the aggregate outcome are all in one Postgres row; the CI findings, where every SARIF result carries the rule id and the derivation it was built from; the cost ledger, where every LLM call is one row with provider and tier and token counts. You can only sell that if people can see the code.

**The composition is the moat.** Individually each layer is solved. Combining them in this exact way — with confidence threading through every layer, causality coming from observable graph state rather than LLM guesses, and patches constrained by the causal chain rather than free-form prompts — is the part nobody has shipped.

If any of this resonates — if you've watched a postmortem be reconstructed by hand for the tenth time, or paid the same OpenAI bill twice because your AI tool re-derived the same answer, or watched a PR ship boundary-violating coupling because the linter doesn't know what "boundary" means in your codebase — give it a star, point it at your codebase, drop the GitHub Action into your repo, file an issue. The repo is at [github.com/rkstlohchab/ASIL](https://github.com/rkstlohchab/ASIL).

The agents you already use are about to get a lot smarter, because they're about to start asking ASIL the questions they couldn't answer alone.

---

*Built solo over 6 months. Python + FastAPI + Neo4j + Qdrant + Postgres + Tree-sitter + Next.js + Tailwind + ReactFlow. 13 source languages, 13 MCP tools, `asil scan` CI command with SARIF + PR-comment output, GitHub Action template + pre-commit hooks, 274 unit + 39 integration tests, roughly 6 hours from `git clone` to "ask my own codebase a question." MIT-licensed.*
