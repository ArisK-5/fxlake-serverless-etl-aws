#!/usr/bin/env python3
"""Replay failed Step Functions executions from DLQ.

This script reads messages from the SQS dead-letter queue (populated by EventBridge
when Step Functions executions fail), extracts the original execution input, and
re-executes the pipeline.
"""

import argparse
import json
import logging
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Module-level clients (for testing, these are patched)
sqs_client = boto3.client("sqs")
sfn_client = boto3.client("stepfunctions")

logger = logging.getLogger(__name__)


def extract_execution_input(message: dict) -> dict[str, Any]:
    """Extract Step Functions input from an SQS message.

    Args:
        message: SQS message with EventBridge event payload in body.

    Returns:
        Parsed input dict that was passed to the failed execution.

    Raises:
        ValueError: If message format is invalid or input cannot be parsed.
    """
    try:
        body = json.loads(message["body"])
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse message body JSON: {e}") from e
    except KeyError as e:
        raise ValueError(f"Missing 'body' in message: {e}") from e

    if "detail" not in body:
        raise ValueError("Missing 'detail' in message")

    detail = body["detail"]
    if "input" not in detail:
        raise ValueError("Missing 'input' in execution detail")

    try:
        return json.loads(detail["input"])
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse input JSON: {e}") from e


def replay_execution(
    sfn_client: Any,
    state_machine_arn: str,
    input_data: dict[str, Any],
) -> str:
    """Start a new execution of the Step Function.

    Args:
        sfn_client: boto3 Step Functions client.
        state_machine_arn: ARN of the state machine to execute.
        input_data: Input dict for the execution.

    Returns:
        ARN of the newly created execution.

    Raises:
        ClientError: If Step Functions call fails.
    """
    response = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps(input_data),
    )
    return response["executionArn"]


def delete_message(
    sqs_client: Any,
    queue_url: str,
    receipt_handle: str,
) -> None:
    """Delete a message from SQS.

    Args:
        sqs_client: boto3 SQS client.
        queue_url: URL of the SQS queue.
        receipt_handle: Receipt handle of the message to delete.

    Raises:
        ClientError: If deletion fails.
    """
    sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)


def read_dlq_messages(
    sqs_client: Any,
    queue_url: str,
    max_messages: int = 10,
) -> list[dict[str, Any]]:
    """Read up to max_messages from the DLQ.

    Args:
        sqs_client: boto3 SQS client.
        queue_url: URL of the SQS queue.
        max_messages: Maximum number of messages to retrieve.

    Returns:
        List of SQS messages (may be empty if queue is empty).
    """
    response = sqs_client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=max_messages,
        WaitTimeSeconds=5,
    )
    return response.get("Messages", [])


def main(
    sqs_client: Any,
    sfn_client: Any,
    queue_url: str,
    state_machine_arn: str,
    max_messages: int = 10,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Main replay orchestration.

    Args:
        sqs_client: boto3 SQS client.
        sfn_client: boto3 Step Functions client.
        queue_url: URL of the DLQ.
        state_machine_arn: ARN of the state machine to re-execute.
        max_messages: Max messages to process in one run.
        dry_run: If True, parse and log but do not execute or delete.

    Returns:
        Tuple of (replayed_count, error_count).
    """
    replayed = 0
    errors = 0

    messages = read_dlq_messages(sqs_client, queue_url, max_messages)
    logger.info(
        "Retrieved messages from DLQ",
        extra={"count": len(messages), "dry_run": dry_run},
    )

    for message in messages:
        try:
            input_data = extract_execution_input(message)
            logger.info(
                "Extracted execution input",
                extra={"execution_arn_from_message": message.get("messageId")},
            )

            if dry_run:
                logger.info(
                    "DRY RUN: Would re-execute Step Function",
                    extra={"input": input_data},
                )
                replayed += 1
            else:
                execution_arn = replay_execution(sfn_client, state_machine_arn, input_data)
                logger.info(
                    "Re-executed Step Function",
                    extra={"new_execution_arn": execution_arn},
                )

                delete_message(sqs_client, queue_url, message["receiptHandle"])
                logger.info(
                    "Deleted message from DLQ",
                    extra={"message_id": message["messageId"]},
                )

                replayed += 1

        except (ValueError, ClientError) as e:
            errors += 1
            logger.error(
                f"Failed to replay message: {e}",
                extra={"message_id": message.get("messageId")},
            )

    logger.info(
        "DLQ replay complete",
        extra={"replayed": replayed, "errors": errors},
    )
    return replayed, errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay failed Step Functions executions from DLQ"
    )
    parser.add_argument(
        "--queue-url",
        required=True,
        help="SQS DLQ URL",
    )
    parser.add_argument(
        "--state-machine-arn",
        required=True,
        help="Step Functions state machine ARN",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=10,
        help="Maximum messages to process (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log only, do not execute or delete",
    )

    args = parser.parse_args()

    # Configure structured logging (consistent with lambda/common/logging.py pattern)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    replayed, errors = main(
        sqs_client=sqs_client,
        sfn_client=sfn_client,
        queue_url=args.queue_url,
        state_machine_arn=args.state_machine_arn,
        max_messages=args.max_messages,
        dry_run=args.dry_run,
    )

    sys.exit(0 if errors == 0 else 1)
