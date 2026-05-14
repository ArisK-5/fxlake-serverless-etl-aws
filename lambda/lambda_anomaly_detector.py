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

logger = configure_logger("anomaly-detector")

DATABASE_NAME = os.environ.get("DATABASE_NAME", "fxlake")
ATHENA_RESULTS_BUCKET = os.environ["ATHENA_RESULTS_BUCKET"]
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "fxlake")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "FXLake/AnomalyDetection")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN")

POLL_INTERVAL_SECONDS = 2
MAX_POLL_ATTEMPTS = 90
ALERT_THRESHOLD = 3.0
WARNING_THRESHOLD = 2.0
MIN_SAMPLE_SIZE = 5
LOOKBACK_DAYS = 30
_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class AnomalyCheck:
    domain: str
    entity: str
    z_score: float
    severity: str
    latest_value: float
    mean_value: float
    stddev_value: float
    sample_count: int

    def __post_init__(self) -> None:
        if self.severity not in ("ALERT", "WARNING", "NORMAL"):
            raise ValueError(f"Invalid severity: {self.severity!r}")
        if self.z_score < 0:
            raise ValueError("z_score cannot be negative")
        if self.severity == "ALERT" and self.z_score < ALERT_THRESHOLD:
            raise ValueError(
                f"ALERT severity requires z_score >= {ALERT_THRESHOLD}, "
                f"got {self.z_score}"
            )
        if self.severity == "NORMAL" and self.z_score >= WARNING_THRESHOLD:
            raise ValueError(
                f"NORMAL severity requires z_score < {WARNING_THRESHOLD}, "
                f"got {self.z_score}"
            )


def compute_z_score(value: float, mean: float, stddev: float) -> float:
    if stddev == 0.0:
        return 0.0
    return abs(value - mean) / stddev


def classify_severity(z_score: float) -> str:
    if z_score >= ALERT_THRESHOLD:
        return "ALERT"
    if z_score >= WARNING_THRESHOLD:
        return "WARNING"
    return "NORMAL"


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


def build_fx_stats_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"WITH stats AS (\n"
        f"  SELECT target_currency,\n"
        f"         AVG(rate) AS mean_rate,\n"
        f"         STDDEV(rate) AS stddev_rate,\n"
        f"         COUNT(*) AS sample_count\n"
        f"  FROM {table_name}\n"
        f"  WHERE date >= DATE_ADD('day', -{LOOKBACK_DAYS}, CURRENT_DATE)\n"
        f"  GROUP BY target_currency\n"
        f"  HAVING COUNT(*) >= {MIN_SAMPLE_SIZE}\n"
        f"),\n"
        f"latest AS (\n"
        f"  SELECT target_currency, rate AS latest_rate\n"
        f"  FROM {table_name}\n"
        f"  WHERE date = (SELECT MAX(date) FROM {table_name})\n"
        f")\n"
        f"SELECT l.target_currency, l.latest_rate,\n"
        f"       s.mean_rate, s.stddev_rate, s.sample_count,\n"
        f"       CASE WHEN s.stddev_rate > 0\n"
        f"            THEN ABS(l.latest_rate - s.mean_rate) / s.stddev_rate\n"
        f"            ELSE 0 END AS z_score\n"
        f"FROM latest l\n"
        f"JOIN stats s ON l.target_currency = s.target_currency"
    )


def build_economic_stats_query(table_name: str) -> str:
    if not _VALID_TABLE_NAME.match(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    return (
        f"WITH stats AS (\n"
        f"  SELECT series_id,\n"
        f"         AVG(value) AS mean_value,\n"
        f"         STDDEV(value) AS stddev_value,\n"
        f"         COUNT(*) AS sample_count\n"
        f"  FROM {table_name}\n"
        f"  WHERE date >= DATE_ADD('day', -{LOOKBACK_DAYS}, CURRENT_DATE)\n"
        f"  GROUP BY series_id\n"
        f"  HAVING COUNT(*) >= {MIN_SAMPLE_SIZE}\n"
        f"),\n"
        f"latest AS (\n"
        f"  SELECT series_id, value AS latest_value\n"
        f"  FROM {table_name}\n"
        f"  WHERE date = (SELECT MAX(date) FROM {table_name})\n"
        f")\n"
        f"SELECT l.series_id, l.latest_value,\n"
        f"       s.mean_value, s.stddev_value, s.sample_count,\n"
        f"       CASE WHEN s.stddev_value > 0\n"
        f"            THEN ABS(l.latest_value - s.mean_value) / s.stddev_value\n"
        f"            ELSE 0 END AS z_score\n"
        f"FROM latest l\n"
        f"JOIN stats s ON l.series_id = s.series_id"
    )


def parse_stats_results(result_set: dict) -> list[dict[str, Any]]:
    rows = result_set.get("Rows", [])
    if len(rows) < 2:
        return []

    parsed: list[dict[str, Any]] = []
    for row in rows[1:]:
        data = row.get("Data", [])
        if len(data) < 6:
            continue
        try:
            sample_count = int(data[4]["VarCharValue"])
            if sample_count < MIN_SAMPLE_SIZE:
                logger.warning(
                    "Skipping entity with insufficient samples",
                    extra={
                        "entity": data[0].get("VarCharValue", ""),
                        "sample_count": sample_count,
                    },
                )
                continue
            parsed.append({
                "entity": data[0]["VarCharValue"],
                "latest_value": float(data[1]["VarCharValue"]),
                "mean_value": float(data[2]["VarCharValue"]),
                "stddev_value": float(data[3]["VarCharValue"]),
                "sample_count": sample_count,
                "z_score": float(data[5]["VarCharValue"]),
            })
        except (KeyError, ValueError) as e:
            logger.warning(
                "Skipping malformed stats row",
                extra={"error": str(e), "row_data": str(data)},
            )
    return parsed


def _build_checks(
    domain: str,
    stats: list[dict[str, Any]],
) -> list[AnomalyCheck]:
    checks: list[AnomalyCheck] = []
    for row in stats:
        severity = classify_severity(row["z_score"])
        checks.append(
            AnomalyCheck(
                domain=domain,
                entity=row["entity"],
                z_score=row["z_score"],
                severity=severity,
                latest_value=row["latest_value"],
                mean_value=row["mean_value"],
                stddev_value=row["stddev_value"],
                sample_count=row["sample_count"],
            )
        )
    return checks


def _publish_anomaly_metrics(
    cloudwatch_client: Any,
    checks: list[AnomalyCheck],
) -> None:
    anomaly_count = sum(1 for c in checks if c.severity != "NORMAL")
    max_z = max((c.z_score for c in checks), default=0.0)

    metric_data = [
        {
            "MetricName": "AnomalyDetected",
            "Value": float(anomaly_count),
            "Unit": "Count",
            "Dimensions": [
                {"Name": "Environment", "Value": "production"},
            ],
        },
        {
            "MetricName": "ZScore",
            "Value": max_z,
            "Unit": "None",
            "Dimensions": [
                {"Name": "Environment", "Value": "production"},
            ],
        },
    ]

    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
    except ClientError as e:
        logger.error(
            "Failed to publish anomaly metrics",
            extra={"error_code": e.response["Error"]["Code"]},
        )


def _send_alert_notification(
    sns_client: Any,
    topic_arn: str | None,
    checks: list[AnomalyCheck],
) -> None:
    if not topic_arn:
        return

    alerts = [c for c in checks if c.severity == "ALERT"]
    if not alerts:
        return

    details = "\n".join(
        f"  - {c.domain}/{c.entity}: z-score={c.z_score:.2f} "
        f"(value={c.latest_value:.6f}, mean={c.mean_value:.6f}, "
        f"stddev={c.stddev_value:.6f})"
        for c in alerts
    )
    message = (
        f"FXLake Anomaly Detection Alert\n\n"
        f"{len(alerts)} anomaly alert(s) detected:\n{details}"
    )

    try:
        sns_client.publish(
            TopicArn=topic_arn,
            Subject="FXLake Anomaly Alert",
            Message=message,
        )
        logger.info(
            "Sent anomaly alert notification",
            extra={"alert_count": len(alerts)},
        )
    except ClientError as e:
        logger.error(
            "Failed to send anomaly alert notification",
            extra={"error_code": e.response["Error"]["Code"]},
        )


def lambda_handler(event: dict, context: Any) -> dict:
    inject_request_id(logger, context)

    database = event.get("database_name", DATABASE_NAME)
    output_location = f"s3://{ATHENA_RESULTS_BUCKET}/results/"

    athena_client = boto3.client("athena")
    cloudwatch_client = boto3.client("cloudwatch")
    sns_client = boto3.client("sns")

    all_checks: list[AnomalyCheck] = []

    with Timer() as timer:
        fx_query = build_fx_stats_query("fx_rates")
        fx_result = _execute_and_wait(
            athena_client, fx_query, database, output_location, WORKGROUP
        )
        fx_stats = parse_stats_results(fx_result)
        all_checks.extend(_build_checks("fx_rates", fx_stats))

        econ_query = build_economic_stats_query("economic_indicators")
        econ_result = _execute_and_wait(
            athena_client, econ_query, database, output_location, WORKGROUP
        )
        econ_stats = parse_stats_results(econ_result)
        all_checks.extend(_build_checks("economic_indicators", econ_stats))

    for check in all_checks:
        log_fn = (
            logger.warning if check.severity != "NORMAL" else logger.info
        )
        log_fn(
            f"Anomaly check {check.domain}/{check.entity}: {check.severity}",
            extra={
                "domain": check.domain,
                "entity": check.entity,
                "z_score": check.z_score,
                "severity": check.severity,
                "latest_value": check.latest_value,
                "mean_value": check.mean_value,
            },
        )

    _publish_anomaly_metrics(cloudwatch_client, all_checks)
    _send_alert_notification(sns_client, SNS_TOPIC_ARN, all_checks)

    alerts = [c for c in all_checks if c.severity == "ALERT"]
    warnings = [c for c in all_checks if c.severity == "WARNING"]
    anomalies = alerts + warnings

    if alerts:
        status = "ALERT"
    elif warnings:
        status = "WARNING"
    else:
        status = "PASSED"

    if not all_checks:
        logger.info(
            "No entities with sufficient history — cold start",
            extra={"lookback_days": LOOKBACK_DAYS, "min_samples": MIN_SAMPLE_SIZE},
        )

    logger.info(
        "Anomaly detection complete",
        extra={
            "status": status,
            "checks_total": len(all_checks),
            "anomalies_total": len(anomalies),
            "alerts_total": len(alerts),
            "warnings_total": len(warnings),
            "duration_ms": timer.duration_ms,
        },
    )

    return {
        "status": status,
        "checks_total": len(all_checks),
        "anomalies_total": len(anomalies),
        "alerts_total": len(alerts),
        "warnings_total": len(warnings),
        "checks": [
            {
                "domain": c.domain,
                "entity": c.entity,
                "z_score": c.z_score,
                "severity": c.severity,
                "latest_value": c.latest_value,
                "mean_value": c.mean_value,
            }
            for c in all_checks
        ],
    }
