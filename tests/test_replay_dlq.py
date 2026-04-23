"""Tests for DLQ replay mechanism."""

import json
from unittest.mock import patch

import pytest

_ACCOUNT = "123456789012"
_REGION = "us-east-1"
_SM_NAME = "fxlake-etl-state-machine"
_SM_ARN = f"arn:aws:states:{_REGION}:{_ACCOUNT}:stateMachine:{_SM_NAME}"
_EXEC_ARN = f"arn:aws:states:{_REGION}:{_ACCOUNT}:execution:{_SM_NAME}"
_QUEUE_URL = (
    f"https://sqs.{_REGION}.amazonaws.com/{_ACCOUNT}/fxlake-pipeline-dlq"
)


def _make_sfn_event(
    execution_arn: str = f"{_EXEC_ARN}:abc123",
    status: str = "FAILED",
    input_data: dict | None = None,
) -> dict:
    """Create a mock EventBridge SQS message for a failed SFN execution."""
    if input_data is None:
        input_data = {"mode": "incremental", "start_date": "2024-01-01"}

    return {
        "messageId": "msg-12345",
        "receiptHandle": "receipt-12345",
        "body": json.dumps({
            "detail": {
                "executionArn": execution_arn,
                "stateMachineArn": _SM_ARN,
                "status": status,
                "input": json.dumps(input_data),
                "startDate": 1704067200,
                "stopDate": 1704067300,
                "error": "States.Runtime",
                "cause": "An error occurred during execution",
            }
        }),
    }


class TestExtractExecutionInput:
    """Test extraction of Step Functions input from SQS message."""

    def test_extract_input_from_valid_message(self):
        """Should extract and parse input from EventBridge message body."""
        from scripts.replay_dlq import extract_execution_input

        msg = _make_sfn_event(input_data={"mode": "backfill", "start": "2023-01-01"})
        input_data = extract_execution_input(msg)

        assert input_data == {"mode": "backfill", "start": "2023-01-01"}

    def test_extract_input_missing_detail(self):
        """Should raise ValueError if detail is missing from message."""
        from scripts.replay_dlq import extract_execution_input

        msg = {"messageId": "msg-1", "receiptHandle": "receipt-1", "body": json.dumps({})}

        with pytest.raises(ValueError, match="Missing 'detail' in message"):
            extract_execution_input(msg)

    def test_extract_input_missing_input_field(self):
        """Should raise ValueError if input field is missing from detail."""
        from scripts.replay_dlq import extract_execution_input

        msg = {
            "messageId": "msg-1",
            "receiptHandle": "receipt-1",
            "body": json.dumps({"detail": {"status": "FAILED"}})
        }

        with pytest.raises(ValueError, match="Missing 'input' in execution detail"):
            extract_execution_input(msg)

    def test_extract_input_malformed_json(self):
        """Should raise ValueError if input JSON is malformed."""
        from scripts.replay_dlq import extract_execution_input

        msg = {
            "messageId": "msg-1",
            "receiptHandle": "receipt-1",
            "body": json.dumps({
                "detail": {"input": "{ invalid json }"}
            })
        }

        with pytest.raises(ValueError, match="Failed to parse input JSON"):
            extract_execution_input(msg)


class TestReplayExecution:
    """Test re-execution of failed Step Functions."""

    @patch("scripts.replay_dlq.sfn_client")
    def test_replay_execution_success(self, mock_sfn):
        """Should call start_execution with extracted input and return execution ARN."""
        from scripts.replay_dlq import replay_execution

        mock_sfn.start_execution.return_value = {
            "executionArn": f"{_EXEC_ARN}:xyz789",
        }

        input_data = {"mode": "incremental"}

        result = replay_execution(
            sfn_client=mock_sfn,
            state_machine_arn=_SM_ARN,
            input_data=input_data,
        )

        assert result == f"{_EXEC_ARN}:xyz789"
        mock_sfn.start_execution.assert_called_once()
        call_args = mock_sfn.start_execution.call_args
        assert call_args.kwargs["stateMachineArn"] == _SM_ARN
        assert json.loads(call_args.kwargs["input"]) == input_data

    @patch("scripts.replay_dlq.sfn_client")
    def test_replay_execution_sfn_error(self, mock_sfn):
        """Should raise error if Step Functions call fails."""
        from botocore.exceptions import ClientError

        from scripts.replay_dlq import replay_execution

        error_response = {
            "Error": {"Code": "InvalidExecutionArn", "Message": "Invalid ARN"},
        }
        mock_sfn.start_execution.side_effect = ClientError(
            error_response, "StartExecution",
        )

        with pytest.raises(ClientError, match="InvalidExecutionArn"):
            replay_execution(
                sfn_client=mock_sfn,
                state_machine_arn=_SM_ARN,
                input_data={},
            )


class TestDeleteMessage:
    """Test deletion of SQS messages after successful replay."""

    @patch("scripts.replay_dlq.sqs_client")
    def test_delete_message_success(self, mock_sqs):
        """Should call delete_message with queue URL and receipt handle."""
        from scripts.replay_dlq import delete_message

        receipt_handle = "receipt-12345"

        delete_message(
            sqs_client=mock_sqs,
            queue_url=_QUEUE_URL,
            receipt_handle=receipt_handle,
        )

        mock_sqs.delete_message.assert_called_once_with(
            QueueUrl=_QUEUE_URL,
            ReceiptHandle=receipt_handle,
        )

    @patch("scripts.replay_dlq.sqs_client")
    def test_delete_message_sqs_error(self, mock_sqs):
        """Should raise error if deletion fails."""
        from botocore.exceptions import ClientError

        from scripts.replay_dlq import delete_message

        error_response = {
            "Error": {"Code": "InvalidHandle", "Message": "Invalid handle"},
        }
        mock_sqs.delete_message.side_effect = ClientError(
            error_response, "DeleteMessage",
        )

        with pytest.raises(ClientError, match="InvalidHandle"):
            delete_message(mock_sqs, _QUEUE_URL, "bad-handle")


class TestReadDlqMessages:
    """Test reading messages from SQS DLQ."""

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_success(self, mock_sqs):
        """Should receive up to max_messages from SQS."""
        from scripts.replay_dlq import read_dlq_messages

        msg1 = _make_sfn_event(execution_arn=f"{_EXEC_ARN}:exec1")
        msg2 = _make_sfn_event(execution_arn=f"{_EXEC_ARN}:exec2")

        mock_sqs.receive_message.return_value = {
            "Messages": [msg1, msg2],
        }

        messages = read_dlq_messages(
            sqs_client=mock_sqs, queue_url=_QUEUE_URL, max_messages=10,
        )

        assert len(messages) == 2
        mock_sqs.receive_message.assert_called_once_with(
            QueueUrl=_QUEUE_URL,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=5,
        )

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_empty_queue(self, mock_sqs):
        """Should return empty list if no messages in queue."""
        from scripts.replay_dlq import read_dlq_messages

        mock_sqs.receive_message.return_value = {}  # No 'Messages' key

        messages = read_dlq_messages(
            sqs_client=mock_sqs, queue_url=_QUEUE_URL, max_messages=10,
        )

        assert messages == []

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_respects_max_messages(self, mock_sqs):
        """Should pass max_messages to SQS receive_message."""
        from scripts.replay_dlq import read_dlq_messages

        mock_sqs.receive_message.return_value = {"Messages": []}

        read_dlq_messages(
            sqs_client=mock_sqs, queue_url=_QUEUE_URL, max_messages=5,
        )

        call_args = mock_sqs.receive_message.call_args
        assert call_args.kwargs["MaxNumberOfMessages"] == 5


class TestMainFunction:
    """Test main replay orchestration."""

    @patch("scripts.replay_dlq.sqs_client")
    @patch("scripts.replay_dlq.sfn_client")
    def test_main_dry_run(self, mock_sfn, mock_sqs):
        """Dry-run mode should not delete messages or execute Step Functions."""
        from scripts.replay_dlq import main

        msg = _make_sfn_event()
        mock_sqs.receive_message.return_value = {"Messages": [msg]}

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=_QUEUE_URL,
            state_machine_arn=_SM_ARN,
            max_messages=10,
            dry_run=True,
        )

        assert replayed == 1  # Dry run still counts as processed
        assert errors == 0
        mock_sfn.start_execution.assert_not_called()
        mock_sqs.delete_message.assert_not_called()

    @patch("scripts.replay_dlq.sqs_client")
    @patch("scripts.replay_dlq.sfn_client")
    def test_main_successful_replay(self, mock_sfn, mock_sqs):
        """Should replay successful messages and delete them from queue."""
        from scripts.replay_dlq import main

        msg = _make_sfn_event()
        mock_sqs.receive_message.return_value = {"Messages": [msg]}
        mock_sfn.start_execution.return_value = {
            "executionArn": f"{_EXEC_ARN}:new-exec",
        }

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=_QUEUE_URL,
            state_machine_arn=_SM_ARN,
            max_messages=10,
            dry_run=False,
        )

        assert replayed == 1
        assert errors == 0
        mock_sfn.start_execution.assert_called_once()
        mock_sqs.delete_message.assert_called_once()

    @patch("scripts.replay_dlq.sqs_client")
    @patch("scripts.replay_dlq.sfn_client")
    def test_main_replay_with_errors(self, mock_sfn, mock_sqs):
        """Should count errors for failed messages and not delete them."""
        from botocore.exceptions import ClientError

        from scripts.replay_dlq import main

        msg1 = _make_sfn_event(execution_arn=f"{_EXEC_ARN}:exec1")
        msg2 = _make_sfn_event(execution_arn=f"{_EXEC_ARN}:exec2")

        mock_sqs.receive_message.return_value = {
            "Messages": [msg1, msg2],
        }

        # First call succeeds, second fails
        error_response = {
            "Error": {"Code": "InvalidExecutionArn", "Message": "Invalid"},
        }
        mock_sfn.start_execution.side_effect = [
            {"executionArn": f"{_EXEC_ARN}:new1"},
            ClientError(error_response, "StartExecution"),
        ]

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=_QUEUE_URL,
            state_machine_arn=_SM_ARN,
            max_messages=10,
            dry_run=False,
        )

        assert replayed == 1
        assert errors == 1
        # Only successful message deleted
        assert mock_sqs.delete_message.call_count == 1
