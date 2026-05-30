"""GitHub PR adapter — works without any tokens for local repos.

Two modes, in order of preference:

  1. `gh` CLI mode: if `gh` is installed and authenticated, we shell out
     to it. This handles private repos and rate limits gracefully because
     `gh` reuses the user's existing auth.
  2. local-git mode: when `gh` is unavailable, we walk `git log` for
     merge commits whose message starts with "Merge pull request #N" and
     reconstruct a minimal `PullRequest` from that. The metadata is
     coarser (no labels, no PR body, no author email) but enough to wire
     `(:PullRequest)-[:MERGES]->(:Commit)` edges and surface "what
     changed before incident X" in the dashboard.

The adapter never authenticates against `api.github.com` directly — that
would force every user to set a PAT before they can index their own repo.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from asil_infra.adapters import NotConfiguredError
from asil_infra.external_models import PullRequest

_MERGE_COMMIT_RE = re.compile(r"^Merge pull request #(\d+) from ([^\s]+)")


class GitHubAdapter:
    def __init__(
        self,
        repo_path: str | Path,
        *,
        repo_key: str | None = None,
        limit: int = 50,
        since_days: int = 30,
        prefer_gh_cli: bool = True,
    ) -> None:
        self._repo_path = Path(repo_path).resolve()
        self._repo_key = repo_key or self._infer_repo_key()
        self._limit = limit
        self._since_days = since_days
        self._prefer_gh_cli = prefer_gh_cli

    # ------------------------------------------------------------------- public

    async def poll(self, env_key: str | None = None) -> list[PullRequest]:
        """Fetch PRs. `env_key` is accepted for protocol compatibility but
        unused — PRs are code-namespace events."""
        if not (self._repo_path / ".git").exists():
            raise NotConfiguredError(f"{self._repo_path} is not a git repo")
        if self._prefer_gh_cli and _gh_available():
            try:
                return await self._poll_via_gh()
            except Exception:
                # fall through to git-log path; never crash poll loop
                pass
        return self._poll_via_git_log()

    # ------------------------------------------------------------------ helpers

    def _infer_repo_key(self) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(self._repo_path), "remote", "get-url", "origin"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            url = r.stdout.strip()
            # https://github.com/org/repo(.git) | git@github.com:org/repo(.git)
            m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
            if m:
                return f"{m.group(1)}/{m.group(2)}"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return f"local:{self._repo_path}"

    async def _poll_via_gh(self) -> list[PullRequest]:
        """Shell out to `gh pr list --json`. Runs in a thread because the
        gh process is short-lived and synchronous."""
        loop = asyncio.get_event_loop()

        def _run() -> str:
            cmd = [
                "gh",
                "pr",
                "list",
                "--repo",
                self._repo_key,
                "--state",
                "all",
                "--limit",
                str(self._limit),
                "--json",
                "number,title,state,author,body,headRefOid,mergeCommit,createdAt,mergedAt,labels,url",
            ]
            r = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
            return r.stdout

        out = await loop.run_in_executor(None, _run)
        raw = json.loads(out)
        prs: list[PullRequest] = []
        for entry in raw:
            prs.append(
                PullRequest(
                    source=f"github://{self._repo_key}",
                    external_id=str(entry["number"]),
                    repo_key=self._repo_key,
                    number=int(entry["number"]),
                    title=entry["title"],
                    state=entry["state"].lower(),
                    author=(entry.get("author") or {}).get("login"),
                    body=entry.get("body"),
                    head_commit_sha=entry.get("headRefOid"),
                    merge_commit_sha=(entry.get("mergeCommit") or {}).get("oid"),
                    created_at=_parse_iso(entry["createdAt"]),
                    merged_at=_parse_iso(entry.get("mergedAt")) if entry.get("mergedAt") else None,
                    labels=[lbl["name"] for lbl in (entry.get("labels") or [])],
                    url=entry.get("url"),
                )
            )
        return prs

    def _poll_via_git_log(self) -> list[PullRequest]:
        """Walk merge commits and parse PR numbers from their subject lines.
        Limited metadata but works in any git repo without network access."""
        since = (datetime.now(UTC) - timedelta(days=self._since_days)).isoformat()
        try:
            r = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._repo_path),
                    "log",
                    f"--since={since}",
                    "--merges",
                    "--pretty=format:%H%x09%an%x09%aI%x09%s",
                    f"-n{self._limit}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError as exc:
            raise NotConfiguredError(f"git log failed in {self._repo_path}: {exc.stderr}") from exc

        prs: list[PullRequest] = []
        for line in r.stdout.splitlines():
            try:
                sha, author, when, subject = line.split("\t", 3)
            except ValueError:
                continue
            m = _MERGE_COMMIT_RE.match(subject)
            if not m:
                continue
            number = int(m.group(1))
            prs.append(
                PullRequest(
                    source=f"git-log://{self._repo_key}",
                    external_id=str(number),
                    repo_key=self._repo_key,
                    number=number,
                    title=subject,
                    state="merged",
                    author=author or None,
                    body=None,
                    head_commit_sha=None,
                    merge_commit_sha=sha,
                    created_at=_parse_iso(when),
                    merged_at=_parse_iso(when),
                )
            )
        return prs


def _gh_available() -> bool:
    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True,
            capture_output=True,
            timeout=2,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _parse_iso(s: str) -> datetime:
    """Parse GitHub's ISO-8601 timestamps. They sometimes ship with a trailing
    Z; `datetime.fromisoformat` accepts that from Python 3.11 onwards."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
