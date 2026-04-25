import json
import os
import re
import time
from typing import Any

import boto3
import polars as pl
from botocore.exceptions import ClientError
from common.logging import Timer, configure_logger, inject_request_id
from quality import (
    QualityResult,
    build_quality_report,
    has_critical_failures,
    run_economic_checks,
    run_fx_checks,
)

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
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
QUARANTINE_BUCKET = os.environ["QUARANTINE_BUCKET"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/Quality")

POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90
ATHENA_QUERY_STRING_LIMIT = 262_144
_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

VALID_DOMAINS = frozenset({"fx_rates", "economic_indicators"})

_FX_COLUMNS = ("date", "source", "base_currency", "target_currency", "rate")
_ECON_COLUMNS = ("date", "source", "series_id", "value")


def _parse_fx_rates(raw_data: dict) -> list[dict[str, Any]]:
    """Extract flat FX rate rows from Frankfurter/ECB raw JSON."""
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


def _parse_economic_indicators(raw_data: dict) -> list[dict[str, Any]]:
    """Extract flat economic indicator rows from FRED raw JSON.

    Handles both formats:
      - dict: {"2020-01-01": 3.6, ...}  (produced by FRED ingestion Lambda)
      - list: [{"date": "2020-01-01", "value": 3.6}, ...]
    """
    source = raw_data.get("source", "fred")
    series_id = raw_data.get("series_id", "")
    observations = raw_data.get("observations", {})

    rows: list[dict[str, Any]] = []
    if isinstance(observations, dict):
        for obs_date, obs_value in sorted(observations.items()):
            rows.append({
                "date": obs_date,
                "source": source,
                "series_id": series_id,
                "value": float(obs_value),
            })
    else:
        for obs in observations:
            rows.append({
                "date": obs["date"],
                "source": source,
                "series_id": series_id,
                "value": float(obs["value"]),
            })
    return rows


def _format_value_tuple(row: dict[str, Any], domain: str) -> str:
    """Format a single row as a SQL VALUES tuple."""
    if domain == "economic_indicators":
        escaped_date = row["date"].replace("'", "''")
        escaped_source = row["source"].replace("'", "''")
        escaped_series = row["series_id"].replace("'", "''")
        return (
            f"('{escaped_date}', '{escaped_source}', "
            f"'{escaped_series}', {row['value']})"
        )
    escaped_date = row["date"].replace("'", "''")
    escaped_source = row["source"].replace("'", "''")
    escaped_base = row["base_currency"].replace("'", "''")
    escaped_target = row["target_currency"].replace("'", "''")
    return (
        f"('{escaped_date}', '{escaped_source}', '{escaped_base}', "
        f"'{escaped_target}', {row['rate']})"
    )


def _build_insert_queries(
    table_name: str,
    rows: list[dict[str, Any]],
    domain: str = "fx_rates",
) -> list[str]:
    """Build batched Athena INSERT INTO queries that stay within the query string limit."""
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    if not rows:
        raise ValueError("No rows to insert — cannot build INSERT query")

    columns = _ECON_COLUMNS if domain == "economic_indicators" else _FX_COLUMNS
    columns_clause = ", ".join(columns)
    header = f"INSERT INTO {table_name} ({columns_clause})\nVALUES\n"

    queries: list[str] = []
    current_tuples: list[str] = []
    current_size = len(header)

    for row in rows:
        tuple_str = _format_value_tuple(row, domain)
        separator_len = 2 if current_tuples else 0
        if current_tuples and current_size + separator_len + len(tuple_str) > ATHENA_QUERY_STRING_LIMIT:
            queries.append(header + ",\n".join(current_tuples))
            current_tuples = []
            current_size = len(header)
        current_tuples.append(tuple_str)
        current_size += len(tuple_str) + separator_len

    if current_tuples:
        queries.append(header + ",\n".join(current_tuples))

    return queries


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
    """Poll Athena until the query reaches a terminal state."""
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


def _rows_to_dataframe(rows: list[dict[str, Any]], domain: str) -> pl.DataFrame:
    """Convert parsed rows to a Polars DataFrame for quality checks."""
    if domain == "economic_indicators":
        schema = {"date": pl.Utf8, "source": pl.Utf8, "series_id": pl.Utf8, "value": pl.Float64}
    else:
        schema = {
            "date": pl.Utf8, "source": pl.Utf8, "base_currency": pl.Utf8,
            "target_currency": pl.Utf8, "rate": pl.Float64,
        }
    return pl.DataFrame(rows, schema=schema)


def _run_quality_checks(
    rows: list[dict[str, Any]],
    domain: str,
    raw_key: str,
    s3_client: Any,
) -> list[QualityResult]:
    """Run quality checks on parsed rows. Write report to S3. Quarantine + raise on CRITICAL."""
    df = _rows_to_dataframe(rows, domain)

    if domain == "economic_indicators":
        results = run_economic_checks(df)
    else:
        results = run_fx_checks(df)

    report = build_quality_report(results, raw_key, domain)
    _write_quality_report(s3_client, report, raw_key, domain)

    failed = [r for r in results if not r.passed]
    if not failed:
        logger.info("All quality checks passed", extra={"raw_key": raw_key, "domain": domain})
        return results

    for r in failed:
        logger.warning(
            "Quality check failed",
            extra={
                "check_name": r.check_name,
                "level": r.level.value,
                "detail": r.message,
                "domain": domain,
            },
        )

    _publish_quality_metric("DataQualityChecksFailed", float(len(failed)), domain)

    if has_critical_failures(results):
        _quarantine_records(s3_client, rows, raw_key, domain)
        _publish_quality_metric("RecordsQuarantined", float(len(rows)), domain)
        raise ValueError(
            f"CRITICAL quality check(s) failed for {raw_key}: "
            + "; ".join(r.message for r in failed if r.level.value == "CRITICAL")
        )

    return results


def _write_quality_report(
    s3_client: Any,
    report: dict,
    raw_key: str,
    domain: str,
) -> str:
    """Write quality report JSON to the processed bucket."""
    stem = raw_key.split("/")[-1].replace(".json", "")
    report_key = f"{domain}/quality_reports/{stem}_quality.json"
    body = json.dumps(report, indent=2).encode()
    try:
        s3_client.put_object(
            Bucket=PROCESSED_BUCKET, Key=report_key, Body=body, ContentType="application/json"
        )
    except ClientError as e:
        logger.error(
            "Failed to write quality report",
            extra={
                "bucket": PROCESSED_BUCKET,
                "key": report_key,
                "error_code": e.response["Error"]["Code"],
            },
            exc_info=True,
        )
        raise
    logger.info(
        "Quality report written",
        extra={"bucket": PROCESSED_BUCKET, "key": report_key},
    )
    return report_key


def _quarantine_records(
    s3_client: Any,
    rows: list[dict[str, Any]],
    raw_key: str,
    domain: str,
) -> str:
    """Write failing records to the quarantine bucket."""
    stem = raw_key.split("/")[-1].replace(".json", "")
    q_key = f"{domain}/quarantine/{stem}.json"
    body = json.dumps(rows).encode()
    try:
        s3_client.put_object(
            Bucket=QUARANTINE_BUCKET, Key=q_key, Body=body, ContentType="application/json"
        )
    except ClientError as e:
        logger.error(
            "Failed to quarantine records",
            extra={
                "bucket": QUARANTINE_BUCKET,
                "key": q_key,
                "record_count": len(rows),
                "error_code": e.response["Error"]["Code"],
            },
            exc_info=True,
        )
        raise
    logger.warning(
        "Quarantined records",
        extra={"bucket": QUARANTINE_BUCKET, "key": q_key, "record_count": len(rows)},
    )
    return q_key


def _publish_quality_metric(
    metric_name: str,
    value: float,
    domain: str,
) -> None:
    """Publish a quality metric to CloudWatch. Swallows errors to avoid aborting pipeline."""
    try:
        cloudwatch = boto3.client("cloudwatch")
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Domain", "Value": domain}],
                }
            ],
        )
    except ClientError as e:
        logger.error(
            "Failed to publish quality metric",
            extra={
                "metric_name": metric_name,
                "error_code": e.response["Error"]["Code"],
            },
        )


def _validate_event(event: dict) -> tuple[str, str, str, str, str, str]:
    """Extract and validate required parameters from the Lambda event."""
    raw_bucket = event.get("raw_bucket", RAW_BUCKET)
    raw_key = event.get("raw_key", "")
    domain = event.get("domain", "fx_rates")
    database = event.get("database_name", DATABASE_NAME)
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"

    if domain not in VALID_DOMAINS:
        raise ValueError(f"Invalid domain: {domain!r} — must be one of {sorted(VALID_DOMAINS)}")
    if not raw_key:
        raise ValueError("Missing required 'raw_key' in event")
    if not raw_bucket:
        raise ValueError("Missing raw_bucket — set RAW_BUCKET env var or pass in event")

    target_table = event.get("target_table", domain)

    return raw_bucket, raw_key, target_table, database, output_location, domain


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)
    raw_bucket, raw_key, target_table, database, output_location, domain = _validate_event(event)

    s3_client = boto3.client("s3")
    athena_client = boto3.client("athena")

    with Timer() as timer:
        raw_data = _read_raw_json(s3_client, raw_bucket, raw_key)

        if domain == "economic_indicators":
            rows = _parse_economic_indicators(raw_data)
        else:
            rows = _parse_fx_rates(raw_data)

        if not rows:
            logger.warning(
                "No rows parsed from raw data — skipping Iceberg write",
                extra={"bucket": raw_bucket, "key": raw_key, "domain": domain},
            )
            return {
                "status": "no_data",
                "raw_key": raw_key,
                "domain": domain,
                "rows_parsed": 0,
            }

        _run_quality_checks(rows, domain, raw_key, s3_client)

        queries = _build_insert_queries(target_table, rows, domain)
        query_execution_ids: list[str] = []
        for i, query in enumerate(queries):
            logger.info(
                "Executing INSERT batch",
                extra={"batch": i + 1, "total_batches": len(queries), "domain": domain},
            )
            qid = _execute_athena_query(
                athena_client, query, database, output_location, WORKGROUP
            )
            _poll_query_completion(athena_client, qid)
            query_execution_ids.append(qid)

    logger.info(
        "Iceberg write complete",
        extra={
            "raw_key": raw_key,
            "target_table": target_table,
            "domain": domain,
            "rows_inserted": len(rows),
            "batches": len(query_execution_ids),
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "status": "ok",
        "raw_key": raw_key,
        "target_table": target_table,
        "domain": domain,
        "rows_inserted": len(rows),
        "batches": len(query_execution_ids),
    }
