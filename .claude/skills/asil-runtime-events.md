---
name: asil-runtime-events
description: Use when adding new runtime event sources (K8s adapter, Prometheus adapter, Loki adapter, postmortem importers, custom event ingestors), or when extending the runtime graph schema. Enforces the namespace split between code and runtime, the typed-event contract, and the "no causal edges in Phase 3" rule.
---

# asil-runtime-events

Phase 3 added a parallel **runtime namespace** to the graph. Code nodes live
under `repo_key`; runtime nodes (`Service`, `Deployment`, `MetricShift`,
`LogSignature`, `Incident`) live under `env_key`. Cross-namespace edges
(`Service-[:RUNS]->File`, `Deployment-[:SHIPPED]->Commit`) connect the two
halves. Keep the namespaces separate by default; cross only at the explicit
bridge points.

## The Phase 3 runtime schema (don't deviate without a design doc)

**Nodes (all carry `env_key` + `source` + `confidence`):**
- `Service {env_key, name, repo_key?, file_paths?}` — identity `(env_key, name)`
- `Deployment {env_key, deployment_id, service_name, at, commit_sha?, description?}` — identity `(env_key, deployment_id)`
- `MetricShift {env_key, service_name, metric, started_at, ended_at?, before?, after?, unit?, description?}` — identity `(env_key, service_name, metric, started_at)`
- `LogSignature {env_key, service_name, signature, signature_hash, first_seen_at, last_seen_at?, count, level?}` — identity `(env_key, service_name, signature_hash)`
- `Incident {id, env_key, title, severity, detected_at, resolved_at?, summary?, affected_services[]}` — identity `(id,)` (globally unique)

**Edges (Phase 3 only — no causal edges yet):**
- `(:Deployment)-[:DEPLOYED]->(:Service)`
- `(:Deployment)-[:SHIPPED]->(:Commit)` (when the commit node exists in the repo namespace)
- `(:MetricShift)-[:OBSERVED_IN]->(:Service)`
- `(:LogSignature)-[:EMITTED_BY]->(:Service)`
- `(:Incident)-[:AFFECTED]->(:Service)`
- `(:Service)-[:RUNS]->(:File)` (when `Service.repo_key` + `file_paths` are set)

Defined in [packages/asil_memory/asil_memory/graph_store.py](../../packages/asil_memory/asil_memory/graph_store.py) (constraints + merge methods) and [packages/asil_infra/asil_infra/models.py](../../packages/asil_infra/asil_infra/models.py) (typed event surface).

## Hard rules

1. **Every runtime event carries `timestamp`, `source`, `confidence`.** `timestamp` becomes one of `at` / `started_at` / `first_seen_at` / `detected_at` depending on label; `source` records which adapter or postmortem produced it; `confidence` is 1.0 for human-authored postmortems and lower for noisy automated extraction (e.g. log clustering should land around 0.6-0.8).

2. **Identity is observable.** No surrogate UUIDs. The MERGE keys in the schema are the identity. Re-ingesting the same postmortem twice creates zero new nodes — that's the test in `test_reingest_is_idempotent`.

3. **No causal edges in Phase 3.** `(:Deployment)-[:PRECEDED]->(:Incident)` and `(:MetricShift)-[:CORRELATED_WITH]->(:Commit)` are Phase 4's moat. Don't write them from a Phase 3 ingestor — the heuristics that compute them belong in `asil_temporal`. Phase 3 lays the substrate; Phase 4 reasons over it.

4. **Code namespace and runtime namespace are isolated.** `clear_env(env_key)` is the runtime-side analogue of `clear_repo(repo_key)`. Neither touches the other's nodes. The test `test_clear_env_leaves_code_nodes_untouched` is load-bearing — don't break it.

5. **MERGE, never CREATE.** Same rule as the code namespace. `merge_service`, `merge_deployment`, etc. all upsert idempotently.

## How to add a new event source (K8s / Prom / Loki adapter)

1. Build typed events from the source's raw data — return instances of `Service`, `Deployment`, `MetricShift`, `LogSignature`, or `Incident`. Don't invent new label types without a design doc; the Phase 4 causality engine knows these five.
2. Set `source` to a URI-like string: `k8s://prod`, `prometheus://staging-eu`, `loki://prod`. Makes provenance traceable when a wrong observation shows up.
3. Call the appropriate `store.merge_*` method per event. They're idempotent; re-running a pull-loop on the same data is safe.
4. For metric shifts derived from raw time series, run change-point detection (PELT or BOCPD) before emitting — don't store one MetricShift per data point.
5. For log signatures, cluster first (e.g. drain3) and emit one LogSignature per cluster with a `count`. Don't emit per-line.

## Adding a new field to an existing label

Same rule as the code schema: add the column to the merge method's prop dict, write the field on every adapter, document the field in the model dataclass. There is no migration framework yet — additions are additive (Cypher just stores the new property), but removals or type changes require a manual `MATCH ... SET n.foo = null` or similar.

## Forbidden patterns

```python
# ❌ Inventing new labels
store.query("MERGE (a:Anomaly {...})")
# Use MetricShift or LogSignature. New labels need a design-decision entry.

# ❌ Skipping confidence
event = Deployment(env_key="prod", deployment_id="d1", service_name="x", at=now,
                   source="k8s://prod")  # no confidence → defaults to 1.0
# 1.0 is fine for postmortems but wrong for noisy auto-extracted data. Set it
# explicitly when emitting from an automated adapter.

# ❌ Writing causal edges from a Phase 3 ingestor
store.query("MATCH (d:Deployment), (i:Incident) MERGE (d)-[:PRECEDED]->(i)")
# Phase 4's asil_temporal owns causal edges. Phase 3 events are inputs to that.

# ❌ Cross-namespace writes outside the documented bridges
store.query("MATCH (svc:Service), (cls:Class) MERGE (svc)-[:WEIRD_LINK]->(cls)")
# RUNS (Service→File) and SHIPPED (Deployment→Commit) are the only bridges.
# Anything else needs a design doc.
```

## Testing

- Loader / parser logic: unit tests with hand-written YAML fragments via `tmp_path`.
- Graph writes: integration tests scoped to a unique `env_key` per test, cleaned up via `clear_env(env_key)`. See `tests/integration/test_postmortem_ingest.py` for the pattern.
- The bundled example postmortem in `research/postmortems/` must always load cleanly — it's the demo data and the Phase 4 eval seed corpus.

## When the K8s / Prom / Loki adapters land (Phase 3 step 2+)

The adapters will be opt-in via the `[project.optional-dependencies]` groups
in `packages/asil_infra/pyproject.toml` (`k8s`, `prom`, `loki`). Users who
only want the postmortem path don't need to pull the heavy clients. Each
adapter ships as a separate module (`k8s_adapter.py`, `prom_adapter.py`,
`loki_adapter.py`) and emits the same typed events the postmortem loader
produces — the graph writer doesn't care which source produced an event.
