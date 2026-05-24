"""Temporal proximity causal linker + lagged-correlation strategy.

Given an Incident node, walk every event in the same env that occurred
within a configurable lookback window before the incident's detected_at
timestamp. Score each candidate by *time distance*: closer in time → higher
proximity confidence, decaying with an exponential half-life. Write the
candidates as `(:Cause)-[:PRECEDED {delta_seconds, confidence, derivation}]->
(:Incident)` edges so downstream queries become a one-hop traversal.

Phase 4 step 1 — temporal proximity:
  confidence(Δt) = exp(-ln(2) · Δt / half_life)
  i.e. confidence halves every `half_life` seconds. Defaults to 5 minutes.

Phase 4 step 2 — lagged-correlation boost:
  After proximity scoring, Deployments whose service_name appears in the
  incident's affected_services list receive an additive +0.6 bonus on top
  of their proximity score (capped at 1.0). This fixes the cause-vs-symptom
  honesty gap from step 1: a metric shift 1min before the incident no longer
  outranks the deployment 7min before when that deployment is on an affected
  service (i.e. is the actual cause, not a sibling symptom).

  Why +0.6, why additive?
    - Calibrated against the bundled postmortem: proximity auth-deploy =
      0.379, latency-spike = 0.871. With +0.6 the deploy becomes 0.979
      and wins. Multiplicative would cap at the original proximity score
      (low for distant deploys), defeating the purpose.
    - The bonus is evidence-based — "this deploy is on the same service
      the incident affected" is an observable fact from the graph, not an
      LLM prediction. It composes cleanly with proximity; edges get
      strategy="temporal_proximity+lagged_correlation" so the derivation
      traces both contributions.

Confidence cap: capped at 1.0. Floor at 0.05 — anything quieter than that
gets dropped as noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CausalCandidate:
    """One candidate cause for an incident. Returned by `find_causes` and
    persisted as an edge property bundle by `TemporalLinker.link_incident`."""

    cause_kind: str  # "Deployment" | "MetricShift" | "LogSignature" | "ConfigChange"
    cause_node_key: dict[str, Any]  # the identity properties of the cause
    cause_label: str  # human-readable description for derivation strings
    delta_seconds: float  # how long BEFORE the incident the cause occurred (positive = earlier)
    confidence: float  # 0..1 strategy-composed score
    derivation: str  # one-line explanation (extended per applied strategy)
    strategy: str = "temporal_proximity"  # composed name; multi-strategy uses "+"


@dataclass(slots=True)
class CausalLinkStats:
    incident_id: str
    candidates_inspected: int = 0
    edges_written: int = 0
    edges_skipped_low_confidence: int = 0
    edges_skipped_after_incident: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_causes(
    graph_store: Any,
    incident_id: str,
    *,
    lookback: timedelta = timedelta(hours=6),
    half_life_seconds: float = 300.0,
    min_confidence: float = 0.05,
    limit: int = 50,
) -> list[CausalCandidate]:
    """Read-only: rank candidate causes for an incident without writing edges.

    Use this from the CLI / MCP tool when you want to inspect what the
    linker would produce before persisting it. `TemporalLinker.link_incident`
    runs the same scoring and writes the edges.
    """
    linker = TemporalLinker(
        graph_store=graph_store,
        lookback=lookback,
        half_life_seconds=half_life_seconds,
        min_confidence=min_confidence,
    )
    return linker.score_incident(incident_id, limit=limit)


# ---------------------------------------------------------------------------
# Linker
# ---------------------------------------------------------------------------


class TemporalLinker:
    """Score + write causal edges for one incident at a time, or for every
    incident in an env at once. Stateless — safe to reuse across calls."""

    def __init__(
        self,
        graph_store: Any,
        *,
        lookback: timedelta = timedelta(hours=6),
        half_life_seconds: float = 300.0,
        min_confidence: float = 0.05,
    ) -> None:
        self._gs = graph_store
        self._lookback = lookback
        self._half_life = half_life_seconds
        self._min_confidence = min_confidence

    # ---------------------------------------------------------------- read

    def score_incident(self, incident_id: str, *, limit: int = 50) -> list[CausalCandidate]:
        incident = self._fetch_incident(incident_id)
        if incident is None:
            return []
        candidates = self._fetch_candidates(incident)
        scored = [self._score(c, incident) for c in candidates]
        scored = [c for c in scored if c is not None]
        # Phase 4 step 2: apply lagged-correlation boost after proximity scoring.
        scored = [_apply_lagged_correlation(c, incident) for c in scored]
        scored.sort(key=lambda c: c.confidence, reverse=True)
        return scored[:limit]

    # ---------------------------------------------------------------- write

    def link_incident(self, incident_id: str) -> CausalLinkStats:
        """Re-resolve all causal edges for one incident. Idempotent: clears
        existing :PRECEDED edges first so heuristic drift across runs doesn't
        compound. Returns per-strategy stats."""
        incident = self._fetch_incident(incident_id)
        if incident is None:
            log.warning("temporal_link_no_incident", incident_id=incident_id)
            return CausalLinkStats(incident_id=incident_id)

        # Clear existing causal edges for this incident so re-running gives a
        # clean slate. Phase 4 step 2+ may want to keep stale edges and just
        # update properties; today we wipe + rewrite.
        self._gs.query(
            "MATCH ()-[r:PRECEDED]->(:Incident {id: $id}) DELETE r",
            id=incident_id,
        )

        candidates = self._fetch_candidates(incident)
        stats = CausalLinkStats(incident_id=incident_id)
        stats.candidates_inspected = len(candidates)

        for raw in candidates:
            scored = self._score(raw, incident)
            if scored is None:
                # was either after the incident or below the floor
                if raw.get("delta_seconds_raw", 0) < 0:
                    stats.edges_skipped_after_incident += 1
                else:
                    stats.edges_skipped_low_confidence += 1
                continue
            # Phase 4 step 2: apply lagged-correlation boost before writing.
            scored = _apply_lagged_correlation(scored, incident)
            self._write_edge(scored, incident_id)
            stats.edges_written += 1
            stats.by_kind[scored.cause_kind] = stats.by_kind.get(scored.cause_kind, 0) + 1

        log.info(
            "temporal_link_done",
            incident_id=incident_id,
            candidates=stats.candidates_inspected,
            written=stats.edges_written,
            after=stats.edges_skipped_after_incident,
            low_conf=stats.edges_skipped_low_confidence,
            by_kind=stats.by_kind,
        )
        return stats

    def link_env(self, env_key: str) -> list[CausalLinkStats]:
        """Walk every incident in an env and link each. Cheap (one Cypher per
        incident). Run after every postmortem ingest or live-event batch."""
        rows = self._gs.query(
            "MATCH (i:Incident {env_key: $env}) RETURN i.id AS id",
            env=env_key,
        )
        out = []
        for row in rows:
            out.append(self.link_incident(row["id"]))
        return out

    # ---------------------------------------------------------------- internals

    def _fetch_incident(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._gs.query(
            """
            MATCH (i:Incident {id: $id})
            RETURN i.id AS id, i.env_key AS env_key, i.detected_at AS detected_at,
                   i.title AS title, i.affected_services AS affected_services
            """,
            id=incident_id,
        )
        return rows[0] if rows else None

    def _fetch_candidates(self, incident: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull every Deployment / MetricShift / LogSignature in the same env
        whose timestamp falls inside [detected_at - lookback, detected_at + 1m].

        The +1 minute right-edge is intentional — incidents are sometimes
        declared seconds after the triggering event, and rounding-down would
        miss the obvious cause.
        """
        env_key = incident["env_key"]
        detected_at = _to_datetime(incident["detected_at"])
        lookback_floor = (detected_at - self._lookback).isoformat()
        future_horizon = (detected_at + timedelta(minutes=1)).isoformat()

        rows: list[dict[str, Any]] = []

        # Deployments
        rows.extend(
            self._gs.query(
                """
                MATCH (d:Deployment {env_key: $env})
                WHERE d.at >= $since AND d.at <= $until
                RETURN 'Deployment' AS kind,
                       d.at AS at,
                       d.deployment_id AS deployment_id,
                       d.service_name AS service_name,
                       d.description AS description,
                       d.commit_sha AS commit_sha
                """,
                env=env_key,
                since=lookback_floor,
                until=future_horizon,
            )
        )

        # MetricShifts — Phase 4 step 1 treats these as potential causes too;
        # in reality they're often *symptoms* of the same root cause. Step 2
        # adds lagged correlation so we can distinguish cause from symptom.
        rows.extend(
            self._gs.query(
                """
                MATCH (m:MetricShift {env_key: $env})
                WHERE m.started_at >= $since AND m.started_at <= $until
                RETURN 'MetricShift' AS kind,
                       m.started_at AS at,
                       m.service_name AS service_name,
                       m.metric AS metric,
                       m.before AS before,
                       m.after AS after,
                       m.unit AS unit
                """,
                env=env_key,
                since=lookback_floor,
                until=future_horizon,
            )
        )

        # LogSignatures
        rows.extend(
            self._gs.query(
                """
                MATCH (l:LogSignature {env_key: $env})
                WHERE l.first_seen_at >= $since AND l.first_seen_at <= $until
                RETURN 'LogSignature' AS kind,
                       l.first_seen_at AS at,
                       l.service_name AS service_name,
                       l.signature AS signature,
                       l.signature_hash AS signature_hash,
                       l.count AS count,
                       l.level AS level
                """,
                env=env_key,
                since=lookback_floor,
                until=future_horizon,
            )
        )

        # Attach raw delta for the linker to compute confidence + skip filters.
        for r in rows:
            r["delta_seconds_raw"] = (detected_at - _to_datetime(r["at"])).total_seconds()
        return rows

    def _score(self, raw: dict[str, Any], incident: dict[str, Any]) -> CausalCandidate | None:
        """Convert one raw candidate row → CausalCandidate, or None if it
        should be dropped (after-incident or below confidence floor)."""
        delta_seconds = raw["delta_seconds_raw"]
        if delta_seconds <= 0:
            # Event happened AT or AFTER the incident — can't be a cause.
            return None
        confidence = _exp_decay(delta_seconds, half_life=self._half_life)
        if confidence < self._min_confidence:
            return None

        kind = raw["kind"]
        node_key, label, derivation = _summarize(raw, delta_seconds, confidence)
        return CausalCandidate(
            cause_kind=kind,
            cause_node_key=node_key,
            cause_label=label,
            delta_seconds=delta_seconds,
            confidence=confidence,
            derivation=derivation,
        )

    def _write_edge(self, c: CausalCandidate, incident_id: str) -> None:
        """Write one :PRECEDED edge with confidence + derivation as props."""
        cypher = _PRECEDED_CYPHER_BY_KIND[c.cause_kind]
        self._gs.query(
            cypher,
            incident_id=incident_id,
            confidence=float(c.confidence),
            delta_seconds=float(c.delta_seconds),
            derivation=c.derivation,
            strategy=c.strategy,
            **c.cause_node_key,
        )


# ---------------------------------------------------------------------------
# scoring + serialization helpers
# ---------------------------------------------------------------------------


def _exp_decay(delta_seconds: float, *, half_life: float) -> float:
    """confidence(Δt) = exp(-ln(2) · Δt / half_life). Capped at 1.0, no floor."""
    if delta_seconds <= 0:
        return 1.0
    return min(1.0, math.exp(-math.log(2) * delta_seconds / half_life))


def _to_datetime(v: Any) -> datetime:
    """Neo4j returns native DateTime; fall back to fromisoformat for plain strings."""
    if isinstance(v, datetime):
        return v
    s = str(v)
    # Neo4j DateTime repr ends with timezone offset; fromisoformat handles it on 3.11+
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _summarize(
    raw: dict[str, Any], delta_seconds: float, confidence: float
) -> tuple[dict[str, Any], str, str]:
    """Return (node_key_props, human_label, derivation_string) for one candidate."""
    kind = raw["kind"]
    delta_min = delta_seconds / 60.0
    delta_phrase = (
        f"{delta_seconds:.0f}s before" if delta_seconds < 60 else f"{delta_min:.1f}min before"
    )

    if kind == "Deployment":
        svc = raw.get("service_name") or "?"
        node_key = {
            "env_key": raw.get("env_key") or _env_from_id_or_default(raw),
            "deployment_id": raw["deployment_id"],
            # service_name is NOT part of the Deployment's MERGE identity key
            # (that's env_key + deployment_id), but we carry it here so the
            # lagged-correlation strategy can check it against
            # incident.affected_services without parsing the label.
            "service_name": svc,
        }
        label = f"Deployment {raw['deployment_id']} on {svc}"
        derivation = (
            f"temporal_proximity: {label} occurred {delta_phrase} the incident "
            f"→ confidence {confidence:.3f} (half-life 5min)"
        )
        return node_key, label, derivation

    if kind == "MetricShift":
        node_key = {
            "service_name": raw["service_name"],
            "metric": raw["metric"],
            "started_at": str(raw["at"]),
        }
        unit = raw.get("unit") or ""
        before, after = raw.get("before"), raw.get("after")
        delta_desc = (
            f" ({before}{unit} → {after}{unit})" if before is not None and after is not None else ""
        )
        label = f"MetricShift {raw['service_name']}.{raw['metric']}{delta_desc}"
        derivation = (
            f"temporal_proximity: {label} started {delta_phrase} the incident "
            f"→ confidence {confidence:.3f} (half-life 5min)"
        )
        return node_key, label, derivation

    if kind == "LogSignature":
        node_key = {
            "service_name": raw["service_name"],
            "signature_hash": raw["signature_hash"],
        }
        sig = str(raw.get("signature") or "")
        sig_clip = sig if len(sig) < 60 else sig[:57] + "…"
        label = f'LogSignature "{sig_clip}" on {raw["service_name"]}'
        derivation = (
            f"temporal_proximity: {label} first seen {delta_phrase} the incident "
            f"→ confidence {confidence:.3f} (half-life 5min)"
        )
        return node_key, label, derivation

    raise ValueError(f"unknown cause kind: {kind}")


def _env_from_id_or_default(raw: dict[str, Any]) -> str:
    # Placeholder — the env is threaded through link_incident, not the row.
    # See _PRECEDED_CYPHER_BY_KIND below: env_key is passed in as a Cypher
    # param, not extracted here.
    return raw.get("env_key", "")


# ---------------------------------------------------------------------------
# lagged-correlation boost (Phase 4 step 2)
# ---------------------------------------------------------------------------

# The additive bonus for a Deployment on an affected service. Calibrated
# against the bundled postmortem: proximity auth-deploy = 0.379, payments
# latency-spike = 0.871. With +0.6 the deploy becomes 0.979 and wins,
# correctly reflecting that the deploy CAUSED the spike, not vice versa.
# Changing this number changes the cause-vs-symptom ordering; always
# re-run the bundled-postmortem integration test after tuning.
_LAGGED_CORRELATION_BONUS = 0.6


def _apply_lagged_correlation(
    candidate: CausalCandidate,
    incident: dict[str, Any],
) -> CausalCandidate:
    """Boost a Deployment candidate whose service appears in affected_services.

    Observable-only: the boost is derived from graph state (incident's
    affected_services list vs the deploy's service_name), not from an LLM.
    MetricShifts and LogSignatures on affected services are *symptoms*;
    Deployments on affected services are candidate *causes*. This asymmetry
    is the insight that closes the cause-vs-symptom honesty gap.
    """
    if candidate.cause_kind != "Deployment":
        return candidate

    # Parse the affected_services — may be a JSON string or a list depending
    # on how Neo4j returns it.
    affected = incident.get("affected_services") or []
    if isinstance(affected, str):
        import json

        try:
            affected = json.loads(affected)
        except (json.JSONDecodeError, TypeError):
            affected = []

    svc = candidate.cause_node_key.get("service_name") or ""
    # The service_name is stored in _summarize's node_key only for MetricShift
    # and LogSignature. For Deployment we need to check the label.
    if not svc:
        # Extract from the label: "Deployment deploy-xxx on <service>"
        parts = candidate.cause_label.split(" on ")
        svc = parts[-1].strip() if len(parts) >= 2 else ""

    if svc and svc in affected:
        new_confidence = min(1.0, candidate.confidence + _LAGGED_CORRELATION_BONUS)
        bonus_derivation = (
            f"; lagged_correlation: deploy is on affected service '{svc}'; "
            f"promoted from symptom-tier to cause-tier with "
            f"+{_LAGGED_CORRELATION_BONUS} bonus "
            f"({candidate.confidence:.3f} → {new_confidence:.3f})"
        )
        candidate.confidence = new_confidence
        candidate.derivation += bonus_derivation
        candidate.strategy = "temporal_proximity+lagged_correlation"

    return candidate


# Per-kind MERGE templates. We MATCH the cause node by its identity tuple +
# the incident by id, then MERGE the :PRECEDED edge so re-runs don't
# duplicate. Properties on the edge get SET each time so a re-run with a
# tighter half-life updates confidence cleanly.
#
# `$strategy` is passed from the CausalCandidate — "temporal_proximity"
# for proximity-only, "temporal_proximity+lagged_correlation" when the
# lagged-correlation boost was applied.
_PRECEDED_CYPHER_BY_KIND: dict[str, str] = {
    "Deployment": """
        MATCH (i:Incident {id: $incident_id})
        MATCH (d:Deployment {env_key: i.env_key, deployment_id: $deployment_id})
        MERGE (d)-[r:PRECEDED]->(i)
        SET r.confidence = $confidence,
            r.delta_seconds = $delta_seconds,
            r.derivation = $derivation,
            r.strategy = $strategy
    """,
    "MetricShift": """
        MATCH (i:Incident {id: $incident_id})
        MATCH (m:MetricShift {env_key: i.env_key, service_name: $service_name,
                              metric: $metric, started_at: $started_at})
        MERGE (m)-[r:PRECEDED]->(i)
        SET r.confidence = $confidence,
            r.delta_seconds = $delta_seconds,
            r.derivation = $derivation,
            r.strategy = $strategy
    """,
    "LogSignature": """
        MATCH (i:Incident {id: $incident_id})
        MATCH (l:LogSignature {env_key: i.env_key, service_name: $service_name,
                               signature_hash: $signature_hash})
        MERGE (l)-[r:PRECEDED]->(i)
        SET r.confidence = $confidence,
            r.delta_seconds = $delta_seconds,
            r.derivation = $derivation,
            r.strategy = $strategy
    """,
}
