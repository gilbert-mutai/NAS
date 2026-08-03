"""Structured logging.

Emits one JSON object per line in deployed environments (Loki/OpenTelemetry
friendly) and human-readable output locally. Every record carries the service
name, timestamp, level and — inside a request — the request id, so a single
correlation id ties an API call to the work it triggered.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from nas.core.config import Settings

# Request-scoped context (request_id, client_ip, ...) is bound here by the
# middleware and merged into every log record emitted during that request.
_CONTEXT_KEYS = ("request_id", "client_ip", "api_key_id", "method", "path")


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route the stdlib logging module through it."""
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _add_service_name(settings.service_name),
    ]

    renderer: structlog.typing.Processor
    if settings.log_format == "json":
        shared_processors.append(structlog.processors.dict_tracebacks)
        renderer = structlog.processors.JSONRenderer()
    else:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Hand stdlib records (uvicorn, sqlalchemy, alembic) to the same formatter so
    # the output stream stays uniformly parseable.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; clear them so records propagate to root
    # instead of being printed twice in two different formats.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # The access log is produced by our own middleware with richer context.
    logging.getLogger("uvicorn.access").disabled = True


def _add_service_name(service_name: str) -> structlog.typing.Processor:
    def processor(
        _logger: Any, _method_name: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        event_dict["service"] = service_name
        return event_dict

    return processor


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_request_context(**values: Any) -> None:
    """Bind request-scoped values onto all subsequent log records in this task."""
    structlog.contextvars.bind_contextvars(**values)


def clear_request_context() -> None:
    structlog.contextvars.unbind_contextvars(*_CONTEXT_KEYS)
