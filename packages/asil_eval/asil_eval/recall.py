"""Top-K retrieval recall over a Q&A corpus.

Why retrieval recall (and not "did the LLM say the right thing")?
  - It's deterministic given the same vectors + graph state. We can run
    it on every commit without flakiness.
  - The retriever is the input layer for every downstream feature — incident
    replay, drift, ask. If retrieval is bad, everything is bad. Catch
    regressions here, not in production demos.
  - It's cheap. Each case is one embed + one query — well under $0.001
    for the 10-case ASIL self-eval.

What it measures:
  For each case `(question, expected_qualified_names)`, run the retriever
  and check whether any of the expected qnames appear in the top-K results.
  Returns recall@1, recall@3, recall@5, recall@10, and per-case detail.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from asil_core.logging import get_logger
from asil_memory import HybridRetriever

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# corpus types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalCase:
    """One question + the qualified_names we expect the retriever to surface.

    `expected_any` semantics: the case passes if ANY of these qnames lands in
    the top-K. We use *any* (not *all*) because for fuzzy questions there are
    often multiple correct answers — the retriever just needs one of them.
    """

    question: str
    expected_any: list[str]
    notes: str = ""


@dataclass(slots=True)
class EvalCorpus:
    name: str
    repo_key: str | None
    cases: list[EvalCase]


@dataclass(slots=True)
class _CaseResult:
    case: EvalCase
    top_qnames: list[str]
    hit_rank: int | None  # 1-indexed rank where the expected match was found, or None

    def hit_at(self, k: int) -> bool:
        return self.hit_rank is not None and self.hit_rank <= k


@dataclass(slots=True)
class RecallResult:
    corpus: EvalCorpus
    cases: list[_CaseResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.cases)

    def recall_at(self, k: int) -> float:
        if not self.cases:
            return 0.0
        hits = sum(1 for c in self.cases if c.hit_at(k))
        return hits / len(self.cases)

    def summary(self) -> dict[str, Any]:
        return {
            "corpus": self.corpus.name,
            "repo_key": self.corpus.repo_key,
            "n_cases": self.n,
            "recall@1": round(self.recall_at(1), 3),
            "recall@3": round(self.recall_at(3), 3),
            "recall@5": round(self.recall_at(5), 3),
            "recall@10": round(self.recall_at(10), 3),
        }


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------


_BUILTIN_CORPORA: dict[str, str] = {
    "asil_self": "asil_self.yaml",
}


def load_corpus(name_or_path: str) -> EvalCorpus:
    """Load by built-in name (`asil_self`) or by filesystem path.

    The shape is intentionally simple YAML so contributors can hand-edit
    without learning a schema library:

      name: asil_self
      repo_key: local:/Users/.../ASIL
      cases:
        - question: "..."
          expected_any: ["pkg.mod.fn", "pkg.mod.OtherFn"]
          notes: ""
    """
    if name_or_path in _BUILTIN_CORPORA:
        text = (
            importlib.resources.files("asil_eval.corpus")
            .joinpath(_BUILTIN_CORPORA[name_or_path])
            .read_text(encoding="utf-8")
        )
    else:
        p = Path(name_or_path)
        if not p.exists():
            raise FileNotFoundError(
                f"corpus {name_or_path!r} not found (built-ins: {sorted(_BUILTIN_CORPORA)})"
            )
        text = p.read_text(encoding="utf-8")

    raw = yaml.safe_load(text) or {}
    cases = [
        EvalCase(
            question=c["question"],
            expected_any=list(c["expected_any"]),
            notes=c.get("notes", ""),
        )
        for c in raw.get("cases", [])
    ]
    return EvalCorpus(
        name=raw.get("name", name_or_path),
        repo_key=raw.get("repo_key"),
        cases=cases,
    )


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def run_recall(
    corpus: EvalCorpus,
    *,
    retriever: HybridRetriever,
    top_k: int = 10,
    repo_key_override: str | None = None,
) -> RecallResult:
    """Run every case through the retriever and collect rank-of-first-hit.

    `repo_key_override` lets you point a corpus at a freshly-ingested copy of
    the same repo (different machine = different `local:/...` path).
    """
    result = RecallResult(corpus=corpus)
    repo_key = repo_key_override or corpus.repo_key

    for case in corpus.cases:
        retrieval = await retriever.retrieve(case.question, repo_key=repo_key)
        top_qnames = [c.qualified_name for c in retrieval.candidates[:top_k]]
        hit_rank: int | None = None
        # Match by suffix so corpora keyed on short qnames still work when the
        # actual graph uses longer module prefixes (e.g. "packages.asil_..." vs
        # "asil_..."). Suffix match is conservative-safe: tail components are
        # unique within a repo by construction.
        for rank, qn in enumerate(top_qnames, start=1):
            if any(
                qn == e or qn.endswith("." + e) or e.endswith("." + qn) for e in case.expected_any
            ):
                hit_rank = rank
                break

        result.cases.append(_CaseResult(case=case, top_qnames=top_qnames, hit_rank=hit_rank))
        log.debug(
            "eval_case_done",
            question=case.question[:60],
            hit_rank=hit_rank,
            top1=top_qnames[0] if top_qnames else None,
        )

    return result
