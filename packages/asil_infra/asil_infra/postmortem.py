"""Postmortem YAML loader + graph ingestor.

A postmortem in our YAML shape is the simplest possible source of historical
runtime events: a structured incident description plus a `timeline:` list of
typed entries. We can hand-author one from any public RCA in 10 minutes and
get back the same graph nodes the K8s/Prom/Loki adapters will eventually
produce when ASIL runs against a real cluster.

Why ship this before the live adapters?
  - The Phase 4 temporal causality engine needs *historical* event sequences
    to validate its causal-edge heuristics. Postmortems are pre-validated by
    humans (the incident really did happen, we know the root cause).
  - It de-risks the schema: if a real RCA can't be expressed as nodes + edges,
    we learn that without needing kubernetes-asyncio + Prometheus + Loki.
  - Demo-able offline: anyone can run `asil postmortem ingest <file>` and see
    the timeline in Neo4j Browser without standing up infra.

File shape (see research/postmortems/*.yaml for examples):

    incident:
      id: INC-2025-08-14-payments-latency
      title: "Payments service latency spike"
      env: prod
      detected_at: "2025-08-14T14:23:00Z"
      ...
    timeline:
      - at: "2025-08-14T14:17:00Z"
        kind: deployment
        service: auth
        deployment_id: deploy-8f2c1d4
        commit_sha: 8f2c1d4
        description: "..."
      - at: "2025-08-14T14:23:00Z"
        kind: metric_shift
        service: payments
        metric: http_request_duration_p99
        before: 120
        after: 4200
        unit: ms
      ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from asil_core.logging import get_logger
from asil_memory import GraphStore
from pydantic import BaseModel, ConfigDict, Field

from asil_infra.models import (
    Deployment,
    Incident,
    LogSignature,
    MetricShift,
    RuntimeKind,
    Service,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# YAML shape
# ---------------------------------------------------------------------------


class _IncidentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    env: str
    detected_at: datetime
    resolved_at: datetime | None = None
    severity: str = "unknown"
    summary: str | None = None
    affected_services: list[str] = Field(default_factory=list)


class _TimelineEntry(BaseModel):
    """Permissive union: one field per `kind`. We dispatch on `kind` in the
    loader rather than using pydantic's discriminated unions because the
    timeline rows aren't symmetric (different required fields per kind)."""

    model_config = ConfigDict(extra="allow")

    at: datetime
    kind: RuntimeKind


@dataclass(slots=True)
class PostmortemFile:
    path: Path
    incident: Incident
    events: list[Any] = field(default_factory=list)  # heterogeneous RuntimeEvent list


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_postmortem(path: str | Path) -> PostmortemFile:
    """Parse a postmortem YAML into typed models. Doesn't touch the graph."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"postmortem not found: {path}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping")

    inc_block = _IncidentBlock.model_validate(raw.get("incident") or {})
    source = f"postmortem:{p.name}"
    incident = Incident(
        env_key=inc_block.env,
        source=source,
        id=inc_block.id,
        title=inc_block.title,
        severity=inc_block.severity,
        detected_at=inc_block.detected_at,
        resolved_at=inc_block.resolved_at,
        summary=inc_block.summary,
        affected_services=list(inc_block.affected_services),
    )

    events: list[Any] = []
    for i, row in enumerate(raw.get("timeline") or []):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: timeline[{i}] must be a mapping, got {type(row).__name__}")
        try:
            event = _row_to_event(row, env_key=inc_block.env, source=source)
        except Exception as e:
            raise ValueError(f"{path}: timeline[{i}] ({row.get('kind')}): {e}") from e
        events.append(event)

    log.info(
        "postmortem_loaded",
        path=str(p),
        incident_id=incident.id,
        events=len(events),
    )
    return PostmortemFile(path=p, incident=incident, events=events)


def _row_to_event(row: dict[str, Any], *, env_key: str, source: str) -> Any:
    """Dispatch one timeline row to the right RuntimeEvent type."""
    kind = RuntimeKind(row["kind"])
    common = {"env_key": env_key, "source": source, "confidence": row.get("confidence", 1.0)}

    if kind is RuntimeKind.deployment:
        return Deployment(
            **common,
            deployment_id=row["deployment_id"],
            service_name=row["service"],
            at=row["at"],
            commit_sha=row.get("commit_sha"),
            description=row.get("description"),
        )
    if kind is RuntimeKind.metric_shift:
        return MetricShift(
            **common,
            service_name=row["service"],
            metric=row["metric"],
            started_at=row["at"],
            ended_at=row.get("ended_at"),
            before=_to_float(row.get("before")),
            after=_to_float(row.get("after")),
            unit=row.get("unit"),
            description=row.get("description"),
        )
    if kind is RuntimeKind.log_signature:
        return LogSignature(
            **common,
            service_name=row["service"],
            signature=row["signature"],
            first_seen_at=row["at"],
            last_seen_at=row.get("last_seen_at"),
            count=int(row.get("count", 1)),
            level=row.get("level"),
        )
    if kind is RuntimeKind.service:
        return Service(
            **common,
            name=row["name"],
            repo_key=row.get("repo_key"),
            file_paths=list(row.get("file_paths") or []),
        )
    if kind is RuntimeKind.incident:
        # Rare — usually the incident is in the top-level `incident:` block.
        # Allow it inline for postmortems that describe multi-incident cascades.
        return Incident(
            **common,
            id=row["id"],
            title=row["title"],
            severity=row.get("severity", "unknown"),
            detected_at=row["at"],
            resolved_at=row.get("resolved_at"),
            summary=row.get("summary"),
            affected_services=list(row.get("affected_services") or []),
        )
    raise ValueError(f"unknown kind: {kind}")


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PostmortemIngestStats:
    incident_id: str
    services: int = 0
    deployments: int = 0
    metric_shifts: int = 0
    log_signatures: int = 0
    extra_incidents: int = 0  # inline-declared incidents in the timeline


def ingest_postmortem(pm: PostmortemFile, store: GraphStore) -> PostmortemIngestStats:
    """Write the parsed postmortem into the graph. Idempotent (MERGE-based)."""
    store.apply_schema()

    stats = PostmortemIngestStats(incident_id=pm.incident.id)

    # Materialize Service nodes for everything the incident touched + every
    # service referenced in the timeline. This guarantees the :AFFECTED edges
    # land on real Service nodes rather than auto-created stubs.
    seen_services: set[str] = set(pm.incident.affected_services)
    for ev in pm.events:
        if isinstance(ev, Service):
            seen_services.add(ev.name)
        elif isinstance(ev, (Deployment, MetricShift, LogSignature)):
            seen_services.add(ev.service_name)

    for svc_name in sorted(seen_services):
        # Prefer the typed Service event if the timeline included one (it may
        # carry repo_key + file_paths); otherwise emit a minimal Service node.
        typed = next(
            (e for e in pm.events if isinstance(e, Service) and e.name == svc_name),
            None,
        )
        svc_obj = typed or Service(
            env_key=pm.incident.env_key,
            source=pm.incident.source,
            name=svc_name,
            confidence=pm.incident.confidence,
        )
        store.merge_service(_service_props(svc_obj))
        stats.services += 1

    # Now incident + timeline events.
    store.merge_incident(
        _incident_props(pm.incident), affected_services=pm.incident.affected_services
    )

    for ev in pm.events:
        if isinstance(ev, Service):
            continue  # already merged above
        if isinstance(ev, Deployment):
            store.merge_deployment(_deployment_props(ev))
            stats.deployments += 1
        elif isinstance(ev, MetricShift):
            store.merge_metric_shift(_metric_shift_props(ev))
            stats.metric_shifts += 1
        elif isinstance(ev, LogSignature):
            store.merge_log_signature(_log_signature_props(ev))
            stats.log_signatures += 1
        elif isinstance(ev, Incident):
            store.merge_incident(_incident_props(ev), affected_services=ev.affected_services)
            stats.extra_incidents += 1

    log.info(
        "postmortem_ingested",
        incident_id=pm.incident.id,
        services=stats.services,
        deployments=stats.deployments,
        metric_shifts=stats.metric_shifts,
        log_signatures=stats.log_signatures,
    )
    return stats


# ---------------------------------------------------------------------------
# property marshallers — pull Neo4j-safe primitives + ISO strings out of pydantic
# ---------------------------------------------------------------------------


def _common_props(ev: Any) -> dict[str, Any]:
    return {
        "env_key": ev.env_key,
        "source": ev.source,
        "confidence": float(ev.confidence),
    }


def _service_props(s: Service) -> dict[str, Any]:
    return {
        **_common_props(s),
        "name": s.name,
        "repo_key": s.repo_key,
        "file_paths": list(s.file_paths),
    }


def _deployment_props(d: Deployment) -> dict[str, Any]:
    return {
        **_common_props(d),
        "deployment_id": d.deployment_id,
        "service_name": d.service_name,
        "at": d.at.isoformat(),
        "commit_sha": d.commit_sha,
        "description": d.description,
    }


def _metric_shift_props(m: MetricShift) -> dict[str, Any]:
    return {
        **_common_props(m),
        "service_name": m.service_name,
        "metric": m.metric,
        "started_at": m.started_at.isoformat(),
        "ended_at": m.ended_at.isoformat() if m.ended_at else None,
        "before": m.before,
        "after": m.after,
        "unit": m.unit,
        "description": m.description,
    }


def _log_signature_props(ls: LogSignature) -> dict[str, Any]:
    return {
        **_common_props(ls),
        "service_name": ls.service_name,
        "signature": ls.signature,
        "signature_hash": ls.signature_hash,
        "first_seen_at": ls.first_seen_at.isoformat(),
        "last_seen_at": ls.last_seen_at.isoformat() if ls.last_seen_at else None,
        "count": ls.count,
        "level": ls.level,
    }


def _incident_props(i: Incident) -> dict[str, Any]:
    return {
        **_common_props(i),
        "id": i.id,
        "title": i.title,
        "severity": i.severity,
        "detected_at": i.detected_at.isoformat(),
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "summary": i.summary,
        "affected_services": list(i.affected_services),
    }


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
