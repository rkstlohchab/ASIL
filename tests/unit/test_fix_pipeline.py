"""Unit tests for the Phase 8 fix pipeline.

We mock the LLM (so generator tests don't hit a real provider) and use
real temp directories for the sandbox (the sandbox is the part that has
to actually work; mocking it would test nothing). The audit log gets
mocked at the connection layer — its integration test lives in
`tests/integration/`.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from asil_fix import (
    LocalSandbox,
    NoOpSandbox,
    PatchGenerator,
)
from asil_fix.audit import AuditLog
from asil_fix.models import FixOutcome, FixProposal, SandboxOutcome
from asil_fix.patch_generator import (
    _aggregate_confidence,
    _extract_diff,
    _files_touched_by_diff,
)

# -------------------------------------------------------------------- helpers


def _proposal(**overrides) -> FixProposal:
    """Build a minimal FixProposal for non-generator tests."""
    base = dict(
        incident_id="INC-test",
        summary="patches a.txt",
        diff="",
        affected_files=["a.txt"],
        causal_chain=[],
        confidence_score=0.5,
        derivation=[],
        model="mock",
        cost_usd=0.0,
        generated_at=datetime.now(UTC),
    )
    base.update(overrides)
    return FixProposal(**base)


# ----------------------------------------------------------------- extractors


def test_extract_diff_from_fenced_block():
    text = (
        "Here is the fix:\n"
        "```diff\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```\n"
        "Hope that helps."
    )
    diff = _extract_diff(text)
    assert "--- a/foo.py" in diff
    assert "+new" in diff
    assert "```" not in diff


def test_extract_diff_from_bare_block():
    text = "Sure.\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    diff = _extract_diff(text)
    assert diff.startswith("--- a/foo.py")


def test_extract_diff_returns_empty_when_none_present():
    assert _extract_diff("just prose") == ""
    assert _extract_diff("") == ""


def test_files_touched_by_diff_extracts_b_paths():
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/bar.py\n+++ b/bar.py\n@@ -2 +2 @@\n-x\n+y\n"
    )
    assert _files_touched_by_diff(diff) == ["foo.py", "bar.py"]


def test_files_touched_dedupes():
    diff = "--- a/foo.py\n+++ b/foo.py\n@@\n-x\n+y\n--- a/foo.py\n+++ b/foo.py\n@@\n-z\n+w\n"
    assert _files_touched_by_diff(diff) == ["foo.py"]


# ----------------------------------------------------------- confidence math


def test_aggregate_confidence_uses_minimum():
    """A high-confidence replay built on a 30% cause should NOT inherit
    the replay's confidence — the weakest link bounds the proposal."""
    causes = [{"confidence": 0.3}]
    assert _aggregate_confidence(causes, replay_confidence=0.9) == 0.3
    causes = [{"confidence": 0.9}]
    assert _aggregate_confidence(causes, replay_confidence=0.4) == 0.4


def test_aggregate_confidence_empty_causes_is_zero():
    assert _aggregate_confidence([], replay_confidence=0.9) == 0.0


# ------------------------------------------------------- patch generator core


def test_patch_generator_raises_without_replay():
    """If `ReplayEngine.replay` returns None, the generator should refuse
    cleanly rather than feed an empty context to the LLM."""
    router = MagicMock()
    graph = MagicMock()

    with patch("asil_fix.patch_generator.ReplayEngine") as RE:
        RE.return_value.replay.return_value = None
        gen = PatchGenerator(router=router, graph_store=graph)
        with pytest.raises(ValueError, match="no replay"):
            asyncio.run(
                gen.propose(incident_id="INC-x", repo_root=".", repo_key="local:test")
            )


def test_patch_generator_raises_without_causes():
    router = MagicMock()
    graph = MagicMock()

    fake_replay = MagicMock()
    fake_replay.top_causes = []
    with patch("asil_fix.patch_generator.ReplayEngine") as RE:
        RE.return_value.replay.return_value = fake_replay
        gen = PatchGenerator(router=router, graph_store=graph)
        with pytest.raises(ValueError, match="no causal chain"):
            asyncio.run(
                gen.propose(incident_id="INC-x", repo_root=".", repo_key="local:test")
            )


def test_patch_generator_happy_path_parses_llm_output(tmp_path):
    """End-to-end: a replay with one cause, a real file under tmp_path, a
    mock LLM that returns a fenced diff — proposal should round-trip."""
    # Set up a fake repo file referenced by the cause props.
    fake_file = tmp_path / "auth.py"
    fake_file.write_text("def authenticate(token):\n    return True\n")

    fake_replay = MagicMock()
    fake_replay.incident_id = "INC-auth"
    fake_replay.incident = {"summary": "auth broke", "severity": "sev2"}
    fake_replay.top_causes = [
        {
            "cause_kind": "Deployment",
            "strategy": "temporal_proximity",
            "confidence": 0.42,
            "delta_seconds": 60.0,
            "derivation": "deploy 1 min before",
            "cause_props": {"file_path": "auth.py", "service_name": "auth"},
        }
    ]
    fake_replay.confidence.score = 0.6

    fake_llm = MagicMock()
    fake_llm.text = (
        "Here is the fix:\n"
        "```diff\n"
        "--- a/auth.py\n"
        "+++ b/auth.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def authenticate(token):\n"
        "+    # TODO: validate token signature\n"
        "     return True\n"
        "```"
    )
    fake_llm.model = "gpt-4o-mini"
    fake_llm.provider = "openai"
    fake_llm.cost_usd = 0.0003

    router = MagicMock()
    router.call = MagicMock(return_value=_async_value(fake_llm))
    graph = MagicMock()

    with patch("asil_fix.patch_generator.ReplayEngine") as RE:
        RE.return_value.replay.return_value = fake_replay
        gen = PatchGenerator(router=router, graph_store=graph)
        proposal = asyncio.run(
            gen.propose(incident_id="INC-auth", repo_root=tmp_path, repo_key="local:test")
        )

    assert proposal.incident_id == "INC-auth"
    assert proposal.affected_files == ["auth.py"]
    assert "TODO: validate token signature" in proposal.diff
    assert proposal.confidence_score == pytest.approx(0.42)  # bounded by weakest cause
    assert proposal.causal_chain == fake_replay.top_causes
    assert proposal.model == "gpt-4o-mini"


def test_patch_generator_truncates_oversized_file(tmp_path):
    """The context window should be capped — a 1MB file shouldn't blow up
    the prompt. We assert that the implicated file is truncated."""
    huge_file = tmp_path / "big.py"
    huge_file.write_text("x = 1\n" * 100_000)  # ~700KB

    fake_replay = MagicMock()
    fake_replay.incident_id = "INC-1"
    fake_replay.incident = {"summary": "x"}
    fake_replay.top_causes = [
        {
            "cause_kind": "Deployment",
            "strategy": "temporal_proximity",
            "confidence": 0.5,
            "delta_seconds": 1.0,
            "derivation": "",
            "cause_props": {"file_path": "big.py"},
        }
    ]
    fake_replay.confidence.score = 0.5

    captured_prompt = {"text": ""}

    def capture_call(**kwargs):
        captured_prompt["text"] = kwargs["messages"][-1]["content"]
        r = MagicMock(text="```diff\n--- a/big.py\n+++ b/big.py\n@@\n-old\n+new\n```",
                      model="m", provider="p", cost_usd=0.0)
        return _async_value(r)

    router = MagicMock()
    router.call = MagicMock(side_effect=capture_call)
    graph = MagicMock()

    with patch("asil_fix.patch_generator.ReplayEngine") as RE:
        RE.return_value.replay.return_value = fake_replay
        gen = PatchGenerator(
            router=router, graph_store=graph, max_context_chars_per_file=500
        )
        asyncio.run(
            gen.propose(incident_id="INC-1", repo_root=tmp_path, repo_key=None)
        )

    assert "[truncated at 500 chars]" in captured_prompt["text"]


# -------------------------------------------------------------- sandbox tests


def test_noop_sandbox_returns_not_run():
    res = NoOpSandbox().run(_proposal(diff="some-diff"), repo_root=".")
    assert res.outcome is SandboxOutcome.not_run
    assert res.duration_seconds == 0.0


def test_local_sandbox_rejects_empty_diff(tmp_path):
    res = LocalSandbox(test_command="true").run(_proposal(diff=""), repo_root=tmp_path)
    assert res.outcome is SandboxOutcome.apply_failed


def test_local_sandbox_apply_then_tests_pass(tmp_path):
    """Build a tiny git repo, generate a real diff for it, run a `true`
    test command, verify the sandbox reports tests_passed."""
    if not _git_available():
        pytest.skip("git not installed")

    _git(["init", str(tmp_path)])
    _git(["-C", str(tmp_path), "config", "user.email", "t@t"])
    _git(["-C", str(tmp_path), "config", "user.name", "t"])
    _git(["-C", str(tmp_path), "config", "commit.gpgsign", "false"])
    (tmp_path / "hello.txt").write_text("hello\n")
    _git(["-C", str(tmp_path), "add", "."])
    _git(["-C", str(tmp_path), "commit", "-m", "initial"])

    diff = (
        "--- a/hello.txt\n"
        "+++ b/hello.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+hello world\n"
    )
    result = LocalSandbox(test_command="true", timeout_seconds=30).run(
        _proposal(diff=diff, affected_files=["hello.txt"]),
        repo_root=tmp_path,
    )
    assert result.outcome is SandboxOutcome.tests_passed
    assert result.duration_seconds > 0


def test_local_sandbox_reports_test_failure(tmp_path):
    """A diff that applies cleanly but a test command that exits non-zero
    should land outcome=tests_failed (not apply_failed)."""
    if not _git_available():
        pytest.skip("git not installed")
    _git(["init", str(tmp_path)])
    _git(["-C", str(tmp_path), "config", "user.email", "t@t"])
    _git(["-C", str(tmp_path), "config", "user.name", "t"])
    _git(["-C", str(tmp_path), "config", "commit.gpgsign", "false"])
    (tmp_path / "a.txt").write_text("a\n")
    _git(["-C", str(tmp_path), "add", "."])
    _git(["-C", str(tmp_path), "commit", "-m", "initial"])

    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    result = LocalSandbox(test_command="false", timeout_seconds=30).run(
        _proposal(diff=diff),
        repo_root=tmp_path,
    )
    assert result.outcome is SandboxOutcome.tests_failed


def test_local_sandbox_apply_failed_when_diff_is_bogus(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    _git(["init", str(tmp_path)])
    _git(["-C", str(tmp_path), "config", "user.email", "t@t"])
    _git(["-C", str(tmp_path), "config", "user.name", "t"])
    _git(["-C", str(tmp_path), "config", "commit.gpgsign", "false"])
    (tmp_path / "x.txt").write_text("x\n")
    _git(["-C", str(tmp_path), "add", "."])
    _git(["-C", str(tmp_path), "commit", "-m", "initial"])

    bogus = "--- a/nonexistent.txt\n+++ b/nonexistent.txt\n@@ -1 +1 @@\n-foo\n+bar\n"
    result = LocalSandbox(test_command="true").run(_proposal(diff=bogus), repo_root=tmp_path)
    assert result.outcome is SandboxOutcome.apply_failed
    assert any("rejected" in n for n in result.notes)


# ------------------------------------------------------------------ audit log


def test_audit_classify_outcomes():
    """The aggregate classifier maps (sandbox outcome, confidence) -> FixOutcome
    along three branches. Pin each."""
    sandbox_passing = MagicMock(outcome=SandboxOutcome.tests_passed)
    sandbox_failing = MagicMock(outcome=SandboxOutcome.tests_failed)
    sandbox_not_run = MagicMock(outcome=SandboxOutcome.not_run)
    sandbox_apply_failed = MagicMock(outcome=SandboxOutcome.apply_failed)
    sandbox_timeout = MagicMock(outcome=SandboxOutcome.timeout)

    # Tests pass + high confidence -> accepted
    assert (
        AuditLog._classify(_proposal(confidence_score=0.9), sandbox_passing, confidence_gate=0.6)
        is FixOutcome.accepted
    )
    # Tests pass + LOW confidence -> inconclusive
    assert (
        AuditLog._classify(_proposal(confidence_score=0.2), sandbox_passing, confidence_gate=0.6)
        is FixOutcome.inconclusive
    )
    # Tests fail -> rejected regardless of confidence
    assert (
        AuditLog._classify(_proposal(confidence_score=0.9), sandbox_failing, confidence_gate=0.6)
        is FixOutcome.rejected
    )
    # Sandbox never ran -> proposed
    assert (
        AuditLog._classify(_proposal(), sandbox_not_run, confidence_gate=0.6)
        is FixOutcome.proposed
    )
    # Apply failed -> rejected (the diff was broken, not the codebase)
    assert (
        AuditLog._classify(_proposal(), sandbox_apply_failed, confidence_gate=0.6)
        is FixOutcome.rejected
    )
    # Timeout -> inconclusive
    assert (
        AuditLog._classify(_proposal(), sandbox_timeout, confidence_gate=0.6)
        is FixOutcome.inconclusive
    )


# ---------------------------------------------------------------- shell helpers


def _async_value(v):
    """Return a coroutine that resolves to `v` — for mocking async router.call."""

    async def _coro():
        return v

    return _coro()


def _git(args: list[str]) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True, timeout=2)
        return True
    except Exception:
        return False
