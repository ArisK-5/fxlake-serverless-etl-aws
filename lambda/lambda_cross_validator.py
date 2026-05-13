import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
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

logger = configure_logger("cross-validator")

DATABASE_NAME = os.environ.get("DATABASE_NAME", "fxlake")
ATHENA_RESULTS_BUCKET = os.environ["ATHENA_RESULTS_BUCKET"]
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "fxlake")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/CrossValidation")

POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90
RATE_DEVIATION_THRESHOLD = 0.01
TEMPORAL_GAP_THRESHOLD_DAYS = 1
_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class CrossValidationCheck:
    check_name: str
    passed: bool
    detail: str
    metric_value: float

    def __post_init__(self) -> None:
        if self.passed and self.metric_value < 0:
            raise ValueError(
                f"Passed check '{self.check_name}' cannot have negative metric_value"
            )
        if not self.passed and self.check_name == "rate_consistency" and self.metric_value == 0.0:
            raise ValueError(
                "Failed rate_consistency check must have non-zero metric_value"
            )


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
            result_set = results.get("ResultSet")
            if result_set is None:
                raise RuntimeError(
                    f"Athena query {qid} returned no ResultSet"
                )
            return result_set
        if state in ("FAILED", "CANCELLED"):
            reason = exec_resp["QueryExecution"]["Status"].get(
                "StateChangeReason", "unknown"
            )
            raise RuntimeError(f"Athena query {qid} {state}: {reason}")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Athena query {qid} did not complete after {MAX_POLL_ATTEMPTS} attempts"
    )


def build_rate_consistency_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"SELECT a.date, a.target_currency, "
        f"a.rate AS frankfurter_rate, b.rate AS ecb_rate, "
        f"ABS(a.rate - b.rate) / NULLIF(b.rate, 0) AS deviation\n"
        f"FROM {table_name} a\n"
        f"JOIN {table_name} b ON a.date = b.date "
        f"AND a.target_currency = b.target_currency\n"
        f"WHERE a.source = 'frankfurter' AND b.source = 'ecb'\n"
        f"AND ABS(a.rate - b.rate) / NULLIF(b.rate, 0) > {RATE_DEVIATION_THRESHOLD}"
    )


def build_temporal_consistency_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"SELECT source, MIN(date) AS min_date, MAX(date) AS max_date\n"
        f"FROM {table_name}\n"
        f"GROUP BY source"
    )


def build_volume_consistency_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"SELECT source, COUNT(*) AS row_count\n"
        f"FROM {table_name}\n"
        f"GROUP BY source"
    )


def parse_rate_consistency_results(result_set: dict) -> list[dict[str, Any]]:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return []

    discrepancies: list[dict[str, Any]] = []
    for row in rows[1:]:
        data = row.get("Data", [])
        if len(data) < 5:
            continue
        try:
            discrepancies.append({
                "date": data[0].get("VarCharValue", ""),
                "target_currency": data[1].get("VarCharValue", ""),
                "frankfurter_rate": float(data[2]["VarCharValue"]),
                "ecb_rate": float(data[3]["VarCharValue"]),
                "deviation": float(data[4]["VarCharValue"]),
            })
        except (KeyError, ValueError) as e:
            logger.warning(
                "Skipping malformed rate consistency row",
                extra={"error": str(e), "row_data": str(data)},
            )
    return discrepancies


def parse_temporal_results(result_set: dict) -> dict[str, dict[str, str]]:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return {}

    parsed: dict[str, dict[str, str]] = {}
    for row in rows[1:]:
        data = row.get("Data", [])
        if len(data) < 3:
            continue
        try:
            source = data[0]["VarCharValue"]
            parsed[source] = {
                "min_date": data[1]["VarCharValue"],
                "max_date": data[2]["VarCharValue"],
            }
        except KeyError as e:
            logger.warning(
                "Skipping malformed temporal row",
                extra={"error": str(e), "row_data": str(data)},
            )
    return parsed


def parse_volume_results(result_set: dict) -> dict[str, int]:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return {}

    parsed: dict[str, int] = {}
    for row in rows[1:]:
        data = row.get("Data", [])
        if len(data) < 2:
            continue
        try:
            source = data[0]["VarCharValue"]
            parsed[source] = int(data[1]["VarCharValue"])
        except (KeyError, ValueError) as e:
            logger.warning(
                "Skipping malformed volume row",
                extra={"error": str(e), "row_data": str(data)},
            )
    return parsed


def check_rate_consistency(
    athena_client: Any,
    database: str,
    output_location: str,
    workgroup: str,
) -> CrossValidationCheck:
    query = build_rate_consistency_query("fx_rates")
    result_set = _execute_and_wait(
        athena_client, query, database, output_location, workgroup
    )
    discrepancies = parse_rate_consistency_results(result_set)
    count = len(discrepancies)

    if count == 0:
        return CrossValidationCheck(
            check_name="rate_consistency",
            passed=True,
            detail="Frankfurter and ECB rates are consistent (deviation <= 1%)",
            metric_value=0.0,
        )

    max_dev = max(d["deviation"] for d in discrepancies)
    sample = discrepancies[:5]
    sample_detail = "; ".join(
        f"{d['date']} {d['target_currency']}: "
        f"{d['frankfurter_rate']:.6f} vs {d['ecb_rate']:.6f} "
        f"({d['deviation']:.4%})"
        for d in sample
    )
    return CrossValidationCheck(
        check_name="rate_consistency",
        passed=False,
        detail=(
            f"{count} currency-pair-dates exceed 1% deviation "
            f"(max {max_dev:.4%}). Sample: {sample_detail}"
        ),
        metric_value=float(count),
    )


def check_temporal_consistency(
    athena_client: Any,
    database: str,
    output_location: str,
    workgroup: str,
) -> CrossValidationCheck:
    query = build_temporal_consistency_query("fx_rates")
    result_set = _execute_and_wait(
        athena_client, query, database, output_location, workgroup
    )
    sources = parse_temporal_results(result_set)

    if len(sources) < 2:
        return CrossValidationCheck(
            check_name="temporal_consistency",
            passed=True,
            detail=f"Only {len(sources)} source(s) present — skipping temporal check",
            metric_value=0.0,
        )

    max_dates = sorted(sources[s]["max_date"] for s in sources)
    min_dates = sorted(sources[s]["min_date"] for s in sources)

    try:
        latest_max = datetime.strptime(max_dates[-1], "%Y-%m-%d")
        earliest_max = datetime.strptime(max_dates[0], "%Y-%m-%d")
        max_gap_days = abs((latest_max - earliest_max).days)

        latest_min = datetime.strptime(min_dates[-1], "%Y-%m-%d")
        earliest_min = datetime.strptime(min_dates[0], "%Y-%m-%d")
        min_gap_days = abs((latest_min - earliest_min).days)
    except ValueError as e:
        raise ValueError(
            f"Cannot parse source dates: {max_dates + min_dates}. Error: {e}"
        ) from e

    gap = max(max_gap_days, min_gap_days)
    passed = gap <= TEMPORAL_GAP_THRESHOLD_DAYS

    ranges_detail = ", ".join(
        f"{src}: {info['min_date']} to {info['max_date']}"
        for src, info in sorted(sources.items())
    )

    return CrossValidationCheck(
        check_name="temporal_consistency",
        passed=passed,
        detail=(
            f"Max date gap: {gap} day(s) (threshold: {TEMPORAL_GAP_THRESHOLD_DAYS}). "
            f"Ranges: {ranges_detail}"
        ),
        metric_value=float(gap),
    )


def check_volume_consistency(
    athena_client: Any,
    database: str,
    output_location: str,
    workgroup: str,
) -> CrossValidationCheck:
    query = build_volume_consistency_query("fx_rates")
    result_set = _execute_and_wait(
        athena_client, query, database, output_location, workgroup
    )
    volumes = parse_volume_results(result_set)

    if len(volumes) < 2:
        return CrossValidationCheck(
            check_name="volume_consistency",
            passed=True,
            detail=f"Only {len(volumes)} source(s) present — skipping volume check",
            metric_value=0.0,
        )

    counts = list(volumes.values())
    avg = sum(counts) / len(counts)
    max_ratio = max(abs(c - avg) / avg for c in counts) if avg > 0 else 0.0
    passed = max_ratio <= 0.5

    volumes_detail = ", ".join(
        f"{src}: {count}" for src, count in sorted(volumes.items())
    )

    return CrossValidationCheck(
        check_name="volume_consistency",
        passed=passed,
        detail=(
            f"Max deviation from mean: {max_ratio:.1%}. "
            f"Counts: {volumes_detail}"
        ),
        metric_value=max_ratio,
    )


def _publish_cross_validation_metrics(
    cloudwatch_client: Any,
    checks: list[CrossValidationCheck],
) -> None:
    metric_data = []
    for check in checks:
        metric_data.append({
            "MetricName": f"CrossSource_{check.check_name}",
            "Value": check.metric_value,
            "Unit": "Count",
            "Dimensions": [
                {"Name": "Environment", "Value": "production"},
            ],
        })

    discrepancy_count = sum(1 for c in checks if not c.passed)
    metric_data.append({
        "MetricName": "CrossSourceDiscrepancy",
        "Value": float(discrepancy_count),
        "Unit": "Count",
        "Dimensions": [
            {"Name": "Environment", "Value": "production"},
        ],
    })

    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
    except ClientError as e:
        logger.error(
            "Failed to publish cross-validation metrics",
            extra={"error_code": e.response["Error"]["Code"]},
        )
        raise


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    database = event.get("database_name", DATABASE_NAME)
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"

    athena_client = boto3.client("athena")
    cloudwatch_client = boto3.client("cloudwatch")

    with Timer() as timer:
        checks = [
            check_rate_consistency(
                athena_client, database, output_location, WORKGROUP
            ),
            check_temporal_consistency(
                athena_client, database, output_location, WORKGROUP
            ),
            check_volume_consistency(
                athena_client, database, output_location, WORKGROUP
            ),
        ]

    for check in checks:
        log_fn = logger.info if check.passed else logger.warning
        log_fn(
            f"Cross-validation {check.check_name}: "
            f"{'PASS' if check.passed else 'WARN'}",
            extra={
                "check_name": check.check_name,
                "passed": check.passed,
                "detail": check.detail,
                "metric_value": check.metric_value,
            },
        )

    _publish_cross_validation_metrics(cloudwatch_client, checks)

    all_passed = all(c.passed for c in checks)
    passed_count = sum(1 for c in checks if c.passed)

    logger.info(
        "Cross-source validation complete",
        extra={
            "passed": all_passed,
            "checks_total": len(checks),
            "checks_passed": passed_count,
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "status": "PASSED" if all_passed else "WARNING",
        "passed": all_passed,
        "checks_total": len(checks),
        "checks_passed": passed_count,
        "checks": [
            {
                "check_name": c.check_name,
                "passed": c.passed,
                "detail": c.detail,
                "metric_value": c.metric_value,
            }
            for c in checks
        ],
    }
