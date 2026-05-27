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
        # Phase 6 (architecture drift)
        "asil.drift_check",
        # Phase 8 (constrained fix proposer)
        "asil.propose_fix",
        # Phase 9.1 (cache parity + provenance preamble)
        "asil.full_research",
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


# ---------------------------------------------------------------------------
# Phase 9.1 — MCP cache short-circuit + provenance preamble
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Minimum surface ModelRouter needs for `ask` short-circuit path."""

    active_profile_name = "tight"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]


def _fake_memory(answer: str = "cached answer", question: str = "how does X work?"):
    """A `Memory` populated with identity fields so we can assert the
    provenance preamble renders all the right pieces."""
    from datetime import UTC, datetime

    from asil_core import Confidence
    from asil_memory import Memory

    return Memory(
        id="mem-1",
        repo_key="local:/repo",
        question=question,
        answer=answer,
        confidence=Confidence(
            score=0.9, evidence_count=3, retrieval_strength=0.8, causal_confidence=0.0
        ),
        citations=[{"qualified_name": "x.y", "file_path": "a.py", "start_line": 1, "kind": "fn"}],
        verifier_unsupported=0,
        model="gpt-4o-mini",
        provider="openai",
        cost_usd=0.001,
        profile="tight",
        created_at=datetime(2026, 4, 12, 14, 23, tzinfo=UTC),
        user_id="alice@startup.dev",
        machine_id="workstation-7",
        origin_agent="claude-code",
        origin_session_id="sess-42",
    )


class _FakeEpisodicCacheHit:
    """EpisodicStore mock that always returns one high-similarity hit."""

    def __init__(self, similarity: float = 0.97) -> None:
        from asil_memory import MemoryHit

        self._hit = MemoryHit(memory=_fake_memory(), similarity=similarity)
        self.bumped: list[str] = []
        self.schema_applied = False
        self.remembered: list[dict] = []

    def apply_schema(self) -> None:
        self.schema_applied = True

    def recall_similar(self, **_kw):
        return [self._hit]

    def bump_recall_hit(self, memory_id: str) -> int:
        self.bumped.append(memory_id)
        return 1

    def remember(self, **kw):
        self.remembered.append(kw)
        return _fake_memory()


@pytest.mark.asyncio
async def test_mcp_ask_cache_hit_returns_provenance_preamble() -> None:
    """High-similarity hit → cached answer + provenance.is_cached=True."""
    from asil_api.mcp_tools import ask

    estore = _FakeEpisodicCacheHit(similarity=0.97)
    out = await ask(
        {"question": "how does X work?", "repo_key": "local:/repo", "client_id": "cursor"},
        graph_store=FakeGraphStore([]),
        vector_store=object(),  # not used on cache hit
        router=_FakeRouter(),  # type: ignore[arg-type]
        episodic_store=estore,  # type: ignore[arg-type]
    )

    prov = out["provenance"]
    assert prov["is_cached"] is True
    assert prov["originated_by_user"] == "alice@startup.dev"
    assert prov["originated_via_agent"] == "claude-code"
    assert prov["originated_on_machine"] == "workstation-7"
    assert prov["similarity"] == 0.97
    assert "Recalled from ASIL" in prov["preamble"]
    assert "alice@startup.dev" in prov["preamble"]
    assert "claude-code" in prov["preamble"]
    # The expensive LLM call is skipped — cost is zero on a cache hit.
    assert out["cost_usd"] == 0.0
    # Verifier wasn't run.
    assert out["verifier"] is None
    # bump_recall_hit was called for the matched memory.
    assert estore.bumped == ["mem-1"]


@pytest.mark.asyncio
async def test_mcp_ask_below_threshold_does_not_short_circuit() -> None:
    """If the closest hit is below `cache_threshold`, we don't fire the
    short-circuit. With no graph candidates the handler falls through to
    'no indexed code matched' rather than serving the cached answer."""
    from asil_api.mcp_tools import ask

    # Below threshold (0.92 default).
    estore = _FakeEpisodicCacheHit(similarity=0.5)

    # FakeGraphStore returns no rows → HybridRetriever returns no candidates →
    # `ask` short-circuits to the "no indexed code" return without an LLM call.
    out = await ask(
        {"question": "how does X work?", "repo_key": "local:/repo"},
        graph_store=FakeGraphStore([]),
        vector_store=_FakeVectorStore(),  # type: ignore[arg-type]
        router=_FakeRouter(),  # type: ignore[arg-type]
        episodic_store=estore,  # type: ignore[arg-type]
    )

    # We did NOT short-circuit (since similarity < threshold). The handler
    # went down the retrieve path; we don't care about the eventual answer
    # text here, only that the provenance reflects a non-cached response.
    assert out["provenance"]["is_cached"] is False
    assert estore.bumped == []  # no recall_hits bump


class _FakeVectorStore:
    """Stand-in for the real Qdrant client — returns zero vector hits so
    HybridRetriever produces no candidates and `ask` short-circuits to the
    'no indexed code matched' branch without an LLM call."""

    def search(self, *_a, **_k):
        return []

    def search_by_vector(self, *_a, **_k):
        return []


@pytest.mark.asyncio
async def test_full_research_forces_no_cache() -> None:
    """`asil.full_research` ignores the cache no matter how high the
    similarity. Hits 0.99? Still bypasses, runs the full pipeline."""
    from asil_api.mcp_tools import call_tool

    estore = _FakeEpisodicCacheHit(similarity=0.99)
    out = await call_tool(
        "asil.full_research",
        {"question": "how does X work?", "repo_key": "local:/repo"},
        graph_store=FakeGraphStore([]),
        vector_store=_FakeVectorStore(),  # type: ignore[arg-type]
        router=_FakeRouter(),  # type: ignore[arg-type]
        episodic_store=estore,  # type: ignore[arg-type]
    )

    # Even though there's a 0.99-similarity hit, the dispatcher forced
    # cache_threshold=1.01 → no short-circuit → no recall_hits bump.
    assert out["provenance"]["is_cached"] is False
    assert estore.bumped == []


def test_ask_schema_includes_phase_9_1_fields() -> None:
    """The `asil.ask` tool schema must advertise the new arguments callers
    rely on to pass their identity through. Without this the catalog lies
    about what the tool accepts."""
    spec = next(t for t in TOOL_CATALOG if t.name == "asil.ask")
    props = spec.input_schema["properties"]
    assert "client_id" in props
    assert "session_id" in props
    assert "cache_threshold" in props
    assert props["cache_threshold"]["default"] == 0.92


def test_full_research_tool_advertised() -> None:
    spec = next(t for t in TOOL_CATALOG if t.name == "asil.full_research")
    # Same required args as `asil.ask`; no cache_threshold (forced internally).
    assert "cache_threshold" not in spec.input_schema["properties"]
    assert spec.input_schema["required"] == ["question"]
