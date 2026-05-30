"""Linear ticket adapter — token-gated.

Linear's API is GraphQL only. We POST a single query that returns the N
most-recently-updated issues across the configured teams, then normalise
each to a `Ticket`.

Required env: `LINEAR_API_KEY` (a personal API key from
linear.app/settings/api). Without it, `poll()` raises `NotConfiguredError`.
"""

from __future__ import annotations

import os
from datetime import datetime

from asil_infra.adapters import NotConfiguredError
from asil_infra.external.slack import _INCIDENT_ID_RE  # share the regex
from asil_infra.external_models import Ticket

_LINEAR_GRAPHQL = """
query RecentIssues($teams: [String!], $first: Int!) {
  issues(
    filter: { team: { key: { in: $teams } } }
    orderBy: updatedAt
    first: $first
  ) {
    nodes {
      id
      identifier
      title
      description
      url
      createdAt
      updatedAt
      completedAt
      priorityLabel
      labels { nodes { name } }
      state { name }
      assignee { displayName }
      creator  { displayName }
    }
  }
}
"""


class LinearAdapter:
    def __init__(
        self,
        teams: list[str],
        *,
        token: str | None = None,
        limit: int = 100,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._teams = teams
        self._token = token or os.environ.get("LINEAR_API_KEY")
        self._limit = limit
        self._timeout = timeout_seconds

    async def poll(self, env_key: str | None = None) -> list[Ticket]:
        if not self._token:
            raise NotConfiguredError(
                "LINEAR_API_KEY not set. Generate one at linear.app/settings/api."
            )
        if not self._teams:
            return []
        try:
            import httpx
        except ImportError as exc:
            raise NotConfiguredError("httpx not installed") from exc

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                "https://api.linear.app/graphql",
                headers={
                    "Authorization": self._token,
                    "Content-Type": "application/json",
                },
                json={
                    "query": _LINEAR_GRAPHQL,
                    "variables": {"teams": self._teams, "first": self._limit},
                },
            )
            if r.status_code != 200:
                raise NotConfiguredError(f"Linear returned {r.status_code}: {r.text[:200]}")
            body = r.json()

        if "errors" in body:
            raise NotConfiguredError(f"Linear graphql error: {body['errors']}")

        out: list[Ticket] = []
        for node in body.get("data", {}).get("issues", {}).get("nodes", []) or []:
            description = node.get("description") or ""
            incident_ids = list(
                {
                    m.group(1)
                    for m in _INCIDENT_ID_RE.finditer(f"{node.get('title', '')}\n{description}")
                }
            )
            out.append(
                Ticket(
                    source="linear://api.linear.app",
                    external_id=node["id"],
                    provider="linear",
                    key=node["identifier"],
                    title=node["title"],
                    status=(node.get("state") or {}).get("name", "unknown"),
                    assignee=(node.get("assignee") or {}).get("displayName"),
                    reporter=(node.get("creator") or {}).get("displayName"),
                    created_at=_parse(node["createdAt"]),
                    updated_at=_parse(node.get("updatedAt")) if node.get("updatedAt") else None,
                    closed_at=_parse(node.get("completedAt")) if node.get("completedAt") else None,
                    url=node.get("url"),
                    incident_ids=incident_ids,
                    labels=[
                        n.get("name")
                        for n in (node.get("labels") or {}).get("nodes", [])
                        if n.get("name")
                    ],
                    priority=node.get("priorityLabel"),
                )
            )
        return out


def _parse(s: str | None) -> datetime:
    if not s:
        raise ValueError("missing timestamp")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
