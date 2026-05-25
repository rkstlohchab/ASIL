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


class PrometheusAdapter:
    """Live Prometheus adapter.

    Polls a configured set of (service, metric, query) probes and emits a
    `MetricShift` whenever the most-recent value diverges from a baseline
    by more than `shift_threshold` (relative change).

    Detection is intentionally simple — a windowed comparison, not change-
    point detection. Two reasons:
      1. Real change-point detection (PELT / BOCPD) needs a multi-minute
         lookback we don't always have in the demo cluster.
      2. The downstream causal linker only cares that *some* signal flipped;
         the precise timestamp gets refined as more data lands.

    Each probe is a tuple `(service_name, metric_name, promql)`. Example:

        probes=[
            ("payments", "p99_latency", 'histogram_quantile(0.99, ...)'),
            ("auth", "error_rate", 'sum(rate(http_5xx_total[1m]))'),
        ]

    `NotConfiguredError` is raised when the endpoint isn't reachable so
    callers can fall through to other adapters cleanly.
    """

    def __init__(
        self,
        endpoint: str,
        probes: list[tuple[str, str, str]] | None = None,
        *,
        shift_threshold: float = 1.5,
        baseline_window_seconds: int = 300,
        current_window_seconds: int = 60,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._probes = probes or []
        self._shift_threshold = shift_threshold
        self._baseline_seconds = baseline_window_seconds
        self._current_seconds = current_window_seconds
        self._timeout = timeout_seconds

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        from datetime import UTC, datetime, timedelta

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # health probe so callers can distinguish "no shifts" from "down"
                r = await client.get(f"{self._endpoint}/-/ready")
                if r.status_code >= 500:
                    raise NotConfiguredError(
                        f"Prometheus returned {r.status_code} from /-/ready"
                    )

                out: list[RuntimeEvent] = []
                now = datetime.now(UTC)
                for service_name, metric, promql in self._probes:
                    baseline = await self._instant_query(
                        client,
                        promql,
                        at=now - timedelta(seconds=self._baseline_seconds),
                    )
                    current = await self._instant_query(client, promql, at=now)
                    if baseline is None or current is None or baseline == 0:
                        continue
                    ratio = current / baseline
                    if ratio < self._shift_threshold and ratio > (1 / self._shift_threshold):
                        continue
                    out.append(
                        MetricShift(
                            env_key=env_key,
                            source=f"prometheus://{self._endpoint}",
                            service_name=service_name,
                            metric=metric,
                            started_at=now - timedelta(seconds=self._current_seconds),
                            ended_at=now,
                            before=baseline,
                            after=current,
                            unit=None,
                            description=(
                                f"{metric} shifted {ratio:.2f}x "
                                f"({baseline:.3f} -> {current:.3f}) "
                                f"over {self._current_seconds}s window"
                            ),
                            confidence=min(1.0, abs(ratio - 1.0) / 5.0),
                        )
                    )
                return out
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise NotConfiguredError(
                f"Prometheus at {self._endpoint} unreachable: {exc}"
            ) from exc

    async def _instant_query(
        self, client: Any, promql: str, *, at: Any
    ) -> float | None:
        """Single-point query at a specific time. Returns the scalar / vector
        value or None if the result is empty."""
        params = {"query": promql, "time": at.timestamp()}
        r = await client.get(f"{self._endpoint}/api/v1/query", params=params)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("status") != "success":
            return None
        result = body.get("data", {}).get("result", [])
        if not result:
            return None
        # vector: [{ metric: {...}, value: [ts, "1.23"] }]
        # scalar: { value: [ts, "1.23"] }
        first = result[0] if isinstance(result, list) else result
        v = first.get("value") if isinstance(first, dict) else None
        if not v or len(v) < 2:
            return None
        try:
            return float(v[1])
        except (TypeError, ValueError):
            return None


class LokiAdapter:
    """Live Loki adapter.

    Polls Loki for recent error / warning log lines, groups them by a
    redacted signature (numeric / hex tokens collapsed), and emits one
    `LogSignature` per distinct pattern.

    Designed for the docker-compose Loki on `:3100`. Wire to your
    real Loki by changing the endpoint.
    """

    def __init__(
        self,
        endpoint: str,
        service_label: str = "job",
        services: list[str] | None = None,
        *,
        lookback_seconds: int = 300,
        level_filter: str = "error",
        limit: int = 1000,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._service_label = service_label
        self._services = services or []
        self._lookback_seconds = lookback_seconds
        self._level_filter = level_filter
        self._limit = limit
        self._timeout = timeout_seconds

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        from datetime import UTC, datetime, timedelta

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(f"{self._endpoint}/ready")
                if r.status_code >= 500:
                    raise NotConfiguredError(
                        f"Loki returned {r.status_code} from /ready"
                    )

                end = datetime.now(UTC)
                start = end - timedelta(seconds=self._lookback_seconds)
                out: list[RuntimeEvent] = []

                # If no services configured, poll across all jobs once.
                services = self._services or [None]  # type: ignore[list-item]
                for service in services:
                    if service:
                        selector = (
                            f'{{{self._service_label}="{service}"}} '
                            f'|~ "(?i){self._level_filter}"'
                        )
                    else:
                        selector = f'{{job=~".+"}} |~ "(?i){self._level_filter}"'
                    params = {
                        "query": selector,
                        "start": str(int(start.timestamp() * 1_000_000_000)),
                        "end": str(int(end.timestamp() * 1_000_000_000)),
                        "limit": str(self._limit),
                        "direction": "backward",
                    }
                    r = await client.get(
                        f"{self._endpoint}/loki/api/v1/query_range",
                        params=params,
                    )
                    if r.status_code != 200:
                        continue
                    body = r.json()
                    if body.get("status") != "success":
                        continue
                    by_signature: dict[str, dict[str, Any]] = {}
                    for stream in body.get("data", {}).get("result", []):
                        labels = stream.get("stream", {})
                        svc = labels.get(self._service_label) or service or "unknown"
                        for entry in stream.get("values", []):
                            ts_ns_str, msg = entry[0], entry[1]
                            sig = _redact_log_signature(msg)
                            key = f"{svc}:{sig}"
                            ts = datetime.fromtimestamp(
                                int(ts_ns_str) / 1_000_000_000, tz=UTC
                            )
                            slot = by_signature.setdefault(
                                key,
                                {
                                    "service": svc,
                                    "signature": sig,
                                    "first_seen_at": ts,
                                    "last_seen_at": ts,
                                    "count": 0,
                                },
                            )
                            slot["count"] += 1
                            if ts < slot["first_seen_at"]:
                                slot["first_seen_at"] = ts
                            if ts > slot["last_seen_at"]:
                                slot["last_seen_at"] = ts

                    for slot in by_signature.values():
                        out.append(
                            LogSignature(
                                env_key=env_key,
                                source=f"loki://{self._endpoint}",
                                service_name=slot["service"],
                                signature=slot["signature"],
                                first_seen_at=slot["first_seen_at"],
                                last_seen_at=slot["last_seen_at"],
                                count=slot["count"],
                                level=self._level_filter,
                                confidence=min(1.0, slot["count"] / 10.0),
                            )
                        )
                return out
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise NotConfiguredError(
                f"Loki at {self._endpoint} unreachable: {exc}"
            ) from exc


_LOG_TOKEN_RE = None


def _redact_log_signature(line: str) -> str:
    """Collapse high-cardinality tokens (UUIDs, hex IDs, numbers) so that
    the same root error message clusters into one signature even when each
    occurrence carries unique trace IDs / counts / paths."""
    import re

    global _LOG_TOKEN_RE
    if _LOG_TOKEN_RE is None:
        # Match numeric / hex / uuid / ISO-timestamp tokens *without* word
        # boundaries on either side — log messages frequently embed numbers
        # next to letters (e.g. `1234ms`, `id=abc123`) where `\b` would not
        # match. The ISO-timestamp alternative is listed first so the digit
        # alternative doesn't gobble up its leading year segment.
        _LOG_TOKEN_RE = re.compile(
            r"(?:"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # uuid
            r"|\d{4}-\d{2}-\d{2}T[\d:.]+Z?"  # iso-8601 timestamp
            r"|0x[0-9a-fA-F]+"
            r"|[0-9a-fA-F]{32,}"  # long hex strings (eg SHAs)
            r"|\d+(?:\.\d+)?"  # any int / float
            r")"
        )
    redacted = _LOG_TOKEN_RE.sub("<n>", line)
    # cap signature length so a huge log line doesn't blow up the graph
    return redacted[:200]


class K8sAdapter:
    """Kubernetes adapter — best-effort implementation.

    Requires a kubeconfig file (defaults to `~/.kube/config` or
    `$KUBECONFIG`). Polls the cluster API for Deployments per namespace and
    emits one `Deployment` event for each, plus one `Service` event per
    K8s Service object.

    No live cluster is provisioned by the docker-compose; this adapter is
    here for users who run ASIL alongside a real cluster. `NotConfiguredError`
    is raised cleanly when no kubeconfig is reachable so the API falls back
    to other adapters.
    """

    def __init__(
        self,
        kubeconfig: str | None = None,
        namespace: str = "default",
        *,
        emit_services: bool = True,
        emit_deployments: bool = True,
    ) -> None:
        self._kubeconfig = kubeconfig
        self._namespace = namespace
        self._emit_services = emit_services
        self._emit_deployments = emit_deployments

    async def poll(self, env_key: str) -> list[RuntimeEvent]:
        try:
            from kubernetes_asyncio import client, config  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NotConfiguredError(
                "kubernetes-asyncio not installed. "
                "Run `uv add kubernetes-asyncio` in the asil_infra workspace."
            ) from exc

        try:
            if self._kubeconfig:
                await config.load_kube_config(config_file=self._kubeconfig)
            else:
                try:
                    await config.load_kube_config()
                except Exception:
                    config.load_incluster_config()
        except Exception as exc:
            raise NotConfiguredError(f"no kubeconfig usable: {exc}") from exc

        out: list[RuntimeEvent] = []
        async with client.ApiClient() as api:
            if self._emit_services:
                core = client.CoreV1Api(api)
                svcs = await core.list_namespaced_service(self._namespace)
                for s in svcs.items:
                    out.append(
                        Service(
                            env_key=env_key,
                            source=f"k8s://{self._namespace}",
                            name=s.metadata.name,
                            confidence=1.0,
                        )
                    )
            if self._emit_deployments:
                apps = client.AppsV1Api(api)
                deps = await apps.list_namespaced_deployment(self._namespace)
                for d in deps.items:
                    created = d.metadata.creation_timestamp
                    out.append(
                        Deployment(
                            env_key=env_key,
                            source=f"k8s://{self._namespace}",
                            deployment_id=f"{self._namespace}/{d.metadata.name}",
                            service_name=d.metadata.name,
                            at=created,
                            description=f"{d.spec.replicas} replicas",
                            confidence=1.0,
                        )
                    )
        return out
