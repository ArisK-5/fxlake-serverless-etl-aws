"""Data quality check framework for the Iceberg writer Lambda.

Pure functions — no AWS dependencies. Each check receives a list of row
dicts and returns an immutable QualityResult. Domain runners compose checks
with per-source configs.
"""

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any


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

    def __post_init__(self) -> None:
        if self.passed and self.failing_row_count != 0:
            raise ValueError(
                f"Inconsistent QualityResult: passed=True but "
                f"failing_row_count={self.failing_row_count}"
            )
        if not self.passed and self.failing_row_count <= 0:
            raise ValueError(
                f"Inconsistent QualityResult: passed=False but "
                f"failing_row_count={self.failing_row_count}"
            )


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------
def check_required_columns(
    rows: list[dict[str, Any]],
    required: list[str],
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify all *required* columns exist as keys in every row."""
    actual = set(rows[0].keys()) if rows else set()
    missing = sorted(set(required) - actual)
    if missing:
        return QualityResult(
            check_name="required_columns",
            level=level,
            passed=False,
            message=f"Missing columns: {', '.join(missing)}",
            failing_row_count=len(rows),
        )
    return QualityResult(
        check_name="required_columns",
        level=level,
        passed=True,
        message="All required columns present",
        failing_row_count=0,
    )


def check_no_nulls(
    rows: list[dict[str, Any]],
    column: str,
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify *column* contains no None values."""
    null_count = sum(1 for row in rows if row.get(column) is None)
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
    rows: list[dict[str, Any]],
    column: str,
    level: CheckLevel = CheckLevel.CRITICAL,
) -> QualityResult:
    """Verify all values in *column* are strictly positive (> 0)."""
    bad_count = sum(1 for row in rows if row[column] <= 0)
    name = f"positive_{column}"
    if bad_count > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {bad_count} non-positive value(s)",
            failing_row_count=bad_count,
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' values are all positive",
        failing_row_count=0,
    )


def check_duplicates(
    rows: list[dict[str, Any]],
    columns: list[str],
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify no duplicate rows exist for the given column combination."""
    keys = [tuple(row[c] for c in columns) for row in rows]
    counts = Counter(keys)
    dup_count = sum(cnt for cnt in counts.values() if cnt > 1)
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
    rows: list[dict[str, Any]],
    column: str,
    min_val: float,
    max_val: float,
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify values in *column* fall within [min_val, max_val]."""
    bad_count = sum(1 for row in rows if row[column] < min_val or row[column] > max_val)
    name = f"range_{column}"
    if bad_count > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {bad_count} value(s) outside [{min_val}, {max_val}]",
            failing_row_count=bad_count,
        )
    return QualityResult(
        check_name=name,
        level=level,
        passed=True,
        message=f"Column '{column}' values within [{min_val}, {max_val}]",
        failing_row_count=0,
    )


def check_value_in_set(
    rows: list[dict[str, Any]],
    column: str,
    valid_values: set[str],
    level: CheckLevel = CheckLevel.WARNING,
) -> QualityResult:
    """Verify all values in *column* belong to *valid_values*."""
    bad_count = sum(1 for row in rows if row[column] not in valid_values)
    name = f"value_set_{column}"
    if bad_count > 0:
        return QualityResult(
            check_name=name,
            level=level,
            passed=False,
            message=f"Column '{column}' has {bad_count} value(s) not in {sorted(valid_values)}",
            failing_row_count=bad_count,
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


def run_fx_checks(rows: list[dict[str, Any]]) -> list[QualityResult]:
    """Run all quality checks for the FX rates domain."""
    return [
        check_required_columns(rows, _FX_REQUIRED),
        check_no_nulls(rows, "date"),
        check_no_nulls(rows, "rate"),
        check_positive_values(rows, "rate"),
        check_rate_range(rows, "rate", min_val=0.0001, max_val=1000.0),
        check_value_in_set(rows, "source", _FX_VALID_SOURCES),
        check_duplicates(rows, ["date", "target_currency"]),
    ]


def run_economic_checks(rows: list[dict[str, Any]]) -> list[QualityResult]:
    """Run all quality checks for the economic indicators domain."""
    return [
        check_required_columns(rows, _ECON_REQUIRED),
        check_no_nulls(rows, "date"),
        check_no_nulls(rows, "value"),
        check_duplicates(rows, ["date", "series_id"]),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def has_critical_failures(results: list[QualityResult]) -> bool:
    """Return True if any CRITICAL check failed."""
    return any(
        r.level == CheckLevel.CRITICAL and not r.passed for r in results
    )


def build_quality_report(
    results: list[QualityResult],
    source_key: str,
    domain: str,
) -> dict[str, Any]:
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
