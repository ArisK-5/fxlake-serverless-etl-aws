"""Tests for lambda/common/logging.py — structured JSON logging."""

import json
import logging
from types import SimpleNamespace

from common.logging import (
    RequestIdFilter,
    Timer,
    _JSONFormatter,
    configure_logger,
    inject_request_id,
)

# ---------------------------------------------------------------------------
# _JSONFormatter
# ---------------------------------------------------------------------------


class TestJSONFormatter:
    def test_basic_output_is_valid_json(self) -> None:
        formatter = _JSONFormatter(service="test-svc")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["service"] == "test-svc"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_extra_fields_merged(self) -> None:
        formatter = _JSONFormatter(service="ingest")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="done",
            args=(),
            exc_info=None,
        )
        record.records = 42  # type: ignore[attr-defined]
        record.source = "ecb"  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["records"] == 42
        assert parsed["source"] == "ecb"

    def test_request_id_included(self) -> None:
        formatter = _JSONFormatter(service="svc")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.request_id = "abc-123"  # type: ignore[attr-defined]
        parsed = json.loads(formatter.format(record))
        assert parsed["request_id"] == "abc-123"

    def test_no_request_id_when_absent(self) -> None:
        formatter = _JSONFormatter(service="svc")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        assert "request_id" not in parsed

    def test_exception_info_included(self) -> None:
        formatter = _JSONFormatter(service="svc")
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="root",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="failed",
            args=(),
            exc_info=exc_info,
        )
        parsed = json.loads(formatter.format(record))
        assert "exception" in parsed
        assert "ValueError: boom" in parsed["exception"]

    def test_timestamp_format(self) -> None:
        formatter = _JSONFormatter(service="svc")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="t",
            args=(),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        ts = parsed["timestamp"]
        # Should match YYYY-MM-DDTHH:MM:SS.mmmZ
        assert ts.endswith("Z")
        assert "T" in ts

    def test_message_formatting_with_args(self) -> None:
        formatter = _JSONFormatter(service="svc")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="count=%d",
            args=(5,),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        assert parsed["message"] == "count=5"


# ---------------------------------------------------------------------------
# RequestIdFilter
# ---------------------------------------------------------------------------


class TestRequestIdFilter:
    def test_attaches_request_id(self) -> None:
        filt = RequestIdFilter("req-xyz")
        record = logging.LogRecord(
            name="root",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        result = filt.filter(record)
        assert result is True
        assert record.request_id == "req-xyz"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# configure_logger
# ---------------------------------------------------------------------------


class TestConfigureLogger:
    def test_returns_root_logger(self) -> None:
        logger = configure_logger("test")
        assert logger is logging.getLogger()

    def test_sets_json_formatter_on_existing_handler(self) -> None:
        logger = configure_logger("my-service")
        assert logger.handlers
        formatter = logger.handlers[0].formatter
        assert isinstance(formatter, _JSONFormatter)

    def test_idempotent_no_duplicate_handlers(self) -> None:
        handler_count_before = len(logging.getLogger().handlers)
        configure_logger("svc1")
        configure_logger("svc2")
        # Should not add extra handlers
        assert len(logging.getLogger().handlers) <= max(handler_count_before, 1)

    def test_sets_level(self) -> None:
        logger = configure_logger("svc", level=logging.DEBUG)
        assert logger.level == logging.DEBUG
        # Reset
        configure_logger("svc", level=logging.INFO)


# ---------------------------------------------------------------------------
# inject_request_id
# ---------------------------------------------------------------------------


class TestInjectRequestId:
    def test_adds_filter_from_lambda_context(self) -> None:
        logger = configure_logger("svc")
        # Remove any prior filters
        for f in list(logger.filters):
            if isinstance(f, RequestIdFilter):
                logger.removeFilter(f)

        ctx = SimpleNamespace(aws_request_id="req-abc-456")
        inject_request_id(logger, ctx)

        req_filters = [f for f in logger.filters if isinstance(f, RequestIdFilter)]
        assert len(req_filters) == 1
        assert req_filters[0].request_id == "req-abc-456"

    def test_replaces_existing_filter_on_warm_start(self) -> None:
        logger = configure_logger("svc")
        ctx1 = SimpleNamespace(aws_request_id="first")
        ctx2 = SimpleNamespace(aws_request_id="second")

        inject_request_id(logger, ctx1)
        inject_request_id(logger, ctx2)

        req_filters = [f for f in logger.filters if isinstance(f, RequestIdFilter)]
        assert len(req_filters) == 1
        assert req_filters[0].request_id == "second"

    def test_noop_when_no_request_id(self) -> None:
        logger = configure_logger("svc")
        before = len([f for f in logger.filters if isinstance(f, RequestIdFilter)])
        inject_request_id(logger, SimpleNamespace())
        after = len([f for f in logger.filters if isinstance(f, RequestIdFilter)])
        assert after == before

    def test_noop_for_none_context(self) -> None:
        logger = configure_logger("svc")
        inject_request_id(logger, None)  # Should not raise


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


class TestTimer:
    def test_measures_elapsed_time(self) -> None:
        import time

        with Timer() as t:
            time.sleep(0.05)
        assert t.duration_ms >= 40  # Allow some tolerance

    def test_zero_duration_for_instant(self) -> None:
        with Timer() as t:
            pass
        assert t.duration_ms >= 0
        assert t.duration_ms < 100  # Sanity check
