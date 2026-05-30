"""Jira ticket adapter — token-gated.

Polls Jira's REST v3 search API for tickets updated in the last
`lookback_seconds` window and emits a `Ticket` per row. Optionally
extracts incident IDs from description / summary so the graph can wire
`(:Ticket)-[:LINKS_TO]->(:Incident)` directly.

Requires:
  - `JIRA_BASE_URL`: e.g. `https://acme.atlassian.net`
  - `JIRA_USER_EMAIL`: the email of a Jira user
  - `JIRA_API_TOKEN`: an API token from `id.atlassian.com/manage/api-tokens`

Without these the adapter raises `NotConfiguredError` cleanly so the CLI
shows up without crashing.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import UTC, datetime, timedelta

from asil_infra.adapters import NotConfiguredError
from asil_infra.external.slack import _INCIDENT_ID_RE  # share the regex
from asil_infra.external_models import Ticket

_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


class JiraAdapter:
    def __init__(
        self,
        projects: list[str],
        *,
        base_url: str | None = None,
        email: str | None = None,
        token: str | None = None,
        lookback_seconds: int = 86_400,
        limit: int = 100,
        timeout_seconds: float = 8.0,
    ) -> None:
        for p in projects:
            if not _PROJECT_RE.match(p):
                raise ValueError(f"jira project key must be uppercase: {p!r}")
        self._projects = projects
        self._base_url = (base_url or os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
        self._email = email or os.environ.get("JIRA_USER_EMAIL")
        self._token = token or os.environ.get("JIRA_API_TOKEN")
        self._lookback = lookback_seconds
        self._limit = limit
        self._timeout = timeout_seconds

    async def poll(self, env_key: str | None = None) -> list[Ticket]:
        if not (self._base_url and self._email and self._token):
            raise NotConfiguredError(
                "Jira credentials missing. Set JIRA_BASE_URL, "
                "JIRA_USER_EMAIL, JIRA_API_TOKEN env vars."
            )
        try:
            import httpx
        except ImportError as exc:
            raise NotConfiguredError("httpx not installed") from exc

        creds = base64.b64encode(f"{self._email}:{self._token}".encode()).decode("ascii")
        auth_header = {"Authorization": f"Basic {creds}", "Accept": "application/json"}

        since = datetime.now(UTC) - timedelta(seconds=self._lookback)
        jql = (
            f"project in ({', '.join(self._projects)}) AND "
            f"updated >= '{since.strftime('%Y-%m-%d %H:%M')}'"
        )

        out: list[Ticket] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.get(
                f"{self._base_url}/rest/api/3/search",
                headers=auth_header,
                params={
                    "jql": jql,
                    "fields": ",".join(
                        [
                            "summary",
                            "status",
                            "assignee",
                            "reporter",
                            "created",
                            "updated",
                            "resolutiondate",
                            "labels",
                            "priority",
                            "description",
                        ]
                    ),
                    "maxResults": str(self._limit),
                },
            )
            if r.status_code != 200:
                raise NotConfiguredError(f"Jira returned {r.status_code}: {r.text[:200]}")
            body = r.json()
            for issue in body.get("issues", []):
                f = issue.get("fields", {})
                description_text = _flatten_atlassian_doc(f.get("description"))
                summary_text = f.get("summary") or ""
                incident_ids = list(
                    {
                        m.group(1)
                        for m in _INCIDENT_ID_RE.finditer(f"{summary_text}\n{description_text}")
                    }
                )
                out.append(
                    Ticket(
                        source=f"jira://{self._base_url}",
                        external_id=issue["key"],
                        provider="jira",
                        key=issue["key"],
                        title=summary_text,
                        status=(f.get("status") or {}).get("name", "unknown"),
                        assignee=(f.get("assignee") or {}).get("displayName"),
                        reporter=(f.get("reporter") or {}).get("displayName"),
                        created_at=datetime.fromisoformat(f["created"].replace("Z", "+00:00")),
                        updated_at=datetime.fromisoformat(f["updated"].replace("Z", "+00:00"))
                        if f.get("updated")
                        else None,
                        closed_at=datetime.fromisoformat(f["resolutiondate"].replace("Z", "+00:00"))
                        if f.get("resolutiondate")
                        else None,
                        url=f"{self._base_url}/browse/{issue['key']}",
                        incident_ids=incident_ids,
                        labels=f.get("labels") or [],
                        priority=(f.get("priority") or {}).get("name"),
                    )
                )
        return out


def _flatten_atlassian_doc(doc) -> str:
    """Atlassian's ADF (Atlassian Document Format) is a JSON-tree rich-text
    structure. We don't need the formatting — just the plain-text
    concatenation for regex extraction. Returns "" on any error."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        return ""
    out: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)

    walk(doc)
    return " ".join(out)
