"""Baseline learner — snapshots the current dependency structure.

Reads :CALLS edges from the graph and captures them as a BaselineSnapshot.
The snapshot becomes the reference point for drift detection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from asil_core.logging import get_logger

from asil_drift.models import BaselineSnapshot, DependencyEdge

log = get_logger(__name__)


class BaselineLearner:
    """Captures the current code graph's dependency structure as a baseline."""

    def __init__(self, graph_store: Any) -> None:
        self._gs = graph_store

    def capture(self, repo_key: str) -> BaselineSnapshot:
        """Read all :CALLS edges for a repo and freeze them as a baseline.

        The baseline is a lightweight snapshot — just the set of
        (caller_qname, callee_qname) pairs. No source code is stored.
        """
        edges = self._fetch_call_edges(repo_key)
        counts = self._fetch_counts(repo_key)

        snapshot = BaselineSnapshot(
            repo_key=repo_key,
            captured_at=datetime.now(),
            edges=edges,
            module_count=counts.get("modules", 0),
            function_count=counts.get("functions", 0),
        )

        log.info(
            "baseline_captured",
            repo_key=repo_key,
            edges=len(edges),
            modules=snapshot.module_count,
            functions=snapshot.function_count,
        )
        return snapshot

    def _fetch_call_edges(self, repo_key: str) -> list[DependencyEdge]:
        rows = self._gs.query(
            """
            MATCH (caller:Function {repo_key: $repo})-[r:CALLS]->(callee:Function)
            RETURN caller.qualified_name AS caller_qname,
                   callee.qualified_name AS callee_qname,
                   caller.file_path AS file_path,
                   r.line AS line
            """,
            repo=repo_key,
        )
        return [
            DependencyEdge(
                caller=r["caller_qname"],
                callee=r["callee_qname"],
                file_path=r.get("file_path") or "",
                line=r.get("line") or 0,
            )
            for r in rows
        ]

    def _fetch_counts(self, repo_key: str) -> dict[str, int]:
        rows = self._gs.query(
            """
            MATCH (f:File {repo_key: $repo})
            WITH count(f) AS modules
            OPTIONAL MATCH (fn:Function {repo_key: $repo})
            RETURN modules, count(fn) AS functions
            """,
            repo=repo_key,
        )
        if rows:
            return {"modules": rows[0].get("modules", 0), "functions": rows[0].get("functions", 0)}
        return {"modules": 0, "functions": 0}
