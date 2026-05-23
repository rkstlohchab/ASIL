---
name: asil-mcp-tool
description: Use when adding, modifying, or reviewing an MCP tool exposed to external agents (Cursor / Claude Code / OpenHands / curl). Enforces the catalog shape, JSON-Schema input, JSON-safe output, async pure-function handler, and the Confidence-on-every-reasoning-output rule.
---

# asil-mcp-tool

ASIL's value to other agents flows through MCP tools. The contract you ship
here gets baked into every agent's tool catalog — changing it later is a
migration. Get it right the first time.

## Where tools live

- **Catalog + handlers:** `apps/api/asil_api/mcp_tools.py`
- **HTTP wiring:** `apps/api/asil_api/main.py` — `GET /mcp/tools`, `POST /mcp/call/{tool_name}`
- **Tests:** `tests/unit/test_mcp_tools.py` (catalog shape + per-handler logic with fake stores)

## Tool contract — non-negotiable

1. **Every tool is async.** Even if the current handler is synchronous, declare it `async def` so future versions can `await` without breaking the interface.

2. **Input is one `dict[str, Any]` `payload`** validated against the tool's `input_schema` (JSON Schema draft 2020-12). Don't take positional arguments; the wire shape is a single JSON object.

3. **Output is a JSON-safe `dict`.** No Pydantic models, no dataclass instances, no `datetime` (use ISO strings). What you return is what the agent sees. Run `json.dumps(result)` mentally before committing.

4. **Reasoning outputs ship a Confidence block.** If the tool returns the result of LLM synthesis (like `asil.ask`), it MUST include:
   ```python
   "confidence": {
       "score": 0.0..1.0,
       "evidence_count": int,
       "retrieval_strength": 0.0..1.0,
       "causal_confidence": 0.0..1.0,
       "derivation": list[str],
   }
   ```
   Pure data tools (`get_callers`, `who_owns`) don't need confidence — they're not reasoning, they're lookup.

5. **Tools are READ-ONLY by default.** Mutations (re-ingest, clear) belong in the CLI. Agents get information, not mutation rights.

6. **Required deps go through the dispatcher.** Some tools need just the graph store; others need the vector store and the LLM router. Declare your needs in `call_tool()` via `_need(...)` and let it fail loudly when a tool's deps aren't available.

## Adding a new tool — checklist

- [ ] Add a `ToolSpec` entry to `TOOL_CATALOG` (name with `asil.` prefix, description in plain English, JSON-Schema input).
- [ ] Write the handler as `async def my_tool(payload, *, graph_store, ...) -> dict`.
- [ ] Add the handler dispatch branch in `call_tool()`.
- [ ] Add a unit test in `tests/unit/test_mcp_tools.py` covering: catalog presence, required-arg validation, happy-path return shape.
- [ ] If the tool reasons (LLM call inside), the system prompt MUST enforce "cite every claim with file:line; refuse if not supported" — copy from `_ASK_SYSTEM_PROMPT`.

## Forbidden patterns

```python
# ❌ Returning a Pydantic model directly
return MyOutput(answer=..., confidence=...)

# ❌ Taking explicit positional args
async def tool(question: str, repo: str | None = None) -> ...

# ❌ Reasoning output without confidence
return {"answer": resp.text, "model": resp.model}

# ❌ Calling an LLM provider SDK directly inside a tool
client = anthropic.Anthropic(); client.messages.create(...)
# (use the injected `router: ModelRouter` — see asil-llm-call skill)
```

## Naming

- Tool names use the `asil.` prefix and snake_case: `asil.get_callers`, `asil.search_code`.
- Don't pluralize (`asil.get_caller` not `asil.get_callers_list`).
- Avoid verbs that imply mutation (`asil.update_*`, `asil.delete_*`) — those land in the CLI, not the tool surface.

## Native MCP transport (deferred to Phase 7)

Currently we expose tools over HTTP only. Native stdio MCP (via the `mcp` Python SDK) ships in Phase 7. The tool definitions in `TOOL_CATALOG` are written once and reused — the only thing changing is the transport. Keep your handlers pure and the migration is mechanical.

Phase 7 note: when you add `apps/api/asil_api/mcp_stdio.py`, copy the catalog + dispatcher; don't re-implement them.
