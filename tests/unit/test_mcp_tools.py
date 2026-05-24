"""Unit tests for MCP tool handlers.

The tool catalog is the public surface ASIL exposes to other agents. These
tests pin the wire shape: tool names, required arguments, JSON-safe return
types. The handlers themselves are tested against fake stores so we can
exercise edge cases (empty results, missing dependencies) without docker.
"""

from __future__ import annotations

from typing import Any

import pytest
from asil_api.mcp_tools import (
    TOOL_CATALOG,
    call_tool,
    commit_history,
    get_callers,
    get_dependencies,
    tool_catalog,
    who_owns,
)

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeGraphStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.calls: list[dict[str, Any]] = []

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append({"cypher": cypher, "params": params})
        return self._rows


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_tool_catalog_contains_all_shipped_tools() -> None:
    names = {t.name for t in TOOL_CATALOG}
    assert names == {
        # Phase 1
        "asil.search_code",
        "asil.get_callers",
        "asil.get_dependencies",
        "asil.who_owns",
        "asil.commit_history",
        "asil.ask",
        # Phase 2 (episodic memory)
        "asil.remember",
        "asil.recall",
        "asil.forget",
        # Phase 4 (temporal causality)
        "asil.find_causes",
        # Phase 5 (execution replay)
        "asil.replay_incident",
    }


def test_tool_catalog_json_shape_is_stable() -> None:
    """Lock the wire shape so we don't drift the contract by accident."""
    catalog = tool_catalog()
    for entry in catalog:
        assert set(entry.keys()) == {"name", "description", "inputSchema"}
        schema = entry["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


# ---------------------------------------------------------------------------
# get_callers / get_dependencies — exercise the Cypher passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_callers_returns_rows_and_query_params() -> None:
    gs = FakeGraphStore(
        [
            {
                "qualified_name": "x.caller1",
                "file_path": "a.py",
                "signature": "()",
                "line": 5,
                "derivation": "same_module",
            }
        ]
    )
    result = await get_callers(
        {"qualified_name": "pkg.target", "repo_key": "test/repo", "limit": 25},
        graph_store=gs,
    )
    assert result["target"] == "pkg.target"
    assert result["count"] == 1
    assert result["callers"][0]["qualified_name"] == "x.caller1"
    # Confirm we forwarded the limit + repo into the Cypher params.
    call = gs.calls[0]["params"]
    assert call["qname"] == "pkg.target"
    assert call["repo"] == "test/repo"
    assert call["limit"] == 25


@pytest.mark.asyncio
async def test_get_callers_requires_qualified_name() -> None:
    gs = FakeGraphStore([])
    with pytest.raises(ValueError, match="qualified_name"):
        await get_callers({}, graph_store=gs)


@pytest.mark.asyncio
async def test_get_dependencies_returns_caller_field() -> None:
    gs = FakeGraphStore([])
    result = await get_dependencies({"qualified_name": "pkg.foo"}, graph_store=gs)
    assert result["caller"] == "pkg.foo"
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# who_owns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_who_owns_returns_file_metadata_when_found() -> None:
    gs = FakeGraphStore(
        [
            {
                "repo_key": "test/repo",
                "spec": ".",
                "path": "pkg/mod.py",
                "language": "python",
                "loc": 120,
                "module_name": "pkg.mod",
            }
        ]
    )
    result = await who_owns({"path": "pkg/mod.py"}, graph_store=gs)
    assert result["found"] is True
    assert result["language"] == "python"
    # Phase 1 placeholder fields explicitly marked.
    assert result["author"] is None
    assert result["last_commit"] is None
    assert "Phase 2" in result["note"]


@pytest.mark.asyncio
async def test_who_owns_returns_found_false_when_path_missing() -> None:
    gs = FakeGraphStore([])
    result = await who_owns({"path": "not/in/index.py"}, graph_store=gs)
    assert result["found"] is False
    assert "no File node" in result["note"]


# ---------------------------------------------------------------------------
# commit_history (Phase 1 stub)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_history_returns_stub_with_not_yet_implemented_flag() -> None:
    result = await commit_history({"path": "anything.py"})
    assert result["commits"] == []
    assert result["not_yet_implemented"] is True


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_rejects_unknown_names() -> None:
    gs = FakeGraphStore()
    result = await call_tool(
        "asil.does_not_exist",
        {},
        graph_store=gs,
        vector_store=None,
        router=None,
    )
    assert "unknown tool" in result["error"]
    assert "asil.get_callers" in result["available"]


@pytest.mark.asyncio
async def test_call_tool_routes_to_get_callers() -> None:
    gs = FakeGraphStore([])
    result = await call_tool(
        "asil.get_callers",
        {"qualified_name": "x.y"},
        graph_store=gs,
        vector_store=None,
        router=None,
    )
    assert result["target"] == "x.y"


@pytest.mark.asyncio
async def test_call_tool_search_code_needs_vector_store_and_router() -> None:
    gs = FakeGraphStore([])
    with pytest.raises(RuntimeError, match="requires the vector store"):
        await call_tool(
            "asil.search_code",
            {"query": "test"},
            graph_store=gs,
            vector_store=None,
            router=None,
        )


# ---------------------------------------------------------------------------
# memory tools (Phase 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_remember_requires_episodic_store_and_router() -> None:
    gs = FakeGraphStore([])
    with pytest.raises(RuntimeError, match=r"vector store and LLM router|episodic"):
        await call_tool(
            "asil.remember",
            {"repo_key": "r", "question": "q", "answer": "a"},
            graph_store=gs,
            vector_store=None,
            router=None,
            episodic_store=None,
        )


@pytest.mark.asyncio
async def test_call_tool_forget_routes_with_only_episodic_store() -> None:
    """`asil.forget` doesn't need the LLM router — just the episodic store."""

    class FakeEpisodic:
        def __init__(self) -> None:
            self.forgotten: list[str] = []

        def forget(self, memory_id: str) -> bool:
            self.forgotten.append(memory_id)
            return True

    estore = FakeEpisodic()
    gs = FakeGraphStore([])
    result = await call_tool(
        "asil.forget",
        {"memory_id": "deadbeef-cafe-1234-5678-abcdef012345"},
        graph_store=gs,
        vector_store=None,
        router=None,
        episodic_store=estore,  # type: ignore[arg-type]
    )
    assert result["removed"] is True
    assert estore.forgotten == ["deadbeef-cafe-1234-5678-abcdef012345"]
