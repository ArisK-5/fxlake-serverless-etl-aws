"""Tests for BaseIngestionHandler — shared orchestration logic used by all source handlers."""
import json
import os
from datetime import date
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from common.base import BaseIngestionHandler

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
