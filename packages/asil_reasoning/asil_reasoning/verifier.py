"""Second-LLM verification pass.

Runs after the answer LLM produces its response. Sends the (question, answer,
candidate snippets) back to the router with a strict "for each claim, is it
supported?" prompt. Returns the parsed claim list + a count of unsupported
claims that downstream scoring uses to discount the Confidence.

Why a second pass and not "just trust the first model"?
  - The answer LLM optimizes for fluent, useful output. Fluency is easy to
    confuse with correctness — a well-written sentence backed by nothing is
    indistinguishable from one backed by good evidence at the surface level.
  - The verifier optimizes for adversarial reading: "show me where this
    specific claim is supported." Different objective, different prompt,
    different model state — catches the failure modes the first pass missed.
  - Cost: ~1 extra LLM call per `ask`. On the `tight` profile that's
    ~$0.0003-0.0010 per question. Easy ROI for trustable Confidence.
  - This is the "one critique pass" allowed by CLAUDE.md's deterministic-
    pipelines-over-multi-agent rule. We DON'T loop; one verifier call max,
    then the result is final.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VerifierClaim:
    """One claim extracted from the answer, with its support verdict."""

    claim: str  # the claim text the verifier identified
    supported: bool  # did any cited snippet back it?
    citation: str | None = None  # qname or file:line of the supporting snippet, if any
    reason: str | None = None  # short justification from the verifier


@dataclass(slots=True)
class VerifierResult:
    answer: str  # echoed back unchanged — we don't rewrite the answer
    claims: list[VerifierClaim] = field(default_factory=list)
    unsupported_count: int = 0
    raw_verifier_text: str = ""
    cost_usd: float = 0.0
    skipped: bool = False  # True if we couldn't run a verifier pass at all
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# Router protocol — minimal contract we need from ModelRouter
# ---------------------------------------------------------------------------


class _ChatRouter(Protocol):
    async def call(
        self,
        tier: Any,
        messages: list[dict[str, Any]],
        *,
        system: str | None = ...,
        max_tokens: int = ...,
        temperature: float = ...,
        **kw: Any,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


_VERIFY_SYSTEM_PROMPT = (
    "You are a strict fact-checker for ASIL. You receive a question, an answer, "
    "and the code snippets that were available to the answer's author. Your only "
    "job is to identify each concrete factual claim in the answer and decide "
    "whether it is supported by the snippets.\n\n"
    "Rules:\n"
    "  1. Output STRICTLY valid JSON: an object with a single key `claims`, whose "
    "value is an array of objects with fields `claim` (string), `supported` "
    "(boolean), `citation` (string or null), `reason` (short string). No prose "
    "outside the JSON.\n"
    "  2. A claim is 'supported' only if a snippet literally backs it. Plausibility "
    "is not support.\n"
    "  3. If the answer makes no concrete factual claims (e.g. it says 'I don't "
    "have evidence'), return `claims: []` — that's a valid honest answer.\n"
    "  4. Cite by the snippet header given to you (e.g. 'graph_store.py:116'). "
    "If no snippet supports the claim, set `citation: null`.\n"
    "  5. Keep each `claim` short — extract the bare assertion, not the wrapping "
    "prose.\n"
    "  6. Aim for 1-6 claims per answer; combine closely-related sentences into "
    "one claim if they share a citation."
)


class Verifier:
    """Per-`ask` verifier. Stateless — construct once per process; safe to share."""

    def __init__(self, router: _ChatRouter, *, tier: str = "verify") -> None:
        self._router = router
        self._tier = tier

    async def verify(
        self,
        question: str,
        answer: str,
        candidates: list,  # list[RetrievalCandidate] — duck-typed to avoid import cycle
        *,
        max_claims: int = 8,
    ) -> VerifierResult:
        if not answer or not answer.strip():
            return VerifierResult(
                answer=answer,
                claims=[],
                skipped=True,
                skip_reason="empty answer",
            )
        if not candidates:
            return VerifierResult(
                answer=answer,
                claims=[],
                skipped=True,
                skip_reason="no candidates to verify against",
            )

        prompt = _build_verify_prompt(question, answer, candidates)
        try:
            resp = await self._router.call(
                tier=self._tier,
                messages=[{"role": "user", "content": prompt}],
                system=_VERIFY_SYSTEM_PROMPT,
                max_tokens=700,
                temperature=0.0,
            )
        except Exception as e:
            log.warning("verifier_call_failed", err=str(e))
            return VerifierResult(
                answer=answer,
                claims=[],
                skipped=True,
                skip_reason=f"router error: {type(e).__name__}",
            )

        claims = _parse_claims(resp.text, cap=max_claims)
        unsupported = sum(1 for c in claims if not c.supported)
        log.info(
            "verifier_done",
            n_claims=len(claims),
            n_unsupported=unsupported,
            cost_usd=round(getattr(resp, "cost_usd", 0.0) or 0.0, 6),
        )
        return VerifierResult(
            answer=answer,
            claims=claims,
            unsupported_count=unsupported,
            raw_verifier_text=resp.text,
            cost_usd=float(getattr(resp, "cost_usd", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# Prompt + parsing helpers
# ---------------------------------------------------------------------------


def _build_verify_prompt(question: str, answer: str, candidates: list) -> str:
    lines = [
        f"Question:\n{question}",
        "",
        f"Answer to fact-check:\n{answer}",
        "",
        "Snippets that were available when the answer was written:",
    ]
    for i, c in enumerate(candidates, 1):
        loc = f"{getattr(c, 'file_path', '?')}:{getattr(c, 'start_line', '?')}"
        header = f"[{i}] {getattr(c, 'qualified_name', '?')}  —  {loc}"
        if getattr(c, "signature", None):
            header += f"  signature: {c.signature}"
        lines.append("")
        lines.append(header)
        if getattr(c, "docstring", None):
            lines.append(f"  doc: {c.docstring.strip()[:300]}")
        text = getattr(c, "text", "") or ""
        if text:
            snippet = text if len(text) <= 1200 else text[:1200] + "\n  …"
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    lines.extend(
        [
            "",
            "Now output the claims as JSON: "
            '{"claims": [{"claim": "...", "supported": true|false, '
            '"citation": "file.py:N" or null, "reason": "..."}]}',
        ]
    )
    return "\n".join(lines)


def _parse_claims(verifier_text: str, *, cap: int) -> list[VerifierClaim]:
    """Extract the claims list from the verifier's response.

    The system prompt asks for strict JSON, but real models occasionally wrap
    it in prose or a fenced code block. Try strict parse first, then a regex
    fallback that pulls the JSON object out of surrounding noise. If both fail,
    return an empty list (treated as "no claims verified" by the scorer, which
    floors the multiplier conservatively).
    """
    data = _try_parse_json(verifier_text) or _try_parse_json_in_fence(verifier_text)
    if data is None or not isinstance(data, dict):
        log.warning("verifier_parse_failed", text_preview=verifier_text[:200])
        return []
    raw_claims = data.get("claims")
    if not isinstance(raw_claims, list):
        return []

    out: list[VerifierClaim] = []
    for entry in raw_claims[:cap]:
        if not isinstance(entry, dict):
            continue
        claim_text = entry.get("claim")
        if not isinstance(claim_text, str) or not claim_text.strip():
            continue
        supported = bool(entry.get("supported", False))
        citation_raw = entry.get("citation")
        citation = (
            citation_raw
            if isinstance(citation_raw, str)
            and citation_raw.strip()
            and citation_raw.lower() != "null"
            else None
        )
        reason_raw = entry.get("reason")
        reason = reason_raw if isinstance(reason_raw, str) and reason_raw.strip() else None
        out.append(
            VerifierClaim(
                claim=claim_text.strip(),
                supported=supported,
                citation=citation,
                reason=reason,
            )
        )
    return out


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJECT_RE = re.compile(r"\{[^{}]*\"claims\".*\}", re.DOTALL)


def _try_parse_json_in_fence(text: str) -> dict[str, Any] | None:
    m = _FENCE_RE.search(text)
    if m:
        return _try_parse_json(m.group(1))
    # last-ditch: regex out the object that contains "claims"
    m = _BARE_OBJECT_RE.search(text)
    if m:
        return _try_parse_json(m.group(0))
    return None
