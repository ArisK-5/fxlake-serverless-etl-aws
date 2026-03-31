import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import lambda_validation_function as validation


def _make_execution_response(state="SUCCEEDED", workgroup="fxlake"):
    return {
        "QueryExecution": {
            "Status": {"State": state},
            "WorkGroup": workgroup,
        }
    }


def _make_results_response(data_row_count: int):
    """Build a GetQueryResults response with a header row + N data rows."""
    header = {"Data": [{"VarCharValue": "col1"}]}
    rows = [header] + [
        {"Data": [{"VarCharValue": f"val{i}"}]} for i in range(data_row_count)
    ]
    return {"ResultSet": {"Rows": rows}}


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
            validation.publish_custom_metric(1, "fxlake")

        call_kwargs = mock_cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == "TestFXLake/Athena"
        metric = call_kwargs["MetricData"][0]
        assert metric["MetricName"] == "EmptyQueryResults"
        assert metric["Value"] == 1
        assert metric["Unit"] == "Count"
        dims = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
        assert dims["WorkGroup"] == "fxlake"
        assert dims["Pipeline"] == "fxlake-etl-test"

    def test_does_not_raise_on_cloudwatch_client_error(self, caplog):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = _make_client_error()
        with caplog.at_level(logging.ERROR):
            with patch.object(validation, "cloudwatch", mock_cw):
                validation.publish_custom_metric(0, "fxlake")

        assert "Failed to publish EmptyQueryResults metric" in caplog.text

    def test_non_client_error_does_not_propagate(self, caplog):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = TypeError("bad value")
        with caplog.at_level(logging.ERROR):
            with patch.object(validation, "cloudwatch", mock_cw):
                validation.publish_custom_metric(0, "fxlake")

        assert "TypeError: bad value" in caplog.text


# ---------------------------------------------------------------------------
# lambda_handler()
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    def test_succeeded_query_with_rows(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_results_response(5)
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["rows"] == 5
        assert result["is_empty"] is False
        assert result["status"] == "SUCCEEDED"
        metric_val = mock_cw.put_metric_data.call_args[1]["MetricData"][0]["Value"]
        assert metric_val == 0

    def test_succeeded_query_empty_results(self):
        mock_athena = MagicMock()
        mock_athena.get_query_execution.return_value = _make_execution_response()
        mock_athena.get_query_results.return_value = _make_results_response(0)
        mock_cw = MagicMock()

        with patch.object(validation, "athena", mock_athena), \
             patch.object(validation, "cloudwatch", mock_cw):
            result = validation.lambda_handler(
                {"QueryExecutionId": "abc-123"}, None
            )

        assert result["rows"] == 0
        assert result["is_empty"] is True
        assert result["status"] == "FAILED"
        metric_val = mock_cw.put_metric_data.call_args[1]["MetricData"][0]["Value"]
        assert metric_val == 1

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
