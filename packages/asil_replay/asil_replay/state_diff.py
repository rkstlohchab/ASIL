"""State diff — before/after architecture snapshot for an incident.

Given an incident with detected_at and resolved_at timestamps, compares
the state of each affected service before and after the incident window:
  - Deployments that happened in the incident window
  - Metric deltas (before → after for each metric shift)
  - Services whose state changed

The diff is purely graph-derived — no LLM reasoning. It tells the reader
"here's what the system looked like before versus after."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MetricDelta:
    """One metric that changed during the incident."""

    service: str
    metric: str
    before: float | None
    after: float | None
    unit: str = ""


@dataclass(slots=True)
class DeploymentSnapshot:
    """One deployment active during the incident window."""

    deployment_id: str
    service: str
    description: str
    commit_sha: str = ""
    at: str = ""


@dataclass(slots=True)
class StateDiff:
    """Before/after comparison for an incident."""

    incident_id: str
    services_involved: list[str] = field(default_factory=list)
    deployments_during: list[DeploymentSnapshot] = field(default_factory=list)
    metric_deltas: list[MetricDelta] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Differ
# ---------------------------------------------------------------------------


class StateDiffer:
    """Computes the state diff for an incident from graph data."""

    def __init__(self, graph_store: Any) -> None:
        self._gs = graph_store

    def diff(self, incident_id: str) -> StateDiff | None:
        """Compute the before/after state diff for one incident.

        Returns None if the incident doesn't exist.
        """
        incident = self._fetch_incident(incident_id)
        if incident is None:
            return None

        affected = self._parse_affected(incident)
        env_key = incident.get("env_key") or ""
        detected_at = str(incident.get("detected_at") or "")
        resolved_at = str(incident.get("resolved_at") or detected_at)

        deployments = self._deployments_during(env_key, affected, detected_at, resolved_at)
        metric_deltas = self._metric_deltas(env_key, affected, detected_at, resolved_at)

        diff = StateDiff(
            incident_id=incident_id,
            services_involved=affected,
            deployments_during=deployments,
            metric_deltas=metric_deltas,
        )

        log.info(
            "state_diff_computed",
            incident_id=incident_id,
            deploys=len(deployments),
            metrics=len(metric_deltas),
        )
        return diff

    # ---------------------------------------------------------------- internals

    def _fetch_incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._gs.query(
            """
            MATCH (i:Incident {id: $id})
            RETURN i.id AS id, i.env_key AS env_key, i.detected_at AS detected_at,
                   i.resolved_at AS resolved_at,
                   i.affected_services AS affected_services
            """,
            id=incident_id,
        )
        return rows[0] if rows else None

    def _parse_affected(self, incident: dict[str, Any]) -> list[str]:
        affected = incident.get("affected_services") or []
        if isinstance(affected, str):
            import json

            try:
                affected = json.loads(affected)
            except (json.JSONDecodeError, TypeError):
                affected = []
        return list(affected)

    def _deployments_during(
        self,
        env_key: str,
        affected: list[str],
        detected_at: str,
        resolved_at: str,
    ) -> list[DeploymentSnapshot]:
        """Find deployments on affected services within the incident window.

        "During" means: deployed_at >= lookback_before_incident AND
        deployed_at <= resolved_at. We use a 30-minute lookback before
        detected_at to catch the triggering deployment.
        """
        from datetime import datetime, timedelta

        try:
            dt = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
            lookback = (dt - timedelta(minutes=30)).isoformat()
        except (ValueError, TypeError):
            lookback = detected_at

        snapshots: list[DeploymentSnapshot] = []
        for svc in affected:
            rows = self._gs.query(
                """
                MATCH (d:Deployment {env_key: $env, service_name: $svc})
                WHERE d.at >= $since AND d.at <= $until
                RETURN d.deployment_id AS deployment_id,
                       d.service_name AS service_name,
                       d.description AS description,
                       d.commit_sha AS commit_sha,
                       d.at AS at
                ORDER BY d.at
                """,
                env=env_key,
                svc=svc,
                since=lookback,
                until=str(resolved_at),
            )
            for r in rows:
                snapshots.append(
                    DeploymentSnapshot(
                        deployment_id=r.get("deployment_id") or "",
                        service=r.get("service_name") or svc,
                        description=r.get("description") or "",
                        commit_sha=r.get("commit_sha") or "",
                        at=str(r.get("at") or ""),
                    )
                )
        return snapshots

    def _metric_deltas(
        self,
        env_key: str,
        affected: list[str],
        detected_at: str,
        resolved_at: str,
    ) -> list[MetricDelta]:
        """Collect metric shifts for affected services during the incident."""
        from datetime import datetime, timedelta

        try:
            dt = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
            lookback = (dt - timedelta(minutes=30)).isoformat()
        except (ValueError, TypeError):
            lookback = detected_at

        deltas: list[MetricDelta] = []
        for svc in affected:
            rows = self._gs.query(
                """
                MATCH (m:MetricShift {env_key: $env, service_name: $svc})
                WHERE m.started_at >= $since AND m.started_at <= $until
                RETURN m.service_name AS service_name,
                       m.metric AS metric,
                       m.before AS before,
                       m.after AS after,
                       m.unit AS unit
                ORDER BY m.started_at
                """,
                env=env_key,
                svc=svc,
                since=lookback,
                until=str(resolved_at),
            )
            for r in rows:
                deltas.append(
                    MetricDelta(
                        service=r.get("service_name") or svc,
                        metric=r.get("metric") or "",
                        before=r.get("before"),
                        after=r.get("after"),
                        unit=r.get("unit") or "",
                    )
                )
        return deltas
