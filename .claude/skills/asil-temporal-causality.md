---
name: asil-temporal-causality
description: Use when working on the causality engine in `packages/asil_temporal` — adding heuristics, modifying scoring math, persisting causal edges, or querying `(:Cause)-[:PRECEDED]->(:Incident)`. Enforces the no-LLM-causality rule, the derivation-always-logged rule, and the cause-vs-symptom honesty that distinguishes Phase 4 step 1 from step 2.
---

# asil-temporal-causality

This is **THE MOAT** — the layer that turns ASIL from "RAG with a graph"
into "engineering intelligence that explains why things happened." Every
causal claim ships with confidence, a delta, a derivation string, and an
edge in the graph. No hallucinated causes, no opaque scores, no
hand-waving about whether something "looks related."

## What lives where

- `packages/asil_temporal/asil_temporal/linker.py` — `TemporalLinker`,
  `find_causes()`, the decay function, the `_PRECEDED_CYPHER_BY_KIND`
  template registry.
- `packages/asil_memory/asil_memory/graph_store.py` — `causes_for_incident()`
  read helper. New `PRECEDED` edge constraint will land here in step 2+
  (multi-strategy edges need a uniqueness rule).

## Hard rules

1. **Causality is observable, not predicted.** Proximity, correlation,
   explicit reference — all observable signals you can compute
   deterministically from graph state. LLM-emitted causality is FORBIDDEN
   from this layer. If a future contributor proposes "have GPT decide what
   caused the incident," reject the PR and point at this rule.

2. **Every `:PRECEDED` edge carries `confidence`, `delta_seconds`,
   `derivation`, `strategy`.** No exceptions. The CLI's `--read` mode and
   the `asil.find_causes` MCP tool both surface these to humans / agents
   downstream; missing a field breaks the trust contract.

3. **MERGE the edges; never CREATE.** Re-running the linker with a tweaked
   half-life replaces the edge properties, not adds duplicate edges. The
   `test_link_is_idempotent_on_rerun` test pins this.

4. **Drop events at or after the incident's `detected_at`.** Mitigation
   deploys, rollbacks, recovery metric shifts — they're responses, not
   causes. `_score()` returns `None` when `delta_seconds <= 0`. The
   `test_after_incident_events_are_skipped` test pins it.

5. **Confidence floor of 0.05 by default.** Below that, edges are noise
   that pollute every causality query. Tune the floor via constructor
   arg if you're running an eval that needs to see weak links, but the
   default is conservative on purpose.

6. **Single half-life for now (5 minutes default).** Per-incident-class
   tuning is Phase 4 step 3+ work. Don't bolt knobs on per-strategy
   without a design doc — the simplicity is load-bearing for the eval.

## The cause-vs-symptom honesty

Phase 4 step 1 ranked candidates by **temporal proximity alone**, which had
a known limitation: a metric shift 1 minute before an incident outranked
the deployment 7 minutes before — even when the deployment was the real
cause. **Phase 4 step 2 (shipped) fixes this** with the lagged-correlation
strategy:

- **Lagged-correlation boost:** after proximity scoring, Deployments whose
  `service_name` appears in the incident's `affected_services` list receive
  an additive **+0.6** confidence bonus (capped at 1.0). This promotes
  deploys on affected services from symptom-tier to cause-tier. On the
  bundled postmortem: auth deploy goes from 0.379 (proximity only) to
  0.979 (proximity + lagged-correlation), correctly outranking the payments
  latency spike at 0.871. The bonus is calibrated against this ground truth;
  changing `_LAGGED_CORRELATION_BONUS` changes the cause-vs-symptom ordering.
- **Why additive, not multiplicative:** multiplying caps you at the original
  proximity score. A deploy 7 minutes before has proximity ~0.38; even a
  2x multiplier only gets to 0.76, still below the 0.87 latency spike.
  Additive bonus treats "this deploy is on an affected service" as evidence
  orthogonal to time.
- **Observable only:** the boost comes from graph state (incident node's
  `affected_services` list vs deploy node's `service_name`), not from an
  LLM. MetricShifts and LogSignatures are explicitly NOT boosted — they're
  symptoms, not candidate causes.
- **Composite strategy string:** boosted edges carry
  `strategy: "temporal_proximity+lagged_correlation"` and the derivation
  traces both contributions.

Future Phase 4 steps will add:

- **Explicit reference:** if a commit message or postmortem summary
  mentions the incident id, the matching deploy gets a high-confidence
  edge with `strategy: "temporal_proximity+explicit_reference"`.
- **Co-occurrence reinforcement:** deploys that consistently precede the
  same metric shifts across incidents get reinforced edges.

## Forbidden patterns

```python
# ❌ Calling an LLM to "decide what caused" something
async def llm_assigns_cause(question, candidates):
    return await router.call(tier="reasoning", messages=[...])
# Causes come from the graph. The LLM consumes the causal edges; it
# doesn't author them.

# ❌ Writing an edge without confidence/derivation
store.query("MATCH (d:Deployment), (i:Incident) MERGE (d)-[:PRECEDED]->(i)")
# Every edge must carry the four props above. Use the templates in
# _PRECEDED_CYPHER_BY_KIND.

# ❌ Inventing a new edge type for a new strategy
# (cause)-[:PROBABLY_CAUSED]-> ...
# Use :PRECEDED with `strategy: "<new_name>"` in the edge properties.
# One edge type per causal relation keeps Cypher queries simple.

# ❌ Skipping the after-incident drop
# Mitigation deploys at T+30min would land in the top of the cause list.
# That breaks the demo and any downstream agent that trusts the order.

# ❌ Coupling the scorer to a specific incident kind
# The decay function is content-agnostic. Per-kind tuning (e.g. shorter
# half-life for security incidents) is config, not code branches.
```

## How to add a new strategy (Phase 4 step 2+)

1. Compute the score from observable graph state. Function signature
   should mirror the existing proximity scorer: `(candidate_row, incident)
   -> CausalCandidate | None`.
2. Add a `strategy: "<name>"` constant. The string lands on the edge as
   `r.strategy = ...`.
3. Compose strategies in `link_incident()`. Pick the strategy with the
   highest confidence for the same `(cause, incident)` pair; the others
   become extra `derivation` entries (an array property on the edge).
4. Don't change the edge type from `:PRECEDED`. Two edge types between the
   same nodes makes every downstream query branch.
5. Update the integration test on the bundled postmortem to assert the new
   strategy moves the auth deploy *above* the latency spike. That's the
   regression test for the cause-vs-symptom honesty.

## Testing

- Scoring math: unit tests with a `FakeGraphStore` that dispatches by
  Cypher keyword. See `tests/unit/test_temporal_linker.py` for the
  pattern.
- Graph writes: integration tests scoped to a unique `env_key`, ingest
  the bundled postmortem under that scope, link, assert. See
  `tests/integration/test_temporal_linker.py`.
- The bundled postmortem's auth deploy MUST appear in the top-3 causes
  of `INC-2026-04-12-payments-cascade`. This is `test_bundled_postmortem_
  links_auth_deployment_as_top_cause`. If you break it, you broke the
  moat — fix it before shipping.

## What `asil.find_causes` returns (MCP contract)

```json
{
  "incident_id": "INC-...",
  "causes": [
    {
      "cause_kind": "Deployment",
      "confidence": 0.379,
      "delta_seconds": 420.0,
      "derivation": "temporal_proximity: Deployment deploy-8f2c1d4 on auth occurred 7.0min before the incident → confidence 0.379 (half-life 5min)",
      "strategy": "temporal_proximity",
      "cause_props": {
        "deployment_id": "deploy-8f2c1d4",
        "service_name": "auth",
        "commit_sha": "8f2c1d4",
        "...": "..."
      }
    },
    ...
  ],
  "count": 2
}
```

External agents (Cursor, Claude Code, OpenHands) consume this directly.
The `derivation` field is the human-readable explanation; the
`confidence` is the number to threshold on. Together they make causal
claims auditable in a way LLM-generated text never can be.
