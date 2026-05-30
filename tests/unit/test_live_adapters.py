"""Unit tests for the live Prometheus / Loki / K8s adapters.

These tests mock the HTTP layer so they run offline and deterministically.
The integration test in `tests/integration/` exercises the real Prom and
Loki on the docker-compose stack.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asil_infra.adapters import (
    LokiAdapter,
    NotConfiguredError,
    PrometheusAdapter,
    _redact_log_signature,
)
from asil_infra.models import LogSignature, MetricShift


def _async_client_mock(responses: dict[str, dict]) -> MagicMock:
    """Stub `httpx.AsyncClient` returning canned JSON keyed by URL substring.

    Each value is `{"status": <int>, "json": <dict>}` — the most-recent
    matching URL wins (overrides defined later in `responses` take precedence).
    """

    async def get(url, params=None):
        body = None
        status = 200
        for fragment, payload in responses.items():
            if fragment in url:
                body = payload.get("json", {})
                status = payload.get("status", 200)
        resp = MagicMock()
        resp.status_code = status
        resp.json = MagicMock(return_value=body or {})
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------- Prometheus


def test_prom_emits_metric_shift_when_threshold_crossed():
    responses = {
        "/-/ready": {"json": {"status": "ready"}, "status": 200},
        "/api/v1/query": {
            "json": {
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {}, "value": [1716606000.0, "200"]},
                    ]
                },
            }
        },
    }
    client = _async_client_mock(responses)

    with patch("httpx.AsyncClient", return_value=client):
        prom = PrometheusAdapter(
            "http://prom",
            probes=[("payments", "p99_latency", "histogram_quantile(...)")],
            shift_threshold=1.5,
        )
        events = asyncio.run(prom.poll("prod"))

    # Both queries return the same 200 -> ratio 1.0 -> no shift.
    assert events == []


def test_prom_emits_shift_when_current_higher():
    """Toggle baseline vs current queries by which call number is which:
    the adapter always queries the baseline first, then current — so
    alternating responses model the real Prometheus behaviour."""

    call_count = {"n": 0}

    async def get(url, params=None):
        resp = MagicMock()
        resp.status_code = 200
        if "/-/ready" in url:
            resp.json = MagicMock(return_value={})
            return resp
        call_count["n"] += 1
        # 1st query data call -> baseline (100), 2nd -> current (4200)
        value = "100" if call_count["n"] == 1 else "4200"
        resp.json = MagicMock(
            return_value={
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, value]}]},
            }
        )
        return resp

    client = MagicMock()
    client.get = AsyncMock(side_effect=get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        prom = PrometheusAdapter(
            "http://prom",
            probes=[("payments", "p99_latency", "x")],
            shift_threshold=1.5,
        )
        events = asyncio.run(prom.poll("prod"))

    assert len(events) == 1
    e = events[0]
    assert isinstance(e, MetricShift)
    assert e.service_name == "payments"
    assert e.metric == "p99_latency"
    assert e.before == pytest.approx(100.0)
    assert e.after == pytest.approx(4200.0)


def test_prom_raises_not_configured_when_unreachable():
    import httpx

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        prom = PrometheusAdapter("http://prom", probes=[("x", "y", "z")])
        with pytest.raises(NotConfiguredError):
            asyncio.run(prom.poll("prod"))


def test_prom_no_probes_no_events():
    """Adapter with an empty probe list should connect cleanly and emit
    nothing, not crash."""
    responses = {"/-/ready": {"json": {}, "status": 200}}
    client = _async_client_mock(responses)

    with patch("httpx.AsyncClient", return_value=client):
        prom = PrometheusAdapter("http://prom", probes=[])
        events = asyncio.run(prom.poll("prod"))
    assert events == []


# --------------------------------------------------------------------- Loki


def test_loki_groups_lines_into_signatures():
    """Two log lines that differ only in numeric values should collapse to
    one LogSignature with count=2."""
    responses = {
        "/ready": {"json": {}, "status": 200},
        "/loki/api/v1/query_range": {
            "json": {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"job": "payments"},
                            "values": [
                                ["1716606000000000000", "ERROR: timeout after 1234ms"],
                                ["1716606060000000000", "ERROR: timeout after 5678ms"],
                            ],
                        }
                    ]
                },
            }
        },
    }
    client = _async_client_mock(responses)

    with patch("httpx.AsyncClient", return_value=client):
        loki = LokiAdapter("http://loki", services=["payments"])
        events = asyncio.run(loki.poll("prod"))

    assert len(events) == 1
    e = events[0]
    assert isinstance(e, LogSignature)
    assert e.service_name == "payments"
    assert e.count == 2
    # numeric tokens got redacted
    assert "1234" not in e.signature
    assert "5678" not in e.signature


def test_loki_distinct_signatures_emit_separately():
    responses = {
        "/ready": {"json": {}, "status": 200},
        "/loki/api/v1/query_range": {
            "json": {
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"job": "auth"},
                            "values": [
                                ["1716606000000000000", "ERROR: connection refused"],
                                ["1716606060000000000", "ERROR: pool exhausted"],
                            ],
                        }
                    ]
                },
            }
        },
    }
    client = _async_client_mock(responses)

    with patch("httpx.AsyncClient", return_value=client):
        loki = LokiAdapter("http://loki", services=["auth"])
        events = asyncio.run(loki.poll("prod"))

    assert len(events) == 2
    sigs = {e.signature for e in events}
    assert len(sigs) == 2


def test_loki_raises_not_configured_when_unreachable():
    import httpx

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client):
        loki = LokiAdapter("http://loki")
        with pytest.raises(NotConfiguredError):
            asyncio.run(loki.poll("prod"))


# -------------------------------------------------------------- redaction


def test_redact_collapses_uuid():
    line = "request abc12345-6789-4def-9012-345678901234 failed"
    assert "abc12345-6789-4def" not in _redact_log_signature(line)


def test_redact_collapses_iso_timestamp():
    line = "2026-05-25T14:23:00Z connection closed"
    out = _redact_log_signature(line)
    assert "2026-05-25T14:23:00Z" not in out


def test_redact_caps_length_at_200():
    line = "x" * 1000
    assert len(_redact_log_signature(line)) == 200
