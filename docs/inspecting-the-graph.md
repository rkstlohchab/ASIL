# Inspecting ASIL's data — Neo4j, Qdrant, Postgres

Everything ASIL stores lives in one of three Docker services that come up with `make up`. Nothing is hidden, nothing is in process memory. This guide walks through how to open each store directly and run useful queries — handy when you want to verify what was ingested, debug recall behaviour, or write your own dashboards.

All three are on `localhost` and credentials match `.env.example` / `docker-compose.yml`. If you've customised either, adjust the URLs and DSNs below.

---

## 1. Neo4j — the knowledge graph

The code graph and the runtime-event graph both live here. Files, functions, classes, imports, call edges, services, deployments, metric shifts, incidents, and the `:PRECEDED` causal edges that connect them.

**Open the browser:** <http://localhost:7474>
- Connect URL: `bolt://localhost:7687`
- Username: `neo4j`
- Password: `asil_dev_password`

### Starter Cypher queries

**Node counts per label** — quickest "is anything in here?" check:

```cypher
MATCH (n)
RETURN labels(n)[0] AS label, count(*) AS n
ORDER BY n DESC;
```

**Every indexed repo:**

```cypher
MATCH (r:Repo)
RETURN r.key AS repo_key, r.commit_sha AS sha, r.indexed_at AS indexed_at
ORDER BY indexed_at DESC;
```

**Every function for a given repo:**

```cypher
MATCH (r:Repo {key: $repo_key})-[:CONTAINS*1..3]->(f:Function)
RETURN f.qualified_name, f.file_path, f.start_line
ORDER BY f.qualified_name
LIMIT 100;
```

(Parameterise `$repo_key` to your repo key, e.g. `"local:/Users/me/code/myrepo"`.)

**Callers of a qualified name** (1-hop incoming):

```cypher
MATCH (caller:Function)-[:CALLS]->(callee:Function {qualified_name: "asil_memory.graph_store.GraphStore"})
RETURN caller.qualified_name, caller.file_path, caller.start_line
LIMIT 50;
```

**Every `:PRECEDED` edge** (the Phase 4 moat) with confidence and strategy:

```cypher
MATCH (cause)-[r:PRECEDED]->(inc:Incident)
RETURN inc.id AS incident,
       labels(cause)[0] AS cause_kind,
       coalesce(cause.id, cause.qualified_name) AS cause_id,
       r.strategy AS strategy,
       r.confidence AS confidence,
       r.delta_seconds AS delta_seconds,
       r.derivation AS derivation
ORDER BY inc.id, r.confidence DESC;
```

**Top causes for one incident:**

```cypher
MATCH (cause)-[r:PRECEDED]->(inc:Incident {id: "INC-2026-04-12-payments-cascade"})
RETURN cause, r.strategy, r.confidence, r.delta_seconds, r.derivation
ORDER BY r.confidence DESC
LIMIT 10;
```

### CLI shortcuts

If you don't want to leave the terminal:

```bash
uv run asil graph stats                       # node counts per label
uv run asil graph stats --repo "local:/path"  # scoped to one repo
uv run asil graph neighbors my.module.Class   # 1-hop neighbourhood of a qualified name
uv run asil graph query "MATCH (f:Function) RETURN f LIMIT 5"   # ad-hoc Cypher
```

---

## 2. Qdrant — the vector indexes

Two collections matter:

| Collection | What it stores |
|---|---|
| `asil_code` | One point per function / class body. Vector = embedding of the AST-aligned chunk. Payload includes `repo_key`, `qualified_name`, `file_path`, `start_line`, `kind`. |
| `asil_memories` | One point per stored conclusion. Vector = embedding of the question. Payload includes `memory_id`, `repo_key`, `question`. The full answer lives in Postgres; Qdrant only holds the recall index. |

**Open the dashboard:** <http://localhost:6333/dashboard>

From the dashboard you can:

- List collections (left sidebar).
- Click a collection → see its config (dim, distance metric, point count).
- Click "Points" → page through points, expand any one to see its payload.
- Click "Search" → paste a vector or use a payload filter to slice.

### Useful curl snippets

If you'd rather hit the REST API directly:

```bash
# List all collections
curl -s http://localhost:6333/collections | jq

# Stats for the code collection
curl -s http://localhost:6333/collections/asil_code | jq '.result | {points_count, vectors_count, status, config: .config.params.vectors}'

# Stats for the episodic memory collection
curl -s http://localhost:6333/collections/asil_memories | jq '.result | {points_count, status}'

# Sample a few points from asil_code (payload only)
curl -s -X POST http://localhost:6333/collections/asil_code/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{"limit": 5, "with_payload": true, "with_vector": false}' | jq '.result.points'
```

### CLI shortcuts

```bash
uv run asil vector stats                          # collection size, vector dim
uv run asil vector search "router selects model"  # top-K semantic matches
```

---

## 3. Postgres — episodic memory, cost ledger, fix audit log

Three tables matter. Connect with `psql` or any SQL client:

```
postgresql://asil:asil_dev_password@localhost:5432/asil
```

```bash
# Drop into psql:
psql "postgresql://asil:asil_dev_password@localhost:5432/asil"

# Or from the docker container directly:
docker exec -it asil-postgres-1 psql -U asil -d asil
```

### `asil_memories` — every stored conclusion

```sql
\d asil_memories                               -- show the schema

-- The 10 most recent memories with cost + confidence:
SELECT
    id,
    repo_key,
    LEFT(question, 80) AS question,
    confidence_score,
    cost_usd,
    model,
    created_at
FROM asil_memories
ORDER BY created_at DESC
LIMIT 10;

-- Top 10 memories by recall_hits (once the cache short-circuit lands):
SELECT
    LEFT(question, 80) AS question,
    recall_hits,
    cost_usd,
    created_at
FROM asil_memories
ORDER BY recall_hits DESC NULLS LAST
LIMIT 10;

-- Count per repo:
SELECT repo_key, count(*) AS n
FROM asil_memories
GROUP BY repo_key
ORDER BY n DESC;
```

### `asil_costs` — one row per LLM call

This is the source of truth for everything cost-related. `cost_summary` and the `/cost` dashboard page read from here.

```sql
\d asil_costs

-- Last 20 calls — what the router actually paid for:
SELECT ts, provider, model, tier, profile, input_tokens, output_tokens, cost_usd
FROM asil_costs
ORDER BY ts DESC
LIMIT 20;

-- Spend by tier in the last 7 days:
SELECT tier, count(*) AS n_calls, sum(cost_usd) AS total_usd, avg(cost_usd) AS avg_usd
FROM asil_costs
WHERE ts >= now() - interval '7 days'
GROUP BY tier
ORDER BY total_usd DESC;

-- Daily spend, last 14 days:
SELECT ts::date AS day, count(*) AS calls, sum(cost_usd) AS spend
FROM asil_costs
WHERE ts >= now() - interval '14 days'
GROUP BY day
ORDER BY day DESC;

-- Average fresh-ask cost (everything except embed-only recall hits):
SELECT avg(cost_usd) AS avg_fresh_usd
FROM asil_costs
WHERE tier IN ('reasoning', 'classify', 'summarize', 'verify')
  AND ts >= now() - interval '30 days';

-- Average recall-hit cost (the cheap embed-only path):
SELECT avg(cost_usd) AS avg_cached_usd
FROM asil_costs
WHERE tier = 'embed'
  AND input_tokens < 100
  AND ts >= now() - interval '30 days';
```

### `asil_fix_audit` — every constrained-fix attempt (Phase 8)

```sql
\d asil_fix_audit

SELECT id, incident_id, outcome, llm_cost_usd, tests_passed, created_at
FROM asil_fix_audit
ORDER BY created_at DESC
LIMIT 10;
```

---

## A note on what "the knowledge graph" actually is

People often ask "where is the knowledge graph stored?" The honest answer is: it's three stores composed by code, not one monolithic graph file.

- **Structural truth** — what functions exist, who calls whom, which file contains what — lives in **Neo4j** as `(:Function)-[:CALLS]->(:Function)` and friends.
- **Semantic similarity** — "this code is about the same thing as that code" — lives in **Qdrant** as vectors over AST-aligned chunks.
- **Conclusions reached over time** — "we already figured out that auth works like this" — lives in **Postgres** (`asil_memories`) with a recall vector in Qdrant.

`HybridRetriever` queries all three on every `asil ask`. The "graph" the marketing refers to is the composition, not any one of them.
