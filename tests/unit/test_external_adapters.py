"""Unit tests for the external-system adapters.

The token-gated adapters (Slack / Jira / Linear) get mocked at the HTTP
layer so they run without real credentials. The GitHub adapter has a
real local-git test path that exercises its `git log` extraction against
this repository itself.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asil_infra.adapters import NotConfiguredError
from asil_infra.external import (
    GitHubAdapter,
    JiraAdapter,
    LinearAdapter,
    SlackAdapter,
)
from asil_infra.external.slack import _INCIDENT_ID_RE

REPO_ROOT = Path(__file__).resolve().parents[2]  # ASIL/


# ---------------------------------------------------------------- GitHub


def test_github_repo_key_inferred_from_origin():
    """When the repo has a github.com remote, repo_key should be 'org/name'.
    Falls back to 'local:<abspath>' otherwise."""
    adapter = GitHubAdapter(REPO_ROOT)
    # The ASIL repo has origin = github.com/rkstlohchab/ASIL
    assert adapter._repo_key.endswith("/ASIL") or adapter._repo_key.startswith("local:")


def test_github_raises_on_non_git_dir(tmp_path):
    adapter = GitHubAdapter(tmp_path)
    with pytest.raises(NotConfiguredError, match="not a git repo"):
        asyncio.run(adapter.poll())


def test_github_git_log_extracts_merge_commits(tmp_path):
    """Build a tiny git repo with one merge commit whose subject matches the
    GitHub merge-commit format, and verify the adapter picks it up via the
    `git log` fallback path."""
    if not _git_available():
        pytest.skip("git not installed")
    # Pin the initial branch — on runners with older git, default is
    # `master`, on newer it's `main`. This test checks out `main` below.
    _git(["init", "-b", "main", str(tmp_path)])
    _git(["-C", str(tmp_path), "config", "user.email", "test@asil.dev"])
    _git(["-C", str(tmp_path), "config", "user.name", "asil test"])
    _git(["-C", str(tmp_path), "config", "commit.gpgsign", "false"])
    (tmp_path / "a.txt").write_text("hello")
    _git(["-C", str(tmp_path), "add", "."])
    _git(["-C", str(tmp_path), "commit", "-m", "initial"])
    _git(["-C", str(tmp_path), "checkout", "-b", "feature"])
    (tmp_path / "b.txt").write_text("world")
    _git(["-C", str(tmp_path), "add", "."])
    _git(["-C", str(tmp_path), "commit", "-m", "add b"])
    _git(["-C", str(tmp_path), "checkout", "main"])
    _git(
        [
            "-C",
            str(tmp_path),
            "merge",
            "--no-ff",
            "feature",
            "-m",
            "Merge pull request #42 from foo/feature",
        ]
    )

    # Force the git-log path (no gh in tests).
    adapter = GitHubAdapter(tmp_path, prefer_gh_cli=False, since_days=365)
    prs = asyncio.run(adapter.poll())
    assert len(prs) == 1
    assert prs[0].number == 42
    assert prs[0].state == "merged"
    assert prs[0].merge_commit_sha is not None
    assert prs[0].title.startswith("Merge pull request #42")


# ----------------------------------------------------------------- Slack


def test_slack_raises_without_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    adapter = SlackAdapter(["C123"])
    with pytest.raises(NotConfiguredError, match="SLACK_BOT_TOKEN"):
        asyncio.run(adapter.poll())


def test_slack_extracts_incident_ids_and_services(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    body = {
        "ok": True,
        "messages": [
            {
                "ts": "1716606000.000100",
                "user": "U1",
                "text": "INC-2026-04-12 payments service is down again",
            },
            {
                "ts": "1716606060.000200",
                "user": "U2",
                "text": "see also INCIDENT-99 affecting auth",
            },
        ],
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=body)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        adapter = SlackAdapter(["C123"], known_services=["payments", "auth"])
        msgs = asyncio.run(adapter.poll())

    assert len(msgs) == 2
    assert msgs[0].incident_ids == ["INC-2026-04-12"]
    assert "payments" in msgs[0].service_names
    assert msgs[1].incident_ids == ["INCIDENT-99"]
    assert "auth" in msgs[1].service_names


def test_slack_api_error_raises_not_configured(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bad")
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"ok": False, "error": "invalid_auth"})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("httpx.AsyncClient", return_value=client),
        pytest.raises(NotConfiguredError, match="invalid_auth"),
    ):
        asyncio.run(SlackAdapter(["C123"]).poll())


def test_incident_id_regex_handles_common_shapes():
    cases = {
        "INC-2026-04-12-payments-cascade": "INC-2026-04-12-payments-cascade",
        "incident-99": "incident-99",
        "INC1234 happened": "INC1234",
    }
    for text, expected in cases.items():
        matches = [m.group(1) for m in _INCIDENT_ID_RE.finditer(text)]
        assert expected in matches


# ------------------------------------------------------------------ Jira


def test_jira_raises_without_credentials(monkeypatch):
    for v in ("JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(NotConfiguredError, match="Jira credentials"):
        asyncio.run(JiraAdapter(["INC"]).poll())


def test_jira_rejects_lowercase_project_keys():
    with pytest.raises(ValueError, match="uppercase"):
        JiraAdapter(["inc"])


def test_jira_normalises_issue_payload(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net")
    monkeypatch.setenv("JIRA_USER_EMAIL", "x@acme.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    issue = {
        "key": "INC-1",
        "fields": {
            "summary": "Payments outage from INC-2026-04-12-payments-cascade",
            "status": {"name": "Done"},
            "assignee": {"displayName": "Alice"},
            "reporter": {"displayName": "Bob"},
            "created": "2026-04-12T14:00:00.000+0000",
            "updated": "2026-04-12T15:00:00.000+0000",
            "resolutiondate": "2026-04-12T16:00:00.000+0000",
            "labels": ["postmortem", "p1"],
            "priority": {"name": "High"},
            "description": None,
        },
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"issues": [issue]})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        tickets = asyncio.run(JiraAdapter(["INC"]).poll())

    assert len(tickets) == 1
    t = tickets[0]
    assert t.key == "INC-1"
    assert t.status == "Done"
    assert t.assignee == "Alice"
    assert t.incident_ids == ["INC-2026-04-12-payments-cascade"]
    assert t.url == "https://acme.atlassian.net/browse/INC-1"


# ---------------------------------------------------------------- Linear


def test_linear_raises_without_token(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(NotConfiguredError, match="LINEAR_API_KEY"):
        asyncio.run(LinearAdapter(["ENG"]).poll())


def test_linear_parses_graphql_payload(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    body = {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "id": "abc",
                        "identifier": "ENG-42",
                        "title": "Investigate INC-2026-04-12-payments-cascade",
                        "description": "see incident-99",
                        "url": "https://linear.app/x/issue/ENG-42",
                        "createdAt": "2026-04-12T14:00:00.000Z",
                        "updatedAt": "2026-04-12T15:00:00.000Z",
                        "completedAt": None,
                        "priorityLabel": "Urgent",
                        "labels": {"nodes": [{"name": "bug"}, {"name": "p1"}]},
                        "state": {"name": "In Progress"},
                        "assignee": {"displayName": "Eve"},
                        "creator": {"displayName": "Mallory"},
                    }
                ]
            }
        }
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=body)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        tickets = asyncio.run(LinearAdapter(["ENG"]).poll())

    assert len(tickets) == 1
    t = tickets[0]
    assert t.key == "ENG-42"
    assert t.provider == "linear"
    assert t.priority == "Urgent"
    assert "bug" in t.labels
    assert {"INC-2026-04-12-payments-cascade", "incident-99"}.issubset(set(t.incident_ids))


# ---------------------------------------------------------------- helpers


def _git(args: list[str]) -> None:
    subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True, timeout=2)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
