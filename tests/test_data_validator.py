from unittest.mock import MagicMock, patch

import pytest
from lambda_data_validator import (
    ValidationCheck,
    _build_null_check_query,
    _build_row_count_query,
    _parse_null_check_results,
    _parse_row_count_results,
    _run_validation_suite,
    _validate_event,
    lambda_handler,
)


# ---------------------------------------------------------------------------
# _validate_event
# ---------------------------------------------------------------------------
class TestValidateEvent:
    def test_defaults(self):
        event = {}
        result = _validate_event(event)
        assert result["database"] == "fxlake"
        assert result["domains"] == ["economic_indicators", "fx_rates"]
        assert "s3://" in result["output_location"]

    def test_custom_domains(self):
        event = {"domains": ["fx_rates"]}
        result = _validate_event(event)
        assert result["domains"] == ["fx_rates"]

    def test_invalid_domain_raises(self):
        event = {"domains": ["bad_domain"]}
        with pytest.raises(ValueError, match="Invalid domain"):
            _validate_event(event)

    def test_expected_counts_passthrough(self):
        event = {"expected_counts": {"fx_rates": {"frankfurter": 100}}}
        result = _validate_event(event)
        assert result["expected_counts"] == {"fx_rates": {"frankfurter": 100}}


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------
class TestQueryBuilders:
    def test_row_count_query(self):
        query = _build_row_count_query("fx_rates")
        assert "SELECT source" in query
        assert "FROM fx_rates" in query
        assert "COUNT(*)" in query
        assert "MIN(date)" in query
        assert "MAX(date)" in query
        assert "GROUP BY source" in query

    def test_row_count_query_economic(self):
        query = _build_row_count_query("economic_indicators")
        assert "FROM economic_indicators" in query

    def test_row_count_query_invalid_table(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            _build_row_count_query("drop; --")

    def test_null_check_fx_rates(self):
        query = _build_null_check_query("fx_rates")
        assert "FROM fx_rates" in query
        assert "rate IS NULL" in query
        assert "date IS NULL" in query

    def test_null_check_economic(self):
        query = _build_null_check_query("economic_indicators")
        assert "FROM economic_indicators" in query
        assert "value IS NULL" in query
        assert "date IS NULL" in query

    def test_null_check_invalid_table(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            _build_null_check_query("Robert'; DROP TABLE--")


# ---------------------------------------------------------------------------
# Result parsers
# ---------------------------------------------------------------------------
class TestParseRowCountResults:
    def test_valid_results(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
                {"Data": [{"VarCharValue": "ecb"}, {"VarCharValue": "440"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        parsed = _parse_row_count_results(result_set)
        assert len(parsed) == 2
        assert parsed["frankfurter"]["rows"] == 620
        assert parsed["frankfurter"]["min_date"] == "2024-01-02"
        assert parsed["ecb"]["rows"] == 440

    def test_empty_results(self):
        result_set = {"Rows": [
            {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                      {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
        ]}
        parsed = _parse_row_count_results(result_set)
        assert parsed == {}

    def test_no_rows_key(self):
        parsed = _parse_row_count_results({})
        assert parsed == {}


class TestParseNullCheckResults:
    def test_zero_nulls(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        count = _parse_null_check_results(result_set)
        assert count == 0

    def test_some_nulls(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "5"}]},
            ]
        }
        count = _parse_null_check_results(result_set)
        assert count == 5

    def test_empty_results(self):
        count = _parse_null_check_results({})
        assert count == -1

    def test_single_row_only(self):
        result_set = {"Rows": [{"Data": [{"VarCharValue": "null_rows"}]}]}
        count = _parse_null_check_results(result_set)
        assert count == -1


# ---------------------------------------------------------------------------
# ValidationCheck
# ---------------------------------------------------------------------------
class TestValidationCheck:
    def test_passed_check(self):
        check = ValidationCheck(
            domain="fx_rates",
            check_name="row_count",
            passed=True,
            detail="All sources present",
        )
        assert check.passed is True
        assert check.domain == "fx_rates"

    def test_failed_check(self):
        check = ValidationCheck(
            domain="fx_rates",
            check_name="null_check",
            passed=False,
            detail="Found 5 null rows",
        )
        assert check.passed is False


# ---------------------------------------------------------------------------
# _run_validation_suite
# ---------------------------------------------------------------------------
class TestRunValidationSuite:
    def _mock_athena(self, row_count_results, null_check_results):
        athena = MagicMock()
        execution_ids = iter(["qid-rc-1", "qid-nc-1", "qid-rc-2", "qid-nc-2"])
        athena.start_query_execution.side_effect = lambda **kw: {
            "QueryExecutionId": next(execution_ids)
        }
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        results_iter = iter([*row_count_results, *null_check_results])
        athena.get_query_results.side_effect = lambda **kw: {
            "ResultSet": next(results_iter)
        }
        return athena

    def test_all_pass(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={},
        )
        assert all(c.passed for c in checks)

    def test_null_rows_fail(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "3"}]},
            ]
        }
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={},
        )
        failed = [c for c in checks if not c.passed]
        assert len(failed) == 1
        assert failed[0].check_name == "null_check"

    def test_empty_table_fails_row_count(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={},
        )
        failed = [c for c in checks if not c.passed]
        assert len(failed) == 1
        assert failed[0].check_name == "row_count"

    def test_malformed_null_check_results(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {"Rows": []}
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={},
        )
        null_checks = [c for c in checks if c.check_name == "null_check"]
        assert len(null_checks) == 1
        assert null_checks[0].passed is False
        assert "Could not parse" in null_checks[0].detail

    def test_expected_count_mismatch(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={"fx_rates": {"frankfurter": 1000}},
        )
        failed = [c for c in checks if not c.passed]
        assert len(failed) == 1
        assert failed[0].check_name == "expected_count"

    def test_expected_count_match(self):
        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        athena = self._mock_athena([rc], [nc])
        checks = _run_validation_suite(
            athena_client=athena,
            domains=["fx_rates"],
            database="fxlake",
            output_location="s3://bucket/results/",
            workgroup="fxlake",
            expected_counts={"fx_rates": {"frankfurter": 620}},
        )
        assert all(c.passed for c in checks)

    def test_query_failure_raises(self):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "syntax error"}
            }
        }
        with pytest.raises(RuntimeError, match="FAILED"):
            _run_validation_suite(
                athena_client=athena,
                domains=["fx_rates"],
                database="fxlake",
                output_location="s3://bucket/results/",
                workgroup="fxlake",
                expected_counts={},
            )


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    @patch("lambda_data_validator.boto3")
    def test_all_pass(self, mock_boto3):
        athena = MagicMock()
        cloudwatch = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else cloudwatch
        )

        execution_ids = iter(["q1", "q2", "q3", "q4"])
        athena.start_query_execution.side_effect = lambda **kw: {
            "QueryExecutionId": next(execution_ids)
        }
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        rc_fx = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc_fx = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        rc_econ = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "fred"}, {"VarCharValue": "31"},
                          {"VarCharValue": "2024-01-01"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc_econ = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        results_iter = iter([rc_fx, nc_fx, rc_econ, nc_econ])
        athena.get_query_results.side_effect = lambda **kw: {
            "ResultSet": next(results_iter)
        }

        context = MagicMock()
        context.aws_request_id = "test-req-id"
        result = lambda_handler({}, context)
        assert result["status"] == "PASSED"
        assert result["passed"] is True
        assert result["checks_total"] == 4
        assert result["checks_passed"] == 4

        cloudwatch.put_metric_data.assert_called_once()
        call_args = cloudwatch.put_metric_data.call_args
        metric = call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "DataValidation"
        assert metric["Value"] == 1.0

    @patch("lambda_data_validator.boto3")
    def test_failure_publishes_zero_metric(self, mock_boto3):
        athena = MagicMock()
        cloudwatch = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else cloudwatch
        )

        athena.start_query_execution.return_value = {"QueryExecutionId": "q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        rc_fx = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
            ]
        }
        nc_fx = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        results_iter = iter([rc_fx, nc_fx])
        athena.get_query_results.side_effect = lambda **kw: {
            "ResultSet": next(results_iter)
        }

        context = MagicMock()
        context.aws_request_id = "test-req-id"
        result = lambda_handler({"domains": ["fx_rates"]}, context)
        assert result["status"] == "FAILED"
        assert result["passed"] is False

        call_args = cloudwatch.put_metric_data.call_args
        metric = call_args[1]["MetricData"][0]
        assert metric["Value"] == 0.0

    @patch("lambda_data_validator.boto3")
    def test_metric_publish_failure_does_not_raise(self, mock_boto3):
        from botocore.exceptions import ClientError

        athena = MagicMock()
        cloudwatch = MagicMock()
        cloudwatch.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "fail"}}, "PutMetricData"
        )
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else cloudwatch
        )

        athena.start_query_execution.return_value = {"QueryExecutionId": "q1"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        rc = {
            "Rows": [
                {"Data": [{"VarCharValue": "source"}, {"VarCharValue": "rows"},
                          {"VarCharValue": "min_date"}, {"VarCharValue": "max_date"}]},
                {"Data": [{"VarCharValue": "frankfurter"}, {"VarCharValue": "620"},
                          {"VarCharValue": "2024-01-02"}, {"VarCharValue": "2024-01-31"}]},
            ]
        }
        nc = {
            "Rows": [
                {"Data": [{"VarCharValue": "null_rows"}]},
                {"Data": [{"VarCharValue": "0"}]},
            ]
        }
        results_iter = iter([rc, nc])
        athena.get_query_results.side_effect = lambda **kw: {
            "ResultSet": next(results_iter)
        }

        context = MagicMock()
        context.aws_request_id = "test-req-id"
        result = lambda_handler({"domains": ["fx_rates"]}, context)
        assert result["status"] == "PASSED"
