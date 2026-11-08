"""Log JSON terstruktur (structlog) ke stdout. Setiap baris memuat service, env, level, trace_id, dan correlation."""

from __future__ import annotations

import logging
import sys
from typing import Any

import orjson
import structlog


def _dumps(obj: Any, default: Any = None) -> str:
    return orjson.dumps(obj, default=default).decode()


def configure_logging(service: str, env: str, level: str = "INFO") -> None:
    static = {"service": service, "env": env}

    def add_static(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in static.items():
            event_dict.setdefault(key, value)
        return event_dict

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        add_static,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Log pustaka (uvicorn, sqlalchemy, nats) lewat stdlib juga dijadikan JSON yang sama.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(serializer=_dumps),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").setLevel("WARNING")


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)
