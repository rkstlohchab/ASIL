"""Unit tests for the state diff module.

Uses a fake graph store to test StateDiffer logic without Neo4j.
"""

from __future__ import annotations

from typing import Any

from asil_replay.state_diff import StateDiffer

# ---------------------------------------------------------------------------
# fake graph store
# ---------------------------------------------------------------------------


class FakeGraphStore:
    """Minimal shim returning canned incident + deployment + metric data."""

    def __init__(
        self,
        incident: dict[str, Any] | None = None,
        deployments: list[dict[str, Any]] | None = None,
        metric_shifts: list[dict[str, Any]] | None = None,
    ) -> None:
        self._incident = incident
        self._deployments = deployments or []
        self._metric_shifts = metric_shifts or []

    def query(self, cypher: str, **kwargs: Any) -> list[dict[str, Any]]:
        if "Incident" in cypher and "RETURN" in cypher:
            if self._incident is None:
                return []
            return [self._incident]
        if "Deployment" in cypher:
            svc = kwargs.get("svc")
            return [d for d in self._deployments if d.get("service_name") == svc]
        if "MetricShift" in cypher:
            svc = kwargs.get("svc")
            return [m for m in self._metric_shifts if m.get("service_name") == svc]
        return []


def _incident(
    *,
    detected_at: str = "2026-04-12T14:24:00+00:00",
    resolved_at: str = "2026-04-12T15:41:00+00:00",
    affected: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": "INC-test",
        "env_key": "prod",
        "detected_at": detected_at,
        "resolved_at": resolved_at,
        "affected_services": affected or ["auth", "payments"],
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_diff_returns_none_for_missing_incident() -> None:
    gs = FakeGraphStore(incident=None)
    result = StateDiffer(gs).diff("nope")
    assert result is None


def test_diff_finds_deployments_during_window() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        deployments=[
            {
                "deployment_id": "deploy-abc",
                "service_name": "auth",
                "description": "Redis pool change",
                "commit_sha": "abc123",
                "at": "2026-04-12T14:17:00+00:00",
            },
        ],
    )
    diff = StateDiffer(gs).diff("INC-test")
    assert diff is not None
    assert len(diff.deployments_during) == 1
    assert diff.deployments_during[0].deployment_id == "deploy-abc"
    assert diff.deployments_during[0].commit_sha == "abc123"


def test_diff_finds_metric_deltas() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        metric_shifts=[
            {
                "service_name": "payments",
                "metric": "p99_latency",
                "before": 120.0,
                "after": 4200.0,
                "unit": "ms",
            },
        ],
    )
    diff = StateDiffer(gs).diff("INC-test")
    assert diff is not None
    assert len(diff.metric_deltas) == 1
    assert diff.metric_deltas[0].metric == "p99_latency"
    assert diff.metric_deltas[0].before == 120.0
    assert diff.metric_deltas[0].after == 4200.0


def test_diff_returns_empty_when_no_events() -> None:
    gs = FakeGraphStore(
        incident=_incident(),
        deployments=[],
        metric_shifts=[],
    )
    diff = StateDiffer(gs).diff("INC-test")
    assert diff is not None
    assert diff.deployments_during == []
    assert diff.metric_deltas == []
    assert diff.services_involved == ["auth", "payments"]


def test_diff_includes_services_from_incident() -> None:
    gs = FakeGraphStore(
        incident=_incident(affected=["gateway", "email"]),
    )
    diff = StateDiffer(gs).diff("INC-test")
    assert diff is not None
    assert diff.services_involved == ["gateway", "email"]
