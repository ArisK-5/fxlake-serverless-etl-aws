import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

NAMESPACE = os.environ["METRIC_NAMESPACE"]
PIPELINE = os.environ["PIPELINE"]

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def publish_custom_metric(value: int, workgroup: str) -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": "EmptyQueryResults",
                    "Dimensions": [
                        {"Name": "WorkGroup", "Value": workgroup},
                        {"Name": "Pipeline", "Value": PIPELINE},
                    ],
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
        logger.info(f"Published metric EmptyQueryResults={value} to {NAMESPACE}")
    except Exception as e:
        # CloudWatch is non-critical; metric failure must not abort validation
        logger.error(
            f"Failed to publish EmptyQueryResults metric to {NAMESPACE} "
            f"(workgroup={workgroup}, value={value}): {type(e).__name__}: {e}",
            exc_info=True,
        )


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

    result_set = response.get("ResultSet", {}).get("Rows", [])
    rows = len(result_set) - 1 if len(result_set) > 1 else 0

    publish_custom_metric(1 if rows == 0 else 0, workgroup)

    logger.info(
        json.dumps(
            {
                "query_execution_id": query_execution_id,
                "rows_returned": rows,
                "namespace": NAMESPACE,
                "workgroup": workgroup,
            }
        )
    )

    return {
        "rows": rows,
        "is_empty": rows == 0,
        "status": "FAILED" if rows == 0 else "SUCCEEDED",
    }
