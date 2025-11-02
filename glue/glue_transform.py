# Glue Python Shell script — flatten exchange rates JSON to CSV/Parquet

import io
import json
import logging
import sys
import traceback
from typing import List

import boto3
import pandas as pd
from awsglue.utils import getResolvedOptions

# -----------------------------
# Logging configuration
# -----------------------------
logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# -----------------------------
# Job parameters
# -----------------------------
args = getResolvedOptions(sys.argv, ["RAW_BUCKET", "PROCESSED_BUCKET", "OUTPUT_FORMAT"])

s3 = boto3.client("s3")
raw_bucket = args["RAW_BUCKET"]
processed_bucket = args["PROCESSED_BUCKET"]
output_format = args["OUTPUT_FORMAT"].lower()

if output_format not in ["csv", "parquet"]:
    raise ValueError("OUTPUT_FORMAT must be either 'csv' or 'parquet'")

logger.info(f"Starting ETL job with format: {output_format}")
logger.info(f"Raw bucket: {raw_bucket}")
logger.info(f"Processed bucket: {processed_bucket}")


# -----------------------------
# Helper functions
# -----------------------------
def list_json_keys(bucket: str) -> List[str]:
    """List all JSON files in the bucket"""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket):
            if "Contents" in page:
                keys.extend(
                    o["Key"] for o in page["Contents"] if o["Key"].endswith(".json")
                )
        return keys
    except Exception:
        logger.error(f"Error listing objects in bucket {bucket}")
        logger.error(traceback.format_exc())
        raise


def process_key(key: str) -> str:
    """Process a single JSON file and convert to CSV or Parquet"""
    try:
        # Read and parse JSON
        resp = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.loads(resp["Body"].read())

        # Extract data from Frankfurter format
        base = payload.get("base")
        rates_by_date = payload.get("rates", {})

        # Transform to rows
        rows = []
        for date, rates in rates_by_date.items():
            for currency, rate in rates.items():
                rows.append(
                    {
                        "base_currency": base,
                        "target_currency": currency,
                        "rate": rate,
                        "date": date,
                    }
                )

        df = pd.DataFrame(rows)

        # Output path (Athena-compatible base folder)
        base_path = "exchange_rates"
        filename = key.split("/")[-1].replace(".json", f".{output_format}")
        out_key = f"{base_path}/{filename}"

        # Save in desired format using in-memory buffers
        if output_format == "parquet":
            buffer = io.BytesIO()
            df.to_parquet(buffer, index=False)
            buffer.seek(0)
            s3.put_object(
                Bucket=processed_bucket,
                Key=out_key,
                Body=buffer.getvalue(),
                ContentType="application/x-parquet",
            )
        else:
            csv_buffer = io.StringIO()
            df.to_csv(csv_buffer, index=False)
            s3.put_object(
                Bucket=processed_bucket,
                Key=out_key,
                Body=csv_buffer.getvalue(),
                ContentType="text/csv",
            )

        logger.info(f"✅ Successfully processed {key} → {out_key}")
        return out_key

    except Exception:
        logger.error(f"❌ Error processing {key}")
        logger.error(traceback.format_exc())
        raise


# -----------------------------
# Main execution
# -----------------------------
def main():
    """Main ETL process"""
    try:
        keys = list_json_keys(raw_bucket)
        logger.info(f"Found {len(keys)} JSON files to process")

        for k in keys:
            process_key(k)

        logger.info("🎉 ETL job completed successfully")

    except Exception:
        logger.error("ETL job failed")
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
