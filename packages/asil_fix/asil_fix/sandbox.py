"""Sandbox executor — apply a `FixProposal` in an ephemeral environment,
run a test command, capture the result.

Two implementations:

  - `LocalSandbox`: copies the repo into a `tempfile.TemporaryDirectory`,
    applies the diff via `git apply`, runs the configured test command
    with a wall-clock timeout. Fast, useful for development, but has no
    network or filesystem isolation beyond "different directory."

  - `NoOpSandbox`: returns a `not_run` result without doing anything.
    Used by `asil fix propose` (read-only mode) and by unit tests that
    don't want to spend real CPU.

The Docker-backed `DockerSandbox` is intentionally a stub here — running
a fresh image, mounting the patched code, dropping network, and
capturing exit codes is straightforward but adds dependency on docker-py.
The interface stays the same, so swapping in the real implementation
later is one constructor change at the call site.

Output is always a `SandboxResult` — never a raised exception. Sandboxes
are the boundary between "the LLM said something" and "we did something
about it"; raising would let one bad proposal kill a batch.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from asil_core.logging import get_logger

from asil_fix.models import FixProposal, SandboxOutcome, SandboxResult

log = get_logger(__name__)


class SandboxExecutor(Protocol):
    """Anything that takes a proposal + repo path and produces a result."""

    def run(self, proposal: FixProposal, repo_root: str | Path) -> SandboxResult: ...


class NoOpSandbox:
    """Returns `not_run` without touching anything. Default for
    propose-only flows."""

    def run(self, proposal: FixProposal, repo_root: str | Path) -> SandboxResult:
        now = datetime.now(UTC)
        return SandboxResult(
            proposal_incident_id=proposal.incident_id,
            outcome=SandboxOutcome.not_run,
            test_command=None,
            stdout_tail="",
            stderr_tail="",
            duration_seconds=0.0,
            started_at=now,
            ended_at=now,
            notes=["no-op sandbox; use LocalSandbox or DockerSandbox to actually test"],
        )


class LocalSandbox:
    """Copies the repo to a temp dir, applies the diff, runs `test_command`.

    Constructor knobs:
      - `test_command`: shell string. Defaults to `make test` because every
        ASIL-style repo has one; override per language.
      - `timeout_seconds`: hard wall-clock cap; SandboxOutcome.timeout on hit.
      - `keep_workdir`: don't delete the temp dir on exit (useful when
        debugging a failed apply). Off by default.

    Apply step uses `git apply --check` first as a dry-run; only commits
    to the real `git apply` if check passes. That way we surface
    `apply_failed` cleanly instead of leaving the temp tree half-patched.
    """

    def __init__(
        self,
        *,
        test_command: str = "make test",
        timeout_seconds: int = 300,
        keep_workdir: bool = False,
    ) -> None:
        self._test_command = test_command
        self._timeout = timeout_seconds
        self._keep_workdir = keep_workdir

    def run(self, proposal: FixProposal, repo_root: str | Path) -> SandboxResult:
        started = datetime.now(UTC)
        t0 = time.monotonic()
        repo_root = Path(repo_root).resolve()

        if not proposal.diff.strip():
            return _result(
                proposal,
                SandboxOutcome.apply_failed,
                test_command=None,
                started=started,
                duration=time.monotonic() - t0,
                notes=["proposal has empty diff"],
            )

        with self._workdir(prefix="asil-fix-") as work:
            shutil.copytree(
                repo_root,
                work / "repo",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".venv", "node_modules", ".next", "__pycache__",
                    ".pytest_cache", ".mypy_cache", ".ruff_cache",
                    ".asil_cache", ".git",
                ),
            )
            sandbox_repo = work / "repo"

            # 1. Validate the diff before touching anything.
            check = self._git(sandbox_repo, ["apply", "--check"], stdin=proposal.diff)
            if check.returncode != 0:
                return _result(
                    proposal,
                    SandboxOutcome.apply_failed,
                    test_command=None,
                    started=started,
                    duration=time.monotonic() - t0,
                    stderr_tail=_tail(check.stderr),
                    notes=["git apply --check rejected the diff"],
                )

            # 2. Apply for real.
            applied = self._git(sandbox_repo, ["apply"], stdin=proposal.diff)
            if applied.returncode != 0:
                return _result(
                    proposal,
                    SandboxOutcome.apply_failed,
                    test_command=None,
                    started=started,
                    duration=time.monotonic() - t0,
                    stderr_tail=_tail(applied.stderr),
                    notes=["git apply failed unexpectedly after --check passed"],
                )

            # 3. Run the test command.
            try:
                tested = subprocess.run(
                    self._test_command,
                    shell=True,
                    cwd=sandbox_repo,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired as exc:
                return _result(
                    proposal,
                    SandboxOutcome.timeout,
                    test_command=self._test_command,
                    started=started,
                    duration=time.monotonic() - t0,
                    stdout_tail=_tail(exc.stdout or ""),
                    stderr_tail=_tail(exc.stderr or ""),
                    notes=[f"timed out after {self._timeout}s"],
                )
            outcome = (
                SandboxOutcome.tests_passed
                if tested.returncode == 0
                else SandboxOutcome.tests_failed
            )
            return _result(
                proposal,
                outcome,
                test_command=self._test_command,
                started=started,
                duration=time.monotonic() - t0,
                stdout_tail=_tail(tested.stdout),
                stderr_tail=_tail(tested.stderr),
            )

    # ---------------------------------------------------------------- internals

    @contextmanager
    def _workdir(self, *, prefix: str):
        path = Path(tempfile.mkdtemp(prefix=prefix))
        try:
            yield path
        finally:
            if not self._keep_workdir:
                shutil.rmtree(path, ignore_errors=True)

    def _git(self, cwd: Path, args: list[str], *, stdin: str | None = None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


# ----------------------------------------------------------------------- shared


def _tail(text: str, *, limit_bytes: int = 4096) -> str:
    if not text:
        return ""
    if len(text) <= limit_bytes:
        return text
    return f"... [truncated, last {limit_bytes} bytes]\n" + text[-limit_bytes:]


def _result(
    proposal: FixProposal,
    outcome: SandboxOutcome,
    *,
    test_command: str | None,
    started: datetime,
    duration: float,
    stdout_tail: str = "",
    stderr_tail: str = "",
    notes: list[str] | None = None,
) -> SandboxResult:
    return SandboxResult(
        proposal_incident_id=proposal.incident_id,
        outcome=outcome,
        test_command=test_command,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        duration_seconds=round(duration, 3),
        started_at=started,
        ended_at=datetime.now(UTC),
        notes=notes or [],
    )
