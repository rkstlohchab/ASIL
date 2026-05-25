"""External-system adapters — GitHub PRs, Slack channels, Jira / Linear tickets.

Each adapter follows the same `poll(...)` contract as the runtime
adapters, but the events they produce extend the code namespace (PRs link
to commits) and the runtime namespace (chat messages and tickets link to
incidents).

Adapters are token-gated: when the relevant env var is unset, the
adapter still imports cleanly so the CLI shows up — but `poll()` raises
`NotConfiguredError`. This keeps the surface honest about what's wired
versus what needs credentials.
"""

from asil_infra.external.github import GitHubAdapter
from asil_infra.external.jira import JiraAdapter
from asil_infra.external.linear import LinearAdapter
from asil_infra.external.slack import SlackAdapter

__all__ = [
    "GitHubAdapter",
    "JiraAdapter",
    "LinearAdapter",
    "SlackAdapter",
]
