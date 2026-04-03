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

### Diagram Generation

Architecture and workflow diagrams are generated via Python scripts:
```bash
uv run assets/cloud-architecture.py
uv run assets/dev-workflow.py
```

## Architecture

The pipeline is orchestrated by **Step Functions** and triggered daily by **EventBridge**, which invokes the Step Functions state machine directly (not the Lambda):

1. **Parallel (Parallel-Ingestion)** — runs Frankfurter and ECB ingestion concurrently. Each branch reads `last_processed_date` from DynamoDB, computes incremental fetch range, saves raw JSON to S3. Returns `status: "no_new_data"` if already caught up. Output shaped by `ResultSelector` to `$.parallel_results.fx` and `$.parallel_results.ecb`. **Does not update DynamoDB** — deferred to steps 4–5.
2. **Choice (Check-New-Data)** — routes to `Pipeline-Already-Up-To-Date` only if **both** sources returned `no_new_data`; otherwise continues to Glue.
3. **Glue Job (Python Shell)** — reads raw JSON from S3, flattens nested `{date: {currency: rate}}` structure using **Polars**, writes Parquet/CSV to processed S3 bucket
4. **Lambda (Update-FX-State)** — commits Frankfurter `last_processed_date` to DynamoDB **only after Glue succeeds**. Calls the `api_ingest` Lambda with `{"action": "update_state", "end_date": "$.parallel_results.fx.Payload.end_date"}`.
5. **Lambda (Update-ECB-State)** — commits ECB `last_processed_date` to DynamoDB after FX state is committed. Calls the `ecb_ingest` Lambda with `{"action": "update_state", "end_date": "$.parallel_results.ecb.Payload.end_date"}`.
6. **Athena** — runs a sample query on the processed data via Glue Data Catalog; results go to a dedicated S3 bucket with 1-day lifecycle TTL
7. **Lambda (Validation)** — checks Athena query status, counts rows, publishes a custom `EmptyQueryResults` CloudWatch metric

### Multi-Source Ingestion

All ingestion Lambdas share a common base class (`lambda/common/base.py`):

| Lambda | Source | Handler class | File naming |
|--------|--------|---------------|-------------|
| `lambda_ingestion_function.py` | Frankfurter API | `FrankfurterHandler` | `exchange_rates_{BASE}_{START}_to_{END}.json` |
| `lambda_ecb_ingestion.py` | ECB SDW SDMX-JSON API | `ECBHandler` | `ecb_rates_{START}_to_{END}.json` |

`BaseIngestionHandler` (abstract) provides:
- `save_to_s3(data, filename)` — S3 write with `source` metadata
- `get_last_processed()` / `update_last_processed(date)` — DynamoDB state (allowlist-guarded fallback)
- `run(event, context)` — routes `update_state` action, incremental mode, or static mode
- `_incremental_ingest()` / `_static_ingest()` / `_handle_update_state()` — shared orchestration

Subclasses implement `fetch_data(start, end)` and `make_filename(start, end)`.

ECB response parsing: SDMX-JSON format (`dataSets[0].series["FREQ:CCY:..."]`) is normalised into `{"base": "EUR", "source": "ecb", "rates": {date: {ccy: rate}}}`.

### Incremental Processing

Each handler supports two modes controlled by the `STATE_TABLE` env var. DynamoDB state is keyed by `(pipeline_id="fxlake", source=<source_name>)` — each source has independent state.

- **Incremental mode** (`STATE_TABLE` set): reads `last_processed_date` from DynamoDB, defaults to `START_DATE` on first run. Fetches `last_processed_date+1` to `min(today, END_DATE)`. Returns `end_date` in payload for Step Functions to pass to the `Lambda-Update-State` step.
- **Static fallback** (`STATE_TABLE` not set): fetches the full `START_DATE..END_DATE` range (used for backfills/testing).

### Step Functions Error Handling

Every state has Retry and Catch blocks:
- **Lambda states**: retry on `Lambda.ServiceException`, `Lambda.AWSLambdaException`, `Lambda.TooManyRequestsException`
- **Glue state**: retry on `Glue.ConcurrentRunsExceededException`, `States.HeartbeatTimeout`
- **Athena state**: retry on `Athena.InternalServerException`, `Athena.TooManyRequestsException`
- **All Catch blocks**: `ResultPath = "$.errorInfo"` preserves the actual error; Fail states use `ErrorPath`/`CausePath` to surface the real cause in execution history

### Key Terraform Files

| File | What it defines |
|------|----------------|
| `step_function.tf` | ASL definition for 7-stage orchestration: Parallel-Ingestion → Choice → Glue → Update-FX-State → Update-ECB-State → Athena → Validation, with Retry/Catch + 6 Fail states + Succeed state. `ResultSelector` shapes Parallel output to named keys; `ResultPath` preserves state across all stages. |
| `dynamodb.tf` | `fxlake-pipeline-state` table for incremental processing state (partition: `pipeline_id`, sort: `source`) |
| `lambda.tf` | Three Lambda functions (api_ingest, ecb_ingest, check_query_results) + EventBridge rule/target (→ Step Functions) |
| `glue.tf` | Glue Python Shell job (Polars, pyarrow dependencies) |
| `athena.tf` | Athena database, table schema, and results bucket config |
| `iam.tf` | All IAM roles/policies (least-privilege per service) |
| `monitoring.tf` | 7 CloudWatch alarms + dashboard |
| `security.tf` | S3 AES-256 encryption + CloudTrail multi-region trail |
| `variables.tf` | All configurable inputs (region, bucket names, date range, currency, output format) |

### Runtime Environments

- Lambda functions: Python 3.12
- Glue Python Shell job: Python 3.9 (Polars 0.18.8 + pyarrow)
- Local dev / diagrams: Python 3.11 (see `.python-version`)

### S3 Bucket Layout

- **Raw:** `exchange_rates_{BASE}_{START}_to_{END}.json`
- **Processed (partitioned):** `exchange_rates/year=YYYY/month=MM/day=DD/{stem}.parquet`
- **Athena results:** `results/` (1-day TTL)
- **CloudTrail logs:** `AWSLogs/{account-id}/...`

Glue writes one Parquet file per date in the source JSON. Athena uses **partition projection** (configured in `athena.tf`) to resolve partitions without `MSCK REPAIR TABLE`. The Glue catalog table defines `year`, `month`, `day` as partition keys with integer type and zero-padded digits.

## Error Handling Patterns

All source files follow these conventions:

- **Module-level config**: `os.environ[]` (not `os.getenv`) for required vars — fails fast at cold start if missing. `os.getenv()` for optional vars (e.g., `STATE_TABLE`)
- **Exception catches are type-specific**: `ClientError` for AWS SDK errors, `Timeout`/`HTTPError`/`ConnectionError` for HTTP, `json.JSONDecodeError`/`KeyError`/`ValueError` for data parsing
- **All catches re-raise** after logging — no silent swallowing (except `publish_custom_metric` which catches `Exception` because metric failure must not abort validation)
- **All error logs include context**: bucket names, filenames, API URLs, query IDs, error codes
- **Type annotations** on all function signatures

## Tests

Tests live in `tests/` and use pytest + moto v5 + responses. 88 tests, 99% coverage.

```bash
uv run pytest tests/ -v                              # Run all tests
uv run pytest tests/test_lambda_ingestion.py -v      # Single file
uv run pytest tests/ --cov=lambda --cov=glue --cov-report=term-missing  # With coverage
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
| `lambda/common/base.py` | 100% |
| `lambda/lambda_ingestion_function.py` | 100% |
| `lambda/lambda_ecb_ingestion.py` | 100% |
| `lambda/lambda_validation_function.py` | 100% |
| `glue/glue_transform.py` | 96% (uncovered: `except Exception` fallthrough in `list_json_keys`, `if __name__` guard) |

**Overall: 99% (325 statements, 4 missed)**

### Test File Organisation

| File | What it covers |
|------|---------------|
| `test_base_handler.py` | `BaseIngestionHandler` — save_to_s3, DynamoDB state, orchestration, saga pattern (24 tests) |
| `test_lambda_ingestion.py` | `FrankfurterHandler` — API calls, filename, integration via `lambda_handler` (19 tests) |
| `test_lambda_ecb_ingestion.py` | `ECBHandler` — SDMX parsing, API calls, integration (16 tests) |
| `test_glue_transform.py` | Glue Polars transform (17 tests) |
| `test_lambda_validation.py` | Validation Lambda, CloudWatch metric (12 tests) |

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

Required secrets: `TF_RAW_BUCKET_NAME`, `TF_PROCESSED_BUCKET_NAME`, `TF_ATHENA_RESULTS_BUCKET_NAME`, `TF_CLOUDTRAIL_LOGS_BUCKET_NAME`, `TF_SNS_EMAIL_ADDRESS`.

### Linting

Ruff config is in `pyproject.toml` (`[tool.ruff]`). Rules: E, F, W, I (PEP 8 + imports).
`tests/conftest.py` suppresses E402 (intentional late imports for `sys.modules` patching).

```bash
uv run ruff check .        # lint
uv run ruff check . --fix  # auto-fix
```

## Planning

- `docs/planning/revised_plan.md` — 10-day extension plan with session prompts
- `docs/planning/decision_log.md` — architectural decisions and trade-offs
