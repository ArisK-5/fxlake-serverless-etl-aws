import json
import logging
import os
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

NAMESPACE = os.environ["METRIC_NAMESPACE"]
PIPELINE = os.environ["PIPELINE"]
FRESHNESS_THRESHOLD_DAYS = 2

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def publish_custom_metric(metric_name: str, value: int, workgroup: str) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "WorkGroup", "Value": workgroup},
                        {"Name": "Pipeline", "Value": PIPELINE},
                    ],
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
        logger.info(f"Published metric {metric_name}={value} to {NAMESPACE}")
    except Exception as e:
        # CloudWatch is non-critical; metric failure must not abort validation
        logger.error(
            f"Failed to publish metric {metric_name} to {NAMESPACE} "
            f"(workgroup={workgroup}, value={value}): {type(e).__name__}: {e}",
            exc_info=True,
        )


def _parse_freshness_result(result_set: dict) -> tuple[str | None, int]:
    """Extract latest_date and total_records from the Athena freshness query result.

    Expected schema: ``SELECT MAX(date) AS latest_date, COUNT(*) AS total_records``.
    Returns (latest_date_str | None, total_records).
    """
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return None, 0

    data_row = rows[1]["Data"]
    latest_date_str = data_row[0].get("VarCharValue") if data_row[0] else None
    total_records = int(data_row[1].get("VarCharValue", "0"))
    return latest_date_str, total_records


def lambda_handler(event: dict, context: Any) -> dict:
    query_execution_id = event.get("QueryExecutionId")
    if not query_execution_id:
        raise ValueError("Missing QueryExecutionId")

    try:
        execution = athena.get_query_execution(QueryExecutionId=query_execution_id)
    except ClientError as e:
        logger.error(
            f"Failed to retrieve Athena execution for "
            f"query_execution_id={query_execution_id}: "
            f"{e.response['Error']['Code']}",
            exc_info=True,
        )
        raise

    state = execution["QueryExecution"]["Status"]["State"]
    workgroup = execution["QueryExecution"].get("WorkGroup", "default")

    if state != "SUCCEEDED":
        logger.error(f"Athena query failed or incomplete. State: {state}")
        raise RuntimeError(f"Athena query did not succeed. Current state: {state}")

    try:
        response = athena.get_query_results(QueryExecutionId=query_execution_id)
    except ClientError as e:
        logger.error(
            f"Failed to fetch Athena results for "
            f"query_execution_id={query_execution_id}: "
            f"{e.response['Error']['Code']}",
            exc_info=True,
        )
        raise

    result_set = response.get("ResultSet", {})
    latest_date_str, total_records = _parse_freshness_result(result_set)

    is_empty = total_records == 0 or latest_date_str is None
    is_fresh = False
    if not is_empty and latest_date_str:
        latest = date.fromisoformat(latest_date_str)
        is_fresh = (date.today() - latest) <= timedelta(days=FRESHNESS_THRESHOLD_DAYS)

    publish_custom_metric("EmptyQueryResults", 1 if is_empty else 0, workgroup)
    publish_custom_metric("StaleFXData", 0 if is_fresh else 1, workgroup)

    logger.info(
        json.dumps(
            {
                "query_execution_id": query_execution_id,
                "latest_date": latest_date_str,
                "total_records": total_records,
                "is_fresh": is_fresh,
                "is_empty": is_empty,
                "namespace": NAMESPACE,
                "workgroup": workgroup,
            }
        )
    )

    return {
        "latest_date": latest_date_str,
        "total_records": total_records,
        "is_fresh": is_fresh,
        "is_empty": is_empty,
        "status": "FAILED" if is_empty else "SUCCEEDED",
    }
