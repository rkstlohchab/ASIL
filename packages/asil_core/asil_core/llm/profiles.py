"""Profile definitions: tight / balanced / generous.

A Profile maps each Tier (a kind of LLM call) to a concrete LLMProvider, plus
an EmbeddingProvider. Switching profiles is a config change — no code changes.

Tiers are intentionally coarse. Add a new tier only when an existing one
genuinely can't represent the call's cost / quality tradeoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from asil_core.config import LLMProfileName, Settings, get_settings
from asil_core.llm.providers import (
    AnthropicProvider,
    DeepSeekProvider,
    EmbeddingProvider,
    LLMProvider,
    LocalEmbeddingProvider,
    MockEmbeddingProvider,
    MockLLMProvider,
    OpenAIProvider,
    VoyageEmbeddingProvider,
)

Tier = Literal["reasoning", "classify", "summarize", "verify", "embed"]
ALL_TIERS: tuple[Tier, ...] = ("reasoning", "classify", "summarize", "verify", "embed")
CHAT_TIERS: tuple[Tier, ...] = ("reasoning", "classify", "summarize", "verify")


@dataclass
class Profile:
    name: str
    chat: dict[Tier, LLMProvider]
    embedding: EmbeddingProvider

    def provider_for(self, tier: Tier) -> LLMProvider:
        try:
            return self.chat[tier]
        except KeyError as e:
            raise ValueError(f"Profile {self.name!r} has no provider for tier {tier!r}") from e


def profile_names() -> list[str]:
    return ["tight", "balanced", "generous"]


def _require(value: str | None, name: str, profile: str) -> str:
    if not value:
        raise RuntimeError(
            f"Profile {profile!r} requires {name} but it is not set in the environment. "
            f"Either set the env var or switch ASIL_LLM_PROFILE."
        )
    return value


def _load_tight(s: Settings) -> Profile:
    # Tight profile: open-weight DeepSeek for reasoning + cheap Qwen-style fast tier.
    # If DeepSeek key isn't set, fall through to mocks so dev still works offline.
    if s.deepseek_api_key:
        reasoning: LLMProvider = DeepSeekProvider(
            api_key=s.deepseek_api_key,
            model="deepseek-chat",
            input_price_per_million=0.27,
            output_price_per_million=1.10,
        )
        fast: LLMProvider = DeepSeekProvider(
            api_key=s.deepseek_api_key,
            model="deepseek-chat",
            input_price_per_million=0.27,
            output_price_per_million=1.10,
        )
    else:
        reasoning = MockLLMProvider(model="deepseek-chat (mocked)")
        fast = MockLLMProvider(model="deepseek-chat (mocked)")

    embedding: EmbeddingProvider = LocalEmbeddingProvider(endpoint=s.asil_embed_endpoint)

    return Profile(
        name="tight",
        chat={
            "reasoning": reasoning,
            "classify": fast,
            "summarize": fast,
            "verify": reasoning,
        },
        embedding=embedding,
    )


def _load_balanced(s: Settings) -> Profile:
    sonnet = AnthropicProvider(
        api_key=_require(s.anthropic_api_key, "ANTHROPIC_API_KEY", "balanced"),
        model="claude-sonnet-4-6",
        input_price_per_million=3.0,
        output_price_per_million=15.0,
    )
    deepseek = DeepSeekProvider(
        api_key=_require(s.deepseek_api_key, "DEEPSEEK_API_KEY", "balanced"),
        model="deepseek-chat",
        input_price_per_million=0.27,
        output_price_per_million=1.10,
    )
    embedding: EmbeddingProvider = VoyageEmbeddingProvider(
        api_key=_require(s.voyage_api_key, "VOYAGE_API_KEY", "balanced"),
    )
    return Profile(
        name="balanced",
        chat={
            "reasoning": sonnet,
            "classify": deepseek,
            "summarize": deepseek,
            "verify": sonnet,
        },
        embedding=embedding,
    )


def _load_generous(s: Settings) -> Profile:
    opus = AnthropicProvider(
        api_key=_require(s.anthropic_api_key, "ANTHROPIC_API_KEY", "generous"),
        model="claude-opus-4-7",
        input_price_per_million=15.0,
        output_price_per_million=75.0,
    )
    sonnet = AnthropicProvider(
        api_key=_require(s.anthropic_api_key, "ANTHROPIC_API_KEY", "generous"),
        model="claude-sonnet-4-6",
        input_price_per_million=3.0,
        output_price_per_million=15.0,
    )
    embedding: EmbeddingProvider = VoyageEmbeddingProvider(
        api_key=_require(s.voyage_api_key, "VOYAGE_API_KEY", "generous"),
    )
    return Profile(
        name="generous",
        chat={
            "reasoning": opus,
            "classify": sonnet,
            "summarize": sonnet,
            "verify": opus,
        },
        embedding=embedding,
    )


def load_profile(name: LLMProfileName | None = None, settings: Settings | None = None) -> Profile:
    s = settings or get_settings()
    target = name or s.asil_llm_profile
    if target == "tight":
        return _load_tight(s)
    if target == "balanced":
        return _load_balanced(s)
    if target == "generous":
        return _load_generous(s)
    raise ValueError(f"Unknown profile: {target!r}. Choose from {profile_names()}.")


def mock_profile() -> Profile:
    """Used in unit tests — no env vars required."""
    mock = MockLLMProvider()
    return Profile(
        name="mock",
        chat={
            "reasoning": mock,
            "classify": mock,
            "summarize": mock,
            "verify": mock,
        },
        embedding=MockEmbeddingProvider(),
    )
