"""Integration tests: full pipeline flow Ingestion → Iceberg Write → Validate.

Uses moto to mock S3, DynamoDB, CloudWatch, and Athena so the entire pipeline
runs locally without any AWS credentials.  HTTP calls to external APIs
(Frankfurter, ECB, FRED) are intercepted by the ``responses`` library.
Athena query execution is patched since moto cannot run real INSERT INTO queries.
"""

import json
import os
from datetime import date
from typing import Any
from unittest.mock import patch

import boto3
import pytest
import responses
from botocore.exceptions import ClientError
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


def _domain_for_key(key: str) -> str:
    """Determine the Iceberg writer domain from a raw S3 key prefix."""
    if key.startswith("fred_"):
        return "economic_indicators"
    return "fx_rates"


def _call_iceberg_writer(
    raw_key: str,
    domain: str | None = None,
    captured_queries: list[str] | None = None,
) -> dict:
    """Call the Iceberg writer Lambda with Athena execution patched.

    Quality checks, S3 reads/writes, and quarantine run against moto.
    Athena INSERT execution is stubbed since moto cannot run real queries.
    """
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
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestFullPipelineFlow:
    """Ingestion → Iceberg Write → Validate: one source end-to-end."""

    @responses.activate
    def test_frankfurter_ingest_and_iceberg_write(
        self, integration_aws, monkeypatch
    ):
        """Frankfurter ingestion writes raw JSON, Iceberg writer processes and inserts."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        result = fx_mod.lambda_handler({}, None)

        assert result["status"] == "ok"
        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 1
        assert raw_keys[0].startswith("exchange_rates_EUR_")

        raw_obj = integration_aws["s3"].get_object(
            Bucket=TEST_RAW_BUCKET, Key=raw_keys[0]
        )
        raw_data = json.loads(raw_obj["Body"].read())
        assert raw_data["base"] == "EUR"
        assert "2024-01-02" in raw_data["rates"]

        captured: list[str] = []
        write_result = _call_iceberg_writer(raw_keys[0], captured_queries=captured)

        assert write_result["status"] == "ok"
        assert write_result["domain"] == "fx_rates"
        assert write_result["rows_inserted"] == 4  # 2 dates × 2 currencies

        assert len(captured) >= 1
        assert "INSERT INTO fx_rates" in captured[0]
        for col in ("date", "source", "base_currency", "target_currency", "rate"):
            assert col in captured[0]

        quality_keys = _s3_keys(
            integration_aws["s3"], TEST_PROCESSED_BUCKET, "fx_rates/quality_reports/"
        )
        assert len(quality_keys) == 1

    @responses.activate
    def test_fred_ingest_and_iceberg_write(
        self, integration_aws, monkeypatch
    ):
        """FRED ingestion → Iceberg writer routes to economic_indicators domain."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_fred_ingestion as fred_mod

        result = fred_mod.lambda_handler({}, None)
        assert result["status"] == "ok"
        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert any(k.startswith("fred_") for k in raw_keys)

        fred_key = next(k for k in raw_keys if k.startswith("fred_"))

        captured: list[str] = []
        write_result = _call_iceberg_writer(fred_key, captured_queries=captured)

        assert write_result["status"] == "ok"
        assert write_result["domain"] == "economic_indicators"
        assert write_result["rows_inserted"] == 2

        assert "INSERT INTO economic_indicators" in captured[0]
        for col in ("date", "source", "series_id", "value"):
            assert col in captured[0]

    @responses.activate
    def test_ecb_ingest_and_iceberg_write(
        self, integration_aws, monkeypatch
    ):
        """ECB ingestion → Iceberg writer correctly inserts source='ecb'."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod

        result = ecb_mod.lambda_handler({}, None)
        assert result["status"] == "ok"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        ecb_key = next(k for k in raw_keys if k.startswith("ecb_"))

        captured: list[str] = []
        write_result = _call_iceberg_writer(ecb_key, captured_queries=captured)

        assert write_result["status"] == "ok"
        assert write_result["domain"] == "fx_rates"
        assert "'ecb'" in captured[0]


@pytest.mark.integration
class TestBackfillPipeline:
    """Backfill mode: ingestion with explicit dates, no DynamoDB state mutation."""

    @responses.activate
    def test_frankfurter_backfill_ingests_and_writes_iceberg(
        self, integration_aws, monkeypatch
    ):
        """Backfill ingestion writes raw JSON, Iceberg writer inserts, DynamoDB untouched."""
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
            f"{FRANKFURTER_API_URL}/2023-06-01..2023-06-30",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        result = fx_mod.lambda_handler(
            {"mode": "backfill", "start_date": "2023-06-01", "end_date": "2023-06-30"},
            None,
        )

        assert result["status"] == "ok"
        assert result["mode"] == "backfill"
        assert result["start_date"] == "2023-06-01"
        assert result["end_date"] == "2023-06-30"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 1

        write_result = _call_iceberg_writer(raw_keys[0])
        assert write_result["status"] == "ok"
        assert write_result["domain"] == "fx_rates"

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"

    @responses.activate
    def test_fred_backfill_routes_to_economic_domain(
        self, integration_aws, monkeypatch
    ):
        """FRED backfill → Iceberg writer routes to economic_indicators domain."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_fred_ingestion as fred_mod

        result = fred_mod.lambda_handler(
            {"mode": "backfill", "start_date": "2023-01-01", "end_date": "2023-12-31"},
            None,
        )

        assert result["status"] == "ok"
        assert result["mode"] == "backfill"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        fred_key = next(k for k in raw_keys if k.startswith("fred_"))

        captured: list[str] = []
        write_result = _call_iceberg_writer(fred_key, captured_queries=captured)

        assert write_result["status"] == "ok"
        assert write_result["domain"] == "economic_indicators"
        assert "INSERT INTO economic_indicators" in captured[0]
        for col in ("date", "source", "series_id", "value"):
            assert col in captured[0]

    @responses.activate
    def test_backfill_api_failure_does_not_corrupt_state(
        self, integration_aws, monkeypatch
    ):
        """API failure during backfill must not affect DynamoDB state."""
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
            f"{FRANKFURTER_API_URL}/2023-06-01..2023-06-30",
            json={"error": "server error"},
            status=500,
        )

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(Exception):
            fx_mod.lambda_handler(
                {"mode": "backfill", "start_date": "2023-06-01", "end_date": "2023-06-30"},
                None,
            )

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"


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
            f"{FRANKFURTER_API_URL}/2024-01-15..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        ingest_result = fx_mod.lambda_handler({}, None)
        assert ingest_result["status"] == "ok"
        assert ingest_result["start_date"] == "2024-01-15"

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-14"

        update_result = fx_mod.lambda_handler(
            {"action": "update_state", "end_date": ingest_result["end_date"]}, None
        )
        assert update_result["status"] == "state_updated"

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

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["start_date"] == "2024-01-11"
        assert ecb_result["start_date"] == "2024-01-13"
        assert fred_result["start_date"] == "2024-01-09"

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
    """Verify quality reports and freshness metrics published during transform and validation."""

    @responses.activate
    def test_quality_report_written_during_iceberg_write(
        self, integration_aws, monkeypatch
    ):
        """Iceberg writer writes a quality report JSON for every processed file."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        result = fx_mod.lambda_handler({}, None)

        _call_iceberg_writer(result["key"])

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

        athena = integration_aws["athena"]
        today_str = date.today().isoformat()

        start_resp = athena.start_query_execution(
            QueryString="SELECT MAX(date) AS latest_date, COUNT(*) AS total_records FROM fx_rates",
            ResultConfiguration={"OutputLocation": "s3://test-athena-results/results/"},
        )
        query_id = start_resp["QueryExecutionId"]

        import lambda_validation_function as val_mod

        with patch.object(val_mod, "athena", athena), patch.object(
            val_mod, "cloudwatch", integration_aws["cloudwatch"]
        ):
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
        """If Iceberg write is never called, DynamoDB state remains unchanged."""
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
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        fx_mod.lambda_handler({}, None)

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

        for source in ("frankfurter", "ecb", "fred"):
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": date.today().isoformat()},
                },
            )

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "no_new_data"
        assert ecb_result["status"] == "no_new_data"
        assert fred_result["status"] == "no_new_data"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert raw_keys == []


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------

SAMPLE_FRANKFURTER_BAD_RATES = {
    "base": "EUR",
    "start_date": "2024-01-02",
    "end_date": "2024-01-03",
    "rates": {
        "2024-01-02": {"USD": -1.1023, "GBP": -0.8671},
        "2024-01-03": {"USD": -1.0956, "GBP": -0.8612},
    },
}


@pytest.mark.integration
class TestCriticalQualityFailure:
    """CRITICAL quality check → quarantine + metrics + saga rollback."""

    @responses.activate
    def test_negative_rates_rejected_by_schema_and_state_not_updated(
        self, integration_aws, monkeypatch
    ):
        """Negative FX rates fail schema validation at ingestion: data never reaches S3,
        DynamoDB state unchanged (saga rollback)."""
        from common.schema_validation import SchemaValidationError

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
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_BAD_RATES,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(SchemaValidationError):
            fx_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 0

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"

    @responses.activate
    def test_negative_rates_never_saved_to_s3(self, integration_aws, monkeypatch):
        """Schema validation rejects bad data before S3 write."""
        from common.schema_validation import SchemaValidationError

        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_BAD_RATES,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(SchemaValidationError):
            fx_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 0


@pytest.mark.integration
class TestAPIErrorPropagation:
    """Upstream API errors propagate correctly through the pipeline."""

    @responses.activate
    def test_frankfurter_http_500_raises(self, integration_aws, monkeypatch):
        """Frankfurter API returning 500 causes lambda_handler to raise."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-02..{date.today().isoformat()}",
            json={"error": "internal server error"},
            status=500,
        )

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(Exception):
            fx_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert raw_keys == []

    @responses.activate
    def test_ecb_http_500_raises(self, integration_aws, monkeypatch):
        """ECB API returning 500 causes lambda_handler to raise."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(responses.GET, ECB_API_URL, json={}, status=500)

        import lambda_ecb_ingestion as ecb_mod

        with pytest.raises(Exception):
            ecb_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert raw_keys == []

    @responses.activate
    def test_fred_http_500_raises(self, integration_aws, monkeypatch):
        """FRED API returning 500 causes lambda_handler to raise."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        responses.add(responses.GET, FRED_API_URL, json={}, status=500)

        import lambda_fred_ingestion as fred_mod

        with pytest.raises(Exception):
            fred_mod.lambda_handler({}, None)

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert raw_keys == []

    @responses.activate
    def test_api_failure_leaves_dynamodb_state_unchanged(
        self, integration_aws, monkeypatch
    ):
        """API failure during ingestion must not advance DynamoDB state."""
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
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json={"error": "server error"},
            status=500,
        )

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(Exception):
            fx_mod.lambda_handler({}, None)

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"


@pytest.mark.integration
class TestIcebergWriteFailureSagaRollback:
    """Iceberg writer failure must prevent state advancement."""

    @responses.activate
    def test_transform_error_prevents_state_update(
        self, integration_aws, monkeypatch
    ):
        """If Iceberg writer raises, update_state must not be called — state unchanged."""
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
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_fx_ingestion as fx_mod
        import lambda_iceberg_writer as writer_mod

        ingest_result = fx_mod.lambda_handler({}, None)
        assert ingest_result["status"] == "ok"

        with patch.object(
            writer_mod, "_execute_athena_query", side_effect=RuntimeError("Athena timeout")
        ):
            with pytest.raises(RuntimeError, match="Athena timeout"):
                writer_mod.lambda_handler(
                    {
                        "raw_bucket": TEST_RAW_BUCKET,
                        "raw_key": ingest_result["key"],
                        "domain": "fx_rates",
                    },
                    None,
                )

        item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert item["last_processed_date"]["S"] == "2024-01-10"

    @responses.activate
    def test_update_state_without_state_table_raises(self, integration_aws):
        """Calling update_state when STATE_TABLE is not set raises RuntimeError."""
        import lambda_fx_ingestion as fx_mod

        with pytest.raises(RuntimeError, match="STATE_TABLE"):
            fx_mod.lambda_handler(
                {"action": "update_state", "end_date": "2024-01-31"}, None
            )

    @responses.activate
    def test_update_state_without_end_date_raises(
        self, integration_aws, monkeypatch
    ):
        """Calling update_state without end_date raises ValueError."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        import lambda_fx_ingestion as fx_mod

        with pytest.raises(ValueError, match="end_date"):
            fx_mod.lambda_handler({"action": "update_state"}, None)


@pytest.mark.integration
class TestValidationErrorScenarios:
    """Validation Lambda handles Athena failure states correctly."""

    def test_athena_non_succeeded_state_raises(self, integration_aws, monkeypatch):
        """Validation raises RuntimeError when Athena query state is FAILED."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        athena = integration_aws["athena"]
        start_resp = athena.start_query_execution(
            QueryString="SELECT 1",
            ResultConfiguration={"OutputLocation": "s3://test-athena-results/results/"},
        )
        query_id = start_resp["QueryExecutionId"]

        import lambda_validation_function as val_mod

        mock_execution = {
            "QueryExecution": {
                "Status": {"State": "FAILED"},
                "WorkGroup": "primary",
            }
        }

        with patch.object(val_mod, "athena", athena), patch.object(
            val_mod, "cloudwatch", integration_aws["cloudwatch"]
        ), patch.object(athena, "get_query_execution", return_value=mock_execution):
            with pytest.raises(RuntimeError, match="did not succeed"):
                val_mod.lambda_handler({"QueryExecutionId": query_id}, None)

    def test_missing_query_execution_id_raises(self, integration_aws, monkeypatch):
        """Validation raises ValueError when QueryExecutionId is missing from event."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        import lambda_validation_function as val_mod

        with pytest.raises(ValueError, match="Missing QueryExecutionId"):
            val_mod.lambda_handler({}, None)

    def test_athena_client_error_propagates(self, integration_aws, monkeypatch):
        """ClientError from Athena get_query_execution propagates to caller."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        import lambda_validation_function as val_mod

        error_response = {
            "Error": {"Code": "InvalidRequestException", "Message": "Query not found"}
        }

        with patch.object(val_mod, "athena") as mock_athena, patch.object(
            val_mod, "cloudwatch", integration_aws["cloudwatch"]
        ):
            mock_athena.get_query_execution.side_effect = ClientError(
                error_response, "GetQueryExecution"
            )
            with pytest.raises(ClientError):
                val_mod.lambda_handler({"QueryExecutionId": "bad-id"}, None)

    def test_empty_athena_results_report_stale(self, integration_aws, monkeypatch):
        """Validation reports empty/stale when Athena returns zero rows."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)

        athena = integration_aws["athena"]
        start_resp = athena.start_query_execution(
            QueryString="SELECT 1",
            ResultConfiguration={"OutputLocation": "s3://test-athena-results/results/"},
        )
        query_id = start_resp["QueryExecutionId"]

        import lambda_validation_function as val_mod

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
                            {},
                            {"VarCharValue": "0"},
                        ]
                    },
                ]
            }
        }

        with patch.object(val_mod, "athena", athena), patch.object(
            val_mod, "cloudwatch", integration_aws["cloudwatch"]
        ), patch.object(
            athena, "get_query_execution", return_value=mock_execution
        ), patch.object(
            athena, "get_query_results", return_value=mock_results
        ):
            result = val_mod.lambda_handler({"QueryExecutionId": query_id}, None)

        assert result["is_empty"] is True
        assert result["is_fresh"] is False
        assert result["status"] == "FAILED"


# ---------------------------------------------------------------------------
# Partial no_new_data routing tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPartialNoNewDataRouting:
    """Verify correct routing when some sources have data and others are caught up.

    Mirrors the Step Functions Choice states: Check-FX-Data, Check-Economic-Data,
    Check-FX-State-Update, Check-ECB-State-Update, Check-FRED-State-Update.
    """

    @responses.activate
    def test_fx_has_data_ecb_fred_caught_up(
        self, integration_aws, monkeypatch
    ):
        """FX has new data; ECB and FRED are caught up — only FX writes and updates."""
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
        for source in ("ecb", "fred"):
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": date.today().isoformat()},
                },
            )

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "ok"
        assert ecb_result["status"] == "no_new_data"
        assert fred_result["status"] == "no_new_data"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 1
        assert raw_keys[0].startswith("exchange_rates_EUR_")

        _call_iceberg_writer(raw_keys[0], domain="fx_rates")

        fx_mod.lambda_handler(
            {"action": "update_state", "end_date": fx_result["end_date"]}, None
        )

        fx_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert fx_item["last_processed_date"]["S"] == fx_result["end_date"]

        ecb_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "ecb"}},
        )["Item"]
        assert ecb_item["last_processed_date"]["S"] == date.today().isoformat()

        fred_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "fred"}},
        )["Item"]
        assert fred_item["last_processed_date"]["S"] == date.today().isoformat()

    @responses.activate
    def test_fred_has_data_fx_ecb_caught_up(
        self, integration_aws, monkeypatch
    ):
        """FRED has new data; FX and ECB are caught up — only economic write and update."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        for source in ("frankfurter", "ecb"):
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": date.today().isoformat()},
                },
            )
        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": "2024-01-08"},
            },
        )

        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "no_new_data"
        assert ecb_result["status"] == "no_new_data"
        assert fred_result["status"] == "ok"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 1
        assert "fred_" in raw_keys[0]

        _call_iceberg_writer(raw_keys[0], domain="economic_indicators")

        fred_mod.lambda_handler(
            {"action": "update_state", "end_date": fred_result["end_date"]}, None
        )

        fred_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "fred"}},
        )["Item"]
        assert fred_item["last_processed_date"]["S"] == fred_result["end_date"]

        fx_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert fx_item["last_processed_date"]["S"] == date.today().isoformat()

    @responses.activate
    def test_fx_and_ecb_have_data_fred_caught_up(
        self, integration_aws, monkeypatch
    ):
        """FX and ECB have new data; FRED caught up — both FX writes happen, FRED skipped."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        for source, last_date in [
            ("frankfurter", "2024-01-10"),
            ("ecb", "2024-01-12"),
        ]:
            ddb.put_item(
                TableName=TEST_STATE_TABLE,
                Item={
                    "pipeline_id": {"S": "fxlake"},
                    "source": {"S": source},
                    "last_processed_date": {"S": last_date},
                },
            )
        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": date.today().isoformat()},
            },
        )

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        assert fx_result["status"] == "ok"
        assert ecb_result["status"] == "ok"
        assert fred_result["status"] == "no_new_data"

        raw_keys = _s3_keys(integration_aws["s3"], TEST_RAW_BUCKET)
        assert len(raw_keys) == 2

        fx_keys = [k for k in raw_keys if k.startswith("exchange_rates_EUR_")]
        ecb_keys = [k for k in raw_keys if k.startswith("ecb_rates_")]
        assert len(fx_keys) == 1
        assert len(ecb_keys) == 1

        _call_iceberg_writer(fx_keys[0], domain="fx_rates")

        fx_mod.lambda_handler(
            {"action": "update_state", "end_date": fx_result["end_date"]}, None
        )
        ecb_mod.lambda_handler(
            {"action": "update_state", "end_date": ecb_result["end_date"]}, None
        )

        fx_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "frankfurter"}},
        )["Item"]
        assert fx_item["last_processed_date"]["S"] == fx_result["end_date"]

        ecb_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "ecb"}},
        )["Item"]
        assert ecb_item["last_processed_date"]["S"] == ecb_result["end_date"]

        fred_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "fred"}},
        )["Item"]
        assert fred_item["last_processed_date"]["S"] == date.today().isoformat()

    @responses.activate
    def test_partial_no_new_data_does_not_corrupt_caught_up_state(
        self, integration_aws, monkeypatch
    ):
        """State for caught-up sources is never modified during partial pipeline runs."""
        monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)
        ddb = integration_aws["dynamodb"]

        ecb_original_date = "2024-05-15"
        fred_original_date = "2024-05-14"

        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "frankfurter"},
                "last_processed_date": {"S": "2024-01-10"},
            },
        )
        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "ecb"},
                "last_processed_date": {"S": ecb_original_date},
            },
        )
        ddb.put_item(
            TableName=TEST_STATE_TABLE,
            Item={
                "pipeline_id": {"S": "fxlake"},
                "source": {"S": "fred"},
                "last_processed_date": {"S": fred_original_date},
            },
        )

        responses.add(
            responses.GET,
            f"{FRANKFURTER_API_URL}/2024-01-11..{date.today().isoformat()}",
            json=SAMPLE_FRANKFURTER_RESPONSE,
            status=200,
        )
        responses.add(responses.GET, ECB_API_URL, json=SAMPLE_ECB_RESPONSE, status=200)
        responses.add(responses.GET, FRED_API_URL, json=SAMPLE_FRED_RESPONSE, status=200)

        import lambda_ecb_ingestion as ecb_mod
        import lambda_fred_ingestion as fred_mod
        import lambda_fx_ingestion as fx_mod

        fx_result = fx_mod.lambda_handler({}, None)
        ecb_result = ecb_mod.lambda_handler({}, None)
        fred_result = fred_mod.lambda_handler({}, None)

        sources_with_data = [
            (mod, result)
            for mod, result in [
                (fx_mod, fx_result),
                (ecb_mod, ecb_result),
                (fred_mod, fred_result),
            ]
            if result["status"] == "ok"
        ]
        for mod, result in sources_with_data:
            mod.lambda_handler(
                {"action": "update_state", "end_date": result["end_date"]}, None
            )

        ecb_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "ecb"}},
        )["Item"]
        fred_item = ddb.get_item(
            TableName=TEST_STATE_TABLE,
            Key={"pipeline_id": {"S": "fxlake"}, "source": {"S": "fred"}},
        )["Item"]

        assert ecb_item["last_processed_date"]["S"] in (
            ecb_original_date,
            ecb_result.get("end_date", ecb_original_date),
        )
        assert fred_item["last_processed_date"]["S"] in (
            fred_original_date,
            fred_result.get("end_date", fred_original_date),
        )
