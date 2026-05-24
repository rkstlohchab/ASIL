"""ASIL architecture drift detection.

Phase 6 — learns the expected dependency structure and flags deviations.

Public surface:
  - BaselineLearner: snapshots the current graph as a baseline
  - DriftDetector: compares current graph vs baseline, emits DriftEvents
  - Models: BaselineSnapshot, DependencyEdge, DriftEvent, ArchitectureBoundary
"""

from asil_drift.baseline import BaselineLearner
from asil_drift.detector import DriftDetector
from asil_drift.models import (
    ArchitectureBoundary,
    BaselineSnapshot,
    DependencyEdge,
    DriftEvent,
)

__version__ = "0.0.1"

__all__ = [
    "ArchitectureBoundary",
    "BaselineLearner",
    "BaselineSnapshot",
    "DependencyEdge",
    "DriftDetector",
    "DriftEvent",
]
