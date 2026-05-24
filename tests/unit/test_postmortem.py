"""Unit tests for the postmortem YAML loader.

The loader's job is to take a YAML file and produce a `PostmortemFile`
with typed event objects. These tests pin the parsing rules (top-level
incident, kind dispatch, timestamp coercion, error surface) without
needing a Neo4j round-trip — ingestion is covered by integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest
from asil_infra import (
    Deployment,
    Incident,
    LogSignature,
    MetricShift,
    PostmortemFile,
    Service,
    load_postmortem,
)


def _write(tmp_path: Path, body: str, name: str = "pm.yaml") -> Path:
    p = tmp_path / name
    p.write_text(dedent(body).lstrip("\n"))
    return p


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_loads_top_level_incident_and_each_timeline_kind(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident:
          id: INC-test
          title: "the bad thing"
          env: prod
          detected_at: "2026-04-12T14:24:00Z"
          severity: high
          affected_services: [payments, cart]
        timeline:
          - at: "2026-04-12T14:17:00Z"
            kind: deployment
            service: auth
            deployment_id: deploy-1
            commit_sha: abc123
            description: "auth deploy"
          - at: "2026-04-12T14:23:00Z"
            kind: metric_shift
            service: payments
            metric: p99
            before: 120
            after: 4200
            unit: ms
          - at: "2026-04-12T14:24:00Z"
            kind: log_signature
            service: payments
            signature: "Redis timeout"
            count: 100
            level: error
        """,
    )
    pm = load_postmortem(p)
    assert isinstance(pm, PostmortemFile)
    assert pm.incident.id == "INC-test"
    assert pm.incident.env_key == "prod"
    assert pm.incident.affected_services == ["payments", "cart"]
    assert pm.incident.detected_at == datetime(2026, 4, 12, 14, 24, tzinfo=UTC)

    assert len(pm.events) == 3
    dep, ms, ls = pm.events
    assert isinstance(dep, Deployment)
    assert dep.deployment_id == "deploy-1"
    assert dep.commit_sha == "abc123"
    assert dep.service_name == "auth"
    assert isinstance(ms, MetricShift)
    assert ms.before == 120.0 and ms.after == 4200.0
    assert ms.metric == "p99"
    assert isinstance(ls, LogSignature)
    assert ls.signature == "Redis timeout"
    assert ls.count == 100
    assert ls.level == "error"


def test_source_field_records_provenance(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline: []
        """,
        name="my_incident.yaml",
    )
    pm = load_postmortem(p)
    assert pm.incident.source == "postmortem:my_incident.yaml"


def test_inline_service_event_carries_repo_key_and_file_paths(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T00:00:01Z"
            kind: service
            name: payments
            repo_key: org/payments-repo
            file_paths: [src/main.py, src/handlers/checkout.py]
        """,
    )
    pm = load_postmortem(p)
    svc = pm.events[0]
    assert isinstance(svc, Service)
    assert svc.repo_key == "org/payments-repo"
    assert svc.file_paths == ["src/main.py", "src/handlers/checkout.py"]


def test_inline_incident_event_allowed_for_cascading_postmortems(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T01:00:00Z"
            kind: incident
            id: INC-secondary
            title: "cascading incident in cart"
            affected_services: [cart]
            severity: medium
        """,
    )
    pm = load_postmortem(p)
    sub = pm.events[0]
    assert isinstance(sub, Incident)
    assert sub.id == "INC-secondary"
    assert sub.affected_services == ["cart"]


# ---------------------------------------------------------------------------
# error surface
# ---------------------------------------------------------------------------


def test_missing_file_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_postmortem("/definitely/does/not/exist.yaml")


def test_non_mapping_top_level_raises_value_error(tmp_path: Path) -> None:
    p = _write(tmp_path, "- just a list\n- not a mapping")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_postmortem(p)


def test_timeline_row_with_unknown_kind_raises_with_index(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T00:00:01Z"
            kind: invented_kind
            service: payments
        """,
    )
    with pytest.raises(ValueError, match="timeline\\[0\\]"):
        load_postmortem(p)


def test_timeline_row_missing_required_field_raises_with_context(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T00:00:01Z"
            kind: deployment
            service: payments
            # deployment_id missing
        """,
    )
    with pytest.raises(ValueError, match=r"timeline\[0\] \(deployment\)"):
        load_postmortem(p)


# ---------------------------------------------------------------------------
# field coercion
# ---------------------------------------------------------------------------


def test_metric_shift_coerces_numeric_strings_to_float(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T00:00:01Z"
            kind: metric_shift
            service: payments
            metric: rate
            before: "0.95"
            after: "0.42"
        """,
    )
    pm = load_postmortem(p)
    ms = pm.events[0]
    assert ms.before == 0.95
    assert ms.after == 0.42


def test_log_signature_hash_is_stable_across_loads(tmp_path: Path) -> None:
    body = """
        incident: {id: x, title: t, env: prod, detected_at: "2026-01-01T00:00:00Z"}
        timeline:
          - at: "2026-01-01T00:00:01Z"
            kind: log_signature
            service: payments
            signature: "Redis timeout after 5000ms"
            count: 1
        """
    pm1 = load_postmortem(_write(tmp_path, body, name="a.yaml"))
    pm2 = load_postmortem(_write(tmp_path, body, name="b.yaml"))
    assert pm1.events[0].signature_hash == pm2.events[0].signature_hash


# ---------------------------------------------------------------------------
# bundled example
# ---------------------------------------------------------------------------


def test_bundled_example_postmortem_loads_cleanly() -> None:
    """The repo's own example postmortem must always parse — it's the demo data."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "research" / "postmortems" / "2025-08-14-payments-redis-cascade.yaml"
    assert example.exists(), f"missing bundled example: {example}"
    pm = load_postmortem(example)
    assert pm.incident.env_key == "prod"
    assert "payments" in pm.incident.affected_services
    assert len(pm.events) >= 8  # 4 deployments + 4 metric shifts + 3 log sigs (approx)
    # Spot-check that the rollback deployment is parsed correctly.
    rollback = next(
        (
            e
            for e in pm.events
            if isinstance(e, Deployment) and "rollback" in (e.description or "").lower()
        ),
        None,
    )
    assert rollback is not None
    assert rollback.commit_sha == "7b1a3e9"


def test_db_pool_exhaustion_postmortem_loads_cleanly() -> None:
    """The DB-pool-exhaustion cascade postmortem (Phase 4 step 2 eval seed)."""
    repo_root = Path(__file__).resolve().parents[2]
    pm_path = repo_root / "research" / "postmortems" / "2026-02-08-db-pool-exhaustion.yaml"
    assert pm_path.exists(), f"missing postmortem: {pm_path}"
    pm = load_postmortem(pm_path)
    assert pm.incident.id == "INC-2026-02-08-db-pool-exhaustion"
    assert pm.incident.env_key == "prod"
    assert set(pm.incident.affected_services) >= {"orders", "inventory", "notifications"}
    assert len(pm.events) >= 8
    # The bad deployment that triggered the cascade should be parseable.
    bad_deploy = next(
        (
            e
            for e in pm.events
            if isinstance(e, Deployment) and e.deployment_id == "deploy-d1a7f3c"
        ),
        None,
    )
    assert bad_deploy is not None
    assert bad_deploy.commit_sha == "d1a7f3c"
    assert bad_deploy.service_name == "orders"


def test_dns_misconfig_postmortem_loads_cleanly() -> None:
    """The DNS-misconfig cascade postmortem (Phase 4 step 2 eval seed)."""
    repo_root = Path(__file__).resolve().parents[2]
    pm_path = repo_root / "research" / "postmortems" / "2026-03-19-dns-misconfig-checkout.yaml"
    assert pm_path.exists(), f"missing postmortem: {pm_path}"
    pm = load_postmortem(pm_path)
    assert pm.incident.id == "INC-2026-03-19-dns-misconfig-checkout"
    assert pm.incident.env_key == "prod"
    assert set(pm.incident.affected_services) >= {"gateway", "payments", "email"}
    # The ConfigMap rollout is modelled as a Deployment with no commit_sha
    # (Phase 3 schema-extension question deferred — see file header).
    cm_rollout = next(
        (
            e
            for e in pm.events
            if isinstance(e, Deployment) and e.deployment_id == "cm-2026-03-19-1"
        ),
        None,
    )
    assert cm_rollout is not None
    assert cm_rollout.commit_sha is None
    assert "ConfigMap" in (cm_rollout.description or "")
