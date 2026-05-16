# FXLake — Serverless ETL on AWS

A **serverless, event-driven ETL pipeline** on AWS that ingests financial data from three independent sources, writes to Apache Iceberg tables via Athena, transforms with dbt Core, enforces data quality checks, and includes self-healing mechanisms for automated recovery. Infrastructure is managed entirely with Terraform.

**Technologies used:**
Terraform · S3 · Lambda · Athena · Apache Iceberg · dbt Core · CodeBuild · Step Functions · EventBridge · DynamoDB · SQS · IAM · SNS · CloudWatch · CloudTrail · X-Ray · GitHub Actions · Python

---

## Table of Contents

- [Overview](#overview)
  - [Repo Structure](#repo-structure)
  - [Cloud Architecture](#cloud-architecture)
  - [Step Function DAG](#step-function-dag)
  - [CI/CD Workflow](#cicd-workflow)
  - [dbt Lineage](#dbt-lineage)
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
│   ├── ci.yml                              # PR checks: ruff + pytest + terraform validate
│   └── deploy.yml                          # Deploy: terraform plan → apply (OIDC auth)
├── assets/
│   ├── dbt-lineage.py                      # dbt lineage diagram generator (from manifest.json)
│   ├── diagrams/                           # Architecture & workflow diagrams (Draw.io + PNG)
│   └── icons/                              # Custom diagram icons
├── dbt/
│   ├── buildspec.yml                       # CodeBuild build specification
│   ├── dbt_project.yml                     # dbt project config (profile, materializations)
│   ├── packages.yml                        # dbt package dependencies (dbt_utils)
│   ├── profiles.yml                        # Athena adapter config (env vars)
│   ├── macros/
│   │   └── generate_quality_report.sql     # quality.py → dbt test mapping macro
│   ├── models/
│   │   ├── staging/                        # Dedup views (stg_fx_rates, stg_economic_indicators)
│   │   └── marts/                          # Iceberg tables (fct_fx_rates, fct_economic_indicators)
│   ├── seeds/                              # Seed data (placeholder)
│   └── tests/
│       ├── generic/
│       │   └── test_positive_values.sql    # Custom generic test
│       ├── cross_source_rate_consistency.sql
│       ├── cross_source_temporal_alignment.sql
│       ├── unique_fct_fx_rates_keys.sql
│       └── unique_fct_economic_indicators_keys.sql
├── docs/
│   ├── adr/                                # Architecture Decision Records (ADR-001–007)
│   ├── data_dictionary.md                  # Data dictionary for all tables
│   ├── planning/                           # Project planning docs
│   └── queries/
│       └── audit_trail.sql                 # Audit trail query examples
├── lambda/
│   ├── common/
│   │   ├── __init__.py
│   │   ├── base.py                         # BaseIngestionHandler (shared logic)
│   │   ├── logging.py                      # Structured JSON logging + Timer
│   │   ├── quality.py                      # Data quality check framework
│   │   └── schema_validation.py            # JSON schema validation utilities
│   ├── lambda_fx_ingestion.py              # Frankfurter API handler
│   ├── lambda_ecb_ingestion.py             # ECB SDW API handler
│   ├── lambda_fred_ingestion.py            # FRED API handler
│   ├── lambda_iceberg_writer.py            # Athena INSERT INTO Iceberg tables
│   ├── lambda_iceberg_maintenance.py       # OPTIMIZE + VACUUM scheduling
│   ├── lambda_anomaly_detector.py          # Z-score anomaly detection
│   ├── lambda_cross_validator.py           # Cross-source FX rate consistency
│   ├── lambda_data_validator.py            # Data freshness validation
│   ├── lambda_validation_function.py       # Athena result validation
│   ├── lambda_dlq_auto_retry.py            # DLQ failure classification + replay
│   ├── lambda_stale_data_backfill.py       # Hourly staleness check + backfill trigger
│   ├── package_lambdas.sh                  # Lambda ZIP packaging script
│   ├── requirements.txt                    # Lambda runtime dependencies
│   └── requirements_iceberg_writer.txt     # Iceberg writer extra dependencies
├── schemas/
│   ├── raw/                                # JSON schemas for API responses
│   └── processed/                          # JSON schemas for Iceberg table records
├── scripts/
│   └── replay_dlq.py                       # Manual DLQ replay utility
├── terraform/
│   ├── bootstrap/main.tf                   # Remote state backend bootstrap
│   ├── modules/lambda_function/            # Reusable Lambda module
│   ├── step_function.tf                    # 12-stage Step Functions ASL
│   ├── lambda.tf                           # Lambda definitions + EventBridge + SQS mapping
│   ├── dlq.tf                              # SQS DLQ + EventBridge failure capture
│   ├── dynamodb.tf                         # Pipeline state table
│   ├── codebuild.tf                        # CodeBuild project for dbt execution
│   ├── s3.tf                               # 5 S3 buckets (raw, processed, athena, cloudtrail, quarantine)
│   ├── athena.tf                           # Iceberg table definitions + Glue Data Catalog
│   ├── iceberg.tf                          # Iceberg table format configuration
│   ├── budget.tf                           # AWS Budgets ($10/month alerts)
│   ├── monitoring.tf                       # 11 CloudWatch alarms + dashboard
│   ├── iam.tf                              # Least-privilege roles/policies
│   ├── security.tf                         # S3 encryption + CloudTrail
│   ├── variables.tf                        # All configurable inputs
│   ├── outputs.tf                          # Exported values (ARNs, names)
│   ├── versions.tf                         # Terraform + provider version constraints
│   ├── providers.tf                        # AWS provider configuration
│   └── backend.tf                          # Remote state backend (S3 + DynamoDB)
├── tests/
│   ├── conftest.py                         # Shared fixtures (moto mocks, env setup)
│   ├── integration/
│   │   ├── test_pipeline_flow.py           # End-to-end pipeline tests (25 tests)
│   │   └── test_multi_source.py            # Multi-source parallel ingestion (7 tests)
│   ├── test_base_handler.py                # Base handler tests (64 tests)
│   ├── test_lambda_fx_ingestion.py         # Frankfurter handler (19 tests)
│   ├── test_lambda_ecb_ingestion.py        # ECB handler (24 tests)
│   ├── test_lambda_fred_ingestion.py       # FRED handler (24 tests)
│   ├── test_iceberg_writer.py              # Iceberg writer (38 tests)
│   ├── test_iceberg_maintenance.py         # Iceberg maintenance (15 tests)
│   ├── test_anomaly_detector.py            # Anomaly detection (20 tests)
│   ├── test_cross_validator.py             # Cross-source validation (46 tests)
│   ├── test_data_quality.py                # Quality framework (29 tests)
│   ├── test_data_validator.py              # Data validator (16 tests)
│   ├── test_lambda_validation.py           # Validation Lambda (16 tests)
│   ├── test_structured_logging.py          # Logging module (18 tests)
│   ├── test_lambda_dlq_auto_retry.py       # DLQ auto-retry (32 tests)
│   ├── test_lambda_stale_data_backfill.py  # Stale data backfill (17 tests)
│   ├── test_schema_validation.py           # Schema validation tests
│   ├── test_iam_policies.py                # IAM policy tests
│   └── test_replay_dlq.py                  # DLQ replay script tests
├── CLAUDE.md
├── LICENSE
├── main.py                                 # Local pipeline entry point
├── Makefile
├── pyproject.toml
└── README.md
```

### Cloud Architecture

The pipeline orchestrates a 12-stage serverless ETL flow using AWS services:

- **3 ingestion Lambdas** (Python 3.12) fetch data from [Frankfurter API](https://frankfurter.dev), [ECB Statistics Data Warehouse](https://data.ecb.europa.eu), and [FRED](https://fred.stlouisfed.org) in parallel, storing raw JSON in S3.
- **DynamoDB** tracks the last-processed date per source for incremental processing — each Lambda only fetches data newer than its watermark.
- **Iceberg writer Lambdas** read raw JSON from S3, run quality checks, and write to **Apache Iceberg** tables via batched Athena `INSERT INTO` queries. ACID transactions, schema evolution, and time travel replace plain Parquet.
- **dbt Core** (via **CodeBuild**) transforms data with modular SQL models — staging views deduplicate and prioritize sources; mart tables materialise as Iceberg.
- **Amazon Athena** queries the transformed data via Glue Data Catalog with partition projection.
- **AWS Step Functions** coordinate the full workflow: Parallel Ingestion → Check New Data → Write FX Iceberg → Write Economic Iceberg → dbt Transform → Check Backfill Mode → Update State (3 steps) → Athena → Validation → Cross-Source Validation.
- **Self-healing Lambdas** provide automated recovery: DLQ auto-retry classifies and replays transient failures; hourly stale data backfill detects gaps and triggers catch-up executions.
- **Amazon EventBridge** triggers the pipeline daily + hourly stale data checks.
- **CloudWatch** provides 11 alarms (including data quality, staleness, and DLQ depth), a dashboard, and structured JSON logging. **X-Ray** traces all Lambda and SDK calls.
- **IAM** enforces least-privilege access; **CloudTrail** records all API activity.
- **Terraform** manages all 80+ resources across 14+ configuration files.

Diagrams are maintained as [Draw.io](https://www.drawio.com) files in `assets/diagrams/`. The dbt lineage diagram is generated from `dbt/target/manifest.json` via [dbt-lineage.py](assets/dbt-lineage.py).

![FXLake — Cloud Architecture](/assets/diagrams/cloud-architecture.png "cloud architecture diagram")

### Step Function DAG

![FXLake — Step Function DAG](/assets/diagrams/step-function-dag.png "step function diagram")

### CI/CD Workflow

![FXLake — CI/CD Workflow](/assets/diagrams/cicd-workflow.png "CI/CD workflow diagram")

### dbt Lineage

![FXLake — dbt Lineage](/assets/diagrams/dbt-lineage.png "dbt lineage diagram")

### Features

- **Multi-Source Ingestion:** Three independent data sources (Frankfurter, ECB, FRED) ingested in parallel via Step Functions.
- **Apache Iceberg Tables:** ACID transactions, schema evolution, time travel, and file-level pruning via Athena-native Iceberg support.
- **dbt Core Transformations:** Modular SQL models with built-in lineage, schema tests, and documentation generation. Staging views deduplicate; mart tables materialise as Iceberg.
- **Incremental Processing:** DynamoDB-backed watermarks ensure each source only fetches new data, with safe fallback on transient errors.
- **Data Quality Framework:** Dual-layer checks — `quality.py` for CRITICAL pre-write gates (quarantine + raise), dbt tests for schema validation. Quality reports written for every file.
- **Cross-Source Validation:** Compares FX rates from Frankfurter and ECB for rate consistency, temporal alignment, and volume distribution.
- **Self-Healing:** DLQ auto-retry classifies failures as transient/permanent and replays executions automatically. Hourly stale data backfill detects gaps and triggers catch-up runs.
- **Backfill Capability:** Historical re-ingestion via `make backfill` without corrupting incremental state.
- **Serverless & Cost-Efficient:** No EC2 instances — Athena charges per TB scanned (effectively $0 at <1 MB/day), Lambdas pay per invocation.
- **Saga Pattern:** DynamoDB state is only committed after dbt transform succeeds, preventing data corruption on partial failures.
- **Structured Observability:** JSON logging across all Lambdas, X-Ray tracing, 11 CloudWatch alarms, and a monitoring dashboard.
- **CI/CD:** GitHub Actions with OIDC authentication — lint + test on PRs, plan + apply on merge.
- **97% Test Coverage:** 636 tests (604 unit + 32 integration) using pytest, moto, and responses.

### Skills Demonstrated

- **Cloud Architecture Design**: Multi-source parallel ingestion pipeline with saga-pattern state management and self-healing mechanisms.
- **Open Table Format**: Apache Iceberg tables with ACID writes, schema evolution, time travel, and compaction scheduling.
- **Data Transformation**: dbt Core with modular SQL models, built-in lineage, schema contracts, and custom generic tests.
- **Serverless Development**: Python Lambdas with shared base class, Athena CTAS for Iceberg writes, CodeBuild for dbt execution.
- **Data Engineering**: Incremental processing, dual-layer quality checks, cross-source validation, anomaly detection, quarantine flow.
- **Infrastructure as Code**: 80+ Terraform resources including reusable modules, remote state backend, and bootstrap configuration.
- **Testing Strategy**: TDD approach with 97% coverage (629 tests), integration tests exercising full pipeline flows with moto.
- **CI/CD Pipeline**: GitHub Actions with OIDC, separate lint/test and plan/apply workflows.
- **Security Best Practices**: Least-privilege IAM, S3 encryption, CloudTrail auditing, OIDC (no stored credentials).
- **Self-Healing & Resilience**: DLQ auto-retry with failure classification, stale data auto-backfill, CloudWatch alarms with SNS notifications.

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

Quality checks run at two layers — `quality.py` pre-write gates in the Iceberg writer Lambda, and dbt schema tests post-transform:

| Check | FX Rates | Economic Indicators | Layer |
|-------|----------|---------------------|-------|
| Required columns present | Yes | Yes | quality.py |
| No null values in key fields | Yes | Yes | Both |
| Positive numeric values | Yes | - | Both |
| Rate within valid range [0.0001, 1000] | Yes | - | quality.py |
| Valid source set | Yes | - | dbt `accepted_values` |
| No duplicate records | Yes | Yes | dbt `unique_combination_of_columns` |

**Severity levels:**
- **CRITICAL**: File is quarantined to a dedicated S3 bucket, CloudWatch metric published, pipeline raises error.
- **WARNING**: CloudWatch metric published, processing continues. Quality report JSON written for every file.

## CI/CD Pipeline

Two GitHub Actions workflows with **OIDC authentication** (no stored AWS credentials):

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | Pull request | Ruff lint + pytest (unit tests) + Lambda packaging + zip verification + `terraform validate` + `terraform fmt -check` |
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
| [ADR-001](docs/adr/ADR-001-polars-over-pyspark.md) | Polars over PySpark for Glue | 32x cost reduction vs single-node ceiling *(superseded by v3)* |
| [ADR-002](docs/adr/ADR-002-dynamodb-for-pipeline-state.md) | DynamoDB for pipeline state | Atomic writes + composite key vs overkill for 3 records |
| [ADR-003](docs/adr/ADR-003-parallel-ingestion-step-functions.md) | Parallel ingestion via Step Functions | 3x faster ingestion vs all-or-nothing failure mode |
| [ADR-004](docs/adr/ADR-004-data-quality-in-glue.md) | Data quality checks in Glue | Single-pass efficiency vs coupled deployment *(superseded by v3)* |
| [ADR-005](docs/adr/ADR-005-apache-iceberg-open-table-format.md) | Apache Iceberg open table format | ACID + schema evolution + time travel vs write path complexity |
| [ADR-006](docs/adr/ADR-006-dbt-core-transformation-layer.md) | dbt Core transformation layer | Modular SQL models + lineage vs additional tool in stack |
| [ADR-007](docs/adr/ADR-007-athena-ctas-over-glue-spark.md) | Athena CTAS over Glue Spark | Near-zero cost at <1 MB/day vs SQL-only transformations |

## Future Improvements

- Add more economic data series from FRED (GDP, CPI, interest rates).
- Add a lightweight API layer (API Gateway + Lambda) for on-demand queries.
- Introduce environment-specific deployments (dev, staging, prod) with Terraform workspaces.
- Migrate to Glue Spark with Iceberg connector if data volumes grow past ~100 MB/day (see ADR-007).
