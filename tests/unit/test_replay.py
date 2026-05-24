"""Unit tests for the replay engine (Phase 5 step 1).

Uses a FakeGraphStore that returns canned data to test the engine's
assembly logic without Neo4j.
"""

from __future__ import annotations

from typing import Any

from asil_replay.replay import (
    IncidentReplay,
    ReplayEngine,
    ServiceCascadeEntry,
    TimelineEntry,
)

# ---------------------------------------------------------------------------
# Fake graph store
# ---------------------------------------------------------------------------


class FakeGraphStore:
    """Minimal fake for the read paths the ReplayEngine uses:
    - query()
    - events_for_service()
    - causes_for_incident()
    """

    def __init__(
        self,
        incident: dict[str, Any] | None = None,
        events: dict[str, list[dict[str, Any]]] | None = None,
        causes: list[dict[str, Any]] | None = None,
    ) -> None:
        self._incident = incident
        self._events = events or {}
        self._causes = causes or []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        if "Incident" in cypher and self._incident is not None:
            return [self._incident]
        return []

    def events_for_service(
        self, env_key: str, service_name: str, *, limit: int = 200, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self._events.get(service_name, [])

    def causes_for_incident(
        self, incident_id: str, *, min_confidence: float = 0.0, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._causes[:limit]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _incident() -> dict[str, Any]:
    return {
        "id": "INC-test",
        "env_key": "prod",
        "detected_at": "2026-04-12T14:24:00+00:00",
        "resolved_at": "2026-04-12T15:30:00+00:00",
        "title": "Payments cascade",
        "severity": "high",
        "summary": "Auth deploy caused latency cascade",
        "affected_services": ["auth", "payments"],
    }


def _events() -> dict[str, list[dict[str, Any]]]:
    return {
        "auth": [
            {
                "kind": "deployment",
                "at": "2026-04-12T14:17:00+00:00",
                "id": "deploy-8f2c1d4",
                "commit_sha": "8f2c1d4",
                "description": "Redis pool refactor",
            },
        ],
        "payments": [
            {
                "kind": "metric_shift",
                "at": "2026-04-12T14:23:00+00:00",
                "metric": "p99",
                "before": 120.0,
                "after": 4200.0,
                "unit": "ms",
            },
            {
                "kind": "incident",
                "at": "2026-04-12T14:24:00+00:00",
                "id": "INC-test",
                "title": "Payments cascade",
            },
        ],
    }


def _causes() -> list[dict[str, Any]]:
    return [
        {
            "cause_kind": "Deployment",
            "cause_props": {
                "deployment_id": "deploy-8f2c1d4",
                "service_name": "auth",
                "at": "2026-04-12T14:17:00+00:00",
            },
            "confidence": 0.979,
            "delta_seconds": 420.0,
            "derivation": "temporal_proximity + lagged_correlation",
            "strategy": "temporal_proximity+lagged_correlation",
        },
        {
            "cause_kind": "MetricShift",
            "cause_props": {
                "service_name": "payments",
                "metric": "p99",
                "started_at": "2026-04-12T14:23:00+00:00",
            },
            "confidence": 0.871,
            "delta_seconds": 60.0,
            "derivation": "temporal_proximity",
            "strategy": "temporal_proximity",
        },
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_replay_returns_none_when_incident_missing() -> None:
    gs = FakeGraphStore(incident=None)
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-nonexistent")
    assert result is None


def test_replay_assembles_full_replay() -> None:
    gs = FakeGraphStore(incident=_incident(), events=_events(), causes=_causes())
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test")
    assert result is not None
    assert isinstance(result, IncidentReplay)

    # Summary lines
    assert any("INC-test" in line for line in result.summary_lines)
    assert any("Payments cascade" in line for line in result.summary_lines)

    # Timeline
    assert len(result.timeline) >= 3  # deploy + metric_shift + incident
    assert all(isinstance(e, TimelineEntry) for e in result.timeline)
    # Sorted chronologically
    ats = [e.at for e in result.timeline]
    assert ats == sorted(ats)

    # Top causes
    assert len(result.top_causes) == 2
    assert result.top_causes[0]["cause_kind"] == "Deployment"

    # Service cascade
    assert len(result.service_cascade) >= 2
    assert all(isinstance(sc, ServiceCascadeEntry) for sc in result.service_cascade)
    # Auth should be first (earlier event)
    assert result.service_cascade[0].service == "auth"

    # Confidence
    assert result.confidence.score > 0
    assert result.confidence.evidence_count == 2


def test_replay_marks_timeline_entries() -> None:
    gs = FakeGraphStore(incident=_incident(), events=_events(), causes=_causes())
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test")
    assert result is not None

    markers = {e.marker for e in result.timeline if e.marker}
    # We should see at least cause and incident markers
    assert "↗ cause" in markers or "▶ INCIDENT" in markers


def test_replay_with_no_causes() -> None:
    gs = FakeGraphStore(incident=_incident(), events=_events(), causes=[])
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test")
    assert result is not None
    assert result.top_causes == []
    assert result.confidence.score == 0.0
    assert result.confidence.evidence_count == 0


def test_replay_with_no_events() -> None:
    gs = FakeGraphStore(incident=_incident(), events={}, causes=_causes())
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test")
    assert result is not None
    assert result.timeline == []
    assert result.service_cascade == []
    # Still has causes
    assert len(result.top_causes) == 2


def test_replay_causes_limit() -> None:
    gs = FakeGraphStore(incident=_incident(), events=_events(), causes=_causes())
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test", causes_limit=1)
    assert result is not None
    assert len(result.top_causes) == 1


def test_confidence_derivation_includes_strategy() -> None:
    gs = FakeGraphStore(incident=_incident(), events=_events(), causes=_causes())
    engine = ReplayEngine(graph_store=gs)
    result = engine.replay("INC-test")
    assert result is not None
    assert "strategies:" in result.confidence.derivation
