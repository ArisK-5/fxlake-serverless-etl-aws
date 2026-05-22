import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from common.logging import configure_logger, inject_request_id

if os.getenv("AWS_XRAY_DAEMON_ADDRESS"):
    try:
        from aws_xray_sdk.core import patch_all

        patch_all()
    except ImportError:
        pass

logger = configure_logger("dlq-auto-retry")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
MAX_MESSAGE_AGE_HOURS = int(os.environ.get("MAX_MESSAGE_AGE_HOURS", "24"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/SelfHealing")

SFN_INPUT_MAX_BYTES = 262_144

TRANSIENT_ERROR_PATTERNS = (
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailable",
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "States.Timeout",
    "States.TaskFailed",
    "InternalError",
    "ServiceException",
    "HTTPError",
    "ConnectionError",
    "Timeout",
)


@dataclass(frozen=True)
class FailureClassification:
    is_transient: bool
    error_code: str
    retry_count: int

    def __post_init__(self) -> None:
        if not self.error_code:
            raise ValueError("error_code cannot be empty")
        if self.retry_count < 0:
            raise ValueError("retry_count cannot be negative")


def is_message_stale(record: dict[str, Any]) -> bool:
    sent_ts_ms = int(record.get("attributes", {}).get("SentTimestamp", "0"))
    if sent_ts_ms == 0:
        return False
    age_hours = (time.time() - sent_ts_ms / 1000) / 3600
    return age_hours > MAX_MESSAGE_AGE_HOURS


def classify_failure(detail: dict, receive_count: int) -> FailureClassification:
    cause = detail.get("cause", "")
    is_transient = any(pattern in cause for pattern in TRANSIENT_ERROR_PATTERNS)
    return FailureClassification(
        is_transient=is_transient,
        error_code=cause[:120] if cause else "unknown",
        retry_count=receive_count,
    )


def extract_execution_input(detail: dict) -> dict[str, Any]:
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
    original_execution_arn: str | None = None,
) -> str:
    input_json = json.dumps(input_data)
    if len(input_json.encode()) > SFN_INPUT_MAX_BYTES:
        raise ValueError(
            f"Input payload ({len(input_json.encode())} bytes) exceeds "
            f"Step Functions limit ({SFN_INPUT_MAX_BYTES} bytes)"
        )

    kwargs: dict[str, Any] = {
        "stateMachineArn": state_machine_arn,
        "input": input_json,
    }
    if original_execution_arn:
        suffix = original_execution_arn.rsplit(":", 1)[-1]
        digest = hashlib.sha256(suffix.encode()).hexdigest()[:8]
        kwargs["name"] = f"replay-{suffix[:54]}-{digest}"

    response = sfn_client.start_execution(**kwargs)
    return response["executionArn"]


def _publish_metric(
    cloudwatch_client: Any,
    metric_name: str,
    value: float = 1.0,
) -> None:
    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{
                "MetricName": metric_name,
                "Value": value,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "Environment", "Value": "production"},
                ],
            }],
        )
    except ClientError as e:
        logger.error(
            "Failed to publish metric",
            extra={
                "metric_name": metric_name,
                "value": value,
                "error_code": e.response["Error"]["Code"],
            },
        )


def _send_permanent_failure_alert(
    sns_client: Any,
    topic_arn: str,
    execution_arn: str,
    error_code: str,
    retry_count: int,
) -> None:
    message = (
        f"FXLake DLQ Auto-Retry — Permanent Failure\n\n"
        f"Execution: {execution_arn}\n"
        f"Error: {error_code}\n"
        f"Retry count: {retry_count}\n\n"
        f"This failure has been classified as permanent and will not be retried. "
        f"Manual investigation required."
    )
    sns_client.publish(
        TopicArn=topic_arn,
        Subject="FXLake Pipeline Permanent Failure",
        Message=message,
    )


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    sfn_client = boto3.client("stepfunctions")
    sns_client = boto3.client("sns")
    cloudwatch_client = boto3.client("cloudwatch")

    retried = 0
    alerted = 0
    errors = 0
    batch_item_failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        receive_count = int(record.get("attributes", {}).get(
            "ApproximateReceiveCount", "1"
        ))

        if is_message_stale(record):
            logger.info(
                "Discarding stale DLQ message",
                extra={
                    "message_id": message_id,
                    "sent_timestamp": record.get("attributes", {}).get("SentTimestamp"),
                    "max_age_hours": MAX_MESSAGE_AGE_HOURS,
                },
            )
            _publish_metric(cloudwatch_client, "DLQStaleMessageDiscarded")
            continue

        try:
            body = json.loads(record["body"])
            detail = body.get("detail", {})
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(
                "Malformed SQS message",
                extra={"message_id": message_id, "error": str(e)},
            )
            errors += 1
            batch_item_failures.append({"itemIdentifier": message_id})
            continue

        classification = classify_failure(detail, receive_count)
        execution_arn = detail.get("executionArn", "unknown")

        logger.info(
            "Classified failure",
            extra={
                "message_id": message_id,
                "is_transient": classification.is_transient,
                "error_code": classification.error_code,
                "retry_count": classification.retry_count,
                "execution_arn": execution_arn,
            },
        )

        should_retry = (
            classification.is_transient
            and classification.retry_count < MAX_RETRIES
        )

        if should_retry:
            try:
                input_data = extract_execution_input(detail)
                new_arn = replay_execution(
                    sfn_client, STATE_MACHINE_ARN, input_data, execution_arn,
                )
                logger.info(
                    "Replayed execution",
                    extra={
                        "original_arn": execution_arn,
                        "new_arn": new_arn,
                        "retry_count": classification.retry_count,
                    },
                )
                retried += 1
                _publish_metric(cloudwatch_client, "DLQRetryAttempt")
            except (ValueError, ClientError) as e:
                logger.error(
                    "Failed to replay execution",
                    extra={
                        "message_id": message_id,
                        "execution_arn": execution_arn,
                        "error": str(e),
                    },
                )
                errors += 1
                batch_item_failures.append({"itemIdentifier": message_id})
        else:
            reason = (
                "max retries exceeded"
                if classification.is_transient
                else "permanent failure"
            )
            logger.warning(
                f"Not retrying: {reason}",
                extra={
                    "execution_arn": execution_arn,
                    "error_code": classification.error_code,
                    "retry_count": classification.retry_count,
                },
            )
            try:
                _send_permanent_failure_alert(
                    sns_client,
                    SNS_TOPIC_ARN,
                    execution_arn,
                    classification.error_code,
                    classification.retry_count,
                )
                alerted += 1
            except ClientError as e:
                logger.error(
                    "Failed to send alert",
                    extra={
                        "error_code": e.response["Error"]["Code"],
                        "execution_arn": execution_arn,
                    },
                )
                errors += 1
                batch_item_failures.append({"itemIdentifier": message_id})
            _publish_metric(cloudwatch_client, "DLQPermanentFailure")

    logger.info(
        "DLQ auto-retry complete",
        extra={
            "retried": retried,
            "alerted": alerted,
            "errors": errors,
            "total_records": len(event.get("Records", [])),
        },
    )

    return {
        "status": "processed",
        "retried": retried,
        "alerted": alerted,
        "errors": errors,
        "batchItemFailures": batch_item_failures,
    }
