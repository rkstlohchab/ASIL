"""Write ParsedFile records into the Neo4j knowledge graph.

Phase 1 step 3 — structural skeleton only:
  - Repo / File / Function / Class / Symbol nodes
  - CONTAINS edges (Repo→File, File→Function/Class/Symbol, Class→Method)

Unresolved info (call sites, imports, inheritance targets) is kept as JSON-string
properties on the source nodes so the SCIP step (Phase 1.6) can promote them
into real edges without re-parsing. Neo4j properties can't hold nested objects;
JSON keeps the data dense without forcing a parallel-array schema we'd regret.

The builder is NOT incremental yet — re-ingesting a repo over an old graph
upserts everything but doesn't delete files that have since been removed.
Phase 1.8 (repo cloner incremental fetch) will add diff-aware cleanup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from asil_core.logging import get_logger
from asil_memory import GraphStore

from asil_ingest.models import (
    ParsedClass,
    ParsedFile,
    ParsedFunction,
    ParsedSymbol,
)
from asil_ingest.repo_cloner import ResolvedRepo

log = get_logger(__name__)


@dataclass(slots=True)
class GraphIngestStats:
    files: int = 0
    functions: int = 0
    classes: int = 0
    symbols: int = 0
    parse_error_files: int = 0
    elapsed_s: float = 0.0
    repo_key: str = ""
    failures: list[str] = field(default_factory=list)


def repo_key_for(repo: ResolvedRepo) -> str:
    """Stable identifier carried on every node so per-repo queries are a property filter."""
    if repo.org and repo.name:
        return f"{repo.org}/{repo.name}"
    return f"local:{repo.path}"


class GraphBuilder:
    """Coordinates the conversion of ParsedFiles → graph writes.

    Construct once per ingest run; reuse across many files. Calls
    `store.apply_schema()` on construction so the constraints exist before
    any MERGE runs (cheap; idempotent).
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._store.apply_schema()

    def upsert_repo(self, repo: ResolvedRepo) -> str:
        key = repo_key_for(repo)
        self._store.merge_repo(
            key=key,
            spec=repo.spec,
            org=repo.org,
            name=repo.name,
            is_local=repo.is_local,
            commit_sha=repo.commit_sha,
            indexed_at=datetime.now(UTC).isoformat(),
        )
        log.info("graph_repo_upserted", repo_key=key, spec=repo.spec)
        return key

    def write_file(self, repo_key: str, parsed: ParsedFile) -> None:
        file_props = _file_props(repo_key, parsed)
        functions_props = [_function_props(repo_key, parsed.path, fn) for fn in parsed.functions]
        # Methods get function nodes too; the CONTAINS edge from their Class is
        # added in the same query via cp.method_qnames.
        method_props: list[dict[str, Any]] = []
        class_props: list[dict[str, Any]] = []
        for cls in parsed.classes:
            for m in cls.methods:
                method_props.append(_function_props(repo_key, parsed.path, m))
            class_props.append(_class_props(repo_key, parsed.path, cls))

        symbols_props = [_symbol_props(repo_key, parsed.path, sym) for sym in parsed.symbols]

        self._store.merge_file_with_children(
            repo_key=repo_key,
            file_props=file_props,
            functions=functions_props + method_props,
            classes=class_props,
            symbols=symbols_props,
        )


# ---------------------------------------------------------------------------
# property marshallers — pull Neo4j-safe primitives + JSON strings out of pydantic
# ---------------------------------------------------------------------------


def _file_props(repo_key: str, parsed: ParsedFile) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "path": parsed.path,
        "language": parsed.language.value,
        "loc": parsed.loc,
        "module_name": parsed.module_name,
        "imports_json": json.dumps([imp.model_dump() for imp in parsed.imports]),
        "parse_errors": parsed.parse_errors,
        "n_functions": len(parsed.functions),
        "n_classes": len(parsed.classes),
        "n_methods": sum(len(c.methods) for c in parsed.classes),
        "n_symbols": len(parsed.symbols),
    }


def _function_props(repo_key: str, file_path: str, fn: ParsedFunction) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "file_path": file_path,
        "qualified_name": fn.qualified_name,
        "name": fn.name,
        "signature": fn.signature,
        "start_line": fn.start_line,
        "end_line": fn.end_line,
        "docstring": fn.docstring,
        "is_async": fn.is_async,
        "is_method": fn.is_method,
        "parent_class": fn.parent_class,
        "decorators": fn.decorators,
        # Calls aren't edges yet — SCIP resolves them in Phase 1.6.
        "calls_json": json.dumps([c.model_dump() for c in fn.calls]),
        "n_calls": len(fn.calls),
    }


def _class_props(repo_key: str, file_path: str, cls: ParsedClass) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "file_path": file_path,
        "qualified_name": cls.qualified_name,
        "name": cls.name,
        "start_line": cls.start_line,
        "end_line": cls.end_line,
        "docstring": cls.docstring,
        "decorators": cls.decorators,
        "base_classes": cls.base_classes,
        # Used by the merge query to draw Class→Method CONTAINS edges.
        "method_qnames": [m.qualified_name for m in cls.methods],
        "n_methods": len(cls.methods),
    }


def _symbol_props(repo_key: str, file_path: str, sym: ParsedSymbol) -> dict[str, Any]:
    return {
        "repo_key": repo_key,
        "file_path": file_path,
        "qualified_name": sym.qualified_name,
        "name": sym.name,
        "kind": sym.kind,
        "line": sym.line,
    }
