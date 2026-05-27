"""Phase 9.2 — unit tests for the write-time dedupe path + the
asil_memory_writes event log helpers.

We bypass EpisodicStore.__init__ (which opens a real psycopg connection)
and inject mocks for `_conn` and `_vector` so the tests run without
docker."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from asil_core import Confidence
from asil_memory.episodic import EpisodicStore, Memory, MemoryHit


def _make_store_with_mocks() -> tuple[EpisodicStore, MagicMock, MagicMock]:
    """Construct an EpisodicStore without touching Postgres or Qdrant."""
    store = EpisodicStore.__new__(EpisodicStore)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    store._conn = conn
    store._dsn = "postgresql://x"
    store._vector = MagicMock()
    return store, conn, cur


def _make_memory(mem_id: str = "mem-existing") -> Memory:
    return Memory(
        id=mem_id,
        repo_key="local:/repo",
        question="how does X work?",
        answer="X works like this.",
        confidence=Confidence(
            score=0.9, evidence_count=3, retrieval_strength=0.8, causal_confidence=0.0
        ),
        citations=[],
        verifier_unsupported=0,
        model="gpt-4o-mini",
        provider="openai",
        cost_usd=0.001,
        profile="tight",
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        user_id="alice@startup.dev",
        machine_id="workstation-7",
        origin_agent="claude-code",
    )


def test_fold_into_existing_runs_jsonb_update():
    """UPDATE bumps recall_hits AND appends to metadata.sources via jsonb_set."""
    store, _conn, cur = _make_store_with_mocks()
    store.get = MagicMock(return_value=_make_memory())  # type: ignore[method-assign]

    out = store._fold_into_existing(
        existing_id="mem-existing",
        source="claude-code-transcript",
        origin_agent="claude-code",
        user_id="alice@startup.dev",
    )

    sql = cur.execute.call_args[0][0]
    params = cur.execute.call_args[0][1]
    assert "recall_hits = recall_hits + 1" in sql
    assert "jsonb_set" in sql
    assert params[0] == "claude-code-transcript"
    assert params[1] == "mem-existing"
    assert out.id == "mem-existing"


def test_log_memory_write_inserts_event_row():
    store, _conn, cur = _make_store_with_mocks()

    store._log_memory_write(
        repo_key="local:/repo",
        user_id="alice@startup.dev",
        origin_agent="claude-code",
        source="claude-code-transcript",
        question_text="how does X work?",
        nearest_existing_id="mem-existing",
        nearest_similarity=0.97,
        outcome="folded",
        resulting_memory_id="mem-existing",
    )

    sql, params = cur.execute.call_args[0]
    assert "INSERT INTO asil_memory_writes" in sql
    assert params[0] == "local:/repo"
    assert params[1] == "alice@startup.dev"
    assert params[2] == "claude-code"
    assert params[3] == "claude-code-transcript"
    assert params[7] == "folded"


def test_log_memory_write_swallows_errors():
    """A logging failure must not blow up the caller — the memory write is
    the user-visible operation, the event log is just telemetry."""
    store, _conn, cur = _make_store_with_mocks()
    cur.execute.side_effect = RuntimeError("postgres down")

    # No raise.
    store._log_memory_write(
        repo_key="r",
        user_id="u",
        origin_agent="cli",
        source=None,
        question_text="q",
        nearest_existing_id=None,
        nearest_similarity=None,
        outcome="inserted",
        resulting_memory_id="mem-1",
    )


def test_write_log_stats_computes_dedupe_rate():
    store, _conn, cur = _make_store_with_mocks()
    # Three separate fetchall() responses for the three queries.
    cur.fetchall.side_effect = [
        [("inserted", 8), ("folded", 12)],  # by outcome
        [("claude-code", 12), ("cli", 8)],  # by agent
        [("claude-code-transcript", 12), ("(direct)", 8)],  # by source
    ]

    out = store.write_log_stats(days=30)
    assert out["total_writes"] == 20
    assert out["inserted"] == 8
    assert out["folded"] == 12
    assert out["dedupe_rate_pct"] == 60.0  # 12 / 20
    assert out["by_agent"]["claude-code"] == 12
    assert out["by_source"]["claude-code-transcript"] == 12


def test_remember_folds_on_high_similarity():
    """The headline test: with dedupe_threshold=0.95 and a 0.97 hit,
    remember() bypasses the INSERT and folds into the existing memory."""
    store, _conn, _cur = _make_store_with_mocks()
    existing = _make_memory()
    store.recall_similar = MagicMock(  # type: ignore[method-assign]
        return_value=[MemoryHit(memory=existing, similarity=0.97)]
    )
    store._fold_into_existing = MagicMock(return_value=existing)  # type: ignore[method-assign]
    store._log_memory_write = MagicMock()  # type: ignore[method-assign]

    out = store.remember(
        repo_key="local:/repo",
        question="how does X work, really?",
        answer="…",
        confidence=Confidence(
            score=0.7, evidence_count=2, retrieval_strength=0.6, causal_confidence=0.0
        ),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="tight",
        question_vector=[0.1] * 8,
        dedupe_threshold=0.95,
        origin_agent="claude-code",
        user_id="alice@startup.dev",
    )

    store._fold_into_existing.assert_called_once()
    args = store._log_memory_write.call_args.kwargs
    assert args["outcome"] == "folded"
    assert args["nearest_similarity"] == 0.97
    assert out.id == existing.id


def test_remember_inserts_when_below_dedupe_threshold(monkeypatch):
    """Below threshold → INSERT path. The nearest_existing_id/similarity
    still get logged so we can track 'almost folded' near-misses."""
    store, _conn, cur = _make_store_with_mocks()
    other = _make_memory(mem_id="mem-other")
    store.recall_similar = MagicMock(  # type: ignore[method-assign]
        return_value=[MemoryHit(memory=other, similarity=0.8)]
    )
    store._fold_into_existing = MagicMock()  # type: ignore[method-assign]
    store._log_memory_write = MagicMock()  # type: ignore[method-assign]
    cur.fetchone.return_value = (datetime.now(UTC),)

    store.remember(
        repo_key="local:/repo",
        question="totally different question?",
        answer="…",
        confidence=Confidence(
            score=0.7, evidence_count=2, retrieval_strength=0.6, causal_confidence=0.0
        ),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="tight",
        question_vector=[0.1] * 8,
        dedupe_threshold=0.95,
    )

    store._fold_into_existing.assert_not_called()
    args = store._log_memory_write.call_args.kwargs
    assert args["outcome"] == "inserted"
    assert args["nearest_existing_id"] == "mem-other"
    assert args["nearest_similarity"] == 0.8


def test_remember_skips_dedupe_when_threshold_none():
    """`asil.remember` MCP tool passes dedupe_threshold=None so out-of-band
    writes always INSERT regardless of similarity."""
    store, _conn, cur = _make_store_with_mocks()
    store.recall_similar = MagicMock()  # type: ignore[method-assign]
    store._fold_into_existing = MagicMock()  # type: ignore[method-assign]
    store._log_memory_write = MagicMock()  # type: ignore[method-assign]
    cur.fetchone.return_value = (datetime.now(UTC),)

    store.remember(
        repo_key="local:/repo",
        question="anything",
        answer="…",
        confidence=Confidence(
            score=0.5, evidence_count=0, retrieval_strength=0.0, causal_confidence=0.0
        ),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="tight",
        question_vector=[0.1] * 8,
        dedupe_threshold=None,
    )

    store.recall_similar.assert_not_called()
    store._fold_into_existing.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
