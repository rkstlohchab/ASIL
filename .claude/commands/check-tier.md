---
description: Scan the codebase for hardcoded LLM model names or direct provider SDK usage that bypasses ModelRouter
---

Scan the codebase for violations of the tier-routed LLM contract (see [asil-llm-call skill](../skills/asil-llm-call.md)).

**Search these patterns** (use `rg` / Grep, exclude `packages/asil_core/asil_core/llm/`, `tests/`, `.claude/`, `docs/`, `PLAN.md`):

1. Direct provider SDK imports:
   - `from anthropic`, `import anthropic`
   - `from openai`, `import openai`
   - `from voyageai`, `from cohere`, `from google.generativeai`

2. Hardcoded model strings:
   - `claude-opus`, `claude-sonnet`, `claude-haiku`, `claude-3`
   - `gpt-4`, `gpt-5`, `gpt-3.5`
   - `deepseek-`, `qwen-`, `llama-`, `gemini-`

3. Direct SDK call patterns:
   - `\.messages\.create\(`
   - `\.chat\.completions\.create\(`
   - `\.embeddings\.create\(`

**For each finding, report:**

- `path:line` with the offending snippet (one line).
- Which tier the call would belong to (`reasoning` / `classify` / `summarize` / `verify` / `embed`).
- The exact replacement (`router.call(tier="...", messages=[...])` or `router.embed([...])`).

**If nothing is found:**

Report:
> ✅ tier-routing intact — no direct provider SDK usage or hardcoded model names outside `packages/asil_core/asil_core/llm/`.

**Important:** do not auto-fix violations. List them and let the user decide which to migrate first. Some test fixtures may legitimately use mock providers directly; flag those for human review rather than auto-rewriting.
