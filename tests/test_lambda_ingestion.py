"""Tests for FrankfurterHandler and its lambda_handler entry point."""
import json
from datetime import date
from unittest.mock import MagicMock

import lambda_ingestion_function as ingestion
import pytest
import requests
import responses
from botocore.exceptions import ClientError
from lambda_ingestion_function import FrankfurterHandler

# Must match TEST_STATE_TABLE in conftest.py — both reference the same moto table name.
TEST_STATE_TABLE = "test-state-table"

SAMPLE_API_RESPONSE = {
    "base": "EUR",
    "start_date": "2024-01-01",
    "end_date": "2024-01-31",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8612},
    },
}

API_URL = "https://api.frankfurter.app/2024-01-01..2024-01-31"


# ---------------------------------------------------------------------------
# FrankfurterHandler.fetch_data(start_date, end_date)
# ---------------------------------------------------------------------------
class TestFetchData:
    @responses.activate
    def test_successful_fetch(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)
        handler = FrankfurterHandler()

        result = handler.fetch_data("2024-01-01", "2024-01-31")

        assert result["base"] == "EUR"
        assert "2024-01-02" in result["rates"]

    @responses.activate
    def test_base_currency_param_sent(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)
        handler = FrankfurterHandler()

        handler.fetch_data("2024-01-01", "2024-01-31")

        assert "base=EUR" in responses.calls[0].request.url

    @responses.activate
    def test_api_timeout(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.Timeout("Request timed out"),
        )

        with pytest.raises(requests.exceptions.Timeout):
            FrankfurterHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_connection_error(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.ConnectionError("DNS resolution failed"),
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            FrankfurterHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_non_json_response(self):
        responses.add(responses.GET, API_URL, body="<html>error</html>", status=200)

        with pytest.raises(requests.exceptions.JSONDecodeError):
            FrankfurterHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_http_error(self):
        responses.add(responses.GET, API_URL, json={"error": "not found"}, status=404)

        with pytest.raises(requests.exceptions.HTTPError):
            FrankfurterHandler().fetch_data("2024-01-01", "2024-01-31")


# ---------------------------------------------------------------------------
# FrankfurterHandler.make_filename(start_date, end_date)
# ---------------------------------------------------------------------------
class TestMakeFilename:
    def test_filename_includes_currency_and_dates(self):
        handler = FrankfurterHandler()
        filename = handler.make_filename("2024-01-01", "2024-01-31")

        assert filename == "exchange_rates_EUR_2024-01-01_to_2024-01-31.json"


# ---------------------------------------------------------------------------
# save_to_s3 — Frankfurter-specific: verify source metadata
# ---------------------------------------------------------------------------
class TestSaveToS3:
    def test_saves_json_with_correct_key_and_metadata(self, s3_mock):
        handler = FrankfurterHandler()
        filename = handler.make_filename("2024-01-01", "2024-01-31")

        result = handler.save_to_s3(SAMPLE_API_RESPONSE, filename)

        assert result == "exchange_rates_EUR_2024-01-01_to_2024-01-31.json"
        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=filename)
        body = json.loads(obj["Body"].read())
        assert body == SAMPLE_API_RESPONSE
        assert obj["ContentType"] == "application/json"
        assert obj["Metadata"]["source"] == "frankfurter"

    def test_s3_write_failure_raises(self, s3_mock):
        handler = FrankfurterHandler()
        handler._s3 = MagicMock()
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
        handler._s3.put_object.side_effect = ClientError(error_response, "PutObject")

        with pytest.raises(ClientError):
            handler.save_to_s3(SAMPLE_API_RESPONSE, "test.json")


# ---------------------------------------------------------------------------
# lambda_handler() — static mode (STATE_TABLE not set)
# ---------------------------------------------------------------------------
class TestLambdaHandlerStatic:
    @responses.activate
    def test_end_to_end_success(self, s3_mock):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["key"] == "exchange_rates_EUR_2024-01-01_to_2024-01-31.json"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"
        assert result["source"] == "frankfurter"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body["base"] == "EUR"
        assert len(body["rates"]) == 2

    @responses.activate
    def test_raises_on_api_failure(self, s3_mock):
        responses.add(responses.GET, API_URL, json={"error": "boom"}, status=500)

        with pytest.raises(requests.exceptions.HTTPError):
            ingestion.lambda_handler({}, None)


# ---------------------------------------------------------------------------
# lambda_handler() — incremental mode (STATE_TABLE set via env)
# ---------------------------------------------------------------------------
class TestLambdaHandlerIncremental:
    @responses.activate
    def test_no_new_data_when_caught_up(self, aws_mock, monkeypatch):
        today = date.today().isoformat()
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": today},
            },
        )

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert result["last_processed_date"] == today

    @responses.activate
    def test_incremental_fetch_returns_payload_without_updating_state(
        self, aws_mock, monkeypatch
    ):
        """Ingestion Lambda returns end_date in payload but does NOT write DynamoDB.
        State commit is deferred to Lambda-Update-State in Step Functions (post-Glue)."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        fetch_url = "https://api.frankfurter.app/2024-01-15..2024-01-31"
        responses.add(responses.GET, fetch_url, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"
        assert result["end_date"] == "2024-01-31"

        # DynamoDB must NOT be updated
        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"

    @responses.activate
    def test_first_run_uses_start_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        # No DynamoDB entry — should default to START_DATE="2024-01-01"
        fetch_url = "https://api.frankfurter.app/2024-01-02..2024-01-31"
        responses.add(responses.GET, fetch_url, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-02"

    @responses.activate
    def test_no_new_data_when_last_processed_equals_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-31"},  # == END_DATE
            },
        )

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert result["last_processed_date"] == "2024-01-31"


# ---------------------------------------------------------------------------
# lambda_handler() — update_state action (called by Step Functions post-Glue)
# ---------------------------------------------------------------------------
class TestLambdaHandlerUpdateState:
    def test_update_state_writes_dynamodb(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        result = ingestion.lambda_handler(
            {"action": "update_state", "end_date": "2024-01-31"}, None
        )

        assert result["status"] == "state_updated"
        assert result["last_processed_date"] == "2024-01-31"

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    def test_update_state_raises_when_dynamodb_not_configured(self):
        """STATE_TABLE not set → handler not in incremental mode → RuntimeError."""
        with pytest.raises(RuntimeError, match="STATE_TABLE"):
            ingestion.lambda_handler(
                {"action": "update_state", "end_date": "2024-01-31"}, None
            )

    def test_update_state_raises_on_missing_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        with pytest.raises(ValueError, match="end_date"):
            ingestion.lambda_handler({"action": "update_state"}, None)

    def test_update_state_raises_on_empty_end_date(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        with pytest.raises(ValueError, match="end_date"):
            ingestion.lambda_handler({"action": "update_state", "end_date": ""}, None)
