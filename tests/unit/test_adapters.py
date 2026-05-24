"""Unit tests for the infrastructure adapter protocol + FileAdapter.

Tests the FileAdapter against a sample YAML fixture and verifies the
K8s/Prometheus/Loki stubs raise NotConfiguredError.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from asil_infra.adapters import (
    FileAdapter,
    InfraAdapter,
    K8sAdapter,
    LokiAdapter,
    NotConfiguredError,
    PrometheusAdapter,
)
from asil_infra.models import Deployment, LogSignature, MetricShift

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
SAMPLE_EVENTS = FIXTURE_DIR / "sample_events.yaml"


# ---------------------------------------------------------------------------
# FileAdapter
# ---------------------------------------------------------------------------


def test_file_adapter_loads_yaml_events() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = adapter.poll_sync("prod")
    assert len(events) == 3
    assert isinstance(events[0], Deployment)
    assert isinstance(events[1], MetricShift)
    assert isinstance(events[2], LogSignature)


def test_file_adapter_populates_env_key_and_source() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = adapter.poll_sync("staging")
    for ev in events:
        assert ev.env_key == "staging"
        assert ev.source.startswith("file:")


def test_file_adapter_deployment_fields() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = adapter.poll_sync("prod")
    deploy = events[0]
    assert isinstance(deploy, Deployment)
    assert deploy.deployment_id == "deploy-test-001"
    assert deploy.service_name == "auth"
    assert deploy.commit_sha == "abc123"
    assert deploy.description == "Redis pool refactor"


def test_file_adapter_metric_shift_fields() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = adapter.poll_sync("prod")
    ms = events[1]
    assert isinstance(ms, MetricShift)
    assert ms.metric == "p99_latency"
    assert ms.before == 120
    assert ms.after == 4200
    assert ms.unit == "ms"


def test_file_adapter_log_signature_fields() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = adapter.poll_sync("prod")
    ls = events[2]
    assert isinstance(ls, LogSignature)
    assert ls.signature == "Connection pool exhausted"
    assert ls.count == 42
    assert ls.level == "error"


@pytest.mark.asyncio
async def test_file_adapter_async_poll() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    events = await adapter.poll("prod")
    assert len(events) == 3


def test_file_adapter_is_infra_adapter() -> None:
    adapter = FileAdapter(SAMPLE_EVENTS)
    assert isinstance(adapter, InfraAdapter)


def test_file_adapter_rejects_unsupported_extension(tmp_path: Path) -> None:
    bad_file = tmp_path / "events.txt"
    bad_file.write_text("hello")
    with pytest.raises(ValueError, match="unsupported file type"):
        FileAdapter(bad_file).poll_sync("prod")


def test_file_adapter_skips_malformed_entries(tmp_path: Path) -> None:
    """Non-dict entries should be skipped without crashing."""
    events_file = tmp_path / "bad.yaml"
    events_file.write_text(
        '- "just a string"\n- kind: deployment\n  deployment_id: d1\n  service_name: svc\n  at: "2026-01-01T00:00:00+00:00"\n'
    )
    adapter = FileAdapter(events_file)
    events = adapter.poll_sync("prod")
    assert len(events) == 1  # only the valid entry


# ---------------------------------------------------------------------------
# Stubs raise NotConfiguredError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_k8s_adapter_raises_not_configured() -> None:
    with pytest.raises(NotConfiguredError, match="kubeconfig"):
        await K8sAdapter().poll("prod")


@pytest.mark.asyncio
async def test_prometheus_adapter_raises_not_configured() -> None:
    with pytest.raises(NotConfiguredError, match="endpoint"):
        await PrometheusAdapter().poll("prod")


@pytest.mark.asyncio
async def test_loki_adapter_raises_not_configured() -> None:
    with pytest.raises(NotConfiguredError, match="endpoint"):
        await LokiAdapter().poll("prod")
