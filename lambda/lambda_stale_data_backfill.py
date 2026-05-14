import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
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

logger = configure_logger("stale-data-backfill")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
STATE_TABLE = os.environ.get("STATE_TABLE", "fxlake-pipeline-state")
PIPELINE_ID = os.environ.get("PIPELINE_ID", "fxlake")
STALE_THRESHOLD_DAYS = int(os.environ.get("STALE_THRESHOLD_DAYS", "2"))
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/SelfHealing")

SOURCES = ("frankfurter", "ecb", "fred")


@dataclass(frozen=True)
class StaleSource:
    source: str
    last_date: str
    gap_days: int
    backfill_start: str
    backfill_end: str

    def __post_init__(self) -> None:
        if self.gap_days < 0:
            raise ValueError("gap_days must be positive")


def check_source_staleness(
    source: str,
    dynamodb_client: Any,
    table_name: str,
    pipeline_id: str,
    threshold_days: int = STALE_THRESHOLD_DAYS,
) -> StaleSource | None:
    today = date.today()

    try:
        response = dynamodb_client.get_item(
            TableName=table_name,
            Key={
                "pipeline_id": {"S": pipeline_id},
                "source": {"S": source},
            },
        )
    except ClientError as e:
        logger.error(
            "Failed to read DynamoDB state",
            extra={
                "source": source,
                "error_code": e.response["Error"]["Code"],
            },
        )
        raise

    item = response.get("Item")
    if not item or "last_processed_date" not in item:
        logger.warning(
            "No DynamoDB entry for source",
            extra={"source": source, "pipeline_id": pipeline_id},
        )
        return StaleSource(
            source=source,
            last_date="none",
            gap_days=(today - date(2020, 1, 1)).days,
            backfill_start="2020-01-02",
            backfill_end=today.isoformat(),
        )

    last_date_str = item["last_processed_date"]["S"]
    last_date = date.fromisoformat(last_date_str)
    gap_days = (today - last_date).days

    if gap_days <= threshold_days:
        return None

    backfill_start = (last_date + timedelta(days=1)).isoformat()
    backfill_end = today.isoformat()

    return StaleSource(
        source=source,
        last_date=last_date_str,
        gap_days=gap_days - 1,
        backfill_start=backfill_start,
        backfill_end=backfill_end,
    )


def trigger_backfill_execution(
    sfn_client: Any,
    state_machine_arn: str,
    stale: StaleSource,
) -> str:
    input_data = {
        "mode": "backfill",
        "start_date": stale.backfill_start,
        "end_date": stale.backfill_end,
    }

    digest = hashlib.sha256(
        f"{stale.source}-{stale.backfill_start}-{stale.backfill_end}".encode()
    ).hexdigest()[:8]
    name = f"backfill-{stale.source}-{stale.backfill_start}-{stale.backfill_end}-{digest}"

    response = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=name,
        input=json.dumps(input_data),
    )
    return response["executionArn"]


def _publish_metrics(
    cloudwatch_client: Any,
    stale_count: int,
    backfill_count: int,
) -> None:
    metric_data = [
        {
            "MetricName": "StaleDataDetected",
            "Value": float(stale_count),
            "Unit": "Count",
            "Dimensions": [
                {"Name": "Environment", "Value": "production"},
            ],
        },
        {
            "MetricName": "BackfillTriggered",
            "Value": float(backfill_count),
            "Unit": "Count",
            "Dimensions": [
                {"Name": "Environment", "Value": "production"},
            ],
        },
    ]

    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
    except ClientError as e:
        logger.error(
            "Failed to publish metrics",
            extra={"error_code": e.response["Error"]["Code"]},
        )


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    sfn_client = boto3.client("stepfunctions")
    cloudwatch_client = boto3.client("cloudwatch")

    stale_sources: list[StaleSource] = []
    backfills_triggered = 0
    errors = 0

    dynamodb_client = boto3.client("dynamodb", region_name="us-east-1")

    for source in SOURCES:
        try:
            result = check_source_staleness(
                source, dynamodb_client,
                STATE_TABLE, PIPELINE_ID, STALE_THRESHOLD_DAYS,
            )
        except ClientError:
            errors += 1
            continue

        if result is not None:
            stale_sources.append(result)
            logger.warning(
                "Stale data detected",
                extra={
                    "source": result.source,
                    "last_date": result.last_date,
                    "gap_days": result.gap_days,
                },
            )

            try:
                arn = trigger_backfill_execution(
                    sfn_client, STATE_MACHINE_ARN, result,
                )
                backfills_triggered += 1
                logger.info(
                    "Backfill triggered",
                    extra={
                        "source": result.source,
                        "execution_arn": arn,
                    },
                )
            except ClientError as e:
                logger.error(
                    "Failed to trigger backfill",
                    extra={
                        "source": result.source,
                        "error_code": e.response["Error"]["Code"],
                    },
                )
                errors += 1

    _publish_metrics(cloudwatch_client, len(stale_sources), backfills_triggered)

    status = "stale_data_found" if stale_sources else "all_fresh"

    logger.info(
        "Stale data check complete",
        extra={
            "status": status,
            "sources_checked": len(SOURCES),
            "stale_count": len(stale_sources),
            "backfills_triggered": backfills_triggered,
            "errors": errors,
        },
    )

    return {
        "status": status,
        "sources_checked": len(SOURCES),
        "backfills_triggered": backfills_triggered,
        "errors": errors,
    }
