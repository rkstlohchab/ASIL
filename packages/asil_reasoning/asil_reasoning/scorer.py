"""Canonical Confidence computation.

Phase 1 computed confidence ad-hoc inside `HybridRetriever`. Phase 2 moves
the math here so it's the single place to change scoring policy, and so the
verifier's downgrade composes cleanly with the retrieval signal.

Two entry points:
  - `score_retrieval(candidates, n_raw_hits)` — same shape as Phase 1's
    `_retrieval_confidence`. Used right after retrieval, before any LLM
    synthesis. Documents "how grounded is this question in the corpus".
  - `score_verified_answer(retrieval_conf, verifier_result)` — compose the
    retrieval confidence with the verifier's verdict on the answer. Documents
    "how grounded is *this answer* in the cited evidence".

We deliberately avoid an LLM-emitted confidence number. LLMs can't reliably
self-rate; the score has to come from observable signals (retrieval
similarity, claim-support count, eventually causal-edge strength). When the
verifier flags an unsupported claim, the multiplier is a contractual penalty,
not a prediction.
"""

from __future__ import annotations

from asil_core import Confidence

# How much each unsupported claim discounts the confidence multiplier.
# 0 unsupported  -> 1.00x
# 1 unsupported  -> 0.70x
# 2 unsupported  -> 0.40x
# 3+ unsupported -> 0.20x (floor)
#
# The values are deliberately harsh — an unsupported claim in a system meant
# to "explain reality with evidence" is a reputational hit, so we'd rather
# downgrade aggressively and surface the issue than pretend it's fine.
_UNSUPPORTED_PENALTY_PER_CLAIM = 0.30
_UNSUPPORTED_PENALTY_FLOOR = 0.20


def score_retrieval(
    candidates: list,  # list[RetrievalCandidate] — avoid import cycle, the dataclass shape is stable
    *,
    n_raw_hits: int,
) -> Confidence:
    """Confidence in *the retrieval* — how grounded is the question in the corpus."""
    if not candidates:
        return Confidence.unknown()

    top_score = max(0.0, min(1.0, _safe_score(candidates[0])))
    top3 = [_safe_score(c) for c in candidates[:3] if _safe_score(c) > 0]
    avg_top3_raw = sum(top3) / len(top3) if top3 else 0.0
    # Clamp to the Confidence-accepted range; defensive against future re-rankers
    # that emit non-normalized scores.
    avg_top3 = max(0.0, min(1.0, avg_top3_raw))
    graph_expanded = sum(1 for c in candidates if getattr(c, "source", None) == "graph_expand")

    return Confidence(
        score=top_score,
        evidence_count=n_raw_hits,
        retrieval_strength=avg_top3,
        causal_confidence=0.0,  # populated by Phase 4 temporal layer
        derivation=[
            f"top hit: {getattr(candidates[0], 'qualified_name', '?')} (score={top_score:.3f})",
            f"vector candidates: {n_raw_hits}",
            f"graph-expanded candidates: {graph_expanded}",
        ],
    )


def score_verified_answer(
    retrieval_conf: Confidence,
    verifier_result,  # asil_reasoning.VerifierResult — avoid import cycle
) -> Confidence:
    """Compose retrieval confidence with the verifier's verdict on the answer.

    The verifier's job is to detect claims in the answer that aren't supported
    by the retrieved evidence. Each unsupported claim discounts the score by a
    fixed amount; the multiplier floors at 0.2 so we never trivialize a partial
    answer to zero.
    """
    multiplier = _unsupported_multiplier(verifier_result.unsupported_count)
    new_score = max(0.0, min(1.0, retrieval_conf.score * multiplier))

    derivation = list(retrieval_conf.derivation)
    derivation.append(f"verifier checked {len(verifier_result.claims)} claims")
    if verifier_result.unsupported_count > 0:
        derivation.append(
            f"verifier flagged {verifier_result.unsupported_count} unsupported "
            f"claim(s) - confidence x {multiplier:.2f}"
        )
        for claim in verifier_result.claims:
            if not claim.supported:
                derivation.append(f'unsupported: "{_clip(claim.claim, 120)}"')
    else:
        derivation.append("all claims supported by cited evidence")

    return Confidence(
        score=new_score,
        evidence_count=retrieval_conf.evidence_count,
        retrieval_strength=retrieval_conf.retrieval_strength,
        causal_confidence=retrieval_conf.causal_confidence,
        derivation=derivation,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _unsupported_multiplier(unsupported_count: int) -> float:
    raw = 1.0 - _UNSUPPORTED_PENALTY_PER_CLAIM * max(0, unsupported_count)
    return max(_UNSUPPORTED_PENALTY_FLOOR, raw)


def _safe_score(candidate) -> float:
    try:
        return float(candidate.score)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _clip(s: str, max_len: int) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= max_len else s[: max_len - 1] + "…"
