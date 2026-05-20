---
name: asil-confidence
description: Use when producing any conclusion in ASIL — retrieval result, causal claim, root-cause hypothesis, drift report. Enforces the Confidence object contract and explains what evidence to collect.
---

# asil-confidence

**Every conclusion ASIL emits ships with a `Confidence` object. Never strip it before returning to the user or persisting to memory.**

This is what turns ASIL from "another LLM that might be lying" into enterprise-trustable infrastructure. It is also the differentiator the v1 demo hangs on.

## The contract

```python
from asil_core import Confidence

@dataclass
class RootCauseHypothesis:
    summary: str
    implicated_commit: str | None
    affected_services: list[str]
    confidence: Confidence              # required field — never optional
```

## How to build a Confidence

```python
result = RootCauseHypothesis(
    summary="Redis timeout introduced in deployment 8f2c…",
    implicated_commit="8f2c1d4",
    affected_services=["payments", "cart"],
    confidence=Confidence(
        score=0.78,                       # 0..1, overall trust
        evidence_count=4,                  # number of independent supports
        retrieval_strength=0.82,           # avg similarity of supporting chunks
        causal_confidence=0.65,            # strength of causal edges traversed
        derivation=[
            "latency spike 3 min after deploy 8f2c1d4",
            "Redis timeout LogSignature observed in payments service",
            "deployment edge to payments service exists in graph",
            "similar historical incident 2024-08-14 with same signature",
        ],
    ),
)
```

## Rules

1. **`score ∈ [0, 1]`.** Use `Confidence.unknown()` if you genuinely have no signal — don't guess.
2. **`derivation` entries must be backed by data.** Each entry is a thing the user could click into and verify. No vibes.
3. **Don't inflate the score** to look more impressive. The verifier pass *downgrades* confidence when it finds unsupported claims — that's expected and correct.
4. **High confidence (>0.7) requires `evidence_count >= 3`** and at least one causal edge with explicit `derivation` source.
5. **Surfaced in every output.** API responses, CLI tables, MCP tool results — Confidence is never stripped.

## Computing the score

Phase 2 will ship the canonical scorer in `packages/asil_reasoning/scorer.py`. Until then, a defensible heuristic:

```python
score = (
    0.4 * retrieval_strength
    + 0.4 * causal_confidence
    + 0.2 * min(evidence_count / 5, 1.0)
)
```

Document any deviation from the canonical scorer in the function's docstring with the WHY.

## When the verifier downgrades

The reasoning pipeline runs a `verify` pass that checks every claim in the answer against the cited evidence. If it finds an unsupported claim:

```python
confidence = Confidence(
    score=original.score * 0.7,           # downgrade multiplier
    evidence_count=original.evidence_count,
    retrieval_strength=original.retrieval_strength,
    causal_confidence=original.causal_confidence,
    derivation=[*original.derivation, "verifier flagged: '<claim>' unsupported"],
)
```

Never delete the original derivation — append the verifier note so the provenance is auditable.
