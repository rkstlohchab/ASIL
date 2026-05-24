"""Integration tests for postmortem → graph ingestion.

These require a running Neo4j (auto-skipped via conftest otherwise). Each
test scopes its writes to a unique `env_key` and cleans up after itself, so
they can run in any order without interfering.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from textwrap import dedent

import pytest
from asil_infra import ingest_postmortem, load_postmortem
from asil_memory import GraphStore


@pytest.fixture
def env_key() -> str:
    """Unique per test so we don't collide with prior runs or the live CLI."""
    return f"test-env-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup(graph_store: GraphStore, env_key: str):
    yield env_key
    graph_store.clear_env(env_key)


def _write(tmp_path: Path, env: str, body: str) -> Path:
    p = tmp_path / "pm.yaml"
    p.write_text(dedent(body).replace("__ENV__", env).lstrip("\n"))
    return p


# ---------------------------------------------------------------------------
# end-to-end writes
# ---------------------------------------------------------------------------


def test_ingest_writes_one_node_per_event_kind(
    graph_store: GraphStore, cleanup: str, tmp_path: Path
) -> None:
    p = _write(
        tmp_path,
        cleanup,
        """
        incident:
          id: INC-write-test
          title: "write test"
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          severity: low
          affected_services: [payments]
        timeline:
          - at: "2026-04-12T14:17:00Z"
            kind: deployment
            service: auth
            deployment_id: deploy-1
            commit_sha: abc123
          - at: "2026-04-12T14:23:00Z"
            kind: metric_shift
            service: payments
            metric: p99
            before: 100
            after: 4000
            unit: ms
          - at: "2026-04-12T14:24:00Z"
            kind: log_signature
            service: payments
            signature: "Redis timeout"
            count: 50
            level: error
        """,
    )
    pm = load_postmortem(p)
    stats = ingest_postmortem(pm, graph_store)

    # auth + payments materialized as Services (payments because affected_services
    # contains it; auth because it appears on a deployment).
    assert stats.services == 2
    assert stats.deployments == 1
    assert stats.metric_shifts == 1
    assert stats.log_signatures == 1

    counts = graph_store.runtime_stats(env_key=cleanup)
    assert counts["Service"] == 2
    assert counts["Deployment"] == 1
    assert counts["MetricShift"] == 1
    assert counts["LogSignature"] == 1
    assert counts["Incident"] == 1


def test_reingest_is_idempotent(graph_store: GraphStore, cleanup: str, tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        cleanup,
        """
        incident:
          id: INC-idempotent
          title: t
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          affected_services: [svc]
        timeline:
          - at: "2026-04-12T14:17:00Z"
            kind: deployment
            service: svc
            deployment_id: deploy-1
          - at: "2026-04-12T14:23:00Z"
            kind: metric_shift
            service: svc
            metric: p99
            before: 100
            after: 200
        """,
    )
    pm = load_postmortem(p)
    ingest_postmortem(pm, graph_store)
    ingest_postmortem(pm, graph_store)

    counts = graph_store.runtime_stats(env_key=cleanup)
    # MERGE-based writes => identical re-ingest creates zero additional nodes.
    assert counts["Service"] == 1
    assert counts["Deployment"] == 1
    assert counts["MetricShift"] == 1


def test_incident_affected_edges_land_on_real_service_nodes(
    graph_store: GraphStore, cleanup: str, tmp_path: Path
) -> None:
    p = _write(
        tmp_path,
        cleanup,
        """
        incident:
          id: INC-affected-edges
          title: t
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          affected_services: [payments, cart]
        timeline: []
        """,
    )
    ingest_postmortem(load_postmortem(p), graph_store)

    rows = graph_store.query(
        """
        MATCH (i:Incident {id: $id})-[:AFFECTED]->(s:Service)
        RETURN s.name AS name ORDER BY s.name
        """,
        id="INC-affected-edges",
    )
    assert [r["name"] for r in rows] == ["cart", "payments"]


def test_events_for_service_returns_chronological_view(
    graph_store: GraphStore, cleanup: str, tmp_path: Path
) -> None:
    p = _write(
        tmp_path,
        cleanup,
        """
        incident:
          id: INC-events-query
          title: t
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          affected_services: [payments]
        timeline:
          - at: "2026-04-12T14:17:00Z"
            kind: deployment
            service: payments
            deployment_id: deploy-1
            description: "first deploy"
          - at: "2026-04-12T14:23:00Z"
            kind: metric_shift
            service: payments
            metric: p99
            before: 100
            after: 4000
          - at: "2026-04-12T14:24:00Z"
            kind: log_signature
            service: payments
            signature: "first error"
            count: 1
        """,
    )
    ingest_postmortem(load_postmortem(p), graph_store)

    events = graph_store.events_for_service(env_key=cleanup, service_name="payments")
    kinds = [e["kind"] for e in events]
    # Deployment (14:17) → metric_shift (14:23) → log_signature (14:24) → incident (14:24)
    assert kinds[:3] == ["deployment", "metric_shift", "log_signature"]
    assert "incident" in kinds


def test_events_for_service_respects_since_filter(
    graph_store: GraphStore, cleanup: str, tmp_path: Path
) -> None:
    p = _write(
        tmp_path,
        cleanup,
        """
        incident:
          id: INC-since-test
          title: t
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          affected_services: [svc]
        timeline:
          - at: "2026-04-12T10:00:00Z"
            kind: deployment
            service: svc
            deployment_id: deploy-early
          - at: "2026-04-12T14:00:00Z"
            kind: deployment
            service: svc
            deployment_id: deploy-late
        """,
    )
    ingest_postmortem(load_postmortem(p), graph_store)

    after_noon = graph_store.events_for_service(
        env_key=cleanup, service_name="svc", since="2026-04-12T12:00:00Z"
    )
    deploys = [e for e in after_noon if e["kind"] == "deployment"]
    assert len(deploys) == 1
    assert deploys[0]["id"] == "deploy-late"


def test_clear_env_removes_all_runtime_nodes_for_env(
    graph_store: GraphStore, env_key: str, tmp_path: Path
) -> None:
    p = _write(
        tmp_path,
        env_key,
        """
        incident:
          id: INC-clear-test
          title: t
          env: __ENV__
          detected_at: "2026-04-12T14:24:00Z"
          affected_services: [a, b]
        timeline:
          - at: "2026-04-12T14:17:00Z"
            kind: deployment
            service: a
            deployment_id: deploy-a
          - at: "2026-04-12T14:23:00Z"
            kind: metric_shift
            service: b
            metric: m
            before: 1
            after: 2
        """,
    )
    ingest_postmortem(load_postmortem(p), graph_store)
    counts_before = graph_store.runtime_stats(env_key=env_key)
    assert sum(counts_before.values()) > 0

    removed = graph_store.clear_env(env_key)
    assert removed > 0
    counts_after = graph_store.runtime_stats(env_key=env_key)
    assert sum(counts_after.values()) == 0


def test_clear_env_leaves_code_nodes_untouched(graph_store: GraphStore, env_key: str) -> None:
    """Code namespace must be isolated from runtime namespace — `clear_env`
    has no business touching Repo/File/Function/Class/Symbol nodes."""
    # Seed a code node in a unique repo
    repo_key = f"code-only-{env_key}"
    graph_store.merge_repo(
        key=repo_key,
        spec=repo_key,
        org=None,
        name=None,
        is_local=True,
        commit_sha=None,
        indexed_at="2026-01-01T00:00:00Z",
    )
    try:
        graph_store.clear_env(env_key)  # no runtime data; should be a no-op
        rows = graph_store.query("MATCH (r:Repo {key: $k}) RETURN count(r) AS n", k=repo_key)
        assert rows[0]["n"] == 1
    finally:
        graph_store.clear_repo(repo_key)


def test_bundled_example_postmortem_ingests_without_error(
    graph_store: GraphStore, env_key: str
) -> None:
    """The repo's own example postmortem is the demo data — ingest it under a
    test env_key so we don't pollute the user's prod env namespace."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "research" / "postmortems" / "2025-08-14-payments-redis-cascade.yaml"
    pm = load_postmortem(example)
    # Re-scope to the test's env so clear_env catches it for cleanup.
    pm.incident.env_key = env_key
    for ev in pm.events:
        ev.env_key = env_key
    try:
        stats = ingest_postmortem(pm, graph_store)
        assert stats.deployments >= 3  # initial deploy + mitigation + rollback
        assert stats.metric_shifts >= 4
        assert stats.log_signatures >= 3
        assert stats.services >= 3  # auth, payments, cart
    finally:
        graph_store.clear_env(env_key)
