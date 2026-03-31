import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Environment variables — must be set BEFORE Lambda modules are imported,
# because they read os.environ at module level.
# ---------------------------------------------------------------------------
os.environ.setdefault("RAW_BUCKET", "test-raw-bucket")
os.environ.setdefault("START_DATE", "2024-01-01")
os.environ.setdefault("END_DATE", "2024-01-31")
os.environ.setdefault("BASE_CURRENCY", "EUR")
os.environ.setdefault("BASE_API_URL", "https://api.frankfurter.app")
os.environ.setdefault("METRIC_NAMESPACE", "TestFXLake/Athena")
os.environ.setdefault("PIPELINE", "fxlake-etl-test")

# Fake AWS credentials for moto
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

# ---------------------------------------------------------------------------
# Mock awsglue — not available outside Glue runtime.
# Must be registered in sys.modules BEFORE glue_transform is imported.
# ---------------------------------------------------------------------------
_glue_utils_mock = MagicMock()
_glue_utils_mock.getResolvedOptions.return_value = {
    "RAW_BUCKET": "test-raw-bucket",
    "PROCESSED_BUCKET": "test-processed-bucket",
    "OUTPUT_FORMAT": "parquet",
    "LOG_LEVEL": "INFO",
}
sys.modules["awsglue"] = MagicMock()
sys.modules["awsglue.utils"] = _glue_utils_mock

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
import boto3
import pytest
from moto import mock_aws


@pytest.fixture()
def s3_mock():
    """Activate moto AWS mock and create the standard S3 buckets."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-raw-bucket")
        client.create_bucket(Bucket="test-processed-bucket")
        yield client
