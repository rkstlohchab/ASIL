"""Confidence — the score every ASIL conclusion ships with.

Never strip this before showing the user. Every retrieval, every causal claim,
every root-cause hypothesis carries one of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Confidence:
    score: float  # 0.0–1.0 overall
    evidence_count: int  # how many independent supports
    retrieval_strength: float = 0.0  # avg similarity of supporting chunks
    causal_confidence: float = 0.0  # strength of any causal edges used
    derivation: list[str] = field(default_factory=list)  # human-readable supports

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0,1], got {self.score}")
        if not 0.0 <= self.retrieval_strength <= 1.0:
            raise ValueError(
                f"retrieval_strength must be in [0,1], got {self.retrieval_strength}"
            )
        if not 0.0 <= self.causal_confidence <= 1.0:
            raise ValueError(
                f"causal_confidence must be in [0,1], got {self.causal_confidence}"
            )
        if self.evidence_count < 0:
            raise ValueError(f"evidence_count must be >= 0, got {self.evidence_count}")

    @classmethod
    def unknown(cls) -> Confidence:
        return cls(score=0.0, evidence_count=0, derivation=["no evidence"])
