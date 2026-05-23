"""Unit tests for HybridRetriever.

The retriever's contract is: given a query, return a ranked list of candidates
where each candidate carries enough provenance to cite a downstream claim.
These tests fake out both stores so the retriever is exercised purely on its
own logic — dedupe, ranking, confidence scoring, graph-expand bounds.
"""

from __future__ import annotations

from typing import Any

import pytest
from asil_memory import HybridRetriever, SearchHit

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeVectorStore:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.last_query_args: dict[str, Any] = {}

    def search(
        self,
        query_vector: list[float],
        *,
        limit: int = 10,
        repo_key: str | None = None,
        kind: str | None = None,
    ) -> list[SearchHit]:
        self.last_query_args = {"limit": limit, "repo_key": repo_key, "kind": kind}
        return self._hits[:limit]


class FakeGraphStore:
    def __init__(self, neighbor_rows: list[dict[str, Any]]) -> None:
        self._rows = neighbor_rows
        self.calls: list[dict[str, Any]] = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params})
        return self._rows


class FakeEmbedder:
    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        # Deterministic but distinct per-input vectors so the retriever can
        # tell apart "embed query" vs "embed something else" if it had a reason to.
        return [[float(i % self._dim) / self._dim for i in range(self._dim)] for _ in texts]


def _hit(
    qname: str,
    score: float,
    *,
    kind: str = "function",
    file_path: str = "x.py",
    start_line: int = 1,
) -> SearchHit:
    return SearchHit(
        id=qname,
        score=score,
        payload={
            "qualified_name": qname,
            "name": qname.rsplit(".", 1)[-1],
            "kind": kind,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": start_line + 5,
            "signature": "()",
            "docstring": None,
            "text": f"# {qname}",
        },
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_vector_hits_in_descending_score_order() -> None:
    vstore = FakeVectorStore([_hit("a.x", 0.9), _hit("a.y", 0.7), _hit("a.z", 0.5)])
    gstore = FakeGraphStore([])
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=FakeEmbedder(),
        final_limit=10,
    )

    result = await retriever.retrieve("test query")
    qnames = [c.qualified_name for c in result.candidates]
    assert qnames == ["a.x", "a.y", "a.z"]
    assert result.candidates[0].source == "vector"
    assert result.candidates[0].score == 0.9


@pytest.mark.asyncio
async def test_graph_expand_adds_neighbors_without_displacing_vector_hits() -> None:
    vstore = FakeVectorStore([_hit("pkg.Foo.bar", 0.9)])
    gstore = FakeGraphStore(
        [
            {
                "label": "Class",
                "qualified_name": "pkg.Foo",
                "name": "Foo",
                "file_path": "x.py",
                "start_line": 1,
                "end_line": 30,
                "signature": None,
                "docstring": "container class",
                "parent_class": None,
            },
            {
                "label": "Function",
                "qualified_name": "pkg.Foo.baz",
                "name": "baz",
                "file_path": "x.py",
                "start_line": 20,
                "end_line": 25,
                "signature": "(self)",
                "docstring": None,
                "parent_class": "pkg.Foo",
            },
        ]
    )
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=FakeEmbedder(),
        final_limit=10,
    )

    result = await retriever.retrieve("test query")
    sources = {c.qualified_name: c.source for c in result.candidates}
    assert sources["pkg.Foo.bar"] == "vector"
    assert sources["pkg.Foo"] == "graph_expand"
    assert sources["pkg.Foo.baz"] == "graph_expand"
    # The vector hit stays at the top — graph-expanded score is capped below.
    assert result.candidates[0].qualified_name == "pkg.Foo.bar"


@pytest.mark.asyncio
async def test_dedup_when_graph_expand_returns_a_vector_hit_qname() -> None:
    vstore = FakeVectorStore([_hit("pkg.Foo.bar", 0.85)])
    gstore = FakeGraphStore(
        [
            {  # neighbor row that happens to match a vector hit
                "label": "Function",
                "qualified_name": "pkg.Foo.bar",
                "name": "bar",
                "file_path": "x.py",
                "start_line": 10,
                "end_line": 15,
                "signature": "()",
                "docstring": None,
                "parent_class": "pkg.Foo",
            }
        ]
    )
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=FakeEmbedder(),
    )
    result = await retriever.retrieve("test")
    qnames = [c.qualified_name for c in result.candidates]
    # Exactly one entry; the vector source wins.
    assert qnames.count("pkg.Foo.bar") == 1
    assert result.candidates[0].source == "vector"


@pytest.mark.asyncio
async def test_respects_repo_and_kind_filters_passed_through_to_vector() -> None:
    vstore = FakeVectorStore([_hit("a.x", 0.8)])
    gstore = FakeGraphStore([])
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=vstore,
        embedder=FakeEmbedder(),
    )
    await retriever.retrieve("q", repo_key="some/repo", kind="class")
    assert vstore.last_query_args["repo_key"] == "some/repo"
    assert vstore.last_query_args["kind"] == "class"


@pytest.mark.asyncio
async def test_returns_unknown_confidence_when_nothing_matches() -> None:
    retriever = HybridRetriever(
        graph_store=FakeGraphStore([]),
        vector_store=FakeVectorStore([]),
        embedder=FakeEmbedder(),
    )
    result = await retriever.retrieve("query with no hits")
    assert result.candidates == []
    assert result.confidence.score == 0.0
    assert result.confidence.evidence_count == 0


@pytest.mark.asyncio
async def test_confidence_reflects_top_score_and_evidence_count() -> None:
    vstore = FakeVectorStore(
        [_hit("a.x", 0.92), _hit("a.y", 0.88), _hit("a.z", 0.80), _hit("a.w", 0.71)]
    )
    retriever = HybridRetriever(
        graph_store=FakeGraphStore([]),
        vector_store=vstore,
        embedder=FakeEmbedder(),
    )
    result = await retriever.retrieve("q")
    assert result.confidence.score == 0.92
    assert result.confidence.evidence_count == 4
    # Average of top-3: (0.92 + 0.88 + 0.80) / 3
    assert abs(result.confidence.retrieval_strength - (0.92 + 0.88 + 0.80) / 3) < 1e-9


@pytest.mark.asyncio
async def test_expand_hops_zero_skips_graph_entirely() -> None:
    gstore = FakeGraphStore([{"label": "Class", "qualified_name": "should.not.appear"}])
    retriever = HybridRetriever(
        graph_store=gstore,
        vector_store=FakeVectorStore([_hit("a.x", 0.9)]),
        embedder=FakeEmbedder(),
        expand_hops=0,
    )
    result = await retriever.retrieve("q")
    assert [c.qualified_name for c in result.candidates] == ["a.x"]
    assert gstore.calls == []  # graph never touched
