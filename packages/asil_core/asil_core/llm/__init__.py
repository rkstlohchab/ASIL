"""ASIL LLM abstraction — tier-routed, cost-tracked, budget-guarded.

Every prompt site in ASIL goes through `ModelRouter.call(tier=..., messages=...)`.
Never call a provider SDK directly — that breaks the tier-routing contract and
makes the active profile a refactor instead of a config flip.
"""

from asil_core.llm.ledger import CostLedger, CostRecord, InMemoryCostLedger
from asil_core.llm.profiles import (
    Profile,
    Tier,
    load_profile,
    profile_names,
)
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
from asil_core.llm.router import ModelRouter
from asil_core.llm.types import CompletionRequest, CompletionResponse

__all__ = [
    "AnthropicProvider",
    "CompletionRequest",
    "CompletionResponse",
    "CostLedger",
    "CostRecord",
    "DeepSeekProvider",
    "EmbeddingProvider",
    "InMemoryCostLedger",
    "LLMProvider",
    "LocalEmbeddingProvider",
    "MockEmbeddingProvider",
    "MockLLMProvider",
    "ModelRouter",
    "OpenAIProvider",
    "Profile",
    "Tier",
    "VoyageEmbeddingProvider",
    "load_profile",
    "profile_names",
]
