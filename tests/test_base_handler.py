"""Tests for BaseIngestionHandler — shared orchestration logic used by all source handlers."""
import json
import os
from datetime import date
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from common.base import BaseIngestionHandler
from common.schema_validation import SchemaValidationError

# Must match TEST_STATE_TABLE in conftest.py — both reference the same moto table name.
TEST_STATE_TABLE = "test-state-table"

SAMPLE_DATA = {
    "base": "EUR",
    "rates": {"2024-01-02": {"USD": 1.1023}},
}


# ---------------------------------------------------------------------------
# Minimal concrete handler — no HTTP calls, controllable fetch failures
# ---------------------------------------------------------------------------
class ConcreteHandler(BaseIngestionHandler):
    """Test double that returns fixed data without making HTTP requests."""

    def __init__(self, fail_fetch: Exception | None = None) -> None:
        super().__init__(
            source_name="test",
            raw_bucket=os.environ["RAW_BUCKET"],
            state_table=os.getenv("STATE_TABLE"),
            start_date=os.environ["START_DATE"],
            end_date=os.environ["END_DATE"],
        )
        self._fail_fetch = fail_fetch

    def fetch_data(self, start_date: str, end_date: str) -> dict:
        if self._fail_fetch is not None:
            raise self._fail_fetch
        return dict(SAMPLE_DATA)

    def make_filename(self, start_date: str, end_date: str) -> str:
        return f"test_{start_date}_to_{end_date}.json"


# ---------------------------------------------------------------------------
# save_to_s3
# ---------------------------------------------------------------------------
class TestSaveToS3:
    def test_saves_json_with_correct_key(self, s3_mock):
        handler = ConcreteHandler()
        filename = handler.save_to_s3(SAMPLE_DATA, "test_2024-01-01_to_2024-01-31.json")

        assert filename == "test_2024-01-01_to_2024-01-31.json"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=filename)
        body = json.loads(obj["Body"].read())
        assert body == SAMPLE_DATA
        assert obj["ContentType"] == "application/json"
        assert obj["Metadata"]["source"] == "test"

    def test_s3_write_failure_raises(self, s3_mock):
        handler = ConcreteHandler()
        handler._s3 = MagicMock()
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Denied"}}
        handler._s3.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(ClientError):
            handler.save_to_s3(SAMPLE_DATA, "test.json")


# ---------------------------------------------------------------------------
# get_last_processed
# ---------------------------------------------------------------------------
class TestGetLastProcessed:
    def test_returns_start_date_when_no_entry(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()

        assert handler.get_last_processed() == "2024-01-01"  # START_DATE from conftest

    def test_returns_stored_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-15"},
            },
        )
        handler = ConcreteHandler()

        assert handler.get_last_processed() == "2024-01-15"

    @pytest.mark.parametrize(
        "code",
        [
            "ProvisionedThroughputExceededException",
            "RequestLimitExceeded",
            "ThrottlingException",
            "InternalServerError",
        ],
    )
    def test_falls_back_on_transient_dynamodb_error(self, aws_mock, monkeypatch, code):
        """All four allowlisted codes fall back to start_date."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        error_response = {"Error": {"Code": code, "Message": "Transient"}}
        handler._dynamodb = MagicMock()
        handler._dynamodb.get_item.side_effect = ClientError(error_response, "GetItem")

        assert handler.get_last_processed() == "2024-01-01"

    @pytest.mark.parametrize("code", ["ResourceNotFoundException", "AccessDeniedException"])
    def test_re_raises_on_infrastructure_error(self, aws_mock, monkeypatch, code):
        """Infrastructure errors must not be silently swallowed."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        error_response = {"Error": {"Code": code, "Message": "Misconfigured"}}
        handler._dynamodb = MagicMock()
        handler._dynamodb.get_item.side_effect = ClientError(error_response, "GetItem")

        with pytest.raises(ClientError):
            handler.get_last_processed()


# ---------------------------------------------------------------------------
# update_last_processed
# ---------------------------------------------------------------------------
class TestUpdateLastProcessed:
    def test_writes_date_to_dynamodb(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        handler.update_last_processed("2024-01-31")

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "test"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    def test_raises_on_dynamodb_client_error(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        error_response = {
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}
        }
        handler._dynamodb = MagicMock()
        handler._dynamodb.put_item.side_effect = ClientError(error_response, "PutItem")

        with pytest.raises(ClientError):
            handler.update_last_processed("2024-01-31")


# ---------------------------------------------------------------------------
# _static_ingest
# ---------------------------------------------------------------------------
class TestStaticIngest:
    def test_fetches_full_range_and_saves(self, s3_mock):
        handler = ConcreteHandler()
        result = handler._static_ingest()

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"
        assert result["source"] == "test"
        assert result["key"] == "test_2024-01-01_to_2024-01-31.json"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body == SAMPLE_DATA


# ---------------------------------------------------------------------------
# _incremental_ingest — orchestration (saga pattern)
# ---------------------------------------------------------------------------
class TestIncrementalIngest:
    def test_no_new_data_when_caught_up(self, aws_mock, monkeypatch):
        today = date.today().isoformat()
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": today},
            },
        )
        handler = ConcreteHandler()

        result = handler._incremental_ingest()

        assert result["status"] == "no_new_data"
        assert result["last_processed_date"] == today
        # end_date always present so Step Functions Parallel-Ingestion can read Payload.end_date
        assert "end_date" in result

    def test_incremental_fetch_does_not_update_state(self, aws_mock, monkeypatch):
        """Incremental ingest returns end_date but does NOT write DynamoDB.
        State commit is deferred to Lambda-Update-State in Step Functions (post-Glue)."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        handler = ConcreteHandler()
        result = handler._incremental_ingest()

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"

        # DynamoDB must NOT be updated
        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "test"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"

    def test_first_run_uses_start_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        result = handler._incremental_ingest()

        # No DynamoDB entry → defaults to START_DATE (2024-01-01), fetch starts 2024-01-02
        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-02"

    def test_no_new_data_when_last_processed_equals_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-31"},  # == END_DATE
            },
        )
        handler = ConcreteHandler()

        result = handler._incremental_ingest()

        assert result["status"] == "no_new_data"

    def test_state_not_updated_on_fetch_failure(self, aws_mock, monkeypatch):
        """DynamoDB state must remain unchanged when fetch_data raises."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        handler = ConcreteHandler(fail_fetch=RuntimeError("API down"))

        with pytest.raises(RuntimeError):
            handler._incremental_ingest()

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "test"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"

    def test_state_not_updated_on_s3_write_failure(self, aws_mock, monkeypatch):
        """DynamoDB state must remain unchanged when save_to_s3 raises."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Denied"}}
        handler = ConcreteHandler()
        handler._s3 = MagicMock()
        handler._s3.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(ClientError):
            handler._incremental_ingest()

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "test"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"


# ---------------------------------------------------------------------------
# _handle_update_state
# ---------------------------------------------------------------------------
class TestHandleUpdateState:
    def test_writes_end_date_to_dynamodb(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()

        result = handler._handle_update_state({"action": "update_state", "end_date": "2024-01-31"})

        assert result == {"status": "state_updated", "last_processed_date": "2024-01-31"}
        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "test"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    def test_raises_when_state_table_not_configured(self):
        handler = ConcreteHandler()  # STATE_TABLE not in env → state_table=None

        with pytest.raises(RuntimeError, match="STATE_TABLE"):
            handler._handle_update_state({"action": "update_state", "end_date": "2024-01-31"})

    def test_raises_on_missing_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()

        with pytest.raises(ValueError, match="end_date"):
            handler._handle_update_state({"action": "update_state"})

    def test_raises_on_empty_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()

        with pytest.raises(ValueError, match="end_date"):
            handler._handle_update_state({"action": "update_state", "end_date": ""})

    def test_raises_on_dynamodb_error(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        error_response = {
            "Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}
        }
        handler._dynamodb = MagicMock()
        handler._dynamodb.put_item.side_effect = ClientError(error_response, "PutItem")

        with pytest.raises(ClientError):
            handler._handle_update_state({"action": "update_state", "end_date": "2024-01-31"})


# ---------------------------------------------------------------------------
# run() — top-level routing
# ---------------------------------------------------------------------------
class TestRun:
    def test_routes_update_state_action(self, aws_mock, monkeypatch):
        """event with action='update_state' must reach _handle_update_state."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()

        result = handler.run({"action": "update_state", "end_date": "2024-01-15"}, None)

        assert result["status"] == "state_updated"
        assert result["last_processed_date"] == "2024-01-15"

    def test_routes_to_incremental_when_state_table_set(self, aws_mock, monkeypatch):
        """Empty event with STATE_TABLE set must reach _incremental_ingest."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "test"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        handler = ConcreteHandler()

        result = handler.run({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"

    def test_routes_to_static_when_no_state_table(self, s3_mock):
        """Empty event without STATE_TABLE must reach _static_ingest."""
        handler = ConcreteHandler()  # STATE_TABLE not set → state_table=None

        result = handler.run({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"


# ---------------------------------------------------------------------------
# _incremental_ingest — fetch-end date capping
# ---------------------------------------------------------------------------
class TestIncrementalIngestDateCapping:
    def test_fetch_end_is_capped_at_today_when_end_date_is_future(
        self, aws_mock, monkeypatch
    ):
        """fetch_end = min(today, END_DATE) — must not request future dates from APIs."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        monkeypatch.setenv("END_DATE", "2099-12-31")
        handler = ConcreteHandler()

        result = handler._incremental_ingest()

        today = date.today().isoformat()
        assert result["end_date"] == today
        assert result["status"] == "ok"

    def test_fetch_end_uses_end_date_when_end_date_is_past(self, aws_mock, monkeypatch):
        """When END_DATE is in the past fetch_end should equal END_DATE, not today."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        # END_DATE defaults to 2024-01-31 from conftest; today is well past that
        handler = ConcreteHandler()

        result = handler._incremental_ingest()

        assert result["end_date"] == "2024-01-31"


# ---------------------------------------------------------------------------
# _perform_ingest and backfill routing
# ---------------------------------------------------------------------------
class TestPerformIngest:
    """Test the shared ingestion logic for all modes (backfill, incremental, static)."""

    @pytest.fixture
    def backfill_dates(self):
        """Standard backfill date range for testing."""
        return "2023-01-01", "2023-06-30"

    def test_backfill_fetches_explicit_date_range(self, s3_mock, backfill_dates):
        """Backfill uses event dates, not DynamoDB or env START_DATE/END_DATE."""
        start, end = backfill_dates
        handler = ConcreteHandler()
        result = handler._perform_ingest(start, end, mode="backfill")

        assert result["status"] == "ok"
        assert result["start_date"] == start
        assert result["end_date"] == end
        assert result["mode"] == "backfill"
        assert result["key"] == f"test_{start}_to_{end}.json"

    def test_backfill_saves_data_to_s3(self, s3_mock, backfill_dates):
        handler = ConcreteHandler()
        start, end = backfill_dates
        result = handler._perform_ingest(start, end, mode="backfill")

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body == SAMPLE_DATA

    def test_backfill_does_not_touch_dynamodb(self, aws_mock, monkeypatch, backfill_dates):
        """Backfill must NOT read or write DynamoDB state."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        handler._dynamodb = MagicMock()
        start, end = backfill_dates

        handler._perform_ingest(start, end, mode="backfill")

        handler._dynamodb.get_item.assert_not_called()
        handler._dynamodb.put_item.assert_not_called()

    def test_backfill_works_without_state_table(self, s3_mock, backfill_dates):
        """Backfill works even when STATE_TABLE is not configured."""
        handler = ConcreteHandler()  # no STATE_TABLE → state_table=None
        start, end = backfill_dates
        result = handler._perform_ingest(start, end, mode="backfill")

        assert result["status"] == "ok"

    def test_static_mode_includes_mode_key_only_for_backfill(self, s3_mock):
        """Only backfill mode should include 'mode' key in response."""
        handler = ConcreteHandler()
        backfill_result = handler._perform_ingest("2023-01-01", "2023-06-30", mode="backfill")
        static_result = handler._perform_ingest("2023-01-01", "2023-06-30", mode="static")

        assert "mode" in backfill_result
        assert "mode" not in static_result

    def test_propagates_fetch_failure(self, s3_mock):
        handler = ConcreteHandler(fail_fetch=RuntimeError("API down"))

        with pytest.raises(RuntimeError, match="API down"):
            handler._perform_ingest("2023-01-01", "2023-06-30", mode="backfill")

    def test_propagates_s3_write_failure(self, s3_mock):
        handler = ConcreteHandler()
        handler._s3 = MagicMock()
        error_response = {"Error": {"Code": "NoSuchBucket", "Message": "Not found"}}
        handler._s3.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(ClientError, match="NoSuchBucket"):
            handler._perform_ingest("2023-01-01", "2023-06-30", mode="backfill")


# ---------------------------------------------------------------------------
# run() — backfill routing
# ---------------------------------------------------------------------------
class TestRunBackfill:
    def test_routes_backfill_mode(self, s3_mock):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-01-01", "end_date": "2023-06-30"}

        result = handler.run(event, None)

        assert result["status"] == "ok"
        assert result["mode"] == "backfill"
        assert result["start_date"] == "2023-01-01"
        assert result["end_date"] == "2023-06-30"

    def test_backfill_requires_start_date(self, s3_mock):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "end_date": "2023-06-30"}

        with pytest.raises(ValueError, match="start_date"):
            handler.run(event, None)

    def test_backfill_requires_end_date(self, s3_mock):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-01-01"}

        with pytest.raises(ValueError, match="end_date"):
            handler.run(event, None)

    def test_backfill_takes_priority_over_incremental(self, aws_mock, monkeypatch):
        """mode=backfill should route to backfill even when STATE_TABLE is set."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-01-01", "end_date": "2023-06-30"}

        result = handler.run(event, None)

        assert result["mode"] == "backfill"

    def test_update_state_still_takes_priority_over_backfill(self, aws_mock, monkeypatch):
        """action=update_state must still work even if mode=backfill is also present."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        handler = ConcreteHandler()
        event = {
            "action": "update_state",
            "mode": "backfill",
            "end_date": "2024-01-31",
        }

        result = handler.run(event, None)

        assert result["status"] == "state_updated"

    @pytest.mark.parametrize("bad_date", ["  ", "\t", "\n", ""])
    def test_backfill_rejects_whitespace_only_start_date(self, s3_mock, bad_date):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": bad_date, "end_date": "2023-06-30"}

        with pytest.raises(ValueError, match="start_date"):
            handler.run(event, None)

    @pytest.mark.parametrize("bad_date", ["  ", "\t", "\n", ""])
    def test_backfill_rejects_whitespace_only_end_date(self, s3_mock, bad_date):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-01-01", "end_date": bad_date}

        with pytest.raises(ValueError, match="end_date"):
            handler.run(event, None)

    @pytest.mark.parametrize(
        "bad_date", ["invalid-date", "2023-13-45", "2023/01/01", "01-01-2023"]
    )
    def test_backfill_rejects_invalid_date_format(self, s3_mock, bad_date):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": bad_date, "end_date": "2023-06-30"}

        with pytest.raises(ValueError, match="date format"):
            handler.run(event, None)

    def test_backfill_rejects_reversed_dates(self, s3_mock):
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-12-31", "end_date": "2023-01-01"}

        with pytest.raises(ValueError, match="start_date.*<=.*end_date"):
            handler.run(event, None)

    def test_backfill_accepts_same_start_and_end_date(self, s3_mock):
        """Single-day backfill should work."""
        handler = ConcreteHandler()
        event = {"mode": "backfill", "start_date": "2023-01-01", "end_date": "2023-01-01"}

        result = handler.run(event, None)

        assert result["status"] == "ok"
        assert result["mode"] == "backfill"


# ---------------------------------------------------------------------------
# Schema validation in _perform_ingest
# ---------------------------------------------------------------------------
class InvalidDataHandler(BaseIngestionHandler):
    """Returns data that fails schema validation."""

    def __init__(self) -> None:
        super().__init__(
            source_name="test",
            raw_bucket=os.environ["RAW_BUCKET"],
            state_table=os.getenv("STATE_TABLE"),
            start_date=os.environ["START_DATE"],
            end_date=os.environ["END_DATE"],
        )

    def fetch_data(self, start_date: str, end_date: str) -> dict:
        return {"bad": "data"}

    def make_filename(self, start_date: str, end_date: str) -> str:
        return f"test_{start_date}_to_{end_date}.json"


class TestSchemaValidationIntegration:
    """Validate that _perform_ingest enforces schema contracts."""

    def test_valid_data_passes_validation(self, s3_mock):
        handler = ConcreteHandler()
        result = handler._perform_ingest("2024-01-01", "2024-01-31", mode="static")
        assert result["status"] == "ok"

    def test_invalid_data_raises_schema_error(self, s3_mock):
        handler = InvalidDataHandler()
        with pytest.raises(SchemaValidationError, match="Cannot detect schema"):
            handler._perform_ingest("2024-01-01", "2024-01-31", mode="static")

    def test_invalid_data_does_not_save_to_s3(self, s3_mock):
        handler = InvalidDataHandler()
        with pytest.raises(SchemaValidationError):
            handler._perform_ingest("2024-01-01", "2024-01-31", mode="static")

        result = s3_mock.list_objects_v2(Bucket="test-raw-bucket")
        assert result.get("KeyCount", 0) == 0

    def test_kill_switch_disables_validation(self, s3_mock, monkeypatch):
        monkeypatch.setenv("SCHEMA_VALIDATION_ENABLED", "false")
        handler = InvalidDataHandler()
        result = handler._perform_ingest("2024-01-01", "2024-01-31", mode="static")
        assert result["status"] == "ok"

    def test_validation_runs_before_s3_save(self, s3_mock):
        handler = InvalidDataHandler()
        handler._s3 = MagicMock()
        with pytest.raises(SchemaValidationError):
            handler._perform_ingest("2024-01-01", "2024-01-31", mode="static")
        handler._s3.put_object.assert_not_called()
