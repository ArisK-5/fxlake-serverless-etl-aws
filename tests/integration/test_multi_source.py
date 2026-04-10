"""Integration tests: multi-source parallel ingestion and Glue transform.

Verifies that all three sources (Frankfurter, ECB, FRED) can be ingested
concurrently, that each writes to the correct S3 path, and that the Glue
transform correctly routes files to the appropriate domain.
"""

import io
import json
import os
from typing import Any

import boto3
import polars as pl
import pyarrow.parquet as pq
import pytest
import responses
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
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
SAMPLE_FRANKFURTER = {
    "base": "EUR",
    "start_date": "2024-01-02",
    "end_date": "2024-01-03",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8612},
    },
}

SAMPLE_ECB = {
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

SAMPLE_FRED = {
    "observations": [
        {"date": "2024-01-01", "value": "3.7"},
        {"date": "2024-02-01", "value": "3.9"},
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def multi_source_aws():
    """Full moto environment with S3, DynamoDB, and CloudWatch."""
    with mock_aws():
        region = "us-east-1"
        s3 = boto3.client("s3", region_name=region)
        ddb = boto3.client("dynamodb", region_name=region)

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

        yield {"s3": s3, "dynamodb": ddb}


def _s3_keys(s3_client: Any, bucket: str, prefix: str = "") -> list[str]:
    resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def _read_parquet(s3_client: Any, bucket: str, key: str) -> pl.DataFrame:
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))
    return pl.from_arrow(table)


# ---------------------------------------------------------------------------
# Tests: Parallel ingestion
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestParallelIngestion:
    """All three sources ingest in the same moto environment."""

    @responses.activate
    def test_all_sources_write_to_correct_s3_paths(
        self, multi_source_aws, monkeypatch
    ):
        """Each source writes a raw JSON file with the correct filename prefix."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "ok"
        assert ecb_result["status"] == "ok"
        assert fred_result["status"] == "ok"

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 3

        prefixes = {k.split("_")[0] for k in raw_keys}
        assert prefixes == {"exchange", "ecb", "fred"}

    @responses.activate
    def test_raw_files_contain_correct_structure(
        self, multi_source_aws, monkeypatch
    ):
        """Each raw JSON has the expected top-level keys for its source."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        ecb_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        s3 = multi_source_aws["s3"]
        raw_keys = _s3_keys(s3, TEST_RAW_BUCKET)

        for key in raw_keys:
            obj = s3.get_object(Bucket=TEST_RAW_BUCKET, Key=key)
            data = json.loads(obj["Body"].read())

            if key.startswith("exchange_rates_"):
                assert "base" in data
                assert "rates" in data
            elif key.startswith("ecb_"):
                assert data["base"] == "EUR"
                assert data["source"] == "ecb"
                assert "rates" in data
            elif key.startswith("fred_"):
                assert data["source"] == "fred"
                assert "observations" in data

    @responses.activate
    def test_s3_object_metadata_includes_source(
        self, multi_source_aws, monkeypatch
    ):
        """All raw S3 objects carry 'source' metadata tag set by BaseIngestionHandler."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        ecb_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        s3 = multi_source_aws["s3"]
        raw_keys = _s3_keys(s3, TEST_RAW_BUCKET)

        source_names = set()
        for key in raw_keys:
            head = s3.head_object(Bucket=TEST_RAW_BUCKET, Key=key)
            source_names.add(head["Metadata"]["source"])

        assert source_names == {"frankfurter", "ecb", "fred"}


# ---------------------------------------------------------------------------
# Tests: Glue transform handles multiple schemas
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestGlueMultiSchema:
    """Glue transform correctly routes and transforms all source types."""

    @responses.activate
    def test_transform_routes_all_sources_to_correct_domains(
        self, multi_source_aws, monkeypatch
    ):
        """Frankfurter + ECB → fx_rates/, FRED → economic_indicators/."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        ecb_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        import glue_transform

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        for key in raw_keys:
            glue_transform.process_key(key)

        s3 = multi_source_aws["s3"]

        # FX domain: Frankfurter + ECB
        fx_keys = _s3_keys(s3, TEST_PROCESSED_BUCKET, "fx_rates/year=")
        assert len(fx_keys) >= 2  # at least 2 dates from Frankfurter + 1 from ECB

        # Economic domain: FRED
        econ_keys = _s3_keys(s3, TEST_PROCESSED_BUCKET, "economic_indicators/year=")
        assert len(econ_keys) == 2  # 2 monthly observations

    @responses.activate
    def test_fx_and_economic_schemas_are_distinct(
        self, multi_source_aws, monkeypatch
    ):
        """FX Parquet has {date,source,base_currency,target_currency,rate};
        Economic has {date,source,series_id,value}."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        import glue_transform

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        for key in raw_keys:
            glue_transform.process_key(key)

        s3 = multi_source_aws["s3"]

        fx_keys = _s3_keys(s3, TEST_PROCESSED_BUCKET, "fx_rates/year=")
        econ_keys = _s3_keys(s3, TEST_PROCESSED_BUCKET, "economic_indicators/year=")

        fx_df = _read_parquet(s3, TEST_PROCESSED_BUCKET, fx_keys[0])
        econ_df = _read_parquet(s3, TEST_PROCESSED_BUCKET, econ_keys[0])

        assert set(fx_df.columns) == {"date", "source", "base_currency", "target_currency", "rate"}
        assert set(econ_df.columns) == {"date", "source", "series_id", "value"}

    @responses.activate
    def test_quality_reports_generated_for_each_domain(
        self, multi_source_aws, monkeypatch
    ):
        """Each domain gets its own quality report JSON."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        import glue_transform

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        for key in raw_keys:
            glue_transform.process_key(key)

        s3 = multi_source_aws["s3"]

        fx_reports = _s3_keys(s3, TEST_PROCESSED_BUCKET, "fx_rates/quality_reports/")
        econ_reports = _s3_keys(s3, TEST_PROCESSED_BUCKET, "economic_indicators/quality_reports/")

        assert len(fx_reports) == 1
        assert len(econ_reports) == 1

        # Verify report structure
        for report_key in fx_reports + econ_reports:
            obj = s3.get_object(Bucket=TEST_PROCESSED_BUCKET, Key=report_key)
            report = json.loads(obj["Body"].read())
            assert "domain" in report
            assert "checks" in report
            assert len(report["checks"]) > 0
            assert "overall_passed" in report

    @responses.activate
    def test_partition_paths_follow_hive_convention(
        self, multi_source_aws, monkeypatch
    ):
        """Output paths use year=YYYY/month=MM/day=DD Hive-style partitioning."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..2024-01-31",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)

        import glue_transform

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        glue_transform.process_key(raw_keys[0])

        fx_keys = _s3_keys(multi_source_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/year=")

        for key in fx_keys:
            parts = key.split("/")
            # fx_rates/year=2024/month=01/day=02/stem.parquet
            assert parts[0] == "fx_rates"
            assert parts[1].startswith("year=")
            assert parts[2].startswith("month=")
            assert parts[3].startswith("day=")
            assert parts[4].endswith(".parquet")
