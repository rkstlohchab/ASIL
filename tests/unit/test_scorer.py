"""Unit tests for the canonical Confidence scorer.

`score_retrieval` reproduces what the retriever used to compute inline;
`score_verified_answer` is new — it composes retrieval confidence with the
verifier's verdict via a fixed multiplier formula. These tests pin the
math so a future tweak to the discount curve doesn't silently shift every
downstream confidence number.
"""

from __future__ import annotations

import pytest
from asil_core import Confidence
from asil_memory import RetrievalCandidate
from asil_reasoning import VerifierClaim, VerifierResult, score_retrieval, score_verified_answer


def _cand(qname: str, score: float, source: str = "vector") -> RetrievalCandidate:
    return RetrievalCandidate(
        qualified_name=qname,
        name=qname.rsplit(".", 1)[-1],
        kind="function",
        file_path="x.py",
        start_line=1,
        end_line=5,
        score=score,
        source=source,
    )


# ---------------------------------------------------------------------------
# score_retrieval
# ---------------------------------------------------------------------------


def test_score_retrieval_returns_unknown_for_empty_candidates() -> None:
    conf = score_retrieval([], n_raw_hits=0)
    assert conf.score == 0.0
    assert conf.evidence_count == 0


def test_score_retrieval_uses_top_candidate_score_as_overall_score() -> None:
    conf = score_retrieval([_cand("a", 0.87)], n_raw_hits=5)
    assert conf.score == 0.87
    assert conf.evidence_count == 5


def test_score_retrieval_clamps_top_score_to_unit_range() -> None:
    # Pathological score > 1 should clamp; defensive against future re-rankers
    # that emit non-normalized values.
    conf = score_retrieval([_cand("a", 1.5)], n_raw_hits=1)
    assert conf.score == 1.0


def test_score_retrieval_avg_top3_excludes_zero_scores() -> None:
    cands = [
        _cand("a", 0.9),
        _cand("b", 0.7),
        _cand("c", 0.0, source="graph_expand"),  # graph-expanded with zero score
    ]
    conf = score_retrieval(cands, n_raw_hits=3)
    # Only the two positive scores contribute: (0.9 + 0.7) / 2 = 0.8
    assert abs(conf.retrieval_strength - 0.8) < 1e-9


def test_score_retrieval_counts_graph_expanded_in_derivation() -> None:
    cands = [
        _cand("a", 0.8),
        _cand("b", 0.4, source="graph_expand"),
        _cand("c", 0.3, source="graph_expand"),
    ]
    conf = score_retrieval(cands, n_raw_hits=1)
    expanded_line = next(d for d in conf.derivation if "graph-expanded" in d)
    assert "2" in expanded_line


# ---------------------------------------------------------------------------
# score_verified_answer
# ---------------------------------------------------------------------------


def _verifier(
    *,
    claims: list[VerifierClaim] | None = None,
    unsupported_count: int | None = None,
) -> VerifierResult:
    claims = claims or []
    return VerifierResult(
        answer="x",
        claims=claims,
        unsupported_count=unsupported_count
        if unsupported_count is not None
        else sum(1 for c in claims if not c.supported),
    )


def _retrieval(score: float = 0.6) -> Confidence:
    return Confidence(
        score=score,
        evidence_count=10,
        retrieval_strength=0.5,
        causal_confidence=0.0,
        derivation=["top hit: x"],
    )


def test_score_verified_answer_keeps_score_when_all_claims_supported() -> None:
    vr = _verifier(
        claims=[
            VerifierClaim(claim="c1", supported=True, citation="f:1"),
            VerifierClaim(claim="c2", supported=True, citation="f:2"),
        ]
    )
    out = score_verified_answer(_retrieval(score=0.6), vr)
    assert out.score == 0.6
    assert any("all claims supported" in d for d in out.derivation)


def test_score_verified_answer_discounts_per_unsupported_claim() -> None:
    # 1 unsupported -> 0.70x; 2 -> 0.40x; 3+ -> 0.20x floor
    base = _retrieval(score=1.0)
    cases = [
        (0, pytest.approx(1.0)),
        (1, pytest.approx(0.70)),
        (2, pytest.approx(0.40)),
        (3, pytest.approx(0.20)),
        (5, pytest.approx(0.20)),  # floor holds
    ]
    for n_unsup, expected in cases:
        claims = [VerifierClaim(claim=f"c{i}", supported=False) for i in range(n_unsup)]
        out = score_verified_answer(base, _verifier(claims=claims))
        assert out.score == expected, f"n_unsupported={n_unsup}"


def test_score_verified_answer_propagates_evidence_count_and_strength() -> None:
    base = Confidence(
        score=0.5,
        evidence_count=12,
        retrieval_strength=0.42,
        causal_confidence=0.18,
        derivation=["original"],
    )
    out = score_verified_answer(base, _verifier())
    assert out.evidence_count == 12
    assert out.retrieval_strength == 0.42
    assert out.causal_confidence == 0.18


def test_score_verified_answer_appends_derivation_lines() -> None:
    vr = _verifier(
        claims=[
            VerifierClaim(claim="a long claim about Neo4j", supported=False, citation=None),
        ]
    )
    out = score_verified_answer(_retrieval(), vr)
    flagged = [d for d in out.derivation if "unsupported" in d]
    assert len(flagged) >= 2  # one summary line + one per-claim line
    assert any("a long claim" in d for d in flagged)


def test_score_verified_answer_floors_score_at_zero_not_negative() -> None:
    # Even with a tiny retrieval score + many unsupported claims, never negative.
    out = score_verified_answer(
        _retrieval(score=0.05),
        _verifier(claims=[VerifierClaim(claim=f"c{i}", supported=False) for i in range(10)]),
    )
    assert out.score >= 0.0
    assert out.score <= 1.0
