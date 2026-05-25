"""ModelRouter — the single entry point for every LLM call in ASIL.

Every prompt site MUST call `router.call(tier=...)`. Hardcoding a model name
in business code is a layering violation and will be caught in review.

Responsibilities:
  • Dispatch the call to the provider configured for the (profile, tier).
  • Record cost via the CostLedger.
  • If today's spend exceeds the configured budget, transparently downgrade
    to a `fallback` profile (typically `tight`). Logged but never raised.
"""

from __future__ import annotations

from datetime import UTC, datetime

from asil_core.config import LLMProfileName, get_settings
from asil_core.llm.ledger import CostLedger, CostRecord, InMemoryCostLedger
from asil_core.llm.profiles import Profile, Tier, load_profile
from asil_core.llm.providers import EmbeddingProvider, LLMProvider
from asil_core.llm.types import CompletionRequest, CompletionResponse
from asil_core.logging import get_logger

log = get_logger(__name__)


class ModelRouter:
    def __init__(
        self,
        profile: Profile,
        ledger: CostLedger | None = None,
        budget_usd: float | None = None,
        fallback: Profile | None = None,
    ) -> None:
        self._primary = profile
        self._active = profile
        self._fallback = fallback
        self._ledger = ledger or InMemoryCostLedger()
        self._budget_usd = budget_usd

    @classmethod
    def from_env(
        cls,
        ledger: CostLedger | None = None,
        profile_name: LLMProfileName | None = None,
    ) -> ModelRouter:
        settings = get_settings()
        primary = load_profile(profile_name, settings=settings)
        fallback = load_profile("tight", settings=settings) if primary.name != "tight" else None
        if ledger is None:
            # Prefer Postgres so cost history survives API restarts. Falls back
            # to the in-memory ledger if Postgres is unreachable so unit tests
            # and offline development never break.
            from asil_core.llm.postgres_ledger import from_settings_or_none

            ledger = from_settings_or_none() or InMemoryCostLedger()
        return cls(
            profile=primary,
            ledger=ledger,
            budget_usd=settings.asil_daily_budget_usd,
            fallback=fallback,
        )

    @property
    def active_profile_name(self) -> str:
        return self._active.name

    async def _check_budget(self) -> None:
        if self._budget_usd is None or self._fallback is None:
            return
        if self._active is self._fallback:
            return
        spent = await self._ledger.spend_today_usd()
        if spent >= self._budget_usd:
            log.warning(
                "budget_exceeded_downgrading",
                spent_usd=round(spent, 4),
                budget_usd=self._budget_usd,
                from_profile=self._active.name,
                to_profile=self._fallback.name,
            )
            self._active = self._fallback

    def _provider(self, tier: Tier) -> LLMProvider:
        return self._active.provider_for(tier)

    def _embedder(self) -> EmbeddingProvider:
        return self._active.embedding

    async def call(
        self,
        tier: Tier,
        messages: list[dict],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: list[dict] | None = None,
        stop: list[str] | None = None,
    ) -> CompletionResponse:
        if tier == "embed":
            raise ValueError("Use ModelRouter.embed() for the embed tier")

        await self._check_budget()
        provider = self._provider(tier)
        req = CompletionRequest(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            tools=tools,
            stop=stop,
        )

        log.debug(
            "llm_call_start",
            tier=tier,
            provider=provider.name,
            model=provider.model,
            profile=self._active.name,
        )
        resp = await provider.complete(req)

        await self._ledger.record(
            CostRecord(
                timestamp=datetime.now(UTC),
                provider=resp.provider,
                model=resp.model,
                tier=tier,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd=resp.cost_usd,
                profile=self._active.name,
            )
        )
        log.info(
            "llm_call_done",
            tier=tier,
            provider=resp.provider,
            model=resp.model,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=round(resp.cost_usd, 6),
            profile=self._active.name,
        )
        return resp

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await self._check_budget()
        embedder = self._embedder()
        vectors = await embedder.embed(texts)

        approx_tokens = sum(max(1, len(t) // 4) for t in texts)
        cost = approx_tokens * embedder.price_per_million / 1_000_000

        await self._ledger.record(
            CostRecord(
                timestamp=datetime.now(UTC),
                provider=embedder.name,
                model=embedder.model,
                tier="embed",
                input_tokens=approx_tokens,
                output_tokens=0,
                cost_usd=cost,
                profile=self._active.name,
            )
        )
        log.info(
            "embed_done",
            provider=embedder.name,
            model=embedder.model,
            count=len(texts),
            approx_tokens=approx_tokens,
            cost_usd=round(cost, 6),
            profile=self._active.name,
        )
        return vectors

    async def spend_today_usd(self) -> float:
        return await self._ledger.spend_today_usd()
