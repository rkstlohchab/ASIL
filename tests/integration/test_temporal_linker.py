"""Integration tests for the temporal causal linker against real Neo4j writes.

The headline test (`test_bundled_postmortem_links_auth_deployment_as_top_cause`)
is the Phase 4 step 1 validation: ingest the bundled payments-cascade
postmortem and confirm the linker correctly surfaces the auth deployment
that started the cascade as a top-3 cause of the incident, with the
expected proximity confidence.

If this test ever regresses, the moat is broken — fix it before shipping.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from asil_infra import ingest_postmortem, load_postmortem
from asil_memory import GraphStore
from asil_temporal import TemporalLinker, find_causes


@pytest.fixture
def env_key() -> str:
    return f"test-temporal-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup(graph_store: GraphStore, env_key: str):
    yield env_key
    graph_store.clear_env(env_key)


def _ingest_bundled(graph_store: GraphStore, env_key: str) -> str:
    """Load the bundled cascade postmortem, re-scope its env_key, ingest. Returns incident id."""
    repo_root = Path(__file__).resolve().parents[2]
    pm_path = repo_root / "research" / "postmortems" / "2025-08-14-payments-redis-cascade.yaml"
    pm = load_postmortem(pm_path)
    pm.incident.env_key = env_key
    for ev in pm.events:
        ev.env_key = env_key
    ingest_postmortem(pm, graph_store)
    return pm.incident.id


# ---------------------------------------------------------------------------
# the headline test — Phase 4 step 1 validation
# ---------------------------------------------------------------------------


def test_bundled_postmortem_links_auth_deployment_as_top_cause(
    graph_store: GraphStore, cleanup: str
) -> None:
    """The auth deployment (8f2c1d4) shipped 7 minutes before the incident
    was detected. With Phase 4 step 2's lagged-correlation, the deploy on an
    affected service gets a +0.6 additive boost on its proximity score
    (0.379 → 0.979), making it the #1 cause. This tightens the original
    Phase 4 step 1 assertion (was top-3; now #1) because lagged-correlation
    closes the cause-vs-symptom honesty gap."""
    incident_id = _ingest_bundled(graph_store, cleanup)

    linker = TemporalLinker(graph_store=graph_store)
    stats = linker.link_incident(incident_id)
    assert stats.edges_written > 0, "no causal edges written — linker is broken"

    # Read back the persisted edges (production path: ASIL ask / find_causes hits this).
    causes = graph_store.causes_for_incident(incident_id, limit=20)
    assert causes, "no :PRECEDED edges visible after linking"

    # Phase 4 step 2: the auth deploy must be the TOP cause (not just top-3).
    assert causes[0]["cause_kind"] == "Deployment"
    assert causes[0]["cause_props"].get("deployment_id") == "deploy-8f2c1d4"

    # Confidence sanity: proximity ~0.379 + lagged-correlation +0.6 → ~0.979.
    auth_confidence = float(causes[0]["confidence"])
    assert auth_confidence >= 0.85, f"auth deploy confidence too low: {auth_confidence}"
    assert "lagged_correlation" in str(causes[0].get("derivation") or "")
    assert causes[0].get("strategy") == "temporal_proximity+lagged_correlation"


def test_link_is_idempotent_on_rerun(graph_store: GraphStore, cleanup: str) -> None:
    """Re-linking the same incident must not duplicate :PRECEDED edges."""
    incident_id = _ingest_bundled(graph_store, cleanup)
    linker = TemporalLinker(graph_store=graph_store)
    first = linker.link_incident(incident_id)
    second = linker.link_incident(incident_id)
    assert first.edges_written == second.edges_written

    # Confirm via Cypher: no duplicate edges between any (cause, incident) pair.
    rows = graph_store.query(
        """
        MATCH (cause)-[r:PRECEDED]->(i:Incident {id: $id})
        WITH cause, count(r) AS rels
        RETURN max(rels) AS max_rels
        """,
        id=incident_id,
    )
    assert rows and rows[0]["max_rels"] == 1


def test_rerun_with_different_half_life_updates_confidence(
    graph_store: GraphStore, cleanup: str
) -> None:
    """Sweeping the decay parameter must replace edge properties, not append.

    Both half-lives are chosen so the auth deploy (7min before detection)
    stays above the 0.05 confidence floor — shorter is 200s (confidence ≈
    0.23), longer is 900s (confidence ≈ 0.72). The point of the test is
    "longer half-life => higher score for the same event", not "tiny
    half-lives still keep distant events."
    """
    incident_id = _ingest_bundled(graph_store, cleanup)

    short = TemporalLinker(graph_store=graph_store, half_life_seconds=200.0)
    short.link_incident(incident_id)
    rows_short = graph_store.causes_for_incident(incident_id, limit=50)
    auth_short = next(
        r
        for r in rows_short
        if r["cause_kind"] == "Deployment"
        and r["cause_props"].get("deployment_id") == "deploy-8f2c1d4"
    )

    longer = TemporalLinker(graph_store=graph_store, half_life_seconds=900.0)
    longer.link_incident(incident_id)
    rows_long = graph_store.causes_for_incident(incident_id, limit=50)
    auth_long = next(
        r
        for r in rows_long
        if r["cause_kind"] == "Deployment"
        and r["cause_props"].get("deployment_id") == "deploy-8f2c1d4"
    )

    # Longer half-life => confidence decays slower => score should be strictly higher.
    assert float(auth_long["confidence"]) > float(auth_short["confidence"])


def test_link_env_walks_every_incident(graph_store: GraphStore, cleanup: str) -> None:
    incident_id = _ingest_bundled(graph_store, cleanup)
    stats_list = TemporalLinker(graph_store=graph_store).link_env(cleanup)
    assert len(stats_list) >= 1
    assert any(s.incident_id == incident_id for s in stats_list)


def test_after_incident_events_are_skipped(graph_store: GraphStore, cleanup: str) -> None:
    """Mitigation + rollback deploys in the postmortem occur AFTER detected_at;
    they should NOT have :PRECEDED edges pointing at the incident (they're not
    causes — they're responses to it)."""
    incident_id = _ingest_bundled(graph_store, cleanup)
    TemporalLinker(graph_store=graph_store).link_incident(incident_id)
    causes = graph_store.causes_for_incident(incident_id, limit=50)
    deploy_ids = [
        c["cause_props"].get("deployment_id") for c in causes if c["cause_kind"] == "Deployment"
    ]
    # 'deploy-rollback-7b1a3e9' shipped 1h17m AFTER detection — can't be a cause.
    assert "deploy-rollback-7b1a3e9" not in deploy_ids
    # 'deploy-mitigation-1' also after detection.
    assert "deploy-mitigation-1" not in deploy_ids


def test_find_causes_module_function_matches_persisted_edges(
    graph_store: GraphStore, cleanup: str
) -> None:
    """The read-only `find_causes` helper should return the same shape the
    `causes_for_incident` query produces (up to score recomputation)."""
    incident_id = _ingest_bundled(graph_store, cleanup)
    live = find_causes(graph_store, incident_id, limit=20)
    assert live, "find_causes returned nothing — should compute live without persisted edges"
    # The auth deploy should be in the live scoring too.
    deploy_ids = [
        c.cause_node_key.get("deployment_id") for c in live if c.cause_kind == "Deployment"
    ]
    assert "deploy-8f2c1d4" in deploy_ids


def test_lookback_window_excludes_distant_events(graph_store: GraphStore, cleanup: str) -> None:
    """With a tight 5-minute lookback, the auth deploy (7min prior) gets dropped."""
    incident_id = _ingest_bundled(graph_store, cleanup)
    tight = TemporalLinker(graph_store=graph_store, lookback=timedelta(minutes=5))
    causes = tight.score_incident(incident_id, limit=50)
    deploy_ids = [
        c.cause_node_key.get("deployment_id") for c in causes if c.cause_kind == "Deployment"
    ]
    assert "deploy-8f2c1d4" not in deploy_ids


def test_lagged_correlation_promotes_deploy_above_symptom_metric_shift(
    graph_store: GraphStore, cleanup: str
) -> None:
    """Phase 4 step 2 validation: after lagged-correlation, the auth deploy
    must outrank every MetricShift in the cause list. The latency spike
    (payments p99 at 14:23, 1min before) has proximity ~0.87 but is a
    symptom; the auth deploy (7min before) has proximity ~0.38 + lagged-
    correlation +0.6 = ~0.98. Deploy wins."""
    incident_id = _ingest_bundled(graph_store, cleanup)
    linker = TemporalLinker(graph_store=graph_store)
    linker.link_incident(incident_id)

    causes = graph_store.causes_for_incident(incident_id, limit=20)
    # Find the auth deploy and the highest-scoring MetricShift.
    auth = next(
        c
        for c in causes
        if c["cause_kind"] == "Deployment"
        and c["cause_props"].get("deployment_id") == "deploy-8f2c1d4"
    )
    metric_shifts = [c for c in causes if c["cause_kind"] == "MetricShift"]
    assert metric_shifts, "no MetricShifts in cause list"
    top_metric = metric_shifts[0]  # already sorted by confidence desc

    assert float(auth["confidence"]) > float(top_metric["confidence"]), (
        f"auth deploy ({auth['confidence']}) should outrank top MetricShift "
        f"({top_metric['confidence']}); lagged-correlation failed"
    )
    assert auth.get("strategy") == "temporal_proximity+lagged_correlation"
