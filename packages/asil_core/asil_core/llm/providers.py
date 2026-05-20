"""Concrete LLM and embedding providers.

Each provider speaks plain HTTP — we deliberately avoid vendor SDKs in the
abstraction layer so swapping providers stays a config change. Pricing is
attached to the provider instance so the router can compute cost without
threading per-model tables.

Prices are USD per 1M tokens, approximate as of 2026-05; adjust as vendors move.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from asil_core.llm.types import CompletionRequest, CompletionResponse


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    input_price_per_million: float
    output_price_per_million: float

    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int
    price_per_million: float

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _compute_cost(
    input_tokens: int,
    output_tokens: int,
    input_price_per_million: float,
    output_price_per_million: float,
) -> float:
    return (
        input_tokens * input_price_per_million / 1_000_000
        + output_tokens * output_price_per_million / 1_000_000
    )


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """Anthropic Messages API. Used for the `generous` / `balanced` profiles."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        input_price_per_million: float,
        output_price_per_million: float,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.base_url = base_url
        self.timeout = timeout

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": req.messages,
        }
        if req.system:
            body["system"] = req.system
        if req.tools:
            body["tools"] = req.tools
        if req.stop:
            body["stop_sequences"] = req.stop

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            data = r.json()

        text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        tool_calls = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
        usage = data.get("usage", {})
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))

        return CompletionResponse(
            text="".join(text_parts),
            model=self.model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_compute_cost(
                input_tokens,
                output_tokens,
                self.input_price_per_million,
                self.output_price_per_million,
            ),
            tool_calls=tool_calls,
            raw=data,
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible (used for OpenAI and DeepSeek and Together/Fireworks-hosted
# open-weight models — they all speak the OpenAI Chat Completions schema).
# ---------------------------------------------------------------------------


class _OpenAICompatibleProvider:
    name: str = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        model: str,
        input_price_per_million: float,
        output_price_per_million: float,
        base_url: str,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.base_url = base_url
        self.timeout = timeout

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        messages = list(req.messages)
        if req.system:
            messages = [{"role": "system", "content": req.system}, *messages]

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        }
        if req.tools:
            body["tools"] = req.tools
        if req.stop:
            body["stop"] = req.stop

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json=body,
            )
            r.raise_for_status()
            data = r.json()

        choice = data["choices"][0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        usage = data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        return CompletionResponse(
            text=text,
            model=self.model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_compute_cost(
                input_tokens,
                output_tokens,
                self.input_price_per_million,
                self.output_price_per_million,
            ),
            tool_calls=tool_calls,
            raw=data,
        )


class OpenAIProvider(_OpenAICompatibleProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        input_price_per_million: float,
        output_price_per_million: float,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            base_url=base_url,
            timeout=timeout,
        )


class DeepSeekProvider(_OpenAICompatibleProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        input_price_per_million: float = 0.27,
        output_price_per_million: float = 1.10,
        base_url: str = "https://api.deepseek.com/v1",
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            base_url=base_url,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Mock (tests + offline dev)
# ---------------------------------------------------------------------------


class MockLLMProvider:
    name = "mock"

    def __init__(
        self,
        model: str = "mock-model",
        canned_response: str = "ok",
        input_price_per_million: float = 0.0,
        output_price_per_million: float = 0.0,
    ) -> None:
        self.model = model
        self.canned_response = canned_response
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        return CompletionResponse(
            text=self.canned_response,
            model=self.model,
            provider=self.name,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class OpenAIEmbeddingProvider:
    """OpenAI embeddings — fallback for the `tight` profile when only OPENAI_API_KEY is set."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dim: int = 1536,
        price_per_million: float = 0.02,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.price_per_million = price_per_million
        self.base_url = base_url
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json={"input": texts, "model": self.model},
            )
            r.raise_for_status()
            data = r.json()
        return [item["embedding"] for item in data["data"]]


class VoyageEmbeddingProvider:
    """Voyage AI embeddings — used in `balanced` / `generous` profiles."""

    name = "voyage"

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3-code",
        dim: int = 1024,
        price_per_million: float = 0.18,
        base_url: str = "https://api.voyageai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.price_per_million = price_per_million
        self.base_url = base_url
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json={"input": texts, "model": self.model},
            )
            r.raise_for_status()
            data = r.json()
        return [item["embedding"] for item in data["data"]]


class LocalEmbeddingProvider:
    """Self-hosted embedding endpoint (BGE-large via TEI / vLLM).

    Expected request: POST {endpoint} {"inputs": [...]} -> {"embeddings": [[...]]}.
    Used in the `tight` profile.
    """

    name = "local"

    def __init__(
        self,
        endpoint: str,
        model: str = "bge-large",
        dim: int = 1024,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.dim = dim
        self.price_per_million = 0.0
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(self.endpoint, json={"inputs": texts})
            r.raise_for_status()
            data = r.json()
        return data.get("embeddings") or data.get("data") or []


class MockEmbeddingProvider:
    name = "mock"

    def __init__(self, model: str = "mock-embed", dim: int = 8) -> None:
        self.model = model
        self.dim = dim
        self.price_per_million = 0.0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Deterministic non-zero vectors so retrievers using cosine don't divide by zero.
        return [[float((i + j + 1) % 7) / 7.0 for j in range(self.dim)] for i in range(len(texts))]
