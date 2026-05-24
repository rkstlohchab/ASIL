"""ASIL infrastructure adapters.

Phase 3 surface:
  - models: typed runtime events (Service, Deployment, MetricShift,
    LogSignature, Incident)
  - postmortem: load a YAML postmortem and ingest its timeline into the
    graph as runtime nodes + provisional edges
  - (forthcoming) k8s_adapter / prom_adapter / loki_adapter — feed runtime
    events from live infrastructure into the same graph schema

Why postmortem-first? Causality (Phase 4) needs historical event sequences
to reason over; postmortems are the cleanest possible source of "what
happened at T, then what happened at T+N." Live adapters become essential
when running ASIL against a production stack — but their output lands in
the same graph shape, so callers don't change.
"""

from asil_infra.models import (
    Deployment,
    Incident,
    LogSignature,
    MetricShift,
    RuntimeEvent,
    RuntimeKind,
    Service,
)
from asil_infra.postmortem import (
    PostmortemFile,
    PostmortemIngestStats,
    ingest_postmortem,
    load_postmortem,
)

__version__ = "0.0.1"

__all__ = [
    "Deployment",
    "Incident",
    "LogSignature",
    "MetricShift",
    "PostmortemFile",
    "PostmortemIngestStats",
    "RuntimeEvent",
    "RuntimeKind",
    "Service",
    "ingest_postmortem",
    "load_postmortem",
]
