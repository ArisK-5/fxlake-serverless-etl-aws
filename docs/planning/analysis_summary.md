# FXLake Forensic Analysis Summary

**Date:** 2026-03-28
**Analyst:** Claude Code (Opus 4.6)
**Commit:** 115f056 (main, clean working tree)

---

## Phase 1: Repository Structure

```
35 files total | 7 directories | Python 3.11 (local)
```

| Directory | Files | Purpose |
|-----------|-------|---------|
| `lambda/` | 2 .py, 2 .zip, 1 .sh, 1 requirements.txt | Lambda function source + packaging |
| `glue/` | 1 .py | Glue Python Shell ETL script |
| `terraform/` | 11 .tf, 1 .tfvars, 1 .tfvars.example, tfstate files | Complete IaC |
| `assets/` | 2 .py, 2 .png, 2 icons | Diagram generation (Python `diagrams` library) |
| Root | Makefile, pyproject.toml, uv.lock, main.py (stub), README.md | Project config |

**Source code:** 3 Python files totaling ~286 lines (76 + 76 + 134).

**Test files:** ZERO. No test directory, no test files, no test framework in dependencies.

**CI/CD:** ZERO. No GitHub Actions, no GitLab CI, no Dockerfile, no serverless.yml.

---

## Phase 2: Infrastructure Inventory

### Terraform State

| Attribute | Value |
|-----------|-------|
| Terraform version | 1.12.2 |
| AWS provider | hashicorp/aws ~> 5.0 |
| Backend | **Local** (no S3 remote state) |
| State serial | 1089 (high = was deployed, iterated) |
| Current resources | **0** (destroyed — `"resources": []`) |
| Total defined resources | **47** across 11 .tf files |

**Key finding:** Infrastructure was previously deployed and then torn down (`make destroy`). The tfstate is empty but present with high serial number.

### Resource Inventory (47 Terraform resources)

| Category | Count | Resources |
|----------|-------|-----------|
| S3 | 7 | 4 buckets + 1 lifecycle + 1 bucket policy + 1 Glue script object |
| Lambda | 5 | 2 functions + EventBridge rule + target + permission |
| Glue | 3 | 1 job + 1 catalog database + 1 catalog table |
| Athena | 1 | 1 workgroup |
| Step Functions | 1 | 1 state machine |
| IAM | 14 | 4 roles + 4 policies + 3 attachments + 3 inline policies |
| Monitoring | 10 | 7 alarms + 1 dashboard + 1 SNS topic + 1 subscription |
| Security | 5 | 3 S3 encryption configs + 1 CloudTrail + 1 log metric filter |
| Logging | 2 | 2 CloudWatch log groups |

### Deployment Method

- **Makefile** with targets: `package`, `init`, `plan`, `deploy`, `destroy`, `clean`
- Lambda packaging via bash script (`package_lambdas.sh`) using `pip3 install --target` + `zip`
- No automated deployment pipeline

### AWS Resource Names (from variables/tfvars)

| Resource | Name Pattern |
|----------|-------------|
| S3 (raw) | `fxlake-raw-data-unique` (user-configured) |
| S3 (processed) | `fxlake-processed-data-unique` |
| S3 (athena) | `fxlake-athena-results-unique` |
| S3 (cloudtrail) | `fxlake-cloudtrail-logs-unique` |
| Lambda (ingestion) | `fxlake-api-ingest-lambda` |
| Lambda (validation) | `fxlake-results-check-lambda` |
| Glue job | `fxlake-glue-transform-job` |
| Step Function | `fxlake-etl-state-machine` |
| Athena workgroup | `fxlake` |
| Athena database | `fxlake` |
| CloudWatch namespace | `FXLake/Athena` |

---

## Phase 3: Code Architecture Deep Dive

### 3.1 Lambda Functions

#### Ingestion Lambda (`lambda/lambda_ingestion_function.py` — 76 lines)

| Attribute | Value |
|-----------|-------|
| Handler | `lambda_ingestion_function.lambda_handler` |
| Runtime | Python 3.12 |
| Timeout | 60s |
| Dependencies | `boto3==1.28.44`, `requests==2.31.0` |
| API endpoint | `https://api.frankfurter.dev/v1/{start}..{end}?base={currency}` |
| S3 output | `s3://raw-bucket/exchange_rates_{BASE}_{START}_to_{END}.json` |

**Environment variables consumed:**
- `RAW_BUCKET` — S3 bucket for raw JSON
- `START_DATE` / `END_DATE` — hardcoded date range (not dynamic)
- `BASE_CURRENCY` — EUR default
- `BASE_API_URL` — Frankfurter API base URL

**Critical observations:**
- Date range is **static** (set at deploy time via Terraform vars) — not dynamic per-execution
- Lambda is triggered by EventBridge daily, but always fetches the **same date range**
- No event parameter parsing — ignores the `event` argument entirely
- Output key is deterministic — **overwrites** the same file every run (not append/partition)

#### Validation Lambda (`lambda/lambda_validation_function.py` — 76 lines)

| Attribute | Value |
|-----------|-------|
| Handler | `lambda_validation_function.lambda_handler` |
| Runtime | Python 3.12 |
| Timeout | 60s |
| Dependencies | `boto3` (from Lambda runtime, not packaged) |

**Environment variables consumed:**
- `METRIC_NAMESPACE` — CloudWatch namespace for custom metric
- `PIPELINE` — dimension value for metric

**Custom CloudWatch metric published:**
- `EmptyQueryResults` in `FXLake/Athena` namespace
- Value: `1` if zero rows, `0` otherwise
- Dimensions: WorkGroup, Pipeline

**Critical observations:**
- Only checks **row count** — no schema validation, no data quality checks
- Receives `QueryExecutionId` from Step Functions (Athena output passthrough)
- No validation of actual data values (ranges, nulls, types)

### 3.2 Glue Job (`glue/glue_transform.py` — 134 lines)

| Attribute | Value |
|-----------|-------|
| Type | Python Shell (not Spark) |
| Python version | 3.9 |
| Max capacity | 0.0625 DPU (minimum) |
| Dependencies | `polars==0.18.8`, `boto3`, `pyarrow` |
| Max retries | 0 |

**Transformation logic:**
1. Lists all `.json` files in raw bucket (paginated)
2. For each JSON file: reads, flattens `{date: {currency: rate}}` → rows
3. Creates Polars DataFrame with columns: `base_currency`, `target_currency`, `rate`, `date`
4. Writes as Parquet (default) or CSV to processed bucket

**Critical observations:**
- **Full refresh every run** — processes ALL JSON files in bucket, not just new ones
- Polars 0.18.8 is **extremely outdated** (current: 1.x with breaking API changes)
- No partitioning — flat output path `exchange_rates/{filename}.parquet`
- No deduplication logic
- No schema validation before write
- `getResolvedOptions` used correctly for Glue parameter injection

### 3.3 Step Functions State Machine

**4 states, linear sequence, no branching:**

```
Lambda-API-Ingestion (30s timeout)
        ↓
Glue-JSON-to-Parquet (180s timeout)
        ↓
Athena-Sample-Query (90s timeout) — "SELECT * FROM exchange_rates LIMIT 100;"
        ↓
Lambda-Validation-Query (30s timeout) — END
```

**Critical observations:**
- **No Retry/Catch blocks** on any state — any failure = entire execution fails
- **No Parallel state** — purely sequential
- Athena query is hardcoded `SELECT * LIMIT 100` — sample query only
- Result passthrough works: Athena output → `$.QueryExecution.QueryExecutionId` → Validation Lambda
- **Original plan assumed ASL JSON file** — actually inline in Terraform `jsonencode()`

### 3.4 Data Flow

```
[Frankfurter API]
  https://api.frankfurter.dev/v1/2024-01-01..2024-12-31?base=EUR
        │
        ▼
[Lambda: Ingestion]
  → s3://raw-bucket/exchange_rates_EUR_2024-01-01_to_2024-12-31.json
        │
        ▼
[Glue: Python Shell + Polars]
  ← reads ALL .json from raw bucket
  → s3://processed-bucket/exchange_rates/exchange_rates_EUR_2024-01-01_to_2024-12-31.parquet
        │
        ▼
[Athena: Sample Query]
  SELECT * FROM exchange_rates LIMIT 100
  → s3://athena-results-bucket/results/ (1-day TTL)
        │
        ▼
[Lambda: Validation]
  → CloudWatch metric: FXLake/Athena/EmptyQueryResults
```

**Data schema (Athena table):**

| Column | Type |
|--------|------|
| base_currency | string |
| target_currency | string |
| rate | double |
| date | string |

---

## Phase 4: Gap Analysis

### 4.1 Multi-Source Ingestion

**Status: ❌ GAP — Single source only**

| Check | Result |
|-------|--------|
| Lambda functions | 1 ingestion Lambda (Frankfurter API only) |
| Unique API endpoints | 1 (`https://api.frankfurter.dev/v1`) |
| Step Functions Parallel state | No — linear only |
| Separate handlers per source | No — single handler |
| Multi-source logic in code | No |

**Gap severity:** 10/10 — completely missing
**Recommendation:** HIGH priority. Create modular ingestion pattern with separate Lambdas per source + Parallel state in Step Functions.

### 4.2 Data Quality Framework

**Status: ❌ GAP — Zero validation**

| Check | Result |
|-------|--------|
| Great Expectations | Not imported |
| Pydantic | Not imported |
| Schema validation | None in Glue job |
| Null checks | None |
| Range validation | None |
| Quarantine pattern | No quarantine bucket/path |
| Failure handling | Generic `raise` only |

The Validation Lambda checks **row count only** (empty vs non-empty). There is no validation of:
- Data types
- Value ranges (e.g., FX rate > 0)
- Null/missing fields
- Schema conformance
- Duplicate detection

**Gap severity:** 10/10 — no framework, no quarantine
**Recommendation:** HIGH priority. Great Expectations or custom Pydantic-based validation + quarantine bucket + dead-letter queue pattern.

### 4.3 Incremental Processing

**Status: ❌ GAP — Full refresh only**

| Check | Result |
|-------|--------|
| State management (DynamoDB/SSM) | None |
| Bookmark/checkpoint | None |
| S3 partitioning (year=/month=/day=) | None — flat path |
| Dynamic date range | No — hardcoded in env vars at deploy time |
| Backfill logic | None |
| Append vs overwrite | Overwrites same filename every run |
| Event parameter parsing | Lambda ignores `event` entirely |

**Gap severity:** 10/10 — fundamentally missing
**Recommendation:** HIGH priority. This is table stakes for production ETL:
1. Dynamic date resolution (Lambda reads current date, not static env var)
2. S3 partitioning (`year=YYYY/month=MM/day=DD/`)
3. State tracking via DynamoDB or SSM Parameter Store
4. Backfill mode via Step Function input parameters

### 4.4 Terraform IaC

**Status: ✅ DONE — Comprehensive**

| Check | Result |
|-------|--------|
| Terraform exists | Yes — 11 .tf files, 47 resources |
| Provider version | ~> 5.0 (current) |
| Backend | ⚠️ Local (not S3 remote state) |
| State | Currently empty (resources destroyed) |
| Modules | None — flat file structure |

**Gap severity:** 2/10 — exists but needs refinement
**Recommendation:** MEDIUM priority improvements:
- Migrate to S3 backend with DynamoDB locking
- Refactor into Terraform modules (networking, compute, storage, monitoring)
- Add `terraform-docs` for auto-generated docs
- Add `tflint` / `checkov` for static analysis

### 4.5 Monitoring & Observability

**Status: ✅ DONE — Solid foundation**

| Check | Result |
|-------|--------|
| Custom CloudWatch metrics | Yes — `EmptyQueryResults` |
| CloudWatch alarms | 7 alarms covering all services |
| Dashboard | Yes — 8 widgets (singleValue) |
| SNS alerts | Yes — email subscription |
| CloudTrail | Yes — multi-region, log validation |
| Unauthorized API detection | Yes — metric filter + alarm |
| Structured logging | Partial — validation Lambda uses JSON, ingestion doesn't |

**Gap severity:** 3/10 — good foundation, needs enhancement
**Recommendation:** LOW-MEDIUM priority:
- Add more custom metrics (RecordsFetched, TransformDuration, etc.)
- Upgrade dashboard to time-series graphs (not just singleValue)
- Add structured JSON logging to all Lambdas
- Add X-Ray tracing

### 4.6 CI/CD Pipeline (not in original plan but critical)

**Status: ❌ GAP — Zero automation**

| Check | Result |
|-------|--------|
| GitHub Actions | None |
| GitLab CI | None |
| Any CI/CD | None |
| Automated testing | None |
| Automated deployment | Makefile only (manual) |

**Gap severity:** 9/10
**Recommendation:** HIGH priority for portfolio credibility. GitHub Actions with: lint, test, terraform plan on PR, terraform apply on merge.

### 4.7 Testing

**Status: ❌ GAP — Zero tests**

| Check | Result |
|-------|--------|
| Test files | None in project |
| Test framework | Not in dependencies |
| Mocking libraries (moto) | Not installed |
| pytest configuration | None |

**Gap severity:** 9/10
**Recommendation:** HIGH priority. Add:
- `pytest` + `moto` for Lambda unit tests
- Glue job tests with local Polars execution
- Terraform validation (`terraform validate`, `tflint`)

---

## Phase 5: Risk & Blocker Assessment

### Risk Matrix

| # | Risk | Severity | Likelihood | Mitigation |
|---|------|----------|------------|------------|
| 1 | **No tests = blind refactoring** | HIGH | CERTAIN | Write tests BEFORE any refactoring |
| 2 | **Local Terraform state** | HIGH | HIGH | Migrate to S3 backend before multi-dev work |
| 3 | **No CI/CD = manual deploy errors** | MEDIUM | HIGH | Add GitHub Actions early (Day 2) |
| 4 | **Polars 0.18.8 → 1.x breaking changes** | MEDIUM | CERTAIN | Version upgrade is a separate task; API changes are significant |
| 5 | **Step Function has no error handling** | MEDIUM | HIGH | Add Retry/Catch before adding complexity |
| 6 | **force_destroy=true on all S3 buckets** | MEDIUM | LOW | Remove for non-dev environments |
| 7 | **EventBridge triggers Lambda directly** (not Step Function) | LOW | N/A | Architectural note: daily trigger hits Lambda, not SFN. SFN must be started separately or Lambda should trigger it. |
| 8 | **No S3 public access blocks** | LOW | LOW | Add `aws_s3_bucket_public_access_block` |
| 9 | **Hardcoded Athena query** | LOW | N/A | Parameterize when adding multi-source |

### Missing Dependencies

| Item | Status |
|------|--------|
| `requirements.txt` completeness | ✅ Ingestion Lambda: `boto3`, `requests` — matches imports |
| Validation Lambda deps | ✅ Only uses `boto3` (included in Lambda runtime) |
| Glue job deps | ✅ Declared in Terraform: `polars==0.18.8,boto3,pyarrow` |
| AWS credentials | Assumed via IAM roles (correct pattern) |
| API keys | None needed — Frankfurter API is public |
| Local dev deps | `pyproject.toml` has `boto3`, `diagrams`, `pandas`, `tree` — no test framework |

### Technical Debt

- **Zero TODOs/FIXMEs** in source code (clean but also means no roadmap markers)
- `main.py` is a stub (empty/minimal) — unused entry point
- Lambda packaging uses `pip3` directly (not uv, not Docker) — inconsistent with local dev tooling
- Both Lambdas share a single IAM role (`lambda_exec`) — violates least-privilege for validation Lambda's Athena/CloudWatch permissions being available to ingestion Lambda

### Architectural Note: EventBridge → Lambda vs EventBridge → Step Functions

The current architecture has EventBridge triggering the **ingestion Lambda directly**, not the Step Function. This means:
- The daily schedule only runs ingestion, NOT the full pipeline
- The Step Function must be triggered manually or separately
- **This may be intentional** (decouple ingestion from transform) or **a gap** (pipeline should run end-to-end daily)

**Recommendation:** Verify intent. If full pipeline should run daily, change EventBridge target to Step Function ARN.

---

## Summary Scorecard

| Extension | Current State | Gap (0-10) | Effort (0-10) | Hiring Signal (0-10) | Priority Score |
|-----------|--------------|------------|---------------|---------------------|----------------|
| Testing + CI/CD | ❌ Zero | 9 | 5 | 8 | **5.8** |
| Incremental Processing | ❌ Full refresh | 10 | 6 | 9 | **6.4** |
| Multi-Source Ingestion | ❌ Single source | 10 | 7 | 9 | **6.2** |
| Data Quality Framework | ❌ No validation | 10 | 6 | 9 | **6.4** |
| Step Function Error Handling | ❌ No Retry/Catch | 8 | 2 | 6 | **5.2** |
| Terraform Hardening | ⚠️ Local state, flat | 3 | 4 | 5 | **2.4** |
| Monitoring Enhancement | ✅ Good foundation | 3 | 3 | 4 | **2.2** |

*Formula: Priority = (Gap x 0.4) + (HiringSignal x 0.4) - (Effort x 0.2)*
