"""Tests for DLQ replay mechanism."""

import json
from unittest.mock import MagicMock, patch
import pytest


def _make_sfn_event(
    execution_arn: str = "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:abc123",
    status: str = "FAILED",
    input_data: dict | None = None,
) -> dict:
    """Create a mock EventBridge SQS message for a failed Step Functions execution."""
    if input_data is None:
        input_data = {"mode": "incremental", "start_date": "2024-01-01"}

    return {
        "messageId": "msg-12345",
        "receiptHandle": "receipt-12345",
        "body": json.dumps({
            "detail": {
                "executionArn": execution_arn,
                "stateMachineArn": "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine",
                "status": status,
                "input": json.dumps(input_data),
                "startDate": 1704067200,
                "stopDate": 1704067300,
                "error": "States.Runtime",
                "cause": "An error occurred during execution"
            }
        })
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
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:xyz789"
        }

        state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine"
        input_data = {"mode": "incremental"}

        result = replay_execution(sfn_client=mock_sfn, state_machine_arn=state_machine_arn, input_data=input_data)

        assert result == "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:xyz789"
        mock_sfn.start_execution.assert_called_once()
        call_args = mock_sfn.start_execution.call_args
        assert call_args.kwargs["stateMachineArn"] == state_machine_arn
        assert json.loads(call_args.kwargs["input"]) == input_data

    @patch("scripts.replay_dlq.sfn_client")
    def test_replay_execution_sfn_error(self, mock_sfn):
        """Should raise error if Step Functions call fails."""
        from scripts.replay_dlq import replay_execution
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "InvalidExecutionArn", "Message": "Invalid ARN"}}
        mock_sfn.start_execution.side_effect = ClientError(error_response, "StartExecution")

        state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine"

        with pytest.raises(ClientError, match="InvalidExecutionArn"):
            replay_execution(sfn_client=mock_sfn, state_machine_arn=state_machine_arn, input_data={})


class TestDeleteMessage:
    """Test deletion of SQS messages after successful replay."""

    @patch("scripts.replay_dlq.sqs_client")
    def test_delete_message_success(self, mock_sqs):
        """Should call delete_message with queue URL and receipt handle."""
        from scripts.replay_dlq import delete_message

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        receipt_handle = "receipt-12345"

        delete_message(sqs_client=mock_sqs, queue_url=queue_url, receipt_handle=receipt_handle)

        mock_sqs.delete_message.assert_called_once_with(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle
        )

    @patch("scripts.replay_dlq.sqs_client")
    def test_delete_message_sqs_error(self, mock_sqs):
        """Should raise error if deletion fails."""
        from scripts.replay_dlq import delete_message
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "InvalidHandle", "Message": "Invalid handle"}}
        mock_sqs.delete_message.side_effect = ClientError(error_response, "DeleteMessage")

        with pytest.raises(ClientError, match="InvalidHandle"):
            delete_message(mock_sqs, "https://sqs.us-east-1.amazonaws.com/123456789012/queue", "bad-handle")


class TestReadDlqMessages:
    """Test reading messages from SQS DLQ."""

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_success(self, mock_sqs):
        """Should receive up to max_messages from SQS."""
        from scripts.replay_dlq import read_dlq_messages

        msg1 = _make_sfn_event(execution_arn="arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:exec1")
        msg2 = _make_sfn_event(execution_arn="arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:exec2")

        mock_sqs.receive_message.return_value = {
            "Messages": [msg1, msg2]
        }

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        messages = read_dlq_messages(sqs_client=mock_sqs, queue_url=queue_url, max_messages=10)

        assert len(messages) == 2
        mock_sqs.receive_message.assert_called_once_with(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=5
        )

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_empty_queue(self, mock_sqs):
        """Should return empty list if no messages in queue."""
        from scripts.replay_dlq import read_dlq_messages

        mock_sqs.receive_message.return_value = {}  # No 'Messages' key

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        messages = read_dlq_messages(sqs_client=mock_sqs, queue_url=queue_url, max_messages=10)

        assert messages == []

    @patch("scripts.replay_dlq.sqs_client")
    def test_read_messages_respects_max_messages(self, mock_sqs):
        """Should pass max_messages to SQS receive_message."""
        from scripts.replay_dlq import read_dlq_messages

        mock_sqs.receive_message.return_value = {"Messages": []}

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        read_dlq_messages(sqs_client=mock_sqs, queue_url=queue_url, max_messages=5)

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

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine"

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=queue_url,
            state_machine_arn=state_machine_arn,
            max_messages=10,
            dry_run=True
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
            "executionArn": "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:new-exec"
        }

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine"

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=queue_url,
            state_machine_arn=state_machine_arn,
            max_messages=10,
            dry_run=False
        )

        assert replayed == 1
        assert errors == 0
        mock_sfn.start_execution.assert_called_once()
        mock_sqs.delete_message.assert_called_once()

    @patch("scripts.replay_dlq.sqs_client")
    @patch("scripts.replay_dlq.sfn_client")
    def test_main_replay_with_errors(self, mock_sfn, mock_sqs):
        """Should count errors for failed messages and not delete them."""
        from scripts.replay_dlq import main
        from botocore.exceptions import ClientError

        msg1 = _make_sfn_event(execution_arn="arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:exec1")
        msg2 = _make_sfn_event(execution_arn="arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:exec2")

        mock_sqs.receive_message.return_value = {"Messages": [msg1, msg2]}

        # First call succeeds, second fails
        error_response = {"Error": {"Code": "InvalidExecutionArn", "Message": "Invalid"}}
        mock_sfn.start_execution.side_effect = [
            {"executionArn": "arn:aws:states:us-east-1:123456789012:execution:fxlake-etl-state-machine:new1"},
            ClientError(error_response, "StartExecution")
        ]

        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/fxlake-pipeline-dlq"
        state_machine_arn = "arn:aws:states:us-east-1:123456789012:stateMachine:fxlake-etl-state-machine"

        replayed, errors = main(
            sqs_client=mock_sqs,
            sfn_client=mock_sfn,
            queue_url=queue_url,
            state_machine_arn=state_machine_arn,
            max_messages=10,
            dry_run=False
        )

        assert replayed == 1
        assert errors == 1
        assert mock_sqs.delete_message.call_count == 1  # Only successful message deleted
