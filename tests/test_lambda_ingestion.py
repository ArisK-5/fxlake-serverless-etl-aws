import json
from unittest.mock import patch

import pytest
import requests
import responses
from botocore.exceptions import ClientError

import lambda_ingestion_function as ingestion

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
# fetch_exchange_rates()
# ---------------------------------------------------------------------------
class TestFetchExchangeRates:
    @responses.activate
    def test_successful_fetch(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.fetch_exchange_rates()

        assert result["base"] == "EUR"
        assert "2024-01-02" in result["rates"]

    @responses.activate
    def test_base_currency_param_sent(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        ingestion.fetch_exchange_rates()

        assert "base=EUR" in responses.calls[0].request.url

    @responses.activate
    def test_api_timeout(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.Timeout("Request timed out"),
        )

        with pytest.raises(requests.exceptions.Timeout):
            ingestion.fetch_exchange_rates()

    @responses.activate
    def test_api_connection_error(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.ConnectionError("DNS resolution failed"),
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            ingestion.fetch_exchange_rates()

    @responses.activate
    def test_api_http_error(self):
        responses.add(responses.GET, API_URL, json={"error": "not found"}, status=404)

        with pytest.raises(requests.exceptions.HTTPError):
            ingestion.fetch_exchange_rates()


# ---------------------------------------------------------------------------
# save_to_s3()
# ---------------------------------------------------------------------------
class TestSaveToS3:
    def test_saves_json_with_correct_key_and_metadata(self, s3_mock):
        filename = ingestion.save_to_s3(SAMPLE_API_RESPONSE)

        assert filename == "exchange_rates_EUR_2024-01-01_to_2024-01-31.json"

        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=filename)
        body = json.loads(obj["Body"].read())
        assert body == SAMPLE_API_RESPONSE
        assert obj["ContentType"] == "application/json"
        assert obj["Metadata"]["start_date"] == "2024-01-01"
        assert obj["Metadata"]["end_date"] == "2024-01-31"
        assert obj["Metadata"]["base_currency"] == "EUR"
        assert obj["Metadata"]["source"] == "frankfurter"

    def test_s3_write_failure(self):
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}
        with patch.object(ingestion, "S3") as mock_s3:
            mock_s3.put_object.side_effect = ClientError(error_response, "PutObject")

            with pytest.raises(ClientError):
                ingestion.save_to_s3(SAMPLE_API_RESPONSE)


# ---------------------------------------------------------------------------
# lambda_handler()
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    @responses.activate
    def test_end_to_end_success(self, s3_mock):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["key"] == "exchange_rates_EUR_2024-01-01_to_2024-01-31.json"
        assert result["start_date"] == "2024-01-01"
        assert result["end_date"] == "2024-01-31"
        assert result["base"] == "EUR"

        # Verify the file landed in S3
        obj = s3_mock.get_object(Bucket="test-raw-bucket", Key=result["key"])
        body = json.loads(obj["Body"].read())
        assert body["base"] == "EUR"
        assert len(body["rates"]) == 2

    @responses.activate
    def test_raises_on_api_failure(self, s3_mock):
        responses.add(responses.GET, API_URL, json={"error": "boom"}, status=500)

        with pytest.raises(requests.exceptions.HTTPError):
            ingestion.lambda_handler({}, None)
