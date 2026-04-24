"""Tests for the Iceberg writer Lambda (lambda_iceberg_writer.py).

Covers: FX rate JSON parsing, INSERT query building, Athena execution + polling,
S3 read errors, empty data handling, and end-to-end lambda_handler flow.
"""

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from lambda_iceberg_writer import (
    _build_insert_query,
    _execute_athena_query,
    _parse_fx_rates,
    _poll_query_completion,
    _read_raw_json,
    lambda_handler,
)
from moto import mock_aws

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_FX_JSON = {
    "base": "EUR",
    "source": "frankfurter",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8643},
    },
}

SAMPLE_ECB_JSON = {
    "base": "EUR",
    "source": "ecb",
    "rates": {
        "2024-01-02": {"USD": 1.1050, "CHF": 0.9310},
    },
}


@pytest.fixture()
def s3_with_fx_json():
    """Moto S3 with a sample FX JSON file uploaded."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-raw-bucket")
        client.put_object(
            Bucket="test-raw-bucket",
            Key="exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
            Body=json.dumps(SAMPLE_FX_JSON),
            ContentType="application/json",
        )
        yield client


@pytest.fixture()
def mock_context():
    ctx = MagicMock()
    ctx.aws_request_id = "test-request-id-12345"
    return ctx


# ---------------------------------------------------------------------------
# _parse_fx_rates
# ---------------------------------------------------------------------------


class TestParseFxRates:
    def test_parses_frankfurter_json(self):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        assert len(rows) == 4
        assert all(r["source"] == "frankfurter" for r in rows)
        assert all(r["base_currency"] == "EUR" for r in rows)

    def test_parses_ecb_json(self):
        rows = _parse_fx_rates(SAMPLE_ECB_JSON)
        assert len(rows) == 2
        assert all(r["source"] == "ecb" for r in rows)

    def test_rows_sorted_by_date_then_currency(self):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates)
        jan02_rows = [r for r in rows if r["date"] == "2024-01-02"]
        currencies = [r["target_currency"] for r in jan02_rows]
        assert currencies == sorted(currencies)

    def test_correct_row_structure(self):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        row = rows[0]
        assert set(row.keys()) == {"date", "source", "base_currency", "target_currency", "rate"}
        assert isinstance(row["rate"], float)

    def test_empty_rates(self):
        rows = _parse_fx_rates({"base": "EUR", "rates": {}})
        assert rows == []

    def test_defaults_source_to_frankfurter(self):
        data = {"base": "USD", "rates": {"2024-01-02": {"EUR": 0.91}}}
        rows = _parse_fx_rates(data)
        assert rows[0]["source"] == "frankfurter"
        assert rows[0]["base_currency"] == "USD"

    def test_rate_cast_to_float(self):
        data = {"base": "EUR", "rates": {"2024-01-02": {"USD": 1}}}
        rows = _parse_fx_rates(data)
        assert rows[0]["rate"] == 1.0
        assert isinstance(rows[0]["rate"], float)


# ---------------------------------------------------------------------------
# _build_insert_query
# ---------------------------------------------------------------------------


class TestBuildInsertQuery:
    def test_generates_valid_insert(self):
        rows = [
            {"date": "2024-01-02", "source": "frankfurter", "base_currency": "EUR",
             "target_currency": "USD", "rate": 1.1023},
        ]
        query = _build_insert_query("fx_rates", rows)
        assert query.startswith("INSERT INTO fx_rates")
        assert "VALUES" in query
        assert "'2024-01-02'" in query
        assert "'frankfurter'" in query
        assert "1.1023" in query

    def test_multiple_rows(self):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        query = _build_insert_query("fx_rates", rows)
        assert query.count("VALUES") == 1
        assert query.count("('") == len(rows)

    def test_empty_rows_raises(self):
        with pytest.raises(ValueError, match="No rows to insert"):
            _build_insert_query("fx_rates", [])

    def test_escapes_single_quotes(self):
        rows = [
            {"date": "2024-01-02", "source": "frank'furter", "base_currency": "EU'R",
             "target_currency": "US'D", "rate": 1.0},
        ]
        query = _build_insert_query("fx_rates", rows)
        assert "frank''furter" in query
        assert "EU''R" in query
        assert "US''D" in query

    def test_includes_all_columns(self):
        rows = [
            {"date": "2024-01-02", "source": "ecb", "base_currency": "EUR",
             "target_currency": "CHF", "rate": 0.931},
        ]
        query = _build_insert_query("fx_rates", rows)
        assert "(date, source, base_currency, target_currency, rate)" in query

    @pytest.mark.parametrize("bad_name", [
        "fx_rates; DROP TABLE users",
        "table--name",
        "123starts_with_digit",
        "",
        "has space",
        "has.dot",
    ])
    def test_rejects_invalid_table_names(self, bad_name):
        rows = [
            {"date": "2024-01-02", "source": "ecb", "base_currency": "EUR",
             "target_currency": "CHF", "rate": 0.931},
        ]
        with pytest.raises(ValueError, match="Invalid table name"):
            _build_insert_query(bad_name, rows)

    def test_accepts_valid_table_names(self):
        rows = [
            {"date": "2024-01-02", "source": "ecb", "base_currency": "EUR",
             "target_currency": "CHF", "rate": 0.931},
        ]
        for name in ["fx_rates", "_private", "Table1", "a"]:
            query = _build_insert_query(name, rows)
            assert f"INSERT INTO {name}" in query


# ---------------------------------------------------------------------------
# _read_raw_json
# ---------------------------------------------------------------------------


class TestReadRawJson:
    def test_reads_valid_json(self, s3_with_fx_json):
        data = _read_raw_json(
            s3_with_fx_json,
            "test-raw-bucket",
            "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
        )
        assert data["base"] == "EUR"
        assert "rates" in data

    def test_missing_key_raises_client_error(self, s3_with_fx_json):
        with pytest.raises(Exception):
            _read_raw_json(s3_with_fx_json, "test-raw-bucket", "nonexistent.json")

    def test_invalid_json_raises(self, s3_with_fx_json):
        s3_with_fx_json.put_object(
            Bucket="test-raw-bucket",
            Key="bad.json",
            Body="not valid json{{{",
        )
        with pytest.raises(Exception):
            _read_raw_json(s3_with_fx_json, "test-raw-bucket", "bad.json")


# ---------------------------------------------------------------------------
# _execute_athena_query
# ---------------------------------------------------------------------------


class TestExecuteAthenaQuery:
    def test_returns_execution_id(self):
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {
            "QueryExecutionId": "qid-123"
        }
        result = _execute_athena_query(
            mock_athena, "SELECT 1", "fxlake", "s3://results/", "fxlake"
        )
        assert result == "qid-123"
        mock_athena.start_query_execution.assert_called_once()

    def test_passes_correct_parameters(self):
        mock_athena = MagicMock()
        mock_athena.start_query_execution.return_value = {
            "QueryExecutionId": "qid-456"
        }
        _execute_athena_query(
            mock_athena, "INSERT INTO t VALUES (1)", "mydb", "s3://out/", "wg"
        )
        call_kwargs = mock_athena.start_query_execution.call_args[1]
        assert call_kwargs["QueryString"] == "INSERT INTO t VALUES (1)"
        assert call_kwargs["QueryExecutionContext"]["Database"] == "mydb"
        assert call_kwargs["ResultConfiguration"]["OutputLocation"] == "s3://out/"
        assert call_kwargs["WorkGroup"] == "wg"

    def test_client_error_propagates(self):
        mock_athena = MagicMock()
        mock_athena.start_query_execution.side_effect = ClientError(
            {"Error": {"Code": "InvalidRequestException", "Message": "bad"}},
            "StartQueryExecution",
        )
        with pytest.raises(ClientError):
            _execute_athena_query(
                mock_athena, "SELECT 1", "fxlake", "s3://results/", "fxlake"
            )


# ---------------------------------------------------------------------------
# _poll_query_completion
# ---------------------------------------------------------------------------


class TestPollQueryCompletion:
    def test_returns_on_success(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        result = _poll_query_completion(mock_athena, "qid-1", poll_interval=0)
        assert result["QueryExecution"]["Status"]["State"] == "SUCCEEDED"

    def test_polls_until_success(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.side_effect = [
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        ]
        result = _poll_query_completion(mock_athena, "qid-2", poll_interval=0)
        assert result["QueryExecution"]["Status"]["State"] == "SUCCEEDED"
        assert mock_athena.get_query_execution.call_count == 3

    def test_raises_on_failed(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "syntax error"}
            }
        }
        with pytest.raises(RuntimeError, match="FAILED.*syntax error"):
            _poll_query_completion(mock_athena, "qid-3", poll_interval=0)

    def test_raises_on_cancelled(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "CANCELLED", "StateChangeReason": "user cancelled"}
            }
        }
        with pytest.raises(RuntimeError, match="CANCELLED"):
            _poll_query_completion(mock_athena, "qid-4", poll_interval=0)

    def test_timeout_raises(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "RUNNING"}}
        }
        with pytest.raises(TimeoutError, match="did not complete"):
            _poll_query_completion(
                mock_athena, "qid-5", poll_interval=0, max_attempts=3
            )
        assert mock_athena.get_query_execution.call_count == 3

    def test_failed_without_reason(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "FAILED"}}
        }
        with pytest.raises(RuntimeError, match="unknown"):
            _poll_query_completion(mock_athena, "qid-6", poll_interval=0)


# ---------------------------------------------------------------------------
# lambda_handler (end-to-end with mocked Athena)
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_successful_write(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-e2e-1"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
            "target_table": "fx_rates",
            "database_name": "fxlake",
        }
        result = lambda_handler(event, mock_context)

        assert result["status"] == "ok"
        assert result["rows_inserted"] == 4
        assert result["query_execution_id"] == "qid-e2e-1"
        assert result["target_table"] == "fx_rates"
        mock_execute.assert_called_once()
        mock_poll.assert_called_once()

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_ecb_source(self, mock_execute, mock_poll, mock_context):
        mock_execute.return_value = "qid-ecb"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-raw-bucket")
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="ecb_rates_2024-01-02_to_2024-01-02.json",
                Body=json.dumps(SAMPLE_ECB_JSON),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "ecb_rates_2024-01-02_to_2024-01-02.json",
                "target_table": "fx_rates",
            }
            result = lambda_handler(event, mock_context)

        assert result["status"] == "ok"
        assert result["rows_inserted"] == 2

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_empty_rates_returns_no_data(
        self, mock_execute, mock_poll, mock_context
    ):
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-raw-bucket")
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="empty.json",
                Body=json.dumps({"base": "EUR", "rates": {}}),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "empty.json",
            }
            result = lambda_handler(event, mock_context)

        assert result["status"] == "no_data"
        assert result["rows_parsed"] == 0
        mock_execute.assert_not_called()
        mock_poll.assert_not_called()

    def test_missing_raw_key_raises(self, mock_context):
        with pytest.raises(ValueError, match="raw_key"):
            lambda_handler({"raw_bucket": "b"}, mock_context)

    def test_missing_raw_bucket_raises(self, monkeypatch, mock_context):
        monkeypatch.setattr("lambda_iceberg_writer.RAW_BUCKET", "")
        with pytest.raises(ValueError, match="raw_bucket"):
            lambda_handler({"raw_key": "k"}, mock_context)

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_athena_failure_propagates(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-fail"
        mock_poll.side_effect = RuntimeError("Athena query qid-fail FAILED: syntax error")

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
        }
        with pytest.raises(RuntimeError, match="FAILED"):
            lambda_handler(event, mock_context)

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_timeout_propagates(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-timeout"
        mock_poll.side_effect = TimeoutError("did not complete")

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
        }
        with pytest.raises(TimeoutError, match="did not complete"):
            lambda_handler(event, mock_context)

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_s3_read_failure_propagates(
        self, mock_execute, mock_poll, mock_context
    ):
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(
                Bucket="test-raw-bucket"
            )
            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "missing.json",
            }
            with pytest.raises(ClientError):
                lambda_handler(event, mock_context)

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_insert_query_contains_correct_data(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-verify"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
            "target_table": "fx_rates",
        }
        lambda_handler(event, mock_context)

        query = mock_execute.call_args[0][1]
        assert "INSERT INTO fx_rates" in query
        assert "'2024-01-02'" in query
        assert "'frankfurter'" in query
        assert "'EUR'" in query
        assert "'USD'" in query
        assert "1.1023" in query

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_uses_default_table_and_database(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-defaults"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
        }
        result = lambda_handler(event, mock_context)

        assert result["target_table"] == "fx_rates"
        query = mock_execute.call_args[0][1]
        assert "INSERT INTO fx_rates" in query
