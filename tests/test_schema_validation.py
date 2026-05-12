"""Tests for schema validation module — schema loading, dispatch, and validation logic."""
import pytest
from common.schema_validation import (
    _SCHEMAS_DIR,
    SchemaValidationError,
    _detect_schema,
    load_schema,
    validate_data,
)


# ---------------------------------------------------------------------------
# Schema file existence
# ---------------------------------------------------------------------------
class TestSchemaFiles:
    """Verify all expected schema files exist on disk."""

    @pytest.mark.parametrize(
        "path",
        [
            "raw/ecb_response.json",
            "raw/fred_response.json",
            "raw/frankfurter_response.json",
            "processed/fx_rates.json",
            "processed/economic_indicators.json",
        ],
    )
    def test_schema_file_exists(self, path):
        assert (_SCHEMAS_DIR / path).is_file()


# ---------------------------------------------------------------------------
# load_schema
# ---------------------------------------------------------------------------
class TestLoadSchema:
    def test_loads_valid_schema(self):
        schema = load_schema("processed/fx_rates.json")
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "properties" in schema

    def test_loads_economic_schema(self):
        schema = load_schema("processed/economic_indicators.json")
        assert schema["title"] == "Normalised Economic Indicators"

    def test_raises_on_nonexistent_file(self):
        load_schema.cache_clear()
        with pytest.raises(FileNotFoundError):
            load_schema("nonexistent/schema.json")

    def test_caches_repeated_loads(self):
        load_schema.cache_clear()
        first = load_schema("processed/fx_rates.json")
        second = load_schema("processed/fx_rates.json")
        assert first is second

    def teardown_method(self):
        load_schema.cache_clear()


# ---------------------------------------------------------------------------
# _detect_schema
# ---------------------------------------------------------------------------
class TestDetectSchema:
    def test_detects_fx_rates(self):
        data = {"base": "EUR", "rates": {"2024-01-02": {"USD": 1.1}}}
        assert _detect_schema(data) == "processed/fx_rates.json"

    def test_detects_economic_indicators(self):
        data = {"source": "fred", "series_id": "UNRATE", "observations": {"2024-01-02": 3.7}}
        assert _detect_schema(data) == "processed/economic_indicators.json"

    def test_raises_on_unrecognised_shape(self):
        with pytest.raises(SchemaValidationError, match="Cannot detect schema"):
            _detect_schema({"unknown_key": "value"})

    def test_fx_takes_priority_when_both_keys_present(self):
        data = {"rates": {"2024-01-02": {"USD": 1.1}}, "observations": {"2024-01-02": 3.7}}
        assert _detect_schema(data) == "processed/fx_rates.json"


# ---------------------------------------------------------------------------
# validate_data — FX rates schema
# ---------------------------------------------------------------------------
class TestValidateFxRates:
    VALID_FX = {
        "base": "EUR",
        "source": "frankfurter",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "amount": 1.0,
        "rates": {
            "2024-01-02": {"USD": 1.1023, "GBP": 0.8671},
            "2024-01-03": {"USD": 1.0987},
        },
    }

    def test_valid_fx_data_passes(self):
        validate_data(dict(self.VALID_FX))

    def test_missing_base_fails(self):
        data = {k: v for k, v in self.VALID_FX.items() if k != "base"}
        with pytest.raises(SchemaValidationError) as exc_info:
            validate_data(data)
        assert "processed/fx_rates.json" in exc_info.value.schema_name
        assert len(exc_info.value.errors) > 0

    def test_missing_rates_fails(self):
        data = {k: v for k, v in self.VALID_FX.items() if k != "rates"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_invalid_base_currency_format_fails(self):
        data = {**self.VALID_FX, "base": "euro"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_negative_rate_fails(self):
        data = {**self.VALID_FX, "rates": {"2024-01-02": {"USD": -1.0}}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_zero_rate_fails(self):
        data = {**self.VALID_FX, "rates": {"2024-01-02": {"USD": 0}}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_invalid_date_key_fails(self):
        data = {**self.VALID_FX, "rates": {"not-a-date": {"USD": 1.1}}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_empty_rates_fails(self):
        data = {**self.VALID_FX, "rates": {}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_string_rate_value_fails(self):
        data = {**self.VALID_FX, "rates": {"2024-01-02": {"USD": "1.1"}}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_minimal_valid_fx(self):
        data = {"base": "EUR", "rates": {"2024-01-02": {"USD": 1.1}}}
        validate_data(data)


# ---------------------------------------------------------------------------
# validate_data — Economic indicators schema
# ---------------------------------------------------------------------------
class TestValidateEconomicIndicators:
    VALID_ECON = {
        "source": "fred",
        "series_id": "UNRATE",
        "observations": {
            "2024-01-01": 3.7,
            "2024-02-01": 3.9,
        },
    }

    def test_valid_economic_data_passes(self):
        validate_data(dict(self.VALID_ECON))

    def test_missing_source_fails(self):
        data = {k: v for k, v in self.VALID_ECON.items() if k != "source"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_missing_series_id_fails(self):
        data = {k: v for k, v in self.VALID_ECON.items() if k != "series_id"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_missing_observations_fails(self):
        data = {k: v for k, v in self.VALID_ECON.items() if k != "observations"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_empty_series_id_fails(self):
        data = {**self.VALID_ECON, "series_id": ""}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_empty_observations_fails(self):
        data = {**self.VALID_ECON, "observations": {}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_invalid_date_key_fails(self):
        data = {**self.VALID_ECON, "observations": {"bad-date": 3.7}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_string_observation_value_fails(self):
        data = {**self.VALID_ECON, "observations": {"2024-01-01": "3.7"}}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_additional_properties_rejected(self):
        data = {**self.VALID_ECON, "extra_field": "not allowed"}
        with pytest.raises(SchemaValidationError):
            validate_data(data)

    def test_negative_observation_value_passes(self):
        data = {**self.VALID_ECON, "observations": {"2024-01-01": -2.5}}
        validate_data(data)


# ---------------------------------------------------------------------------
# Kill switch (SCHEMA_VALIDATION_ENABLED)
# ---------------------------------------------------------------------------
class TestKillSwitch:
    def test_disabled_skips_validation(self, monkeypatch):
        monkeypatch.setenv("SCHEMA_VALIDATION_ENABLED", "false")
        validate_data({"bad": "data"})

    def test_disabled_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("SCHEMA_VALIDATION_ENABLED", "False")
        validate_data({"bad": "data"})

    def test_enabled_by_default(self):
        with pytest.raises(SchemaValidationError):
            validate_data({"bad": "data"})

    def test_explicit_true_validates(self, monkeypatch):
        monkeypatch.setenv("SCHEMA_VALIDATION_ENABLED", "true")
        with pytest.raises(SchemaValidationError):
            validate_data({"bad": "data"})


# ---------------------------------------------------------------------------
# SchemaValidationError attributes
# ---------------------------------------------------------------------------
class TestSchemaValidationError:
    def test_error_attributes(self):
        err = SchemaValidationError(
            "test message",
            schema_name="processed/fx_rates.json",
            errors=["err1", "err2"],
        )
        assert str(err) == "test message"
        assert err.schema_name == "processed/fx_rates.json"
        assert err.errors == ["err1", "err2"]
