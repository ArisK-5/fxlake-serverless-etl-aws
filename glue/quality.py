"""Data quality check framework for the Glue transform job.

Pure functions — no AWS dependencies. Each check receives a Polars DataFrame
and returns an immutable QualityResult. Domain runners compose checks with
per-source configs.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Set

import polars as pl


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
class CheckLevel(Enum):
    """Severity of a quality check failure."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


@dataclass(frozen=True)
class QualityResult:
    """Immutable result of a single quality check."""

    check_name: str
    level: CheckLevel
    passed: bool
    message: str
    failing_row_count: int


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------
def check_required_columns(
    df: pl.DataFrame,
    required: List[str],
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify all *required* columns exist in *df*."""
    actual = set(df.columns)
    missing = sorted(set(required) - actual)
    if missing:
        return QualityResult(
            check_name="required_columns",
            level=level,
            passed=False,
            message=f"Missing columns: {', '.join(missing)}",
            failing_row_count=len(df),
        )
    return QualityResult(
        check_name="required_columns",
        level=level,
        passed=True,
        message="All required columns present",
        failing_row_count=0,
    )


def check_no_nulls(
    df: pl.DataFrame,
    column: str,
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify *column* contains no null values."""
    null_count = df[column].null_count()
    name = f"no_nulls_{column}"
    if null_count > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {null_count} null(s)",
            failing_row_count=null_count,
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' has no nulls",
        failing_row_count=0,
    )


def check_positive_values(
    df: pl.DataFrame,
    column: str,
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify all values in *column* are strictly positive (> 0)."""
    bad = df.filter(pl.col(column) <= 0)
    name = f"positive_{column}"
    if len(bad) > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {len(bad)} non-positive value(s)",
            failing_row_count=len(bad),
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' values are all positive",
        failing_row_count=0,
    )


def check_duplicates(
    df: pl.DataFrame,
    columns: List[str],
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify no duplicate rows exist for the given column combination."""
    dup_mask = df.select(columns).is_duplicated()
    dup_count = dup_mask.sum()
    name = f"no_duplicate_{'_'.join(columns)}"
    if dup_count > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Found {dup_count} duplicate row(s) on {columns}",
            failing_row_count=dup_count,
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"No duplicates on {columns}",
        failing_row_count=0,
    )


def check_rate_range(
    df: pl.DataFrame,
    column: str,
    min_val: float,
    max_val: float,
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify values in *column* fall within [min_val, max_val]."""
    bad = df.filter((pl.col(column) < min_val) | (pl.col(column) > max_val))
    name = f"range_{column}"
    if len(bad) > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {len(bad)} value(s) outside [{min_val}, {max_val}]",
            failing_row_count=len(bad),
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' values within [{min_val}, {max_val}]",
        failing_row_count=0,
    )


def check_value_in_set(
    df: pl.DataFrame,
    column: str,
    valid_values: Set[str],
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify all values in *column* belong to *valid_values*."""
    bad = df.filter(~pl.col(column).is_in(list(valid_values)))
    name = f"value_set_{column}"
    if len(bad) > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {len(bad)} value(s) not in {sorted(valid_values)}",
            failing_row_count=len(bad),
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' values all valid",
        failing_row_count=0,
    )


# ---------------------------------------------------------------------------
# Domain runners
# ---------------------------------------------------------------------------
_FX_REQUIRED = ["date", "source", "base_currency", "target_currency", "rate"]
_FX_VALID_SOURCES = {"frankfurter", "ecb"}

_ECON_REQUIRED = ["date", "source", "series_id", "value"]


def run_fx_checks(df: pl.DataFrame) -> List[QualityResult]:
    """Run all quality checks for the FX rates domain."""
    return [
        check_required_columns(df, _FX_REQUIRED),
        check_no_nulls(df, "date"),
        check_no_nulls(df, "rate"),
        check_positive_values(df, "rate"),
        check_rate_range(df, "rate", min_val=0.0001, max_val=1000.0),
        check_value_in_set(df, "source", _FX_VALID_SOURCES),
        check_duplicates(df, ["date", "target_currency"]),
    ]


def run_economic_checks(df: pl.DataFrame) -> List[QualityResult]:
    """Run all quality checks for the economic indicators domain."""
    return [
        check_required_columns(df, _ECON_REQUIRED),
        check_no_nulls(df, "date"),
        check_no_nulls(df, "value"),
        check_duplicates(df, ["date", "series_id"]),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def has_critical_failures(results: List[QualityResult]) -> bool:
    """Return True if any CRITICAL check failed."""
    return any(
        r.level == CheckLevel.CRITICAL and not r.passed for r in results
    )


def build_quality_report(
    results: List[QualityResult],
    source_key: str,
    domain: str,
) -> Dict[str, Any]:
    """Build a JSON-serialisable quality report dictionary."""
    return {
        "source_key": source_key,
        "domain": domain,
        "overall_passed": all(r.passed for r in results),
        "checks": [
            {
                "check_name": r.check_name,
                "level": r.level.value,
                "passed": r.passed,
                "message": r.message,
                "failing_row_count": r.failing_row_count,
            }
            for r in results
        ],
    }
