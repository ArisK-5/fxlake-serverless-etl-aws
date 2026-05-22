import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault(
    "STATE_MACHINE_ARN",
    "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl",
)
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:fxlake-alerts")

from lambda_dlq_auto_retry import (
    FailureClassification,
    _publish_metric,
    _send_permanent_failure_alert,
    classify_failure,
    extract_execution_input,
    is_message_stale,
    lambda_handler,
    replay_execution,
)

SFN_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl"
EXEC_ARN = "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl:daily-2024-01-15"


def _make_sqs_event(
    detail: dict,
    receive_count: int = 1,
    sent_timestamp_ms: int | None = None,
) -> dict:
    body = json.dumps({
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": detail,
    })
    if sent_timestamp_ms is None:
        sent_timestamp_ms = int(time.time() * 1000)
    return {
        "Records": [{
            "messageId": "msg-001",
            "receiptHandle": "receipt-001",
            "body": body,
            "attributes": {
                "ApproximateReceiveCount": str(receive_count),
                "SentTimestamp": str(sent_timestamp_ms),
            },
        }],
    }


def _make_detail(
    status: str = "FAILED",
    cause: str = "ThrottlingException",
    execution_arn: str = EXEC_ARN,
    input_data: dict | None = None,
) -> dict:
    return {
        "executionArn": execution_arn,
        "status": status,
        "input": json.dumps(input_data or {}),
        "cause": cause,
    }


# ---------------------------------------------------------------------------
# FailureClassification dataclass
# ---------------------------------------------------------------------------
class TestFailureClassification:
    def test_frozen(self):
        fc = FailureClassification(
            is_transient=True, error_code="ThrottlingException", retry_count=1,
        )
        with pytest.raises(AttributeError):
            fc.is_transient = False

    def test_valid_transient(self):
        fc = FailureClassification(
            is_transient=True, error_code="ThrottlingException", retry_count=2,
        )
        assert fc.is_transient is True
        assert fc.retry_count == 2

    def test_valid_permanent(self):
        fc = FailureClassification(
            is_transient=False, error_code="ValidationException", retry_count=0,
        )
        assert fc.is_transient is False

    def test_negative_retry_count_rejected(self):
        with pytest.raises(ValueError, match="retry_count cannot be negative"):
            FailureClassification(
                is_transient=True, error_code="Throttle", retry_count=-1,
            )

    def test_empty_error_code_rejected(self):
        with pytest.raises(ValueError, match="error_code cannot be empty"):
            FailureClassification(
                is_transient=False, error_code="", retry_count=0,
            )


# ---------------------------------------------------------------------------
# is_message_stale
# ---------------------------------------------------------------------------
class TestIsMessageStale:
    def test_fresh_message_is_not_stale(self):
        record = {
            "attributes": {"SentTimestamp": str(int(time.time() * 1000))},
        }
        assert is_message_stale(record) is False

    def test_old_message_is_stale(self):
        two_days_ago_ms = int((time.time() - 48 * 3600) * 1000)
        record = {
            "attributes": {"SentTimestamp": str(two_days_ago_ms)},
        }
        assert is_message_stale(record) is True

    def test_boundary_just_under_threshold_is_not_stale(self):
        just_under_24h_ms = int((time.time() - 23.9 * 3600) * 1000)
        record = {
            "attributes": {"SentTimestamp": str(just_under_24h_ms)},
        }
        assert is_message_stale(record) is False

    def test_missing_sent_timestamp_defaults_to_not_stale(self):
        record = {"attributes": {}}
        assert is_message_stale(record) is False

    def test_missing_attributes_defaults_to_not_stale(self):
        record = {}
        assert is_message_stale(record) is False

    def test_zero_sent_timestamp_defaults_to_not_stale(self):
        record = {"attributes": {"SentTimestamp": "0"}}
        assert is_message_stale(record) is False


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------
class TestClassifyFailure:
    def test_throttling_is_transient(self):
        detail = _make_detail(cause="ThrottlingException: Rate exceeded")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True
        assert result.retry_count == 1

    def test_too_many_requests_is_transient(self):
        detail = _make_detail(cause="TooManyRequestsException")
        result = classify_failure(detail, receive_count=2)
        assert result.is_transient is True

    def test_service_unavailable_is_transient(self):
        detail = _make_detail(cause="ServiceUnavailable: try again")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_lambda_service_exception_is_transient(self):
        detail = _make_detail(cause="Lambda.ServiceException")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_timeout_is_transient(self):
        detail = _make_detail(cause="States.Timeout")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_http_error_is_transient(self):
        detail = _make_detail(
            cause="522 Server Error: HTTPError for url: https://api.frankfurter.dev/v1/..."
        )
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_connection_error_is_transient(self):
        detail = _make_detail(
            cause="ConnectionError: Connection refused"
        )
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_request_timeout_is_transient(self):
        detail = _make_detail(
            cause="Timeout: Read timed out"
        )
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is True

    def test_validation_error_is_permanent(self):
        detail = _make_detail(cause="ValueError: Invalid input data")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is False

    def test_unknown_error_is_permanent(self):
        detail = _make_detail(cause="SomethingWeird happened")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is False

    def test_missing_cause_is_permanent(self):
        detail = {"executionArn": EXEC_ARN, "status": "FAILED", "input": "{}"}
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is False

    def test_empty_cause_is_permanent(self):
        detail = _make_detail(cause="")
        result = classify_failure(detail, receive_count=1)
        assert result.is_transient is False


# ---------------------------------------------------------------------------
# extract_execution_input
# ---------------------------------------------------------------------------
class TestExtractExecutionInput:
    def test_parses_valid_eventbridge_message(self):
        detail = _make_detail(input_data={"mode": "backfill", "start_date": "2024-01-01"})
        result = extract_execution_input(detail)
        assert result == {"mode": "backfill", "start_date": "2024-01-01"}

    def test_parses_empty_input(self):
        detail = _make_detail(input_data={})
        result = extract_execution_input(detail)
        assert result == {}

    def test_raises_on_missing_input(self):
        detail = {"executionArn": EXEC_ARN, "status": "FAILED"}
        with pytest.raises(ValueError, match="Missing 'input'"):
            extract_execution_input(detail)

    def test_raises_on_invalid_json_input(self):
        detail = {"executionArn": EXEC_ARN, "input": "not-json{{{"}
        with pytest.raises(ValueError, match="Failed to parse"):
            extract_execution_input(detail)


# ---------------------------------------------------------------------------
# replay_execution
# ---------------------------------------------------------------------------
class TestReplayExecution:
    def test_starts_execution_with_correct_input(self):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new-exec"}

        arn = replay_execution(sfn, SFN_ARN, {"mode": "backfill"}, EXEC_ARN)

        assert arn == "arn:new-exec"
        call_kwargs = sfn.start_execution.call_args.kwargs
        assert call_kwargs["stateMachineArn"] == SFN_ARN
        assert json.loads(call_kwargs["input"]) == {"mode": "backfill"}

    def test_generates_replay_name_from_original_arn(self):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new-exec"}

        replay_execution(sfn, SFN_ARN, {}, EXEC_ARN)

        call_kwargs = sfn.start_execution.call_args.kwargs
        assert call_kwargs["name"].startswith("replay-")

    def test_no_name_without_original_arn(self):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new-exec"}

        replay_execution(sfn, SFN_ARN, {}, None)

        call_kwargs = sfn.start_execution.call_args.kwargs
        assert "name" not in call_kwargs

    def test_raises_on_oversized_input(self):
        sfn = MagicMock()
        huge_input = {"data": "x" * 300_000}

        with pytest.raises(ValueError, match="exceeds Step Functions limit"):
            replay_execution(sfn, SFN_ARN, huge_input, None)

    def test_propagates_client_error(self):
        sfn = MagicMock()
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}},
            "StartExecution",
        )
        with pytest.raises(ClientError):
            replay_execution(sfn, SFN_ARN, {}, None)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------
class TestLambdaHandler:
    @patch("lambda_dlq_auto_retry.boto3")
    def test_retries_transient_failure(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new-exec"}
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException", input_data={"mode": "daily"})
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-123")

        result = lambda_handler(event, context)

        assert result["status"] == "processed"
        assert result["retried"] == 1
        assert result["alerted"] == 0
        sfn.start_execution.assert_called_once()
        sns.publish.assert_not_called()

    @patch("lambda_dlq_auto_retry.boto3")
    def test_alerts_on_permanent_failure(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ValueError: bad data")
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-456")

        result = lambda_handler(event, context)

        assert result["alerted"] == 1
        assert result["retried"] == 0
        sfn.start_execution.assert_not_called()
        sns.publish.assert_called_once()

    @patch("lambda_dlq_auto_retry.boto3")
    def test_alerts_when_max_retries_exceeded(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException")
        event = _make_sqs_event(detail, receive_count=4)
        context = MagicMock(aws_request_id="req-789")

        result = lambda_handler(event, context)

        assert result["alerted"] == 1
        assert result["retried"] == 0
        sfn.start_execution.assert_not_called()
        sns.publish.assert_called_once()

    @patch("lambda_dlq_auto_retry.boto3")
    def test_consumes_message_after_permanent_alert(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ValueError: bad")
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-abc")

        result = lambda_handler(event, context)

        assert result["batchItemFailures"] == []
        assert result["alerted"] == 1

    @patch("lambda_dlq_auto_retry.boto3")
    def test_empty_batch_item_failures_on_retry(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new"}
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException", input_data={})
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-def")

        result = lambda_handler(event, context)

        assert result["batchItemFailures"] == []

    @patch("lambda_dlq_auto_retry.boto3")
    def test_skips_malformed_message(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        event = {
            "Records": [{
                "messageId": "msg-bad",
                "receiptHandle": "receipt-bad",
                "body": "not-json{{{",
                "attributes": {"ApproximateReceiveCount": "1"},
            }],
        }
        context = MagicMock(aws_request_id="req-bad")

        result = lambda_handler(event, context)

        assert result["errors"] == 1
        assert result["retried"] == 0
        assert result["alerted"] == 0

    @patch("lambda_dlq_auto_retry.boto3")
    def test_publishes_retry_metric(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new"}
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException", input_data={})
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-met")

        lambda_handler(event, context)

        cw.put_metric_data.assert_called()
        call_args = cw.put_metric_data.call_args.kwargs
        metric_names = [m["MetricName"] for m in call_args["MetricData"]]
        assert "DLQRetryAttempt" in metric_names

    @patch("lambda_dlq_auto_retry.boto3")
    def test_publishes_permanent_failure_metric(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ValueError: bad data")
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-perm")

        lambda_handler(event, context)

        cw.put_metric_data.assert_called()
        call_args = cw.put_metric_data.call_args.kwargs
        metric_names = [m["MetricName"] for m in call_args["MetricData"]]
        assert "DLQPermanentFailure" in metric_names

    @patch("lambda_dlq_auto_retry.boto3")
    def test_sfn_client_error_returns_batch_failure(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}},
            "StartExecution",
        )
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException", input_data={})
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-err")

        result = lambda_handler(event, context)

        assert result["errors"] == 1
        assert len(result["batchItemFailures"]) == 1

    @patch("lambda_dlq_auto_retry.boto3")
    def test_sns_alert_includes_error_details(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(
            cause="ValueError: Invalid currency pair",
            execution_arn=EXEC_ARN,
        )
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-sns")

        lambda_handler(event, context)

        call_kwargs = sns.publish.call_args.kwargs
        assert "ValueError" in call_kwargs["Message"]
        assert EXEC_ARN in call_kwargs["Message"]

    @patch("lambda_dlq_auto_retry.boto3")
    def test_sns_failure_returns_batch_failure_for_retry(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        sns.publish.side_effect = ClientError(
            {"Error": {"Code": "AuthorizationError", "Message": "denied"}},
            "Publish",
        )
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ValueError: bad data")
        event = _make_sqs_event(detail, receive_count=1)
        context = MagicMock(aws_request_id="req-sns-fail")

        result = lambda_handler(event, context)

        assert result["errors"] == 1
        assert result["alerted"] == 0
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-001"
        cw.put_metric_data.assert_called()

    @patch("lambda_dlq_auto_retry.boto3")
    def test_boundary_receive_count_equals_max_retries(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException")
        event = _make_sqs_event(detail, receive_count=3)
        context = MagicMock(aws_request_id="req-boundary")

        result = lambda_handler(event, context)

        assert result["retried"] == 0
        assert result["alerted"] == 1
        sfn.start_execution.assert_not_called()

    @patch("lambda_dlq_auto_retry.boto3")
    def test_discards_stale_message_without_retry_or_alert(self, mock_boto3):
        sfn = MagicMock()
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        detail = _make_detail(cause="ThrottlingException", input_data={"mode": "daily"})
        two_days_ago_ms = int((time.time() - 48 * 3600) * 1000)
        event = _make_sqs_event(detail, receive_count=1, sent_timestamp_ms=two_days_ago_ms)
        context = MagicMock(aws_request_id="req-stale")

        result = lambda_handler(event, context)

        assert result["retried"] == 0
        assert result["alerted"] == 0
        assert result["errors"] == 0
        assert result["batchItemFailures"] == []
        sfn.start_execution.assert_not_called()
        sns.publish.assert_not_called()

        metric_calls = cw.put_metric_data.call_args_list
        metric_names = [
            m.kwargs["MetricData"][0]["MetricName"] for m in metric_calls
        ]
        assert "DLQStaleMessageDiscarded" in metric_names

    @patch("lambda_dlq_auto_retry.boto3")
    def test_multi_message_batch(self, mock_boto3):
        sfn = MagicMock()
        sfn.start_execution.return_value = {"executionArn": "arn:new"}
        sns = MagicMock()
        cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "stepfunctions": sfn, "sns": sns, "cloudwatch": cw,
        }[svc]

        transient_detail = _make_detail(
            cause="ThrottlingException", input_data={"mode": "daily"},
        )
        permanent_detail = _make_detail(cause="ValueError: bad data")
        now_ms = str(int(time.time() * 1000))
        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "receiptHandle": "r1",
                    "body": json.dumps({"detail": transient_detail}),
                    "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": now_ms},
                },
                {
                    "messageId": "msg-002",
                    "receiptHandle": "r2",
                    "body": json.dumps({"detail": permanent_detail}),
                    "attributes": {"ApproximateReceiveCount": "1", "SentTimestamp": now_ms},
                },
            ],
        }
        context = MagicMock(aws_request_id="req-batch")

        result = lambda_handler(event, context)

        assert result["retried"] == 1
        assert result["alerted"] == 1
        assert result["batchItemFailures"] == []


# ---------------------------------------------------------------------------
# _publish_metric error path
# ---------------------------------------------------------------------------
class TestPublishMetric:
    def test_logs_error_on_client_error(self):
        cw = MagicMock()
        cw.put_metric_data.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceError", "Message": "oops"}},
            "PutMetricData",
        )

        _publish_metric(cw, "TestMetric", 1.0)

        cw.put_metric_data.assert_called_once()

    def test_succeeds_normally(self):
        cw = MagicMock()

        _publish_metric(cw, "TestMetric", 42.0)

        call_kwargs = cw.put_metric_data.call_args.kwargs
        assert call_kwargs["MetricData"][0]["MetricName"] == "TestMetric"
        assert call_kwargs["MetricData"][0]["Value"] == 42.0


# ---------------------------------------------------------------------------
# _send_permanent_failure_alert
# ---------------------------------------------------------------------------
class TestSendPermanentFailureAlert:
    def test_publishes_to_sns(self):
        sns = MagicMock()

        _send_permanent_failure_alert(
            sns, "arn:sns:topic", EXEC_ARN, "ValueError", 3,
        )

        call_kwargs = sns.publish.call_args.kwargs
        assert call_kwargs["TopicArn"] == "arn:sns:topic"
        assert "ValueError" in call_kwargs["Message"]
        assert "3" in call_kwargs["Message"]

    def test_propagates_client_error(self):
        sns = MagicMock()
        sns.publish.side_effect = ClientError(
            {"Error": {"Code": "NotFound", "Message": "topic gone"}},
            "Publish",
        )

        with pytest.raises(ClientError):
            _send_permanent_failure_alert(
                sns, "arn:sns:topic", EXEC_ARN, "ValueError", 1,
            )
