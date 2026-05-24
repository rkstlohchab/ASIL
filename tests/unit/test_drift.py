"""Unit tests for architecture drift detection.

Uses a fake graph store to test BaselineLearner + DriftDetector logic
without Neo4j.
"""

from __future__ import annotations

from typing import Any

from asil_drift import (
    ArchitectureBoundary,
    BaselineLearner,
    BaselineSnapshot,
    DependencyEdge,
    DriftDetector,
)

# ---------------------------------------------------------------------------
# fake graph store
# ---------------------------------------------------------------------------


class FakeGraphStore:
    """Shim returning canned call edges and counts."""

    def __init__(
        self,
        edges: list[dict[str, Any]] | None = None,
        modules: int = 0,
        functions: int = 0,
    ) -> None:
        self._edges = edges or []
        self._modules = modules
        self._functions = functions

    def query(self, cypher: str, **kwargs: Any) -> list[dict[str, Any]]:
        if "CALLS" in cypher and "RETURN" in cypher:
            return self._edges
        if "count" in cypher.lower():
            return [{"modules": self._modules, "functions": self._functions}]
        return []


# ---------------------------------------------------------------------------
# BaselineLearner
# ---------------------------------------------------------------------------


def test_baseline_captures_edges() -> None:
    gs = FakeGraphStore(
        edges=[
            {"caller_qname": "a.foo", "callee_qname": "b.bar", "file_path": "a.py", "line": 10},
            {"caller_qname": "c.baz", "callee_qname": "d.qux", "file_path": "c.py", "line": 20},
        ],
        modules=5,
        functions=12,
    )
    snapshot = BaselineLearner(gs).capture("test/repo")
    assert snapshot.repo_key == "test/repo"
    assert len(snapshot.edges) == 2
    assert snapshot.module_count == 5
    assert snapshot.function_count == 12


def test_baseline_edge_set() -> None:
    snapshot = BaselineSnapshot(
        repo_key="test/repo",
        edges=[
            DependencyEdge(caller="a.foo", callee="b.bar"),
            DependencyEdge(caller="c.baz", callee="d.qux"),
        ],
    )
    assert snapshot.edge_set == {("a.foo", "b.bar"), ("c.baz", "d.qux")}


def test_baseline_empty_graph() -> None:
    gs = FakeGraphStore()
    snapshot = BaselineLearner(gs).capture("empty/repo")
    assert len(snapshot.edges) == 0
    assert snapshot.module_count == 0


# ---------------------------------------------------------------------------
# DriftDetector
# ---------------------------------------------------------------------------


def test_detect_new_dependency() -> None:
    """Edge in current graph but not in baseline → new_dependency."""
    gs = FakeGraphStore(
        edges=[
            {"caller_qname": "a.foo", "callee_qname": "b.bar", "file_path": "a.py", "line": 10},
        ],
    )
    baseline = BaselineSnapshot(repo_key="test/repo")  # empty baseline
    events = DriftDetector(gs).detect("test/repo", baseline)
    assert any(e.kind == "new_dependency" and e.caller == "a.foo" for e in events)


def test_detect_removed_dependency() -> None:
    """Edge in baseline but not in current graph → removed_dependency."""
    gs = FakeGraphStore(edges=[])  # current graph empty
    baseline = BaselineSnapshot(
        repo_key="test/repo",
        edges=[DependencyEdge(caller="a.foo", callee="b.bar")],
    )
    events = DriftDetector(gs).detect("test/repo", baseline)
    assert any(e.kind == "removed_dependency" and e.caller == "a.foo" for e in events)


def test_detect_no_drift_when_same() -> None:
    """Same edges in both → no events."""
    gs = FakeGraphStore(
        edges=[
            {"caller_qname": "a.foo", "callee_qname": "b.bar", "file_path": "a.py", "line": 10},
        ],
    )
    baseline = BaselineSnapshot(
        repo_key="test/repo",
        edges=[DependencyEdge(caller="a.foo", callee="b.bar")],
    )
    events = DriftDetector(gs).detect("test/repo", baseline)
    assert events == []


def test_detect_boundary_violation() -> None:
    """Edge matching a forbidden boundary → boundary_violation."""
    gs = FakeGraphStore(
        edges=[
            {
                "caller_qname": "auth.handler",
                "callee_qname": "payment._internal_process",
                "file_path": "auth.py",
                "line": 5,
            },
        ],
    )
    baseline = BaselineSnapshot(repo_key="test/repo")
    boundary = ArchitectureBoundary(
        name="auth_no_payment_internals",
        source_pattern="auth.*",
        forbidden_pattern="payment._*",
        description="auth should not depend on payment internals",
    )
    events = DriftDetector(gs).detect("test/repo", baseline, boundaries=[boundary])
    violations = [e for e in events if e.kind == "boundary_violation"]
    assert len(violations) == 1
    assert violations[0].boundary_name == "auth_no_payment_internals"
    assert violations[0].severity == "critical"


def test_detect_boundary_no_violation() -> None:
    """Edge that doesn't match any boundary → no boundary_violation."""
    gs = FakeGraphStore(
        edges=[
            {
                "caller_qname": "auth.handler",
                "callee_qname": "auth.utils",
                "file_path": "auth.py",
                "line": 5,
            },
        ],
    )
    baseline = BaselineSnapshot(
        repo_key="test/repo",
        edges=[DependencyEdge(caller="auth.handler", callee="auth.utils")],
    )
    boundary = ArchitectureBoundary(
        name="auth_no_payment_internals",
        source_pattern="auth.*",
        forbidden_pattern="payment._*",
        description="auth should not depend on payment internals",
    )
    events = DriftDetector(gs).detect("test/repo", baseline, boundaries=[boundary])
    assert events == []
