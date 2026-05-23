"""ASIL eval harness.

Phase 1 surface:
  - recall: top-K retrieval recall over a hand-labeled Q&A corpus
  - corpus: built-in YAML corpora for known repos (currently ASIL itself)

Future phases:
  - end_to_end: full hero-query eval (causal accuracy on postmortems) — Phase 4/5
  - calibration: confidence calibration against ground truth — Phase 2
"""

from asil_eval.recall import (
    EvalCase,
    EvalCorpus,
    RecallResult,
    load_corpus,
    run_recall,
)

__version__ = "0.0.1"

__all__ = [
    "EvalCase",
    "EvalCorpus",
    "RecallResult",
    "load_corpus",
    "run_recall",
]
