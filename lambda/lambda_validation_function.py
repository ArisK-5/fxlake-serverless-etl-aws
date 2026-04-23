import os
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from common.logging import Timer, configure_logger, inject_request_id

NAMESPACE = os.environ["METRIC_NAMESPACE"]
PIPELINE = os.environ["PIPELINE"]
SLA_NAMESPACE = os.environ["SLA_NAMESPACE"]
FRESHNESS_THRESHOLD_DAYS = 2

athena = boto3.client("athena")
cloudwatch = boto3.client("cloudwatch")

logger = configure_logger("validation")


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
        logger.info(
            "Published CloudWatch metric",
            extra={"metric": metric_name, "value": value, "namespace": NAMESPACE},
        )
    except Exception as e:
        # CloudWatch is non-critical; metric failure must not abort validation
        logger.error(
            "Failed to publish CloudWatch metric",
            extra={
                "metric": metric_name,
                "namespace": NAMESPACE,
                "workgroup": workgroup,
                "value": value,
                "error_type": type(e).__name__,
                "error": str(e),
            },
            exc_info=True,
        )


def publish_sla_metric(is_compliant: bool) -> None:
    value = 1.0 if is_compliant else 0.0
    try:
        cloudwatch.put_metric_data(
            Namespace=SLA_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "PipelineSLACompliance",
                    "Dimensions": [
                        {"Name": "Environment", "Value": "production"},
                    ],
                    "Value": value,
                    "Unit": "None",
                }
            ],
        )
        logger.info(
            "Published SLA metric",
            extra={"compliant": is_compliant, "value": value, "namespace": SLA_NAMESPACE},
        )
    except Exception as e:
        logger.error(
            "Failed to publish SLA metric",
            extra={
                "namespace": SLA_NAMESPACE,
                "value": value,
                "error_type": type(e).__name__,
                "error": str(e),
            },
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

    data_row = rows[1].get("Data", [])
    if len(data_row) < 2:
        return None, 0
    latest_date_str = data_row[0].get("VarCharValue") if data_row[0] else None
    total_records = int(data_row[1].get("VarCharValue", "0"))
    return latest_date_str, total_records


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    query_execution_id = event.get("QueryExecutionId")
    if not query_execution_id:
        raise ValueError("Missing QueryExecutionId")

    with Timer() as timer:
        try:
            execution = athena.get_query_execution(QueryExecutionId=query_execution_id)
        except ClientError as e:
            logger.error(
                "Failed to retrieve Athena execution",
                extra={
                    "query_execution_id": query_execution_id,
                    "error_code": e.response["Error"]["Code"],
                },
                exc_info=True,
            )
            raise

        state = execution["QueryExecution"]["Status"]["State"]
        workgroup = execution["QueryExecution"].get("WorkGroup", "default")

        if state != "SUCCEEDED":
            logger.error(
                "Athena query failed or incomplete",
                extra={"query_execution_id": query_execution_id, "state": state},
            )
            raise RuntimeError(f"Athena query did not succeed. Current state: {state}")

        try:
            response = athena.get_query_results(QueryExecutionId=query_execution_id)
        except ClientError as e:
            logger.error(
                "Failed to fetch Athena results",
                extra={
                    "query_execution_id": query_execution_id,
                    "error_code": e.response["Error"]["Code"],
                },
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
        publish_sla_metric(is_compliant=is_fresh and not is_empty)

    logger.info(
        "Validation complete",
        extra={
            "query_execution_id": query_execution_id,
            "latest_date": latest_date_str,
            "total_records": total_records,
            "is_fresh": is_fresh,
            "is_empty": is_empty,
            "workgroup": workgroup,
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "latest_date": latest_date_str,
        "total_records": total_records,
        "is_fresh": is_fresh,
        "is_empty": is_empty,
        "status": "FAILED" if is_empty else "SUCCEEDED",
    }
