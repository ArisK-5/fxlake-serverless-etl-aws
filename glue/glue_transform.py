import io
import json
import logging
import sys
import traceback
from typing import List

import boto3
import polars as pl
import pyarrow.parquet as pq
from awsglue.utils import getResolvedOptions

# -----------------------------
# Job parameters using Glue parser
# -----------------------------
args = getResolvedOptions(
    sys.argv,
    ["RAW_BUCKET", "PROCESSED_BUCKET", "OUTPUT_FORMAT", "LOG_LEVEL"],
)

raw_bucket = args["RAW_BUCKET"]
processed_bucket = args["PROCESSED_BUCKET"]
output_format = args["OUTPUT_FORMAT"].lower()
log_level = args["LOG_LEVEL"].upper()

if output_format not in ["csv", "parquet"]:
    raise ValueError("OUTPUT_FORMAT must be either 'csv' or 'parquet'")

# -----------------------------
# Setup boto3 client
# -----------------------------
s3 = boto3.client("s3")

# -----------------------------
# Logging configuration
# -----------------------------
logger = logging.getLogger()
logger.setLevel(log_level)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
handler.setFormatter(formatter)
logger.addHandler(handler)

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
        resp = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.loads(resp["Body"].read())

        base = payload.get("base")
        rates_by_date = payload.get("rates", {})

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

        df = pl.DataFrame(rows)

        base_path = "exchange_rates"
        filename = key.split("/")[-1].replace(".json", f".{output_format}")
        out_key = f"{base_path}/{filename}"

        if output_format == "parquet":
            # Convert Polars DataFrame to Arrow Table
            table = df.to_arrow()
            buffer = io.BytesIO()
            pq.write_table(table, buffer)
            buffer.seek(0)
            s3.put_object(
                Bucket=processed_bucket,
                Key=out_key,
                Body=buffer.getvalue(),
                ContentType="application/x-parquet",
            )
        else:
            csv_str = df.write_csv()
            s3.put_object(
                Bucket=processed_bucket,
                Key=out_key,
                Body=csv_str,
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
    try:
        keys = list_json_keys(raw_bucket)
        logger.info(f"Found {len(keys)} JSON files to process")

        for key in keys:
            process_key(key)

        logger.info("🎉 ETL job completed successfully")

    except Exception:
        logger.error("ETL job failed")
        logger.error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
