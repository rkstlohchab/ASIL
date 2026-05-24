"""Architecture drift detection models.

Phase 6 types:
  - BaselineSnapshot: frozen picture of the graph's dependency structure
  - DriftEvent: one detected deviation from the baseline
  - ArchitectureBoundary: an explicit rule about allowed/forbidden dependencies
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class DependencyEdge:
    """One observed dependency between two modules/functions."""

    caller: str  # qualified_name of the caller
    callee: str  # qualified_name of the callee
    file_path: str = ""
    line: int = 0


@dataclass(slots=True)
class BaselineSnapshot:
    """A frozen picture of the code graph's dependency structure.

    Created by BaselineLearner.capture(). The snapshot records which
    dependency edges exist between modules/services at a point in time.
    """

    repo_key: str
    captured_at: datetime = field(default_factory=datetime.now)
    edges: list[DependencyEdge] = field(default_factory=list)
    module_count: int = 0
    function_count: int = 0

    @property
    def edge_set(self) -> set[tuple[str, str]]:
        """Unique (caller, callee) pairs for fast lookup."""
        return {(e.caller, e.callee) for e in self.edges}


@dataclass(slots=True)
class ArchitectureBoundary:
    """An explicit rule about allowed dependencies.

    Example: 'auth should not depend on payment internals.'
    """

    name: str
    source_pattern: str  # glob/prefix matching caller qualified names
    forbidden_pattern: str  # glob/prefix matching callee qualified names
    description: str = ""


@dataclass(slots=True)
class DriftEvent:
    """One detected deviation from the baseline.

    Produced by DriftDetector.detect(). Each event records a new dependency
    that wasn't present in the baseline, with context about why it matters.
    """

    kind: str  # "new_dependency" | "boundary_violation" | "removed_dependency"
    caller: str
    callee: str
    severity: str = "warning"  # "info" | "warning" | "critical"
    description: str = ""
    boundary_name: str | None = None  # which boundary was violated, if any
