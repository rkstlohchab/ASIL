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


def test_savings_math_is_correct():
    ledger = PostgresCostLedger("postgresql://u@h/db")
    out = ledger.savings_vs_no_memory(100)
    assert out["memory_conclusions"] == 100
    assert out["fresh_cost_estimate_usd"] == pytest.approx(1.0)
    assert out["with_memory_cost_estimate_usd"] == pytest.approx(0.01)
    assert out["saved_usd"] == pytest.approx(0.99)
    assert out["savings_pct"] == pytest.approx(99.0)


def test_savings_math_zero_memory_is_zero_savings():
    ledger = PostgresCostLedger("postgresql://u@h/db")
    out = ledger.savings_vs_no_memory(0)
    assert out["saved_usd"] == 0.0
    assert out["savings_pct"] == 0.0


def test_savings_math_respects_custom_per_call_cost():
    ledger = PostgresCostLedger("postgresql://u@h/db")
    out = ledger.savings_vs_no_memory(
        50, fresh_cost_estimate_usd=0.05, cached_cost_estimate_usd=0.0005
    )
    assert out["fresh_per_call_usd"] == 0.05
    assert out["cached_per_call_usd"] == 0.0005
    assert out["saved_usd"] == pytest.approx(50 * (0.05 - 0.0005))
