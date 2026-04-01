import json
import logging
import os
from datetime import date, timedelta
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError

RAW_BUCKET = os.environ["RAW_BUCKET"]
START_DATE = os.environ["START_DATE"]
END_DATE = os.environ["END_DATE"]
BASE_CURRENCY = os.environ["BASE_CURRENCY"]
BASE_API_URL = os.environ["BASE_API_URL"]
STATE_TABLE = os.getenv("STATE_TABLE")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3 = boto3.client("s3")
DYNAMODB = boto3.client("dynamodb") if STATE_TABLE else None

PIPELINE_ID = "fxlake"
SOURCE = "frankfurter"


def get_last_processed_date() -> str:
    """Read last_processed_date from DynamoDB; defaults to START_DATE if no entry."""
    try:
        resp = DYNAMODB.get_item(
            TableName=STATE_TABLE,
            Key={
                "pipeline_id": {"S": PIPELINE_ID},
                "source": {"S": SOURCE},
            },
        )
        item = resp.get("Item")
        if item:
            return item["last_processed_date"]["S"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "AccessDeniedException"):
            logger.error(
                f"DynamoDB table {STATE_TABLE} is inaccessible (code={code}). "
                "Check that the table exists and the Lambda execution role has GetItem permission.",
                exc_info=True,
            )
            raise
        # Transient errors (throttling, internal errors) — fall back with warning
        logger.warning(
            f"Transient DynamoDB read error for table {STATE_TABLE} "
            f"(code={code}), defaulting to START_DATE={START_DATE}"
        )
    return START_DATE


def update_last_processed_date(processed_date: str) -> None:
    """Write last_processed_date to DynamoDB state table."""
    try:
        DYNAMODB.put_item(
            TableName=STATE_TABLE,
            Item={
                "pipeline_id": {"S": PIPELINE_ID},
                "source": {"S": SOURCE},
                "last_processed_date": {"S": processed_date},
            },
        )
        logger.info(
            f"Updated DynamoDB state: table={STATE_TABLE}, last_processed_date={processed_date}"
        )
    except ClientError as e:
        logger.error(
            f"Failed to update state in DynamoDB table {STATE_TABLE} "
            f"(processed_date={processed_date}): {e.response['Error']['Code']}",
            exc_info=True,
        )
        raise


def fetch_exchange_rates(start_date: str, end_date: str) -> dict:
    """Fetch exchange rates for the given date range."""
    api_url = f"{BASE_API_URL}/{start_date}..{end_date}"
    params = {"base": BASE_CURRENCY}

    try:
        resp = requests.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        logger.debug("Successfully fetched exchange rates from API")
        return resp.json()
    except json.JSONDecodeError:
        logger.error(
            f"API returned non-JSON response from {api_url}: "
            f"status={resp.status_code}, body={resp.text[:200]}",
            exc_info=True,
        )
        raise
    except requests.exceptions.Timeout as e:
        logger.error(f"Timeout fetching {api_url}: {e}")
        raise
    except requests.exceptions.HTTPError as e:
        logger.error(
            f"HTTP error fetching {api_url}: status={e.response.status_code}",
            exc_info=True,
        )
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching {api_url}: {e}", exc_info=True)
        raise


def save_to_s3(data: dict, start_date: str, end_date: str) -> str:
    """Save data to S3 with proper naming."""
    filename = f"exchange_rates_{BASE_CURRENCY}_{start_date}_to_{end_date}.json"
    body = json.dumps(data)

    try:
        S3.put_object(
            Bucket=RAW_BUCKET,
            Key=filename,
            Body=body,
            ContentType="application/json",
            Metadata={
                "start_date": start_date,
                "end_date": end_date,
                "base_currency": BASE_CURRENCY,
                "source": "frankfurter",
            },
        )
        logger.debug(f"Saved exchange rates data to S3 as {filename}")
        return filename
    except ClientError as e:
        logger.error(
            f"Failed to write s3://{RAW_BUCKET}/{filename}: "
            f"{e.response['Error']['Code']}",
            exc_info=True,
        )
        raise


def lambda_handler(event: dict, _context: Any) -> dict:
    try:
        if event.get("action") == "update_state":
            return _handle_update_state(event)
        if STATE_TABLE:
            return _incremental_ingest()
        return _static_ingest()
    except Exception:
        logger.error(
            "Unhandled exception in lambda_handler",
            exc_info=True,
            extra={"base": BASE_CURRENCY},
        )
        raise


def _handle_update_state(event: dict) -> dict:
    """Commit last_processed_date to DynamoDB. Called by Step Functions after Glue succeeds."""
    if DYNAMODB is None or STATE_TABLE is None:
        raise RuntimeError(
            "update_state action requires STATE_TABLE env var — "
            "Lambda is not configured for incremental mode"
        )
    end_date = event.get("end_date")
    if not end_date:
        logger.error(
            f"update_state called without 'end_date'. Received keys: {list(event.keys())}"
        )
        raise ValueError("Missing required field 'end_date' in update_state event")
    update_last_processed_date(end_date)
    return {"status": "state_updated", "last_processed_date": end_date}


def _incremental_ingest() -> dict:
    """Fetch only dates newer than last_processed_date in DynamoDB."""
    last_processed = get_last_processed_date()
    fetch_start = (date.fromisoformat(last_processed) + timedelta(days=1)).isoformat()
    today = date.today().isoformat()
    fetch_end = min(today, END_DATE)  # ISO format: string comparison == date comparison

    if fetch_start > fetch_end:
        logger.info(
            f"Already caught up (last_processed_date={last_processed}), no new data to fetch"
        )
        return {"status": "no_new_data", "last_processed_date": last_processed}

    data = fetch_exchange_rates(fetch_start, fetch_end)
    filename = save_to_s3(data, fetch_start, fetch_end)

    logger.info(
        f"Incremental ingestion succeeded: {fetch_start}..{fetch_end}, file: {filename}"
    )
    return {
        "status": "ok",
        "key": filename,
        "start_date": fetch_start,
        "end_date": fetch_end,
        "base": BASE_CURRENCY,
    }


def _static_ingest() -> dict:
    """Fetch the full configured date range (START_DATE..END_DATE)."""
    data = fetch_exchange_rates(START_DATE, END_DATE)
    filename = save_to_s3(data, START_DATE, END_DATE)

    logger.info(f"Static ingestion succeeded, saved file: {filename}")
    return {
        "status": "ok",
        "key": filename,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "base": BASE_CURRENCY,
    }
