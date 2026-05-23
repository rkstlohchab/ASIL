---
name: asil-graph-schema
description: Use when adding new node labels, edge types, or properties to the Neo4j graph. Enforces the Phase 1 schema invariants (every domain node carries `repo_key`, identity via uniqueness constraints, MERGE-based idempotency, unresolved data as JSON not parallel arrays).
---

# asil-graph-schema

The graph is the substrate for everything in ASIL — temporal causality (Phase 4),
execution replay (Phase 5), drift detection (Phase 6) all sit on top of it.
Schema changes are load-bearing; do them deliberately.

## The Phase 1 schema (don't deviate without a design doc)

**Nodes:**
- `Repo {key, spec, org, name, is_local, commit_sha, indexed_at}` — one per indexed repo.
- `File {repo_key, path, language, loc, module_name, imports_json, ...}`
- `Function {repo_key, qualified_name, name, signature, start_line, end_line, docstring, is_async, is_method, parent_class, decorators, calls_json, n_calls}`
- `Class {repo_key, qualified_name, name, start_line, end_line, docstring, decorators, base_classes, method_qnames, n_methods}`
- `Symbol {repo_key, qualified_name, name, kind, line}` — top-level vars / constants.

**Edges:**
- `(:Repo)-[:CONTAINS]->(:File)`
- `(:File)-[:CONTAINS]->(:Function | :Class | :Symbol)`
- `(:Class)-[:CONTAINS]->(:Function)` (for methods)
- `(:Function)-[:CALLS {line, derivation, callee_text}]->(:Function)` (resolved by `CallResolver`)

Defined in:
- `packages/asil_memory/asil_memory/graph_store.py` (constraints, MERGE templates).
- `packages/asil_ingest/asil_ingest/graph_builder.py` (property marshallers).

## Invariants — break these and you'll regret it

1. **Every domain node carries `repo_key`.** Even though Repo identity uses a separate `key` property, all other labels carry `repo_key` as a denormalized scope. That makes "scope this query to one repo" a property filter, not a join. Don't add a new label without it.

2. **Identity comes from `(repo_key, qualified_name)`** for everything except `Repo` itself (which uses `key` alone). Uniqueness constraints in `SCHEMA_CYPHER` enforce this. If you add a new label, add a matching constraint in the same file — uncomstrained MERGEs silently duplicate.

3. **MERGE, never CREATE.** All writes go through `merge_repo` / `merge_file_with_children` so re-ingest is idempotent. The graph builder is intentionally a single MERGE-driven query per file.

4. **No nested objects as properties.** Neo4j properties are primitives or arrays of primitives. Lists of dicts (`imports`, `calls`) go in as **JSON strings** (`imports_json`, `calls_json`), not as parallel arrays. The structure can be promoted to real edges later (the `CallResolver` is the example of this).

5. **No bidirectional `(a)-[:RELATES]->(b)` followed by `(b)-[:RELATES]->(a)`.** Pick a direction. `CONTAINS` is parent → child. `CALLS` is caller → callee. `INHERITS_FROM` (when it lands) is subclass → superclass.

6. **Cypher node labels are PascalCase singular** (`Service`, not `Services`). Edge types are SCREAMING_SNAKE_CASE (`PRECEDED`, `CASCADED_TO`).

## Phase 4 will add (already implied by PLAN.md — don't preempt)

- `(:Service)`, `(:Deployment)`, `(:ConfigChange)`, `(:Event)`, `(:MetricShift)`, `(:Alert)`, `(:Incident)`, `(:TrafficSpike)`, `(:LogSignature)`.
- Causal edges with `{delta_seconds, confidence, derivation}` properties — exactly the same Confidence-on-every-claim rule as the rest of the system.

Don't add these in Phase 2 / 3 even if it feels easy. The schema is frozen until then; the causality engine is its own design pass.

## Reviewing existing code

Run `/check-tier` (LLM call hygiene) and inspect with `asil graph stats` / `asil graph neighbors <qname>`. For a new label, also run the integration tests in `tests/integration/test_graph_builder.py` — they pin schema invariants.
