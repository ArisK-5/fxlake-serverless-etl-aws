# Glue Python Shell script — flatten exchange rates JSON to CSV/Parquet
import json
import os
import sys
from typing import List

import boto3
import pandas as pd
from awsglue.utils import getResolvedOptions

# Get job parameters
args = getResolvedOptions(sys.argv, ["RAW_BUCKET", "PROCESSED_BUCKET", "OUTPUT_FORMAT"])

s3 = boto3.client("s3")
raw_bucket = args["RAW_BUCKET"]
processed_bucket = args["PROCESSED_BUCKET"]
output_format = args["OUTPUT_FORMAT"].lower()

if output_format not in ["csv", "parquet"]:
    raise ValueError("OUTPUT_FORMAT must be either 'csv' or 'parquet'")

print(f"Starting ETL job with format: {output_format}")
print(f"Raw bucket: {raw_bucket}")
print(f"Processed bucket: {processed_bucket}")


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
    except Exception as e:
        print(f"Error listing objects in bucket {bucket}: {str(e)}")
        raise


def process_key(key: str) -> str:
    """Process a single JSON file and convert to CSV or Parquet"""
    try:
        # Read and parse JSON
        resp = s3.get_object(Bucket=raw_bucket, Key=key)
        payload = json.loads(resp["Body"].read())

        # Extract data from Frankfurter format
        base = payload.get("base")
        amount = payload.get("amount", 1.0)
        rates_by_date = payload.get("rates", {})

        # Transform to rows - handle multiple dates
        rows = []
        for date, rates in rates_by_date.items():
            for currency, rate in rates.items():
                rows.append(
                    {
                        "base_currency": base,
                        "target_currency": currency,
                        "rate": rate * amount,  # Adjust for amount if not 1.0
                        "date": date,
                    }
                )

        # Create DataFrame
        df = pd.DataFrame(rows)

        # Determine output path - store in a subfolder that matches Athena table configuration
        base_path = "exchange_rates"

        # Prefer S3 object metadata for start/end/base so we can compute a
        # deterministic output filename. This ensures re-runs for the same
        # date-range overwrite the previous processed output instead of
        # producing duplicates.
        metadata = resp.get("Metadata", {}) or {}
        start_date = metadata.get("start_date")
        end_date = metadata.get("end_date")
        base_meta = metadata.get("base_currency")

        if start_date and end_date and base_meta:
            filename = f"exchange_rates_{base_meta}_{start_date}_to_{end_date}.{output_format}"
        else:
            # Fall back to the raw key name if metadata isn't available
            filename = os.path.basename(key).replace(".json", f".{output_format}")

        out_key = f"{base_path}/{filename}"

        # Save in desired format
        if output_format == "parquet":
            temp_file = "/tmp/temp.parquet"
            df.to_parquet(temp_file, index=False)
            with open(temp_file, "rb") as f:
                s3.put_object(
                    Bucket=processed_bucket,
                    Key=out_key,
                    Body=f.read(),
                    ContentType="application/x-parquet",
                )
            os.remove(temp_file)  # Clean up
        else:
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            s3.put_object(
                Bucket=processed_bucket,
                Key=out_key,
                Body=csv_bytes,
                ContentType="text/csv",
            )

        print(f"Successfully processed {key} to {out_key}")
        return out_key
    except Exception as e:
        print(f"Error processing {key}: {str(e)}")
        raise


def main():
    """Main ETL process"""
    try:
        keys = list_json_keys(raw_bucket)
        print(f"Found {len(keys)} files to process")

        for k in keys:
            process_key(k)

        print("ETL job completed successfully")
    except Exception as e:
        print(f"ETL job failed: {str(e)}")
        raise


if __name__ == "__main__":
    main()
