# Phase 1 — End-to-end validation guide

This is the checklist that proves Phase 1 works on your machine. Run each step in order; each one has expected output + failure modes.

**Phase 1 demo bar** (from [PLAN.md](../PLAN.md#phase-1--repo-intelligence--structural-graph-weeks-38--done-2026-05-23)): `asil ingest <repo>` followed by `asil ask "<question>"` returns a natural-language answer with file:line citations and a Confidence object.

Assumes Phase 0 already passed — see [phase-0-testing.md](phase-0-testing.md) for that checklist.

---

## Prereqs (quick recap)

| Tool | Why |
|---|---|
| Docker Desktop running | Hosts neo4j, qdrant, postgres, redis, loki, prometheus, grafana |
| `OPENAI_API_KEY` set in `.env` | Embeddings + reasoning go through the `tight` profile's OpenAI fallback |
| `make up` already executed | All 7 docker services healthy |

Cost note: a full Phase 1 validation (ingest + ask + eval) on this repo runs ~$0.001 against OpenAI.

---

## Step 1 — All tests green

```bash
make test-unit
# expected: 79 passed (40 from Phase 0 + 7 retriever + 13 resolver + 9 mcp tools + 10 eval ...)

uv run pytest tests/integration -q
# expected: 12 passed (5 graph + 7 vector)
```

If unit tests fail, lint failures are usually the reason. Run `make format` and retry.

If integration tests skip with "neo4j unreachable" or "qdrant unreachable", restart that container via Docker Desktop (long-uptime Bolt drift is the usual culprit — see Phase 0 testing guide).

---

## Step 2 — Ingest the ASIL repo into itself

```bash
uv run asil ingest . --embed
```

**Expected output (stats table):**

| metric | value |
|---|---|
| files parsed | ~30–50 (grows with code) |
| functions (incl. methods) | ~150–300 |
| classes | ~30–70 |
| call sites | ~600–1500 |
| files with parse errors | 0 |
| graph writes | == files parsed |
| vector writes | == functions + classes |
| call edges resolved | partial — typically 14–25% of call sites |

The "call edges resolved" number looks low at first glance. Sanity check: most calls are stdlib (`len`, `print`, `range`, `dict`, `str.format`, `pathlib.Path`, ...) which aren't in our index. Only intra-repo calls can be resolved by the lightweight resolver. Run:

```bash
uv run asil graph resolve-calls "local:$(pwd)"
```

and look at the per-strategy breakdown. `same_module` + `self_method` should dominate; that's correct.

**Failure modes:**
- `qdrant unreachable` → restart `asil-qdrant` container; retry.
- `neo4j unreachable` → restart `asil-neo4j` container.
- `OPENAI_API_KEY` errors → check `.env`; embedding calls need a real key.

---

## Step 3 — Confirm graph + vector state

```bash
uv run asil graph stats
# expected: 1 Repo, ~30+ Files, ~150+ Functions, ~30+ Classes, ~30+ Symbols

uv run asil vector stats
# expected: total points == functions + classes
```

If you re-ran ingest more than once on the same repo, the counts should be stable (MERGE-based idempotency).

---

## Step 4 — Browse the graph

Open <http://localhost:7474> (login `neo4j` / `asil_dev_password`). Try:

```cypher
MATCH (c:Class {name: "GraphStore"})-[:CONTAINS]->(m:Function)
RETURN c, m
```

You should see GraphStore at the center with ~13 method nodes around it (1 Class + N Functions connected by CONTAINS edges).

```cypher
MATCH (caller:Function)-[r:CALLS]->(callee:Function {name: "merge_repo"})
RETURN caller.qualified_name, r.derivation
```

You'll see the resolved internal callers of `merge_repo`. The `r.derivation` field shows which heuristic created the edge (`same_module`, `self_method`, etc.).

---

## Step 5 — Semantic search

```bash
uv run asil vector search "where do we connect to Neo4j" --limit 5
```

Top hit should be `GraphStore.verify_connectivity` with score ≥ 0.4 and a meaningful gap (~0.04+) to rank 2. If the gap is small or rank 1 is something unrelated, the index is probably stale — re-run `asil ingest . --embed` and retry.

---

## Step 6 — The Phase 1 hero query: `asil ask`

```bash
uv run asil ask "How does the LLM router pick a provider for a given tier?"
```

**Expected:**

- An "answer" panel containing prose that mentions `ModelRouter._provider` and `ModelRouter.call` with `(router.py:NN)` style citations.
- A "confidence" table with `score` ≥ 0.5 (yellow/green), `evidence_count` = 20, and a `derivation` list naming the top hit.
- LLM cost in the table ≤ $0.001.

Try several questions of varying difficulty:

```bash
uv run asil ask "Where is the daily budget enforced before an LLM call goes out?"
uv run asil ask "What ensures every conclusion ships with a confidence score?"
uv run asil ask "How does ASIL avoid creating duplicate nodes when re-ingesting?"
uv run asil ask "How do we deploy ASIL to Kubernetes?"      # SHOULD fail gracefully
```

The last question should produce a low-confidence answer that explicitly says the indexed code doesn't cover Kubernetes deployment. That's the refusal behavior — the system prompt enforces "don't invent". If the LLM hallucinates an answer, file a bug; the prompt regression matters.

Pass `--show-candidates` to see the retrieval table that fed the LLM:

```bash
uv run asil ask "What chunks the AST output into pieces we can embed?" --show-candidates
```

---

## Step 7 — Run the regression harness

```bash
uv run asil eval recall asil_self --repo "local:$(pwd)" --show-details
```

**Phase 1 baseline (acceptance criteria):**

| metric | baseline | bar |
|---|---|---|
| recall@1 | 60% | informational |
| recall@3 | 60% | 80% (Phase 2 fix) |
| recall@5 | 80% | meets PLAN.md bar |
| recall@10 | 80% | floor |

**About the 60% recall@3:** the original PLAN.md aimed for 80% at this rank. The gap is real and expected: vector-only retrieval, with no re-ranker, occasionally lets a lexically-similar test outrank the production code it's testing. Phase 2 introduces the verifier pass + cross-encoder re-ranking that closes this. **Don't tune the corpus to hide the gap.** The corpus is a regression catcher; if recall numbers move in the wrong direction in future commits, this command is how we notice.

`--show-details` prints per-case results. Look at the misses — they're the questions where retrieval needs help.

---

## Step 8 — MCP tools (other agents can call ASIL)

Start the API:

```bash
uv run uvicorn asil_api.main:app --reload
```

In another terminal, list the tools:

```bash
curl -s http://localhost:8000/mcp/tools | jq '.[] | .name'
# expected: 6 tools — search_code, get_callers, get_dependencies,
#                     who_owns, commit_history, ask
```

Call one:

```bash
curl -s -X POST http://localhost:8000/mcp/call/asil.search_code \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"query":"how does the router pick a provider","limit":5}}' \
  | jq '.result.candidates[] | {score, qualified_name}'
```

You should see the top 5 ranked candidates as JSON, ready for any MCP-compatible client (Cursor, Claude Code, OpenHands) to consume once we wire native stdio MCP in Phase 7.

Call the highest-level tool — the same hybrid retrieve → reason flow as `asil ask`, but JSON in/out:

```bash
curl -s -X POST http://localhost:8000/mcp/call/asil.ask \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"question":"What chunks the AST output into pieces we can embed?"}}' \
  | jq '{answer: .result.answer, confidence: .result.confidence.score, citations: [.result.citations[].qualified_name]}'
```

---

## "Phase 1 demo passed" checklist

You're done with Phase 1 when **every** box ticks:

- [ ] `make test-unit` shows 79+ passed.
- [ ] `uv run pytest tests/integration -q` shows 12 passed (5 graph + 7 vector).
- [ ] `asil ingest . --embed` completes without errors; stats table populated.
- [ ] `asil graph stats` shows non-zero Repo/File/Function/Class.
- [ ] `asil vector stats` shows `total points == functions + classes`.
- [ ] `asil ask "..."` returns an answer with file:line citations AND a Confidence card.
- [ ] `asil ask "How do we deploy to Kubernetes?"` (off-corpus) refuses gracefully — low confidence, no fabrication.
- [ ] `asil eval recall asil_self` shows recall@5 ≥ 80%.
- [ ] `curl http://localhost:8000/mcp/tools` lists 6 tools.
- [ ] `curl ... /mcp/call/asil.search_code` returns ranked JSON candidates.

When that's all green, record a short screen capture and we move to Phase 2.

---

## Known Phase 1 limitations (intentional, fixed later)

| Limitation | Why deferred | Fix lands in |
|---|---|---|
| recall@3 is 60% on `asil_self`, below the 80% bar | Vector-only retrieval, no re-ranker | Phase 2 (verifier + cross-encoder) |
| Only ~14% of call sites get `:CALLS` edges | Stdlib/3rd-party calls aren't in the index; full SCIP isn't wired | Phase 1.x polish + Phase 4 |
| Python only (no JS / TS / Go) | Each grammar has different idioms; better as a focused commit | Phase 1.x polish |
| Re-ingest is a full re-scan | `git fetch` + diff-aware re-parse not yet wired | Phase 1.x polish |
| `who_owns` and `commit_history` return stubs | Commit/Author nodes ship in Phase 2 | Phase 2 |
| MCP transport is HTTP only (no stdio) | Native MCP SDK integration is its own design lift | Phase 7 (UI + MCP polish) |

None of these block the moat work in Phases 2–5. They're polish items that get prioritized when a real user hits them.

---

## Troubleshooting

- **"`local:/path/to/ASIL` not found"** in `asil eval`: the repo_key changed (different machine / clone path). Re-run `asil ingest .` to refresh, then re-run eval.
- **Neo4j Bolt port stuck after long uptime**: restart `asil-neo4j` in Docker Desktop.
- **OpenAI rate limits during ingest --embed**: the embedder batches at 32 chunks per request. If you still hit limits, drop `EMBED_BATCH_SIZE` in [embedder.py](../packages/asil_ingest/asil_ingest/embedder.py) and re-run.
- **`asil ask` answer references things not in the snippets**: regression — the system prompt should prevent this. Open an issue with the exact question + the `--show-candidates` output.
