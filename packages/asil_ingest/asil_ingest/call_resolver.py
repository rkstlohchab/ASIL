"""Resolve `Function.calls_json` text references into real `:CALLS` edges.

Phase 1 step 3 stored each call site as a JSON blob: `[{callee: "router.call",
line: 87}, ...]`. That preserved the data without needing a symbol table.
This module is the symbol-table substitute: a lightweight resolver that
walks the graph, takes each unresolved callee text, and tries to point it
at an actual `Function` node in the same repo.

Why not run real SCIP yet? SCIP's strength is cross-file/cross-language
symbol resolution with full type inference. We don't need that for Phase 1's
demo bar — the vast majority of calls in a well-structured Python repo
resolve via a handful of heuristics. SCIP becomes essential when we hit
big polyglot repos with non-obvious imports; that's Phase 1.6 hardening
and Phase 4 cross-language causality.

Resolution strategies, tried in order of decreasing confidence:
  1. exact          — callee text is already a fully-qualified name in our index
  2. self_method    — `self.x(...)` inside a method → resolve to parent_class.x
  3. cls_method     — `cls.x(...)` inside a classmethod → same as self_method
  4. same_module    — bare `foo(...)` → look up `<module>.foo` in same file's module
  5. import_alias   — `j.dumps` where `j = json` (via aliased import) → `json.dumps`
  6. import_member  — `Optional` after `from typing import Optional` → `typing.Optional`

Unresolved calls stay in `calls_json`. The edge property `derivation` records
which heuristic matched so the data is auditable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CallResolutionStats:
    callers_inspected: int = 0
    total_call_sites: int = 0
    resolved: int = 0
    unresolved: int = 0
    by_strategy: dict[str, int] = field(default_factory=dict)

    def bump(self, strategy: str) -> None:
        self.resolved += 1
        self.by_strategy[strategy] = self.by_strategy.get(strategy, 0) + 1


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


class CallResolver:
    """Promotes calls_json blobs to :CALLS edges for one repo.

    Holds a lookup table of `qualified_name -> True` so we can answer
    "does this callee point to a known function?" in O(1). Built fresh
    per repo to keep memory bounded.
    """

    def __init__(self, graph_store: Any) -> None:
        self._gs = graph_store

    def resolve_repo(self, repo_key: str, *, batch: int = 200) -> CallResolutionStats:
        log.info("call_resolution_started", repo_key=repo_key)
        function_index = self._load_function_index(repo_key)
        file_imports = self._load_file_imports(repo_key)

        stats = CallResolutionStats()
        edges_to_write: list[dict[str, Any]] = []

        # Stream callers in batches so very large repos don't have to materialize
        # everything before the first write.
        for caller in self._iter_callers(repo_key):
            stats.callers_inspected += 1
            try:
                calls = json.loads(caller["calls_json"]) if caller.get("calls_json") else []
            except (TypeError, ValueError):
                continue
            stats.total_call_sites += len(calls)

            module_prefix = caller["file_module"] or ""
            parent_class = caller["parent_class"]
            imports = file_imports.get(caller["file_path"], _EMPTY_IMPORTS)

            for call in calls:
                callee_text = (call.get("callee") or "").strip()
                if not callee_text:
                    continue
                resolved, strategy = _resolve_one(
                    callee_text=callee_text,
                    module_prefix=module_prefix,
                    parent_class=parent_class,
                    imports=imports,
                    function_index=function_index,
                )
                if resolved is None:
                    stats.unresolved += 1
                    continue
                stats.bump(strategy)
                edges_to_write.append(
                    {
                        "caller_qname": caller["qualified_name"],
                        "callee_qname": resolved,
                        "line": int(call.get("line") or 0),
                        "derivation": strategy,
                        "callee_text": callee_text,
                    }
                )
                if len(edges_to_write) >= batch:
                    self._write_edges(repo_key, edges_to_write)
                    edges_to_write.clear()

        if edges_to_write:
            self._write_edges(repo_key, edges_to_write)

        log.info(
            "call_resolution_done",
            repo_key=repo_key,
            callers=stats.callers_inspected,
            sites=stats.total_call_sites,
            resolved=stats.resolved,
            unresolved=stats.unresolved,
            by_strategy=stats.by_strategy,
        )
        return stats

    def clear_repo_edges(self, repo_key: str) -> int:
        """Detach existing :CALLS edges before re-resolving so we don't
        compound errors across runs. Returns number of edges removed."""
        cypher = """
        MATCH (:Function {repo_key: $key})-[r:CALLS]->(:Function {repo_key: $key})
        WITH r, count(r) AS _c
        DELETE r
        RETURN count(*) AS removed
        """
        rows = self._gs.query(cypher, key=repo_key)
        return int(rows[0]["removed"]) if rows else 0

    # ------------------------------------------------------------------ reads

    def _iter_callers(self, repo_key: str) -> list[dict[str, Any]]:
        cypher = """
        MATCH (file:File {repo_key: $key})-[:CONTAINS]->(fn:Function {repo_key: $key})
        WHERE fn.n_calls > 0
        RETURN
            fn.qualified_name AS qualified_name,
            fn.parent_class AS parent_class,
            fn.calls_json AS calls_json,
            fn.file_path AS file_path,
            file.module_name AS file_module
        """
        return self._gs.query(cypher, key=repo_key)

    def _load_function_index(self, repo_key: str) -> set[str]:
        cypher = """
        MATCH (fn:Function {repo_key: $key})
        RETURN fn.qualified_name AS qn
        """
        return {row["qn"] for row in self._gs.query(cypher, key=repo_key) if row["qn"]}

    def _load_file_imports(self, repo_key: str) -> dict[str, _FileImports]:
        cypher = """
        MATCH (f:File {repo_key: $key})
        WHERE f.imports_json IS NOT NULL
        RETURN f.path AS path, f.imports_json AS imports_json
        """
        out: dict[str, _FileImports] = {}
        for row in self._gs.query(cypher, key=repo_key):
            try:
                imports = json.loads(row["imports_json"] or "[]")
            except (TypeError, ValueError):
                imports = []
            out[row["path"]] = _index_imports(imports)
        return out

    # ------------------------------------------------------------------ writes

    def _write_edges(self, repo_key: str, edges: list[dict[str, Any]]) -> None:
        """Batch UNWIND + MATCH + MERGE so one round-trip handles many edges."""
        cypher = """
        UNWIND $edges AS e
        MATCH (caller:Function {repo_key: $key, qualified_name: e.caller_qname})
        MATCH (callee:Function {repo_key: $key, qualified_name: e.callee_qname})
        MERGE (caller)-[r:CALLS {line: e.line}]->(callee)
        SET r.derivation = e.derivation, r.callee_text = e.callee_text
        """
        self._gs.query(cypher, key=repo_key, edges=edges)


# ---------------------------------------------------------------------------
# heuristics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _FileImports:
    # `from typing import Any, Optional as Opt` → members = {"Any": "typing.Any",
    # "Opt": "typing.Optional"}.
    members: dict[str, str] = field(default_factory=dict)
    # `import json as j` → aliases = {"j": "json"}; `import json` → {"json": "json"}.
    aliases: dict[str, str] = field(default_factory=dict)


_EMPTY_IMPORTS = _FileImports()


def _index_imports(imports: list[dict[str, Any]]) -> _FileImports:
    fi = _FileImports()
    for imp in imports:
        mod = imp.get("module") or ""
        if not mod:
            continue
        names = imp.get("names") or []
        alias_of = imp.get("alias_of") or {}

        if names:
            # `from module import a, b as c` — each name resolves to module.name
            for name in names:
                if name == "*":
                    continue
                fi.members[name] = f"{mod}.{name}"
            for alias, original in alias_of.items():
                fi.members[alias] = f"{mod}.{original}"
        else:
            # `import module` or `import module as m`
            if alias_of:
                for alias, original in alias_of.items():
                    fi.aliases[alias] = original
            else:
                # The bare-imported name is the last segment of the dotted path.
                last = mod.rsplit(".", 1)[-1]
                fi.aliases[last] = mod
                fi.aliases[mod] = mod  # also let full path resolve to itself
    return fi


def _resolve_one(
    *,
    callee_text: str,
    module_prefix: str | None,
    parent_class: str | None,
    imports: _FileImports,
    function_index: set[str],
) -> tuple[str | None, str]:
    """Return (resolved_qname, strategy) or (None, '<unresolved>')."""
    text = callee_text.strip()

    # 1. exact qname (rare but cheap to check first)
    if text in function_index:
        return text, "exact"

    # 2/3. method calls: self.x / cls.x / super().x within a class
    if parent_class:
        for prefix in ("self.", "cls."):
            if text.startswith(prefix):
                rest = text[len(prefix) :]
                candidate = f"{parent_class}.{rest}"
                if candidate in function_index:
                    return candidate, "self_method"
                break

    # 5/6. dotted callee: try resolving the head via imports
    if "." in text:
        head, _, tail = text.partition(".")
        if head in imports.aliases:
            target = f"{imports.aliases[head]}.{tail}"
            if target in function_index:
                return target, "import_alias"
        if head in imports.members:
            target = f"{imports.members[head]}.{tail}"
            if target in function_index:
                return target, "import_member"

    # 4. bare name: try same-module lookup
    if "." not in text and module_prefix:
        candidate = f"{module_prefix}.{text}"
        if candidate in function_index:
            return candidate, "same_module"

    # 6 (continued). bare name imported from elsewhere
    if "." not in text and text in imports.members:
        target = imports.members[text]
        if target in function_index:
            return target, "import_member"

    return None, "<unresolved>"
