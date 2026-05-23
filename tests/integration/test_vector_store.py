"""Integration tests for the Qdrant VectorStore + Embedder.

Skipped if Qdrant is unreachable. The embedder tests use ASIL's mock LLM
profile so they don't require a real OpenAI key — embeddings come from
MockEmbeddingProvider, which produces deterministic small vectors.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from asil_core.llm import (
    InMemoryCostLedger,
    MockEmbeddingProvider,
    MockLLMProvider,
    ModelRouter,
    Profile,
)
from asil_ingest import Embedder, SourceLanguage, parse_source
from asil_memory import (
    VectorPoint,
    VectorStore,
    point_id_for,
)

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def fresh_collection_name() -> str:
    """A unique collection per test so we never collide with another test or
    with a real `asil_code` collection from prior CLI runs."""
    return f"asil_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_collection(vector_store: VectorStore, fresh_collection_name: str):
    yield fresh_collection_name
    # qdrant_client raises if the collection doesn't exist on delete; check first.
    if vector_store._client.collection_exists(fresh_collection_name):  # type: ignore[attr-defined]
        vector_store._client.delete_collection(fresh_collection_name)  # type: ignore[attr-defined]


@pytest.fixture
def mock_router() -> ModelRouter:
    """Router backed by mock providers — no API key required."""
    mock_chat = MockLLMProvider()
    profile = Profile(
        name="mock",
        chat={
            "reasoning": mock_chat,
            "classify": mock_chat,
            "summarize": mock_chat,
            "verify": mock_chat,
        },
        embedding=MockEmbeddingProvider(dim=8),
    )
    return ModelRouter(profile=profile, ledger=InMemoryCostLedger())


# --- VectorStore direct ----------------------------------------------------


def test_ensure_collection_creates_then_idempotent(
    vector_store: VectorStore, cleanup_collection: str
) -> None:
    vector_store.ensure_collection(cleanup_collection, dim=8)
    vector_store.ensure_collection(cleanup_collection, dim=8)  # re-running is fine


def test_ensure_collection_rejects_dim_mismatch(
    vector_store: VectorStore, cleanup_collection: str
) -> None:
    from asil_memory import VectorStoreError

    vector_store.ensure_collection(cleanup_collection, dim=8)
    with pytest.raises(VectorStoreError, match="exists with dim"):
        vector_store.ensure_collection(cleanup_collection, dim=16)


def test_upsert_and_search(vector_store: VectorStore, cleanup_collection: str) -> None:
    vector_store.ensure_collection(cleanup_collection, dim=4)
    points = [
        VectorPoint(
            id=point_id_for("test/repo", "a"),
            vector=[1.0, 0.0, 0.0, 0.0],
            payload={"repo_key": "test/repo", "qualified_name": "a", "kind": "function"},
        ),
        VectorPoint(
            id=point_id_for("test/repo", "b"),
            vector=[0.0, 1.0, 0.0, 0.0],
            payload={"repo_key": "test/repo", "qualified_name": "b", "kind": "function"},
        ),
        VectorPoint(
            id=point_id_for("test/repo", "c"),
            vector=[0.9, 0.1, 0.0, 0.0],
            payload={"repo_key": "test/repo", "qualified_name": "c", "kind": "class"},
        ),
    ]
    vector_store.upsert_batch(points, collection=cleanup_collection)
    assert vector_store.count(cleanup_collection) == 3

    # Query the [1,0,0,0] axis — "a" wins, "c" close behind.
    hits = vector_store.search([1.0, 0.0, 0.0, 0.0], limit=2, collection=cleanup_collection)
    assert len(hits) == 2
    assert hits[0].payload["qualified_name"] == "a"
    assert hits[1].payload["qualified_name"] == "c"

    # `kind` filter narrows to class only.
    hits_cls = vector_store.search(
        [1.0, 0.0, 0.0, 0.0], limit=5, kind="class", collection=cleanup_collection
    )
    assert [h.payload["qualified_name"] for h in hits_cls] == ["c"]


def test_clear_repo_removes_matching_points(
    vector_store: VectorStore, cleanup_collection: str
) -> None:
    vector_store.ensure_collection(cleanup_collection, dim=4)
    points = [
        VectorPoint(
            id=point_id_for("keep/repo", "x"),
            vector=[1.0, 0.0, 0.0, 0.0],
            payload={"repo_key": "keep/repo", "qualified_name": "x"},
        ),
        VectorPoint(
            id=point_id_for("drop/repo", "y"),
            vector=[0.0, 1.0, 0.0, 0.0],
            payload={"repo_key": "drop/repo", "qualified_name": "y"},
        ),
    ]
    vector_store.upsert_batch(points, collection=cleanup_collection)

    removed = vector_store.clear_repo("drop/repo", collection=cleanup_collection)
    assert removed == 1
    assert vector_store.count(cleanup_collection, repo_key="keep/repo") == 1
    assert vector_store.count(cleanup_collection, repo_key="drop/repo") == 0


def test_upsert_is_idempotent(vector_store: VectorStore, cleanup_collection: str) -> None:
    vector_store.ensure_collection(cleanup_collection, dim=4)
    pt = VectorPoint(
        id=point_id_for("repo", "fn"),
        vector=[0.1, 0.2, 0.3, 0.4],
        payload={"repo_key": "repo", "qualified_name": "fn"},
    )
    vector_store.upsert_batch([pt], collection=cleanup_collection)
    vector_store.upsert_batch([pt], collection=cleanup_collection)
    assert vector_store.count(cleanup_collection) == 1


# --- Embedder end-to-end ---------------------------------------------------


@pytest.mark.asyncio
async def test_embedder_writes_one_chunk_per_function_and_class(
    vector_store: VectorStore,
    cleanup_collection: str,
    mock_router: ModelRouter,
    tmp_path: Path,
) -> None:
    src = """
def foo(x: int) -> int:
    return x + 1

class C:
    def m(self) -> None:
        pass
""".lstrip()
    (tmp_path / "ex.py").write_text(src)
    parsed = parse_source(src, SourceLanguage.python, path="ex.py", module_name="ex")

    embedder = Embedder(
        router=mock_router,
        vector_store=vector_store,
        repo_root=tmp_path,
        collection=cleanup_collection,
    )
    dim = await embedder.probe_dim()
    embedder.ensure_collection(dim)

    written = await embedder.embed_file("test/repo", parsed)
    # 1 top-level function (foo) + 1 class (C) + 1 method (C.m) = 3 chunks
    assert written == 3
    assert vector_store.count(cleanup_collection) == 3

    # Search with a query vector — order doesn't matter, just that all 3 IDs come back.
    hits = vector_store.search([0.5] * dim, limit=10, collection=cleanup_collection)
    qnames = {h.payload["qualified_name"] for h in hits}
    assert qnames == {"ex.foo", "ex.C", "ex.C.m"}


@pytest.mark.asyncio
async def test_embedder_reembed_is_idempotent(
    vector_store: VectorStore,
    cleanup_collection: str,
    mock_router: ModelRouter,
    tmp_path: Path,
) -> None:
    src = "def f(): pass\n"
    (tmp_path / "x.py").write_text(src)
    parsed = parse_source(src, SourceLanguage.python, path="x.py", module_name="x")

    embedder = Embedder(
        router=mock_router,
        vector_store=vector_store,
        repo_root=tmp_path,
        collection=cleanup_collection,
    )
    embedder.ensure_collection(await embedder.probe_dim())
    await embedder.embed_file("test/repo", parsed)
    await embedder.embed_file("test/repo", parsed)
    assert vector_store.count(cleanup_collection) == 1
