import importlib
import io
import json
import logging
import sys
from unittest.mock import MagicMock

import polars as pl
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

import glue_transform

SAMPLE_RATES_JSON = {
    "base": "EUR",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8612},
    },
}


def _put_json(s3_client, key, data):
    s3_client.put_object(
        Bucket="test-raw-bucket",
        Key=key,
        Body=json.dumps(data),
    )


def _read_parquet_from_s3(s3_client, key):
    obj = s3_client.get_object(Bucket="test-processed-bucket", Key=key)
    table = pq.read_table(io.BytesIO(obj["Body"].read()))
    return pl.from_arrow(table)


def _read_csv_from_s3(s3_client, key):
    obj = s3_client.get_object(Bucket="test-processed-bucket", Key=key)
    return pl.read_csv(obj["Body"].read())


# ---------------------------------------------------------------------------
# process_key() — Parquet output (default)
# ---------------------------------------------------------------------------
class TestProcessKeyParquet:
    def test_flattens_nested_rates(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.process_key("rates.json")

        df = _read_parquet_from_s3(s3_mock, "exchange_rates/rates.parquet")
        assert df.shape == (4, 4)
        assert set(df.columns) == {"base_currency", "target_currency", "rate", "date"}

    def test_correct_values(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.process_key("rates.json")

        df = _read_parquet_from_s3(s3_mock, "exchange_rates/rates.parquet")
        assert (df["base_currency"] == "EUR").all()

        usd_jan2 = df.filter(
            (pl.col("date") == "2024-01-02") & (pl.col("target_currency") == "USD")
        )
        assert usd_jan2.shape[0] == 1
        assert abs(usd_jan2["rate"][0] - 1.1023) < 1e-6

    def test_returns_output_key(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        out_key = glue_transform.process_key("rates.json")

        assert out_key == "exchange_rates/rates.parquet"

    def test_prefixed_key_strips_prefix(self, s3_mock):
        _put_json(s3_mock, "prefix/rates.json", SAMPLE_RATES_JSON)

        out_key = glue_transform.process_key("prefix/rates.json")

        assert out_key == "exchange_rates/rates.parquet"
        df = _read_parquet_from_s3(s3_mock, "exchange_rates/rates.parquet")
        assert df.shape == (4, 4)

    def test_missing_key_raises(self, s3_mock):
        with pytest.raises(ClientError):
            glue_transform.process_key("nonexistent.json")

    def test_missing_base_field_raises(self, s3_mock):
        _put_json(s3_mock, "no_base.json", {"rates": {"2024-01-02": {"USD": 1.1}}})

        with pytest.raises(KeyError):
            glue_transform.process_key("no_base.json")


# ---------------------------------------------------------------------------
# process_key() — CSV output
# ---------------------------------------------------------------------------
class TestProcessKeyCSV:
    def test_csv_output(self, s3_mock, monkeypatch):
        monkeypatch.setattr(glue_transform, "output_format", "csv")
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        out_key = glue_transform.process_key("rates.json")

        assert out_key == "exchange_rates/rates.csv"
        df = _read_csv_from_s3(s3_mock, "exchange_rates/rates.csv")
        assert df.shape == (4, 4)
        assert set(df.columns) == {"base_currency", "target_currency", "rate", "date"}


# ---------------------------------------------------------------------------
# process_key() — empty input
# ---------------------------------------------------------------------------
class TestProcessKeyEmpty:
    def test_empty_rates(self, s3_mock, caplog):
        _put_json(s3_mock, "empty.json", {"base": "EUR", "rates": {}})

        with caplog.at_level(logging.WARNING):
            glue_transform.process_key("empty.json")

        assert "No rates found" in caplog.text

        # Empty DataFrame produces a valid but empty Parquet file
        df = _read_parquet_from_s3(s3_mock, "exchange_rates/empty.parquet")
        assert len(df) == 0


# ---------------------------------------------------------------------------
# list_json_keys()
# ---------------------------------------------------------------------------
class TestListJsonKeys:
    def test_lists_only_json_files(self, s3_mock):
        _put_json(s3_mock, "a.json", {})
        _put_json(s3_mock, "b.json", {})
        s3_mock.put_object(Bucket="test-raw-bucket", Key="c.csv", Body=b"data")

        keys = glue_transform.list_json_keys("test-raw-bucket")

        assert sorted(keys) == ["a.json", "b.json"]

    def test_empty_bucket(self, s3_mock):
        keys = glue_transform.list_json_keys("test-raw-bucket")

        assert keys == []

    def test_lists_multiple_files(self, s3_mock):
        for i in range(5):
            _put_json(s3_mock, f"file{i}.json", {})

        keys = glue_transform.list_json_keys("test-raw-bucket")

        assert len(keys) == 5

    def test_nonexistent_bucket_raises_client_error(self, s3_mock):
        with pytest.raises(ClientError):
            glue_transform.list_json_keys("nonexistent-bucket")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------
class TestMain:
    def test_processes_all_files(self, s3_mock):
        _put_json(s3_mock, "a.json", SAMPLE_RATES_JSON)
        _put_json(s3_mock, "b.json", SAMPLE_RATES_JSON)

        glue_transform.main()

        df_a = _read_parquet_from_s3(s3_mock, "exchange_rates/a.parquet")
        df_b = _read_parquet_from_s3(s3_mock, "exchange_rates/b.parquet")
        assert df_a.shape == (4, 4)
        assert df_b.shape == (4, 4)

    def test_reraises_on_bad_file(self, s3_mock):
        _put_json(s3_mock, "good.json", SAMPLE_RATES_JSON)
        s3_mock.put_object(
            Bucket="test-raw-bucket", Key="bad.json", Body=b"not json"
        )

        with pytest.raises(Exception):
            glue_transform.main()


# ---------------------------------------------------------------------------
# Module-level OUTPUT_FORMAT guard
# ---------------------------------------------------------------------------
class TestOutputFormatGuard:
    def test_invalid_output_format_raises(self):
        mock = MagicMock()
        mock.getResolvedOptions.return_value = {
            "RAW_BUCKET": "test-raw-bucket",
            "PROCESSED_BUCKET": "test-processed-bucket",
            "OUTPUT_FORMAT": "xml",
            "LOG_LEVEL": "INFO",
        }
        sys.modules["awsglue.utils"] = mock

        with pytest.raises(ValueError, match="OUTPUT_FORMAT must be either"):
            importlib.reload(glue_transform)

        # Restore valid config so other tests aren't affected
        mock.getResolvedOptions.return_value = {
            "RAW_BUCKET": "test-raw-bucket",
            "PROCESSED_BUCKET": "test-processed-bucket",
            "OUTPUT_FORMAT": "parquet",
            "LOG_LEVEL": "INFO",
        }
        importlib.reload(glue_transform)
