"""Structured logging setup.

Every ASIL process should call `configure_logging()` once at startup before
emitting any logs. Logs are JSON-structured in non-dev environments so they
ingest cleanly into Loki.
"""

from __future__ import annotations

import logging
import sys

import structlog

from asil_core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.asil_log_level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.asil_env == "dev":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
