import logging
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import lambda_validation_function as validation
import pytest
from botocore.exceptions import ClientError


def _make_execution_response(state="SUCCEEDED", workgroup="fxlake"):
    return {
        "QueryExecution": {
            "Status": {"State": state},
            "WorkGroup": workgroup,
        }
    }


def _make_freshness_response(latest_date: str, total_records: int):
    """Build a GetQueryResults response for the freshness query.

    Schema: ``SELECT MAX(date) AS latest_date, COUNT(*) AS total_records``
    Row 0 = header, Row 1 = data.
    """
    header = {"Data": [{"VarCharValue": "latest_date"}, {"VarCharValue": "total_records"}]}
    data = {"Data": [{"VarCharValue": latest_date}, {"VarCharValue": str(total_records)}]}
    return {"ResultSet": {"Rows": [header, data]}}


def _make_empty_response():
    """Build a GetQueryResults response where the table is empty (NULL latest_date, 0 records)."""
    header = {"Data": [{"VarCharValue": "latest_date"}, {"VarCharValue": "total_records"}]}
    data = {"Data": [{}, {"VarCharValue": "0"}]}
    return {"ResultSet": {"Rows": [header, data]}}


def _make_client_error(code="InternalError", message="error"):
    return ClientError(
        {"Error": {"Code": code, "Message": message}}, "TestOperation"
    )


# ---------------------------------------------------------------------------
# publish_custom_metric()
# ---------------------------------------------------------------------------
class TestPublishCustomMetric:
    def test_publishes_correct_metric_data(self):
        mock_cw = MagicMock()
        with patch.object(validation, "cloudwatch", mock_cw):
            validation.publish_custom_metric("EmptyQueryResults", 1, "fxlake")

        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "TestFXLake/Athena"
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "EmptyQueryResults"
        assert metric["Value"] == 1
        assert metric["Unit"] == "Count"
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["WorkGroup"] == "fxlake"
        assert dims["Pipeline"] == "fxlake-etl-test"

    def test_publishes_stale_metric(self):
        mock_cw = MagicMock()
        with patch.object(validation, "cloudwatch", mock_cw):
            validation.publish_custom_metric("StaleFXData", 1, "fxlake")

        metric = mock_cw.put_metric_data.call_args[1]["MetricData"][0]
        assert metric["MetricName"] == "StaleFXData"

    def test_does_not_raise_on_cloudwatch_client_error(self, caplog):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = _make_client_error()
        with caplog.at_level(logging.ERROR):
            with patch.object(validation, "cloudwatch", mock_cw):
                validation.publish_custom_metric("EmptyQueryResults", 0, "fxlake")

        assert "Failed to publish CloudWatch metric" in caplog.text

    def test_non_client_error_does_not_propagate(self, caplog):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = TypeError("bad value")
        with caplog.at_level(logging.ERROR):
            with patch.object(validation, "cloudwatch", mock_cw):
                validation.publish_custom_metric("EmptyQueryResults", 0, "fxlake")

        assert "TypeError: bad value" in caplog.text


# ---------------------------------------------------------------------------
# lambda_handler()
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    def test_fresh_data_returns_success(self):
        """Recent latest_date (today) → is_fresh=True, status=SUCCEEDED."""
        today = date.today().isoformat()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_freshness_response(today, 500)
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["latest_date"] == today
        assert result["total_records"] == 500
        assert result["is_fresh"] is True
        assert result["status"] == "SUCCEEDED"
        # EmptyQueryResults=0 (has records) and StaleFXData=0 (fresh)
        calls = mock_cw.put_metric_data.call_args_list
        metric_names = [c[1]["MetricData"][0]["MetricName"] for c in calls]
        assert "EmptyQueryResults" in metric_names
        assert "StaleFXData" in metric_names
        empty_val = next(
            c[1]["MetricData"][0]["Value"] for c in calls
            if c[1]["MetricData"][0]["MetricName"] == "EmptyQueryResults"
        )
        stale_val = next(
            c[1]["MetricData"][0]["Value"] for c in calls
            if c[1]["MetricData"][0]["MetricName"] == "StaleFXData"
        )
        assert empty_val == 0
        assert stale_val == 0

    def test_stale_data_publishes_metric(self):
        """latest_date 5 days ago → is_fresh=False, StaleFXData=1."""
        stale_date = (date.today() - timedelta(days=5)).isoformat()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_freshness_response(
            stale_date, 100
        )
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["latest_date"] == stale_date
        assert result["is_fresh"] is False
        assert result["status"] == "SUCCEEDED"
        calls = mock_cw.put_metric_data.call_args_list
        stale_val = next(
            c[1]["MetricData"][0]["Value"] for c in calls
            if c[1]["MetricData"][0]["MetricName"] == "StaleFXData"
        )
        assert stale_val == 1

    def test_boundary_two_days_is_fresh(self):
        """latest_date exactly 2 days ago → still fresh."""
        boundary_date = (date.today() - timedelta(days=2)).isoformat()
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_freshness_response(
            boundary_date, 50
        )
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["is_fresh"] is True

    def test_empty_table_returns_failed(self):
        """Empty table (NULL latest_date, 0 records) → is_empty=True, status=FAILED."""
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_empty_response()
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["total_records"] == 0
        assert result["is_empty"] is True
        assert result["is_fresh"] is False
        assert result["status"] == "FAILED"
        calls = mock_cw.put_metric_data.call_args_list
        empty_val = next(
            c[1]["MetricData"][0]["Value"] for c in calls
            if c[1]["MetricData"][0]["MetricName"] == "EmptyQueryResults"
        )
        assert empty_val == 1

    def test_failed_query_raises(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response(
            state="FAILED"
        )

        with patch.object(validation, "athena", mock_athena):
            with pytest.raises(RuntimeError, match="did not succeed"):
                validation.lambda_handler(
                    {"QueryExecutionId": "abc-123"}, None
                )

    @pytest.mark.parametrize("state", ["RUNNING", "QUEUED"])
    def test_non_terminal_state_raises(self, state):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response(
            state=state
        )

        with patch.object(validation, "athena", mock_athena):
            with pytest.raises(RuntimeError, match="did not succeed"):
                validation.lambda_handler(
                    {"QueryExecutionId": "abc-123"}, None
                )

    def test_get_query_execution_client_error_raises(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.side_effect = _make_client_error(
            "InvalidRequestException", "Query not found"
        )

        with patch.object(validation, "athena", mock_athena):
            with pytest.raises(ClientError):
                validation.lambda_handler(
                    {"QueryExecutionId": "bad-id"}, None
                )

    def test_get_query_results_client_error_raises(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.side_effect = _make_client_error(
            "AccessDeniedException", "Not authorized"
        )

        with patch.object(validation, "athena", mock_athena):
            with pytest.raises(ClientError):
                validation.lambda_handler(
                    {"QueryExecutionId": "abc-123"}, None
                )

    def test_missing_execution_id_raises(self):
        with pytest.raises(ValueError, match="Missing QueryExecutionId"):
            validation.lambda_handler({}, None)

    def test_none_execution_id_raises(self):
        with pytest.raises(ValueError, match="Missing QueryExecutionId"):
            validation.lambda_handler({"QueryExecutionId": None}, None)

    def test_malformed_athena_row_returns_empty(self):
        """Athena row missing 'Data' key should be treated as empty."""
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "latest_date"}, {"VarCharValue": "total_records"}]},
                    {},  # malformed row — no "Data" key
                ]
            }
        }
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["is_empty"] is True
        assert result["status"] == "FAILED"
