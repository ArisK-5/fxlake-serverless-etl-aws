"""Tests for glue/quality.py — data quality check framework.

Uses known-bad data (nulls, negatives, duplicates) to verify each check
function and the domain-level runners.
"""

import polars as pl
import pytest
from quality import (
    CheckLevel,
    QualityResult,
    build_quality_report,
    check_duplicates,
    check_no_nulls,
    check_positive_values,
    check_rate_range,
    check_required_columns,
    check_value_in_set,
    has_critical_failures,
    run_economic_checks,
    run_fx_checks,
)


# ---------------------------------------------------------------------------
# QualityResult dataclass
# ---------------------------------------------------------------------------
class TestQualityResult:
    def test_frozen(self) -> None:
        r = QualityResult(
            check_name="test",
            level=CheckLevel.CRITICAL,
            passed=True,
            message="ok",
            failing_row_count=0,
        )
        with pytest.raises(AttributeError):
            r.passed = False  # type: ignore[misc]

    def test_inconsistent_passed_true_with_failures_raises(self) -> None:
        with pytest.raises(ValueError, match="passed=True"):
            QualityResult(
                check_name="test",
                level=CheckLevel.CRITICAL,
                passed=True,
                message="ok",
                failing_row_count=5,
            )

    def test_inconsistent_passed_false_with_zero_failures_raises(self) -> None:
        with pytest.raises(ValueError, match="passed=False"):
            QualityResult(
                check_name="test",
                level=CheckLevel.CRITICAL,
                passed=False,
                message="bad",
                failing_row_count=0,
            )

    def test_fields(self) -> None:
        r = QualityResult(
            check_name="nulls",
            level=CheckLevel.WARNING,
            passed=False,
            message="found nulls",
            failing_row_count=3,
        )
        assert r.check_name == "nulls"
        assert r.level == CheckLevel.WARNING
        assert r.passed is False
        assert r.failing_row_count == 3


# ---------------------------------------------------------------------------
# check_required_columns
# ---------------------------------------------------------------------------
class TestCheckRequiredColumns:
    def test_all_present(self) -> None:
        df = pl.DataFrame({"a": [1], "b": [2], "c": [3]})
        r = check_required_columns(df, ["a", "b"])
        assert r.passed is True
        assert r.level == CheckLevel.CRITICAL

    def test_missing_columns(self) -> None:
        df = pl.DataFrame({"a": [1]})
        r = check_required_columns(df, ["a", "b", "c"])
        assert r.passed is False
        assert "b" in r.message
        assert "c" in r.message
        assert r.level == CheckLevel.CRITICAL


# ---------------------------------------------------------------------------
# check_no_nulls
# ---------------------------------------------------------------------------
class TestCheckNoNulls:
    def test_no_nulls(self) -> None:
        df = pl.DataFrame({"date": ["2024-01-01"], "rate": [1.1]})
        r = check_no_nulls(df, "date")
        assert r.passed is True

    def test_has_nulls(self) -> None:
        df = pl.DataFrame({"date": [None, "2024-01-01", None], "rate": [1.0, 1.1, 1.2]})
        r = check_no_nulls(df, "date")
        assert r.passed is False
        assert r.failing_row_count == 2

    def test_custom_level(self) -> None:
        df = pl.DataFrame({"val": [None]})
        r = check_no_nulls(df, "val", level=CheckLevel.WARNING)
        assert r.level == CheckLevel.WARNING


# ---------------------------------------------------------------------------
# check_positive_values
# ---------------------------------------------------------------------------
class TestCheckPositiveValues:
    def test_all_positive(self) -> None:
        df = pl.DataFrame({"rate": [1.1, 2.2, 0.5]})
        r = check_positive_values(df, "rate")
        assert r.passed is True

    def test_has_zero(self) -> None:
        df = pl.DataFrame({"rate": [1.1, 0.0, 2.2]})
        r = check_positive_values(df, "rate")
        assert r.passed is False
        assert r.failing_row_count == 1

    def test_has_negative(self) -> None:
        df = pl.DataFrame({"rate": [1.1, -0.5, 2.2]})
        r = check_positive_values(df, "rate")
        assert r.passed is False
        assert r.failing_row_count == 1


# ---------------------------------------------------------------------------
# check_duplicates
# ---------------------------------------------------------------------------
class TestCheckDuplicates:
    def test_no_duplicates(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-01-02"],
            "target_currency": ["USD", "USD"],
        })
        r = check_duplicates(df, ["date", "target_currency"])
        assert r.passed is True

    def test_has_duplicates(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-01-01", "2024-01-02"],
            "target_currency": ["USD", "USD", "GBP"],
        })
        r = check_duplicates(df, ["date", "target_currency"])
        assert r.passed is False
        assert r.failing_row_count == 2


# ---------------------------------------------------------------------------
# check_rate_range
# ---------------------------------------------------------------------------
class TestCheckRateRange:
    def test_within_range(self) -> None:
        df = pl.DataFrame({"rate": [1.1, 0.5, 50.0]})
        r = check_rate_range(df, "rate", min_val=0.0001, max_val=1000.0)
        assert r.passed is True

    def test_out_of_range(self) -> None:
        df = pl.DataFrame({"rate": [1.1, 0.00001, 5000.0, 2.0]})
        r = check_rate_range(df, "rate", min_val=0.0001, max_val=1000.0)
        assert r.passed is False
        assert r.failing_row_count == 2


# ---------------------------------------------------------------------------
# check_value_in_set
# ---------------------------------------------------------------------------
class TestCheckValueInSet:
    def test_all_valid(self) -> None:
        df = pl.DataFrame({"source": ["frankfurter", "ecb", "ecb"]})
        r = check_value_in_set(df, "source", {"frankfurter", "ecb"})
        assert r.passed is True

    def test_invalid_values(self) -> None:
        df = pl.DataFrame({"source": ["frankfurter", "unknown", "ecb"]})
        r = check_value_in_set(df, "source", {"frankfurter", "ecb"})
        assert r.passed is False
        assert r.failing_row_count == 1


# ---------------------------------------------------------------------------
# run_fx_checks
# ---------------------------------------------------------------------------
class TestRunFxChecks:
    def test_clean_data_passes(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-01-02"],
            "source": ["frankfurter", "frankfurter"],
            "base_currency": ["EUR", "EUR"],
            "target_currency": ["USD", "GBP"],
            "rate": [1.1, 0.85],
        })
        results = run_fx_checks(df)
        assert all(r.passed for r in results)
        assert not has_critical_failures(results)

    def test_null_date_critical(self) -> None:
        df = pl.DataFrame({
            "date": [None, "2024-01-02"],
            "source": ["frankfurter", "frankfurter"],
            "base_currency": ["EUR", "EUR"],
            "target_currency": ["USD", "GBP"],
            "rate": [1.1, 0.85],
        })
        results = run_fx_checks(df)
        assert has_critical_failures(results)

    def test_negative_rate_critical(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-01-02"],
            "source": ["frankfurter", "frankfurter"],
            "base_currency": ["EUR", "EUR"],
            "target_currency": ["USD", "GBP"],
            "rate": [-1.0, 0.85],
        })
        results = run_fx_checks(df)
        assert has_critical_failures(results)

    def test_duplicate_date_currency_warning(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-01-01"],
            "source": ["frankfurter", "frankfurter"],
            "base_currency": ["EUR", "EUR"],
            "target_currency": ["USD", "USD"],
            "rate": [1.1, 1.1],
        })
        results = run_fx_checks(df)
        dup_results = [r for r in results if r.check_name == "no_duplicate_date_target_currency"]
        assert len(dup_results) == 1
        assert dup_results[0].passed is False
        assert dup_results[0].level == CheckLevel.WARNING


# ---------------------------------------------------------------------------
# run_economic_checks
# ---------------------------------------------------------------------------
class TestRunEconomicChecks:
    def test_clean_data_passes(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-02-01"],
            "source": ["fred", "fred"],
            "series_id": ["UNRATE", "UNRATE"],
            "value": [3.7, 3.9],
        })
        results = run_economic_checks(df)
        assert all(r.passed for r in results)

    def test_null_value_critical(self) -> None:
        df = pl.DataFrame({
            "date": ["2024-01-01", "2024-02-01"],
            "source": ["fred", "fred"],
            "series_id": ["UNRATE", "UNRATE"],
            "value": [None, 3.9],
        })
        results = run_economic_checks(df)
        assert has_critical_failures(results)

    def test_null_date_critical(self) -> None:
        df = pl.DataFrame({
            "date": [None, "2024-02-01"],
            "source": ["fred", "fred"],
            "series_id": ["UNRATE", "UNRATE"],
            "value": [3.7, 3.9],
        })
        results = run_economic_checks(df)
        assert has_critical_failures(results)


# ---------------------------------------------------------------------------
# has_critical_failures / build_quality_report
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_has_critical_failures_true(self) -> None:
        results = [
            QualityResult("a", CheckLevel.WARNING, False, "warn", 1),
            QualityResult("b", CheckLevel.CRITICAL, False, "fail", 2),
        ]
        assert has_critical_failures(results) is True

    def test_has_critical_failures_false(self) -> None:
        results = [
            QualityResult("a", CheckLevel.WARNING, False, "warn", 1),
            QualityResult("b", CheckLevel.CRITICAL, True, "ok", 0),
        ]
        assert has_critical_failures(results) is False

    def test_build_quality_report(self) -> None:
        results = [
            QualityResult("check_a", CheckLevel.CRITICAL, True, "ok", 0),
            QualityResult("check_b", CheckLevel.WARNING, False, "bad", 5),
        ]
        report = build_quality_report(results, "test-key.json", "fx_rates")
        assert report["source_key"] == "test-key.json"
        assert report["domain"] == "fx_rates"
        assert report["overall_passed"] is False
        assert len(report["checks"]) == 2
        assert report["checks"][1]["passed"] is False
        assert report["checks"][1]["failing_row_count"] == 5

    def test_build_quality_report_all_pass(self) -> None:
        results = [
            QualityResult("check_a", CheckLevel.CRITICAL, True, "ok", 0),
        ]
        report = build_quality_report(results, "k", "fx_rates")
        assert report["overall_passed"] is True
