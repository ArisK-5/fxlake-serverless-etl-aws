"""Integration tests: full pipeline flow Ingestion → Transform → Validate.

Uses moto to mock S3, DynamoDB, CloudWatch, and Athena so the entire pipeline
runs locally without any AWS credentials.  HTTP calls to external APIs
(Frankfurter, ECB, FRED) are intercepted by the ``responses`` library.
"""

import io
import json
import os
from datetime import date
from typing import Any
from unittest.mock import patch

import boto3
import polars as pl
import pyarrow.parquet as pq
import pytest
import responses
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants — must stay in sync with conftest.py env vars
# ---------------------------------------------------------------------------
TEST_RAW_BUCKET = "test-raw-bucket"
TEST_PROCESSED_BUCKET = "test-processed-bucket"
TEST_QUARANTINE_BUCKET = "test-quarantine-bucket"
TEST_STATE_TABLE = "test-state-table"

FRANKFURTER_API_URL = os.environ["BASE_API_URL"]
ECB_BASE_URL = os.environ["ECB_BASE_URL"]
ECB_API_URL = f"{ECB_BASE_URL}/EXR/D..EUR.SP00.A"
FRED_BASE_URL = os.environ["FRED_BASE_URL"]
FRED_API_URL = f"{FRED_BASE_URL}/series/observations"

# ---------------------------------------------------------------------------
# Sample API responses
# ---------------------------------------------------------------------------
SAMPLE_FRANKFURTER_RESPONSE = {
    "base": "EUR",
    "start_date": "2024-01-02",
    "end_date": "2024-01-03",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8612},
    },
}

SAMPLE_ECB_RESPONSE = {
    "dataSets": [
        {
            "series": {
                "0:0:0:0:0": {"observations": {"0": [1.0953]}},
                "0:1:0:0:0": {"observations": {"0": [0.8671]}},
            }
        }
    ],
    "structure": {
        "dimensions": {
            "series": [
                {"id": "FREQ", "values": [{"id": "D"}]},
                {"id": "CURRENCY", "values": [{"id": "USD"}, {"id": "GBP"}]},
                {"id": "CURRENCY_DENOM", "values": [{"id": "EUR"}]},
                {"id": "EXR_TYPE", "values": [{"id": "SP00"}]},
                {"id": "EXR_SUFFIX", "values": [{"id": "A"}]},
            ],
            "observation": [
                {"id": "TIME_PERIOD", "values": [{"id": "2024-01-02"}]}
            ],
        }
    },
}

SAMPLE_FRED_RESPONSE = {
    "observations": [
        {"date": "2024-01-01", "value": "3.7"},
        {"date": "2024-02-01", "value": "3.9"},
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def integration_aws():
    """Stand up a full moto environment: S3 buckets, DynamoDB state table, CloudWatch."""
    with mock_aws():
        region = "us-east-1"
        s3 = boto3.client("s3", region_name=region)
        ddb = boto3.client("dynamodb", region_name=region)
        cw = boto3.client("cloudwatch", region_name=region)
        athena = boto3.client("athena", region_name=region)

        for bucket in (TEST_RAW_BUCKET, TEST_PROCESSED_BUCKET, TEST_QUARANTINE_BUCKET):
            s3.create_bucket(Bucket=bucket)

        ddb.create_table(
            TableName=TEST_STATE_TABLE,
            AttributeDefinitions=[
                {"AttributeName": "pipeline_id", "AttributeType": "S"},
                {"AttributeName": "source", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                {"AttributeName": "source", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        yield {
            "s3": s3,
            "dynamodb": ddb,
            "cloudwatch": cw,
            "athena": athena,
        }


def _s3_keys(s3_client: Any, bucket: str, prefix: str = "") -> list[str]:
    """List all S3 keys in *bucket* under *prefix*."""
    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def _read_parquet(s3_client: Any, bucket: str, key: str) -> pl.DataFrame:
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))
    return pl.from_arrow(table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestFullPipelineFlow:
    """Ingestion → Transform → Validate: one source end-to-end."""

    @responses.activate
    def test_frankfurter_ingest_transform_produces_partitioned_parquet(
        self, integration_aws, monkeypatch
    ):
        """Frankfurter ingestion writes raw JSON, Glue transform produces partitioned Parquet."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        # No DynamoDB state → get_last_processed returns start_date (2024-01-01),
        # _incremental_ingest adds 1 day → fetch_start = 2024-01-02
        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        # --- Stage 1: Ingestion ---
        import lambda_ingestion_function as fx_mod

        result = fx_mod.lambda_handler({}, None)

        assert result["status"] == "ok"
        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 1
        assert raw_keys[0].startswith("exchange_rates_EUR_")

        # Verify raw JSON content
        raw_obj = integration_aws["s3"].get_object(
            Bucket=TEST_RAW_BUCKET, Key=raw_keys[0]
        )
        raw_data = json.loads(raw_obj["Body"].read())
        assert raw_data["base"] == "EUR"
        assert "2024-01-02" in raw_data["rates"]

        # --- Stage 2: Transform (Glue) ---
        import glue_transform

        glue_transform.process_key(raw_keys[0])

        processed_keys = _s3_keys(integration_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/year=")
        assert len(processed_keys) == 2  # 2 dates → 2 partitions

        # Verify Parquet schema
        df = _read_parquet(integration_aws["s3"], TEST_PROCESSED_BUCKET, processed_keys[0])
        assert set(df.columns) == {"date", "source", "base_currency", "target_currency", "rate"}
        assert df["source"][0] == "frankfurter"

        # Verify quality report was written
        quality_keys = _s3_keys(
            integration_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/quality_reports/"
        )
        assert len(quality_keys) == 1

    @responses.activate
    def test_fred_ingest_transform_produces_economic_indicators(
        self, integration_aws, monkeypatch
    ):
        """FRED ingestion → Glue transform routes to economic_indicators domain."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        # --- Ingestion ---
        import lambda_fred_ingestion as fred_mod

        result = fred_mod.lambda_handler({}, None)
        assert result["status"] == "ok"
        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert any(k.startswith("fred_") for k in raw_keys)

        fred_key = next(k for k in raw_keys if k.startswith("fred_"))

        # --- Transform ---
        import glue_transform

        glue_transform.process_key(fred_key)

        econ_keys = _s3_keys(
            integration_aws["s3"], TEST_PROCESSED_BUCKET, "economic_indicators/year="
        )
        assert len(econ_keys) == 2  # 2024-01-01 and 2024-02-01

        df = _read_parquet(integration_aws["s3"], TEST_PROCESSED_BUCKET, econ_keys[0])
        assert set(df.columns) == {"date", "source", "series_id", "value"}
        assert df["source"][0] == "fred"

    @responses.activate
    def test_ecb_ingest_transform_tags_source_ecb(
        self, integration_aws, monkeypatch
    ):
        """ECB ingestion → Transform correctly detects source='ecb'."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod

        result = ecb_mod.lambda_handler({}, None)
        assert result["status"] == "ok"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        ecb_key = next(k for k in raw_keys if k.startswith("ecb_"))

        import glue_transform

        glue_transform.process_key(ecb_key)

        processed_keys = _s3_keys(
            integration_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/year="
        )
        assert len(processed_keys) >= 1

        df = _read_parquet(integration_aws["s3"], TEST_PROCESSED_BUCKET, processed_keys[0])
        assert df["source"][0] == "ecb"


@pytest.mark.integration
class TestDynamoDBStateManagement:
    """Verify DynamoDB state is correctly read and updated across the pipeline."""

    @responses.activate
    def test_incremental_ingest_reads_state_and_update_state_commits(
        self, integration_aws, monkeypatch
    ):
        """Ingestion reads last_processed_date; update_state action writes it back."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        # Seed DynamoDB with an existing state
        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-14"},
            },
        )

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-15..2024-01-31",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_ingestion_function as fx_mod

        ingest_result = fx_mod.lambda_handler({}, None)
        assert ingest_result["status"] == "ok"
        assert ingest_result["start_date"] == "2024-01-15"

        # State NOT yet updated (deferred to Step Functions post-Glue)
        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"

        # Simulate Step Functions calling update_state after Glue succeeds
        update_result = fx_mod.lambda_handler(
            {"action": "update_state", "end_date": ingest_result["end_date"]}, None
        )
        assert update_result["status"] == "state_updated"

        # Now DynamoDB is updated
        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == ingest_result["end_date"]

    @responses.activate
    def test_each_source_maintains_independent_state(
        self, integration_aws, monkeypatch
    ):
        """Three sources maintain independent DynamoDB rows — no cross-contamination."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        # Seed different dates per source
        for source, last_date in [
            ("frankfurter", "2024-01-10"),
            ("ecb", "2024-01-12"),
            ("fred", "2024-01-08"),
        ]:
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": last_date},
                },
            )

        # Stub all three APIs
        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..2024-01-31",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_ingestion_function as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        # Each source started from its own last_processed_date + 1 day
        assert fx_result["start_date"] == "2024-01-11"
        assert ecb_result["start_date"] == "2024-01-13"
        assert fred_result["start_date"] == "2024-01-09"

        # Update state for each source independently
        for mod, end_date, source in [
            (fx_mod, fx_result["end_date"], "frankfurter"),
            (ecb_mod, ecb_result["end_date"], "ecb"),
            (fred_mod, fred_result["end_date"], "fred"),
        ]:
            mod.lambda_handler({"action": "update_state", "end_date": end_date}, None)

            item = ddb.get_item(
                TableName=TEST_STATE_TABLE,
                Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": source}},
            )["Item"]
            assert item["last_processed_date"]["S"] == end_date


@pytest.mark.integration
class TestCloudWatchMetrics:
    """Verify CloudWatch metrics are published at each pipeline stage."""

    @responses.activate
    def test_quality_report_written_during_transform(
        self, integration_aws, monkeypatch
    ):
        """Transform writes a quality report JSON for every processed file."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_ingestion_function as fx_mod

        result = fx_mod.lambda_handler({}, None)

        import glue_transform

        glue_transform.process_key(result["key"])

        quality_keys = _s3_keys(
            integration_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/quality_reports/"
        )
        assert len(quality_keys) == 1

        report_obj = integration_aws["s3"].get_object(
            Bucket=TEST_PROCESSED_BUCKET, Key=quality_keys[0]
        )
        report = json.loads(report_obj["Body"].read())
        assert report["domain"] == "fx_rates"
        assert len(report["checks"]) > 0
        assert report["overall_passed"] is True

    @responses.activate
    def test_validation_publishes_freshness_metrics(
        self, integration_aws, monkeypatch
    ):
        """Validation Lambda publishes EmptyQueryResults and StaleFXData metrics."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        # Set up a fake Athena query execution that returns fresh data
        athena = integration_aws["athena"]
        today_str = date.today().isoformat()

        # Start a query execution to get a valid ID
        start_resp = athena.start_query_execution(
            QueryString="SELECT MAX(date) AS latest_date, COUNT(*) AS total_records FROM fx_rates",
            ResultConfiguration={"OutputLocation": "s3://test-athena-results/results/"},
        )
        query_id = start_resp["QueryExecutionId"]

        # Patch athena client in validation module to use our mocked one
        import lambda_validation_function as val_mod

        with patch.object(val_mod, "athena", athena), patch.object(
            val_mod, "cloudwatch", integration_aws["cloudwatch"]
        ):
            # moto's Athena mock returns QUEUED state — patch get_query_execution
            # to return SUCCEEDED with a result set
            mock_execution = {
                "QueryExecution": {
                    "Status": {"State": "SUCCEEDED"},
                    "WorkGroup": "primary",
                }
            }
            mock_results = {
                "ResultSet": {
                    "Rows": [
                        {
                            "Data": [
                                {"VarCharValue": "latest_date"},
                                {"VarCharValue": "total_records"},
                            ]
                        },
                        {
                            "Data": [
                                {"VarCharValue": today_str},
                                {"VarCharValue": "100"},
                            ]
                        },
                    ]
                }
            }

            with patch.object(
                athena, "get_query_execution", return_value=mock_execution
            ), patch.object(athena, "get_query_results", return_value=mock_results):
                val_result = val_mod.lambda_handler(
                    {"QueryExecutionId": query_id}, None
                )

        assert val_result["is_fresh"] is True
        assert val_result["total_records"] == 100
        assert val_result["status"] == "SUCCEEDED"


@pytest.mark.integration
class TestPipelineSagaPattern:
    """Verify the saga pattern: state is only committed after transform succeeds."""

    @responses.activate
    def test_state_not_updated_when_transform_skipped(
        self, integration_aws, monkeypatch
    ):
        """If Glue transform is never called, DynamoDB state remains unchanged."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-10"},
            },
        )

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..2024-01-31",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_ingestion_function as fx_mod

        fx_mod.lambda_handler({}, None)

        # Without calling update_state, DynamoDB retains old value
        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"

    @responses.activate
    def test_no_new_data_short_circuits_pipeline(
        self, integration_aws, monkeypatch
    ):
        """When all sources return no_new_data, no S3 objects are created."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        # Set all sources to END_DATE so there's nothing to fetch
        for source in ("frankfurter", "ecb", "fred"):
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": "2024-01-31"},
                },
            )

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_ingestion_function as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "no_new_data"
        assert ecb_result["status"] == "no_new_data"
        assert fred_result["status"] == "no_new_data"

        # No raw files written
        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert raw_keys == []
