"""Unit tests for the temporal causality scoring math.

The linker's logic is testable in isolation by faking the graph store —
the scoring function (`_exp_decay`), the per-row dispatch
(`_summarize`), and the candidate filtering (after-incident drop,
confidence-floor drop) all run without a Neo4j round-trip.

Integration tests cover the actual Cypher writes; they live in
`tests/integration/test_temporal_linker_ingest.py`.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import pytest
from asil_temporal.linker import (
    _EXPLICIT_REFERENCE_BONUS,
    _LAGGED_CORRELATION_BONUS,
    CausalCandidate,
    TemporalLinker,
    _apply_explicit_reference,
    _apply_lagged_correlation,
    _exp_decay,
    _summarize,
    find_causes,
)

# ---------------------------------------------------------------------------
# the decay function
# ---------------------------------------------------------------------------


def test_decay_returns_one_for_event_at_incident_time() -> None:
    assert _exp_decay(0.0, half_life=300.0) == pytest.approx(1.0)


def test_decay_halves_at_one_half_life() -> None:
    # 5 minutes (default half-life) → confidence 0.5
    assert _exp_decay(300.0, half_life=300.0) == pytest.approx(0.5, rel=1e-6)


def test_decay_quarters_at_two_half_lives() -> None:
    assert _exp_decay(600.0, half_life=300.0) == pytest.approx(0.25, rel=1e-6)


def test_decay_is_monotonic_decreasing_with_delta() -> None:
    prev = 1.0
    for dt in (10, 60, 120, 300, 600, 1200, 3600):
        cur = _exp_decay(float(dt), half_life=300.0)
        assert cur < prev
        prev = cur


def test_decay_caps_at_one_for_negative_delta() -> None:
    # Defensive: negative delta means event is AFTER incident; we drop it
    # upstream but the decay function shouldn't blow up.
    assert _exp_decay(-100.0, half_life=300.0) == 1.0


def test_decay_respects_custom_half_life() -> None:
    # With a 60s half-life, 60s out should be 0.5; with 300s, 60s out is much higher
    short = _exp_decay(60.0, half_life=60.0)
    long_ = _exp_decay(60.0, half_life=300.0)
    assert short == pytest.approx(0.5, rel=1e-6)
    assert long_ > 0.8  # exp(-ln2 * 60/300) = exp(-0.139) ≈ 0.87
    assert long_ > short


# ---------------------------------------------------------------------------
# _summarize: row → (node_key, label, derivation)
# ---------------------------------------------------------------------------


def test_summarize_deployment_produces_human_readable_label() -> None:
    row = {
        "kind": "Deployment",
        "deployment_id": "deploy-8f2c1d4",
        "service_name": "auth",
        "commit_sha": "8f2c1d4",
    }
    node_key, label, derivation = _summarize(row, delta_seconds=420.0, confidence=0.38)
    assert node_key["deployment_id"] == "deploy-8f2c1d4"
    assert "deploy-8f2c1d4" in label
    assert "auth" in label
    assert "7.0min before" in derivation
    assert "0.380" in derivation
    assert "temporal_proximity" in derivation


def test_summarize_metric_shift_includes_before_after_in_label() -> None:
    row = {
        "kind": "MetricShift",
        "service_name": "payments",
        "metric": "p99",
        "at": "2026-04-12T14:23:00+00:00",
        "before": 120.0,
        "after": 4200.0,
        "unit": "ms",
    }
    node_key, label, _ = _summarize(row, delta_seconds=60.0, confidence=0.87)
    assert node_key == {
        "service_name": "payments",
        "metric": "p99",
        "started_at": "2026-04-12T14:23:00+00:00",
    }
    assert "120.0ms → 4200.0ms" in label
    assert "payments" in label


def test_summarize_log_signature_clips_long_signatures() -> None:
    row = {
        "kind": "LogSignature",
        "service_name": "payments",
        "signature_hash": "abc12345",
        "signature": "Redis connection timeout after 5000ms; pool exhausted; "
        "waiters=812; max_size=64; this is a really long signature",
    }
    _, label, _ = _summarize(row, delta_seconds=30.0, confidence=0.9)
    assert label.endswith('…" on payments')
    assert len(label) < 120


def test_summarize_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown cause kind"):
        _summarize({"kind": "Mystery"}, delta_seconds=10.0, confidence=0.5)


def test_summarize_uses_seconds_for_sub_minute_deltas() -> None:
    row = {"kind": "Deployment", "deployment_id": "x", "service_name": "y"}
    _, _, derivation = _summarize(row, delta_seconds=42.0, confidence=0.99)
    assert "42s before" in derivation


# ---------------------------------------------------------------------------
# TemporalLinker.score_incident — using a fake graph store
# ---------------------------------------------------------------------------


class FakeGraphStore:
    """Pre-canned responses for the linker's three Cypher queries
    (incident lookup, candidate fetch, edge writes)."""

    def __init__(
        self,
        incident: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> None:
        self._incident = incident
        self._candidates = candidates
        self.write_calls: list[dict[str, Any]] = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        # Dispatch by feature keywords in the query — robust against whitespace.
        c = " ".join(cypher.split())
        if "MATCH (i:Incident {id: $id})" in c and "RETURN i.id AS id" in c:
            return [self._incident] if self._incident else []
        if "MATCH (d:Deployment" in c and "RETURN 'Deployment'" in c:
            return [r for r in self._candidates if r.get("kind") == "Deployment"]
        if "MATCH (m:MetricShift" in c and "RETURN 'MetricShift'" in c:
            return [r for r in self._candidates if r.get("kind") == "MetricShift"]
        if "MATCH (l:LogSignature" in c and "RETURN 'LogSignature'" in c:
            return [r for r in self._candidates if r.get("kind") == "LogSignature"]
        if "DELETE r" in c:
            return []
        # an edge-write call
        self.write_calls.append({"cypher": c, "params": params})
        return []


def _incident(
    *,
    id: str = "INC-test",
    env: str = "prod",
    detected_at: str = "2026-04-12T14:24:00+00:00",
    affected_services: list[str] | None = None,
    summary: str = "",
) -> dict[str, Any]:
    return {
        "id": id,
        "env_key": env,
        "detected_at": detected_at,
        "title": "test",
        "affected_services": affected_services or [],
        "summary": summary,
    }


def _deploy(
    *, at: str, id_: str = "d1", svc: str = "auth", commit_sha: str | None = None
) -> dict[str, Any]:
    return {
        "kind": "Deployment",
        "at": at,
        "deployment_id": id_,
        "service_name": svc,
        "description": None,
        "commit_sha": commit_sha,
    }


def test_score_incident_orders_candidates_by_confidence_desc() -> None:
    # incident at 14:24:00. All three deploys are within the 5min half-life
    # confidence floor; the test pins the descending order, not survival count.
    gs = FakeGraphStore(
        incident=_incident(),
        candidates=[
            _deploy(at="2026-04-12T14:23:00+00:00", id_="very-close"),  # 60s before → ~0.87
            _deploy(at="2026-04-12T14:19:00+00:00", id_="medium"),  # 5min before → 0.5
            _deploy(at="2026-04-12T14:14:00+00:00", id_="further"),  # 10min before → 0.25
        ],
    )
    linker = TemporalLinker(graph_store=gs)
    scored = linker.score_incident("INC-test")
    assert [c.cause_node_key["deployment_id"] for c in scored] == [
        "very-close",
        "medium",
        "further",
    ]
    assert scored[0].confidence > scored[1].confidence > scored[2].confidence


def test_score_incident_drops_events_after_incident() -> None:
    gs = FakeGraphStore(
        incident=_incident(detected_at="2026-04-12T14:24:00+00:00"),
        candidates=[
            _deploy(at="2026-04-12T14:30:00+00:00", id_="after-incident"),  # 6min AFTER
            _deploy(at="2026-04-12T14:20:00+00:00", id_="before-incident"),  # 4min BEFORE
        ],
    )
    scored = TemporalLinker(graph_store=gs).score_incident("INC-test")
    ids = [c.cause_node_key["deployment_id"] for c in scored]
    assert "after-incident" not in ids
    assert "before-incident" in ids


def test_score_incident_drops_below_confidence_floor() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        candidates=[
            _deploy(at="2026-04-12T08:00:00+00:00", id_="6.4h-before"),  # confidence ≈ 1e-23
        ],
    )
    linker = TemporalLinker(graph_store=gs, min_confidence=0.05)
    scored = linker.score_incident("INC-test")
    assert scored == []  # below the floor


def test_score_incident_returns_empty_when_incident_missing() -> None:
    gs = FakeGraphStore(incident=None, candidates=[])
    assert TemporalLinker(graph_store=gs).score_incident("nope") == []


def test_score_incident_respects_limit() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        candidates=[
            _deploy(at=f"2026-04-12T14:{23 - i:02d}:00+00:00", id_=f"d{i}") for i in range(5)
        ],
    )
    scored = TemporalLinker(graph_store=gs).score_incident("INC-test", limit=3)
    assert len(scored) == 3


# ---------------------------------------------------------------------------
# find_causes (module-level convenience) just wraps the linker
# ---------------------------------------------------------------------------


def test_find_causes_passes_through_filters() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        candidates=[_deploy(at="2026-04-12T14:23:00+00:00", id_="d1")],
    )
    out = find_causes(gs, "INC-test", lookback=timedelta(hours=1), half_life_seconds=60.0)
    # 60s before, half-life 60s → confidence ≈ 0.5
    assert len(out) == 1
    assert out[0].confidence == pytest.approx(0.5, rel=1e-2)


# ---------------------------------------------------------------------------
# CausalCandidate dataclass surface
# ---------------------------------------------------------------------------


def test_causal_candidate_default_state() -> None:
    c = CausalCandidate(
        cause_kind="Deployment",
        cause_node_key={"deployment_id": "x"},
        cause_label="x",
        delta_seconds=60.0,
        confidence=0.5,
        derivation="test",
    )
    assert c.cause_kind == "Deployment"
    assert c.delta_seconds == 60.0
    assert math.isclose(c.confidence, 0.5)


# ---------------------------------------------------------------------------
# lagged-correlation boost (Phase 4 step 2)
# ---------------------------------------------------------------------------


def _candidate(
    *,
    kind: str = "Deployment",
    deployment_id: str = "d1",
    service_name: str = "auth",
    confidence: float = 0.38,
    derivation: str = "temporal_proximity: ...",
) -> CausalCandidate:
    """Build a CausalCandidate for lagged-correlation tests."""
    return CausalCandidate(
        cause_kind=kind,
        cause_node_key={"deployment_id": deployment_id, "service_name": service_name},
        cause_label=f"Deployment {deployment_id} on {service_name}",
        delta_seconds=420.0,
        confidence=confidence,
        derivation=derivation,
    )


def test_lagged_correlation_boosts_deploy_on_affected_service() -> None:
    c = _candidate(service_name="auth", confidence=0.38)
    incident = {"affected_services": ["auth", "payments", "cart"]}
    result = _apply_lagged_correlation(c, incident)
    assert result.confidence == pytest.approx(0.38 + _LAGGED_CORRELATION_BONUS, rel=1e-6)
    assert "lagged_correlation" in result.strategy
    assert result.strategy == "temporal_proximity+lagged_correlation"
    assert "lagged_correlation" in result.derivation
    assert "affected service 'auth'" in result.derivation


def test_lagged_correlation_does_not_boost_metric_shift() -> None:
    c = CausalCandidate(
        cause_kind="MetricShift",
        cause_node_key={"service_name": "payments", "metric": "p99", "started_at": "..."},
        cause_label="MetricShift p99 on payments",
        delta_seconds=60.0,
        confidence=0.87,
        derivation="temporal_proximity: ...",
    )
    incident = {"affected_services": ["auth", "payments", "cart"]}
    result = _apply_lagged_correlation(c, incident)
    # MetricShifts are symptoms; no boost.
    assert result.confidence == 0.87
    assert result.strategy == "temporal_proximity"


def test_lagged_correlation_does_not_boost_deploy_not_on_affected() -> None:
    c = _candidate(service_name="unrelated", confidence=0.5)
    incident = {"affected_services": ["auth", "payments"]}
    result = _apply_lagged_correlation(c, incident)
    assert result.confidence == 0.5
    assert result.strategy == "temporal_proximity"


def test_lagged_correlation_caps_at_one() -> None:
    c = _candidate(service_name="auth", confidence=0.95)
    incident = {"affected_services": ["auth"]}
    result = _apply_lagged_correlation(c, incident)
    # 0.95 + 0.6 = 1.55 → capped at 1.0
    assert result.confidence == 1.0
    assert "lagged_correlation" in result.strategy


def test_score_incident_applies_lagged_correlation() -> None:
    """End-to-end through FakeGraphStore: a deploy 7min before on an affected
    service should outrank a MetricShift 1min before."""
    gs = FakeGraphStore(
        incident=_incident(affected_services=["auth", "payments", "cart"]),
        candidates=[
            # auth deploy 7min before → proximity ~0.38
            _deploy(at="2026-04-12T14:17:00+00:00", id_="deploy-8f2c1d4", svc="auth"),
            # payments metric shift 1min before → proximity ~0.87
            {
                "kind": "MetricShift",
                "at": "2026-04-12T14:23:00+00:00",
                "service_name": "payments",
                "metric": "p99",
                "before": 120.0,
                "after": 4200.0,
                "unit": "ms",
            },
        ],
    )
    linker = TemporalLinker(graph_store=gs)
    scored = linker.score_incident("INC-test")
    # The deploy should be #1 now (boosted by lagged-correlation)
    assert scored[0].cause_kind == "Deployment"
    assert scored[0].cause_node_key["deployment_id"] == "deploy-8f2c1d4"
    assert scored[0].confidence >= 0.85
    assert "lagged_correlation" in scored[0].strategy
    # The metric shift should be #2 (proximity only)
    assert scored[1].cause_kind == "MetricShift"
    assert scored[1].strategy == "temporal_proximity"


# ---------------------------------------------------------------------------
# explicit-reference boost (Phase 4 step 3)
# ---------------------------------------------------------------------------


def test_explicit_reference_boosts_when_deploy_id_in_summary() -> None:
    c = _candidate(service_name="auth", confidence=0.5)
    c.cause_node_key["deployment_id"] = "deploy-abc123"
    c.cause_node_key["commit_sha"] = ""
    incident = {
        "summary": "Root cause was deploy-abc123 which broke Redis.",
        "affected_services": [],
    }
    result = _apply_explicit_reference(c, incident)
    assert result.confidence == pytest.approx(min(1.0, 0.5 + _EXPLICIT_REFERENCE_BONUS))
    assert "explicit_reference" in result.strategy
    assert "deployment_id 'deploy-abc123'" in result.derivation


def test_explicit_reference_boosts_when_commit_sha_in_summary() -> None:
    c = _candidate(service_name="auth", confidence=0.4)
    c.cause_node_key["deployment_id"] = "deploy-xyz"
    c.cause_node_key["commit_sha"] = "8f2c1d4"
    incident = {"summary": "Auth deployment 8f2c1d4 caused the cascade.", "affected_services": []}
    result = _apply_explicit_reference(c, incident)
    assert result.confidence == pytest.approx(min(1.0, 0.4 + _EXPLICIT_REFERENCE_BONUS))
    assert "explicit_reference" in result.strategy
    assert "commit_sha '8f2c1d4'" in result.derivation


def test_explicit_reference_no_match_leaves_unchanged() -> None:
    c = _candidate(service_name="auth", confidence=0.5)
    c.cause_node_key["deployment_id"] = "deploy-xyz"
    c.cause_node_key["commit_sha"] = "abc123"
    incident = {"summary": "Something unrelated happened.", "affected_services": []}
    result = _apply_explicit_reference(c, incident)
    assert result.confidence == 0.5
    assert "explicit_reference" not in result.strategy


def test_explicit_reference_caps_at_one() -> None:
    c = _candidate(service_name="auth", confidence=0.9)
    c.cause_node_key["deployment_id"] = "deploy-boom"
    c.cause_node_key["commit_sha"] = ""
    incident = {"summary": "deploy-boom was the root cause", "affected_services": []}
    result = _apply_explicit_reference(c, incident)
    assert result.confidence == 1.0


def test_explicit_reference_skips_non_deployment() -> None:
    c = CausalCandidate(
        cause_kind="MetricShift",
        cause_node_key={"service_name": "payments", "metric": "p99", "started_at": "x"},
        cause_label="MetricShift payments.p99",
        delta_seconds=60.0,
        confidence=0.87,
        derivation="temporal_proximity: ...",
    )
    incident = {"summary": "p99 was mentioned but shouldn't boost.", "affected_services": []}
    result = _apply_explicit_reference(c, incident)
    assert result.confidence == 0.87


def test_score_incident_applies_all_three_strategies() -> None:
    """End-to-end: a deploy on an affected service whose commit SHA appears
    in the summary should get both lagged-correlation AND explicit-reference boosts."""
    gs = FakeGraphStore(
        incident=_incident(
            affected_services=["auth", "payments"],
            summary="Auth deployment 8f2c1d4 broke Redis pool.",
        ),
        candidates=[
            _deploy(
                at="2026-04-12T14:17:00+00:00",
                id_="deploy-8f2c1d4",
                svc="auth",
                commit_sha="8f2c1d4",
            ),
        ],
    )
    linker = TemporalLinker(graph_store=gs)
    scored = linker.score_incident("INC-test")
    assert len(scored) == 1
    c = scored[0]
    # Should have all three strategies
    assert "temporal_proximity" in c.strategy
    assert "lagged_correlation" in c.strategy
    assert "explicit_reference" in c.strategy
    # Confidence should be capped at 1.0 (0.379 + 0.6 + 0.8 > 1.0)
    assert c.confidence == 1.0
