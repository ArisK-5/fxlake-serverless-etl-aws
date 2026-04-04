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


def _detect_fx_source(key: str, payload: dict) -> str:
    """Derive the data source name for FX rate files.

    Prefers the explicit ``source`` field in the payload (set by ECB handler).
    Falls back to filename-prefix detection for Frankfurter files, which store
    the raw API response without a source annotation.

    Assumption: any FX file without an explicit ``source`` field and without an
    ``ecb_`` prefix is assumed to originate from Frankfurter. If a third FX source
    is added without explicit source metadata, update this fallback to check for
    additional prefixes to avoid silent misattribution.
    """
    if "source" in payload:
        return payload["source"]
    stem = key.split("/")[-1]
    if stem.startswith("ecb_"):
        return "ecb"
    return "frankfurter"


def _process_fx_key(key: str) -> List[str]:
    """Transform one FX rates JSON file (Frankfurter or ECB) into date-partitioned output.

    Input format: ``{"base": <currency>, "rates": {date: {currency: rate}}}``.
    Output schema: ``{date, source, base_currency, target_currency, rate}``.
    Output path: ``fx_rates/year=YYYY/month=MM/day=DD/<stem>.<format>``.

    Returns a list of S3 keys written (one per date). Returns ``[]`` if no rows.
    """
    try:
        obj = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.load(obj["Body"])

        base = payload["base"]
        rates = payload.get("rates", {})
        source = _detect_fx_source(key, payload)

        rows = [
            {
                "date": dt,
                "source": source,
                "base_currency": base,
                "target_currency": tgt,
                "rate": rate,
            }
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
            out_key = f"fx_rates/{partition}/{stem}.{output_format}"

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
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in key={key}: {e}", exc_info=True)
        raise
    except KeyError as e:
        logger.error(f"Missing required field in key={key}: {e}", exc_info=True)
        raise
    except ValueError as e:
        logger.error(f"Invalid value in key={key}: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error processing key={key}: {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise


def _process_economic_key(key: str) -> List[str]:
    """Transform one FRED economic indicators JSON file into date-partitioned output.

    Input format: ``{"source": "fred", "series_id": <id>, "observations": {date: value}}``.
    Output schema: ``{date, source, series_id, value}``.
    Output path: ``economic_indicators/year=YYYY/month=MM/day=DD/<stem>.<format>``.

    Returns a list of S3 keys written (one per date). Returns ``[]`` if no rows.
    """
    try:
        obj = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.load(obj["Body"])

        source = payload["source"]
        series_id = payload["series_id"]
        observations = payload.get("observations", {})

        rows = [
            {"date": dt, "source": source, "series_id": series_id, "value": value}
            for dt, value in observations.items()
        ]

        if not rows:
            logger.warning(f"No observations found in {key}, skipping output")
            return []

        df = pl.DataFrame(rows)
        stem = key.split("/")[-1].replace(".json", "")
        out_keys: List[str] = []

        for dt in sorted(df["date"].unique().to_list()):
            year, month, day = dt.split("-")
            partition = f"year={year}/month={month}/day={day}"
            out_key = f"economic_indicators/{partition}/{stem}.{output_format}"

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
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in key={key}: {e}", exc_info=True)
        raise
    except KeyError as e:
        logger.error(f"Missing required field in key={key}: {e}", exc_info=True)
        raise
    except ValueError as e:
        logger.error(f"Invalid value in key={key}: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error processing key={key}: {type(e).__name__}: {e}",
            exc_info=True,
        )
        raise


def process_key(key: str) -> List[str]:
    """Dispatch one raw JSON file to the appropriate domain transform.

    Routing rule (based on filename stem, not directory prefix):
    - ``fred_*`` → economic indicators domain → ``economic_indicators/``
    - ``exchange_rates_*`` (Frankfurter) and ``ecb_*`` (ECB) → FX rates domain → ``fx_rates/``

    If a new FX source is added, its files will route to ``fx_rates/`` automatically
    unless they start with ``fred_``. Add an explicit prefix check here if needed.

    Returns a list of S3 output keys written.
    """
    stem = key.split("/")[-1]
    if stem.startswith("fred_"):
        return _process_economic_key(key)
    return _process_fx_key(key)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    keys = list_json_keys(raw_bucket)
    logger.info(f"Found {len(keys)} JSON files")

    for i, key in enumerate(keys):
        try:
            process_key(key)
        except Exception as e:
            logger.error(
                f"ETL failed on key={key} ({i}/{len(keys)} files processed before failure): "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    logger.info("ETL completed successfully")


if __name__ == "__main__":
    main()
