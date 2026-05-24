"""Execution replay engine — orchestrates timeline, cascade, and causes.

The `ReplayEngine` is the single entry point. Given an incident ID, it:
  1. Fetches the incident node from the graph
  2. Collects all runtime events across affected services (TimelineBuilder)
  3. Reads persisted :PRECEDED edges (top causes)
  4. Derives the service cascade ordering (CascadeReconstructor)
  5. Computes an aggregate confidence card
  6. Returns an `IncidentReplay` dataclass

The engine reads from the graph — it never invents causes. If the temporal
linker hasn't run yet, causes and cascade are empty/flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asil_core.confidence import Confidence
from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TimelineEntry:
    """One event in the incident timeline."""

    at: str  # ISO-8601
    kind: str  # deployment, metric_shift, log_signature, incident
    service: str
    description: str
    marker: str = ""  # "↗ cause", "▶ INCIDENT", "↓ response", or ""


@dataclass(slots=True)
class ServiceCascadeEntry:
    """One service in the cascade ordering."""

    service: str
    first_event_at: str  # ISO-8601
    first_event_kind: str
    first_event_description: str


@dataclass(slots=True)
class IncidentReplay:
    """The full replay of an incident — returned by ReplayEngine.replay()."""

    incident: dict[str, Any]
    summary_lines: list[str]
    timeline: list[TimelineEntry]
    top_causes: list[dict[str, Any]]
    service_cascade: list[ServiceCascadeEntry]
    confidence: Confidence


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ReplayEngine:
    """Read-only engine that produces an IncidentReplay from graph state."""

    def __init__(self, graph_store: Any) -> None:
        self._gs = graph_store

    def replay(self, incident_id: str, *, causes_limit: int = 5) -> IncidentReplay | None:
        """Produce the full replay for one incident. Returns None if the
        incident doesn't exist in the graph."""
        incident = self._fetch_incident(incident_id)
        if incident is None:
            log.warning("replay_no_incident", incident_id=incident_id)
            return None

        affected = self._parse_affected(incident)
        detected_at = incident.get("detected_at") or ""

        # 1. Build timeline
        timeline = self._build_timeline(incident, affected, detected_at)

        # 2. Read top causes
        top_causes = self._gs.causes_for_incident(incident_id, limit=causes_limit)
        cause_node_keys = self._cause_node_keys(top_causes)

        # 3. Mark timeline entries
        self._mark_timeline(timeline, detected_at, cause_node_keys)

        # 4. Build service cascade
        cascade = self._build_cascade(timeline, affected)

        # 5. Compute confidence card
        confidence = self._aggregate_confidence(top_causes)

        # 6. Build summary lines
        summary_lines = self._build_summary(incident, affected)

        log.info(
            "replay_built",
            incident_id=incident_id,
            timeline_events=len(timeline),
            causes=len(top_causes),
            services=len(cascade),
        )

        return IncidentReplay(
            incident=incident,
            summary_lines=summary_lines,
            timeline=timeline,
            top_causes=top_causes,
            service_cascade=cascade,
            confidence=confidence,
        )

    # ---------------------------------------------------------------- internals

    def _fetch_incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._gs.query(
            """
            MATCH (i:Incident {id: $id})
            RETURN i.id AS id, i.env_key AS env_key, i.detected_at AS detected_at,
                   i.resolved_at AS resolved_at, i.title AS title,
                   i.severity AS severity, i.summary AS summary,
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

    def _build_timeline(
        self,
        incident: dict[str, Any],
        affected: list[str],
        detected_at: str,
    ) -> list[TimelineEntry]:
        """Collect all runtime events for affected services + merge + sort."""
        env_key = incident.get("env_key") or ""
        entries: list[TimelineEntry] = []

        for svc in affected:
            events = self._gs.events_for_service(env_key, svc, limit=500)
            for ev in events:
                at = str(ev.get("at") or "")
                kind = ev.get("kind") or ""
                desc = self._event_description(ev)
                entries.append(TimelineEntry(at=at, kind=kind, service=svc, description=desc))

        # Deduplicate by (kind, service, at)
        seen: set[tuple[str, str, str]] = set()
        deduped: list[TimelineEntry] = []
        for e in entries:
            key = (e.kind, e.service, e.at)
            if key not in seen:
                seen.add(key)
                deduped.append(e)

        deduped.sort(key=lambda e: e.at)
        return deduped

    def _event_description(self, ev: dict[str, Any]) -> str:
        """Build a human-readable description from an event dict."""
        kind = ev.get("kind") or ""
        if kind == "deployment":
            dep_id = ev.get("id") or ""
            desc = ev.get("description") or ""
            sha = ev.get("commit_sha") or ""
            parts = [f"Deployment {dep_id}"]
            if sha:
                parts.append(f"(sha: {sha})")
            if desc:
                parts.append(f"- {desc}")
            return " ".join(parts)
        if kind == "metric_shift":
            metric = ev.get("metric") or ""
            before = ev.get("before")
            after = ev.get("after")
            unit = ev.get("unit") or ""
            desc = ev.get("description") or ""
            if before is not None and after is not None:
                return desc or f"{metric}: {before}{unit} -> {after}{unit}"
            return desc or f"{metric} shifted"
        if kind == "log_signature":
            sig = ev.get("signature") or ""
            count = ev.get("count") or ""
            return f'Log: "{sig}" (count={count})'
        if kind == "incident":
            return ev.get("title") or ev.get("id") or "incident"
        return str(ev.get("description") or kind)

    def _cause_node_keys(self, causes: list[dict[str, Any]]) -> set[tuple[str, str]]:
        """Extract (kind, at) keys from cause nodes for timeline marking."""
        keys: set[tuple[str, str]] = set()
        for c in causes:
            kind = c.get("cause_kind") or ""
            props = c.get("cause_props") or {}
            # Map cause_kind to timeline kind
            timeline_kind = {
                "Deployment": "deployment",
                "MetricShift": "metric_shift",
                "LogSignature": "log_signature",
            }.get(kind, kind.lower())
            at = str(props.get("at") or props.get("started_at") or props.get("first_seen_at") or "")
            svc = str(props.get("service_name") or "")
            if at:
                keys.add((timeline_kind, svc, at))
        return keys

    def _mark_timeline(
        self,
        timeline: list[TimelineEntry],
        detected_at: str,
        cause_keys: set[tuple[str, str]],
    ) -> None:
        """Mark each timeline entry with a marker based on its role."""
        detected_str = str(detected_at)
        for entry in timeline:
            entry_key = (entry.kind, entry.service, entry.at)
            if entry.kind == "incident":
                entry.marker = "▶ INCIDENT"
            elif entry_key in cause_keys:
                entry.marker = "↗ cause"
            elif entry.at >= detected_str and entry.kind != "incident":
                entry.marker = "↓ response"

    def _build_cascade(
        self, timeline: list[TimelineEntry], affected: list[str]
    ) -> list[ServiceCascadeEntry]:
        """Order services by their earliest event time."""
        first_by_svc: dict[str, TimelineEntry] = {}
        for entry in timeline:
            if entry.service not in first_by_svc:
                first_by_svc[entry.service] = entry

        # Sort by first event time; include only affected services
        cascade: list[ServiceCascadeEntry] = []
        for svc in sorted(first_by_svc, key=lambda s: first_by_svc[s].at):
            if svc in affected:
                e = first_by_svc[svc]
                cascade.append(
                    ServiceCascadeEntry(
                        service=svc,
                        first_event_at=e.at,
                        first_event_kind=e.kind,
                        first_event_description=e.description,
                    )
                )
        return cascade

    def _aggregate_confidence(self, causes: list[dict[str, Any]]) -> Confidence:
        """Build a Confidence from the top causes."""
        if not causes:
            return Confidence(
                score=0.0,
                evidence_count=0,
                derivation="no causal edges found; run `asil temporal link` first",
            )
        scores = [float(c.get("confidence") or 0.0) for c in causes]
        avg = sum(scores) / len(scores) if scores else 0.0
        strategies = set()
        for c in causes:
            s = c.get("strategy") or "unknown"
            strategies.add(s)
        return Confidence(
            score=avg,
            evidence_count=len(causes),
            derivation=(
                f"average of {len(causes)} causal edges; "
                f"strategies: {', '.join(sorted(strategies))}"
            ),
        )

    def _build_summary(self, incident: dict[str, Any], affected: list[str]) -> list[str]:
        """Build human-readable summary lines for the incident header."""
        lines: list[str] = []
        lines.append(f"Incident: {incident.get('id') or 'unknown'}")
        title = incident.get("title") or ""
        if title:
            lines.append(f"Title: {title}")
        lines.append(f"Env: {incident.get('env_key') or '?'}")
        lines.append(f"Severity: {incident.get('severity') or '?'}")
        lines.append(f"Detected: {incident.get('detected_at') or '?'}")
        resolved = incident.get("resolved_at")
        if resolved:
            lines.append(f"Resolved: {resolved}")
        if affected:
            lines.append(f"Affected: {', '.join(affected)}")
        return lines
