import json
import logging
import os

import boto3

NAMESPACE = os.getenv("METRIC_NAMESPACE")
PIPELINE = os.getenv("PIPELINE")

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def publish_custom_metric(value, workgroup):
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
        logger.error(f"Failed to publish metric: {e}")


def lambda_handler(event, context):
    query_execution_id = event.get("QueryExecutionId")
    if not query_execution_id:
        raise ValueError("Missing QueryExecutionId")

    # Get Athena query execution status
    execution = athena.get_query_execution(QueryExecutionId=query_execution_id)
    state = execution["QueryExecution"]["Status"]["State"]
    workgroup = execution["QueryExecution"].get("WorkGroup", "default")

    if state != "SUCCEEDED":
        logger.error(f"Athena query failed or incomplete. State: {state}")
        raise RuntimeError(f"Athena query did not succeed. Current state: {state}")

    # Fetch results
    response = athena.get_query_results(QueryExecutionId=query_execution_id)
    result_set = response.get("ResultSet", {}).get("Rows", [])
    rows = len(result_set) - 1 if len(result_set) > 1 else 0

    # Publish metric with dimensions
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
