# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FXLake is a serverless ETL pipeline on AWS that fetches daily foreign exchange rates from multiple sources (Frankfurter API, ECB Statistics Data Warehouse), transforms them with Polars, and makes them queryable via Athena. Infrastructure is managed entirely with Terraform.

## Commands

### Dependency Management

Uses `uv` (not pip) for Python dependencies:
```bash
uv sync          # Install dependencies
uv add <pkg>     # Add a new dependency
```

Lambda functions have their own `lambda/requirements.txt` (managed separately from pyproject.toml).

### Lambda Packaging

```bash
make package     # Runs lambda/package_lambdas.sh — produces .zip files for all Lambda functions
```

### Infrastructure (Terraform)

```bash
make init        # terraform init
make plan        # terraform plan
make deploy      # terraform apply -auto-approve
make destroy     # terraform destroy -auto-approve
make clean       # Remove .zip files and all Terraform local state/cache
```

All Terraform commands run from the `terraform/` directory. Configuration goes in `terraform/terraform.tfvars` (copy from `terraform/terraform.tfvars.example`).

### Backfill

```bash
make backfill START=2023-01-01 END=2023-12-31   # Historical re-ingestion (does not affect DynamoDB state)
```

Starts a Step Functions execution with `mode: "backfill"` input. Lambdas use the provided dates directly and skip DynamoDB state management.

### Diagram Generation

Architecture and workflow diagrams are generated via Python scripts:
```bash
uv run assets/cloud-architecture.py
uv run assets/dev-workflow.py
```

## Architecture

The pipeline is orchestrated by **Step Functions** and triggered daily by **EventBridge**, which invokes the Step Functions state machine directly (not the Lambda):

1. **Parallel (Parallel-Ingestion)** — runs Frankfurter, ECB, and FRED ingestion concurrently (3 branches). Each branch reads `last_processed_date` from DynamoDB, computes incremental fetch range, saves raw JSON to S3. Returns `status: "no_new_data"` if already caught up. Output shaped by `ResultSelector` to `$.parallel_results.fx`, `$.parallel_results.ecb`, and `$.parallel_results.fred`. **Does not update DynamoDB** — deferred to steps 4–6.
2. **Choice (Check-New-Data)** — routes to `Pipeline-Already-Up-To-Date` only if **all three** sources returned `no_new_data`; otherwise continues to Glue.
3. **Glue Job (Python Shell)** — reads raw JSON from S3, routes by filename prefix: `fred_*` → `economic_indicators/` domain, all others → `fx_rates/` domain. Runs data quality checks (via `glue/quality.py`): CRITICAL failures → quarantine to dedicated S3 bucket + raise; WARNING failures → log + CloudWatch metric. Writes quality report JSON for every file. Writes Parquet/CSV partitioned by date.
4. **Lambda (Update-FX-State)** — commits Frankfurter `last_processed_date` to DynamoDB **only after Glue succeeds**. Calls the `api_ingest` Lambda with `{"action": "update_state", "end_date": "$.parallel_results.fx.Payload.end_date"}`.
5. **Lambda (Update-ECB-State)** — commits ECB `last_processed_date` to DynamoDB after FX state is committed. Calls the `ecb_ingest` Lambda with `{"action": "update_state", "end_date": "$.parallel_results.ecb.Payload.end_date"}`.
6. **Lambda (Update-FRED-State)** — commits FRED `last_processed_date` to DynamoDB after ECB state is committed. Calls the `fred_ingest` Lambda with `{"action": "update_state", "end_date": "$.parallel_results.fred.Payload.end_date"}`.
7. **Athena** — runs a data freshness query (`SELECT MAX(date) AS latest_date, COUNT(*) AS total_records FROM fx_rates`) via Glue Data Catalog; results go to a dedicated S3 bucket with 1-day lifecycle TTL
8. **Lambda (Validation)** — parses `latest_date` and `total_records` from Athena results, checks if `latest_date` is within 2 days (freshness threshold), publishes `EmptyQueryResults` and `StaleFXData` CloudWatch metrics

### Multi-Source Ingestion

All ingestion Lambdas share a common base class (`lambda/common/base.py`):

| Lambda | Source | Handler class | File naming |
|--------|--------|---------------|-------------|
| `lambda_ingestion_function.py` | Frankfurter API | `FrankfurterHandler` | `exchange_rates_{BASE}_{START}_to_{END}.json` |
| `lambda_ecb_ingestion.py` | ECB SDW SDMX-JSON API | `ECBHandler` | `ecb_rates_{START}_to_{END}.json` |
| `lambda_fred_ingestion.py` | FRED (Federal Reserve Economic Data) | `FREDHandler` | `fred_{series}_{START}_to_{END}.json` |

FRED-specific: fetches `series/observations` for a single configurable series (default `UNRATE`). Drops `"."` sentinel values (missing/unreleased data) silently; raises `ValueError` if the entire response is missing values.

`BaseIngestionHandler` (abstract) provides:
- `save_to_s3(data, filename)` — S3 write with `source` metadata
- `get_last_processed()` / `update_last_processed(date)` — DynamoDB state (allowlist-guarded fallback)
- `run(event, context)` — routes `update_state` action, backfill mode, incremental mode, or static mode
- `_handle_update_state()` / `_handle_backfill()` / `_incremental_ingest()` / `_static_ingest()` — mode dispatchers
- `_perform_ingest(start_date, end_date, mode)` — unified fetch → save → log → return workflow for all ingest modes

Subclasses implement `fetch_data(start, end)` and `make_filename(start, end)`.

ECB response parsing: SDMX-JSON format (`dataSets[0].series["FREQ:CCY:..."]`) is normalised into `{"base": "EUR", "source": "ecb", "rates": {date: {ccy: rate}}}`.

### Incremental Processing

Each handler supports two modes controlled by the `STATE_TABLE` env var. DynamoDB state is keyed by `(pipeline_id="fxlake", source=<source_name>)` — each source has independent state.

- **Incremental mode** (`STATE_TABLE` set): reads `last_processed_date` from DynamoDB, defaults to `START_DATE` on first run. Fetches `last_processed_date+1` to `min(today, END_DATE)`. Returns `end_date` in payload for Step Functions to pass to the `Lambda-Update-State` step.
- **Backfill mode** (`event.mode == "backfill"`): uses `start_date` and `end_date` from the event payload directly. Does NOT read or write DynamoDB state — safe for historical re-ingestion without corrupting the incremental watermark. Triggered via `make backfill START=... END=...` or manual Step Functions execution with `{"mode": "backfill", "start_date": "...", "end_date": "..."}`.
- **Static fallback** (`STATE_TABLE` not set): fetches the full `START_DATE..END_DATE` range (used for testing).

### Step Functions Error Handling

Every state has Retry and Catch blocks:
- **Lambda states**: retry on `Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.TooManyRequestsException`
- **Glue state**: retry on `Glue.ConcurrentRunsExceededException`, `States.HeartbeatTimeout`
- **Athena state**: retry on `Athena.InternalServerException`, `Athena.TooManyRequestsException`
- **All Catch blocks**: `ResultPath = "$.errorInfo"` preserves the actual error; Fail states use `ErrorPath`/`CausePath` to surface the real cause in execution history

### Data Quality Framework

`glue/quality.py` — pure check functions (no AWS deps), imported by `glue_transform.py` via `--extra-py-files`.

**Architecture:**
- `CheckLevel` enum: `CRITICAL` (quarantine + raise) vs `WARNING` (log + metric)
- `QualityResult` frozen dataclass: immutable check result with `__post_init__` invariant validation (rejects `passed=True` with `failing_row_count>0` and vice versa)
- 6 check functions: `check_required_columns`, `check_no_nulls`, `check_positive_values`, `check_duplicates`, `check_rate_range`, `check_value_in_set`
- 2 domain runners: `run_fx_checks(df)`, `run_economic_checks(df)`
- Helpers: `has_critical_failures(results)`, `build_quality_report(results, key, domain)`

**Per-domain checks:**
- FX rates: required columns, no null date/rate, positive rate, rate range [0.0001, 1000], valid source set, no duplicate date+target_currency (WARNING)
- Economic indicators: required columns, no null date/value, no duplicate date+series_id (WARNING)

**Integration in `glue_transform.py`:**
- `_enforce_quality(df, key, domain, run_checks_fn)` — called after DataFrame creation, before partitioning
- Always writes quality report JSON to `{domain}/quality_reports/{stem}_quality.json`
- CRITICAL failure: quarantines full DataFrame to quarantine bucket (`{domain}/quarantine/{stem}.json`), publishes `RecordsQuarantined` + `DataQualityChecksFailed` metrics, raises `ValueError`
- WARNING failure: publishes `DataQualityChecksFailed` metric, continues processing

**CloudWatch metrics:** `{metric_namespace_prefix}/Quality` namespace, `Domain` dimension.

### Key Terraform Files

| File | What it defines |
|------|----------------|
| `step_function.tf` | ASL definition for 8-stage orchestration: Parallel-Ingestion (3 branches) → Choice → Glue → Update-FX-State → Update-ECB-State → Update-FRED-State → Athena → Validation, with Retry/Catch + 7 Fail states + Succeed state. `ResultSelector` shapes Parallel output to named keys (`fx`, `ecb`, `fred`); `ResultPath` preserves state across all stages. |
| `dynamodb.tf` | `fxlake-pipeline-state` table for incremental processing state (partition: `pipeline_id`, sort: `source`) |
| `lambda.tf` | Frankfurter + validation Lambdas (inline), ECB + FRED Lambdas (via `modules/lambda_function`), EventBridge rule/target (→ Step Functions) |
| `glue.tf` | Glue Python Shell job (Polars, pyarrow deps) + quality.py S3 upload via `--extra-py-files` |
| `athena.tf` | Athena database, table schema, and results bucket config |
| `iam.tf` | All IAM roles/policies (least-privilege per service) |
| `monitoring.tf` | 11 CloudWatch alarms (incl. quality, quarantine, stale data) + dashboard with quality metrics row |
| `s3.tf` | 5 S3 buckets (raw, processed, athena_results, cloudtrail_logs, quarantine) + quarantine public access block + Athena results 1-day lifecycle |
| `security.tf` | S3 AES-256 encryption + CloudTrail multi-region trail |
| `variables.tf` | All configurable inputs (region, bucket names, date range, currency, output format) |
| `versions.tf` | Pinned Terraform version (`>= 1.5, < 2.0`) and AWS provider version (`~> 5.0`) |
| `backend.tf` | Remote state backend config (S3 + DynamoDB locking) — commented out until bootstrap is run |
| `bootstrap/main.tf` | Standalone config to create state bucket (versioned, KMS-encrypted) + lock table — run once before migrating |
| `modules/lambda_function/` | Reusable module: Lambda function + dedicated IAM role + CloudWatch log group. Used by ECB and FRED Lambdas |

### Runtime Environments

- Lambda functions: Python 3.12
- Glue Python Shell job: Python 3.9 (Polars 0.18.8 + pyarrow)
- Local dev / diagrams: Python 3.11 (see `.python-version`)

### S3 Bucket Layout

- **Raw:** `exchange_rates_{BASE}_{START}_to_{END}.json`, `ecb_rates_{START}_to_{END}.json`, `fred_{series}_{START}_to_{END}.json`
- **Processed — FX rates:** `fx_rates/year=YYYY/month=MM/day=DD/{stem}.parquet` — schema: `{date, source, base_currency, target_currency, rate}`
- **Processed — Economic indicators:** `economic_indicators/year=YYYY/month=MM/day=DD/{stem}.parquet` — schema: `{date, source, series_id, value}`
- **Athena results:** `results/` (1-day TTL)
- **Quarantine:** `{domain}/quarantine/{stem}.json` — records that failed CRITICAL quality checks
- **Quality reports:** `{domain}/quality_reports/{stem}_quality.json` (in processed bucket)
- **CloudTrail logs:** `AWSLogs/{account-id}/...`

Glue routes files by filename prefix (`fred_*` → economic domain, all others → FX domain) and writes one Parquet file per date. Athena uses **partition projection** on both catalog tables (`fx_rates` and `economic_indicators`) to resolve partitions without `MSCK REPAIR TABLE`.

### Structured Logging & Observability

All Lambda functions emit **structured JSON logs** (one JSON object per line), compatible with CloudWatch Logs Insights.

**Core module** — `lambda/common/logging.py`:
- `_JSONFormatter(service)` — formats each `LogRecord` as JSON: `timestamp`, `level`, `service`, `message`, plus any `extra={}` fields. `request_id` is included only when `inject_request_id()` has been called
- `RequestIdFilter(request_id)` — attaches the Lambda `aws_request_id` to every log record via `logging.Filter`
- `configure_logger(service)` — configures the root logger with JSON formatting; idempotent (replaces formatter on Lambda's pre-installed handler)
- `inject_request_id(logger, context)` — extracts request ID from Lambda context; handles warm starts by removing stale filters
- `Timer` — context manager measuring monotonic time via `time.monotonic_ns()`; exposes `duration_ms` property

**Logging pattern across all handlers:**
```python
logger = configure_logger("frankfurter")
logger.info("Ingestion complete", extra={"records": 42, "key": "exchange_rates_..."})
```

**CloudWatch Logs Insights query:**
```
fields @timestamp, service, message, records
| filter level = "ERROR"
| sort @timestamp desc
```

**X-Ray tracing** — all Lambda functions have `tracing_config { mode = "Active" }` in Terraform. The `aws-xray-sdk` (`patch_all()`) instruments boto3 and requests calls. Activation is gated on `AWS_XRAY_DAEMON_ADDRESS` (present in Lambda runtime, absent locally/in tests). IAM: `AWSXRayDaemonWriteAccess` managed policy on all Lambda roles.

## Error Handling Patterns

All source files follow these conventions:

- **Module-level config**: `os.environ[]` (not `os.getenv`) for required vars — fails fast at cold start if missing. `os.getenv()` for optional vars (e.g., `STATE_TABLE`)
- **Exception catches are type-specific**: `ClientError` for AWS SDK errors, `Timeout`/`HTTPError`/`ConnectionError` for HTTP, `json.JSONDecodeError`/`KeyError`/`ValueError` for data parsing
- **All catches re-raise** after logging — no silent swallowing (except `publish_custom_metric` which catches `Exception` because metric failure must not abort validation)
- **All error logs include context**: bucket names, filenames, API URLs, query IDs, error codes
- **Type annotations** on all function signatures

## Tests

Tests live in `tests/` and use pytest + moto v5 + responses. 247 tests (215 unit + 32 integration), 97% coverage.

```bash
make test                # Run unit tests only (ignores tests/integration/)
make test-integration    # Run integration tests only (-m integration)
make test-all            # Run all tests with coverage
uv run pytest tests/test_lambda_ingestion.py -v      # Single file
```

### Test Setup (conftest.py)

- `awsglue.utils` is mocked in `conftest.py` via `sys.modules` before any import of `glue_transform` — required because `getResolvedOptions` runs at module level
- Module-level env vars (`RAW_BUCKET`, `START_DATE`, etc.) are set via `os.environ.setdefault` before Lambda modules are imported
- `s3_mock` fixture activates `mock_aws()` and creates both S3 buckets (`test-raw-bucket`, `test-processed-bucket`)
- `aws_mock` fixture activates `mock_aws()` with S3 buckets + DynamoDB `test-state-table`; used by incremental ingestion tests
- Incremental tests use `monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)` — handlers read `STATE_TABLE` from env in `__init__`, so moto intercepts the `boto3.client("dynamodb")` call automatically

### Test Coverage

| File | Coverage |
|------|----------|
| `lambda/common/logging.py` | 95% |
| `lambda/common/base.py` | 96% |
| `lambda/lambda_ingestion_function.py` | 100% |
| `lambda/lambda_ecb_ingestion.py` | 100% |
| `lambda/lambda_fred_ingestion.py` | 100% |
| `lambda/lambda_validation_function.py` | 98% |
| `glue/glue_transform.py` | 93% (uncovered: generic `except Exception` fallthrough lines, `if __name__` guard, metric publish warning) |
| `glue/quality.py` | 100% |

**Overall: 97%**

### Integration Tests

Integration tests live in `tests/integration/` and exercise the full pipeline locally using moto + responses mocks. They are marked with `@pytest.mark.integration` (registered in `pyproject.toml`).

| File | What it covers |
|------|---------------|
| `test_pipeline_flow.py` | End-to-end: Ingestion → Transform → Validate for each source; DynamoDB state management (incremental + update_state); CloudWatch metrics; saga pattern; CRITICAL quality failure + quarantine; API HTTP 500 propagation; Glue failure saga rollback; validation Athena errors; backfill pipeline (25 tests) |
| `test_multi_source.py` | All 3 sources ingested in parallel; correct S3 path prefixes; JSON structure per source; S3 metadata tags; Glue routes to correct domains; distinct FX vs economic schemas; quality reports per domain; Hive partition paths (7 tests) |

**Key patterns:**
- Each test file has its own `mock_aws()` fixture creating S3 buckets, DynamoDB table, and CloudWatch/Athena clients
- `responses.activate` intercepts all HTTP calls (Frankfurter, ECB SDMX, FRED)
- Incremental mode tested via `monkeypatch.setenv("STATE_TABLE", ...)` — first run defaults to `START_DATE`, subsequent runs read DynamoDB state
- Validation tests patch `athena_client.get_query_results` to simulate Athena output without running queries

### Test File Organisation

| File | What it covers |
|------|---------------|
| `test_base_handler.py` | `BaseIngestionHandler` — save_to_s3, DynamoDB state, orchestration, saga pattern, backfill validation, _perform_ingest (55 tests) |
| `test_lambda_ingestion.py` | `FrankfurterHandler` — API calls, filename, integration via `lambda_handler` (19 tests) |
| `test_lambda_ecb_ingestion.py` | `ECBHandler` — SDMX parsing, API calls, integration (20 tests) |
| `test_lambda_fred_ingestion.py` | `FREDHandler` — parse/sentinel drop, fetch, filename, static/incremental `lambda_handler` (23 tests) |
| `test_glue_transform.py` | Glue hybrid transform — FX routes, ECB source detection, FRED economic domain, quality integration (35 tests) |
| `test_data_quality.py` | Pure quality checks — each check function, domain runners, report builder, invariant validation (29 tests) |
| `test_lambda_validation.py` | Validation Lambda — freshness check, staleness metric, empty results, malformed rows (16 tests) |
| `test_structured_logging.py` | Structured logging — JSON formatter, request ID filter, configure_logger, inject_request_id, Timer (18 tests) |
| `integration/test_pipeline_flow.py` | Full pipeline flow — ingestion → transform → validate, DynamoDB state, saga pattern, CRITICAL quality + quarantine, API 500 errors, Glue failure rollback, validation Athena errors, backfill pipeline (25 tests) |
| `integration/test_multi_source.py` | Multi-source parallel ingestion, Glue schema routing, quality reports (7 tests) |

## CI/CD

Two GitHub Actions workflows in `.github/workflows/`:

| Workflow | Trigger | Jobs |
|----------|---------|------|
| `ci.yml` | PR → `main` or `fxlake-v2-production` | `python-lint-test` (ruff + pytest), `terraform-validate` (init + validate + fmt check) |
| `deploy.yml` | Push → `main` or `fxlake-v2-production` | `terraform-plan` (always, uploads `tfplan` artifact with 1-day retention), `terraform-apply` (downloads artifact, manual approval via `production` environment — applies the exact plan from the plan job, not a fresh plan) |

### AWS Authentication (OIDC)

The deploy workflow uses OIDC — no long-lived AWS keys stored in GitHub. Required setup:
1. Create an IAM role with `sts:AssumeRoleWithWebIdentity` trust for `token.actions.githubusercontent.com`
2. Store the role ARN as `AWS_ROLE_ARN` in repo secrets (Settings → Secrets → Actions)

### Terraform Variables in CI

Each `variables.tf` input that has no default must be stored as a GitHub secret prefixed with `TF_`. The deploy workflow writes them to `terraform.tfvars` at runtime via environment variables (never interpolated inline in `run:` steps — security best practice).

Required secrets: `TF_RAW_BUCKET_NAME`, `TF_PROCESSED_BUCKET_NAME`, `TF_ATHENA_RESULTS_BUCKET_NAME`, `TF_CLOUDTRAIL_LOGS_BUCKET_NAME`, `TF_QUARANTINE_BUCKET_NAME`, `TF_SNS_EMAIL_ADDRESS`, `FRED_API_KEY`.

### Linting

Ruff config is in `pyproject.toml` (`[tool.ruff]`). Rules: E, F, W, I (PEP 8 + imports).
`tests/conftest.py` suppresses E402 (intentional late imports for `sys.modules` patching).

```bash
uv run ruff check .        # lint
uv run ruff check . --fix  # auto-fix
```

## Architecture Decision Records

ADRs live in `docs/adr/` and document the key architectural choices with full context, consequences, and alternatives considered:

| ADR | Decision | Key trade-off |
|-----|----------|---------------|
| [ADR-001](docs/adr/ADR-001-polars-over-pyspark.md) | Polars over PySpark | 32x cost reduction (0.0625 DPU) vs single-node ceiling |
| [ADR-002](docs/adr/ADR-002-dynamodb-for-pipeline-state.md) | DynamoDB for pipeline state | Atomic writes + composite key vs overkill for 3 records |
| [ADR-003](docs/adr/ADR-003-parallel-ingestion-step-functions.md) | Parallel ingestion via Step Functions | 3x faster ingestion vs all-or-nothing failure mode |
| [ADR-004](docs/adr/ADR-004-data-quality-in-glue.md) | Data quality checks in Glue | Single-pass efficiency vs coupled deployment |

## Planning

- `docs/planning/revised_plan.md` — 10-day extension plan with session prompts
- `docs/planning/decision_log.md` — architectural decisions and trade-offs
