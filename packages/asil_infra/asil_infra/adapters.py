"""Infrastructure adapter protocol + file-based adapter.

Phase 3 step 2: defines the `InfraAdapter` protocol that all event sources
implement, plus a `FileAdapter` that reads events from YAML/JSON files. The
file adapter enables testing and demos without a live K8s/Prom/Loki stack.

Design:
  - `InfraAdapter.poll()` returns a list of `RuntimeEvent`s (the same models
    from `asil_infra.models`). The graph writer doesn't care which adapter
    produced the events.
  - Adapters are stateless and async (even if the file adapter doesn't need
    async, the protocol requires it for K8s/Prom/Loki).
  - `NotConfiguredError` is raised when an adapter needs credentials/config
    that aren't present (e.g., no kubeconfig for K8s).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from asil_core.logging import get_logger

from asil_infra.models import (
    Deployment,
    Incident,
    LogSignature,
    MetricShift,
    RuntimeEvent,
    RuntimeKind,
    Service,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotConfiguredError(Exception):
    """Raised when an adapter's prerequisites are not met."""

    pass


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class InfraAdapter(Protocol):
    """Any source of runtime events."""

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        """Fetch current events for a given environment."""
        ...


# ---------------------------------------------------------------------------
# File adapter
# ---------------------------------------------------------------------------


class FileAdapter:
    """Reads runtime events from a YAML or JSON file.

    File format: a list of dicts, each with at minimum `kind` (matching
    `RuntimeKind`) and the fields the corresponding model requires.

    Example YAML::

        - kind: deployment
          deployment_id: deploy-abc123
          service_name: auth
          at: "2026-04-12T14:17:00+00:00"
          description: "Redis pool refactor"

        - kind: metric_shift
          service_name: payments
          metric: p99_latency
          started_at: "2026-04-12T14:23:00+00:00"
          before: 120
          after: 4200
          unit: ms
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        """Parse the file and return typed RuntimeEvents."""
        return self.poll_sync(env_key)

    def poll_sync(self, env_key: str) -> list[RuntimeEvent]:
        """Synchronous version for non-async contexts."""
        raw = self._load_file()
        events: list[RuntimeEvent] = []
        source = f"file:{self._path.name}"

        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                log.warning("file_adapter_skip_non_dict", index=i)
                continue
            try:
                event = self._parse_entry(entry, env_key=env_key, source=source)
                events.append(event)
            except Exception as exc:
                log.warning("file_adapter_parse_error", index=i, error=str(exc))

        log.info("file_adapter_loaded", path=str(self._path), events=len(events))
        return events

    def _load_file(self) -> list[dict[str, Any]]:
        text = self._path.read_text(encoding="utf-8")
        if self._path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        elif self._path.suffix == ".json":
            data = json.loads(text)
        else:
            raise ValueError(f"unsupported file type: {self._path.suffix}")

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "events" in data:
            return data["events"]
        raise ValueError(f"expected a list or dict with 'events' key, got {type(data).__name__}")

    def _parse_entry(self, entry: dict[str, Any], *, env_key: str, source: str) -> RuntimeEvent:
        kind = entry.get("kind", "")
        common = {"env_key": env_key, "source": source, "confidence": entry.get("confidence", 1.0)}

        if kind == RuntimeKind.service:
            return Service(
                name=entry["name"],
                repo_key=entry.get("repo_key"),
                file_paths=entry.get("file_paths", []),
                **common,
            )

        if kind == RuntimeKind.deployment:
            return Deployment(
                deployment_id=entry["deployment_id"],
                service_name=entry["service_name"],
                at=entry["at"],
                commit_sha=entry.get("commit_sha"),
                description=entry.get("description"),
                **common,
            )

        if kind == RuntimeKind.metric_shift:
            return MetricShift(
                service_name=entry["service_name"],
                metric=entry["metric"],
                started_at=entry["started_at"],
                ended_at=entry.get("ended_at"),
                before=entry.get("before"),
                after=entry.get("after"),
                unit=entry.get("unit"),
                description=entry.get("description"),
                **common,
            )

        if kind == RuntimeKind.log_signature:
            return LogSignature(
                service_name=entry["service_name"],
                signature=entry["signature"],
                first_seen_at=entry["first_seen_at"],
                last_seen_at=entry.get("last_seen_at"),
                count=entry.get("count", 1),
                level=entry.get("level"),
                **common,
            )

        if kind == RuntimeKind.incident:
            return Incident(
                id=entry["id"],
                title=entry["title"],
                severity=entry.get("severity", "unknown"),
                detected_at=entry["detected_at"],
                resolved_at=entry.get("resolved_at"),
                summary=entry.get("summary"),
                affected_services=entry.get("affected_services", []),
                **common,
            )

        raise ValueError(f"unknown event kind: {kind!r}")


# ---------------------------------------------------------------------------
# K8s / Prometheus / Loki stubs
# ---------------------------------------------------------------------------


class K8sAdapter:
    """Kubernetes adapter — stub for Phase 3 step 3.

    Requires a kubeconfig or in-cluster config. When implemented, polls
    the cluster API for Deployment, Service, Pod, and ConfigMap events
    and normalizes them to RuntimeEvent.
    """

    def __init__(self, kubeconfig: str | None = None) -> None:
        self._kubeconfig = kubeconfig

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        raise NotConfiguredError(
            "K8s adapter requires a kubeconfig. Provide --kubeconfig or set "
            "KUBECONFIG env var. See PLAN.md Phase 3 step 3."
        )


class PrometheusAdapter:
    """Prometheus adapter — stub for Phase 3 step 3.

    When implemented, scrapes key metrics and runs change-point detection
    to emit MetricShift events.
    """

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = endpoint

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        raise NotConfiguredError(
            "Prometheus adapter requires an endpoint URL. Set PROMETHEUS_URL "
            "or pass --prometheus-url. See PLAN.md Phase 3 step 3."
        )


class LokiAdapter:
    """Loki adapter — stub for Phase 3 step 3.

    When implemented, streams logs, extracts error signatures, and emits
    LogSignature events.
    """

    def __init__(self, endpoint: str | None = None) -> None:
        self._endpoint = endpoint

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        raise NotConfiguredError(
            "Loki adapter requires an endpoint URL. Set LOKI_URL "
            "or pass --loki-url. See PLAN.md Phase 3 step 3."
        )
