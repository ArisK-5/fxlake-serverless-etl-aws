"""Integration tests: multi-source parallel ingestion and Iceberg write.

Verifies that all three sources (Frankfurter, ECB, FRED) can be ingested
concurrently, that each writes to the correct S3 path, and that the Iceberg
writer correctly routes files to the appropriate domain with quality reports.
"""

import json
import os
from datetime import date
from typing import Any
from unittest.mock import patch

import boto3
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


def _domain_for_key(key: str) -> str:
    if key.startswith("fred_"):
        return "economic_indicators"
    return "fx_rates"


def _call_iceberg_writer(
    raw_key: str,
    domain: str | None = None,
    captured_queries: list[str] | None = None,
) -> dict:
    """Call the Iceberg writer Lambda with Athena execution patched."""
    import lambda_iceberg_writer as writer_mod

    resolved_domain = domain or _domain_for_key(raw_key)

    def fake_execute(athena_client: Any, query: str, database: str,
                     output_location: str, workgroup: str) -> str:
        if captured_queries is not None:
            captured_queries.append(query)
        return "fake-query-id"

    def fake_poll(athena_client: Any, query_execution_id: str,
                  poll_interval: int = 2, max_attempts: int = 90) -> dict:
        return {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

    event = {
        "raw_bucket": TEST_RAW_BUCKET,
        "raw_key": raw_key,
        "domain": resolved_domain,
    }

    with patch.object(writer_mod, "_execute_athena_query", side_effect=fake_execute), \
         patch.object(writer_mod, "_poll_query_completion", side_effect=fake_poll):
        return writer_mod.lambda_handler(event, None)


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
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
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
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
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
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
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
# Tests: Iceberg writer handles multiple schemas
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestIcebergMultiSchema:
    """Iceberg writer correctly routes and writes all source types."""

    @responses.activate
    def test_iceberg_write_routes_all_sources_to_correct_domains(
        self, multi_source_aws, monkeypatch
    ):
        """Frankfurter + ECB → fx_rates INSERT, FRED → economic_indicators INSERT."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
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

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)

        fx_queries: list[str] = []
        econ_queries: list[str] = []
        for key in raw_keys:
            domain = _domain_for_key(key)
            target = fx_queries if domain == "fx_rates" else econ_queries
            _call_iceberg_writer(key, domain=domain, captured_queries=target)

        assert len(fx_queries) >= 2
        assert all("INSERT INTO fx_rates" in q for q in fx_queries)

        assert len(econ_queries) == 1
        assert "INSERT INTO economic_indicators" in econ_queries[0]

    @responses.activate
    def test_fx_and_economic_insert_columns_are_distinct(
        self, multi_source_aws, monkeypatch
    ):
        """FX INSERT uses (date,source,base_currency,target_currency,rate);
        Economic uses (date,source,series_id,value)."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)

        fx_queries: list[str] = []
        econ_queries: list[str] = []
        for key in raw_keys:
            domain = _domain_for_key(key)
            target = fx_queries if domain == "fx_rates" else econ_queries
            _call_iceberg_writer(key, domain=domain, captured_queries=target)

        assert len(fx_queries) >= 1
        assert "date, source, base_currency, target_currency, rate" in fx_queries[0]

        assert len(econ_queries) == 1
        assert "date, source, series_id, value" in econ_queries[0]

    @responses.activate
    def test_quality_reports_generated_for_each_domain(
        self, multi_source_aws, monkeypatch
    ):
        """Each domain gets its own quality report JSON."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED, status=200)

        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)
        fred_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        for key in raw_keys:
            _call_iceberg_writer(key)

        s3 = multi_source_aws["s3"]

        fx_reports = _s3_keys(s3, TEST_PROCESSED_BUCKET, "fx_rates/quality_reports/")
        econ_reports = _s3_keys(s3, TEST_PROCESSED_BUCKET, "economic_indicators/quality_reports/")

        assert len(fx_reports) == 1
        assert len(econ_reports) == 1

        for report_key in fx_reports + econ_reports:
            obj = s3.get_object(Bucket=TEST_PROCESSED_BUCKET, Key=report_key)
            report = json.loads(obj["Body"].read())
            assert "domain" in report
            assert "checks" in report
            assert len(report["checks"]) > 0
            assert "overall_passed" in report

    @responses.activate
    def test_iceberg_writer_returns_correct_metadata(
        self, multi_source_aws, monkeypatch
    ):
        """Each Iceberg writer call returns status, domain, and row count."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(multi_source_aws["s3"], TEST_RAW_BUCKET)
        result = _call_iceberg_writer(raw_keys[0])

        assert result["status"] == "ok"
        assert result["domain"] == "fx_rates"
        assert result["target_table"] == "fx_rates"
        assert result["rows_inserted"] > 0
        assert result["batches"] >= 1
