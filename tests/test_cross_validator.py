import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("ATHENA_RESULTS_BUCKET", "test-athena-results")

from lambda_cross_validator import (
    CrossValidationCheck,
    build_rate_consistency_query,
    build_temporal_consistency_query,
    build_volume_consistency_query,
    check_rate_consistency,
    check_temporal_consistency,
    check_volume_consistency,
    lambda_handler,
    parse_rate_consistency_results,
    parse_temporal_results,
    parse_volume_results,
)


class TestBuildQueries:
    def test_rate_consistency_query(self):
        query = build_rate_consistency_query("fx_rates")
        assert "frankfurter" in query
        assert "ecb" in query
        assert "deviation" in query.lower()
        assert "JOIN" in query

    def test_temporal_consistency_query(self):
        query = build_temporal_consistency_query("fx_rates")
        assert "MIN(date)" in query
        assert "MAX(date)" in query
        assert "GROUP BY source" in query

    def test_volume_consistency_query(self):
        query = build_volume_consistency_query("fx_rates")
        assert "COUNT(*)" in query
        assert "GROUP BY source" in query

    def test_invalid_table_name_rate(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            build_rate_consistency_query("bad; DROP TABLE")

    def test_invalid_table_name_temporal(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            build_temporal_consistency_query("bad; DROP TABLE")

    def test_invalid_table_name_volume(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            build_volume_consistency_query("bad; DROP TABLE")


class TestParseResults:
    def test_parse_rate_consistency_empty(self):
        assert parse_rate_consistency_results({"Rows": []}) == []

    def test_parse_rate_consistency_header_only(self):
        result_set = {
            "Rows": [
                {"Data": [
                    {"VarCharValue": "date"},
                    {"VarCharValue": "target_currency"},
                    {"VarCharValue": "frankfurter_rate"},
                    {"VarCharValue": "ecb_rate"},
                    {"VarCharValue": "deviation"},
                ]}
            ]
        }
        assert parse_rate_consistency_results(result_set) == []

    def test_parse_rate_consistency_with_discrepancies(self):
        result_set = {
            "Rows": [
                {"Data": [
                    {"VarCharValue": "date"},
                    {"VarCharValue": "target_currency"},
                    {"VarCharValue": "frankfurter_rate"},
                    {"VarCharValue": "ecb_rate"},
                    {"VarCharValue": "deviation"},
                ]},
                {"Data": [
                    {"VarCharValue": "2024-01-15"},
                    {"VarCharValue": "USD"},
                    {"VarCharValue": "1.0850"},
                    {"VarCharValue": "1.0720"},
                    {"VarCharValue": "0.0121"},
                ]},
            ]
        }
        discrepancies = parse_rate_consistency_results(result_set)
        assert len(discrepancies) == 1
        assert discrepancies[0]["date"] == "2024-01-15"
        assert discrepancies[0]["target_currency"] == "USD"
        assert discrepancies[0]["deviation"] == pytest.approx(0.0121)

    def test_parse_rate_consistency_short_row_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "header"}]},
                {"Data": [{"VarCharValue": "short"}]},
            ]
        }
        assert parse_rate_consistency_results(result_set) == []

    def test_parse_temporal_empty(self):
        assert parse_temporal_results({"Rows": []}) == {}

    def test_parse_temporal_with_sources(self):
        result_set = {
            "Rows": [
                {"Data": [
                    {"VarCharValue": "source"},
                    {"VarCharValue": "min_date"},
                    {"VarCharValue": "max_date"},
                ]},
                {"Data": [
                    {"VarCharValue": "frankfurter"},
                    {"VarCharValue": "2024-01-01"},
                    {"VarCharValue": "2024-12-31"},
                ]},
                {"Data": [
                    {"VarCharValue": "ecb"},
                    {"VarCharValue": "2024-01-02"},
                    {"VarCharValue": "2024-12-31"},
                ]},
            ]
        }
        parsed = parse_temporal_results(result_set)
        assert len(parsed) == 2
        assert parsed["frankfurter"]["min_date"] == "2024-01-01"
        assert parsed["ecb"]["min_date"] == "2024-01-02"

    def test_parse_temporal_short_row_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "header"}]},
                {"Data": [{"VarCharValue": "short"}]},
            ]
        }
        assert parse_temporal_results(result_set) == {}

    def test_parse_volume_empty(self):
        assert parse_volume_results({"Rows": []}) == {}

    def test_parse_volume_with_sources(self):
        result_set = {
            "Rows": [
                {"Data": [
                    {"VarCharValue": "source"},
                    {"VarCharValue": "row_count"},
                ]},
                {"Data": [
                    {"VarCharValue": "frankfurter"},
                    {"VarCharValue": "5000"},
                ]},
                {"Data": [
                    {"VarCharValue": "ecb"},
                    {"VarCharValue": "4800"},
                ]},
            ]
        }
        parsed = parse_volume_results(result_set)
        assert parsed["frankfurter"] == 5000
        assert parsed["ecb"] == 4800

    def test_parse_volume_short_row_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "header"}]},
                {"Data": [{"VarCharValue": "short"}]},
            ]
        }
        assert parse_volume_results(result_set) == {}


class TestCrossValidationCheck:
    def test_frozen(self):
        check = CrossValidationCheck(
            check_name="test",
            passed=True,
            detail="ok",
            metric_value=0.0,
        )
        with pytest.raises(AttributeError):
            check.passed = False


def _make_athena_mock(result_set: dict) -> MagicMock:
    mock = MagicMock()
    mock.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
    mock.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    mock.get_query_results.return_value = {"ResultSet": result_set}
    return mock


class TestCheckRateConsistency:
    def test_no_discrepancies(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "header"}] * 5},
        ]})
        result = check_rate_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True
        assert result.metric_value == 0.0

    def test_with_discrepancies(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [
                {"VarCharValue": "date"},
                {"VarCharValue": "currency"},
                {"VarCharValue": "frank_rate"},
                {"VarCharValue": "ecb_rate"},
                {"VarCharValue": "deviation"},
            ]},
            {"Data": [
                {"VarCharValue": "2024-03-15"},
                {"VarCharValue": "USD"},
                {"VarCharValue": "1.095"},
                {"VarCharValue": "1.080"},
                {"VarCharValue": "0.0139"},
            ]},
            {"Data": [
                {"VarCharValue": "2024-03-16"},
                {"VarCharValue": "GBP"},
                {"VarCharValue": "0.862"},
                {"VarCharValue": "0.850"},
                {"VarCharValue": "0.0141"},
            ]},
        ]})
        result = check_rate_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is False
        assert result.metric_value == 2.0
        assert "2 currency-pair-dates" in result.detail


class TestCheckTemporalConsistency:
    def test_single_source_skips(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
        ]})
        result = check_temporal_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True
        assert "skipping" in result.detail.lower()

    def test_aligned_sources(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
        ]})
        result = check_temporal_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True
        assert "0 day(s)" in result.detail

    def test_misaligned_sources(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-28"},
            ]},
        ]})
        result = check_temporal_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is False
        assert "3 day(s)" in result.detail

    def test_within_threshold(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-30"},
            ]},
        ]})
        result = check_temporal_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True
        assert "1 day(s)" in result.detail


class TestCheckVolumeConsistency:
    def test_single_source_skips(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 2},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "5000"},
            ]},
        ]})
        result = check_volume_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True
        assert "skipping" in result.detail.lower()

    def test_balanced_volumes(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 2},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "5000"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "4800"},
            ]},
        ]})
        result = check_volume_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is True

    def test_imbalanced_volumes(self):
        athena = _make_athena_mock({"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 2},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "10000"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "100"},
            ]},
        ]})
        result = check_volume_consistency(athena, "fxlake", "s3://bucket/", "wg")
        assert result.passed is False


class TestPublishMetrics:
    def test_publish_success(self):
        from lambda_cross_validator import _publish_cross_validation_metrics

        cw = MagicMock()
        checks = [
            CrossValidationCheck("rate_consistency", True, "ok", 0.0),
            CrossValidationCheck("temporal_consistency", False, "gap", 3.0),
        ]
        _publish_cross_validation_metrics(cw, checks)
        cw.put_metric_data.assert_called_once()
        call_args = cw.put_metric_data.call_args
        metric_data = call_args[1]["MetricData"]
        assert len(metric_data) == 3
        discrepancy_metric = next(
            m for m in metric_data if m["MetricName"] == "CrossSourceDiscrepancy"
        )
        assert discrepancy_metric["Value"] == 1.0

    def test_publish_client_error_logged(self):
        from lambda_cross_validator import _publish_cross_validation_metrics

        cw = MagicMock()
        cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "fail"}},
            "PutMetricData",
        )
        checks = [CrossValidationCheck("test", True, "ok", 0.0)]
        _publish_cross_validation_metrics(cw, checks)


class TestLambdaHandler:
    @patch("lambda_cross_validator.boto3")
    def test_all_checks_pass(self, mock_boto3):
        no_rows = {"Rows": [{"Data": [{"VarCharValue": "h"}] * 5}]}
        single_source = {"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
        ]}
        single_volume = {"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 2},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "5000"},
            ]},
        ]}

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": no_rows},
            {"ResultSet": single_source},
            {"ResultSet": single_volume},
        ]

        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc: athena if svc == "athena" else cw

        context = MagicMock()
        context.aws_request_id = "req-123"

        result = lambda_handler({}, context)
        assert result["status"] == "PASSED"
        assert result["passed"] is True
        assert result["checks_total"] == 3
        assert result["checks_passed"] == 3

    @patch("lambda_cross_validator.boto3")
    def test_discrepancy_returns_warning(self, mock_boto3):
        rate_rows = {"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 5},
            {"Data": [
                {"VarCharValue": "2024-01-15"},
                {"VarCharValue": "USD"},
                {"VarCharValue": "1.095"},
                {"VarCharValue": "1.080"},
                {"VarCharValue": "0.0139"},
            ]},
        ]}
        temporal = {"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 3},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "2024-01-01"},
                {"VarCharValue": "2024-12-31"},
            ]},
        ]}
        volume = {"Rows": [
            {"Data": [{"VarCharValue": "h"}] * 2},
            {"Data": [
                {"VarCharValue": "frankfurter"},
                {"VarCharValue": "5000"},
            ]},
            {"Data": [
                {"VarCharValue": "ecb"},
                {"VarCharValue": "4800"},
            ]},
        ]}

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": rate_rows},
            {"ResultSet": temporal},
            {"ResultSet": volume},
        ]

        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc: athena if svc == "athena" else cw

        context = MagicMock()
        context.aws_request_id = "req-456"

        result = lambda_handler({}, context)
        assert result["status"] == "WARNING"
        assert result["passed"] is False
        assert result["checks_passed"] == 2

    @patch("lambda_cross_validator.boto3")
    def test_custom_database(self, mock_boto3):
        empty = {"Rows": [{"Data": [{"VarCharValue": "h"}] * 5}]}

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.return_value = {"ResultSet": empty}

        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc: athena if svc == "athena" else cw

        context = MagicMock()
        context.aws_request_id = "req-789"

        lambda_handler({"database_name": "custom_db"}, context)

        calls = athena.start_query_execution.call_args_list
        for call in calls:
            assert call[1]["QueryExecutionContext"]["Database"] == "custom_db"


class TestAthenaPolling:
    def test_query_failure_raises(self):
        from lambda_cross_validator import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-fail"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED",
                    "StateChangeReason": "syntax error",
                }
            }
        }
        with pytest.raises(RuntimeError, match="syntax error"):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    def test_query_cancelled_raises(self):
        from lambda_cross_validator import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-cancel"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "CANCELLED"}
            }
        }
        with pytest.raises(RuntimeError, match="CANCELLED"):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    @patch("lambda_cross_validator.time.sleep")
    def test_query_timeout_raises(self, mock_sleep):
        from lambda_cross_validator import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-timeout"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "RUNNING"}}
        }
        with pytest.raises(TimeoutError):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    @patch("lambda_cross_validator.time.sleep")
    def test_query_polls_then_succeeds(self, mock_sleep):
        from lambda_cross_validator import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-poll"}
        athena.get_query_execution.side_effect = [
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        ]
        athena.get_query_results.return_value = {
            "ResultSet": {"Rows": []}
        }
        result = _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")
        assert result == {"Rows": []}
        assert mock_sleep.call_count == 2
