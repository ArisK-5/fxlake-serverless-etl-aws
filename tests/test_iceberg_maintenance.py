from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest


class TestMaintenanceResult:
    def test_fields(self):
        from lambda_iceberg_maintenance import MaintenanceResult

        result = MaintenanceResult(
            table="fx_rates_v3",
            operation="OPTIMIZE",
            success=True,
            duration_ms=1234.5,
            detail="Completed",
        )
        assert result.table == "fx_rates_v3"
        assert result.operation == "OPTIMIZE"
        assert result.success is True
        assert result.duration_ms == 1234.5
        assert result.detail == "Completed"

    def test_immutability(self):
        from lambda_iceberg_maintenance import MaintenanceResult

        result = MaintenanceResult(
            table="fx_rates_v3",
            operation="OPTIMIZE",
            success=True,
            duration_ms=100.0,
            detail="OK",
        )
        with pytest.raises(FrozenInstanceError):
            result.success = False


class TestExecuteStatement:
    def test_success(self):
        from lambda_iceberg_maintenance import _execute_statement

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-123"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        result = _execute_statement(
            mock_client,
            "OPTIMIZE fx_rates_v3 REWRITE DATA USING BIN_PACK",
            "fxlake",
            "s3://bucket/results/",
            "fxlake",
        )
        assert result is True
        mock_client.start_query_execution.assert_called_once()

    def test_failure(self):
        from lambda_iceberg_maintenance import _execute_statement

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-456"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED",
                    "StateChangeReason": "Table not found",
                }
            }
        }

        result = _execute_statement(
            mock_client,
            "OPTIMIZE missing_table REWRITE DATA USING BIN_PACK",
            "fxlake",
            "s3://bucket/results/",
            "fxlake",
        )
        assert result is False

    def test_cancelled(self):
        from lambda_iceberg_maintenance import _execute_statement

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-789"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "CANCELLED"}}
        }

        result = _execute_statement(
            mock_client,
            "VACUUM fx_rates_v3",
            "fxlake",
            "s3://bucket/results/",
            "fxlake",
        )
        assert result is False

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    @patch("lambda_iceberg_maintenance.MAX_POLL_ATTEMPTS", 2)
    def test_timeout(self):
        from lambda_iceberg_maintenance import _execute_statement

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-timeout"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "RUNNING"}}
        }

        result = _execute_statement(
            mock_client,
            "OPTIMIZE fx_rates_v3 REWRITE DATA USING BIN_PACK",
            "fxlake",
            "s3://bucket/results/",
            "fxlake",
        )
        assert result is False
        assert mock_client.get_query_execution.call_count == 2

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    def test_polling(self):
        from lambda_iceberg_maintenance import _execute_statement

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-poll"
        }
        mock_client.get_query_execution.side_effect = [
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "RUNNING"}}},
            {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        ]

        result = _execute_statement(
            mock_client,
            "OPTIMIZE fx_rates_v3 REWRITE DATA USING BIN_PACK",
            "fxlake",
            "s3://bucket/results/",
            "fxlake",
        )
        assert result is True
        assert mock_client.get_query_execution.call_count == 3


class TestRunMaintenance:
    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    def test_all_tables(self):
        from lambda_iceberg_maintenance import TABLES, _run_maintenance

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-all"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        results = _run_maintenance(
            mock_client, "fxlake", "s3://bucket/results/", "fxlake"
        )
        assert len(results) == len(TABLES) * 2
        assert all(r.success for r in results)
        operations = {r.operation for r in results}
        assert operations == {"OPTIMIZE", "VACUUM"}

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    def test_failure_does_not_abort(self):
        from lambda_iceberg_maintenance import TABLES, _run_maintenance

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-mix"
        }
        call_count = {"n": 0}

        def alternate_results(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "QueryExecution": {
                        "Status": {
                            "State": "FAILED",
                            "StateChangeReason": "Error",
                        }
                    }
                }
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        mock_client.get_query_execution.side_effect = alternate_results

        results = _run_maintenance(
            mock_client, "fxlake", "s3://bucket/results/", "fxlake"
        )
        assert len(results) == len(TABLES) * 2
        failed = [r for r in results if not r.success]
        succeeded = [r for r in results if r.success]
        assert len(failed) >= 1
        assert len(succeeded) >= 1

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    def test_covers_all_tables(self):
        from lambda_iceberg_maintenance import TABLES, _run_maintenance

        mock_client = MagicMock()
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-cov"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        results = _run_maintenance(
            mock_client, "fxlake", "s3://bucket/results/", "fxlake"
        )
        tables_maintained = {r.table for r in results}
        assert tables_maintained == set(TABLES)


class TestLambdaHandler:
    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    @patch("lambda_iceberg_maintenance.boto3")
    def test_all_succeed(self, mock_boto3):
        from lambda_iceberg_maintenance import lambda_handler

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-h1"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        result = lambda_handler({}, MagicMock())
        assert result["status"] == "success"
        assert result["failed_count"] == 0

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    @patch("lambda_iceberg_maintenance.boto3")
    def test_partial_failure(self, mock_boto3):
        from lambda_iceberg_maintenance import lambda_handler

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-h2"
        }
        call_count = {"n": 0}

        def alternate(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "QueryExecution": {
                        "Status": {
                            "State": "FAILED",
                            "StateChangeReason": "Err",
                        }
                    }
                }
            return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        mock_client.get_query_execution.side_effect = alternate

        result = lambda_handler({}, MagicMock())
        assert result["status"] == "partial_failure"
        assert result["failed_count"] >= 1

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    @patch("lambda_iceberg_maintenance.boto3")
    def test_metric_publish_failure_swallowed(self, mock_boto3):
        from lambda_iceberg_maintenance import lambda_handler

        mock_athena = MagicMock()
        mock_cw = MagicMock()

        def client_factory(service, **kwargs):
            if service == "athena":
                return mock_athena
            return mock_cw

        mock_boto3.client.side_effect = client_factory
        mock_athena.start_query_execution.return_value = {
            "QueryExecutionId": "qid-h3"
        }
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }
        from botocore.exceptions import ClientError

        mock_cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "fail"}},
            "PutMetricData",
        )

        result = lambda_handler({}, MagicMock())
        assert result["status"] == "success"

    @patch("lambda_iceberg_maintenance.POLL_INTERVAL_SECONDS", 0)
    @patch("lambda_iceberg_maintenance.boto3")
    def test_custom_tables_via_event(self, mock_boto3):
        from lambda_iceberg_maintenance import lambda_handler

        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.start_query_execution.return_value = {
            "QueryExecutionId": "qid-h4"
        }
        mock_client.get_query_execution.return_value = {
            "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
        }

        result = lambda_handler(
            {"tables": ["fx_rates_v3"]}, MagicMock()
        )
        assert result["total_operations"] == 2
        assert all("fx_rates_v3" in r["table"] for r in result["results"])
