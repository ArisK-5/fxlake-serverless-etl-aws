import io
import json
import logging
import sys
from typing import List

import boto3
import polars as pl
import pyarrow.parquet as pq
from awsglue.utils import getResolvedOptions

# -----------------------------
# Parameters
# -----------------------------
args = getResolvedOptions(
    sys.argv,
    ["RAW_BUCKET", "PROCESSED_BUCKET", "OUTPUT_FORMAT", "LOG_LEVEL"],
)

raw_bucket = args["RAW_BUCKET"]
processed_bucket = args["PROCESSED_BUCKET"]
output_format = args["OUTPUT_FORMAT"].lower()
log_level = args["LOG_LEVEL"].upper()

if output_format not in ("csv", "parquet"):
    raise ValueError("OUTPUT_FORMAT must be either 'csv' or 'parquet'")

s3 = boto3.client("s3")

# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger()
logger.setLevel(log_level)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

logger.info(f"Starting ETL with format={output_format}")
logger.info(f"Raw bucket={raw_bucket}, Processed bucket={processed_bucket}")


# -----------------------------
# Helpers
# -----------------------------
def list_json_keys(bucket: str) -> List[str]:
    try:
        paginator = s3.get_paginator("list_objects_v2")
        keys = []
        for page in paginator.paginate(Bucket=bucket):
            keys.extend(
                [
                    obj["Key"]
                    for obj in page.get("Contents", [])
                    if obj["Key"].endswith(".json")
                ]
            )
        return keys
    except Exception:
        logger.error(f"Failed to list JSON keys in {bucket}", exc_info=True)
        raise


def process_key(key: str) -> str:
    try:
        obj = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.load(obj["Body"])

        base = payload.get("base")
        rates = payload.get("rates", {})

        # Flatten with list comprehension
        rows = [
            {"base_currency": base, "target_currency": tgt, "rate": rate, "date": dt}
            for dt, daily_rates in rates.items()
            for tgt, rate in daily_rates.items()
        ]

        df = pl.DataFrame(rows)

        base_path = "exchange_rates"
        filename = key.split("/")[-1].replace(".json", f".{output_format}")
        out_key = f"{base_path}/{filename}"

        if output_format == "parquet":
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

        logger.info(f"Processed {key} → {out_key}")
        return out_key

    except Exception:
        logger.error(f"Error processing {key}", exc_info=True)
        raise


# -----------------------------
# Main
# -----------------------------
def main():
    try:
        keys = list_json_keys(raw_bucket)
        logger.info(f"Found {len(keys)} JSON files")

        for key in keys:
            process_key(key)

        logger.info("ETL completed successfully")

    except Exception:
        logger.error("ETL failed", exc_info=True)
        raise


if __name__ == "__main__":
    main()
