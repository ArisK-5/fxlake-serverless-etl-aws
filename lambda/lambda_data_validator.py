import os
import re
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

logger = configure_logger("data-validator")

DATABASE_NAME = os.environ.get("DATABASE_NAME", "fxlake")
ATHENA_RESULTS_BUCKET = os.environ["ATHENA_RESULTS_BUCKET"]
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "fxlake")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/Validation")

POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90
_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

VALID_DOMAINS = frozenset({"fx_rates", "economic_indicators"})

_NULL_COLUMNS = {
    "fx_rates": ("date", "rate"),
    "economic_indicators": ("date", "value"),
}


@dataclass(frozen=True)
class ValidationCheck:
    domain: str
    check_name: str
    passed: bool
    detail: str


def _validate_event(event: dict) -> dict[str, Any]:
    database = event.get("database_name", DATABASE_NAME)
    domains = event.get("domains", sorted(VALID_DOMAINS))
    expected_counts = event.get("expected_counts", {})
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"

    for d in domains:
        if d not in VALID_DOMAINS:
            raise ValueError(
                f"Invalid domain: {d!r} — must be one of {sorted(VALID_DOMAINS)}"
            )

    return {
        "database": database,
        "domains": domains,
        "output_location": output_location,
        "expected_counts": expected_counts,
    }


def _build_row_count_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"SELECT source, COUNT(*) AS rows, "
        f"MIN(date) AS min_date, MAX(date) AS max_date\n"
        f"FROM {table_name}\n"
        f"GROUP BY source"
    )


def _build_null_check_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    cols = _NULL_COLUMNS.get(table_name, ("date",))
    conditions = " OR ".join(f"{c} IS NULL" for c in cols)
    return f"SELECT COUNT(*) AS null_rows FROM {table_name} WHERE {conditions}"


def _execute_and_wait(
    athena_client: Any,
    query: str,
    database: str,
    output_location: str,
    workgroup: str,
) -> dict:
    response = athena_client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
        WorkGroup=workgroup,
    )
    qid = response["QueryExecutionId"]
    logger.info("Started Athena query", extra={"query_execution_id": qid})

    for _ in range(MAX_POLL_ATTEMPTS):
        exec_resp = athena_client.get_query_execution(QueryExecutionId=qid)
        state = exec_resp["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            results = athena_client.get_query_results(QueryExecutionId=qid)
            return results["ResultSet"]
        if state in ("FAILED", "CANCELLED"):
            reason = exec_resp["QueryExecution"]["Status"].get(
                "StateChangeReason", "unknown"
            )
            raise RuntimeError(f"Athena query {qid} {state}: {reason}")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Athena query {qid} did not complete after {MAX_POLL_ATTEMPTS} attempts"
    )


def _parse_row_count_results(result_set: dict) -> dict[str, dict[str, Any]]:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return {}

    parsed: dict[str, dict[str, Any]] = {}
    for row in rows[1:]:
        data = row.get("Data", [])
        if len(data) < 4:
            continue
        source = data[0].get("VarCharValue", "")
        parsed[source] = {
            "rows": int(data[1].get("VarCharValue", "0")),
            "min_date": data[2].get("VarCharValue", ""),
            "max_date": data[3].get("VarCharValue", ""),
        }
    return parsed


def _parse_null_check_results(result_set: dict) -> int:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return -1
    data = rows[1].get("Data", [])
    if not data:
        return -1
    return int(data[0].get("VarCharValue", "-1"))


def _run_validation_suite(
    athena_client: Any,
    domains: list[str],
    database: str,
    output_location: str,
    workgroup: str,
    expected_counts: dict[str, dict[str, int]],
) -> list[ValidationCheck]:
    checks: list[ValidationCheck] = []

    for domain in domains:
        rc_query = _build_row_count_query(domain)
        rc_result = _execute_and_wait(
            athena_client, rc_query, database, output_location, workgroup
        )
        source_counts = _parse_row_count_results(rc_result)

        if not source_counts:
            checks.append(ValidationCheck(
                domain=domain,
                check_name="row_count",
                passed=False,
                detail=f"No rows found in {domain}",
            ))
        else:
            total = sum(s["rows"] for s in source_counts.values())
            sources_detail = ", ".join(
                f"{src}: {info['rows']} rows ({info['min_date']} to {info['max_date']})"
                for src, info in sorted(source_counts.items())
            )
            checks.append(ValidationCheck(
                domain=domain,
                check_name="row_count",
                passed=True,
                detail=f"{total} total rows — {sources_detail}",
            ))

        nc_query = _build_null_check_query(domain)
        nc_result = _execute_and_wait(
            athena_client, nc_query, database, output_location, workgroup
        )
        null_count = _parse_null_check_results(nc_result)

        if null_count < 0:
            checks.append(ValidationCheck(
                domain=domain,
                check_name="null_check",
                passed=False,
                detail="Could not parse null check results from Athena",
            ))
        else:
            checks.append(ValidationCheck(
                domain=domain,
                check_name="null_check",
                passed=null_count == 0,
                detail=(
                    "No null values in required columns"
                    if null_count == 0
                    else f"Found {null_count} rows with null values in required columns"
                ),
            ))

        domain_expected = expected_counts.get(domain, {})
        for src, expected in domain_expected.items():
            actual = source_counts.get(src, {}).get("rows", 0)
            checks.append(ValidationCheck(
                domain=domain,
                check_name="expected_count",
                passed=actual == expected,
                detail=(
                    f"{src}: expected {expected}, got {actual}"
                ),
            ))

    return checks


def _publish_validation_metric(
    cloudwatch_client: Any,
    passed: bool,
) -> None:
    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "DataValidation",
                    "Value": 1.0 if passed else 0.0,
                    "Unit": "None",
                    "Dimensions": [
                        {"Name": "Environment", "Value": "production"},
                    ],
                }
            ],
        )
    except ClientError as e:
        logger.error(
            "Failed to publish validation metric",
            extra={"error_code": e.response["Error"]["Code"]},
        )


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)
    params = _validate_event(event)

    athena_client = boto3.client("athena")
    cloudwatch_client = boto3.client("cloudwatch")

    with Timer() as timer:
        checks = _run_validation_suite(
            athena_client=athena_client,
            domains=params["domains"],
            database=params["database"],
            output_location=params["output_location"],
            workgroup=WORKGROUP,
            expected_counts=params["expected_counts"],
        )

    passed_count = sum(1 for c in checks if c.passed)
    all_passed = all(c.passed for c in checks)

    for check in checks:
        log_fn = logger.info if check.passed else logger.warning
        log_fn(
            f"Validation {check.check_name}: {'PASS' if check.passed else 'FAIL'}",
            extra={
                "domain": check.domain,
                "check_name": check.check_name,
                "passed": check.passed,
                "detail": check.detail,
            },
        )

    _publish_validation_metric(cloudwatch_client, all_passed)

    logger.info(
        "Data validation complete",
        extra={
            "passed": all_passed,
            "checks_total": len(checks),
            "checks_passed": passed_count,
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "status": "PASSED" if all_passed else "FAILED",
        "passed": all_passed,
        "checks_total": len(checks),
        "checks_passed": passed_count,
        "checks": [
            {
                "domain": c.domain,
                "check_name": c.check_name,
                "passed": c.passed,
                "detail": c.detail,
            }
            for c in checks
        ],
    }
