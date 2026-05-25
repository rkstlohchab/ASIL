"""Models for the Phase 8 fix pipeline.

Two key invariants:
  - `FixProposal.causal_chain` is the source of truth for *why* this fix
    exists. It MUST be a slice of the Phase-5 `ReplayResult` that the
    proposal was generated from, so reviewers can audit "did the LLM
    actually act on the evidence ASIL handed it?"
  - `SandboxResult.outcome` is one of a small enum so the audit log can
    aggregate cleanly. Free-form strings are reserved for `notes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class SandboxOutcome(StrEnum):
    """Top-level result of running a proposed patch in a sandbox."""

    not_run = "not_run"          # propose-only — sandbox never executed
    apply_failed = "apply_failed"  # the diff didn't apply cleanly
    tests_passed = "tests_passed"
    tests_failed = "tests_failed"
    timeout = "timeout"
    sandbox_error = "sandbox_error"


class FixOutcome(StrEnum):
    """Aggregate outcome surfaced to the audit log."""

    proposed = "proposed"
    accepted = "accepted"     # tests green, confidence above gate
    rejected = "rejected"     # tests red, low confidence, or apply_failed
    inconclusive = "inconclusive"


@dataclass(slots=True)
class FixProposal:
    """One proposed fix for one incident.

    Identity = (incident_id, generated_at). The same incident can have
    many proposals — for example, one per causal strategy ("the
    proximity strategy said the auth deploy did it; the lagged-
    correlation strategy agreed; here are two proposed reverts").
    """

    incident_id: str
    summary: str            # 1-line "what this fix does"
    diff: str               # unified diff body, ready for `git apply`
    affected_files: list[str]
    causal_chain: list[dict[str, Any]]  # slice of ReplayResult.top_causes
    confidence_score: float  # 0.0-1.0; assembled from causal + retrieval + verifier
    derivation: list[str]    # human-readable reasoning trail
    model: str
    cost_usd: float
    generated_at: datetime
    repo_key: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SandboxResult:
    """Outcome of applying a `FixProposal` in an ephemeral environment."""

    proposal_incident_id: str
    outcome: SandboxOutcome
    test_command: str | None
    stdout_tail: str        # last ~4KB of test stdout (truncated for the ledger)
    stderr_tail: str
    duration_seconds: float
    started_at: datetime
    ended_at: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome is SandboxOutcome.tests_passed
