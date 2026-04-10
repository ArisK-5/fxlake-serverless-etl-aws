# FXLake — Serverless ETL on AWS

A **serverless, event-driven ETL pipeline** on AWS that ingests financial data from three independent sources, transforms it with Polars, enforces data quality checks, and makes it queryable via Athena. Infrastructure is managed entirely with Terraform.

**Technologies used:**
Terraform · S3 · Lambda · Glue · Athena · Step Functions · EventBridge · DynamoDB · IAM · SNS · CloudWatch · CloudTrail · X-Ray · GitHub Actions · Python (Polars)

---

## Table of Contents

- [Overview](#overview)
  - [Repo Structure](#repo-structure)
  - [Cloud Architecture](#cloud-architecture)
  - [Development Workflow](#development-workflow)
  - [Features](#features)
  - [Skills Demonstrated](#skills-demonstrated)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Setup & Deployment](#setup--deployment)
  - [Running the Pipeline](#running-the-pipeline)
  - [Backfill Historical Data](#backfill-historical-data)
- [Data Sources](#data-sources)
- [Data Quality Framework](#data-quality-framework)
- [CI/CD Pipeline](#cicd-pipeline)
- [Architecture Decision Records](#architecture-decision-records)
- [Future Improvements](#future-improvements)

## Overview

### Repo Structure

```bash
.
├── .github/workflows/
│   ├── ci.yml                          # PR checks: ruff + pytest + terraform validate
│   └── deploy.yml                      # Deploy: terraform plan → apply (OIDC auth)
├── assets/
│   ├── cloud-architecture.py           # Architecture diagram generator
│   ├── dev-workflow.py                 # Dev workflow diagram generator
│   ├── diagrams/                       # Generated PNG diagrams
│   └── icons/                          # Custom diagram icons
├── docs/
│   ├── adr/                            # Architecture Decision Records (ADR-001–004)
│   └── planning/                       # Project planning docs
├── glue/
│   ├── glue_transform.py               # Glue Python Shell job (Polars transform)
│   └── quality.py                      # Data quality check framework
├── lambda/
│   ├── common/
│   │   ├── base.py                    # BaseIngestionHandler (shared logic)
│   │   └── logging.py                 # Structured JSON logging + Timer
│   ├── lambda_fx_ingestion.py         # Frankfurter API handler
│   ├── lambda_ecb_ingestion.py        # ECB SDW API handler
│   ├── lambda_fred_ingestion.py       # FRED API handler
│   ├── lambda_validation_function.py  # Athena result validation
│   ├── package_lambdas.sh             # Lambda ZIP packaging script
│   └── requirements.txt               # Lambda runtime dependencies
├── terraform/
│   ├── bootstrap/main.tf              # Remote state backend bootstrap
│   ├── modules/lambda_function/       # Reusable Lambda module (ECB, FRED)
│   ├── step_function.tf               # 9-stage Step Functions ASL
│   ├── lambda.tf                      # Lambda definitions + EventBridge
│   ├── glue.tf                        # Glue job + quality.py upload
│   ├── dynamodb.tf                    # Pipeline state table
│   ├── s3.tf                          # 5 S3 buckets (raw, processed, athena, cloudtrail, quarantine)
│   ├── athena.tf                      # Glue Data Catalog + partition projection
│   ├── monitoring.tf                  # 11 CloudWatch alarms + dashboard
│   ├── iam.tf                         # Least-privilege roles/policies
│   ├── security.tf                    # S3 encryption + CloudTrail
│   ├── variables.tf                   # All configurable inputs
│   ├── outputs.tf                     # Exported values (ARNs, names)
│   ├── versions.tf                    # Terraform + provider version constraints
│   ├── providers.tf                   # AWS provider configuration
│   └── backend.tf                     # Remote state backend (S3 + DynamoDB)
├── tests/
│   ├── conftest.py                    # Shared fixtures (moto mocks, env setup)
│   ├── integration/
│   │   ├── test_pipeline_flow.py      # End-to-end pipeline tests (25 tests)
│   │   └── test_multi_source.py       # Multi-source parallel ingestion (7 tests)
│   ├── test_base_handler.py           # Base handler tests (55 tests)
│   ├── test_lambda_fx_ingestion.py    # Frankfurter handler (19 tests)
│   ├── test_lambda_ecb_ingestion.py   # ECB handler (20 tests)
│   ├── test_lambda_fred_ingestion.py  # FRED handler (23 tests)
│   ├── test_glue_transform.py         # Glue transform (35 tests)
│   ├── test_data_quality.py           # Quality framework (29 tests)
│   ├── test_lambda_validation.py      # Validation Lambda (16 tests)
│   └── test_structured_logging.py     # Logging module (18 tests)
├── CLAUDE.md
├── Makefile
├── pyproject.toml
└── README.md
```

### Cloud Architecture

The pipeline orchestrates a 9-stage serverless ETL flow using AWS services:

- **3 AWS Lambdas** (Python 3.12) ingest data from [Frankfurter API](https://frankfurter.dev), [ECB Statistics Data Warehouse](https://data.ecb.europa.eu), and [FRED](https://fred.stlouisfed.org) in parallel, storing raw JSON in S3.
- **DynamoDB** tracks the last-processed date per source for incremental processing — each Lambda only fetches data newer than its watermark.
- **AWS Glue** (Python Shell with Polars) transforms raw JSON into Parquet, routes files to the correct domain (`fx_rates` or `economic_indicators`), and runs data quality checks. Records failing CRITICAL checks are quarantined to a dedicated S3 bucket.
- **Amazon Athena** queries the transformed data via Glue Data Catalog with partition projection (no `MSCK REPAIR TABLE` needed).
- **AWS Step Functions** coordinate the full workflow: Parallel Ingestion (3 branches) → Check New Data → Glue → Check Backfill Mode → Update State (3 sequential steps) → Athena → Validation. Backfill executions skip state updates to protect the incremental watermark.
- **Amazon EventBridge** triggers the pipeline daily.
- **CloudWatch** provides 11 alarms (including data quality and staleness), a dashboard, and structured JSON logging. **X-Ray** traces all Lambda and SDK calls.
- **IAM** enforces least-privilege access; **CloudTrail** records all API activity.
- **Terraform** manages all 65+ resources across 11+ configuration files.

Diagrams are generated with [Diagrams](https://diagrams.mingrammer.com) — see [cloud-architecture.py](assets/cloud-architecture.py) and [dev-workflow.py](assets/dev-workflow.py).

![FXLake — Cloud Architecture](/assets/diagrams/cloud-architecture.png "cloud architecture diagram")

### Development Workflow

![FXLake — Development Workflow](/assets/diagrams/dev-workflow.png "development workflow diagram")

### Features

- **Multi-Source Ingestion:** Three independent data sources (Frankfurter, ECB, FRED) ingested in parallel via Step Functions.
- **Incremental Processing:** DynamoDB-backed watermarks ensure each source only fetches new data, with safe fallback on transient errors.
- **Data Quality Framework:** Automated checks (null detection, range validation, duplicate detection) with CRITICAL failures quarantined and WARNING failures logged.
- **Backfill Capability:** Historical re-ingestion via `make backfill` without corrupting incremental state.
- **Serverless & Cost-Efficient:** No EC2 instances — Glue runs at 0.0625 DPU (smallest Python Shell), Lambdas pay per invocation.
- **Saga Pattern:** DynamoDB state is only committed after Glue succeeds, preventing data corruption on partial failures.
- **Structured Observability:** JSON logging across all Lambdas, X-Ray tracing, 11 CloudWatch alarms, and a monitoring dashboard.
- **CI/CD:** GitHub Actions with OIDC authentication — lint + test on PRs, plan + apply on merge.
- **97% Test Coverage:** 247 tests (215 unit + 32 integration) using pytest, moto, and responses.

### Skills Demonstrated

- **Cloud Architecture Design**: Multi-source parallel ingestion pipeline with saga-pattern state management.
- **Serverless Development**: Python Lambdas with shared base class, Glue Python Shell with Polars for efficient transforms.
- **Data Engineering**: Incremental processing, data quality checks, quarantine flow, Hive-partitioned Parquet output.
- **Infrastructure as Code**: 47+ Terraform resources including reusable modules, remote state backend, and bootstrap configuration.
- **Testing Strategy**: TDD approach with 97% coverage, integration tests exercising full pipeline flows with moto.
- **CI/CD Pipeline**: GitHub Actions with OIDC, separate lint/test and plan/apply workflows.
- **Security Best Practices**: Least-privilege IAM, S3 encryption, CloudTrail auditing, OIDC (no stored credentials).
- **Monitoring & Alerting**: CloudWatch alarms for quality, staleness, and failures with SNS notifications.

## Getting Started

### Prerequisites

- **AWS CLI**: Installed and configured with proper credentials
- **Terraform**: Version 1.5+ (install via [Terraform downloads](https://developer.hashicorp.com/terraform/downloads) or Homebrew)
- **Python**: Version 3.11+ — [uv](https://docs.astral.sh/uv/) recommended for dependency management
- **Make**: For running provided Makefile targets (pre-installed on macOS/Linux)
- **FRED API Key**: Free from [FRED](https://fred.stlouisfed.org/docs/api/api_key.html) — required for economic data ingestion

### Setup & Deployment

1. **Clone the repository:**

```bash
git clone https://github.com/ArisK-5/fxlake-serverless-etl-aws.git
cd fxlake-serverless-etl-aws
```

2. **Install Python dependencies** (for local development/testing):

```bash
uv sync
```

3. **Configure Terraform variables:**

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
```

Edit `terraform/terraform.tfvars` — set unique S3 bucket names, your email for SNS alerts, and your FRED API key:

```hcl
raw_bucket_name       = "your-unique-raw-bucket"
processed_bucket_name = "your-unique-processed-bucket"
sns_email_address     = "your@email.com"
fred_api_key          = "your-fred-api-key"
# ... see terraform.tfvars.example for all options
```

4. **Package Lambda functions:**

```bash
make package
```

5. **Bootstrap remote state backend** (first time only):

```bash
cd terraform/bootstrap && terraform init && terraform apply
cd ../..
```

Then uncomment the backend configuration in `terraform/backend.tf` and run `terraform init -migrate-state`.

6. **Deploy infrastructure:**

```bash
make init    # Initialize Terraform
make plan    # Preview changes
make deploy  # Apply changes
```

### Running the Pipeline

The pipeline runs automatically on a daily schedule via EventBridge. You can also trigger it manually:

- **AWS Console:** Start a Step Functions execution with empty input `{}`
- **AWS CLI:** `aws stepfunctions start-execution --state-machine-arn <ARN>`

### Backfill Historical Data

Re-ingest historical data without affecting the incremental watermark:

```bash
make backfill START=2023-01-01 END=2023-12-31
```

This starts a Step Functions execution with `mode: "backfill"` input. The pipeline skips DynamoDB state updates, so incremental processing continues from where it left off on the next scheduled run.

## Data Sources

| Source | Lambda | Data | API |
|--------|--------|------|-----|
| **Frankfurter** | `lambda_fx_ingestion.py` | Daily FX rates (EUR base) | [frankfurter.dev](https://frankfurter.dev) |
| **ECB** | `lambda_ecb_ingestion.py` | ECB official exchange rates (SDMX-JSON) | [data.ecb.europa.eu](https://data.ecb.europa.eu) |
| **FRED** | `lambda_fred_ingestion.py` | US economic indicators (default: unemployment rate) | [fred.stlouisfed.org](https://fred.stlouisfed.org) |

All ingestion Lambdas share a common base class (`lambda/common/base.py`) that provides S3 writes, DynamoDB state management, and mode routing (incremental/backfill/static). Subclasses only implement `fetch_data()` and `make_filename()`.

## Data Quality Framework

The Glue job runs automated quality checks on every file before writing to the processed bucket:

| Check | FX Rates | Economic Indicators |
|-------|----------|---------------------|
| Required columns present | Yes | Yes |
| No null values in key fields | Yes | Yes |
| Positive numeric values | Yes | - |
| Rate within valid range [0.0001, 1000] | Yes | - |
| Valid source set | Yes | - |
| No duplicate records | Yes (WARNING) | Yes (WARNING) |

**Severity levels:**
- **CRITICAL**: File is quarantined to a dedicated S3 bucket, CloudWatch metric published, pipeline raises error.
- **WARNING**: CloudWatch metric published, processing continues. Quality report JSON written for every file.

## CI/CD Pipeline

Two GitHub Actions workflows with **OIDC authentication** (no stored AWS credentials):

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | Pull request | Ruff lint + pytest (unit tests) + `terraform validate` + `terraform fmt -check` |
| `deploy.yml` | Push to `main` | `terraform plan` (uploads artifact) → manual approval → `terraform apply` (exact plan from artifact) |

### Required GitHub Secrets

| Secret | Purpose |
|--------|---------|
| `AWS_ROLE_ARN` | IAM role ARN for OIDC authentication |
| `TF_RAW_BUCKET_NAME` | S3 bucket name for raw data |
| `TF_PROCESSED_BUCKET_NAME` | S3 bucket name for processed data |
| `TF_ATHENA_RESULTS_BUCKET_NAME` | S3 bucket for Athena results |
| `TF_CLOUDTRAIL_LOGS_BUCKET_NAME` | S3 bucket for CloudTrail logs |
| `TF_QUARANTINE_BUCKET_NAME` | S3 bucket for quarantined records |
| `TF_SNS_EMAIL_ADDRESS` | Email for SNS alarm notifications |
| `FRED_API_KEY` | FRED API key for economic data |

## Architecture Decision Records

Key design decisions are documented as ADRs in [`docs/adr/`](docs/adr/):

| ADR | Decision | Key Trade-off |
|-----|----------|---------------|
| [ADR-001](docs/adr/ADR-001-polars-over-pyspark.md) | Polars over PySpark for Glue | 32x cost reduction (0.0625 DPU) vs single-node ceiling |
| [ADR-002](docs/adr/ADR-002-dynamodb-for-pipeline-state.md) | DynamoDB for pipeline state | Atomic writes + composite key vs overkill for 3 records |
| [ADR-003](docs/adr/ADR-003-parallel-ingestion-step-functions.md) | Parallel ingestion via Step Functions | 3x faster ingestion vs all-or-nothing failure mode |
| [ADR-004](docs/adr/ADR-004-data-quality-in-glue.md) | Data quality checks in Glue | Single-pass efficiency vs coupled deployment |

## Future Improvements

- Add more economic data series from FRED (GDP, CPI, interest rates).
- Implement data versioning and schema evolution tracking.
- Add a cost monitoring dashboard for AWS spending.
- Introduce environment-specific deployments (dev, staging, prod) with Terraform workspaces.
- Add a lightweight API layer (API Gateway + Lambda) for on-demand queries.
