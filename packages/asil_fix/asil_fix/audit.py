"""Audit log for the Phase 8 fix pipeline.

Every fix proposal — whether sandboxed, accepted, or thrown away — is
written to Postgres so reviewers can reconstruct *why* the LLM proposed
a given change, *what* causal chain it acted on, and *what* the sandbox
returned. This is the trust contract that makes autonomous code changes
auditable: nothing can be lost, nothing can be silently overridden.

Schema is one wide row per fix attempt. Aggregations (acceptance rate,
$ per accepted fix, average time-to-result) live in the query layer.

Falls back to a no-op stub when Postgres is unreachable so unit tests
and offline workflows don't break.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import psycopg
from asil_core.llm.postgres_ledger import _normalize_dsn
from asil_core.logging import get_logger
from psycopg.rows import dict_row

from asil_fix.models import FixOutcome, FixProposal, SandboxResult

log = get_logger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asil_fix_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    incident_id     TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    confidence_score DOUBLE PRECISION NOT NULL,
    sandbox_outcome TEXT,
    sandbox_duration_seconds DOUBLE PRECISION,
    affected_files  JSONB NOT NULL,
    causal_chain    JSONB NOT NULL,
    diff            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    cost_usd        DOUBLE PRECISION NOT NULL,
    derivation      JSONB NOT NULL,
    sandbox_stdout_tail TEXT,
    sandbox_stderr_tail TEXT
);

CREATE INDEX IF NOT EXISTS asil_fix_audit_ts_desc ON asil_fix_audit (ts DESC);
CREATE INDEX IF NOT EXISTS asil_fix_audit_incident ON asil_fix_audit (incident_id, ts DESC);
"""


@dataclass(slots=True)
class FixAuditEntry:
    """Read-side projection of one row in `asil_fix_audit`."""

    id: int
    ts: datetime
    incident_id: str
    outcome: FixOutcome
    confidence_score: float
    sandbox_outcome: str | None
    sandbox_duration_seconds: float | None
    affected_files: list[str]
    summary: str
    model: str
    cost_usd: float
    diff: str
    causal_chain: list[dict[str, Any]]
    derivation: list[str]
    sandbox_stdout_tail: str | None
    sandbox_stderr_tail: str | None


class AuditLog:
    """Postgres-backed audit log. Thin wrapper around a single table.

    `record()` is the write path; `list_for_incident` / `recent` are the
    read helpers used by the CLI's `asil fix list` command and the
    `/fixes` UI page (not yet shipped).
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalize_dsn(dsn)
        self._connect_ok: bool | None = None

    # ----------------------------------------------------------------- lifecycle

    def verify_connectivity(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1")
        self._connect_ok = True

    def apply_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
            conn.commit()

    @contextmanager
    def _connect(self):
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            yield conn

    # -------------------------------------------------------------------- write

    def record(
        self,
        proposal: FixProposal,
        sandbox: SandboxResult,
        *,
        confidence_gate: float = 0.6,
    ) -> FixOutcome:
        """Persist one fix attempt. Returns the aggregated `FixOutcome`."""
        outcome = self._classify(proposal, sandbox, confidence_gate=confidence_gate)

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO asil_fix_audit ("
                    "ts, incident_id, outcome, confidence_score, sandbox_outcome, "
                    "sandbox_duration_seconds, affected_files, causal_chain, diff, "
                    "summary, model, cost_usd, derivation, sandbox_stdout_tail, "
                    "sandbox_stderr_tail"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        proposal.generated_at,
                        proposal.incident_id,
                        outcome.value,
                        proposal.confidence_score,
                        sandbox.outcome.value,
                        sandbox.duration_seconds,
                        json.dumps(proposal.affected_files),
                        json.dumps(_scrub(proposal.causal_chain)),
                        proposal.diff,
                        proposal.summary,
                        proposal.model,
                        proposal.cost_usd,
                        json.dumps(proposal.derivation),
                        sandbox.stdout_tail or None,
                        sandbox.stderr_tail or None,
                    ),
                )
            conn.commit()
        return outcome

    @staticmethod
    def _classify(
        proposal: FixProposal,
        sandbox: SandboxResult,
        *,
        confidence_gate: float,
    ) -> FixOutcome:
        """Two gates, both must hold for `accepted`:
          1. The sandbox ran the test command and it passed.
          2. The proposal's confidence is above the configured floor.

        Otherwise it's `proposed` (we never ran tests), `rejected` (we
        did, and they failed), or `inconclusive` (timeout / sandbox
        error). This shape stays stable so dashboards can pivot on it.
        """
        from asil_fix.models import SandboxOutcome

        if sandbox.outcome is SandboxOutcome.not_run:
            return FixOutcome.proposed
        if sandbox.outcome is SandboxOutcome.tests_passed:
            if proposal.confidence_score >= confidence_gate:
                return FixOutcome.accepted
            return FixOutcome.inconclusive
        if sandbox.outcome in {
            SandboxOutcome.tests_failed,
            SandboxOutcome.apply_failed,
        }:
            return FixOutcome.rejected
        return FixOutcome.inconclusive

    # --------------------------------------------------------------------- read

    def list_for_incident(self, incident_id: str, *, limit: int = 50) -> list[FixAuditEntry]:
        cypher = (
            "SELECT * FROM asil_fix_audit "
            "WHERE incident_id = %s ORDER BY ts DESC LIMIT %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(cypher, (incident_id, limit))
            rows = cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    def recent(self, *, limit: int = 20) -> list[FixAuditEntry]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM asil_fix_audit ORDER BY ts DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
        return [_row_to_entry(r) for r in rows]

    def aggregates(self, *, days: int = 30) -> dict[str, Any]:
        """Single read for the future `/fixes` UI page. Avoids N+1 queries
        by computing every panel's numbers in one round trip."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, count(*) AS n, sum(cost_usd) AS cost "
                "FROM asil_fix_audit "
                "WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY outcome",
                (days,),
            )
            by_outcome = {
                r["outcome"]: {"count": int(r["n"]), "cost_usd": float(r["cost"] or 0)}
                for r in cur.fetchall()
            }
            cur.execute(
                "SELECT count(*) AS total, sum(cost_usd) AS cost "
                "FROM asil_fix_audit "
                "WHERE ts >= now() - (%s || ' days')::interval",
                (days,),
            )
            row = cur.fetchone() or {"total": 0, "cost": 0}
        return {
            "days": days,
            "total": int(row["total"]),
            "total_cost_usd": float(row["cost"] or 0),
            "by_outcome": by_outcome,
        }


def from_settings_or_none() -> AuditLog | None:
    """Build an audit log from settings — None when Postgres isn't reachable.

    Callers should fall back to a no-op (skip recording) instead of
    refusing to propose. The CLI uses this pattern in `asil fix propose`.
    """
    from asil_core.config import get_settings

    settings = get_settings()
    dsn = settings.postgres_dsn
    if not dsn:
        return None
    log_ = AuditLog(dsn)
    try:
        log_.verify_connectivity()
        log_.apply_schema()
    except Exception:
        return None
    return log_


def _scrub(value: Any) -> Any:
    """Make a value JSON-safe — Neo4j returns datetime / Node objects we'd
    rather store as ISO strings."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_to_entry(r: dict[str, Any]) -> FixAuditEntry:
    return FixAuditEntry(
        id=int(r["id"]),
        ts=r["ts"] if isinstance(r["ts"], datetime) else datetime.fromisoformat(str(r["ts"])),
        incident_id=r["incident_id"],
        outcome=FixOutcome(r["outcome"]),
        confidence_score=float(r["confidence_score"]),
        sandbox_outcome=r.get("sandbox_outcome"),
        sandbox_duration_seconds=(
            float(r["sandbox_duration_seconds"])
            if r.get("sandbox_duration_seconds") is not None
            else None
        ),
        affected_files=list(r["affected_files"] or []),
        summary=r["summary"],
        model=r["model"],
        cost_usd=float(r["cost_usd"]),
        diff=r["diff"],
        causal_chain=list(r["causal_chain"] or []),
        derivation=list(r["derivation"] or []),
        sandbox_stdout_tail=r.get("sandbox_stdout_tail"),
        sandbox_stderr_tail=r.get("sandbox_stderr_tail"),
    )


_ = asdict  # silence unused-import; kept for future use
