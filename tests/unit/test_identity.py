"""Unit tests for `asil_core.identity` — the helpers that resolve user,
machine, and origin-agent for memory-write attribution."""

from __future__ import annotations

import asil_core.identity as identity


def _reset() -> None:
    identity.reset_caches_for_tests()


def test_get_user_id_prefers_env(monkeypatch):
    _reset()
    monkeypatch.setenv("ASIL_USER_ID", "alice@startup.dev")
    assert identity.get_user_id() == "alice@startup.dev"


def test_get_user_id_falls_back_to_unknown(monkeypatch):
    """When the env is empty AND `git config user.email` returns nothing,
    we should return the safe literal 'unknown' rather than raising."""
    _reset()
    monkeypatch.delenv("ASIL_USER_ID", raising=False)
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": ""})(),
    )
    assert identity.get_user_id() == "unknown"


def test_get_user_id_uses_git_when_env_absent(monkeypatch):
    _reset()
    monkeypatch.delenv("ASIL_USER_ID", raising=False)

    def fake_run(*_a, **_k):
        return type("R", (), {"stdout": "bob@startup.dev\n"})()

    monkeypatch.setattr(identity.subprocess, "run", fake_run)
    assert identity.get_user_id() == "bob@startup.dev"


def test_get_machine_id_prefers_env(monkeypatch):
    _reset()
    monkeypatch.setenv("ASIL_MACHINE_ID", "workstation-7")
    assert identity.get_machine_id() == "workstation-7"


def test_get_machine_id_persists_to_file(monkeypatch, tmp_path):
    _reset()
    monkeypatch.delenv("ASIL_MACHINE_ID", raising=False)
    monkeypatch.setattr(identity, "_MACHINE_ID_PATH", tmp_path / "mid")
    first = identity.get_machine_id()
    assert first  # non-empty
    assert (tmp_path / "mid").read_text().strip() == first
    # Reset the in-process cache so a second call re-reads the file rather
    # than re-generating; both should agree.
    _reset()
    second = identity.get_machine_id()
    assert second == first


def test_get_origin_agent_passthrough_then_default():
    assert identity.get_origin_agent("claude-code") == "claude-code"
    assert identity.get_origin_agent(None) == "cli"
    assert identity.get_origin_agent("  ") == "cli"  # whitespace counts as empty
