"""ASIL infrastructure adapters.

Phase 3 surface:
  - models: typed runtime events (Service, Deployment, MetricShift,
    LogSignature, Incident)
  - postmortem: load a YAML postmortem and ingest its timeline into the
    graph as runtime nodes + provisional edges
  - adapters: InfraAdapter protocol + FileAdapter for YAML/JSON event files,
    K8s/Prometheus/Loki stubs for future live infrastructure integration

Why postmortem-first? Causality (Phase 4) needs historical event sequences
to reason over; postmortems are the cleanest possible source of "what
happened at T, then what happened at T+N." Live adapters become essential
when running ASIL against a production stack — but their output lands in
the same graph shape, so callers don't change.
"""

from asil_infra.adapters import (
    FileAdapter,
    InfraAdapter,
    K8sAdapter,
    LokiAdapter,
    NotConfiguredError,
    PrometheusAdapter,
)
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
    "FileAdapter",
    "Incident",
    "InfraAdapter",
    "K8sAdapter",
    "LogSignature",
    "LokiAdapter",
    "MetricShift",
    "NotConfiguredError",
    "PostmortemFile",
    "PostmortemIngestStats",
    "PrometheusAdapter",
    "RuntimeEvent",
    "RuntimeKind",
    "Service",
    "ingest_postmortem",
    "load_postmortem",
]
