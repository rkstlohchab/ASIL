"""Unit tests for the eval harness.

The harness wraps the retriever and reports rank-of-first-hit per case.
We mock the retriever to control exactly what comes back so we can pin
the recall@K math.
"""

from __future__ import annotations

from typing import Any

import pytest
from asil_core import Confidence
from asil_eval import (
    EvalCase,
    EvalCorpus,
    load_corpus,
    run_recall,
)
from asil_memory import RetrievalCandidate, RetrievalResult


class FakeRetriever:
    """Minimal stand-in for HybridRetriever. Replays canned candidates per query."""

    def __init__(self, query_to_qnames: dict[str, list[str]]) -> None:
        self._q = query_to_qnames

    async def retrieve(
        self, query: str, *, repo_key: str | None = None, kind: str | None = None
    ) -> RetrievalResult:
        qnames = self._q.get(query, [])
        candidates = [
            RetrievalCandidate(
                qualified_name=qn,
                name=qn.rsplit(".", 1)[-1],
                kind="function",
                file_path="x.py",
                start_line=1,
                end_line=5,
                score=1.0 - 0.01 * i,
                source="vector",
            )
            for i, qn in enumerate(qnames)
        ]
        return RetrievalResult(
            query=query,
            candidates=candidates,
            confidence=Confidence(score=0.5, evidence_count=len(candidates)),
        )


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------


def test_loads_builtin_asil_self_corpus() -> None:
    corpus = load_corpus("asil_self")
    assert corpus.name == "asil_self"
    assert len(corpus.cases) >= 10
    # Every case should at least nominate one expected answer.
    assert all(c.expected_any for c in corpus.cases)


def test_loads_corpus_from_filesystem_path(tmp_path: Any) -> None:
    yaml_text = """
name: tiny
repo_key: local:/x
cases:
  - question: "hi?"
    expected_any: ["pkg.bye"]
"""
    p = tmp_path / "tiny.yaml"
    p.write_text(yaml_text)
    corpus = load_corpus(str(p))
    assert corpus.name == "tiny"
    assert corpus.cases[0].question == "hi?"
    assert corpus.cases[0].expected_any == ["pkg.bye"]


def test_load_corpus_raises_on_missing_path() -> None:
    with pytest.raises(FileNotFoundError):
        load_corpus("/definitely/does/not/exist.yaml")


# ---------------------------------------------------------------------------
# recall math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_at_one_when_top_hit_matches_expected() -> None:
    corpus = EvalCorpus(
        name="t",
        repo_key="r",
        cases=[EvalCase(question="q", expected_any=["pkg.target"])],
    )
    retriever = FakeRetriever({"q": ["pkg.target", "pkg.other"]})
    result = await run_recall(corpus, retriever=retriever)  # type: ignore[arg-type]
    assert result.recall_at(1) == 1.0


@pytest.mark.asyncio
async def test_recall_at_three_misses_when_match_is_at_rank_five() -> None:
    corpus = EvalCorpus(
        name="t",
        repo_key="r",
        cases=[EvalCase(question="q", expected_any=["pkg.target"])],
    )
    retriever = FakeRetriever({"q": ["a", "b", "c", "d", "pkg.target"]})
    result = await run_recall(corpus, retriever=retriever)  # type: ignore[arg-type]
    assert result.recall_at(1) == 0.0
    assert result.recall_at(3) == 0.0
    assert result.recall_at(5) == 1.0
    assert result.recall_at(10) == 1.0


@pytest.mark.asyncio
async def test_suffix_match_allows_short_expected_qnames() -> None:
    """Corpora can specify the tail-only qname; the harness must match it
    against any qname whose suffix is the expected string."""
    corpus = EvalCorpus(
        name="t",
        repo_key="r",
        cases=[EvalCase(question="q", expected_any=["target"])],
    )
    retriever = FakeRetriever({"q": ["really.long.module.path.target"]})
    result = await run_recall(corpus, retriever=retriever)  # type: ignore[arg-type]
    assert result.recall_at(1) == 1.0


@pytest.mark.asyncio
async def test_misses_dont_break_aggregation() -> None:
    corpus = EvalCorpus(
        name="t",
        repo_key="r",
        cases=[
            EvalCase(question="q1", expected_any=["pkg.hit"]),
            EvalCase(question="q2", expected_any=["pkg.never_returned"]),
        ],
    )
    retriever = FakeRetriever({"q1": ["pkg.hit"], "q2": ["nothing", "else"]})
    result = await run_recall(corpus, retriever=retriever)  # type: ignore[arg-type]
    assert result.recall_at(1) == 0.5  # 1 of 2 cases passes
    assert result.cases[0].hit_rank == 1
    assert result.cases[1].hit_rank is None


@pytest.mark.asyncio
async def test_summary_dict_includes_all_recall_buckets() -> None:
    corpus = EvalCorpus(name="t", repo_key="r", cases=[])
    retriever = FakeRetriever({})
    result = await run_recall(corpus, retriever=retriever)  # type: ignore[arg-type]
    summary = result.summary()
    assert set(summary) >= {
        "corpus",
        "repo_key",
        "n_cases",
        "recall@1",
        "recall@3",
        "recall@5",
        "recall@10",
    }
