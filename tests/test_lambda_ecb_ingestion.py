"""Tests for ECBHandler and its lambda_handler entry point."""
import json
import os

import lambda_ecb_ingestion as ecb_module
import pytest
import requests
import responses
from lambda_ecb_ingestion import ECBHandler

# Must match TEST_STATE_TABLE in conftest.py — both reference the same moto table name.
TEST_STATE_TABLE = "test-state-table"

ECB_BASE_URL = os.environ["ECB_BASE_URL"]
ECB_API_URL = f"{ECB_BASE_URL}/EXR/D..EUR.SP00.A"

# Minimal ECB SDMX-JSON response with two currencies on one date
SAMPLE_ECB_RESPONSE = {
    "dataSets": [
        {
            "series": {
                "0:0:0:0:0": {"observations": {"0": [1.0953]}},
                "0:1:0:0:0": {"observations": {"0": [0.8671]}},
            }
        }
    ],
    "structure": {
        "dimensions": {
            "series": [
                {"id": "FREQ", "values": [{"id": "D"}]},
                {"id": "CURRENCY", "values": [{"id": "USD"}, {"id": "GBP"}]},
                {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
            ],
            "observation": [
                {"id": "TIME_PERIOD", "values": [{"id": "2024-01-02"}]}
            ],
        }
    },
}

EXPECTED_PARSED = {
    "base": "EUR",
    "source": "ecb",
    "rates": {"2024-01-02": {"USD": 1.0953, "GBP": 0.8671}},
}


# ---------------------------------------------------------------------------
# ECBHandler._parse_ecb_response
# ---------------------------------------------------------------------------
class TestParseEcbResponse:
    def test_normalises_to_fxlake_format(self):
        handler = ECBHandler()
        result = handler._parse_ecb_response(SAMPLE_ECB_RESPONSE)

        assert result == EXPECTED_PARSED

    def test_drops_dates_with_no_observations(self):
        """Dates present in structure but with no series observations are dropped."""
        raw = {
            "dataSets": [{"series": {"0:0:0:0:0": {"observations": {"0": [1.09]}}}}],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "D"}]},
                        {"id": "CURRENCY", "values": [{"id": "USD"}]},
                        {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                        {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                        {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
                    ],
                    "observation": [
                        # index 0 has data, index 1 does not
                        {
                            "id": "TIME_PERIOD",
                            "values": [{"id": "2024-01-02"}, {"id": "2024-01-06"}],
                        },
                    ],
                }
            },
        }
        handler = ECBHandler()
        result = handler._parse_ecb_response(raw)

        assert "2024-01-02" in result["rates"]
        assert "2024-01-06" not in result["rates"]

    def test_null_observation_values_skipped(self):
        """None observation values (missing data) must not appear in output."""
        raw = {
            "dataSets": [
                {
                    "series": {
                        "0:0:0:0:0": {"observations": {"0": [None], "1": [1.09]}},
                    }
                }
            ],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "D"}]},
                        {"id": "CURRENCY", "values": [{"id": "USD"}]},
                        {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                        {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                        {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
                    ],
                    "observation": [
                        {
                            "id": "TIME_PERIOD",
                            "values": [{"id": "2024-01-01"}, {"id": "2024-01-02"}],
                        },
                    ],
                }
            },
        }
        handler = ECBHandler()
        result = handler._parse_ecb_response(raw)

        assert "2024-01-01" not in result["rates"]  # None observation → dropped
        assert result["rates"]["2024-01-02"]["USD"] == 1.09

    def test_raises_on_malformed_structure(self):
        handler = ECBHandler()

        with pytest.raises((KeyError, IndexError, TypeError, ValueError)):
            handler._parse_ecb_response({"dataSets": [], "structure": {}})

    def test_empty_series_raises_value_error(self):
        """Empty dataSets[0].series must raise ValueError — prevents DynamoDB state advancing."""
        raw = {
            "dataSets": [{"series": {}}],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "D"}]},
                        {"id": "CURRENCY", "values": [{"id": "USD"}]},
                        {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                        {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                        {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
                    ],
                    "observation": [
                        {"id": "TIME_PERIOD", "values": [{"id": "2024-01-02"}]}
                    ],
                }
            },
        }
        handler = ECBHandler()

        with pytest.raises(ValueError, match="no rate observations"):
            handler._parse_ecb_response(raw)

    def test_raises_when_currency_dimension_missing(self):
        """Missing CURRENCY dimension must raise ValueError with available dim names."""
        raw = {
            "dataSets": [{"series": {}}],
            "structure": {
                "dimensions": {
                    "series": [{"id": "FREQ", "values": [{"id": "D"}]}],
                    "observation": [
                        {"id": "TIME_PERIOD", "values": [{"id": "2024-01-02"}]}
                    ],
                }
            },
        }
        handler = ECBHandler()

        with pytest.raises(ValueError, match="CURRENCY"):
            handler._parse_ecb_response(raw)

    def test_raises_when_time_period_dimension_missing(self):
        """Missing TIME_PERIOD dimension must raise ValueError with available dim names."""
        raw = {
            "dataSets": [{"series": {}}],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "D"}]},
                        {"id": "CURRENCY", "values": [{"id": "USD"}]},
                        {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                        {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                        {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
                    ],
                    "observation": [{"id": "SOME_OTHER_DIM", "values": []}],
                }
            },
        }
        handler = ECBHandler()

        with pytest.raises(ValueError, match="TIME_PERIOD"):
            handler._parse_ecb_response(raw)


# ---------------------------------------------------------------------------
# ECBHandler.fetch_data(start_date, end_date)
# ---------------------------------------------------------------------------
class TestFetchData:
    @responses.activate
    def test_successful_fetch_returns_parsed_rates(self):
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)
        handler = ECBHandler()

        result = handler.fetch_data("2024-01-02", "2024-01-02")

        assert result == EXPECTED_PARSED

    @responses.activate
    def test_sends_correct_query_params(self):
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)
        handler = ECBHandler()

        handler.fetch_data("2024-01-02", "2024-01-05")

        request_url = responses.calls[0].request.url
        assert "startPeriod=2024-01-02" in request_url
        assert "endPeriod=2024-01-05" in request_url
        assert "format=jsondata" in request_url

    @responses.activate
    def test_api_timeout_raises(self):
        responses.add(
            responses.GET, ECB_API_URL, body=requests.exceptions.Timeout("timed out")
        )

        with pytest.raises(requests.exceptions.Timeout):
            ECBHandler().fetch_data("2024-01-02", "2024-01-05")

    @responses.activate
    def test_api_http_error_raises(self):
        responses.add(responses.GET, ECB_API_URL, json={"error": "not found"}, status=404)

        with pytest.raises(requests.exceptions.HTTPError):
            ECBHandler().fetch_data("2024-01-02", "2024-01-05")

    @responses.activate
    def test_api_non_json_response_raises(self):
        responses.add(responses.GET, ECB_API_URL, body="<html>error</html>", status=200)

        with pytest.raises(requests.exceptions.JSONDecodeError):
            ECBHandler().fetch_data("2024-01-02", "2024-01-05")

    @responses.activate
    def test_api_connection_error_raises(self):
        responses.add(
            responses.GET,
            ECB_API_URL,
            body=requests.exceptions.ConnectionError("no route"),
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            ECBHandler().fetch_data("2024-01-02", "2024-01-05")

    @responses.activate
    def test_empty_body_returns_none(self):
        """ECB API returns HTTP 200 with empty body when no data for period."""
        responses.add(responses.GET, ECB_API_URL, body=b"", status=200)

        result = ECBHandler().fetch_data("2024-01-06", "2024-01-07")
        assert result is None

    @responses.activate
    def test_malformed_sdmx_raises_key_error(self):
        responses.add(responses.GET, ECB_API_URL, json={"structure": {}}, status=200)

        with pytest.raises((KeyError, IndexError)):
            ECBHandler().fetch_data("2024-01-02", "2024-01-05")


# ---------------------------------------------------------------------------
# ECBHandler.make_filename
# ---------------------------------------------------------------------------
class TestMakeFilename:
    def test_filename_format(self):
        handler = ECBHandler()
        expected = "ecb_rates_2024-01-02_to_2024-01-05.json"
        assert handler.make_filename("2024-01-02", "2024-01-05") == expected


# ---------------------------------------------------------------------------
# lambda_handler() — static mode
# ---------------------------------------------------------------------------
class TestLambdaHandlerStatic:
    @responses.activate
    def test_end_to_end_success(self, s3_mock):
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)

        result = ecb_module.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["key"] == "ecb_rates_2024-01-01_to_2024-01-31.json"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"
        assert result["source"] == "ecb"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body["base"] == "EUR"
        assert body["source"] == "ecb"

    @responses.activate
    def test_raises_on_api_failure(self, s3_mock):
        responses.add(responses.GET, ECB_API_URL, json={"error": "boom"}, status=500)

        with pytest.raises(requests.exceptions.HTTPError):
            ecb_module.lambda_handler({}, None)


# ---------------------------------------------------------------------------
# lambda_handler() — incremental mode (STATE_TABLE set via env)
# ---------------------------------------------------------------------------
class TestLambdaHandlerIncremental:
    @responses.activate
    def test_incremental_fetch_uses_ecb_source_key(self, aws_mock, monkeypatch):
        """DynamoDB state is keyed by source="ecb", separate from "frankfurter"."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "ecb"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        fetch_url = ECB_API_URL
        responses.add(responses.GET, fetch_url, json=SAMPLE_ECB_RESPONSE, status=200)

        result = ecb_module.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"
        assert result["source"] == "ecb"

    def test_update_state_action(self, aws_mock, monkeypatch):
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        result = ecb_module.lambda_handler(
            {"action": "update_state", "end_date": "2024-01-31"}, None
        )

        assert result["status"] == "state_updated"
        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "ecb"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    @responses.activate
    @responses.activate
    def test_empty_api_response_returns_no_new_data(self, aws_mock, monkeypatch):
        """ECB API empty body (no data for period) returns no_new_data."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "ecb"},
                "last_processed_date": {"S": "2024-05-10"},
            },
        )
        responses.add(responses.GET, ECB_API_URL, body=b"", status=200)

        result = ecb_module.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert result["source"] == "ecb"

    @responses.activate
    def test_ecb_reads_only_its_own_dynamodb_row(self, aws_mock, monkeypatch):
        """ECB handler must only read source='ecb' row, not source='frankfurter'."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        # Seed both source rows with different dates in the same table
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
                "source": {"S": "ecb"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)

        result = ecb_module.lambda_handler({}, None)

        # ECB must have read source="ecb" (last_processed=2024-01-14), not "frankfurter"
        assert result["start_date"] == "2024-01-15"
