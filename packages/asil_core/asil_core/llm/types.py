"""Shared request/response types for the LLM abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CompletionRequest:
    messages: list[dict[str, Any]]
    max_tokens: int = 1024
    temperature: float = 0.0
    system: str | None = None
    tools: list[dict[str, Any]] | None = None
    stop: list[str] | None = None


@dataclass(slots=True)
class CompletionResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
