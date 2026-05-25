"""Integration smoke for the Prometheus / Loki adapters against the
docker-compose stack. Skipped automatically if the services are not up.

Run `make up` first if you want these to execute.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from asil_infra.adapters import (
    LokiAdapter,
    NotConfiguredError,
    PrometheusAdapter,
)

PROM_URL = "http://localhost:9090"
LOKI_URL = "http://localhost:3100"


def _service_up(url: str, path: str) -> bool:
    try:
        with httpx.Client(timeout=1.0) as client:
            r = client.get(f"{url}{path}")
            return r.status_code < 500
    except Exception:
        return False


pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not _service_up(PROM_URL, "/-/ready"),
    reason="Prometheus not reachable on localhost:9090 — run `make up`",
)
def test_prom_adapter_round_trips_against_live_stack():
    prom = PrometheusAdapter(
        PROM_URL,
        probes=[("self", "up", "up")],
        shift_threshold=1000.0,  # so stable `up` never triggers a shift
    )
    events = asyncio.run(prom.poll("prod"))
    # `up` is steady at 1.0 on a healthy cluster, so 0 shifts expected.
    assert events == []


@pytest.mark.skipif(
    not _service_up(LOKI_URL, "/ready"),
    reason="Loki not reachable on localhost:3100 — run `make up`",
)
def test_loki_adapter_round_trips_against_live_stack():
    loki = LokiAdapter(LOKI_URL, lookback_seconds=3600)
    try:
        events = asyncio.run(loki.poll("prod"))
    except NotConfiguredError:
        pytest.skip("Loki responded but isn't ready yet")
    # No services emitting logs in the demo stack -> empty list, no crash.
    assert isinstance(events, list)
