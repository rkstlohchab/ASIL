"""External-system event models — Pull Requests, chat messages, tickets.

These are *not* runtime events (no env_key, no graph runtime namespace).
They live alongside the code namespace because they describe changes to
the code (PRs) or human conversations about incidents and tickets.

Graph mapping (added in GraphStore via `merge_pull_request` /
`merge_chat_message` / `merge_ticket`):

  (:PullRequest {repo_key, number})    -[:MERGES]-> (:Commit)
                                       -[:AUTHORED_BY]-> (:Author)
  (:ChatMessage {channel, ts})         -[:DISCUSSES]-> (:Incident)
                                       -[:MENTIONS]-> (:Service)
  (:Ticket {provider, key})            -[:LINKS_TO]-> (:Incident)
                                       -[:ASSIGNED_TO]-> (:Author)

Every event carries `source` (which adapter produced it) and `external_id`
(the canonical identifier in the source system) so re-polls MERGE on the
same key instead of creating duplicates.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ExternalKind(StrEnum):
    pull_request = "pull_request"
    chat_message = "chat_message"
    ticket = "ticket"


class _ExternalBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Annotated[
        str,
        Field(
            description=(
                "Which adapter produced this event. "
                "Examples: 'github://rkstlohchab/ASIL', 'slack://#incidents', "
                "'jira://INC', 'linear://ENG'."
            )
        ),
    ]
    external_id: Annotated[str, Field(min_length=1, description="Stable id in the source system.")]


class PullRequest(_ExternalBase):
    """A GitHub / GitLab / Bitbucket pull request.

    Identity = (repo_key, number). Linked into the code namespace via
    `merge_commit_sha` and `head_commit_sha` (when MERGED, the merge commit
    becomes a `(:PullRequest)-[:MERGES]->(:Commit)` edge).
    """

    repo_key: Annotated[str, Field(min_length=1)]
    number: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1)]
    state: str  # "open" | "closed" | "merged"
    author: str | None = None
    body: str | None = None
    head_commit_sha: str | None = None
    merge_commit_sha: str | None = None
    created_at: datetime
    merged_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    url: str | None = None

    def node_key(self) -> tuple[str, int]:
        return (self.repo_key, self.number)


class ChatMessage(_ExternalBase):
    """A message from Slack / Discord / Teams.

    Identity = (channel, ts). For Slack, `ts` is the message timestamp
    string ("1716606000.123456") which is unique per channel.

    `incident_ids` is populated when the message mentions an incident
    identifier (e.g. "INC-2026-04-12") so the graph builder can wire
    `(:ChatMessage)-[:DISCUSSES]->(:Incident)` edges directly.
    """

    channel: Annotated[str, Field(min_length=1)]
    ts: Annotated[str, Field(min_length=1)]
    author: str | None = None
    text: Annotated[str, Field(min_length=1)]
    posted_at: datetime
    permalink: str | None = None
    incident_ids: list[str] = Field(default_factory=list)
    service_names: list[str] = Field(default_factory=list)

    def node_key(self) -> tuple[str, str]:
        return (self.channel, self.ts)


class Ticket(_ExternalBase):
    """A Jira / Linear / Asana ticket.

    Identity = (provider, key). For Jira, `key` looks like "INC-1234". For
    Linear, it looks like "ENG-567".
    """

    provider: str  # "jira" | "linear" | ...
    key: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    status: str
    assignee: str | None = None
    reporter: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    url: str | None = None
    incident_ids: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    priority: str | None = None

    def node_key(self) -> tuple[str, str]:
        return (self.provider, self.key)


ExternalEvent = PullRequest | ChatMessage | Ticket
