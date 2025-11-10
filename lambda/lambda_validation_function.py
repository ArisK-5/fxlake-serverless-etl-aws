import json
import logging
import os

import boto3

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

NAMESPACE = os.getenv("METRIC_NAMESPACE")


def lambda_handler(event, context):
    query_execution_id = event.get("QueryExecutionId")
    if not query_execution_id:
        raise ValueError("Missing QueryExecutionId")

    # Check Athena query state
    execution = athena.get_query_execution(QueryExecutionId=query_execution_id)
    state = execution["QueryExecution"]["Status"]["State"]

    if state != "SUCCEEDED":
        logger.error(f"Athena query failed or incomplete. State: {state}")
        raise RuntimeError(f"Athena query did not succeed. Current state: {state}")

    # Fetch results
    response = athena.get_query_results(QueryExecutionId=query_execution_id)
    result_set = response.get("ResultSet", {}).get("Rows", [])
    rows = len(result_set) - 1 if len(result_set) > 1 else 0

    # Publish metric
    cloudwatch.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {
                "MetricName": "EmptyQueryResults",
                "Value": 1 if rows == 0 else 0,
                "Unit": "Count",
            }
        ],
    )

    logger.info(
        json.dumps(
            {
                "query_execution_id": query_execution_id,
                "rows_returned": rows,
                "namespace": NAMESPACE,
            }
        )
    )

    return {
        "rows": rows,
        "is_empty": rows == 0,
        "status": "FAILED" if rows == 0 else "SUCCEEDED",
    }
