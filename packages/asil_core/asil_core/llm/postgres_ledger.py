"""Postgres-backed cost ledger — survives API restarts so the dashboard can
show cost over arbitrary time windows.

Schema is intentionally narrow: one row per LLM call. Aggregations live in
the query layer (`spend_by_day`, `spend_by_provider`, ...) so the write path
stays single-INSERT and lock-free.

Wired in `ModelRouter.from_env()` when the Postgres DSN resolves and the
table can be created; falls back transparently to the in-memory ledger
otherwise so unit tests and offline use never break.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from asil_core.llm.ledger import CostRecord


def _normalize_dsn(dsn: str) -> str:
    """Strip SQLAlchemy-style `+driver` qualifiers so psycopg accepts the DSN.
    Identical to `asil_memory.episodic._normalize_dsn` — duplicated to avoid
    a dependency from asil_core onto asil_memory.
    """
    if dsn.startswith("postgresql+"):
        prefix, _, rest = dsn.partition("://")
        scheme = prefix.split("+", 1)[0]
        return f"{scheme}://{rest}"
    return dsn


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asil_costs (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    tier            TEXT NOT NULL,
    profile         TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS asil_costs_ts_desc ON asil_costs (ts DESC);
CREATE INDEX IF NOT EXISTS asil_costs_provider_ts ON asil_costs (provider, ts DESC);
"""


@dataclass(slots=True)
class CostAggregates:
    total_usd: float
    calls: int
    by_provider: dict[str, float]
    by_tier: dict[str, float]
    by_day: list[tuple[str, float]]  # [(ISO date, cost), ...] oldest -> newest


class PostgresCostLedger:
    """Implements the `CostLedger` Protocol against Postgres.

    Connections are short-lived (one per write) — fine at Phase 7 scale where
    LLM calls are an order of magnitude slower than the round-trip. A pooled
    variant lives in `ModelRouter` only if we observe contention.
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

    async def record(self, entry: CostRecord) -> None:
        # psycopg3's sync API is fine here — LLM-call cadence is low enough that
        # offloading to a thread would add more latency than the insert itself.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO asil_costs "
                    "(ts, provider, model, tier, profile, input_tokens, output_tokens, cost_usd) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        entry.timestamp,
                        entry.provider,
                        entry.model,
                        entry.tier,
                        entry.profile,
                        entry.input_tokens,
                        entry.output_tokens,
                        entry.cost_usd,
                    ),
                )
            conn.commit()

    async def spend_today_usd(self) -> float:
        today = datetime.now(UTC).date()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(sum(cost_usd), 0) AS total "
                "FROM asil_costs WHERE ts::date = %s",
                (today,),
            )
            row = cur.fetchone()
        return float(row["total"]) if row else 0.0

    # --------------------------------------------------------- read aggregations

    def aggregates(self, *, days: int = 30) -> CostAggregates:
        """One read for the dashboard. Returns totals + breakdowns + a per-day
        time series for the trailing `days` window."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(sum(cost_usd), 0) AS total, count(*) AS calls "
                "FROM asil_costs WHERE ts >= now() - (%s || ' days')::interval",
                (days,),
            )
            head = cur.fetchone() or {"total": 0.0, "calls": 0}

            cur.execute(
                "SELECT provider, sum(cost_usd) AS cost "
                "FROM asil_costs WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY provider ORDER BY cost DESC",
                (days,),
            )
            by_provider = {r["provider"]: float(r["cost"]) for r in cur.fetchall()}

            cur.execute(
                "SELECT tier, sum(cost_usd) AS cost "
                "FROM asil_costs WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY tier ORDER BY cost DESC",
                (days,),
            )
            by_tier = {r["tier"]: float(r["cost"]) for r in cur.fetchall()}

            cur.execute(
                "SELECT ts::date AS day, sum(cost_usd) AS cost "
                "FROM asil_costs WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY day ORDER BY day ASC",
                (days,),
            )
            by_day = [
                (r["day"].isoformat(), float(r["cost"])) for r in cur.fetchall()
            ]

        return CostAggregates(
            total_usd=float(head["total"]),
            calls=int(head["calls"]),
            by_provider=by_provider,
            by_tier=by_tier,
            by_day=by_day,
        )

    # -------------------------------------------------------------- savings calc

    def savings_vs_no_memory(
        self,
        memory_count: int,
        *,
        fresh_cost_estimate_usd: float = 0.01,
        cached_cost_estimate_usd: float = 0.0001,
    ) -> dict[str, Any]:
        """Estimate how much money episodic memory saved across the full
        history. Assumes every memory hit replaces what would have been a
        full pipeline run (the conservative case — actual savings are higher
        when the same conclusion gets re-used many times)."""
        fresh_total = memory_count * fresh_cost_estimate_usd
        with_memory_total = memory_count * cached_cost_estimate_usd
        saved = fresh_total - with_memory_total
        pct = (saved / fresh_total * 100) if fresh_total > 0 else 0.0
        return {
            "memory_conclusions": memory_count,
            "fresh_cost_estimate_usd": round(fresh_total, 4),
            "with_memory_cost_estimate_usd": round(with_memory_total, 4),
            "saved_usd": round(saved, 4),
            "savings_pct": round(pct, 2),
            "fresh_per_call_usd": fresh_cost_estimate_usd,
            "cached_per_call_usd": cached_cost_estimate_usd,
        }


def from_settings_or_none() -> PostgresCostLedger | None:
    """Build a Postgres ledger from settings — None if Postgres is unreachable.

    The caller (`ModelRouter.from_env`) should fall back to the in-memory
    ledger when this returns None so the system stays usable offline.
    """
    from asil_core.config import get_settings

    settings = get_settings()
    dsn = settings.postgres_dsn
    if not dsn:
        return None
    ledger = PostgresCostLedger(dsn)
    try:
        ledger.verify_connectivity()
        ledger.apply_schema()
    except Exception:
        return None
    return ledger
