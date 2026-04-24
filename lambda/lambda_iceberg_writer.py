import json
import os
import re
import time
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

logger = configure_logger("iceberg-writer")

DATABASE_NAME = os.environ.get("DATABASE_NAME", "fxlake")
ATHENA_RESULTS_BUCKET = os.environ.get("ATHENA_RESULTS_BUCKET", "")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "fxlake")
RAW_BUCKET = os.environ.get("RAW_BUCKET", "")

POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90
_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _parse_fx_rates(raw_data: dict) -> list[dict[str, Any]]:
    """Extract flat FX rate rows from Frankfurter/ECB raw JSON.

    Frankfurter format: {"base": "EUR", "rates": {"2024-01-02": {"USD": 1.1, ...}, ...}}
    ECB format: same structure after normalisation in the ECB handler.

    Returns a list of dicts with keys: date, source, base_currency, target_currency, rate.
    """
    source = raw_data.get("source", "frankfurter")
    base_currency = raw_data.get("base", "EUR")
    rates_by_date = raw_data.get("rates", {})

    rows: list[dict[str, Any]] = []
    for rate_date, currencies in sorted(rates_by_date.items()):
        for target_currency, rate in sorted(currencies.items()):
            rows.append({
                "date": rate_date,
                "source": source,
                "base_currency": base_currency,
                "target_currency": target_currency,
                "rate": float(rate),
            })
    return rows


def _build_insert_query(table_name: str, rows: list[dict[str, Any]]) -> str:
    """Build an Athena INSERT INTO ... VALUES query for the Iceberg table.

    Athena Iceberg INSERT INTO supports standard SQL VALUES syntax.
    String values are single-quoted and escaped; doubles are unquoted.
    """
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    if not rows:
        raise ValueError("No rows to insert — cannot build INSERT query")

    value_tuples: list[str] = []
    for row in rows:
        escaped_date = row["date"].replace("'", "''")
        escaped_source = row["source"].replace("'", "''")
        escaped_base = row["base_currency"].replace("'", "''")
        escaped_target = row["target_currency"].replace("'", "''")
        value_tuples.append(
            f"('{escaped_date}', '{escaped_source}', '{escaped_base}', "
            f"'{escaped_target}', {row['rate']})"
        )

    values_clause = ",\n".join(value_tuples)
    return (
        f"INSERT INTO {table_name} (date, source, base_currency, target_currency, rate)\n"
        f"VALUES\n{values_clause}"
    )


def _read_raw_json(s3_client: Any, bucket: str, key: str) -> dict:
    """Download and parse a JSON object from S3."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)
    except ClientError as e:
        logger.error(
            "Failed to read raw JSON from S3",
            extra={
                "bucket": bucket,
                "key": key,
                "error_code": e.response["Error"]["Code"],
            },
            exc_info=True,
        )
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(
            "Failed to parse raw JSON",
            extra={"bucket": bucket, "key": key, "error": str(e)},
            exc_info=True,
        )
        raise


def _execute_athena_query(
    athena_client: Any,
    query: str,
    database: str,
    output_location: str,
    workgroup: str,
) -> str:
    """Start an Athena query and return the QueryExecutionId."""
    try:
        response = athena_client.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": database},
            ResultConfiguration={"OutputLocation": output_location},
            WorkGroup=workgroup,
        )
        query_execution_id = response["QueryExecutionId"]
        logger.info(
            "Started Athena query",
            extra={
                "query_execution_id": query_execution_id,
                "database": database,
                "workgroup": workgroup,
            },
        )
        return query_execution_id
    except ClientError as e:
        logger.error(
            "Failed to start Athena query",
            extra={
                "database": database,
                "error_code": e.response["Error"]["Code"],
            },
            exc_info=True,
        )
        raise


def _poll_query_completion(
    athena_client: Any,
    query_execution_id: str,
    poll_interval: int = POLL_INTERVAL_SECONDS,
    max_attempts: int = MAX_POLL_ATTEMPTS,
) -> dict:
    """Poll Athena until the query reaches a terminal state.

    Returns the final GetQueryExecution response.
    Raises TimeoutError if max_attempts exceeded, RuntimeError on FAILED/CANCELLED.
    """
    for attempt in range(1, max_attempts + 1):
        response = athena_client.get_query_execution(
            QueryExecutionId=query_execution_id
        )
        state = response["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            logger.info(
                "Athena query succeeded",
                extra={
                    "query_execution_id": query_execution_id,
                    "poll_attempts": attempt,
                },
            )
            return response

        if state in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get(
                "StateChangeReason", "unknown"
            )
            logger.error(
                "Athena query failed",
                extra={
                    "query_execution_id": query_execution_id,
                    "state": state,
                    "reason": reason,
                },
            )
            raise RuntimeError(
                f"Athena query {query_execution_id} {state}: {reason}"
            )

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Athena query {query_execution_id} did not complete "
        f"after {max_attempts} poll attempts"
    )


def _validate_event(event: dict) -> tuple[str, str, str, str, str]:
    """Extract and validate required parameters from the Lambda event."""
    raw_bucket = event.get("raw_bucket", RAW_BUCKET)
    raw_key = event.get("raw_key", "")
    target_table = event.get("target_table", "fx_rates")
    database = event.get("database_name", DATABASE_NAME)
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"

    if not raw_key:
        raise ValueError("Missing required 'raw_key' in event")
    if not raw_bucket:
        raise ValueError("Missing raw_bucket — set RAW_BUCKET env var or pass in event")

    return raw_bucket, raw_key, target_table, database, output_location


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)
    raw_bucket, raw_key, target_table, database, output_location = _validate_event(event)

    s3_client = boto3.client("s3")
    athena_client = boto3.client("athena")

    with Timer() as timer:
        raw_data = _read_raw_json(s3_client, raw_bucket, raw_key)
        rows = _parse_fx_rates(raw_data)

        if not rows:
            logger.warning(
                "No FX rate rows parsed from raw data — skipping Iceberg write",
                extra={"bucket": raw_bucket, "key": raw_key},
            )
            return {
                "status": "no_data",
                "raw_key": raw_key,
                "rows_parsed": 0,
            }

        query = _build_insert_query(target_table, rows)
        query_execution_id = _execute_athena_query(
            athena_client, query, database, output_location, WORKGROUP
        )
        _poll_query_completion(athena_client, query_execution_id)

    logger.info(
        "Iceberg write complete",
        extra={
            "raw_key": raw_key,
            "target_table": target_table,
            "rows_inserted": len(rows),
            "query_execution_id": query_execution_id,
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "status": "ok",
        "raw_key": raw_key,
        "target_table": target_table,
        "rows_inserted": len(rows),
        "query_execution_id": query_execution_id,
    }
