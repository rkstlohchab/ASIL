"""ASIL Phase 8 — deterministic fix pipeline.

The constrained autonomous coder. Unlike free-form coding agents (Cursor /
OpenHands / Aider), this pipeline only proposes patches when it has a
Phase-5 causal chain to work from. The LLM is given:

  - The incident summary.
  - The top causal candidates with their derivation + confidence.
  - The specific files / functions implicated by those causes.

and it MUST output a minimal unified diff. The diff is then optionally
applied in an ephemeral sandbox, the test suite is run, and the outcome
is logged with full provenance. Nothing is pushed or merged
automatically — the proposal + sandbox result is the artifact a human
or a higher-level orchestrator decides on.

Designed to live behind a feature flag because it can modify code.
Read-only by default (`asil fix propose` shows the diff and exits);
opt-in to run the sandbox (`asil fix run`).
"""

from asil_fix.audit import AuditLog, FixAuditEntry
from asil_fix.models import (
    FixOutcome,
    FixProposal,
    SandboxOutcome,
    SandboxResult,
)
from asil_fix.patch_generator import PatchGenerator
from asil_fix.sandbox import LocalSandbox, NoOpSandbox, SandboxExecutor

__all__ = [
    "AuditLog",
    "FixAuditEntry",
    "FixOutcome",
    "FixProposal",
    "LocalSandbox",
    "NoOpSandbox",
    "PatchGenerator",
    "SandboxExecutor",
    "SandboxOutcome",
    "SandboxResult",
]
