"""ASIL temporal causality engine — the moat.

What this package adds that nothing else in ASIL does:

  Phase 1 → "given a question, find the right code"
  Phase 2 → "verify the answer is supported by the code"
  Phase 3 → "given a runtime event, find related events in time"
  Phase 4 → "given an outcome (incident), derive WHICH events CAUSED it,
            with confidence, derivation, and evidence — and write those
            causal claims as first-class graph edges that downstream
            reasoning, replay, and drift detection all consume."

The Phase 4 step 1 surface is the temporal-proximity linker. Given an
Incident, walk events within a configurable lookback window, score each
candidate by time-distance (closer in time = higher proximity confidence,
decaying with a half-life), and write `(:Cause)-[:PRECEDED]->(:Incident)`
edges with the score, the time delta, and a derivation string explaining
how the score was computed.

Future Phase 4 steps add:
  - Lagged-correlation scoring for paired time series (MetricShift vs Deployment)
  - Explicit-reference scoring (commit message mentions incident ID → high confidence)
  - Co-occurrence reinforcement (deploys that consistently precede the same
    metric shifts get reinforced edges)
  - Time-windowed graph queries ("as-of T") for state diffs

We deliberately avoid LLM-emitted causality. LLMs hallucinate causes;
observable signals (Δt, correlation, explicit reference) don't. The
causal edges this engine writes are *observations*, not predictions —
and they ship with Confidence + derivation, the same hard rule as
every other ASIL output.
"""

from asil_temporal.linker import (
    CausalCandidate,
    CausalLinkStats,
    TemporalLinker,
    find_causes,
)

__version__ = "0.0.1"

__all__ = [
    "CausalCandidate",
    "CausalLinkStats",
    "TemporalLinker",
    "find_causes",
]
