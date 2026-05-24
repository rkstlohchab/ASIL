"""Unit tests for the second-pass verifier.

The verifier is one LLM call wrapped in strict prompt + parsing. Tests fake
the router so we can pin: prompt shape, parsing fallbacks (strict JSON, fenced
JSON, regex-matched JSON object), skip semantics (empty answer, no candidates,
router error), and counting of unsupported claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from asil_memory import RetrievalCandidate
from asil_reasoning import Verifier, VerifierResult

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeResp:
    text: str
    cost_usd: float = 0.0


class FakeRouter:
    """Replays canned responses, capturing the last request."""

    def __init__(self, text: str = '{"claims": []}', cost_usd: float = 0.0) -> None:
        self.text = text
        self.cost_usd = cost_usd
        self.last_call: dict[str, Any] | None = None
        self.error: Exception | None = None

    async def call(self, tier: Any, messages: list[dict[str, Any]], **kw: Any) -> _FakeResp:
        self.last_call = {"tier": tier, "messages": messages, "kw": kw}
        if self.error:
            raise self.error
        return _FakeResp(text=self.text, cost_usd=self.cost_usd)


def _cand(qname: str, *, file_path: str = "x.py", start_line: int = 1) -> RetrievalCandidate:
    return RetrievalCandidate(
        qualified_name=qname,
        name=qname.rsplit(".", 1)[-1],
        kind="function",
        file_path=file_path,
        start_line=start_line,
        end_line=start_line + 5,
        score=0.8,
        source="vector",
        signature="()",
        docstring="example",
        text=f"def {qname.rsplit('.', 1)[-1]}(): pass",
    )


# ---------------------------------------------------------------------------
# skip paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_skips_when_answer_is_empty() -> None:
    router = FakeRouter()
    v = Verifier(router=router)
    result = await v.verify("q", "", [_cand("a.b")])
    assert result.skipped is True
    assert "empty answer" in (result.skip_reason or "")
    assert router.last_call is None


@pytest.mark.asyncio
async def test_verifier_skips_when_no_candidates() -> None:
    router = FakeRouter()
    v = Verifier(router=router)
    result = await v.verify("q", "answer", [])
    assert result.skipped is True
    assert "no candidates" in (result.skip_reason or "")


@pytest.mark.asyncio
async def test_verifier_skips_gracefully_on_router_error() -> None:
    router = FakeRouter()
    router.error = RuntimeError("network down")
    v = Verifier(router=router)
    result = await v.verify("q", "answer", [_cand("a.b")])
    assert result.skipped is True
    assert "RuntimeError" in (result.skip_reason or "")
    assert result.claims == []


# ---------------------------------------------------------------------------
# happy path + parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_parses_strict_json_response() -> None:
    payload = (
        '{"claims": ['
        '{"claim": "GraphStore connects to neo4j", "supported": true, '
        '"citation": "graph_store.py:116", "reason": "verify_connectivity"}, '
        '{"claim": "GraphStore uses HTTP", "supported": false, '
        '"citation": null, "reason": "no snippet mentions HTTP"}'
        "]}"
    )
    v = Verifier(router=FakeRouter(text=payload, cost_usd=0.0003))
    result = await v.verify("how does GraphStore work?", "answer", [_cand("pkg.GraphStore")])
    assert len(result.claims) == 2
    assert result.claims[0].supported is True
    assert result.claims[0].citation == "graph_store.py:116"
    assert result.claims[1].supported is False
    assert result.claims[1].citation is None
    assert result.unsupported_count == 1
    assert result.cost_usd == 0.0003
    assert not result.skipped


@pytest.mark.asyncio
async def test_verifier_parses_response_wrapped_in_fenced_block() -> None:
    payload = (
        "Sure, here's the analysis:\n"
        "```json\n"
        '{"claims": [{"claim": "C", "supported": true, "citation": "f.py:1", "reason": "ok"}]}\n'
        "```\n"
        "Hope that helps!"
    )
    v = Verifier(router=FakeRouter(text=payload))
    result = await v.verify("q", "answer", [_cand("a.b")])
    assert len(result.claims) == 1
    assert result.claims[0].supported is True


@pytest.mark.asyncio
async def test_verifier_parses_response_via_regex_fallback() -> None:
    payload = (
        "Looking at the answer, my analysis is "
        '{"claims": [{"claim": "X", "supported": false, "citation": null, "reason": "missing"}]} '
        "and that's all."
    )
    v = Verifier(router=FakeRouter(text=payload))
    result = await v.verify("q", "answer", [_cand("a.b")])
    assert len(result.claims) == 1
    assert result.claims[0].supported is False
    assert result.unsupported_count == 1


@pytest.mark.asyncio
async def test_verifier_returns_empty_claims_when_parse_fails() -> None:
    v = Verifier(router=FakeRouter(text="completely unparseable nonsense"))
    result = await v.verify("q", "answer", [_cand("a.b")])
    # The verifier doesn't skip — it ran the call — but found nothing to parse.
    assert result.skipped is False
    assert result.claims == []
    assert result.unsupported_count == 0


# ---------------------------------------------------------------------------
# prompt shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_sends_system_prompt_and_includes_snippets_in_user_message() -> None:
    router = FakeRouter()
    v = Verifier(router=router)
    candidates = [_cand("pkg.foo", file_path="foo.py", start_line=42)]
    await v.verify("q?", "A. B.", candidates)
    assert router.last_call is not None
    user_msg = router.last_call["messages"][0]["content"]
    assert "Question:" in user_msg
    assert "q?" in user_msg
    assert "Answer to fact-check:" in user_msg
    assert "A. B." in user_msg
    assert "foo.py:42" in user_msg  # candidate citation in the snippet header
    # Tier should be the verify tier (or fall back).
    assert router.last_call["kw"]["system"] is not None
    assert "fact-checker" in router.last_call["kw"]["system"]


@pytest.mark.asyncio
async def test_verifier_caps_claims_count() -> None:
    # Cook up a payload with 12 claims; cap should trim to max_claims.
    many = (
        '{"claims": ['
        + ",".join(
            f'{{"claim": "c{i}", "supported": true, "citation": "f.py:{i}", "reason": "r"}}'
            for i in range(12)
        )
        + "]}"
    )
    v = Verifier(router=FakeRouter(text=many))
    result = await v.verify("q", "answer", [_cand("a.b")], max_claims=5)
    assert len(result.claims) == 5


# ---------------------------------------------------------------------------
# dataclass surface
# ---------------------------------------------------------------------------


def test_verifier_result_default_state() -> None:
    r = VerifierResult(answer="a")
    assert r.claims == []
    assert r.unsupported_count == 0
    assert r.skipped is False
    assert r.cost_usd == 0.0
