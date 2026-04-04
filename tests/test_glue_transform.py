import importlib
import io
import json
import logging
import sys
from typing import List
from unittest.mock import MagicMock, patch

import glue_transform
import polars as pl
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

SAMPLE_RATES_JSON = {
    "base": "EUR",
    "rates": {
        "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
        "2024-01-03": {"USD": 1.0956, "GBP": 0.8612},
    },
}

SAMPLE_FRED_JSON = {
    "source": "fred",
    "series_id": "UNRATE",
    "observations": {
        "2024-01-01": 3.7,
        "2024-02-01": 3.9,
    },
}

# Partition paths produced by SAMPLE_RATES_JSON
PARTITION_JAN02 = "fx_rates/year=2024/month=01/day=02"
PARTITION_JAN03 = "fx_rates/year=2024/month=01/day=03"

# Partition paths produced by SAMPLE_FRED_JSON
PARTITION_FRED_JAN = "economic_indicators/year=2024/month=01/day=01"
PARTITION_FRED_FEB = "economic_indicators/year=2024/month=02/day=01"


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


def _list_processed_keys(s3_client, prefix="fx_rates/") -> List[str]:
    result = s3_client.list_objects_v2(Bucket="test-processed-bucket", Prefix=prefix)
    return [obj["Key"] for obj in result.get("Contents", [])]


# ---------------------------------------------------------------------------
# process_key() — Parquet output (default)
# ---------------------------------------------------------------------------
class TestProcessKeyParquet:
    def test_flattens_nested_rates(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.process_key("rates.json")

        df_jan02 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/rates.parquet")
        df_jan03 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN03}/rates.parquet")
        assert df_jan02.shape == (2, 5)
        assert df_jan03.shape == (2, 5)
        assert set(df_jan02.columns) == {"base_currency", "target_currency", "rate", "date", "source"}

    def test_correct_values(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.process_key("rates.json")

        df = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/rates.parquet")
        assert (df["base_currency"] == "EUR").all()

        usd = df.filter(pl.col("target_currency") == "USD")
        assert usd.shape[0] == 1
        assert abs(usd["rate"][0] - 1.1023) < 1e-6

    def test_returns_output_keys(self, s3_mock):
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        out_keys = glue_transform.process_key("rates.json")

        assert len(out_keys) == 2
        assert f"{PARTITION_JAN02}/rates.parquet" in out_keys
        assert f"{PARTITION_JAN03}/rates.parquet" in out_keys

    def test_prefixed_key_strips_prefix(self, s3_mock):
        _put_json(s3_mock, "prefix/rates.json", SAMPLE_RATES_JSON)

        out_keys = glue_transform.process_key("prefix/rates.json")

        assert len(out_keys) == 2
        assert all("rates.parquet" in k for k in out_keys)
        df = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/rates.parquet")
        assert df.shape == (2, 5)

    def test_missing_key_raises(self, s3_mock):
        with pytest.raises(ClientError):
            glue_transform.process_key("nonexistent.json")

    def test_missing_base_field_raises(self, s3_mock):
        _put_json(s3_mock, "no_base.json", {"rates": {"2024-01-02": {"USD": 1.1}}})

        with pytest.raises(KeyError):
            glue_transform.process_key("no_base.json")

    def test_s3_write_failure_raises_client_error(self, s3_mock):
        """_write_partition S3 put_object ClientError propagates through process_key."""
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)
        error_response = {"Error": {"Code": "AccessDenied", "Message": "Denied"}}
        with patch.object(glue_transform, "s3") as mock_s3:
            mock_s3.get_object.return_value = {
                "Body": io.BytesIO(json.dumps(SAMPLE_RATES_JSON).encode())
            }
            mock_s3.put_object.side_effect = ClientError(error_response, "PutObject")

            with pytest.raises(ClientError):
                glue_transform.process_key("rates.json")

    def test_serialization_failure_raises(self, s3_mock, monkeypatch):
        """_write_partition serialization error propagates through process_key."""
        import pyarrow

        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)
        monkeypatch.setattr(
            glue_transform.pq,
            "write_table",
            MagicMock(side_effect=pyarrow.lib.ArrowException("bad")),
        )

        with pytest.raises(pyarrow.lib.ArrowException):
            glue_transform.process_key("rates.json")


# ---------------------------------------------------------------------------
# process_key() — CSV output
# ---------------------------------------------------------------------------
class TestProcessKeyCSV:
    def test_csv_output(self, s3_mock, monkeypatch):
        monkeypatch.setattr(glue_transform, "output_format", "csv")
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        out_keys = glue_transform.process_key("rates.json")

        assert f"{PARTITION_JAN02}/rates.csv" in out_keys
        assert f"{PARTITION_JAN03}/rates.csv" in out_keys
        df = _read_csv_from_s3(s3_mock, f"{PARTITION_JAN02}/rates.csv")
        assert df.shape == (2, 5)
        assert set(df.columns) == {"base_currency", "target_currency", "rate", "date", "source"}


# ---------------------------------------------------------------------------
# process_key() — empty input
# ---------------------------------------------------------------------------
class TestProcessKeyEmpty:
    def test_empty_rates(self, s3_mock, caplog):
        _put_json(s3_mock, "empty.json", {"base": "EUR", "rates": {}})

        with caplog.at_level(logging.WARNING):
            out_keys = glue_transform.process_key("empty.json")

        assert "No rates found" in caplog.text
        assert out_keys == []
        assert _list_processed_keys(s3_mock) == []


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

        # Check both partition dates for both files (4 output files total)
        df_a_jan02 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/a.parquet")
        df_a_jan03 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN03}/a.parquet")
        df_b_jan02 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/b.parquet")
        df_b_jan03 = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN03}/b.parquet")
        assert df_a_jan02.shape == (2, 5)
        assert df_a_jan03.shape == (2, 5)
        assert df_b_jan02.shape == (2, 5)
        assert df_b_jan03.shape == (2, 5)
        assert len(_list_processed_keys(s3_mock)) == 4

    def test_reraises_on_bad_file(self, s3_mock):
        _put_json(s3_mock, "good.json", SAMPLE_RATES_JSON)
        s3_mock.put_object(
            Bucket="test-raw-bucket", Key="bad.json", Body=b"not json"
        )

        with pytest.raises(json.JSONDecodeError):
            glue_transform.main()


# ---------------------------------------------------------------------------
# process_key() — ECB source detection (payload source field)
# ---------------------------------------------------------------------------
class TestSourceDetection:
    def test_ecb_payload_source_field_used(self, s3_mock):
        """ECB payload has explicit 'source' field — must appear in output column."""
        ecb_data = {
            "base": "EUR",
            "source": "ecb",
            "rates": {"2024-01-02": {"USD": 1.1023}},
        }
        _put_json(s3_mock, "ecb_rates.json", ecb_data)

        glue_transform.process_key("ecb_rates.json")

        df = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/ecb_rates.parquet")
        assert (df["source"] == "ecb").all()

    def test_frankfurter_no_source_field_defaults_to_frankfurter(self, s3_mock):
        """Frankfurter payload has no 'source' field — inferred as 'frankfurter'."""
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.process_key("rates.json")

        df = _read_parquet_from_s3(s3_mock, f"{PARTITION_JAN02}/rates.parquet")
        assert (df["source"] == "frankfurter").all()


# ---------------------------------------------------------------------------
# process_key() — FRED economic indicators dispatch
# ---------------------------------------------------------------------------
class TestProcessEconomicKey:
    def test_fred_key_dispatches_to_economic_path(self, s3_mock):
        """Files with 'fred_' prefix must land in economic_indicators/ not fx_rates/."""
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-02-01.json", SAMPLE_FRED_JSON)

        out_keys = glue_transform.process_key("fred_unrate_2024-01-01_to_2024-02-01.json")

        assert all("economic_indicators/" in k for k in out_keys)
        assert not any("fx_rates/" in k for k in out_keys)

    def test_returns_two_partition_keys(self, s3_mock):
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-02-01.json", SAMPLE_FRED_JSON)

        out_keys = glue_transform.process_key("fred_unrate_2024-01-01_to_2024-02-01.json")

        assert len(out_keys) == 2
        assert f"{PARTITION_FRED_JAN}/fred_unrate_2024-01-01_to_2024-02-01.parquet" in out_keys
        assert f"{PARTITION_FRED_FEB}/fred_unrate_2024-01-01_to_2024-02-01.parquet" in out_keys

    def test_output_schema(self, s3_mock):
        """Economic indicators schema: {date, source, series_id, value}."""
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-02-01.json", SAMPLE_FRED_JSON)

        glue_transform.process_key("fred_unrate_2024-01-01_to_2024-02-01.json")

        df = _read_parquet_from_s3(
            s3_mock, f"{PARTITION_FRED_JAN}/fred_unrate_2024-01-01_to_2024-02-01.parquet"
        )
        assert df.shape == (1, 4)
        assert set(df.columns) == {"date", "source", "series_id", "value"}

    def test_correct_values(self, s3_mock):
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-02-01.json", SAMPLE_FRED_JSON)

        glue_transform.process_key("fred_unrate_2024-01-01_to_2024-02-01.json")

        df = _read_parquet_from_s3(
            s3_mock, f"{PARTITION_FRED_JAN}/fred_unrate_2024-01-01_to_2024-02-01.parquet"
        )
        assert df["source"][0] == "fred"
        assert df["series_id"][0] == "UNRATE"
        assert abs(df["value"][0] - 3.7) < 1e-6

    def test_missing_series_id_raises(self, s3_mock):
        bad_payload = {"source": "fred", "observations": {"2024-01-01": 3.7}}
        _put_json(s3_mock, "fred_bad.json", bad_payload)

        with pytest.raises(KeyError):
            glue_transform.process_key("fred_bad.json")

    def test_empty_observations_returns_empty_keys(self, s3_mock, caplog):
        empty_payload = {"source": "fred", "series_id": "UNRATE", "observations": {}}
        _put_json(s3_mock, "fred_empty.json", empty_payload)

        with caplog.at_level(logging.WARNING):
            out_keys = glue_transform.process_key("fred_empty.json")

        assert "No observations found" in caplog.text
        assert out_keys == []

    def test_csv_output_format(self, s3_mock, monkeypatch):
        monkeypatch.setattr(glue_transform, "output_format", "csv")
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-02-01.json", SAMPLE_FRED_JSON)

        out_keys = glue_transform.process_key("fred_unrate_2024-01-01_to_2024-02-01.json")

        assert all(k.endswith(".csv") for k in out_keys)
        df = _read_csv_from_s3(s3_mock, out_keys[0])
        assert set(df.columns) == {"date", "source", "series_id", "value"}


# ---------------------------------------------------------------------------
# Cross-domain isolation — mixed FX + FRED run
# ---------------------------------------------------------------------------
class TestCrossDomainIsolation:
    def test_mixed_domain_files_route_to_separate_domains(self, s3_mock):
        """FRED and FX files processed in one main() call must land in separate domains."""
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-01-31.json", SAMPLE_FRED_JSON)
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.main()

        fx_keys = _list_processed_keys(s3_mock, prefix="fx_rates/")
        econ_keys = _list_processed_keys(s3_mock, prefix="economic_indicators/")

        assert len(fx_keys) == 2, "FX file should produce 2 date partitions"
        assert len(econ_keys) == 2, "FRED file should produce 2 date partitions"
        assert all(k.startswith("fx_rates/") for k in fx_keys)
        assert all(k.startswith("economic_indicators/") for k in econ_keys)

    def test_fred_data_not_written_to_fx_domain(self, s3_mock):
        """FRED observations must never appear under fx_rates/."""
        _put_json(s3_mock, "fred_unrate_2024-01-01_to_2024-01-31.json", SAMPLE_FRED_JSON)

        glue_transform.main()

        fx_keys = _list_processed_keys(s3_mock, prefix="fx_rates/")
        assert fx_keys == [], "FRED file must not write anything to fx_rates/"

    def test_fx_data_not_written_to_economic_domain(self, s3_mock):
        """FX rates must never appear under economic_indicators/."""
        _put_json(s3_mock, "rates.json", SAMPLE_RATES_JSON)

        glue_transform.main()

        econ_keys = _list_processed_keys(s3_mock, prefix="economic_indicators/")
        assert econ_keys == [], "FX file must not write anything to economic_indicators/"


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
