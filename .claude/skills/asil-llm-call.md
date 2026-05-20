---
name: asil-llm-call
description: Use when adding, modifying, or reviewing any LLM-calling code. Enforces tier-routed model calls via asil_core.llm.ModelRouter and forbids hardcoded model names or direct provider SDK usage.
---

# asil-llm-call

**Every LLM call in ASIL goes through `ModelRouter.call(tier=...)`. Hardcoding a model name is a layering violation and must be rejected in review.**

## The pattern

```python
from asil_core.llm import ModelRouter

router = ModelRouter.from_env()                       # reads ASIL_LLM_PROFILE
resp = await router.call(
    tier="reasoning",                                 # pick the right tier ↓
    messages=[{"role": "user", "content": prompt}],
    max_tokens=1024,
    temperature=0.0,
)
print(resp.text, resp.cost_usd, resp.provider, resp.model)
```

For embeddings:

```python
vectors = await router.embed(["chunk one", "chunk two"])
```

## Tier guide

| Tier | When to use | Active model on `tight` |
|---|---|---|
| `reasoning` | Deep analysis, root-cause synthesis, final answers | DeepSeek V4 |
| `classify` | Short structured outputs (labels, routing decisions) | DeepSeek V4 |
| `summarize` | Condensing long context for downstream steps | DeepSeek V4 |
| `verify` | Second-pass critique against evidence | DeepSeek V4 |
| `embed` | Vector embeddings | BGE-large (self-hosted) |

The same `tier` resolves to different providers on `balanced` / `generous` — that's the whole point. Profiles are documented in [packages/asil_core/asil_core/llm/profiles.py](../../packages/asil_core/asil_core/llm/profiles.py).

## Forbidden patterns

```python
# ❌ Direct provider SDK
import anthropic
client = anthropic.Anthropic()
client.messages.create(model="claude-opus-4-7", ...)

# ❌ OpenAI SDK
from openai import AsyncOpenAI
await AsyncOpenAI().chat.completions.create(model="gpt-5.5", ...)

# ❌ Hardcoded model string anywhere outside packages/asil_core/asil_core/llm/
model_name = "claude-sonnet-4-6"
```

These bypass the cost ledger, the budget guard, and the tier abstraction. If you find yourself writing this, stop and use `router.call(tier=...)`.

## Why this matters

The user is on a tight budget now but the architecture must scale to `generous` without a refactor. Tier-tagging makes the profile a one-line config flip. Hardcoding model names anywhere outside the provider modules breaks that contract.

## Reviewing existing code

Run `/check-tier` (project slash command) to scan for violations across the codebase.
