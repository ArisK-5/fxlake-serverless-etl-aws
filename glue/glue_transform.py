import io
import json
import logging
import sys
from typing import List

import boto3
import polars as pl
import pyarrow
import pyarrow.parquet as pq
from awsglue.utils import getResolvedOptions
from botocore.exceptions import ClientError

# -----------------------------
# Parameters
# -----------------------------
args = getResolvedOptions(
    sys.argv,
    ["RAW_BUCKET", "PROCESSED_BUCKET", "OUTPUT_FORMAT", "LOG_LEVEL"],
)

raw_bucket: str = args["RAW_BUCKET"]
processed_bucket: str = args["PROCESSED_BUCKET"]
output_format: str = args["OUTPUT_FORMAT"].lower()
log_level: str = args["LOG_LEVEL"].upper()

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
        keys: List[str] = []
        for page in paginator.paginate(Bucket=bucket):
            keys.extend(
                [
                    obj["Key"]
                    for obj in page.get("Contents", [])
                    if obj["Key"].endswith(".json")
                ]
            )
        return keys
    except ClientError as e:
        logger.error(
            f"S3 ClientError listing keys in bucket={bucket}: "
            f"{e.response['Error']['Code']}",
            exc_info=True,
        )
        raise
    except Exception:
        logger.error(
            f"Unexpected error listing keys in bucket={bucket}", exc_info=True
        )
        raise


def _write_partition(df: "pl.DataFrame", out_key: str) -> None:
    """Write a single-date DataFrame to S3 in the configured output format."""
    # Phase 1: serialization (Polars/PyArrow errors logged separately from S3 errors)
    try:
        if output_format == "parquet":
            buffer = io.BytesIO()
            pq.write_table(df.to_arrow(), buffer)
            buffer.seek(0)
            body: bytes = buffer.getvalue()
            content_type = "application/x-parquet"
        else:
            body = df.write_csv().encode()
            content_type = "text/csv"
    except (pl.exceptions.PolarsError, pyarrow.lib.ArrowException, ValueError, OSError) as e:
        logger.error(
            f"Serialization error for partition s3://{processed_bucket}/{out_key} "
            f"(format={output_format}): {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise

    # Phase 2: S3 write
    try:
        s3.put_object(
            Bucket=processed_bucket,
            Key=out_key,
            Body=body,
            ContentType=content_type,
        )
    except ClientError as e:
        logger.error(
            f"S3 error writing partition s3://{processed_bucket}/{out_key}: "
            f"{e.response['Error']['Code']}",
            exc_info=True,
        )
        raise


def process_key(key: str) -> List[str]:
    """Transform one raw JSON file into date-partitioned output files.

    Returns a list of S3 keys written (one per date in the source data).
    Returns an empty list if the source contains no rate rows.
    """
    try:
        obj = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.load(obj["Body"])

        base = payload["base"]
        rates = payload.get("rates", {})

        rows = [
            {"base_currency": base, "target_currency": tgt, "rate": rate, "date": dt}
            for dt, daily_rates in rates.items()
            for tgt, rate in daily_rates.items()
        ]

        if not rows:
            logger.warning(f"No rates found in {key}, skipping output")
            return []

        df = pl.DataFrame(rows)
        stem = key.split("/")[-1].replace(".json", "")
        out_keys: List[str] = []

        for dt in sorted(df["date"].unique().to_list()):
            year, month, day = dt.split("-")
            partition = f"year={year}/month={month}/day={day}"
            out_key = f"exchange_rates/{partition}/{stem}.{output_format}"

            _write_partition(df.filter(pl.col("date") == dt), out_key)
            out_keys.append(out_key)
            logger.info(f"Processed {key} → {out_key}")

        return out_keys

    except ClientError as e:
        logger.error(
            f"S3 error processing key={key}: {e.response['Error']['Code']}",
            exc_info=True,
        )
        raise
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"Malformed payload in key={key}: {e}", exc_info=True)
        raise
    except Exception:
        logger.error(f"Unexpected error processing key={key}", exc_info=True)
        raise


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    keys = list_json_keys(raw_bucket)
    logger.info(f"Found {len(keys)} JSON files")

    for i, key in enumerate(keys):
        try:
            process_key(key)
        except Exception:
            logger.error(
                f"ETL failed on key={key} ({i}/{len(keys)} files processed before failure)",
                exc_info=True,
            )
            raise

    logger.info("ETL completed successfully")


if __name__ == "__main__":
    main()
