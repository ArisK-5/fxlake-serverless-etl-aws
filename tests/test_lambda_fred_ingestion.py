"""Tests for FREDHandler and its lambda_handler entry point."""
import json
import os
from datetime import date

import lambda_fred_ingestion as fred_module
import pytest
import requests
import responses
from lambda_fred_ingestion import FREDHandler

# Must match TEST_STATE_TABLE in conftest.py — both reference the same moto table name.
TEST_STATE_TABLE = "test-state-table"

FRED_BASE_URL = os.environ["FRED_BASE_URL"]
FRED_API_URL = f"{FRED_BASE_URL}/series/observations"
FRED_SERIES = os.environ["FRED_SERIES"]  # "UNRATE"

# Minimal FRED API response with two monthly observations
SAMPLE_FRED_RESPONSE = {
    "realtime_start": "2024-01-31",
    "realtime_end": "2024-01-31",
    "observation_start": "2024-01-01",
    "observation_end": "2024-01-31",
    "units": "pc1",
    "output_type": 1,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "asc",
    "count": 1,
    "offset": 0,
    "limit": 100000,
    "observations": [
        {
            "realtime_start": "2024-01-31",
            "realtime_end": "2024-01-31",
            "date": "2024-01-01",
            "value": "3.7",
        },
        {
            "realtime_start": "2024-01-31",
            "realtime_end": "2024-01-31",
            "date": "2024-02-01",
            "value": "3.9",
        },
    ],
}

EXPECTED_PARSED = {
    "source": "fred",
    "series_id": FRED_SERIES,
    "observations": {"2024-01-01": 3.7, "2024-02-01": 3.9},
}


# ---------------------------------------------------------------------------
# FREDHandler._parse_fred_response
# ---------------------------------------------------------------------------
class TestParseFredResponse:
    def test_normalises_to_fxlake_format(self):
        handler = FREDHandler()
        result = handler._parse_fred_response(SAMPLE_FRED_RESPONSE)

        assert result == EXPECTED_PARSED

    def test_drops_missing_value_sentinels(self):
        """FRED '.' values (missing/unreleased data) must be dropped."""
        raw = {
            "observations": [
                {"date": "2024-01-01", "value": "."},
                {"date": "2024-02-01", "value": "3.9"},
            ]
        }
        handler = FREDHandler()
        result = handler._parse_fred_response(raw)

        assert "2024-01-01" not in result["observations"]
        assert result["observations"]["2024-02-01"] == 3.9

    def test_values_are_floats(self):
        """Observation values must be float, not strings."""
        handler = FREDHandler()
        result = handler._parse_fred_response(SAMPLE_FRED_RESPONSE)

        for value in result["observations"].values():
            assert isinstance(value, float)

    def test_raises_when_observations_key_missing(self):
        handler = FREDHandler()

        with pytest.raises(ValueError, match="missing 'observations' key"):
            handler._parse_fred_response({"realtime_start": "2024-01-01"})

    def test_raises_when_all_values_missing(self):
        """All-sentinel responses must raise ValueError — prevents state advancing."""
        raw = {
            "observations": [
                {"date": "2024-01-01", "value": "."},
                {"date": "2024-02-01", "value": "."},
            ]
        }
        handler = FREDHandler()

        with pytest.raises(ValueError, match="no valid observations"):
            handler._parse_fred_response(raw)

    def test_raises_on_malformed_observation(self):
        """Observation objects missing 'date' or 'value' must raise KeyError."""
        raw = {"observations": [{"date": "2024-01-01"}]}  # missing 'value'
        handler = FREDHandler()

        with pytest.raises(KeyError):
            handler._parse_fred_response(raw)

    def test_empty_observations_list_returns_none(self):
        raw = {"observations": []}
        handler = FREDHandler()

        result = handler._parse_fred_response(raw)
        assert result is None

    def test_series_id_from_env(self):
        """series_id in output must match FRED_SERIES env var."""
        handler = FREDHandler()
        result = handler._parse_fred_response(SAMPLE_FRED_RESPONSE)

        assert result["series_id"] == FRED_SERIES


# ---------------------------------------------------------------------------
# FREDHandler.fetch_data(start_date, end_date)
# ---------------------------------------------------------------------------
class TestFetchData:
    @responses.activate
    def test_successful_fetch_returns_parsed_observations(self):
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)
        handler = FREDHandler()

        result = handler.fetch_data("2024-01-01", "2024-01-31")

        assert result == EXPECTED_PARSED

    @responses.activate
    def test_sends_correct_query_params(self):
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)
        handler = FREDHandler()

        handler.fetch_data("2024-01-01", "2024-01-31")

        request_url = responses.calls[0].request.url
        assert f"series_id={FRED_SERIES}" in request_url
        assert "observation_start=2024-01-01" in request_url
        assert "observation_end=2024-01-31" in request_url
        assert "file_type=json" in request_url
        assert "api_key=test_fred_api_key" in request_url

    @responses.activate
    def test_api_timeout_raises(self):
        responses.add(
            responses.GET, FRED_API_URL, body=requests.exceptions.Timeout("timed out")
        )

        with pytest.raises(requests.exceptions.Timeout):
            FREDHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_http_error_raises(self):
        responses.add(
            responses.GET, FRED_API_URL, json={"error_message": "bad key"}, status=400
        )

        with pytest.raises(requests.exceptions.HTTPError):
            FREDHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_non_json_response_raises(self):
        responses.add(responses.GET, FRED_API_URL, body="<html>error</html>", status=200)

        with pytest.raises(requests.exceptions.JSONDecodeError):
            FREDHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_connection_error_raises(self):
        responses.add(
            responses.GET,
            FRED_API_URL,
            body=requests.exceptions.ConnectionError("no route"),
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            FREDHandler().fetch_data("2024-01-01", "2024-01-31")

    @responses.activate
    def test_malformed_response_raises_value_error(self):
        responses.add(responses.GET, FRED_API_URL, json={"error": "no data"}, status=200)

        with pytest.raises(ValueError, match="missing 'observations' key"):
            FREDHandler().fetch_data("2024-01-01", "2024-01-31")


# ---------------------------------------------------------------------------
# FREDHandler.make_filename
# ---------------------------------------------------------------------------
class TestMakeFilename:
    def test_filename_format_lowercases_series(self):
        handler = FREDHandler()
        expected = f"fred_{FRED_SERIES.lower()}_2024-01-01_to_2024-01-31.json"
        assert handler.make_filename("2024-01-01", "2024-01-31") == expected

    def test_filename_starts_with_fred_prefix(self):
        """Glue transform dispatches FRED files by 'fred_' prefix — must be present."""
        handler = FREDHandler()
        filename = handler.make_filename("2024-01-01", "2024-01-31")
        assert filename.startswith("fred_")


# ---------------------------------------------------------------------------
# lambda_handler() — static mode
# ---------------------------------------------------------------------------
class TestLambdaHandlerStatic:
    @responses.activate
    def test_end_to_end_success(self, s3_mock):
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        result = fred_module.lambda_handler({}, None)

        assert result["status"] == "ok"
        expected_key = f"fred_{FRED_SERIES.lower()}_2024-01-01_to_2024-01-31.json"
        assert result["key"] == expected_key
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"
        assert result["source"] == "fred"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body["source"] == "fred"
        assert body["series_id"] == FRED_SERIES
        assert "observations" in body

    @responses.activate
    def test_raises_on_api_failure(self, s3_mock):
        responses.add(
            responses.GET, FRED_API_URL, json={"error_message": "bad key"}, status=400
        )

        with pytest.raises(requests.exceptions.HTTPError):
            fred_module.lambda_handler({}, None)


# ---------------------------------------------------------------------------
# lambda_handler() — incremental mode (STATE_TABLE set via env)
# ---------------------------------------------------------------------------
class TestLambdaHandlerIncremental:
    @responses.activate
    def test_incremental_fetch_uses_fred_source_key(self, aws_mock, monkeypatch):
        """DynamoDB state is keyed by source='fred', separate from other sources."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        result = fred_module.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"
        assert result["source"] == "fred"

    def test_update_state_action(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        result = fred_module.lambda_handler(
            {"action": "update_state", "end_date": "2024-01-31"}, None
        )

        assert result["status"] == "state_updated"
        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "fred"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    @responses.activate
    def test_fred_reads_only_its_own_dynamodb_row(self, aws_mock, monkeypatch):
        """FRED handler must only read source='fred' row, not 'frankfurter' or 'ecb'."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-05"},
            },
        )
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": "2023-12-31"},
            },
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        result = fred_module.lambda_handler({}, None)

        # FRED must have read source="fred" (last_processed=2023-12-31), not "frankfurter"
        assert result["start_date"] == "2024-01-01"

    @responses.activate
    def test_no_new_data_returns_correct_status(self, aws_mock, monkeypatch):
        """When already caught up, returns no_new_data with end_date for Step Functions."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        today = date.today().isoformat()
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": today},
            },
        )

        result = fred_module.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert "end_date" in result

    @responses.activate
    def test_empty_api_response_returns_no_new_data(self, aws_mock, monkeypatch):
        """Monthly series with no new release returns no_new_data instead of raising."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": "2024-04-30"},
            },
        )
        empty_response = {"observations": []}
        responses.add(responses.GET, FRED_API_URL, json=empty_response, status=200)

        result = fred_module.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert result["source"] == "fred"
