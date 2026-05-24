"""Drift detector — compares current graph state against a baseline.

Given a BaselineSnapshot and the current graph, identifies:
  1. New dependencies not in the baseline
  2. Boundary violations (explicit forbidden edges)
  3. Removed dependencies (edges in baseline but not current)
"""

from __future__ import annotations

import fnmatch
from typing import Any

from asil_core.logging import get_logger

from asil_drift.baseline import BaselineLearner
from asil_drift.models import ArchitectureBoundary, DependencyEdge, DriftEvent

log = get_logger(__name__)


class DriftDetector:
    """Detects architecture drift by comparing current vs baseline."""

    def __init__(self, graph_store: Any) -> None:
        self._gs = graph_store
        self._learner = BaselineLearner(graph_store)

    def detect(
        self,
        repo_key: str,
        baseline: Any,  # BaselineSnapshot
        *,
        boundaries: list[ArchitectureBoundary] | None = None,
    ) -> list[DriftEvent]:
        """Compare current graph state against a baseline snapshot.

        Returns a list of DriftEvent instances describing deviations.
        """
        current_edges = self._learner._fetch_call_edges(repo_key)
        current_set = {(e.caller, e.callee) for e in current_edges}
        baseline_set = baseline.edge_set

        events: list[DriftEvent] = []

        # 1. New dependencies (in current but not baseline)
        new_edges = current_set - baseline_set
        for caller, callee in sorted(new_edges):
            event = DriftEvent(
                kind="new_dependency",
                caller=caller,
                callee=callee,
                severity="warning",
                description=(
                    f"New dependency: {caller} → {callee}. "
                    f"This edge was absent in the baseline captured at "
                    f"{baseline.captured_at.isoformat()}"
                ),
            )
            events.append(event)

        # 2. Boundary violations (check both new and existing edges)
        if boundaries:
            for edge in current_edges:
                violation = self._check_boundaries(edge, boundaries)
                if violation is not None:
                    events.append(violation)

        # 3. Removed dependencies (in baseline but not current)
        removed_edges = baseline_set - current_set
        for caller, callee in sorted(removed_edges):
            events.append(
                DriftEvent(
                    kind="removed_dependency",
                    caller=caller,
                    callee=callee,
                    severity="info",
                    description=(
                        f"Dependency removed: {caller} → {callee}. "
                        f"This edge existed in the baseline but is no longer present."
                    ),
                )
            )

        log.info(
            "drift_detected",
            repo_key=repo_key,
            new=len(new_edges),
            removed=len(removed_edges),
            total_events=len(events),
        )
        return events

    def _check_boundaries(
        self,
        edge: DependencyEdge,
        boundaries: list[ArchitectureBoundary],
    ) -> DriftEvent | None:
        """Check if an edge violates any declared boundary."""
        for boundary in boundaries:
            if fnmatch.fnmatch(edge.caller, boundary.source_pattern) and fnmatch.fnmatch(
                edge.callee, boundary.forbidden_pattern
            ):
                return DriftEvent(
                    kind="boundary_violation",
                    caller=edge.caller,
                    callee=edge.callee,
                    severity="critical",
                    description=(
                        f"Boundary violation ({boundary.name}): "
                        f"{edge.caller} depends on {edge.callee}. "
                        f"{boundary.description}"
                    ),
                    boundary_name=boundary.name,
                )
        return None
