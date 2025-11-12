import json
import logging
import os

import boto3
import requests

RAW_BUCKET = os.environ["RAW_BUCKET"]
START_DATE = os.environ["START_DATE"]
END_DATE = os.environ["END_DATE"]
BASE_CURRENCY = os.environ["BASE_CURRENCY"]
BASE_API_URL = os.environ["BASE_API_URL"]

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3 = boto3.client("s3")


def fetch_exchange_rates():
    """Fetch exchange rates for the configured date range"""
    api_url = f"{BASE_API_URL}/{START_DATE}..{END_DATE}"
    params = {"base": BASE_CURRENCY}

    try:
        resp = requests.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        logger.debug("Successfully fetched exchange rates from API")
        return resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching data from Frankfurter API: {str(e)}")
        raise


def save_to_s3(data):
    """Save data to S3 with proper naming"""
    filename = f"exchange_rates_{BASE_CURRENCY}_{START_DATE}_to_{END_DATE}.json"

    try:
        S3.put_object(
            Bucket=RAW_BUCKET,
            Key=filename,
            Body=json.dumps(data),
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
    except Exception as e:
        logger.error(f"Error saving to S3: {str(e)}")
        raise


def lambda_handler(event, context):
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

    except Exception as e:
        logger.error(f"Error in lambda execution: {str(e)}")
        raise
