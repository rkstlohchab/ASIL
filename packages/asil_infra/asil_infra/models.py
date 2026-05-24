"""Typed runtime events.

Every runtime observation ASIL ingests becomes one of these. They map 1:1 to
graph node labels in `asil_memory.graph_store` (`Service`, `Deployment`,
`MetricShift`, `LogSignature`, `Incident`). The Pydantic layer here exists
so postmortem YAMLs, K8s adapters, and Prometheus adapters all produce the
same canonical shape — the graph writer doesn't care which source produced
the event.

Design rules for Phase 3:
  - Every event carries `timestamp` (or start/end for ranges), `source`
    (which adapter or postmortem produced it), and `confidence` (how
    reliable the observation is — 1.0 for human-authored postmortems, less
    for noisy log-derived signatures).
  - Identity comes from observable fields, not surrogate ids — re-ingest
    is idempotent because the graph MERGEs on the same key. See `node_key`
    on each type.
  - No causal edges in Phase 3. `Incident.affected_services` records the
    fact, but `(:Deployment)-[:PRECEDED]->(:Incident)` is Phase 4.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class RuntimeKind(StrEnum):
    """Discriminator on the timeline entry's `kind:` field in a postmortem YAML."""

    service = "service"
    deployment = "deployment"
    metric_shift = "metric_shift"
    log_signature = "log_signature"
    incident = "incident"


class _RuntimeBase(BaseModel):
    """Common fields for every runtime node.

    `env_key` scopes events to an environment (prod, staging-eu, etc.) the
    same way `repo_key` scopes code nodes to a repository. Cross-namespace
    edges (e.g. `Service-[:RUNS]->File`) are what bridge the two halves of
    the graph.
    """

    model_config = ConfigDict(extra="forbid")

    env_key: Annotated[str, Field(min_length=1)]
    source: Annotated[
        str,
        Field(
            description=(
                "Which adapter / file produced this event. Examples: "
                "'postmortem:research/postmortems/cf-2025-08.yaml', "
                "'k8s://prod', 'prometheus://prod'."
            )
        ),
    ]
    confidence: Annotated[float, Field(ge=0.0, le=1.0, default=1.0)] = 1.0


class Service(_RuntimeBase):
    """A deployed service. Identity = (env_key, name)."""

    name: Annotated[str, Field(min_length=1)]
    repo_key: str | None = None  # optional link back into the code graph
    file_paths: list[str] = Field(default_factory=list)  # optional list of files this service runs

    def node_key(self) -> tuple[str, str]:
        return (self.env_key, self.name)


class Deployment(_RuntimeBase):
    """A discrete deploy event. Identity = (env_key, deployment_id)."""

    deployment_id: Annotated[str, Field(min_length=1)]
    service_name: Annotated[str, Field(min_length=1)]
    at: datetime  # when it shipped
    commit_sha: str | None = None
    description: str | None = None

    def node_key(self) -> tuple[str, str]:
        return (self.env_key, self.deployment_id)


class MetricShift(_RuntimeBase):
    """A change-point in a time series. Identity = (env_key, service, metric, started_at).

    Reference values (`before`, `after`, `unit`) are stored as properties so
    queries like "show me every metric shift > 10x" don't need a separate
    time-series store.
    """

    service_name: Annotated[str, Field(min_length=1)]
    metric: Annotated[str, Field(min_length=1)]
    started_at: datetime
    ended_at: datetime | None = None
    before: float | None = None
    after: float | None = None
    unit: str | None = None
    description: str | None = None

    def node_key(self) -> tuple[str, str, str, str]:
        return (self.env_key, self.service_name, self.metric, self.started_at.isoformat())


class LogSignature(_RuntimeBase):
    """A clustered log pattern. Identity = (env_key, service, signature_hash).

    We hash the signature text so re-occurrences of the same pattern merge
    instead of creating new nodes per minute.
    """

    service_name: Annotated[str, Field(min_length=1)]
    signature: Annotated[str, Field(min_length=1)]
    first_seen_at: datetime
    last_seen_at: datetime | None = None
    count: int = 1
    level: str | None = None  # "error" | "warning" | "info" | ...

    @property
    def signature_hash(self) -> str:
        return hashlib.sha1(self.signature.encode("utf-8")).hexdigest()[:16]

    def node_key(self) -> tuple[str, str, str]:
        return (self.env_key, self.service_name, self.signature_hash)


class Incident(_RuntimeBase):
    """A declared incident. Identity = (id,) — globally unique by intent.

    `affected_services` is captured as a property AND as edges
    `(:Incident)-[:AFFECTED]->(:Service)` for fan-out queries.
    """

    id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    severity: str = "unknown"
    detected_at: datetime
    resolved_at: datetime | None = None
    summary: str | None = None
    affected_services: list[str] = Field(default_factory=list)

    def node_key(self) -> tuple[str]:
        return (self.id,)


RuntimeEvent = Service | Deployment | MetricShift | LogSignature | Incident
