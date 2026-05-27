"""Unit tests for the Postgres cost ledger.

We test the write/read SQL by mocking psycopg's connection — the goal is to
pin the SQL shape and the savings-math, not exercise Postgres (the
integration test in tests/integration covers that)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from asil_core.llm import CostRecord
from asil_core.llm.postgres_ledger import PostgresCostLedger, _normalize_dsn


def test_normalize_dsn_strips_sqlalchemy_driver():
    assert (
        _normalize_dsn("postgresql+asyncpg://a:b@h/db")
        == "postgresql://a:b@h/db"
    )
    assert (
        _normalize_dsn("postgresql+psycopg://u:p@h/db")
        == "postgresql://u:p@h/db"
    )


def test_normalize_dsn_passes_through_plain():
    assert _normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_record_inserts_one_row():
    ledger = PostgresCostLedger("postgresql://u@h/db")
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    with patch(
        "asil_core.llm.postgres_ledger.psycopg.connect", return_value=conn
    ):
        asyncio.run(
            ledger.record(
                CostRecord(
                    timestamp=datetime(2026, 5, 25, tzinfo=UTC),
                    provider="openai",
                    model="gpt-4o-mini",
                    tier="reasoning",
                    profile="tight",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.0003,
                )
            )
        )

    args, _ = cur.execute.call_args
    sql, params = args
    assert "INSERT INTO asil_costs" in sql
    assert params[1] == "openai"
    assert params[2] == "gpt-4o-mini"
    assert params[7] == 0.0003
    conn.commit.assert_called_once()


def _mock_savings_cursor(
    *,
    has_recall_hits: bool,
    cache_hits: int,
    avg_fresh: float,
    avg_cached: float,
):
    """Build a mocked psycopg cursor whose fetchone() yields the rows that
    `savings_vs_no_memory` expects in order: column-exists probe, then the
    aggregates over asil_memories, then the aggregate over asil_costs."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    responses: list = [
        {"column_name": "recall_hits"} if has_recall_hits else None,
    ]
    if has_recall_hits:
        responses.append({"hits": cache_hits, "avg_fresh": avg_fresh})
    responses.append({"avg_cached": avg_cached})
    cur.fetchone.side_effect = responses
    return cur


def _mock_connection(cur: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn


def test_savings_math_measured_from_real_ledger():
    """With recall_hits column present and cache hits recorded, savings are
    computed from the real average costs — not hardcoded estimates."""
    ledger = PostgresCostLedger("postgresql://u@h/db")
    cur = _mock_savings_cursor(
        has_recall_hits=True,
        cache_hits=10,
        avg_fresh=0.005,
        avg_cached=0.00005,
    )
    conn = _mock_connection(cur)
    with patch(
        "asil_core.llm.postgres_ledger.psycopg.connect", return_value=conn
    ):
        out = ledger.savings_vs_no_memory(100, days=30)

    assert out["memory_conclusions"] == 100
    assert out["cache_hits"] == 10
    assert out["avg_fresh_usd"] == pytest.approx(0.005, rel=1e-3)
    assert out["avg_cached_usd"] == pytest.approx(0.00005, rel=1e-3)
    assert out["saved_usd"] == pytest.approx(10 * (0.005 - 0.00005), rel=1e-3)
    assert out["savings_pct"] == pytest.approx(
        (0.005 - 0.00005) / 0.005 * 100.0, rel=1e-2
    )
    assert out["measured"] is True


def test_savings_math_no_cache_hits_returns_null_pct():
    """When no cache hits have fired yet, the function refuses to fabricate a
    percentage and surfaces a 'measured = False' marker."""
    ledger = PostgresCostLedger("postgresql://u@h/db")
    cur = _mock_savings_cursor(
        has_recall_hits=True,
        cache_hits=0,
        avg_fresh=0.0,
        avg_cached=0.0,
    )
    conn = _mock_connection(cur)
    with patch(
        "asil_core.llm.postgres_ledger.psycopg.connect", return_value=conn
    ):
        out = ledger.savings_vs_no_memory(0)

    assert out["cache_hits"] == 0
    assert out["saved_usd"] == 0.0
    assert out["savings_pct"] is None
    assert out["measured"] is False
    assert "No cache hits" in out["note"]


def test_savings_math_old_schema_without_recall_hits_column():
    """Backward compat: if the recall_hits column hasn't been added yet,
    savings still returns zeros rather than raising."""
    ledger = PostgresCostLedger("postgresql://u@h/db")
    cur = _mock_savings_cursor(
        has_recall_hits=False,
        cache_hits=0,
        avg_fresh=0.0,
        avg_cached=0.0,
    )
    conn = _mock_connection(cur)
    with patch(
        "asil_core.llm.postgres_ledger.psycopg.connect", return_value=conn
    ):
        out = ledger.savings_vs_no_memory(5)

    assert out["cache_hits"] == 0
    assert out["measured"] is False
