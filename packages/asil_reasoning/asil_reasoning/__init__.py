"""ASIL reasoning pipeline.

Phase 2 ships two pieces:
  - Scorer: canonical computation of the `Confidence` dataclass.
  - Verifier: second LLM pass that checks each claim in an answer against
    the cited retrieval candidates. Returns the same answer plus a list of
    supported/unsupported claims and a confidence multiplier.

Phase 4/5 add the full deterministic pipeline (Retrieve → Graph → Temporal →
Reason → Verify → Score → Respond) on top of these building blocks.
"""

from asil_reasoning.scorer import (
    score_retrieval,
    score_verified_answer,
)
from asil_reasoning.verifier import (
    Verifier,
    VerifierClaim,
    VerifierResult,
)

__version__ = "0.0.1"

__all__ = [
    "Verifier",
    "VerifierClaim",
    "VerifierResult",
    "score_retrieval",
    "score_verified_answer",
]
