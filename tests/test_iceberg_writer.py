"""Tests for the Iceberg writer Lambda (lambda_iceberg_writer.py).

Covers: FX rate + economic indicator JSON parsing, INSERT query building,
Athena execution + polling, S3 read errors, empty data handling, quality
checks integration (pass/warning/critical), quarantine, and end-to-end
lambda_handler flow for both domains.
"""

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from lambda_iceberg_writer import (
    _build_insert_queries,
    _execute_athena_query,
    _parse_economic_indicators,
    _parse_fx_rates,
    _poll_query_completion,
    _publish_quality_metric,
    _quarantine_records,
    _read_raw_json,
    _run_quality_checks,
    _write_quality_report,
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

SAMPLE_FRED_JSON = {
    "source": "fred",
    "series_id": "UNRATE",
    "observations": {
        "2024-01-01": 3.7,
        "2024-02-01": 3.9,
    },
}


@pytest.fixture()
def s3_with_fx_json():
    """Moto S3 with a sample FX JSON file uploaded."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-raw-bucket")
        client.create_bucket(Bucket="test-processed-bucket")
        client.create_bucket(Bucket="test-quarantine-bucket")
        client.put_object(
            Bucket="test-raw-bucket",
            Key="exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
            Body=json.dumps(SAMPLE_FX_JSON),
            ContentType="application/json",
        )
        yield client


@pytest.fixture()
def s3_with_fred_json():
    """Moto S3 with a sample FRED JSON file uploaded."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-raw-bucket")
        client.create_bucket(Bucket="test-processed-bucket")
        client.create_bucket(Bucket="test-quarantine-bucket")
        client.put_object(
            Bucket="test-raw-bucket",
            Key="fred_UNRATE_2024-01-01_to_2024-02-01.json",
            Body=json.dumps(SAMPLE_FRED_JSON),
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
# _parse_economic_indicators
# ---------------------------------------------------------------------------


class TestParseEconomicIndicators:
    def test_parses_fred_json(self):
        rows = _parse_economic_indicators(SAMPLE_FRED_JSON)
        assert len(rows) == 2
        assert all(r["source"] == "fred" for r in rows)
        assert all(r["series_id"] == "UNRATE" for r in rows)

    def test_correct_row_structure(self):
        rows = _parse_economic_indicators(SAMPLE_FRED_JSON)
        row = rows[0]
        assert set(row.keys()) == {"date", "source", "series_id", "value"}
        assert isinstance(row["value"], float)

    def test_empty_observations(self):
        data = {"source": "fred", "series_id": "UNRATE", "observations": []}
        rows = _parse_economic_indicators(data)
        assert rows == []

    def test_defaults_source_to_fred(self):
        data = {"series_id": "GDP", "observations": [{"date": "2024-01-01", "value": "100.5"}]}
        rows = _parse_economic_indicators(data)
        assert rows[0]["source"] == "fred"

    def test_value_cast_to_float(self):
        data = {
            "source": "fred",
            "series_id": "X",
            "observations": [{"date": "2024-01-01", "value": "42"}],
        }
        rows = _parse_economic_indicators(data)
        assert rows[0]["value"] == 42.0
        assert isinstance(rows[0]["value"], float)


# ---------------------------------------------------------------------------
# _build_insert_queries
# ---------------------------------------------------------------------------


class TestBuildInsertQueries:
    def test_generates_valid_fx_insert(self):
        rows = [
            {"date": "2024-01-02", "source": "frankfurter", "base_currency": "EUR",
             "target_currency": "USD", "rate": 1.1023},
        ]
        queries = _build_insert_queries("fx_rates", rows)
        assert len(queries) == 1
        assert queries[0].startswith("INSERT INTO fx_rates")
        assert "VALUES" in queries[0]
        assert "'2024-01-02'" in queries[0]
        assert "'frankfurter'" in queries[0]
        assert "1.1023" in queries[0]

    def test_generates_valid_econ_insert(self):
        rows = [
            {"date": "2024-01-01", "source": "fred", "series_id": "UNRATE", "value": 3.7},
        ]
        queries = _build_insert_queries("economic_indicators", rows, domain="economic_indicators")
        assert len(queries) == 1
        assert queries[0].startswith("INSERT INTO economic_indicators")
        assert "(date, source, series_id, value)" in queries[0]
        assert "'UNRATE'" in queries[0]
        assert "3.7" in queries[0]

    def test_multiple_rows_single_batch(self):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        queries = _build_insert_queries("fx_rates", rows)
        assert len(queries) == 1
        assert queries[0].count("VALUES") == 1
        assert queries[0].count("('") == len(rows)

    def test_empty_rows_raises(self):
        with pytest.raises(ValueError, match="No rows to insert"):
            _build_insert_queries("fx_rates", [])

    def test_escapes_single_quotes(self):
        rows = [
            {"date": "2024-01-02", "source": "frank'furter", "base_currency": "EU'R",
             "target_currency": "US'D", "rate": 1.0},
        ]
        queries = _build_insert_queries("fx_rates", rows)
        assert "frank''furter" in queries[0]
        assert "EU''R" in queries[0]
        assert "US''D" in queries[0]

    def test_escapes_single_quotes_econ(self):
        rows = [
            {"date": "2024-01-01", "source": "fre'd", "series_id": "UN'RATE", "value": 3.7},
        ]
        queries = _build_insert_queries("economic_indicators", rows, domain="economic_indicators")
        assert "fre''d" in queries[0]
        assert "UN''RATE" in queries[0]

    def test_includes_all_fx_columns(self):
        rows = [
            {"date": "2024-01-02", "source": "ecb", "base_currency": "EUR",
             "target_currency": "CHF", "rate": 0.931},
        ]
        queries = _build_insert_queries("fx_rates", rows)
        assert "(date, source, base_currency, target_currency, rate)" in queries[0]

    def test_includes_all_econ_columns(self):
        rows = [
            {"date": "2024-01-01", "source": "fred", "series_id": "UNRATE", "value": 3.7},
        ]
        queries = _build_insert_queries("t", rows, domain="economic_indicators")
        assert "(date, source, series_id, value)" in queries[0]

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
            _build_insert_queries(bad_name, rows)

    def test_accepts_valid_table_names(self):
        rows = [
            {"date": "2024-01-02", "source": "ecb", "base_currency": "EUR",
             "target_currency": "CHF", "rate": 0.931},
        ]
        for name in ["fx_rates", "_private", "Table1", "a"]:
            queries = _build_insert_queries(name, rows)
            assert f"INSERT INTO {name}" in queries[0]

    def test_batches_when_exceeding_limit(self, monkeypatch):
        monkeypatch.setattr("lambda_iceberg_writer.ATHENA_QUERY_STRING_LIMIT", 500)
        rows = [
            {"date": f"2024-01-{i:02d}", "source": "ecb", "base_currency": "EUR",
             "target_currency": "USD", "rate": 1.1}
            for i in range(1, 20)
        ]
        queries = _build_insert_queries("fx_rates", rows)
        assert len(queries) > 1
        for q in queries:
            assert q.startswith("INSERT INTO fx_rates")
            assert "VALUES" in q

    def test_all_rows_present_across_batches(self, monkeypatch):
        monkeypatch.setattr("lambda_iceberg_writer.ATHENA_QUERY_STRING_LIMIT", 500)
        rows = [
            {"date": f"2024-01-{i:02d}", "source": "ecb", "base_currency": "EUR",
             "target_currency": "USD", "rate": float(i)}
            for i in range(1, 20)
        ]
        queries = _build_insert_queries("fx_rates", rows)
        combined = "\n".join(queries)
        for i in range(1, 20):
            assert f"2024-01-{i:02d}" in combined


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
# _run_quality_checks
# ---------------------------------------------------------------------------


class TestRunQualityChecks:
    def test_fx_quality_passes(self, s3_with_fx_json):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        results = _run_quality_checks(rows, "fx_rates", "test.json", s3_with_fx_json)
        assert all(r.passed for r in results if r.level.value == "CRITICAL")

    def test_econ_quality_passes(self, s3_with_fred_json):
        rows = _parse_economic_indicators(SAMPLE_FRED_JSON)
        results = _run_quality_checks(
            rows, "economic_indicators", "test.json", s3_with_fred_json
        )
        assert all(r.passed for r in results if r.level.value == "CRITICAL")

    def test_writes_quality_report_to_s3(self, s3_with_fx_json):
        rows = _parse_fx_rates(SAMPLE_FX_JSON)
        _run_quality_checks(rows, "fx_rates", "exchange_rates.json", s3_with_fx_json)

        report_obj = s3_with_fx_json.get_object(
            Bucket="test-processed-bucket",
            Key="fx_rates/quality_reports/exchange_rates_quality.json",
        )
        report = json.loads(report_obj["Body"].read())
        assert report["domain"] == "fx_rates"
        assert "checks" in report

    def test_critical_failure_raises_and_quarantines(self, s3_with_fx_json):
        rows = [
            {"date": "2024-01-02", "source": "frankfurter", "base_currency": "EUR",
             "target_currency": "USD", "rate": -1.0},
        ]
        with pytest.raises(ValueError, match="CRITICAL"):
            _run_quality_checks(rows, "fx_rates", "bad_rates.json", s3_with_fx_json)

        quarantine_obj = s3_with_fx_json.get_object(
            Bucket="test-quarantine-bucket",
            Key="fx_rates/quarantine/bad_rates.json",
        )
        quarantined = json.loads(quarantine_obj["Body"].read())
        assert len(quarantined) == 1

    def test_warning_does_not_raise(self, s3_with_fx_json):
        rows = [
            {"date": "2024-01-02", "source": "frankfurter", "base_currency": "EUR",
             "target_currency": "USD", "rate": 1.1},
            {"date": "2024-01-02", "source": "frankfurter", "base_currency": "EUR",
             "target_currency": "USD", "rate": 1.1},
        ]
        results = _run_quality_checks(rows, "fx_rates", "dup.json", s3_with_fx_json)
        warnings = [r for r in results if not r.passed]
        assert len(warnings) > 0
        assert all(r.level.value == "WARNING" for r in warnings)

    def test_econ_critical_failure_quarantines(self, s3_with_fred_json):
        rows = [
            {"date": None, "source": "fred", "series_id": "UNRATE", "value": 3.7},
        ]
        with pytest.raises(ValueError, match="CRITICAL"):
            _run_quality_checks(
                rows, "economic_indicators", "fred_bad.json", s3_with_fred_json
            )

        quarantine_obj = s3_with_fred_json.get_object(
            Bucket="test-quarantine-bucket",
            Key="economic_indicators/quarantine/fred_bad.json",
        )
        quarantined = json.loads(quarantine_obj["Body"].read())
        assert len(quarantined) == 1


# ---------------------------------------------------------------------------
# _write_quality_report
# ---------------------------------------------------------------------------


class TestWriteQualityReport:
    def test_writes_to_correct_path(self, s3_with_fx_json):
        report = {"domain": "fx_rates", "checks": []}
        key = _write_quality_report(
            s3_with_fx_json, report, "exchange_rates_EUR_2024.json", "fx_rates"
        )
        assert key == "fx_rates/quality_reports/exchange_rates_EUR_2024_quality.json"

        obj = s3_with_fx_json.get_object(
            Bucket="test-processed-bucket", Key=key
        )
        assert json.loads(obj["Body"].read()) == report

    def test_econ_report_path(self, s3_with_fred_json):
        report = {"domain": "economic_indicators", "checks": []}
        key = _write_quality_report(
            s3_with_fred_json, report, "fred_UNRATE_2024.json", "economic_indicators"
        )
        assert key == "economic_indicators/quality_reports/fred_UNRATE_2024_quality.json"


# ---------------------------------------------------------------------------
# lambda_handler (end-to-end with mocked Athena)
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_successful_fx_write(
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
            "domain": "fx_rates",
            "database_name": "fxlake",
        }
        result = lambda_handler(event, mock_context)

        assert result["status"] == "ok"
        assert result["rows_inserted"] == 4
        assert result["batches"] == 1
        assert result["target_table"] == "fx_rates"
        assert result["domain"] == "fx_rates"
        mock_execute.assert_called_once()
        mock_poll.assert_called_once()

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_successful_econ_write(
        self, mock_execute, mock_poll, s3_with_fred_json, mock_context
    ):
        mock_execute.return_value = "qid-econ-1"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "fred_UNRATE_2024-01-01_to_2024-02-01.json",
            "target_table": "economic_indicators",
            "domain": "economic_indicators",
            "database_name": "fxlake",
        }
        result = lambda_handler(event, mock_context)

        assert result["status"] == "ok"
        assert result["rows_inserted"] == 2
        assert result["domain"] == "economic_indicators"
        query = mock_execute.call_args[0][1]
        assert "INSERT INTO economic_indicators" in query
        assert "'UNRATE'" in query

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
            s3.create_bucket(Bucket="test-processed-bucket")
            s3.create_bucket(Bucket="test-quarantine-bucket")
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="ecb_rates_2024-01-02_to_2024-01-02.json",
                Body=json.dumps(SAMPLE_ECB_JSON),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "ecb_rates_2024-01-02_to_2024-01-02.json",
                "target_table": "fx_rates",
                "domain": "fx_rates",
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
            s3.create_bucket(Bucket="test-processed-bucket")
            s3.create_bucket(Bucket="test-quarantine-bucket")
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

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_empty_econ_returns_no_data(
        self, mock_execute, mock_poll, mock_context
    ):
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-raw-bucket")
            s3.create_bucket(Bucket="test-processed-bucket")
            s3.create_bucket(Bucket="test-quarantine-bucket")
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="fred_empty.json",
                Body=json.dumps({
                    "source": "fred",
                    "series_id": "UNRATE",
                    "observations": [],
                }),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "fred_empty.json",
                "domain": "economic_indicators",
            }
            result = lambda_handler(event, mock_context)

        assert result["status"] == "no_data"
        assert result["domain"] == "economic_indicators"
        mock_execute.assert_not_called()

    def test_missing_raw_key_raises(self, mock_context):
        with pytest.raises(ValueError, match="raw_key"):
            lambda_handler({"raw_bucket": "b"}, mock_context)

    def test_missing_raw_bucket_raises(self, monkeypatch, mock_context):
        monkeypatch.setattr("lambda_iceberg_writer.RAW_BUCKET", "")
        with pytest.raises(ValueError, match="raw_bucket"):
            lambda_handler({"raw_key": "k"}, mock_context)

    def test_invalid_domain_raises(self, mock_context):
        with pytest.raises(ValueError, match="Invalid domain"):
            lambda_handler(
                {"raw_bucket": "b", "raw_key": "k", "domain": "bad_domain"},
                mock_context,
            )

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

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_quality_failure_blocks_iceberg_write(
        self, mock_execute, mock_poll, mock_context
    ):
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-raw-bucket")
            s3.create_bucket(Bucket="test-processed-bucket")
            s3.create_bucket(Bucket="test-quarantine-bucket")
            bad_data = {
                "base": "EUR",
                "source": "frankfurter",
                "rates": {"2024-01-02": {"USD": -5.0}},
            }
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="bad_rates.json",
                Body=json.dumps(bad_data),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "bad_rates.json",
                "domain": "fx_rates",
            }
            with pytest.raises(ValueError, match="CRITICAL"):
                lambda_handler(event, mock_context)

        mock_execute.assert_not_called()
        mock_poll.assert_not_called()

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_quality_warning_allows_iceberg_write(
        self, mock_execute, mock_poll, mock_context
    ):
        mock_execute.return_value = "qid-warning"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-raw-bucket")
            s3.create_bucket(Bucket="test-processed-bucket")
            s3.create_bucket(Bucket="test-quarantine-bucket")
            dup_data = {
                "base": "EUR",
                "source": "frankfurter",
                "rates": {
                    "2024-01-02": {"USD": 1.1023},
                    "2024-01-03": {"USD": 1.0956},
                },
            }
            s3.put_object(
                Bucket="test-raw-bucket",
                Key="dup_rates.json",
                Body=json.dumps(dup_data),
            )

            event = {
                "raw_bucket": "test-raw-bucket",
                "raw_key": "dup_rates.json",
                "domain": "fx_rates",
            }
            result = lambda_handler(event, mock_context)

        assert result["status"] == "ok"
        mock_execute.assert_called_once()

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_writes_quality_report_on_success(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-report"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
            "domain": "fx_rates",
        }
        lambda_handler(event, mock_context)

        report_obj = s3_with_fx_json.get_object(
            Bucket="test-processed-bucket",
            Key="fx_rates/quality_reports/exchange_rates_EUR_2024-01-02_to_2024-01-03_quality.json",
        )
        report = json.loads(report_obj["Body"].read())
        assert report["domain"] == "fx_rates"
        assert report["overall_passed"] is True

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_econ_end_to_end_writes_report(
        self, mock_execute, mock_poll, s3_with_fred_json, mock_context
    ):
        mock_execute.return_value = "qid-econ-report"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "fred_UNRATE_2024-01-01_to_2024-02-01.json",
            "domain": "economic_indicators",
        }
        lambda_handler(event, mock_context)

        report_obj = s3_with_fred_json.get_object(
            Bucket="test-processed-bucket",
            Key="economic_indicators/quality_reports/fred_UNRATE_2024-01-01_to_2024-02-01_quality.json",
        )
        report = json.loads(report_obj["Body"].read())
        assert report["domain"] == "economic_indicators"
        assert report["overall_passed"] is True

    @patch("lambda_iceberg_writer._poll_query_completion")
    @patch("lambda_iceberg_writer._execute_athena_query")
    def test_domain_defaults_to_fx_rates(
        self, mock_execute, mock_poll, s3_with_fx_json, mock_context
    ):
        mock_execute.return_value = "qid-default-domain"
        mock_poll.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        event = {
            "raw_bucket": "test-raw-bucket",
            "raw_key": "exchange_rates_EUR_2024-01-02_to_2024-01-03.json",
        }
        result = lambda_handler(event, mock_context)
        assert result["domain"] == "fx_rates"


# ---------------------------------------------------------------------------
# _quarantine_records
# ---------------------------------------------------------------------------


class TestQuarantineRecords:
    def test_writes_to_correct_path(self, s3_with_fx_json):
        rows = [{"date": "2024-01-02", "source": "test", "base_currency": "EUR",
                 "target_currency": "USD", "rate": -1.0}]
        key = _quarantine_records(s3_with_fx_json, rows, "bad_rates.json", "fx_rates")
        assert key == "fx_rates/quarantine/bad_rates.json"

        obj = s3_with_fx_json.get_object(Bucket="test-quarantine-bucket", Key=key)
        data = json.loads(obj["Body"].read())
        assert len(data) == 1

    def test_s3_write_failure_raises(self):
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "PutObject",
        )
        rows = [{"date": "2024-01-02", "value": 1.0}]
        with pytest.raises(ClientError):
            _quarantine_records(mock_s3, rows, "test.json", "fx_rates")


# ---------------------------------------------------------------------------
# _write_quality_report — error paths
# ---------------------------------------------------------------------------


class TestWriteQualityReportErrors:
    def test_s3_write_failure_raises(self):
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "PutObject",
        )
        with pytest.raises(ClientError):
            _write_quality_report(mock_s3, {"checks": []}, "test.json", "fx_rates")


# ---------------------------------------------------------------------------
# _publish_quality_metric
# ---------------------------------------------------------------------------


class TestPublishQualityMetric:
    @patch("lambda_iceberg_writer.boto3")
    def test_publishes_metric(self, mock_boto3):
        mock_cw = MagicMock()
        mock_boto3.client.return_value = mock_cw

        _publish_quality_metric("DataQualityChecksFailed", 2.0, "fx_rates")

        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["MetricData"][0]["MetricName"] == "DataQualityChecksFailed"
        assert call_kwargs["MetricData"][0]["Value"] == 2.0

    @patch("lambda_iceberg_writer.boto3")
    def test_swallows_client_error(self, mock_boto3):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "oops"}},
            "PutMetricData",
        )
        mock_boto3.client.return_value = mock_cw

        _publish_quality_metric("TestMetric", 1.0, "fx_rates")
