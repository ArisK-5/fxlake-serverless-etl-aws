"""Structured JSON logging for FXLake Lambda functions.

Produces one JSON object per log line, compatible with CloudWatch Logs Insights.
Usage::

    from common.logging import configure_logger

    logger = configure_logger("frankfurter")
    logger.info("Ingestion complete", extra={"records": 42, "duration_ms": 120})

CloudWatch Logs Insights query example::

    fields @timestamp, service, message, records
    | filter level = "ERROR"
    | sort @timestamp desc
"""

import json
import logging
import time
from typing import Any


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Standard fields: ``timestamp``, ``level``, ``service``, ``message``.
    Any keys passed via ``extra={}`` are merged at the top level.
    The ``request_id`` is included when set on the logger via
    :func:`configure_logger` or :func:`inject_request_id`.
    """

    _RESERVED = logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        log_dict: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "service": self._service,
            "message": record.getMessage(),
        }

        # Inject request_id if available (set by inject_request_id)
        request_id = getattr(record, "request_id", None)
        if request_id:
            log_dict["request_id"] = request_id

        # Merge caller-supplied extra fields (skip stdlib internal attrs)
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and key not in ("request_id",):
                log_dict[key] = value

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_dict["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_dict, default=str)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """ISO-8601 with milliseconds."""
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z"


class RequestIdFilter(logging.Filter):
    """Attach ``request_id`` to every log record."""

    def __init__(self, request_id: str) -> None:
        super().__init__()
        self.request_id = request_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = self.request_id  # type: ignore[attr-defined]
        return True


def configure_logger(service: str, level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger with a JSON formatter and return it.

    Safe to call multiple times — replaces the existing formatter rather
    than adding duplicate handlers (Lambda reuses the root logger's
    StreamHandler across invocations).

    Args:
        service: Logical service name embedded in every log line.
        level: Logging level (default ``INFO``).

    Returns:
        The configured root logger.
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    formatter = _JSONFormatter(service)

    # Lambda runtime pre-installs a handler on the root logger.
    # Replace its formatter instead of adding a new handler.
    if logger.handlers:
        for handler in logger.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def inject_request_id(logger: logging.Logger, context: Any) -> None:
    """Extract the AWS request ID from a Lambda context and attach it as a filter.

    Args:
        logger: Logger returned by :func:`configure_logger`.
        context: Lambda context object (has ``aws_request_id`` attribute).
    """
    request_id = getattr(context, "aws_request_id", None)
    if not request_id:
        return

    # Remove any existing RequestIdFilter to avoid duplicates across warm starts
    for f in list(logger.filters):
        if isinstance(f, RequestIdFilter):
            logger.removeFilter(f)

    logger.addFilter(RequestIdFilter(request_id))


class Timer:
    """Context manager that measures elapsed monotonic time in milliseconds.

    Usage::

        with Timer() as t:
            do_work()
        logger.info("Done", extra={"duration_ms": t.duration_ms})
    """

    def __init__(self) -> None:
        self.start_ns: int = 0
        self.end_ns: int = 0

    def __enter__(self) -> "Timer":
        self.start_ns = time.monotonic_ns()
        return self

    def __exit__(self, *_: Any) -> None:
        self.end_ns = time.monotonic_ns()

    @property
    def duration_ms(self) -> int:
        """Elapsed time in whole milliseconds."""
        return (self.end_ns - self.start_ns) // 1_000_000
