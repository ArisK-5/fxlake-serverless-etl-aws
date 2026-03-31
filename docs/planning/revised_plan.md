# FXLake — Revised 10-Day Extension Plan

**Based on:** Forensic analysis of commit 115f056 (2026-03-28)
**Goal:** Extend from demo to production-grade multi-source data platform
**Target:** EU Data Engineer hiring signal

---

## Priority Ranking (Scored)

| Rank | Extension | Priority Score | Days |
|------|-----------|---------------|------|
| 1 | Incremental Processing + Partitioning | 6.4 | 1.5 |
| 2 | Data Quality Framework | 6.4 | 2 |
| 3 | Multi-Source Ingestion | 6.2 | 2 |
| 4 | Testing (pytest + moto) | 5.8 | 1.5 |
| 5 | CI/CD (GitHub Actions) | 5.8 | 1 |
| 6 | Step Function Error Handling | 5.2 | 0.5 |
| 7 | Terraform Hardening | 2.4 | 0.5 |
| 8 | Monitoring Enhancement | 2.2 | 1 |

---

## Pacing Note

Each "Day" below is a logical work unit of 5-8 hours, NOT a calendar day. At 2-3 hours/day, each "Day" spans 2-3 calendar sittings. Sessions within a Day are independent and can be done in separate sittings. Quality over speed — move on only when the current session's acceptance criteria are met.

---

## Day-by-Day Breakdown

### Day 1: Foundation — Testing Framework + Step Function Resilience

**Status:** ✅ COMPLETED (2026-03-30)
**Branch:** `day-01-testing-foundation`
**PRs:** #10 (→ fxlake-v2-production)

**Rationale:** Tests must exist BEFORE any refactoring. Step Function error handling is quick and prevents cascading failures during development.

#### Planned vs. Delivered

| Planned | Delivered | Notes |
|---------|-----------|-------|
| ~20 unit tests | 37 tests (10 ingestion, 12 validation, 15 transform) | Scope expanded by iterative PR reviews |
| 80%+ coverage (nice-to-have) | 96% coverage (ingestion 97%, validation 95%, transform 95%) | Exceeded target — reviews drove edge-case tests |
| Retry/Catch in Step Functions | Retry/Catch + service-specific errors + `ResultPath` + dynamic `ErrorPath`/`CausePath` | See D16-D20 in decision_log.md |
| No source code changes planned | Error handling hardened across all 3 source files | Unplanned — discovered through 3 rounds of code review |
| No type annotations planned | Type annotations added to all function signatures | Unplanned — reviewer recommendation |
| No dependency cleanup planned | Removed spurious `pandas` + `tree` from pyproject.toml (~150MB saved) | Unplanned — reviewer caught unused deps |

#### Key Differences Explained

1. **37 tests vs ~20:** Three rounds of PR review (`/pr-review-toolkit:review-pr`) surfaced missing edge cases (non-JSON API response, connection errors, missing `base` field, `ClientError` specificity, parametrized non-terminal Athena states). Each review round added tests.

2. **Error handling hardening (unplanned):** Reviews revealed issues in production code that should be fixed before building on top of it:
   - `fetch_exchange_rates()`: `json.JSONDecodeError` fell through all catches (not a `RequestException` subclass)
   - `save_to_s3()`: bare `except Exception` → narrowed to `ClientError`
   - `publish_custom_metric()`: `except ClientError` was too narrow for its purpose → widened back to `except Exception` (justified — see D16)
   - `lambda_handler()`: outer try/except removed then restored with structured context (see D17)

3. **Step Function error classes refined (unplanned):** Generic `States.TaskFailed` replaced with service-specific errors (`Athena.InternalServerException`, `Lambda.AWSLambdaException`, `Glue.ConcurrentRunsExceededException`). See D18-D19.

4. **`importlib.reload` test pattern:** The `TestOutputFormatGuard` test validates module-level config via `importlib.reload(glue_transform)` — must restore valid config after test to avoid affecting other tests.

#### Session 1A: Testing scaffold (morning)

**Approach:** NET-NEW
**Files created:**
- `tests/conftest.py` — shared fixtures (mocked S3, env vars)
- `tests/test_lambda_ingestion.py` — unit tests for ingestion Lambda
- `tests/test_lambda_validation.py` — unit tests for validation Lambda
- `tests/test_glue_transform.py` — Polars transform logic (testable without Glue runtime)
- `pyproject.toml` — add test dependencies

**Dependencies:** None
**Validation:** `uv run pytest tests/ -v`

**Claude Code prompt:**
```
Add a pytest test suite for the FXLake project. Create tests/ directory with:

1. tests/conftest.py - shared fixtures using moto to mock S3 and CloudWatch. Set up environment variables (RAW_BUCKET, START_DATE, END_DATE, BASE_CURRENCY, BASE_API_URL, METRIC_NAMESPACE, PIPELINE).

2. tests/test_lambda_ingestion.py - test lambda_ingestion_function:
   - Test fetch_exchange_rates() with mocked requests (use responses or unittest.mock.patch)
   - Test save_to_s3() with moto S3
   - Test lambda_handler() end-to-end with mocked API + moto S3
   - Test error handling (API timeout, S3 write failure)

3. tests/test_lambda_validation.py - test lambda_validation_function:
   - Test publish_custom_metric() with moto CloudWatch
   - Test lambda_handler() with succeeded query (mock athena.get_query_execution and get_query_results)
   - Test lambda_handler() with failed query state
   - Test missing QueryExecutionId raises ValueError

4. tests/test_glue_transform.py - test the Polars transformation logic ONLY (extract the transform logic into a testable function if needed):
   - Test flattening of nested {date: {currency: rate}} JSON
   - Test Parquet output
   - Test CSV output
   - Test empty input handling

Add to pyproject.toml: pytest, moto[s3,cloudwatch,athena], responses as dev dependencies via uv.

Do NOT modify the Glue script's getResolvedOptions usage — mock it in tests.

After completing, update:
- CLAUDE.md: update Tests section with test count, coverage, and any new patterns
- docs/planning/revised_plan.md: mark Session 1A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 1B: Step Function Retry/Catch (afternoon)

**Approach:** REFACTOR
**Files affected:** `terraform/step_function.tf`
**Dependencies:** None
**Validation:** `cd terraform && terraform validate`

**Claude Code prompt:**
```
Add Retry and Catch blocks to every state in terraform/step_function.tf:

1. Lambda-API-Ingestion:
   - Retry: Lambda.ServiceException, Lambda.TooManyRequestsException — interval 3s, max 2 attempts, backoff 2.0
   - Catch: States.ALL → new "Ingestion-Failed" Fail state with cause/error

2. Glue-JSON-to-Parquet:
   - Retry: States.TaskFailed — interval 10s, max 1 attempt
   - Catch: States.ALL → new "Transform-Failed" Fail state

3. Athena-Sample-Query:
   - Retry: States.TaskFailed — interval 5s, max 2 attempts, backoff 2.0
   - Catch: States.ALL → new "Query-Failed" Fail state

4. Lambda-Validation-Query:
   - Retry: same as ingestion Lambda
   - Catch: States.ALL → new "Validation-Failed" Fail state

Add the 4 Fail states at the end. Each should have descriptive Error and Cause strings.
Keep the inline jsonencode() pattern — do NOT extract to a separate ASL JSON file.

After completing, update:
- CLAUDE.md: update Step Functions section with error handling patterns
- docs/planning/revised_plan.md: mark Session 1B complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions (e.g., error class choices)
```

---

### Day 2: Incremental Processing + CI/CD

**Rationale:** Incremental processing is the #1 gap for "production-grade" credibility. CI/CD pairs naturally since we now have tests.

#### Session 2A: Incremental ingestion (morning)

**Status:** ✅ COMPLETED (2026-03-31)

**Approach:** REFACTOR existing Lambda + Terraform
**Files affected:**
- `lambda/lambda_ingestion_function.py` — dynamic date resolution
- `terraform/lambda.tf` — update env vars
- `terraform/variables.tf` — add DynamoDB table variable
- `terraform/step_function.tf` — Choice state after ingestion

**New files:**
- `terraform/dynamodb.tf` — state tracking table
- `terraform/iam.tf` — DynamoDB permissions for Lambda role

**Dependencies:** Tests from Day 1 (run before + after refactor)
**Validation:** `uv run pytest tests/test_lambda_ingestion.py -v` + `cd terraform && terraform validate`

#### Planned vs. Delivered

| Planned | Delivered | Notes |
|---------|-----------|-------|
| DynamoDB state table (partition: pipeline_id, sort: source) | ✅ Delivered | `fxlake-pipeline-state` via `terraform/dynamodb.tf` |
| Incremental fetch with DynamoDB read/write | ✅ Delivered | `get_last_processed_date` + `update_last_processed_date` |
| `no_new_data` early return | ✅ Delivered | Returns `{status: "no_new_data"}` when caught up |
| Static fallback without STATE_TABLE | ✅ Delivered | `_static_ingest()` path, tested explicitly |
| Choice state in Step Functions | ✅ Delivered | `Check-New-Data` Choice + `Pipeline-Already-Up-To-Date` Succeed state |
| IAM DynamoDB policy | ✅ Delivered | `fxlake-lambda-dynamodb` policy with `GetItem`/`PutItem` only |
| Update existing tests | ✅ Delivered | 8 new tests; existing tests updated for new function signatures |
| Test count: ~20 incremental tests | 18 ingestion tests total (8 new + 10 updated) | Kept focused on real failure modes |
| Coverage: maintain 96% | 97% total, ingestion Lambda 100% | Improved |

**Claude Code prompt:**
```
Convert the ingestion Lambda from static date range to incremental daily processing:

1. Create terraform/dynamodb.tf with a DynamoDB table "fxlake-pipeline-state" (partition key: "pipeline_id", sort key: "source"). Add attributes for last_processed_date (string).

2. Modify lambda/lambda_ingestion_function.py:
   - Read last_processed_date from DynamoDB (default to START_DATE env var if no entry)
   - Compute fetch range: last_processed_date+1 to today (or END_DATE if set and earlier)
   - After successful S3 write, update DynamoDB with new last_processed_date
   - Add new env var: STATE_TABLE (DynamoDB table name)
   - If already caught up (last_processed >= today), return early with status "no_new_data"
   - Keep backward compatibility: if STATE_TABLE is not set, fall back to static behavior

3. Update terraform/lambda.tf to pass STATE_TABLE env var.

4. Update terraform/iam.tf to grant Lambda DynamoDB read/write on the state table.

5. Update terraform/step_function.tf: add a Choice state after ingestion that checks for "no_new_data" and skips to End if nothing to process.

6. Update tests to cover the new incremental logic.

After completing, update:
- CLAUDE.md: update Architecture section with incremental processing details
- docs/planning/revised_plan.md: mark Session 2A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 2B: S3 partitioning + CI/CD (afternoon)

**Status:** ✅ COMPLETED (2026-03-31)

**Approach:** REFACTOR Glue + NET-NEW CI/CD
**Files affected:**
- `glue/glue_transform.py` — add partitioned output paths
- `terraform/athena.tf` — add partition projection
- `pyproject.toml` — add ruff + ruff config

**New files:**
- `.github/workflows/ci.yml` — lint + test on PR
- `.github/workflows/deploy.yml` — terraform plan on PR, apply on merge to main

**Dependencies:** Tests passing
**Validation:** `uv run pytest -v` + `cd terraform && terraform validate`

#### Planned vs. Delivered

| Planned | Delivered | Notes |
|---------|-----------|-------|
| Partitioned Glue output | ✅ Delivered | `year=YYYY/month=MM/day=DD/` Hive partitions, one file per date |
| Partition projection in Athena | ✅ Delivered | Integer type + `digits=2` for zero-padded paths, no MSCK REPAIR needed |
| `ci.yml` — lint + test on PR | ✅ Delivered | ruff + pytest jobs, both jobs must pass |
| `ci.yml` — terraform validate on PR | ✅ Delivered | init -backend=false + validate + fmt -check |
| `deploy.yml` — plan + apply with OIDC | ✅ Delivered | Plan artifact → manual-approval Apply via `production` environment |
| Ruff added as dev dep | ✅ Delivered | Configured in `pyproject.toml` with E/F/W/I rules |
| Ruff CI-clean code | Fixed 3 isort violations | Auto-fixed by `ruff --fix`; root cause: import ordering in test files |
| Secrets in `run:` — safe pattern | ✅ Used env: indirection | Secrets bound to job-level `env:`, referenced as `$VAR` in heredoc — not inline `${{ secrets.* }}` |

**Claude Code prompt (Part 1 — Partitioning):**
```
Add date-based S3 partitioning to the Glue transform:

1. Modify glue/glue_transform.py:
   - Output path: exchange_rates/year=YYYY/month=MM/day=DD/{filename}.parquet
   - Parse date from each row and group output by date
   - Each partition directory gets its own parquet file

2. Update terraform/athena.tf:
   - Add partition projection to the Glue catalog table:
     year (integer, 2020-2030), month (integer, 1-12), day (integer, 1-31)
   - Update storage_descriptor location
   - Set projection.enabled = true

3. Update tests/test_glue_transform.py for partitioned output.
```

**Claude Code prompt (Part 2 — CI/CD):**
```
Create GitHub Actions CI/CD pipeline:

1. .github/workflows/ci.yml (triggers on PR to main):
   - Job 1: Python lint + test
     - Setup Python 3.11, install uv
     - uv sync --dev
     - uv run ruff check .
     - uv run pytest tests/ -v
   - Job 2: Terraform validate
     - Setup Terraform
     - terraform init -backend=false
     - terraform validate
     - terraform fmt -check

2. .github/workflows/deploy.yml (triggers on push to main):
   - Job: terraform plan (always)
   - Job: terraform apply (only on main, requires manual approval via environment)
   - Use OIDC for AWS authentication (add placeholder for role ARN)

Add ruff to pyproject.toml dev dependencies.

After completing, update:
- CLAUDE.md: update with CI/CD section and S3 partitioning details
- docs/planning/revised_plan.md: mark Session 2B complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 2C: Fix EventBridge Daily Trigger (evening)

**Approach:** REFACTOR Terraform
**Files affected:** terraform/lambda.tf

**Validation:** `cd terraform` + `terraform validate` + `terraform plan` # Should show: aws_cloudwatch_event_target changed

**Claude Code prompt (Part 3 — EventBridge Trigger):**
```
Fix the EventBridge daily trigger to invoke Step Functions instead of Lambda directly:

1. Locate the aws_cloudwatch_event_target resource (currently named "invoke_lambda").

2. Update the target:
   OLD:
   resource "aws_cloudwatch_event_target" "invoke_lambda" {
     rule = aws_cloudwatch_event_rule.daily_trigger.name
     arn  = aws_lambda_function.api_ingest.arn
   }

   NEW:
   resource "aws_cloudwatch_event_target" "invoke_step_function" {
     rule     = aws_cloudwatch_event_rule.daily_trigger.name
     arn      = aws_sfn_state_machine.etl.arn
     role_arn = aws_iam_role.eventbridge_sfn_invoke_role.arn
   }

3. Create the IAM role for EventBridge to invoke Step Functions:
   In terraform/iam.tf (or create terraform/eventbridge_iam.tf):

   resource "aws_iam_role" "eventbridge_sfn_invoke_role" {
     name = "eventbridge-invoke-stepfunctions-role"
     assume_role_policy = jsonencode({
       Version = "2012-10-17"
       Statement = [{
         Action = "sts:AssumeRole"
         Effect = "Allow"
         Principal = { Service = "events.amazonaws.com" }
       }]
     })
   }

   resource "aws_iam_role_policy" "eventbridge_sfn_invoke_policy" {
     role = aws_iam_role.eventbridge_sfn_invoke_role.id
     policy = jsonencode({
       Version = "2012-10-17"
       Statement = [{
         Effect   = "Allow"
         Action   = "states:StartExecution"
         Resource = aws_sfn_state_machine.etl.arn
       }]
     })
   }

4. Rename the target resource from "invoke_lambda" to "invoke_step_function" for clarity.

5. Remove or repurpose aws_lambda_permission "allow_eventbridge" — EventBridge no longer invokes the Lambda directly.

6. Update any references to the old target name.

CRITICAL: This must be done BEFORE Session 3B (Parallel state), otherwise the daily trigger will still only run the old single Lambda.

After completing, update:
- CLAUDE.md: update Architecture section with EventBridge → Step Functions change
- docs/planning/revised_plan.md: mark Session 2C complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

---

### Day 3-4: Multi-Source Ingestion

**Rationale:** This is the marquee feature that transforms "single API demo" into "data platform."

#### Session 3A: Ingestion abstraction + second source (Day 3 morning)

**Approach:** REFACTOR + NET-NEW
**Files affected:**
- `lambda/lambda_ingestion_function.py` — extract common pattern

**New files:**
- `lambda/common/` — shared utilities (S3 writer, state tracker, base handler)
- `lambda/lambda_ecb_ingestion.py` — ECB Statistical Data Warehouse (free, no API key)
- `lambda/requirements.txt` — update

**Dependencies:** Incremental processing (Day 2)
**Validation:** `uv run pytest tests/ -v`

**Claude Code prompt:**
```
Create a multi-source ingestion architecture:

1. Create lambda/common/__init__.py and lambda/common/base.py with:
   - BaseIngestionHandler class:
     - __init__(self, source_name, raw_bucket, state_table)
     - abstract method: fetch_data(start_date, end_date) -> dict
     - concrete method: save_to_s3(data, filename) — current S3 logic
     - concrete method: get_last_processed() / update_last_processed() — DynamoDB logic
     - concrete method: run(event, context) — orchestration (check state → fetch → save → update)

2. Refactor lambda/lambda_ingestion_function.py to use BaseIngestionHandler:
   - class FrankfurterHandler(BaseIngestionHandler)
   - Override fetch_data() with existing API call logic
   - lambda_handler calls FrankfurterHandler().run(event, context)

3. Create lambda/lambda_ecb_ingestion.py:
   - class ECBHandler(BaseIngestionHandler)
   - Fetch from ECB Daily FX API (simplified): https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?format=jsondata
   - Parse simple JSON response (no SDMX complexity)
   - Output: s3://raw-bucket/ecb_rates_{START}_to_{END}.json

4. Update lambda/package_lambdas.sh to package all Lambda functions (including common/ module).

5. Update tests: test both handlers, test base class.

IMPORTANT: Keep the existing lambda_handler function signature unchanged for backward compatibility with Terraform.

After completing, update:
- CLAUDE.md: update Architecture section with multi-source ingestion details
- docs/planning/revised_plan.md: mark Session 3A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 3B: Terraform + Step Functions Parallel (Day 3 afternoon)

**Approach:** REFACTOR Terraform
**Files affected:**
- `terraform/lambda.tf` — add new Lambda function
- `terraform/step_function.tf` — add Parallel state
- `terraform/variables.tf` — add ECB-specific variables
- `terraform/iam.tf` — add permissions for new Lambda

**Dependencies:** New Lambda code from Session 3A
**Validation:** `cd terraform && terraform validate && terraform plan`

**Claude Code prompt:**
```
Add the ECB ingestion Lambda to Terraform and convert Step Functions to parallel ingestion:

1. Add to terraform/lambda.tf:
   - aws_lambda_function "ecb_ingest" — similar to api_ingest but with ECB-specific env vars
   - Package: lambda_ecb_ingestion.zip

2. Modify terraform/step_function.tf:
   - Replace "Lambda-API-Ingestion" with a Parallel state "Parallel-Ingestion" containing:
     - Branch 1: Lambda-FX-Ingestion (existing Frankfurter)
     - Branch 2: Lambda-ECB-Ingestion (new ECB)
   - Add ResultSelector to merge outputs
   - Parallel state flows into existing Glue step
   - Add Retry/Catch on the Parallel state

3. Update terraform/iam.tf:
   - Grant new Lambda same S3 + DynamoDB permissions
   - Grant Step Functions permission to invoke new Lambda

4. Update terraform/variables.tf with ECB-specific defaults.

After completing, update:
- CLAUDE.md: update Architecture section with Parallel state and new Lambda
- docs/planning/revised_plan.md: mark Session 3B complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 4A: Third source + testing (Day 4 morning)

**Claude Code prompt:**
```
Add a third data source — Alpha Vantage (stock/commodity prices) or FRED (Federal Reserve Economic Data, free API):

0a. BEFORE STARTING: Verify FRED_API_KEY is available (request at fred.stlouisfed.org if not).
0b. If FRED key unavailable, use World Bank API as fallback third source.

1. Create lambda/lambda_fred_ingestion.py:
   - class FREDHandler(BaseIngestionHandler)
   - Fetch from FRED API: https://api.stlouisfed.org/fred/series/observations
   - Series: GDP, UNRATE, CPIAUCSL (configurable via env var)
   - API key via environment variable (FRED_API_KEY)
   - Output: s3://raw-bucket/fred_{series}_{START}_to_{END}.json

2. Update terraform/ for the third Lambda + add to Parallel state.

3. Update package_lambdas.sh.

4. Write comprehensive tests for all three ingestion handlers.

5. Update Glue transform with a hybrid schema approach (two data domains):

   Domain 1 — FX Rates (Frankfurter + ECB):
   - Unified Athena table "fx_rates": {date, source, base_currency, target_currency, rate}
   - Detect source from filename prefix (exchange_rates_, ecb_)
   - Normalize both to common FX schema with "source" column
   - Output path: fx_rates/year=YYYY/month=MM/day=DD/

   Domain 2 — Economic Indicators (FRED):
   - Separate Athena table "economic_indicators": {date, source, series_id, value, unit}
   - Detect from filename prefix (fred_)
   - Own transform path — different data semantics (GDP in billions != FX rate)
   - Output path: economic_indicators/year=YYYY/month=MM/day=DD/

   Rationale: Forcing GDP/unemployment into an FX rate schema is data modeling malpractice.
   Adding a new FX source requires zero transform code. Adding a new data domain is a deliberate decision.

6. Update terraform/athena.tf: add second Glue catalog table for economic_indicators.

After completing, update:
- CLAUDE.md: update with third source, hybrid schema, and new Athena tables
- docs/planning/revised_plan.md: mark Session 4A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 4B: Glue multi-source transform (Day 4 afternoon)

**Validation:** Full test suite + terraform validate

---

### Day 5-6: Data Quality Framework

**Rationale:** This is the strongest "production engineering" signal. Demonstrates understanding of data reliability.

#### Session 5A: Quality checks + quarantine pattern (Day 5)

**Approach:** NET-NEW
**New files:**
- `lambda/common/quality.py` — data quality check framework
- `terraform/s3.tf` — add quarantine bucket
- `tests/test_data_quality.py`

**Claude Code prompt:**
```
Build a data quality framework integrated into the Glue transform:

1. Create lambda/common/quality.py (or glue/quality.py if better for Glue packaging):
   - DataQualityChecker class with methods:
     - check_not_null(df, columns) → returns QualityResult
     - check_positive(df, column) → returns QualityResult (for rates > 0)
     - check_date_format(df, column, fmt) → returns QualityResult
     - check_no_duplicates(df, columns) → returns QualityResult
     - check_row_count(df, min_rows, max_rows) → returns QualityResult
     - check_value_range(df, column, min_val, max_val) → returns QualityResult
   - QualityResult dataclass: passed (bool), check_name, failed_count, total_count, sample_failures
   - run_all_checks(df, config) → list[QualityResult]

2. Modify glue/glue_transform.py:
   - After creating DataFrame, run quality checks
   - If any CRITICAL check fails: write failed records to quarantine bucket, raise exception
   - If WARNING checks fail: log but continue, publish CloudWatch metric
   - Always write quality report JSON alongside the data

3. Add terraform/s3.tf: aws_s3_bucket "quarantine" with encryption.

4. Add quality check configuration per source:
   - FX rates: rate > 0, date not null, no duplicate (date, target_currency) pairs
   - ECB: similar checks adapted to ECB schema
   - FRED: value not null, date not null

5. Add custom CloudWatch metrics: DataQualityChecksFailed, RecordsQuarantined.

6. Write tests with known-bad data (nulls, negatives, duplicates).

After completing, update:
- CLAUDE.md: update with data quality framework section
- docs/planning/revised_plan.md: mark Session 5A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 6A: Quality dashboard + alerting (Day 6 morning)

**Claude Code prompt:**
```
Extend monitoring for data quality:

1. Update terraform/monitoring.tf:
   - Add CloudWatch alarm for DataQualityChecksFailed > 0
   - Add CloudWatch alarm for RecordsQuarantined > threshold
   - Add quality metrics to the dashboard (new row of widgets)

2. Create a quality report Lambda (optional, if time):
   - Reads quality report JSONs from processed bucket
   - Generates summary and publishes to SNS

3. Update the Athena sample query to be more meaningful:
   - Instead of SELECT * LIMIT 100, run a data freshness check:
     SELECT MAX(date) as latest_date, COUNT(*) as total_records FROM exchange_rates
   - Validation Lambda checks if latest_date is recent (within 2 days)

After completing, update:
- CLAUDE.md: update monitoring section with quality dashboard details
- docs/planning/revised_plan.md: mark Session 6A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

---

### Day 7-8: Polish + Terraform Hardening

#### Session 7A: Remote state backend + demonstration module (Day 7)

**Approach:** NET-NEW (no migration of existing resources)
**New files:**
- `terraform/backend.tf` — S3 + DynamoDB state backend
- `terraform/bootstrap/main.tf` — bootstraps the backend resources
- `terraform/modules/lambda_function/` — reusable Lambda module (main.tf, variables.tf, outputs.tf)

**Claude Code prompt:**
```
Add Terraform remote state and a reusable Lambda module:

1. Create terraform/backend.tf:
   - S3 backend with DynamoDB locking (commented out initially, with migration instructions)
   - Document the bootstrap process in a comment block

2. Create terraform/bootstrap/main.tf:
   - aws_s3_bucket for state (versioned, encrypted, no public access)
   - aws_dynamodb_table for locking (partition key: LockID)
   - Standalone — run once to create backend resources before migrating

3. Create terraform/modules/lambda_function/:
   - Reusable module that creates: aws_lambda_function, aws_cloudwatch_log_group, IAM role + policy attachment
   - Variables: function_name, handler, runtime, filename, env_vars (map), s3_bucket_arns, additional_policy_json
   - Outputs: function_arn, function_name, role_arn

4. Use the new module for ECB and FRED Lambdas (created in Day 3-4):
   - Refactor terraform/lambda.tf: keep existing Frankfurter Lambda as-is, replace ECB/FRED definitions with module calls
   - This demonstrates "I know how to write modules" without risking migration of 47 existing resources

5. Add terraform/versions.tf with pinned provider versions.

Do NOT migrate existing resources into modules — that requires moved blocks and is high-risk mid-sprint.
The existing 11 .tf flat files are already well-organized by service domain.

After completing, update:
- CLAUDE.md: update Terraform section with remote state and module details
- docs/planning/revised_plan.md: mark Session 7A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 7B: Structured logging + observability (Day 7 afternoon)

**Claude Code prompt:**
```
Add structured JSON logging and observability across all Lambda functions:

1. Create lambda/common/logging.py:
   - configure_logger(service_name) → returns logger with JSON formatter
   - Include: timestamp, level, service, request_id, extra fields
   - Compatible with CloudWatch Logs Insights queries

2. Refactor all Lambda handlers to use structured logging:
   - Replace f-string logs with structured key-value logs
   - Add execution timing (start/end/duration)
   - Add record counts to ingestion logs

3. Add X-Ray tracing:
   - terraform/lambda.tf: add tracing_config { mode = "Active" }
   - Add aws-xray-sdk to Lambda dependencies
   - Instrument boto3 calls

After completing, update:
- CLAUDE.md: update with structured logging and observability details
- docs/planning/revised_plan.md: mark Session 7B complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

---

### Day 8: Integration testing + documentation

#### Session 8A: Integration tests (Day 8 morning)

**Claude Code prompt:**
```
Create integration tests that validate the full pipeline logic locally:

1. tests/integration/test_pipeline_flow.py:
   - Use moto to mock ALL AWS services
   - Test: Ingestion → Transform → Validate flow
   - Verify S3 objects created at each stage
   - Verify DynamoDB state updated
   - Verify CloudWatch metrics published

2. tests/integration/test_multi_source.py:
   - Test parallel ingestion of all sources
   - Verify each source writes to correct S3 path
   - Verify Glue handles multiple input schemas

3. Add Makefile target: make test-integration

After completing, update:
- CLAUDE.md: update Tests section with integration test details
- docs/planning/revised_plan.md: mark Session 8A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

---

### Day 9: Advanced Features

#### Session 9A: Backfill capability (Day 9 morning)

**Claude Code prompt:**
```
Add backfill mode to the pipeline:

1. Modify Step Function to accept input parameters:
   - { "mode": "backfill", "start_date": "2023-01-01", "end_date": "2023-12-31" }
   - { "mode": "incremental" } — default, uses DynamoDB state
   - Pass mode/dates through to Lambda via Step Function parameters

2. Modify Lambda handlers to respect mode parameter from event:
   - "backfill": use provided dates, ignore DynamoDB state
   - "incremental": use DynamoDB state (existing behavior)

3. Add Makefile target: make backfill START=2023-01-01 END=2023-12-31
   - Calls: aws stepfunctions start-execution with backfill input

4. Write tests for backfill mode.

After completing, update:
- CLAUDE.md: update with backfill capability details
- docs/planning/revised_plan.md: mark Session 9A complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

#### Session 9B: Architecture Decision Records (Day 9 afternoon)

**Claude Code prompt:**
```
Create docs/adr/ directory with Architecture Decision Records:

1. ADR-001: Use Polars over PySpark for transformation
   - Context: Glue Python Shell vs Spark
   - Decision: Polars for cost (0.0625 DPU) and performance
   - Consequences: Limited to single-node processing

2. ADR-002: DynamoDB for pipeline state tracking
   - Context: SSM Parameter Store vs DynamoDB vs S3 marker files
   - Decision: DynamoDB for atomic updates and query flexibility

3. ADR-003: Parallel ingestion with Step Functions
   - Context: Sequential vs parallel source ingestion
   - Decision: Parallel state for independent sources

4. ADR-004: Data quality checks in Glue vs separate Lambda
   - Context: Where to validate data
   - Decision: In Glue transform for single-pass efficiency

Use the standard ADR template (Title, Status, Context, Decision, Consequences).

After completing, update:
- CLAUDE.md: add ADR section with links
- docs/planning/revised_plan.md: mark Session 9B complete with planned-vs-delivered
- docs/planning/decision_log.md: add any new decisions made during implementation
```

---

### Day 10: Final Polish + Demo Readiness

#### Session 10A: README + diagram update (Day 10 morning)

**Claude Code prompt:**
```
Update README.md and architecture diagrams to reflect all changes:

1. Update assets/cloud-architecture.py to show:
   - Multiple data sources (Frankfurter, ECB, FRED)
   - Parallel ingestion in Step Functions
   - DynamoDB state table
   - Quarantine bucket
   - Updated data flow

2. Update README.md:
   - Architecture section with new diagram
   - Multi-source ingestion documentation
   - Data quality framework description
   - Setup instructions for FRED API key
   - How to run backfill
   - CI/CD pipeline description
   - Link to ADRs

3. Regenerate diagrams: uv run assets/cloud-architecture.py

After completing, update:
- CLAUDE.md: final review — ensure all sections reflect current state
- docs/planning/revised_plan.md: mark Session 10A complete, update Day 10 checklist
- docs/planning/decision_log.md: add any final decisions
```

#### Session 10B: Final validation (Day 10 afternoon)

**Checklist:**
- [ ] `uv run pytest tests/ -v` — all tests pass
- [ ] `uv run ruff check .` — no lint errors
- [ ] `cd terraform && terraform validate` — valid
- [ ] `cd terraform && terraform fmt -check` — formatted
- [ ] `cd terraform && terraform plan` — clean plan
- [ ] GitHub Actions CI passes on PR
- [ ] README accurately reflects current state
- [ ] Architecture diagram is up-to-date
- [ ] All ADRs written
- [ ] No hardcoded credentials or secrets
- [ ] .gitignore covers terraform.tfvars, .terraform/, *.zip

---

## Acceptance Criteria (Per Day)

| Day | Must Have | Nice to Have |
|-----|----------|--------------|
| 1 | pytest suite passes, Retry/Catch in SFN | 80%+ coverage |
| 2 | Incremental processing works, CI pipeline runs | S3 partition projection |
| 3 | 2nd source ingests, Parallel state works | Base class is clean/extensible |
| 4 | 3rd source ingests, Glue handles multi-schema | Source auto-detection |
| 5 | Quality checks run, quarantine bucket works | Quality report JSON |
| 6 | Quality alarms fire on bad data | Dashboard has quality row |
| 7 | Remote state backend + Lambda module, structured logging | X-Ray tracing |
| 8 | Integration tests pass | Full pipeline mock test |
| 9 | Backfill mode works, ADRs written | Makefile backfill target |
| 10 | README + diagrams updated, all tests green | Demo script/recording |
