import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from common.logging import Timer, configure_logger, inject_request_id

if os.getenv("AWS_XRAY_DAEMON_ADDRESS"):
    try:
        from aws_xray_sdk.core import patch_all

        patch_all()
    except ImportError:
        pass

logger = configure_logger("iceberg-maintenance")

DATABASE_NAME = os.environ.get("DATABASE_NAME", "fxlake")
ATHENA_RESULTS_BUCKET = os.environ["ATHENA_RESULTS_BUCKET"]
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "fxlake")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/Maintenance")

TABLES = ("fx_rates_v3", "economic_indicators_v3")
POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90


@dataclass(frozen=True)
class MaintenanceResult:
    table: str
    operation: str
    success: bool
    duration_ms: float
    detail: str


def _execute_statement(
    athena_client: Any,
    sql: str,
    database: str,
    output_location: str,
    workgroup: str,
) -> bool:
    query_id = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
        WorkGroup=workgroup,
    )["QueryExecutionId"]

    logger.info("Started Athena query", extra={"query_id": query_id, "sql": sql})

    for _ in range(MAX_POLL_ATTEMPTS):
        response = athena_client.get_query_execution(QueryExecutionId=query_id)
        state = response["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            return True
        if state in ("FAILED", "CANCELLED"):
            reason = (
                response["QueryExecution"]["Status"]
                .get("StateChangeReason", "Unknown")
            )
            logger.error(
                "Athena query failed",
                extra={"query_id": query_id, "state": state, "reason": reason},
            )
            return False

        time.sleep(POLL_INTERVAL_SECONDS)

    logger.error("Athena query timed out", extra={"query_id": query_id})
    return False


def _run_maintenance(
    athena_client: Any,
    database: str,
    output_location: str,
    workgroup: str,
    tables: tuple[str, ...] = TABLES,
) -> list[MaintenanceResult]:
    results: list[MaintenanceResult] = []

    for table in tables:
        for operation, sql in (
            ("OPTIMIZE", f"OPTIMIZE {table} REWRITE DATA USING BIN_PACK"),
            ("VACUUM", f"VACUUM {table}"),
        ):
            timer = Timer()
            with timer:
                success = _execute_statement(
                    athena_client, sql, database, output_location, workgroup
                )

            detail = "Completed" if success else "Failed"
            result = MaintenanceResult(
                table=table,
                operation=operation,
                success=success,
                duration_ms=timer.duration_ms,
                detail=detail,
            )
            results.append(result)
            logger.info(
                "Maintenance operation complete",
                extra={
                    "table": table,
                    "operation": operation,
                    "success": success,
                    "duration_ms": timer.duration_ms,
                },
            )

    return results


def _publish_metrics(
    cw_client: Any,
    results: list[MaintenanceResult],
    namespace: str,
) -> None:
    failed_count = sum(1 for r in results if not r.success)
    try:
        cw_client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": "MaintenanceJobFailed",
                    "Value": failed_count,
                    "Unit": "Count",
                },
                {
                    "MetricName": "MaintenanceOperationsTotal",
                    "Value": len(results),
                    "Unit": "Count",
                },
            ],
        )
    except ClientError:
        logger.warning("Failed to publish maintenance metrics")


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    database = event.get("database_name", DATABASE_NAME)
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"
    workgroup = event.get("workgroup", WORKGROUP)

    tables_override = event.get("tables")
    tables = tuple(tables_override) if tables_override else TABLES

    athena_client = boto3.client("athena")
    results = _run_maintenance(athena_client, database, output_location, workgroup, tables)

    cw_client = boto3.client("cloudwatch")
    _publish_metrics(cw_client, results, METRIC_NAMESPACE)

    failed = [r for r in results if not r.success]
    status = "success" if not failed else "partial_failure"

    summary = {
        "status": status,
        "total_operations": len(results),
        "failed_count": len(failed),
        "results": [
            {
                "table": r.table,
                "operation": r.operation,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "detail": r.detail,
            }
            for r in results
        ],
    }

    logger.info("Maintenance complete", extra=summary)
    return summary
