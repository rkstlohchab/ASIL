"""Integration tests for the Neo4j graph builder.

These require a running Neo4j (skipped automatically otherwise — see conftest).
Each test uses a unique repo_key + cleans up after itself so they can run in
any order without interfering with each other.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from asil_ingest import (
    GraphBuilder,
    ResolvedRepo,
    SourceLanguage,
    parse_source,
    repo_key_for,
)
from asil_memory import GraphStore


@pytest.fixture
def repo(tmp_path: Path) -> ResolvedRepo:
    """A unique-named local repo so tests don't collide on repo_key."""
    return ResolvedRepo(
        spec=str(tmp_path),
        path=tmp_path,
        is_local=True,
        org=None,
        name=f"asil-test-{tmp_path.name}",
    )


@pytest.fixture
def cleaner(graph_store: GraphStore, repo: ResolvedRepo):
    key = repo_key_for(repo)
    yield key
    graph_store.clear_repo(key)


def test_apply_schema_idempotent(graph_store: GraphStore) -> None:
    graph_store.apply_schema()
    graph_store.apply_schema()  # no error on re-run


def test_writes_repo_file_function_class_symbol_nodes(
    graph_store: GraphStore, repo: ResolvedRepo, cleaner: str
) -> None:
    builder = GraphBuilder(graph_store)
    repo_key = builder.upsert_repo(repo)
    assert repo_key == cleaner

    src = dedent(
        """
        '''module-level stuff.'''

        CONFIG_TIMEOUT = 30

        def top_level(x: int) -> int:
            return x + 1

        class Service:
            '''demo class.'''

            def __init__(self) -> None:
                self.value = 0

            async def run(self) -> int:
                return self.value
        """
    ).lstrip("\n")

    parsed = parse_source(src, SourceLanguage.python, path="example.py", module_name="example")
    builder.write_file(repo_key, parsed)

    counts = graph_store.stats(repo_key=repo_key)
    assert counts["Repo"] == 1
    assert counts["File"] == 1
    assert counts["Function"] == 3  # top_level + __init__ + run
    assert counts["Class"] == 1
    assert counts["Symbol"] == 3  # CONFIG_TIMEOUT + top_level + Service

    # Verify a few key edges exist.
    rows = graph_store.query(
        """
        MATCH (r:Repo {key: $key})-[:CONTAINS]->(f:File)-[:CONTAINS]->(fn:Function)
        RETURN fn.qualified_name AS qn
        ORDER BY fn.qualified_name
        """,
        key=repo_key,
    )
    qnames = [r["qn"] for r in rows]
    assert "example.top_level" in qnames
    assert "example.Service.__init__" in qnames
    assert "example.Service.run" in qnames

    method_rows = graph_store.query(
        """
        MATCH (c:Class {repo_key: $key, qualified_name: 'example.Service'})-[:CONTAINS]->(m:Function)
        RETURN m.qualified_name AS qn ORDER BY m.qualified_name
        """,
        key=repo_key,
    )
    method_qnames = [r["qn"] for r in method_rows]
    assert method_qnames == ["example.Service.__init__", "example.Service.run"]


def test_call_data_persisted_as_json_property(
    graph_store: GraphStore, repo: ResolvedRepo, cleaner: str
) -> None:
    """SCIP step will promote these to real edges later — for now they're a JSON blob."""
    builder = GraphBuilder(graph_store)
    repo_key = builder.upsert_repo(repo)

    parsed = parse_source(
        "def outer():\n    inner()\n    other.method()\n",
        SourceLanguage.python,
        path="m.py",
        module_name="m",
    )
    builder.write_file(repo_key, parsed)

    rows = graph_store.query(
        """
        MATCH (fn:Function {repo_key: $key, qualified_name: 'm.outer'})
        RETURN fn.calls_json AS calls_json, fn.n_calls AS n_calls
        """,
        key=repo_key,
    )
    assert rows
    import json as _json

    payload = _json.loads(rows[0]["calls_json"])
    callees = [c["callee"] for c in payload]
    assert "inner" in callees
    assert "other.method" in callees
    assert rows[0]["n_calls"] == 2


def test_reingest_is_idempotent(graph_store: GraphStore, repo: ResolvedRepo, cleaner: str) -> None:
    builder = GraphBuilder(graph_store)
    repo_key = builder.upsert_repo(repo)
    parsed = parse_source("def f(): pass\n", SourceLanguage.python, path="x.py", module_name="x")
    builder.write_file(repo_key, parsed)
    builder.write_file(repo_key, parsed)
    counts = graph_store.stats(repo_key=repo_key)
    assert counts["Function"] == 1  # MERGE didn't duplicate
    assert counts["File"] == 1


def test_clear_repo_removes_everything(graph_store: GraphStore, repo: ResolvedRepo) -> None:
    builder = GraphBuilder(graph_store)
    repo_key = builder.upsert_repo(repo)
    parsed = parse_source(
        "class C:\n    def m(self): pass\n",
        SourceLanguage.python,
        path="c.py",
        module_name="c",
    )
    builder.write_file(repo_key, parsed)

    before = graph_store.stats(repo_key=repo_key)
    assert sum(before.values()) > 0

    removed = graph_store.clear_repo(repo_key)
    assert removed >= 3  # at minimum File + Function + Class

    after = graph_store.stats(repo_key=repo_key)
    assert sum(after.values()) == 0
