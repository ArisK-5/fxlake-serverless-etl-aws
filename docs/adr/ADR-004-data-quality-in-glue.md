# ADR-004: Data quality checks in Glue vs separate Lambda

**Status:** Accepted
**Date:** 2026-03-28

## Context

FXLake needs data quality validation — checking for nulls, duplicates, out-of-range rates, missing columns — before writing to the processed layer. Two placement options:

1. **Separate Lambda** after Glue: Glue writes Parquet, then a validation Lambda reads the Parquet back and checks quality. Adds a Step Functions state and an extra S3 read/write cycle.
2. **Inside Glue transform:** Quality checks run on the in-memory DataFrame before writing Parquet. Single pass — data is loaded once, validated, then written (or quarantined).

## Decision

Run data quality checks **inside the Glue transform job** as a single-pass operation. Quality logic lives in a separate pure-Python module (`glue/quality.py`) loaded via `--extra-py-files`, keeping the check functions testable in isolation.

**Architecture:**

```
glue_transform.py          quality.py (pure functions)
  _enforce_quality() ──────► run_fx_checks(df) / run_economic_checks(df)
       │                          │
       │                    ◄─────┘ List[QualityResult]
       │
       ├─ CRITICAL failure → quarantine to S3 + raise ValueError
       └─ WARNING/pass     → write Parquet + quality report JSON
```

**Check levels:**
- `CRITICAL` — quarantines the entire DataFrame to the quarantine bucket, publishes `RecordsQuarantined` + `DataQualityChecksFailed` CloudWatch metrics, raises `ValueError` to fail the Step Functions execution
- `WARNING` — publishes `DataQualityChecksFailed` metric, logs the issue, continues processing

**Per-domain checks:**
- FX rates: required columns, no null date/rate, positive rate, rate range [0.0001, 1000], valid source set, no duplicate date+target_currency
- Economic indicators: required columns, no null date/value, no duplicate date+series_id

## Consequences

### Positive

- **Single pass** — data loaded from S3 once, validated in memory, written once. No extra S3 round-trip that a separate Lambda would require
- **Atomic quality gate** — if CRITICAL checks fail, no Parquet is written. The processed layer never contains data that failed validation
- **Pure-function testability** — `quality.py` has zero AWS dependencies. All 6 check functions + 2 domain runners are tested with plain Polars DataFrames (29 tests, 100% coverage)
- **Immutable results** — `QualityResult` is a `@dataclass(frozen=True)` with `__post_init__` invariant validation (rejects `passed=True` with `failing_row_count > 0`)
- **Observable** — quality reports written as JSON alongside each Parquet file; CloudWatch metrics + 11 alarms provide operational visibility
- **No extra infrastructure** — no additional Lambda, IAM role, or Step Functions state

### Negative

- **Coupled deployment** — changing a quality rule requires redeploying the Glue script (S3 object upload via Terraform). A separate Lambda could be deployed independently
- **Python 3.9 constraint** — Glue Python Shell runs Python 3.9; quality checks must be compatible (no 3.10+ syntax like `match`/`case`)
- **All-or-nothing per file** — CRITICAL failure quarantines the entire file, not individual bad rows. Partial acceptance would require row-level filtering (added complexity for marginal benefit given the small file sizes)
- **Glue job failure = pipeline failure** — a bug in quality.py can crash the entire Glue job. Mitigated by comprehensive test coverage (100%) and the `_enforce_quality` wrapper catching only expected paths

### Why Not AWS Glue Data Quality (native)

AWS Glue Data Quality is a managed service that runs DQDL rules on Glue ETL (Spark) jobs. It requires Spark, not Python Shell, and would force a switch to 2+ DPU (see ADR-001). The custom approach costs nothing extra and gives full control over check logic, quarantine behavior, and metric publishing.

### Why Not Great Expectations

Great Expectations is a powerful data quality framework but adds significant dependency weight (~100+ MB) and an opinionated project structure (checkpoints, data docs, stores). FXLake's 6 check functions in 67 lines of pure Python are simpler to maintain and deploy via `--extra-py-files` than a Great Expectations suite.
