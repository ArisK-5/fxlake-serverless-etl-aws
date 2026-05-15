import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("ATHENA_RESULTS_BUCKET", "test-athena-results")

from lambda_anomaly_detector import (
    ALERT_THRESHOLD,
    WARNING_THRESHOLD,
    AnomalyCheck,
    _build_checks,
    build_economic_stats_query,
    build_fx_stats_query,
    classify_severity,
    compute_z_score,
    lambda_handler,
    parse_stats_results,
)


class TestAnomalyCheck:
    def test_frozen(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=0.5,
            severity="NORMAL",
            latest_value=1.08,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        with pytest.raises(AttributeError):
            check.severity = "ALERT"

    def test_valid_normal(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=1.5,
            severity="NORMAL",
            latest_value=1.08,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "NORMAL"

    def test_valid_warning(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=2.5,
            severity="WARNING",
            latest_value=1.12,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "WARNING"

    def test_valid_alert(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=3.5,
            severity="ALERT",
            latest_value=1.14,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "ALERT"

    def test_rejects_invalid_severity(self):
        with pytest.raises(ValueError, match="Invalid severity"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=1.0,
                severity="CRITICAL",
                latest_value=1.08,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_rejects_negative_z_score(self):
        with pytest.raises(ValueError, match="z_score cannot be negative"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=-0.5,
                severity="NORMAL",
                latest_value=1.08,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_rejects_alert_with_low_z_score(self):
        with pytest.raises(ValueError, match="ALERT severity requires"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=2.5,
                severity="ALERT",
                latest_value=1.08,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_rejects_normal_with_high_z_score(self):
        with pytest.raises(ValueError, match="NORMAL severity requires"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=2.5,
                severity="NORMAL",
                latest_value=1.08,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_rejects_warning_with_alert_z_score(self):
        with pytest.raises(ValueError, match="WARNING severity requires"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=3.5,
                severity="WARNING",
                latest_value=1.14,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_rejects_warning_with_normal_z_score(self):
        with pytest.raises(ValueError, match="WARNING severity requires"):
            AnomalyCheck(
                domain="fx_rates",
                entity="EUR/USD",
                z_score=1.5,
                severity="WARNING",
                latest_value=1.10,
                mean_value=1.07,
                stddev_value=0.02,
                sample_count=30,
            )

    def test_boundary_z_score_at_alert_threshold(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=ALERT_THRESHOLD,
            severity="ALERT",
            latest_value=1.13,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "ALERT"

    def test_boundary_z_score_at_warning_threshold(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=WARNING_THRESHOLD,
            severity="WARNING",
            latest_value=1.11,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "WARNING"

    def test_boundary_z_score_just_below_warning(self):
        check = AnomalyCheck(
            domain="fx_rates",
            entity="EUR/USD",
            z_score=1.999,
            severity="NORMAL",
            latest_value=1.10,
            mean_value=1.07,
            stddev_value=0.02,
            sample_count=30,
        )
        assert check.severity == "NORMAL"


class TestComputeZScore:
    def test_normal_deviation(self):
        z = compute_z_score(1.10, 1.08, 0.02)
        assert z == pytest.approx(1.0)

    def test_zero_stddev_returns_zero(self):
        assert compute_z_score(1.10, 1.08, 0.0) == 0.0

    def test_exact_mean_returns_zero(self):
        assert compute_z_score(1.08, 1.08, 0.02) == 0.0

    def test_negative_deviation(self):
        z = compute_z_score(1.06, 1.08, 0.02)
        assert z == pytest.approx(1.0)

    def test_large_deviation(self):
        z = compute_z_score(1.20, 1.08, 0.02)
        assert z == pytest.approx(6.0)


class TestClassifySeverity:
    def test_alert(self):
        assert classify_severity(3.5) == "ALERT"

    def test_alert_at_threshold(self):
        assert classify_severity(ALERT_THRESHOLD) == "ALERT"

    def test_warning(self):
        assert classify_severity(2.5) == "WARNING"

    def test_warning_at_threshold(self):
        assert classify_severity(WARNING_THRESHOLD) == "WARNING"

    def test_normal(self):
        assert classify_severity(1.5) == "NORMAL"

    def test_zero(self):
        assert classify_severity(0.0) == "NORMAL"


class TestBuildQueries:
    def test_fx_stats_query(self):
        query = build_fx_stats_query("fx_rates")
        assert "AVG(rate)" in query
        assert "STDDEV(rate)" in query or "stddev" in query.lower()
        assert "fx_rates" in query
        assert "target_currency" in query

    def test_fx_stats_query_invalid_table(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            build_fx_stats_query("bad; DROP TABLE")

    def test_economic_stats_query(self):
        query = build_economic_stats_query("economic_indicators")
        assert "AVG(value)" in query
        assert "STDDEV(value)" in query or "stddev" in query.lower()
        assert "economic_indicators" in query
        assert "series_id" in query

    def test_economic_stats_query_invalid_table(self):
        with pytest.raises(ValueError, match="Invalid table name"):
            build_economic_stats_query("bad; DROP TABLE")


class TestParseStatsResults:
    def test_empty_rows(self):
        assert parse_stats_results({"Rows": []}) == []

    def test_header_only(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "entity"}, {"VarCharValue": "latest_value"},
                          {"VarCharValue": "mean_val"}, {"VarCharValue": "stddev_val"},
                          {"VarCharValue": "sample_count"}, {"VarCharValue": "z_score"}]}
            ]
        }
        assert parse_stats_results(result_set) == []

    def test_valid_rows(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "h"}] * 6},
                {"Data": [
                    {"VarCharValue": "USD"},
                    {"VarCharValue": "1.0800"},
                    {"VarCharValue": "1.0700"},
                    {"VarCharValue": "0.0200"},
                    {"VarCharValue": "30"},
                    {"VarCharValue": "0.5000"},
                ]},
                {"Data": [
                    {"VarCharValue": "GBP"},
                    {"VarCharValue": "0.8600"},
                    {"VarCharValue": "0.8500"},
                    {"VarCharValue": "0.0100"},
                    {"VarCharValue": "28"},
                    {"VarCharValue": "1.0000"},
                ]},
            ]
        }
        parsed = parse_stats_results(result_set)
        assert len(parsed) == 2
        assert parsed[0]["entity"] == "USD"
        assert parsed[0]["latest_value"] == pytest.approx(1.08)
        assert parsed[0]["z_score"] == pytest.approx(0.5)
        assert parsed[1]["entity"] == "GBP"

    def test_short_row_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "h"}] * 6},
                {"Data": [{"VarCharValue": "short"}]},
            ]
        }
        assert parse_stats_results(result_set) == []

    def test_missing_varcharvalue_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "h"}] * 6},
                {"Data": [
                    {"VarCharValue": "USD"},
                    {},
                    {"VarCharValue": "1.07"},
                    {"VarCharValue": "0.02"},
                    {"VarCharValue": "30"},
                    {"VarCharValue": "0.5"},
                ]},
            ]
        }
        assert parse_stats_results(result_set) == []

    def test_invalid_float_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "h"}] * 6},
                {"Data": [
                    {"VarCharValue": "USD"},
                    {"VarCharValue": "not_a_number"},
                    {"VarCharValue": "1.07"},
                    {"VarCharValue": "0.02"},
                    {"VarCharValue": "30"},
                    {"VarCharValue": "0.5"},
                ]},
            ]
        }
        assert parse_stats_results(result_set) == []

    def test_insufficient_samples_skipped(self):
        result_set = {
            "Rows": [
                {"Data": [{"VarCharValue": "h"}] * 6},
                {"Data": [
                    {"VarCharValue": "USD"},
                    {"VarCharValue": "1.08"},
                    {"VarCharValue": "1.07"},
                    {"VarCharValue": "0.02"},
                    {"VarCharValue": "3"},
                    {"VarCharValue": "0.5"},
                ]},
            ]
        }
        parsed = parse_stats_results(result_set)
        assert len(parsed) == 0


class TestBuildChecks:
    def test_builds_correct_checks(self):
        stats = [
            {
                "entity": "USD",
                "latest_value": 1.15,
                "mean_value": 1.07,
                "stddev_value": 0.02,
                "sample_count": 30,
                "z_score": 4.0,
            },
            {
                "entity": "GBP",
                "latest_value": 0.86,
                "mean_value": 0.85,
                "stddev_value": 0.01,
                "sample_count": 28,
                "z_score": 1.0,
            },
        ]
        checks = _build_checks("fx_rates", stats)
        assert len(checks) == 2
        assert checks[0].domain == "fx_rates"
        assert checks[0].entity == "USD"
        assert checks[0].severity == "ALERT"
        assert checks[1].severity == "NORMAL"

    def test_empty_stats(self):
        checks = _build_checks("fx_rates", [])
        assert checks == []

    def test_warning_severity(self):
        stats = [
            {
                "entity": "JPY",
                "latest_value": 155.0,
                "mean_value": 150.0,
                "stddev_value": 2.0,
                "sample_count": 30,
                "z_score": 2.5,
            },
        ]
        checks = _build_checks("fx_rates", stats)
        assert len(checks) == 1
        assert checks[0].severity == "WARNING"

    def test_multi_domain(self):
        fx_stats = [
            {
                "entity": "USD",
                "latest_value": 1.15,
                "mean_value": 1.07,
                "stddev_value": 0.02,
                "sample_count": 30,
                "z_score": 4.0,
            },
        ]
        econ_stats = [
            {
                "entity": "UNRATE",
                "latest_value": 8.5,
                "mean_value": 4.0,
                "stddev_value": 1.0,
                "sample_count": 30,
                "z_score": 4.5,
            },
        ]
        fx_checks = _build_checks("fx_rates", fx_stats)
        econ_checks = _build_checks("economic_indicators", econ_stats)
        all_checks = fx_checks + econ_checks
        assert len(all_checks) == 2
        assert all_checks[0].domain == "fx_rates"
        assert all_checks[1].domain == "economic_indicators"
        assert all(c.severity == "ALERT" for c in all_checks)


def _make_athena_mock(result_set: dict) -> MagicMock:
    mock = MagicMock()
    mock.start_query_execution.return_value = {"QueryExecutionId": "qid-123"}
    mock.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    mock.get_query_results.return_value = {"ResultSet": result_set}
    return mock


def _make_normal_fx_result() -> dict:
    return {
        "Rows": [
            {"Data": [{"VarCharValue": "h"}] * 6},
            {"Data": [
                {"VarCharValue": "USD"},
                {"VarCharValue": "1.0800"},
                {"VarCharValue": "1.0750"},
                {"VarCharValue": "0.0200"},
                {"VarCharValue": "30"},
                {"VarCharValue": "0.2500"},
            ]},
        ]
    }


def _make_anomaly_fx_result() -> dict:
    return {
        "Rows": [
            {"Data": [{"VarCharValue": "h"}] * 6},
            {"Data": [
                {"VarCharValue": "USD"},
                {"VarCharValue": "1.1500"},
                {"VarCharValue": "1.0700"},
                {"VarCharValue": "0.0200"},
                {"VarCharValue": "30"},
                {"VarCharValue": "4.0000"},
            ]},
        ]
    }


def _make_warning_fx_result() -> dict:
    return {
        "Rows": [
            {"Data": [{"VarCharValue": "h"}] * 6},
            {"Data": [
                {"VarCharValue": "USD"},
                {"VarCharValue": "1.1200"},
                {"VarCharValue": "1.0700"},
                {"VarCharValue": "0.0200"},
                {"VarCharValue": "30"},
                {"VarCharValue": "2.5000"},
            ]},
        ]
    }


def _make_empty_result() -> dict:
    return {"Rows": [{"Data": [{"VarCharValue": "h"}] * 6}]}


class TestLambdaHandler:
    @patch("lambda_anomaly_detector.boto3")
    def test_normal_data_no_anomalies(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_normal_fx_result()},
            {"ResultSet": _make_normal_fx_result()},
        ]
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-normal"

        result = lambda_handler({}, context)
        assert result["status"] == "PASSED"
        assert result["anomalies_total"] == 0
        assert result["alerts_total"] == 0
        assert result["warnings_total"] == 0
        sns.publish.assert_not_called()

    @patch("lambda_anomaly_detector.boto3")
    def test_anomaly_detected_triggers_alert(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_anomaly_fx_result()},
            {"ResultSet": _make_empty_result()},
        ]
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-alert"

        with patch("lambda_anomaly_detector.SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:alerts"):
            result = lambda_handler({}, context)

        assert result["status"] == "ALERT"
        assert result["alerts_total"] >= 1
        sns.publish.assert_called_once()
        publish_args = sns.publish.call_args
        assert "Anomaly" in publish_args[1]["Subject"]

    @patch("lambda_anomaly_detector.boto3")
    def test_warning_no_sns(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_warning_fx_result()},
            {"ResultSet": _make_empty_result()},
        ]
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-warn"

        result = lambda_handler({}, context)
        assert result["status"] == "WARNING"
        assert result["warnings_total"] >= 1
        assert result["alerts_total"] == 0
        sns.publish.assert_not_called()

    @patch("lambda_anomaly_detector.boto3")
    def test_cold_start_graceful_skip(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_empty_result()},
            {"ResultSet": _make_empty_result()},
        ]
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-cold"

        result = lambda_handler({}, context)
        assert result["status"] == "PASSED"
        assert result["anomalies_total"] == 0
        assert "cold_start" in result or result["checks_total"] == 0

    @patch("lambda_anomaly_detector.boto3")
    def test_custom_database(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.return_value = {"ResultSet": _make_empty_result()}
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-db"

        lambda_handler({"database_name": "custom_db"}, context)

        calls = athena.start_query_execution.call_args_list
        for call in calls:
            assert call[1]["QueryExecutionContext"]["Database"] == "custom_db"

    @patch("lambda_anomaly_detector.boto3")
    def test_metrics_published(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_normal_fx_result()},
            {"ResultSet": _make_empty_result()},
        ]
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-metrics"

        lambda_handler({}, context)
        cw.put_metric_data.assert_called_once()
        call_args = cw.put_metric_data.call_args
        metric_data = call_args[1]["MetricData"]
        metric_names = [m["MetricName"] for m in metric_data]
        assert "AnomalyDetected" in metric_names
        assert "ZScore" in metric_names


class TestLambdaHandlerErrors:
    @patch("lambda_anomaly_detector.boto3")
    def test_athena_error_propagates(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED",
                    "StateChangeReason": "table not found",
                }
            }
        }
        cw = MagicMock()
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-err"

        with pytest.raises(RuntimeError, match="table not found"):
            lambda_handler({}, context)

    @patch("lambda_anomaly_detector.boto3")
    def test_metric_publish_error_propagates(self, mock_boto3):
        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.side_effect = [
            {"ResultSet": _make_normal_fx_result()},
            {"ResultSet": _make_empty_result()},
        ]
        cw = MagicMock()
        cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "fail"}},
            "PutMetricData",
        )
        sns = MagicMock()
        mock_boto3.client.side_effect = lambda svc: (
            athena if svc == "athena" else sns if svc == "sns" else cw
        )

        context = MagicMock()
        context.aws_request_id = "req-metric-err"

        with pytest.raises(ClientError):
            lambda_handler({}, context)


class TestAthenaPolling:
    def test_query_failure_raises(self):
        from lambda_anomaly_detector import _execute_and_wait

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

    def test_missing_result_set_raises(self):
        from lambda_anomaly_detector import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-no-rs"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        athena.get_query_results.return_value = {}
        with pytest.raises(RuntimeError, match="no ResultSet"):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    def test_cancelled_raises(self):
        from lambda_anomaly_detector import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-cancel"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "CANCELLED"}}
        }
        with pytest.raises(RuntimeError, match="CANCELLED"):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    @patch("lambda_anomaly_detector.time.sleep")
    def test_timeout_raises(self, mock_sleep):
        from lambda_anomaly_detector import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-timeout"}
        athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "RUNNING"}}
        }
        with pytest.raises(TimeoutError):
            _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")

    @patch("lambda_anomaly_detector.time.sleep")
    def test_polls_then_succeeds(self, mock_sleep):
        from lambda_anomaly_detector import _execute_and_wait

        athena = MagicMock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "qid-poll"}
        athena.get_query_execution.side_effect = [
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        ]
        athena.get_query_results.return_value = {"ResultSet": {"Rows": []}}
        result = _execute_and_wait(athena, "SELECT 1", "db", "s3://b/", "wg")
        assert result == {"Rows": []}
        assert mock_sleep.call_count == 2


class TestPublishMetrics:
    def test_publish_with_anomalies(self):
        from lambda_anomaly_detector import _publish_anomaly_metrics

        cw = MagicMock()
        checks = [
            AnomalyCheck("fx_rates", "USD", 3.5, "ALERT", 1.15, 1.07, 0.02, 30),
            AnomalyCheck("fx_rates", "GBP", 0.5, "NORMAL", 0.86, 0.85, 0.01, 30),
        ]
        _publish_anomaly_metrics(cw, checks)
        cw.put_metric_data.assert_called_once()
        call_args = cw.put_metric_data.call_args
        metric_data = call_args[1]["MetricData"]
        anomaly_metric = next(m for m in metric_data if m["MetricName"] == "AnomalyDetected")
        assert anomaly_metric["Value"] == 1.0
        zscore_metric = next(m for m in metric_data if m["MetricName"] == "ZScore")
        assert zscore_metric["Value"] == 3.5

    def test_publish_no_anomalies(self):
        from lambda_anomaly_detector import _publish_anomaly_metrics

        cw = MagicMock()
        checks = [
            AnomalyCheck("fx_rates", "USD", 0.5, "NORMAL", 1.08, 1.07, 0.02, 30),
        ]
        _publish_anomaly_metrics(cw, checks)
        call_args = cw.put_metric_data.call_args
        metric_data = call_args[1]["MetricData"]
        anomaly_metric = next(m for m in metric_data if m["MetricName"] == "AnomalyDetected")
        assert anomaly_metric["Value"] == 0.0

    def test_publish_empty_checks(self):
        from lambda_anomaly_detector import _publish_anomaly_metrics

        cw = MagicMock()
        _publish_anomaly_metrics(cw, [])
        cw.put_metric_data.assert_called_once()

    def test_publish_client_error_propagates(self):
        from lambda_anomaly_detector import _publish_anomaly_metrics

        cw = MagicMock()
        cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "fail"}},
            "PutMetricData",
        )
        with pytest.raises(ClientError):
            _publish_anomaly_metrics(cw, [])


class TestSNSNotification:
    def test_alert_sends_sns(self):
        from lambda_anomaly_detector import _send_alert_notification

        sns = MagicMock()
        checks = [
            AnomalyCheck("fx_rates", "USD", 3.5, "ALERT", 1.15, 1.07, 0.02, 30),
        ]
        _send_alert_notification(sns, "arn:aws:sns:us-east-1:123:alerts", checks)
        sns.publish.assert_called_once()
        call_args = sns.publish.call_args
        assert "Anomaly" in call_args[1]["Subject"]
        assert "USD" in call_args[1]["Message"]

    def test_no_alerts_no_sns(self):
        from lambda_anomaly_detector import _send_alert_notification

        sns = MagicMock()
        checks = [
            AnomalyCheck("fx_rates", "USD", 1.5, "NORMAL", 1.08, 1.07, 0.02, 30),
        ]
        _send_alert_notification(sns, "arn:aws:sns:us-east-1:123:alerts", checks)
        sns.publish.assert_not_called()

    def test_no_topic_arn_skips(self):
        from lambda_anomaly_detector import _send_alert_notification

        sns = MagicMock()
        checks = [
            AnomalyCheck("fx_rates", "USD", 3.5, "ALERT", 1.15, 1.07, 0.02, 30),
        ]
        _send_alert_notification(sns, None, checks)
        sns.publish.assert_not_called()

    def test_sns_client_error_propagates(self):
        from lambda_anomaly_detector import _send_alert_notification

        sns = MagicMock()
        sns.publish.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "fail"}},
            "Publish",
        )
        checks = [
            AnomalyCheck("fx_rates", "USD", 3.5, "ALERT", 1.15, 1.07, 0.02, 30),
        ]
        with pytest.raises(ClientError):
            _send_alert_notification(sns, "arn:aws:sns:us-east-1:123:alerts", checks)
