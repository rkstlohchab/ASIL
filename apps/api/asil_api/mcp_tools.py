"""ASIL's MCP tool surface.

This is the layer that makes ASIL useful *to other agents*. Each tool is a
pure async function: input dict → output dict. The same handlers are exposed
two ways:

  - over HTTP via `apps.api.main`: `GET /mcp/tools` lists the schemas,
    `POST /mcp/call/{tool}` invokes one. This is what the CLI's smoke tests
    and any HTTP-based client (curl, Postman, custom integrations) use.
  - over native MCP stdio: planned for Phase 7 polish, when we ship the
    `asil-mcp` console script so Claude Code / Cursor / OpenHands can wire
    ASIL in as a first-class MCP server. The tool definitions below are the
    contract; only the transport changes.

Design rules:
  - Every tool is async so it can call ModelRouter / GraphStore / VectorStore
    without ceremony when needed.
  - Every tool returns JSON-safe primitives. No Pydantic models, no dataclass
    instances — the wire shape is what users see, and what tests check.
  - Every tool's payload includes a `confidence` block when the result is the
    outcome of reasoning (e.g. `ask`). Hard rule per CLAUDE.md.
  - Tools are read-only by default. Mutations (re-ingest, clear) belong in
    the CLI; agents don't get them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from asil_core.llm import ModelRouter
from asil_memory import (
    GraphStore,
    HybridRetriever,
    VectorStore,
)

# ---------------------------------------------------------------------------
# Tool catalog — public-facing JSON Schemas, paired with handler refs.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


TOOL_CATALOG: list[ToolSpec] = [
    ToolSpec(
        name="asil.search_code",
        description=(
            "Hybrid semantic + graph search for code. Returns top-K candidate "
            "functions/classes with citations. Use this when you have a natural-"
            "language description of what you're looking for."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "repo_key": {
                    "type": ["string", "null"],
                    "description": "Optional repo scope (e.g. 'org/name'). Omit for cross-repo.",
                },
                "kind": {
                    "type": ["string", "null"],
                    "enum": ["function", "class", None],
                    "description": "Filter by node kind.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="asil.get_callers",
        description=(
            "List every Function that calls the given target (1-hop). Uses :CALLS "
            "edges resolved by the parser + import-aware heuristics. Returns "
            "[{qualified_name, file_path, line, derivation}]."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": "Fully-qualified function name, e.g. 'pkg.mod.Class.method'.",
                },
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="asil.get_dependencies",
        description=(
            "Inverse of get_callers — every Function that this one calls. "
            "Useful for tracing 'what does this function actually depend on?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {"type": "string"},
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="asil.who_owns",
        description=(
            "Phase 1 placeholder. Returns the containing file + (when available) "
            "the last commit author from git blame. Author resolution proper "
            "lands in Phase 2's commit-history ingestor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "repo_key": {"type": ["string", "null"]},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="asil.commit_history",
        description=(
            "Phase 1 placeholder. The Commit/Author nodes wire up in Phase 2; "
            "this tool currently returns an empty list and a `not_yet_implemented` "
            "flag so callers can detect the stub."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "since": {"type": ["string", "null"]},
                "repo_key": {"type": ["string", "null"]},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="asil.ask",
        description=(
            "Highest-level tool. Embeds the question, runs the hybrid retriever, "
            "passes the top candidates to the reasoning LLM with a strict cite-"
            "everything system prompt. Returns {answer, confidence, citations}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["question"],
        },
    ),
]


def tool_catalog() -> list[dict[str, Any]]:
    """JSON-safe view of the catalog for `GET /mcp/tools`."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        for t in TOOL_CATALOG
    ]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def search_code(
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore,
    router: ModelRouter,
) -> dict[str, Any]:
    query = _required_str(payload, "query")
    repo_key = payload.get("repo_key")
    kind = payload.get("kind")
    limit = int(payload.get("limit", 10))

    retriever = HybridRetriever(
        graph_store=graph_store,
        vector_store=vector_store,
        embedder=router,
        final_limit=limit,
    )
    result = await retriever.retrieve(query, repo_key=repo_key, kind=kind)
    return {
        "query": query,
        "candidates": [_candidate_dict(c) for c in result.candidates],
        "confidence": _confidence_dict(result.confidence),
    }


async def get_callers(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    qname = _required_str(payload, "qualified_name")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 50))
    rows = graph_store.query(
        """
        MATCH (caller:Function)-[r:CALLS]->(callee:Function {qualified_name: $qname})
        WHERE ($repo IS NULL OR caller.repo_key = $repo)
        RETURN caller.qualified_name AS qualified_name,
               caller.file_path      AS file_path,
               caller.signature      AS signature,
               r.line                AS line,
               r.derivation          AS derivation
        ORDER BY caller.qualified_name
        LIMIT $limit
        """,
        qname=qname,
        repo=repo_key,
        limit=limit,
    )
    return {"target": qname, "callers": rows, "count": len(rows)}


async def get_dependencies(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    qname = _required_str(payload, "qualified_name")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 50))
    rows = graph_store.query(
        """
        MATCH (caller:Function {qualified_name: $qname})-[r:CALLS]->(callee:Function)
        WHERE ($repo IS NULL OR caller.repo_key = $repo)
        RETURN callee.qualified_name AS qualified_name,
               callee.file_path      AS file_path,
               callee.signature      AS signature,
               r.line                AS line,
               r.derivation          AS derivation
        ORDER BY callee.qualified_name
        LIMIT $limit
        """,
        qname=qname,
        repo=repo_key,
        limit=limit,
    )
    return {"caller": qname, "dependencies": rows, "count": len(rows)}


async def who_owns(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    path = _required_str(payload, "path")
    repo_key = payload.get("repo_key")
    rows = graph_store.query(
        """
        MATCH (r:Repo)-[:CONTAINS]->(f:File {path: $path})
        WHERE ($repo IS NULL OR f.repo_key = $repo)
        RETURN r.key AS repo_key, r.spec AS spec, f.path AS path,
               f.language AS language, f.loc AS loc,
               f.module_name AS module_name
        LIMIT 1
        """,
        path=path,
        repo=repo_key,
    )
    if not rows:
        return {"path": path, "found": False, "note": "no File node with that path"}
    out = dict(rows[0])
    out["found"] = True
    # Phase 1 placeholder — Author resolution lands in Phase 2 alongside Commit nodes.
    out["author"] = None
    out["last_commit"] = None
    out["note"] = "author/commit resolution is Phase 2 work; returning file metadata only"
    return out


async def commit_history(payload: dict[str, Any]) -> dict[str, Any]:
    _required_str(payload, "path")
    return {
        "commits": [],
        "not_yet_implemented": True,
        "note": "Commit/Author nodes ship in Phase 2 (memory + commit ingestor).",
    }


async def ask(
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore,
    router: ModelRouter,
) -> dict[str, Any]:
    question = _required_str(payload, "question")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 8))

    retriever = HybridRetriever(
        graph_store=graph_store,
        vector_store=vector_store,
        embedder=router,
        final_limit=limit,
    )
    result = await retriever.retrieve(question, repo_key=repo_key)
    if not result.candidates:
        return {
            "question": question,
            "answer": "No indexed code matched this question. Try a different phrasing or ingest the relevant repo.",
            "citations": [],
            "confidence": _confidence_dict(result.confidence),
            "cost_usd": 0.0,
        }

    prompt = _build_ask_prompt(question, result)
    resp = await router.call(
        tier="reasoning",
        messages=[{"role": "user", "content": prompt}],
        system=_ASK_SYSTEM_PROMPT,
        max_tokens=900,
        temperature=0.1,
    )
    citations = [
        {
            "qualified_name": c.qualified_name,
            "file_path": c.file_path,
            "start_line": c.start_line,
            "kind": c.kind,
            "score": round(c.score, 4),
        }
        for c in result.candidates
    ]
    return {
        "question": question,
        "answer": resp.text,
        "citations": citations,
        "confidence": _confidence_dict(result.confidence),
        "cost_usd": resp.cost_usd,
        "model": resp.model,
        "provider": resp.provider,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_TOOL_HANDLERS: dict[str, str] = {t.name: t.name for t in TOOL_CATALOG}


async def call_tool(
    name: str,
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore | None,
    router: ModelRouter | None,
) -> dict[str, Any]:
    """Route a tool call by name. Validates name + required deps for that tool."""
    if name not in _TOOL_HANDLERS:
        return {"error": f"unknown tool: {name!r}", "available": list(_TOOL_HANDLERS)}

    if name == "asil.search_code":
        _need(vector_store, router, name=name)
        return await search_code(
            payload,
            graph_store=graph_store,
            vector_store=vector_store,
            router=router,  # type: ignore[arg-type]
        )
    if name == "asil.get_callers":
        return await get_callers(payload, graph_store=graph_store)
    if name == "asil.get_dependencies":
        return await get_dependencies(payload, graph_store=graph_store)
    if name == "asil.who_owns":
        return await who_owns(payload, graph_store=graph_store)
    if name == "asil.commit_history":
        return await commit_history(payload)
    if name == "asil.ask":
        _need(vector_store, router, name=name)
        return await ask(
            payload,
            graph_store=graph_store,
            vector_store=vector_store,
            router=router,  # type: ignore[arg-type]
        )
    return {"error": f"handler missing for {name!r}"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _required_str(payload: dict[str, Any], key: str) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key!r} is required and must be a non-empty string")
    return v


def _need(*deps: Any, name: str) -> None:
    if any(d is None for d in deps):
        raise RuntimeError(
            f"tool {name!r} requires the vector store and LLM router; "
            "they were not available in this server context."
        )


def _candidate_dict(c: Any) -> dict[str, Any]:
    return {
        "qualified_name": c.qualified_name,
        "name": c.name,
        "kind": c.kind,
        "file_path": c.file_path,
        "start_line": c.start_line,
        "end_line": c.end_line,
        "score": round(c.score, 4),
        "source": c.source,
        "signature": c.signature,
        "docstring": c.docstring,
        "parent_class": c.parent_class,
    }


def _confidence_dict(conf: Any) -> dict[str, Any]:
    return {
        "score": round(conf.score, 4),
        "evidence_count": conf.evidence_count,
        "retrieval_strength": round(conf.retrieval_strength, 4),
        "causal_confidence": round(conf.causal_confidence, 4),
        "derivation": list(conf.derivation),
    }


_ASK_SYSTEM_PROMPT = (
    "You are ASIL, the engineering intelligence layer for this codebase. "
    "Answer the user's question using ONLY the code snippets provided. "
    "Rules:\n"
    "  1. Cite every concrete claim with the file:line of the supporting snippet, like (graph_store.py:116).\n"
    "  2. If the snippets don't actually answer the question, say so plainly — do not invent.\n"
    "  3. Prefer short, direct prose. Use a fenced ```py``` block only when quoting code is the clearest answer.\n"
    "  4. Never reference 'the snippets' or 'the context' in your response — speak as if you simply know the code.\n"
    "  5. If the answer requires details not present, end with one sentence on what additional evidence would resolve it."
)


def _build_ask_prompt(question: str, result: Any) -> str:
    """Same shape as the CLI's ask prompt — kept here so the MCP path and the
    CLI path stay consistent. If we change one, update the other."""
    lines = [f"Question: {question}", "", "Code snippets retrieved (most relevant first):"]
    for i, c in enumerate(result.candidates, 1):
        header = f"[{i}] {c.qualified_name}  —  {c.file_path}:{c.start_line}"
        if c.signature:
            header += f"  signature: {c.signature}"
        lines.append("")
        lines.append(header)
        if c.docstring:
            lines.append(f"  doc: {c.docstring.strip()[:300]}")
        if c.text:
            snippet = c.text if len(c.text) <= 1200 else c.text[:1200] + "\n  …"
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    lines.extend(["", "Answer the question now. Cite with file:line as specified."])
    return "\n".join(lines)


__all__ = [
    "TOOL_CATALOG",
    "ToolSpec",
    "ask",
    "call_tool",
    "commit_history",
    "get_callers",
    "get_dependencies",
    "search_code",
    "tool_catalog",
    "who_owns",
]


# Acknowledge json import for serialization callers that build payloads;
# kept eager so MyPy/test discovery don't lose the symbol on dead-code passes.
_ = json
