import json
import os

import boto3
import requests

# Configuration from environment variables (set via terraform.tfvars)
RAW_BUCKET = os.environ["RAW_BUCKET"]
START_DATE = os.environ["START_DATE"]
END_DATE = os.environ["END_DATE"]
BASE_CURRENCY = os.environ["BASE_CURRENCY"]
BASE_API_URL = os.environ["BASE_API_URL"]

S3 = boto3.client("s3")


def fetch_exchange_rates():
    """Fetch exchange rates for the configured date range"""
    api_url = f"{BASE_API_URL}/{START_DATE}..{END_DATE}"
    params = {"base": BASE_CURRENCY}

    try:
        resp = requests.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from Frankfurter API: {str(e)}")
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
        return filename
    except Exception as e:
        print(f"Error saving to S3: {str(e)}")
        raise


def lambda_handler(event, context):
    """
    Lambda handler that fetches historical exchange rates from Frankfurter API.
    All configuration is done through terraform.tfvars.
    """
    try:
        # Fetch data from Frankfurter API
        data = fetch_exchange_rates()

        # Save to S3
        filename = save_to_s3(data)

        return {
            "status": "ok",
            "key": filename,
            "start_date": START_DATE,
            "end_date": END_DATE,
            "base": BASE_CURRENCY,
        }

    except Exception as e:
        print(f"Error in lambda execution: {str(e)}")
        return {"status": "error", "error": str(e)}
