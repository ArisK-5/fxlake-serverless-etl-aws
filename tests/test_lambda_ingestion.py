import json
from datetime import date
from unittest.mock import MagicMock, patch

import lambda_ingestion_function as ingestion
import pytest
import requests
import responses
from botocore.exceptions import ClientError

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
# fetch_exchange_rates(start_date, end_date)
# ---------------------------------------------------------------------------
class TestFetchExchangeRates:
    @responses.activate
    def test_successful_fetch(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        result = ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")

        assert result["base"] == "EUR"
        assert "2024-01-02" in result["rates"]

    @responses.activate
    def test_base_currency_param_sent(self):
        responses.add(responses.GET, API_URL, json=SAMPLE_API_RESPONSE, status=200)

        ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")

        assert "base=EUR" in responses.calls[0].request.url

    @responses.activate
    def test_api_timeout(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.Timeout("Request timed out"),
        )

        with pytest.raises(requests.exceptions.Timeout):
            ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_connection_error(self):
        responses.add(
            responses.GET,
            API_URL,
            body=requests.exceptions.ConnectionError("DNS resolution failed"),
        )

        with pytest.raises(requests.exceptions.ConnectionError):
            ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_non_json_response(self):
        responses.add(responses.GET, API_URL, body="<html>error</html>", status=200)

        with pytest.raises(requests.exceptions.JSONDecodeError):
            ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")

    @responses.activate
    def test_api_http_error(self):
        responses.add(responses.GET, API_URL, json={"error": "not found"}, status=404)

        with pytest.raises(requests.exceptions.HTTPError):
            ingestion.fetch_exchange_rates("2024-01-01", "2024-01-31")


# ---------------------------------------------------------------------------
# save_to_s3(data, start_date, end_date)
# ---------------------------------------------------------------------------
class TestSaveToS3:
    def test_saves_json_with_correct_key_and_metadata(self, s3_mock):
        filename = ingestion.save_to_s3(SAMPLE_API_RESPONSE, "2024-01-01", "2024-01-31")

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
                ingestion.save_to_s3(SAMPLE_API_RESPONSE, "2024-01-01", "2024-01-31")


# ---------------------------------------------------------------------------
# get_last_processed_date()
# ---------------------------------------------------------------------------
class TestGetLastProcessedDate:
    def test_returns_start_date_when_no_entry(self, aws_mock):
        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            result = ingestion.get_last_processed_date()

        assert result == "2024-01-01"  # START_DATE from conftest

    def test_returns_stored_date(self, aws_mock):
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-15"},
            },
        )

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            result = ingestion.get_last_processed_date()

        assert result == "2024-01-15"

    def test_falls_back_on_dynamodb_client_error(self):
        error_response = {
            "Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}
        }
        mock_ddb = MagicMock()
        mock_ddb.get_item.side_effect = ClientError(error_response, "GetItem")

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", mock_ddb
        ):
            result = ingestion.get_last_processed_date()

        assert result == "2024-01-01"  # START_DATE from conftest


# ---------------------------------------------------------------------------
# update_last_processed_date(date)
# ---------------------------------------------------------------------------
class TestUpdateLastProcessedDate:
    def test_writes_date_to_dynamodb(self, aws_mock):
        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            ingestion.update_last_processed_date("2024-01-31")

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
            },
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    def test_raises_on_dynamodb_client_error(self):
        error_response = {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "Throttled",
            }
        }
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = ClientError(error_response, "PutItem")

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", mock_ddb
        ):
            with pytest.raises(ClientError):
                ingestion.update_last_processed_date("2024-01-31")


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
        assert result["base"] == "EUR"

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
# lambda_handler() — incremental mode (STATE_TABLE set)
# ---------------------------------------------------------------------------
class TestLambdaHandlerIncremental:
    @responses.activate
    def test_no_new_data_when_caught_up(self, aws_mock):
        today = date.today().isoformat()
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": today},
            },
        )

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            result = ingestion.lambda_handler({}, None)

        assert result["status"] == "no_new_data"
        assert result["last_processed_date"] == today

    @responses.activate
    def test_incremental_fetch_updates_state(self, aws_mock):
        aws_mock["dynamodb"].put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )
        # fetch range: 2024-01-15..min(today, END_DATE="2024-01-31") = 2024-01-15..2024-01-31
        fetch_url = "https://api.frankfurter.app/2024-01-15..2024-01-31"
        responses.add(responses.GET, fetch_url, json=SAMPLE_API_RESPONSE, status=200)

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            result = ingestion.lambda_handler({}, None)

        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-15"
        assert result["end_date"] == "2024-01-31"

        item = aws_mock["dynamodb"].get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-31"

    @responses.activate
    def test_first_run_uses_start_date(self, aws_mock):
        # No DynamoDB entry — should default to START_DATE="2024-01-01"
        fetch_url = "https://api.frankfurter.app/2024-01-02..2024-01-31"
        responses.add(responses.GET, fetch_url, json=SAMPLE_API_RESPONSE, status=200)

        with patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE), patch.object(
            ingestion, "DYNAMODB", aws_mock["dynamodb"]
        ):
            result = ingestion.lambda_handler({}, None)

        # START_DATE is 2024-01-01, so fetch starts from 2024-01-02
        assert result["status"] == "ok"
        assert result["start_date"] == "2024-01-02"
