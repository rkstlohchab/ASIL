# ASIL Handoff Document

**Purpose:** Hand the project off to a fresh Claude Code session with zero context loss.
**Last updated:** 2026-05-25.
**Phase status:** Phases 0–6 ✅ DONE. Next: Phase 7 (stretch) — Minimal UI + MCP polish.
**Test bar:** 234/234 passing on `pytest tests/unit tests/integration -q` against the live docker stack.

---

## Read this before you touch anything

In this exact order:

1. **[CLAUDE.md](CLAUDE.md)** — hard rules (tier-routed LLM calls, Confidence on every conclusion, deterministic pipelines, phase gates, no UI until Phase 7).
2. **[PLAN.md](PLAN.md)** — phased roadmap, eval bars, what each phase is for.
3. **[AGENTS.md](AGENTS.md)** — tool-agnostic entry point that points back at the skills.
4. **Skills in [.claude/skills/](.claude/skills/)** — load-bearing invariants:
   - `asil-llm-call.md` — every LLM call goes through `ModelRouter.call(tier=...)`. No hardcoded model names.
   - `asil-confidence.md` — every conclusion ships with a `Confidence` object.
   - `asil-positioning.md` — "Engineering Intelligence Infrastructure," never "AI OS" / "autonomous coder."
   - `asil-phase-gate.md` — don't start Phase N+1 until N has demoed.
   - `asil-graph-schema.md` — Neo4j schema invariants for the code namespace (Repo/File/Function/Class/Symbol).
   - `asil-runtime-events.md` — runtime-namespace schema (Service/Deployment/MetricShift/LogSignature/Incident).
   - `asil-temporal-causality.md` — **the moat rules**: observable-only causality, derivation always logged, no LLM-emitted causes.
   - `asil-memory.md` — episodic store contract.
   - `asil-mcp-tool.md` — MCP tool surface contract.
   - `asil-eval-corpus.md` — don't tune corpora to hide gaps.
5. **[docs/phase-0-testing.md](docs/phase-0-testing.md)** and **[docs/phase-1-testing.md](docs/phase-1-testing.md)** — validation checklists you can mirror.

Don't skip these. The rules in CLAUDE.md + the skills are how this project stays coherent across sessions and contributors. Most of the technical debt you'll otherwise reintroduce has already been priced into the architecture; the skills tell you where the price is hidden.

---

## What's been built (state of the project)

```
ingest a repo ──► Tree-sitter parser ──► Neo4j knowledge graph (code namespace, scoped by repo_key)
                                          │
                                          ├─ vector embeddings → Qdrant (function-level chunks)
                                          └─ call edges (heuristic resolver, ~14% resolution rate)

ingest a postmortem ──► postmortem.py ──► Neo4j runtime namespace (scoped by env_key)
                                          (Service / Deployment / MetricShift / LogSignature / Incident)
                                          + bridge edges (Deployment-SHIPPED->Commit when available)

asil ask "<question>"  ──► HybridRetriever (vector + graph expand)
                          ──► answer LLM (cited)
                          ──► Verifier (per-claim ✓/✗)
                          ──► Confidence (canonical scorer)
                          ──► EpisodicStore (Postgres + Qdrant; recall surfaces prior conclusions next time)

asil temporal link <env> ──► TemporalLinker (proximity + lagged-correlation + explicit-reference)
                            ──► (:Cause)-[:PRECEDED {confidence, delta_seconds, derivation, strategy}]->(:Incident)

asil temporal causes <id>  ──► causes_for_incident query
                              ──► ranked table OR JSON via asil.find_causes MCP tool

asil replay <id>  ──► ReplayEngine (timeline + causes + cascade + state diff + confidence)
                     ──► Rich terminal view with 6 panels (header, timeline, causes, cascade, state diff, confidence)
                     ──► asil.replay_incident MCP tool for programmatic access

asil drift baseline <repo>  ──► BaselineLearner (snapshot :CALLS edges)
asil drift report <repo>    ──► DriftDetector (new/removed deps + boundary violations)
                               ──► asil.drift_check MCP tool
```

**12 MCP tools live** at `POST /mcp/call/{name}`: `asil.{search_code, get_callers, get_dependencies, who_owns, commit_history (stub), ask, remember, recall, forget, find_causes, replay_incident, drift_check}`.

**Workspace** (uv-managed monorepo):

```
apps/api/             FastAPI gateway + MCP HTTP server
apps/cli/             Typer CLI (the primary UX)
packages/asil_core/        ✅ LLM router, Confidence, config, logging
packages/asil_ingest/      ✅ Tree-sitter (Python only), repo cloner, embedder, graph builder, call resolver
packages/asil_memory/      ✅ GraphStore (Neo4j), VectorStore (Qdrant), HybridRetriever, EpisodicStore (Postgres+Qdrant)
packages/asil_reasoning/   ✅ Verifier (second-pass LLM checker), canonical Scorer
packages/asil_eval/        ✅ recall harness + asil_self corpus (10 Q&A)
packages/asil_infra/       ✅ Phase 3: runtime-event models, postmortem ingestor, InfraAdapter protocol, FileAdapter, K8s/Prom/Loki stubs
packages/asil_temporal/    ✅ Phase 4: temporal-proximity + lagged-correlation + explicit-reference (THE MOAT)
packages/asil_replay/      ✅ Phase 5: replay engine (timeline + cascade + state diff + confidence) + MCP tool
packages/asil_drift/       ✅ Phase 6: baseline learner, drift detector, boundary rules + MCP tool
```

**Bundled demo data:** 5 postmortems in [research/postmortems/](research/postmortems/):
  1. `2025-08-14-payments-redis-cascade.yaml` — original, tests all 3 strategies
  2. `2026-02-08-db-pool-exhaustion.yaml` — DB pool pattern
  3. `2026-03-19-dns-misconfig-checkout.yaml` — DNS cascade
  4. `2026-01-15-tls-gateway-outage.yaml` — explicit-reference (names deploy ID in summary)
  5. `2025-11-22-config-oom.yaml` — lagged-correlation only (no SHA)

**Git log (latest commits at top):**

```
1a7fdd0  data(eval): 2 new postmortems for eval corpus expansion
017c84b  feat(asil_drift): architecture drift detection (Phase 6)
9a0e8ee  feat(asil_infra): infrastructure adapters + FileAdapter (Phase 3 step 2)
c18a953  feat(asil_replay+api): state diff + MCP replay tool (Phase 5 steps 2-3)
45c6ede  feat(asil_temporal): explicit-reference strategy (Phase 4 step 3)
```

---

## Your job: remaining tasks

Phases 0–6 are **DONE**. The engine work is complete. What remains is stretch work:

### Task 1 — JS/TS Tree-sitter parser

**Why now:** Python-only parser blocks ingesting any web/native/mobile repo, including the user's `~/Documents/GitHub/workplace` (a React Native app). Mechanical work; same `tree-sitter-language-pack` shim.

**Files to modify:**
- [`packages/asil_ingest/asil_ingest/treesitter_parser.py`](packages/asil_ingest/asil_ingest/treesitter_parser.py) — add `_parse_typescript`, `_parse_javascript`, `_parse_tsx` dispatch methods. Mirror the existing `_parse_python` shape — same shim helpers (`_kind`, `_named_children`, `_text`, etc.).
- The existing `TreeSitterParser.__init__` rejects non-Python with `NotImplementedError`. Remove that gate; add per-language dispatch.
- `models.py` already has `SourceLanguage.{javascript, typescript, tsx}` — don't add new enum values.

**Tree-sitter node names you need (different from Python):**
- `function_declaration` (top-level), `arrow_function` (in `lexical_declaration` → `variable_declarator`), `method_definition` (inside `class_body`)
- `class_declaration`, `class_body`
- `import_statement` with `import_clause` → `named_imports` / `namespace_import` / `identifier` (default)
- `call_expression` (not `call`)
- TypeScript adds: `interface_declaration`, `type_alias_declaration`, `enum_declaration`, `function_signature`, `type_annotation`

**Scope cut for step 1:** function/class/import/call extraction. **Skip** interfaces, type aliases, enums, default-export plumbing, dynamic imports, decorators (decorators on JS classes are a syntax-level thing, defer). Document the limitations in the parser docstring.

**Module-name convention for JS/TS:** there's no module system the way Python has one. Use the dotted file path as the qualified-name prefix. E.g., `src/components/Button.tsx` → `module_name = "src.components.Button"`. The existing CLI in `apps/cli/asil_cli/main.py` already derives `module = rel.removesuffix(".py").replace("/", ".")` for Python; do the same for `.ts` / `.tsx` / `.js` / `.mjs` / `.cjs`.

**Tests to create:**
- `tests/unit/test_treesitter_parser_javascript.py` — fixtures with:
  - top-level `function foo()` and arrow `const bar = () => {...}`
  - `class C { method() {...} async other() {...} }`
  - `import x from 'y'`, `import { a, b as c } from 'd'`, `import * as ns from 'e'`
  - call sites inside functions
- `tests/unit/test_treesitter_parser_typescript.py` — same shape, plus TS-typed signatures (`function foo(x: number): string`)
- `tests/unit/test_treesitter_parser_tsx.py` — minimal: JSX in a component, confirm parser doesn't crash + extracts the component function

**Success criteria:**
1. `uv run pytest tests/unit -q` adds ~15 passing tests, total goes from 130 → ~145.
2. Live smoke against the user's workplace folder:
   ```bash
   uv run asil ingest /Users/raksithlochabb/Documents/GitHub/workplace \
     --language typescript --language tsx --language javascript \
     --limit 50 --no-graph
   ```
   Should report > 0 functions parsed. Don't write to the graph (`--no-graph`) on this smoke — there's no value in indexing 50 files; the goal is to confirm parsing works.
3. No Python tests regress.

**Commit message convention:**
```
feat(asil_ingest): JS/TS/TSX Tree-sitter parsers (Phase 1.8)

Extracts function declarations, arrow functions, classes, methods,
imports, and call sites for .js/.mjs/.cjs/.ts/.tsx files. Same shim
pattern as the Python parser; per-language dispatch in
TreeSitterParser.parse(). Interfaces, type aliases, enums, default
exports, decorators, dynamic imports are deliberately out of scope —
documented in the parser docstring.

Module-name convention: file path with separators → dots (matches Python).

+15 unit tests covering JS arrow functions, TS typed signatures, TSX
components with JSX, import variants.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

### Task 2 — 2 more postmortems (eval-corpus enrichment)

**Why now:** Phase 4 step 2 (next task) needs more ground truth to validate against. The single bundled postmortem proves the linker runs; multiple postmortems prove the linker generalizes.

**Files to create:**
- `research/postmortems/2026-02-08-db-pool-exhaustion.yaml`
- `research/postmortems/2026-03-19-dns-misconfig-checkout.yaml`

**Shape:** follow the bundled cascade postmortem exactly. Top-level `incident:` block + `timeline:` list of `kind:` entries. The asil-eval-corpus skill applies — phrase like real incidents, don't game the structure.

**Scenarios to write:**

1. **DB connection pool exhaustion** (`2026-02-08-db-pool-exhaustion.yaml`):
   - Env: `prod`
   - Services: `orders`, `inventory`, `notifications`
   - Trigger: orders service deploys a query optimization that adds a `JOIN` doubling per-request DB time
   - Cascade: orders p95 latency climbs → orders takes longer to release DB connections → pool fills → inventory and notifications (sharing the same Postgres instance) start timing out → checkout fails
   - Resolution: rollback after 47 minutes
   - Aim for 8–12 timeline events

2. **DNS misconfig causing checkout cascade** (`2026-03-19-dns-misconfig-checkout.yaml`):
   - Env: `prod`
   - Services: `gateway`, `payments`, `email`
   - Trigger: ConfigMap rollout changes a DNS suffix; payments service can no longer resolve `email.internal`
   - Cascade: payments emits log signature "name resolution failed: email.internal" → checkout flow blocks on email send → gateway 5xx rate climbs → eventually payments retries exhaust
   - Resolution: revert ConfigMap; 23-minute incident
   - Note: this is the first postmortem with a `kind: config_change` event — STOP and check, that kind isn't in the schema yet. **DO NOT** invent new event kinds without a design doc; for this postmortem, model the ConfigMap rollout as a `kind: deployment` with `description: "ConfigMap rollout: email DNS suffix change"` and `deployment_id: cm-2026-03-19-1`. The schema-extension question (a proper `:ConfigChange` label) is a Phase 3 step 2 question, not a postmortem-author question.

**Success criteria:**
- Both files load cleanly: `uv run python -c "from asil_infra import load_postmortem; load_postmortem('research/postmortems/2026-02-08-db-pool-exhaustion.yaml')"` produces no errors.
- `uv run asil postmortem ingest <each-file>` writes the expected node counts (visible in the stats table).
- Update `tests/unit/test_postmortem.py::test_bundled_example_postmortem_loads_cleanly` to also assert these new files load.

**Commit message:**
```
chore(eval): add 2 postmortems to research corpus (Phase 4 step 2 prep)

Adds DB-pool-exhaustion (orders→inventory→notifications) and
DNS-misconfig-checkout (gateway→payments→email) cascades. Different
shapes from the bundled cascade — exercise the Phase 4 step 2
lagged-correlation linker against multiple ground-truth patterns.

Both follow the postmortem YAML shape; no schema changes.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

### Task 3 — Phase 4 step 2: lagged correlation

**Why now:** Phase 4 step 1 ranks by temporal proximity alone, which has a known limitation: a metric shift 1 min before an incident outranks the deployment 7 min before, even when the deployment is the actual cause (the metric shift is the symptom). This task fixes it.

**The insight:** A `Deployment` whose `service_name` appears in `incident.affected_services` is much more likely to be a cause than a `MetricShift` whose `service_name` is in the same list (the latter is the symptom). Use this as a strategy.

**Files to create / modify:**
- New module: `packages/asil_temporal/asil_temporal/lagged_correlation.py`
- Modify: `packages/asil_temporal/asil_temporal/linker.py` — extend `TemporalLinker` to apply the lagged-correlation strategy as a second pass after proximity scoring, BEFORE writing edges. The same `(:Cause)-[:PRECEDED]->(:Incident)` edge gets written but with `strategy: "temporal_proximity+lagged_correlation"` and the boosted confidence.
- Modify: `packages/asil_temporal/asil_temporal/__init__.py` — export new public names.
- Extend: `tests/integration/test_temporal_linker.py` — new test `test_lagged_correlation_promotes_deploy_above_symptom_metric_shift`. Pins the regression: after Phase 4 step 2, the auth deploy must outrank the latency spike in `causes_for_incident` for the bundled postmortem.

**Algorithm:**
1. For each scored `CausalCandidate` from the proximity pass:
   - If `candidate.cause_kind == "Deployment"` AND `candidate.cause_props.service_name in incident.affected_services`:
     - Apply an additive bonus: `confidence += 0.6`, capped at 1.0.
     - Set `strategy = "temporal_proximity+lagged_correlation"`.
     - Append to derivation: `"lagged_correlation: deploy is on affected service <X>; promoted from symptom-tier to cause-tier with +0.6 bonus"`.
   - Otherwise: leave the proximity score + strategy untouched.

**The +0.6 figure** is calibrated against the bundled postmortem (proximity auth-deploy = 0.379, latency-spike = 0.871; with +0.6 the deploy becomes 0.979 and wins). Document why this value in a comment so future tuners understand the trade-off.

**Why additive, not multiplicative:** multiplying caps you at the original proximity score (which was low for the auth deploy because it was 7 min away). Additive bonus rewards "this is on the affected service" as evidence orthogonal to time.

**Success criteria:**
1. New integration test asserts `deploy-8f2c1d4` is now the **top** cause (not just top-3) of `INC-2026-04-12-payments-cascade`, with confidence ≥ 0.85.
2. The existing `test_bundled_postmortem_links_auth_deployment_as_top_cause` still passes (the deploy stays in the top-3 — in fact it's #1 now; tighten the assertion to `causes[0].cause_props["deployment_id"] == "deploy-8f2c1d4"`).
3. Live demo:
   ```bash
   uv run asil events clear prod --yes
   uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
   uv run asil temporal link prod
   uv run asil temporal causes INC-2026-04-12-payments-cascade
   ```
   → top row is now Deployment `deploy-8f2c1d4 on auth` with confidence ~0.98 and derivation mentioning both proximity AND lagged_correlation.

**Update the asil-temporal-causality skill:** the "cause-vs-symptom honesty" section needs editing to reflect that step 2 fixed the problem; add a paragraph documenting the lagged-correlation strategy.

**Commit message:**
```
feat(asil_temporal): lagged-correlation strategy (Phase 4 step 2)

Closes the cause-vs-symptom gap from step 1. Deployments whose service
appears in incident.affected_services receive an additive +0.6 bonus on
top of their temporal-proximity score; symptoms (MetricShifts and
LogSignatures on the same affected services) stay at proximity-only.

On the bundled cascade, deploy-8f2c1d4 (auth, 7min before) moves from
#2 (confidence 0.379) to #1 (confidence 0.979), correctly outranking
the payments latency spike at 0.871 (which is the SYMPTOM of the deploy's
bad Redis pool refactor, not a cause).

The strategy is composable: edges get strategy="temporal_proximity+lagged_
correlation" so the derivation traces both contributions.

+ 1 integration test pinning the new ordering. Existing headline test
tightened to assert auth deploy is #1 (was top-3).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

### Task 4 — Phase 5 step 1: execution replay engine

**Why now:** This is the **hero demo** from PLAN.md — `asil replay <incident_id>` produces the full causal timeline + cascade in one terminal view. After Phase 4 step 2 lands, the underlying data is correct; Phase 5 step 1 makes it human-readable.

**Files to create:**
- `packages/asil_replay/pyproject.toml` (mirror `packages/asil_temporal/pyproject.toml` shape)
- `packages/asil_replay/asil_replay/__init__.py` — export public surface
- `packages/asil_replay/asil_replay/timeline.py` — `TimelineBuilder` class
- `packages/asil_replay/asil_replay/cascade.py` — `CascadeReconstructor` class
- `packages/asil_replay/asil_replay/replay.py` — `ReplayEngine` (orchestrator that produces the full `IncidentReplay` dataclass)
- Add `packages/asil_replay` to root `pyproject.toml` workspace members.
- Add `asil-replay` to CLI/api dependencies (in `apps/cli/pyproject.toml` and `apps/api/pyproject.toml`).
- Add CLI: `asil replay <incident_id>` in `apps/cli/asil_cli/main.py` (mirror the pattern of `asil temporal causes`).
- Add MCP tool: `asil.replay_incident` in `apps/api/asil_api/mcp_tools.py`.
- Tests: `tests/unit/test_replay.py` (with fake graph store) + `tests/integration/test_replay.py` (against real Neo4j with bundled postmortem).
- New skill: `.claude/skills/asil-execution-replay.md`.

**The `IncidentReplay` dataclass:**

```python
@dataclass(slots=True)
class IncidentReplay:
    incident: dict  # raw incident node props
    summary_lines: list[str]   # human-readable header
    timeline: list[TimelineEntry]  # all events ordered chronologically, marked cause/symptom/response
    top_causes: list[CausalCandidate]  # from causes_for_incident
    service_cascade: list[ServiceCascadeEntry]  # services ordered by first-event time
    confidence: Confidence
```

**The CLI output** (`asil replay <id>`) should print, in order:

1. **Header panel** — incident id, title, env, severity, detected_at, resolved_at, duration, affected_services.
2. **Timeline table** — chronologically ordered events. Columns: `at`, `kind`, `service`, `description`, `marker` (`↗ cause` / `▶ INCIDENT` / `↓ response`). Highlight the row at `detected_at` with the `▶` marker. Events with a `:PRECEDED` edge get `↗ cause`; events after `detected_at` get `↓ response`; everything else is plain.
3. **Top causes table** — pulled from `causes_for_incident`; same shape as `asil temporal causes`. Top 5 or all if fewer.
4. **Service cascade** — ASCII flow: `auth ──► payments ──► cart` with each service's first event time below the name. Sort services by their earliest event in the window. Use Rich's `Panel` for visual separation.
5. **Confidence card** — average confidence across the top causes + evidence count + derivation summary.

**`TimelineBuilder` implementation hints:**
- Reuse `GraphStore.events_for_service` and call it for each service in `incident.affected_services`, then merge + dedupe by `(kind, service, at)`.
- OR write a new dedicated Cypher: `MATCH (i:Incident {id: $id})-[:AFFECTED]->(svc:Service) WITH collect(svc.name) AS svcs ...` then 4 separate sub-queries (same pattern as `events_for_service`).
- For each timeline entry, check if a `:PRECEDED` edge exists from this event to the incident — that's how you mark `↗ cause`.

**`CascadeReconstructor` implementation hints:**
- For each affected service, find its earliest event time. Sort services by that time. The order IS the cascade — earliest = root cause's service, later = downstream.
- Output format:
  ```
  auth         (first event 14:17 — deploy)
   ↓
  payments     (first event 14:23 — metric shift)
   ↓
  cart         (first event 14:26 — metric shift)
  ```

**Success criteria:**
1. Live demo on the bundled postmortem produces a single-screen output with all 5 sections legible. The cascade ordering is `auth → payments → cart`. The top cause is the auth deploy. Total runtime < 1 second.
2. `tests/unit/test_replay.py` — 5–8 tests with fake stores (timeline ordering, cause-marker logic, cascade ordering by earliest event, confidence aggregation).
3. `tests/integration/test_replay.py` — 2 tests: bundled-postmortem end-to-end + a "no events" graceful empty state.
4. The MCP tool `asil.replay_incident` returns the same data as JSON: `{incident, timeline, top_causes, service_cascade, confidence}`.
5. **Critical:** the asil-temporal-causality skill's hard rules apply. Replay does NOT invent causes — it reads `:PRECEDED` edges. If you find yourself writing prose like "the incident was probably caused by X," stop and pull from the graph instead.

**Commit message:**
```
feat(asil_replay): execution replay engine + `asil replay` (Phase 5 step 1)

The hero demo from PLAN.md. Given an incident id, produces a single
terminal view containing:
  - incident header (id, title, severity, window, affected services)
  - chronological timeline marked with cause/symptom/response
  - top causes (from :PRECEDED edges; same shape as `asil temporal causes`)
  - service cascade (services ordered by earliest event)
  - aggregated confidence card

asil_replay package: TimelineBuilder, CascadeReconstructor, ReplayEngine
orchestrator returning an IncidentReplay dataclass. CLI prints the rich
view; MCP tool returns the same data as JSON for external agents.

Doesn't invent causes — reads :PRECEDED edges that the Phase 4 linker
wrote. See asil-execution-replay skill for the contract.

+ unit tests (timeline ordering, cause markers, cascade derivation,
empty-state handling) and integration test against the bundled cascade.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

### Task 5 — Layperson documentation

**Why now:** The user wants the project to be open-sourceable. Right now CLAUDE.md / PLAN.md / skills are excellent for contributors but unreadable for someone who'd benefit from ASIL without knowing how it works.

**File to create:** `WHAT_IS_ASIL.md` at the repo root.

**Audience:** A smart non-engineer (PM, founder, recruiter) — or an engineer who's never seen the project before and has 5 minutes.

**Structure:**

1. **The 30-second pitch** — one paragraph. Avoid: "AI OS," "autonomous coder," "chatbot." Use: "Engineering Intelligence Infrastructure," "the layer underneath coding agents," "explains reality with evidence."

2. **What ASIL does** — a concrete scenario walkthrough. Use the bundled postmortem. Show the actual commands and outputs (real screenshots/output blocks from a `make up` → `asil postmortem ingest` → `asil temporal causes` → `asil replay` flow).

3. **Why this is hard / why existing tools can't do it** — short table comparing Cursor / Claude Code / Devin / Datadog / ASIL across columns like "understands code structurally," "explains incidents causally," "remembers across sessions," "every claim auditable."

4. **How to use it in 5 minutes** — copy-paste-friendly setup (`make bootstrap`, `make up`, `asil ingest .`, `asil ask "..."`). Include the cost (~$0.001 per query on the tight profile with OpenAI).

5. **How AI agents use it (MCP)** — `curl` example hitting `POST /mcp/call/asil.find_causes`. Explain in 2 sentences why this matters: any agent that speaks MCP can ask ASIL "what caused this incident?" and get an audit-trail-grade answer.

6. **What it's NOT** — explicitly: not a chatbot, not an autonomous coder, not a code-completion tool. Sit underneath those.

7. **Where it's going** — one-paragraph roadmap: K8s adapter + execution replay + drift detection + multi-language polish.

**Style guide for the doc:**
- Avoid jargon. Define every acronym on first use.
- No bullet lists deeper than 2 levels.
- Use real output blocks, not pseudo-output.
- One ASCII diagram (the same one in CLAUDE.md's current-phase block is fine).
- Link out to PLAN.md / AGENTS.md / CLAUDE.md / skills for the technical reader.

**Success criteria:**
- A non-engineer who reads the doc can describe to a friend what ASIL is.
- An engineer who reads the doc can run `asil postmortem ingest <bundled> && asil temporal causes <bundled-incident-id>` within 10 minutes of `git clone`.
- The doc references `WHAT_IS_ASIL.md` at the top of README.md.

**Commit message:**
```
docs: WHAT_IS_ASIL.md — layperson explainer for open-source readers

Targets a non-engineer audience (PMs, founders, recruiters) and engineers
who have 5 minutes. 7 short sections: 30s pitch, concrete scenario (bundled
cascade), comparison table vs Cursor/Devin/Datadog, 5-min setup, MCP curl
example, what ASIL is NOT, roadmap.

Linked from README.md.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

### Task 6 — Final updates + status

After tasks 1–5 are committed:

1. **Update PLAN.md** — bump the Status column:
   - Phase 1 row: "✅ DONE 2026-05-23 + JS/TS parser ✅ <date>"
   - Phase 4 row: "◐ step 1 + step 2 ✅ <date> (proximity + lagged-correlation)"
   - Phase 5 row: "◐ step 1 ✅ <date> (replay engine)"

2. **Update README.md status block** to reflect all of the above.

3. **Update CLAUDE.md** — current phase block: "Phase 5 step 1 done; Phase 4 step 2 done; multi-language ingestion live."

4. **Update AGENTS.md** to reference the new `asil-execution-replay` skill.

5. **Update `.claude/settings.json`** SessionStart hook to mention Phase 5 step 1.

6. **Final test sweep:**
   ```bash
   uv run ruff format .
   uv run ruff check . --fix
   uv run pytest tests/unit tests/integration -q
   ```
   Target: all green. Expect ~190 tests total.

7. **One final commit** with the doc updates:
   ```
   docs: PLAN/README/CLAUDE/AGENTS bumps for JS/TS + Phase 4 step 2 + Phase 5 step 1
   ```

---

## Hard rules — DO NOT BREAK these

Summarized from the skills. Re-read the skill files when you encounter the topic.

1. **All LLM calls go through `ModelRouter.call(tier=...)`.** No hardcoded model names. Tier values: `reasoning` / `classify` / `summarize` / `verify` / `embed`. (`asil-llm-call`)
2. **Every conclusion ships with a `Confidence` object.** Never strip it. (`asil-confidence`)
3. **Causality is observable, not predicted.** Never use an LLM to decide what caused an incident. The temporal linker computes from graph state. (`asil-temporal-causality`)
4. **Deterministic pipelines over multi-agent debate.** One critique pass max (the Verifier). No agents arguing. (CLAUDE.md)
5. **No frontend / Next.js until Phase 7.** CLI is the UX. (CLAUDE.md)
6. **Positioning: "Engineering Intelligence Infrastructure."** Never "AI OS," "autonomous coder," "AI engineer," "chatbot." (`asil-positioning`)
7. **Schemas:** code namespace = `repo_key`, runtime namespace = `env_key`. Cross only at documented bridges (`Service-RUNS->File`, `Deployment-SHIPPED->Commit`). MERGE everywhere; never CREATE. (`asil-graph-schema`, `asil-runtime-events`)
8. **MCP tools:** async, JSON-safe output, Confidence on every reasoning result, read-only by default, never auto-fix. (`asil-mcp-tool`)
9. **Eval corpora:** don't tune them to make numbers go up. (`asil-eval-corpus`)
10. **Phase gates:** demo + design doc before moving on. (`asil-phase-gate`)
11. **No `pip install`** — this is a `uv` workspace. Add deps with `uv add` against the right workspace member.
12. **Never read `os.environ` directly.** Go through `asil_core.get_settings()`.

---

## Gotchas — things you'll otherwise rediscover painfully

These are real bugs / API quirks I hit while building. The codebase already handles them; you'll re-trip them if you forget.

1. **`tree-sitter-language-pack` returns a Rust-backed binding** where every accessor is a METHOD, not a property. Use `node.kind()`, `node.start_position()`, `node.named_child(i)`, etc. — not `node.type` / `node.start_point` / `node.named_children`. The `_kind`, `_named_children`, `_text` shim in [treesitter_parser.py](packages/asil_ingest/asil_ingest/treesitter_parser.py) isolates this. Use it.

2. **Tree-sitter Python docstrings** appear as either `expression_statement > string` (some grammar versions) or just `string` directly under the function body's `block`. The `_py_docstring` helper handles both — copy that pattern for JS/TS if you implement them (`/** ... */` JSDoc blocks).

3. **`tree.root_node` is a METHOD in tree-sitter-language-pack.** `tree.root_node()`, not `tree.root_node`. Same for `node.has_error()` — always paren.

4. **Neo4j timestamps in our graph are ISO-8601 strings**, not native `DateTime`. Why: the postmortem ingestor writes `.isoformat()`. When you compare in Cypher, use `WHERE n.at >= $since AND n.at <= $until` (string comparison). DO NOT wrap with `datetime($since)` — that returns 0 rows because it compares DateTime to string. There's one comment in [linker.py](packages/asil_temporal/asil_temporal/linker.py) marking this; if you migrate to native `DateTime` properties, update both the ingestor's `_*_props` and the linker's three sub-queries in one PR.

5. **Cypher disallows mixing `collect()` with non-aggregated variables in the same WITH clause.** Don't try to do `WITH deps + ms + logs + collect(...) AS all_events` — use 4 separate sub-queries merged in Python. See [graph_store.py:events_for_service](packages/asil_memory/asil_memory/graph_store.py) for the pattern.

6. **The `tight` LLM profile auto-falls-back: DeepSeek → OpenAI → mock.** Users with only an `OPENAI_API_KEY` get `gpt-4o-mini` automatically. See [profiles.py](packages/asil_core/asil_core/llm/profiles.py).

7. **`uv sync` from this monorepo**: each new workspace member needs an entry in **both** the root `pyproject.toml`'s `[tool.uv.workspace] members = [...]` AND any package that depends on it (CLI / api typically) needs both a `dependencies = [...]` entry AND a `[tool.uv.sources] new-package = { workspace = true }` entry. Miss either, you get import errors.

8. **Ruff config has `ASYNC109` ignored** (legit use of `timeout` param on async functions when forwarding to httpx). Don't add new ignores without a comment.

9. **Markdown linter complains about en-dashes (`–`) and ambiguous Unicode (`×`).** Use ASCII (`-`, `x`). Cosmetic but lint blocks commits.

10. **Empty-collection Qdrant ops fail with 404.** `clear_repo` checks `collection_exists` first; do the same if you add new collections.

11. **The integration test conftest deletes the `asil_memories` Qdrant collection on teardown** so the CLI's 1536-dim text-embedding-3-small can recreate it after tests run with 4/8-dim fake vectors. Don't change that without re-thinking the test/live coexistence.

12. **Don't poll for background work.** When a Bash command is `run_in_background: true`, the harness notifies you when it finishes — don't `sleep` and check.

13. **Docker commands may silently produce 0-byte output in some sandbox environments.** If `docker ps` returns nothing, that's possibly the sandbox, not the actual state. Probe via Python: `uv run python -c "from asil_memory import GraphStore; s = GraphStore(); s.verify_connectivity(); print('ok')"`.

---

## How to commit

Conventional commit format:

```
<type>(<scope>): <short summary>

<body — explain the WHY, the WHAT, the trade-offs. multi-paragraph fine.>

<bullet list of test counts / coverage if relevant>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `style`. Use `feat(asil_temporal):` not `feat: asil_temporal`. NEVER skip the `Co-Authored-By` line.

Pass commit messages via heredoc to preserve formatting:

```bash
git commit -m "$(cat <<'EOF'
feat(asil_x): one-liner summary

Body paragraph(s).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

Do not amend prior commits. Do not push (user does that). Do not skip hooks.

---

## How to validate at the end

Full pipeline, top to bottom:

```bash
# 1. Cleanslate
uv run asil events clear prod --yes
uv run asil memory clear local:$(pwd) --yes 2>/dev/null || true

# 2. Code side
uv run asil ingest .              # Python repo
uv run asil ingest /Users/raksithlochabb/Documents/GitHub/workplace \
  --language typescript --language tsx --language javascript --limit 200
  # ← post-JS/TS: this should now succeed with > 0 functions
uv run asil ask "How does the LLM router pick a provider for a tier?"

# 3. Runtime side
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil postmortem ingest research/postmortems/2026-02-08-db-pool-exhaustion.yaml
uv run asil postmortem ingest research/postmortems/2026-03-19-dns-misconfig-checkout.yaml

# 4. The moat
uv run asil temporal link prod
uv run asil temporal causes INC-2026-04-12-payments-cascade
  # ← post-Phase-4-step-2: top cause should be deploy-8f2c1d4, NOT the latency spike

# 5. The hero demo (Phase 5 step 1)
uv run asil replay INC-2026-04-12-payments-cascade
  # ← should print incident header + timeline + top causes + cascade + confidence

# 6. Tests
uv run pytest tests/unit tests/integration -q
  # ← expect ~190 passed

# 7. MCP surface (in two terminals)
uv run uvicorn asil_api.main:app --reload
curl -s http://localhost:8000/mcp/tools | jq '.[] | .name'
  # ← should include asil.find_causes and (post-Phase-5) asil.replay_incident
curl -s -X POST http://localhost:8000/mcp/call/asil.replay_incident \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"incident_id":"INC-2026-04-12-payments-cascade"}}' | jq
```

Report the recall numbers + commit hashes back to the user when done.

---

## When in doubt

- **What's the right scope for this change?** Re-read PLAN.md for the phase it belongs to.
- **Am I drifting into commodity coding-agent territory?** Re-read `asil-positioning.md`.
- **Is this an architectural change?** Discuss with the user via `AskUserQuestion` before writing code.
- **Does my proposed fix break the bundled-postmortem regression test?** STOP. That test is the moat's regression guard.
- **Should I write a new MCP tool?** Re-read `asil-mcp-tool.md` first — there are non-negotiable contract rules.
- **The user's repo at `/Users/raksithlochabb/Documents/GitHub/workplace`** is a React Native TypeScript app. Post-Task-1, ingest it as a demo (with `--no-graph` for the first smoke; full ingest if the user okays the cost).

The user values: forward progress on the moat, clean phase gates, honest engineering (don't game tests), demos over screenshots. They use the project as a portfolio + potential startup vehicle. Phase 4 step 1 is the high-water mark; anything that backslides on the moat needs to be discussed before committing.

Good luck. The previous Claude built ~10k lines of working code with full test coverage — the architecture is sound. Just don't break the rules above.
