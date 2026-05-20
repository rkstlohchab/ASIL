from __future__ import annotations

from datetime import UTC

import pytest
from asil_core.llm import (
    InMemoryCostLedger,
    MockEmbeddingProvider,
    MockLLMProvider,
    ModelRouter,
    Profile,
)


def _profile(name: str, mock: MockLLMProvider) -> Profile:
    return Profile(
        name=name,
        chat={
            "reasoning": mock,
            "classify": mock,
            "summarize": mock,
            "verify": mock,
        },
        embedding=MockEmbeddingProvider(),
    )


@pytest.mark.asyncio
async def test_router_dispatches_to_provider_for_tier() -> None:
    mock = MockLLMProvider(canned_response="hello from mock")
    router = ModelRouter(profile=_profile("mock", mock))

    resp = await router.call(
        tier="reasoning",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert resp.text == "hello from mock"
    assert resp.provider == "mock"
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_router_records_cost_in_ledger() -> None:
    mock = MockLLMProvider(
        canned_response="ok",
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )
    ledger = InMemoryCostLedger()
    router = ModelRouter(profile=_profile("mock", mock), ledger=ledger)

    await router.call(tier="reasoning", messages=[{"role": "user", "content": "x"}])

    records = ledger.all_records()
    assert len(records) == 1
    assert records[0].tier == "reasoning"
    assert records[0].profile == "mock"
    # mock provider reports 10 in / 5 out tokens
    assert records[0].input_tokens == 10
    assert records[0].output_tokens == 5


@pytest.mark.asyncio
async def test_router_downgrades_to_fallback_when_budget_exceeded() -> None:
    primary = MockLLMProvider(canned_response="primary")
    fallback = MockLLMProvider(canned_response="fallback")

    ledger = InMemoryCostLedger()
    # pre-load ledger so spend_today_usd exceeds the budget
    from datetime import datetime

    from asil_core.llm import CostRecord

    await ledger.record(
        CostRecord(
            timestamp=datetime.now(UTC),
            provider="seed",
            model="seed",
            tier="reasoning",
            input_tokens=0,
            output_tokens=0,
            cost_usd=10.0,
            profile="primary",
        )
    )

    router = ModelRouter(
        profile=_profile("primary", primary),
        fallback=_profile("fallback", fallback),
        ledger=ledger,
        budget_usd=1.0,
    )

    resp = await router.call(tier="reasoning", messages=[{"role": "user", "content": "x"}])

    assert resp.text == "fallback"
    assert router.active_profile_name == "fallback"


@pytest.mark.asyncio
async def test_router_embed_uses_embedding_provider() -> None:
    mock = MockLLMProvider()
    router = ModelRouter(profile=_profile("mock", mock))

    vecs = await router.embed(["alpha", "beta"])

    assert len(vecs) == 2
    assert all(len(v) == 8 for v in vecs)


@pytest.mark.asyncio
async def test_router_rejects_embed_via_call() -> None:
    mock = MockLLMProvider()
    router = ModelRouter(profile=_profile("mock", mock))

    with pytest.raises(ValueError, match="embed"):
        await router.call(tier="embed", messages=[])  # type: ignore[arg-type]
