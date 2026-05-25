"""ASIL eval harness.

Phase 1 surface:
  - recall: top-K retrieval recall over a hand-labeled Q&A corpus
  - corpus: built-in YAML corpora for known repos (currently ASIL itself)

Future phases:
  - end_to_end: full hero-query eval (causal accuracy on postmortems) — Phase 4/5
  - calibration: confidence calibration against ground truth — Phase 2
"""

from asil_eval.pr_comment import to_pr_comment
from asil_eval.recall import (
    EvalCase,
    EvalCorpus,
    RecallResult,
    load_corpus,
    run_recall,
)
from asil_eval.sarif import to_sarif
from asil_eval.scan import ScanFinding, ScanReport, Severity, run_scan

__version__ = "0.0.1"

__all__ = [
    "EvalCase",
    "EvalCorpus",
    "RecallResult",
    "ScanFinding",
    "ScanReport",
    "Severity",
    "load_corpus",
    "run_recall",
    "run_scan",
    "to_pr_comment",
    "to_sarif",
]
