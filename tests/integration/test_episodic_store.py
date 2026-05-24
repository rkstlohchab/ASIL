"""Integration tests for EpisodicStore.

Requires Postgres reachable; auto-skips otherwise. Each test uses a unique
repo_key so they don't collide with prior runs or with other tests, and
cleans up after itself.
"""

from __future__ import annotations

import uuid

import pytest
from asil_core import Confidence
from asil_memory import (
    EPISODIC_COLLECTION,
    EpisodicStore,
    EpisodicStoreError,
    VectorStore,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def episodic_store(vector_store: VectorStore):
    try:
        s = EpisodicStore(vector_store=vector_store)
        s.verify_connectivity()
    except EpisodicStoreError as e:
        pytest.skip(f"postgres unreachable: {e}")
    s.apply_schema()
    yield s
    s.close()


@pytest.fixture
def repo_key() -> str:
    """Unique per test so writes don't bleed across tests."""
    return f"test-episodic/{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup(episodic_store: EpisodicStore, repo_key: str):
    yield repo_key
    episodic_store.clear_repo(repo_key)


def _conf(score: float = 0.7, evidence: int = 5) -> Confidence:
    return Confidence(
        score=score,
        evidence_count=evidence,
        retrieval_strength=0.6,
        causal_confidence=0.0,
        derivation=["test"],
    )


# ---------------------------------------------------------------------------
# schema + write/read round-trip
# ---------------------------------------------------------------------------


def test_apply_schema_is_idempotent(episodic_store: EpisodicStore) -> None:
    episodic_store.apply_schema()
    episodic_store.apply_schema()  # no error on re-run


def test_remember_then_get_round_trips_every_field(
    episodic_store: EpisodicStore, cleanup: str, vector_store: VectorStore
) -> None:
    citations = [{"qualified_name": "pkg.foo", "file_path": "foo.py", "start_line": 10}]
    vec = [0.1] * 8
    # Pre-create the collection at the right dim so the test doesn't rely on
    # whatever the live `asil_memories` collection happens to be sized at.
    if vector_store._client.collection_exists(EPISODIC_COLLECTION):  # type: ignore[attr-defined]
        vector_store._client.delete_collection(EPISODIC_COLLECTION)  # type: ignore[attr-defined]
    vector_store.ensure_collection(EPISODIC_COLLECTION, dim=len(vec))

    written = episodic_store.remember(
        repo_key=cleanup,
        question="how does X work?",
        answer="X does Y. (foo.py:10)",
        confidence=_conf(0.81, evidence=7),
        citations=citations,
        model="gpt-4o-mini",
        provider="openai",
        cost_usd=0.0007,
        profile="tight",
        verifier_unsupported=0,
        question_vector=vec,
    )

    fetched = episodic_store.get(written.id)
    assert fetched is not None
    assert fetched.id == written.id
    assert fetched.repo_key == cleanup
    assert fetched.question == "how does X work?"
    assert fetched.answer == "X does Y. (foo.py:10)"
    assert fetched.confidence.score == pytest.approx(0.81)
    assert fetched.confidence.evidence_count == 7
    assert fetched.citations == citations
    assert fetched.model == "gpt-4o-mini"
    assert fetched.provider == "openai"
    assert fetched.cost_usd == pytest.approx(0.0007)
    assert fetched.profile == "tight"
    assert fetched.verifier_unsupported == 0
    assert fetched.created_at is not None


def test_recall_recent_returns_newest_first(episodic_store: EpisodicStore, cleanup: str) -> None:
    ids = []
    for q in ["q1", "q2", "q3"]:
        m = episodic_store.remember(
            repo_key=cleanup,
            question=q,
            answer="a",
            confidence=_conf(),
            citations=[],
            model="m",
            provider="p",
            cost_usd=0.0,
            profile="tight",
        )
        ids.append(m.id)

    recent = episodic_store.recall_recent(repo_key=cleanup, limit=10)
    # Most recent first → reverse insertion order.
    assert [m.id for m in recent] == list(reversed(ids))


def test_recall_recent_scopes_to_repo_key(episodic_store: EpisodicStore, cleanup: str) -> None:
    other_repo = f"test-episodic-other/{uuid.uuid4().hex[:8]}"
    try:
        episodic_store.remember(
            repo_key=cleanup,
            question="a",
            answer="x",
            confidence=_conf(),
            citations=[],
            model="m",
            provider="p",
            cost_usd=0.0,
            profile="t",
        )
        episodic_store.remember(
            repo_key=other_repo,
            question="b",
            answer="x",
            confidence=_conf(),
            citations=[],
            model="m",
            provider="p",
            cost_usd=0.0,
            profile="t",
        )

        scoped = episodic_store.recall_recent(repo_key=cleanup, limit=10)
        assert all(m.repo_key == cleanup for m in scoped)
        assert len(scoped) == 1
    finally:
        episodic_store.clear_repo(other_repo)


def test_recall_similar_uses_vector_store(
    episodic_store: EpisodicStore, cleanup: str, vector_store: VectorStore
) -> None:
    """Memories with matching question vectors should come back ranked by similarity."""
    # Drop any existing episodic collection so we can size it to the test's dim;
    # other tests in this module may have created it at a different dim.
    if vector_store._client.collection_exists(EPISODIC_COLLECTION):  # type: ignore[attr-defined]
        vector_store._client.delete_collection(EPISODIC_COLLECTION)  # type: ignore[attr-defined]
    vector_store.ensure_collection(EPISODIC_COLLECTION, dim=4)
    # Vectors are unit-distance from each other along different axes; query
    # vector should rank the matching memory first.
    episodic_store.remember(
        repo_key=cleanup,
        question="closer match",
        answer="x",
        confidence=_conf(),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="t",
        question_vector=[1.0, 0.0, 0.0, 0.0],
    )
    episodic_store.remember(
        repo_key=cleanup,
        question="far match",
        answer="y",
        confidence=_conf(),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="t",
        question_vector=[0.0, 1.0, 0.0, 0.0],
    )

    hits = episodic_store.recall_similar(
        query_vector=[1.0, 0.0, 0.0, 0.0],
        repo_key=cleanup,
        limit=2,
    )
    assert len(hits) == 2
    assert hits[0].memory.question == "closer match"
    assert hits[0].similarity > hits[1].similarity


def test_forget_removes_row(episodic_store: EpisodicStore, cleanup: str) -> None:
    m = episodic_store.remember(
        repo_key=cleanup,
        question="ephemeral",
        answer="x",
        confidence=_conf(),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="t",
    )
    assert episodic_store.get(m.id) is not None
    assert episodic_store.forget(m.id) is True
    assert episodic_store.get(m.id) is None
    # forgetting a missing id is a no-op (False, not an error)
    assert episodic_store.forget(m.id) is False


def test_clear_repo_wipes_all_memories(episodic_store: EpisodicStore, cleanup: str) -> None:
    for q in ("a", "b", "c"):
        episodic_store.remember(
            repo_key=cleanup,
            question=q,
            answer="x",
            confidence=_conf(),
            citations=[],
            model="m",
            provider="p",
            cost_usd=0.0,
            profile="t",
        )
    assert episodic_store.count(repo_key=cleanup) == 3
    removed = episodic_store.clear_repo(cleanup)
    assert removed == 3
    assert episodic_store.count(repo_key=cleanup) == 0


def test_stats_returns_overall_and_per_repo(episodic_store: EpisodicStore, cleanup: str) -> None:
    episodic_store.remember(
        repo_key=cleanup,
        question="q",
        answer="a",
        confidence=_conf(),
        citations=[],
        model="m",
        provider="p",
        cost_usd=0.0,
        profile="t",
    )
    info = episodic_store.stats()
    assert info["total"] >= 1
    assert cleanup in info["per_repo"]
    assert info["per_repo"][cleanup] == 1


# ---------------------------------------------------------------------------
# DSN normalization
# ---------------------------------------------------------------------------


def test_dsn_normalizer_strips_sqlalchemy_driver_suffix() -> None:
    from asil_memory.episodic import _normalize_dsn  # type: ignore[attr-defined]

    assert _normalize_dsn("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# ---------------------------------------------------------------------------
# graceful skip when Qdrant unavailable
# ---------------------------------------------------------------------------


def test_remember_works_without_vector_store() -> None:
    """Memory should persist even if no vector store is wired (no semantic recall
    available, but the Postgres write succeeds)."""
    try:
        no_vec = EpisodicStore(vector_store=None)
        no_vec.verify_connectivity()
    except EpisodicStoreError as e:
        pytest.skip(f"postgres unreachable: {e}")
    try:
        no_vec.apply_schema()
        repo_key = f"test-episodic-novec/{uuid.uuid4().hex[:8]}"
        try:
            m = no_vec.remember(
                repo_key=repo_key,
                question="q",
                answer="a",
                confidence=_conf(),
                citations=[],
                model="m",
                provider="p",
                cost_usd=0.0,
                profile="t",
            )
            assert m.id
            assert no_vec.get(m.id) is not None
            # recall_similar without vector_store returns empty list, not an error.
            assert no_vec.recall_similar(query_vector=[0.0] * 4, repo_key=repo_key) == []
        finally:
            no_vec.clear_repo(repo_key)
    finally:
        no_vec.close()


def test_vector_store_fixture_is_present(vector_store: VectorStore) -> None:
    """Sanity: confirm the conftest fixture loaded; skipped tests above would
    otherwise look like a missing dep rather than a real Qdrant-down condition."""
    assert vector_store is not None
