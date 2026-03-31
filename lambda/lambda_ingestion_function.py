import json
import logging
import os
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError

RAW_BUCKET = os.environ["RAW_BUCKET"]
START_DATE = os.environ["START_DATE"]
END_DATE = os.environ["END_DATE"]
BASE_CURRENCY = os.environ["BASE_CURRENCY"]
BASE_API_URL = os.environ["BASE_API_URL"]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3 = boto3.client("s3")


def fetch_exchange_rates() -> dict:
    """Fetch exchange rates for the configured date range"""
    api_url = f"{BASE_API_URL}/{START_DATE}..{END_DATE}"
    params = {"base": BASE_CURRENCY}

    try:
        resp = requests.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        logger.debug("Successfully fetched exchange rates from API")
        return resp.json()
    except json.JSONDecodeError as e:
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


def save_to_s3(data: dict) -> str:
    """Save data to S3 with proper naming"""
    filename = f"exchange_rates_{BASE_CURRENCY}_{START_DATE}_to_{END_DATE}.json"
    body = json.dumps(data)

    try:
        S3.put_object(
            Bucket=RAW_BUCKET,
            Key=filename,
            Body=body,
            ContentType="application/json",
            Metadata={
                "start_date": START_DATE,
                "end_date": END_DATE,
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


def lambda_handler(event: dict, context: Any) -> dict:
    try:
        data = fetch_exchange_rates()
        filename = save_to_s3(data)
        logger.info(f"Lambda ingestion succeeded, saved file: {filename}")

        return {
            "status": "ok",
            "key": filename,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "base": BASE_CURRENCY,
        }
    except Exception:
        logger.error(
            "Unhandled exception in lambda_handler",
            exc_info=True,
            extra={
                "start_date": START_DATE,
                "end_date": END_DATE,
                "base": BASE_CURRENCY,
            },
        )
        raise
