# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FXLake is a serverless ETL pipeline on AWS that fetches daily foreign exchange rates from the [Frankfurter API](https://api.frankfurter.app), transforms them with Polars, and makes them queryable via Athena. Infrastructure is managed entirely with Terraform.

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
make package     # Runs lambda/package_lambdas.sh — produces .zip files for both Lambda functions
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

The pipeline is orchestrated by **Step Functions** and runs on a daily **EventBridge** schedule:

1. **Lambda (Ingestion)** — fetches FX rates JSON from Frankfurter API → saves to S3 raw bucket
2. **Glue Job (Python Shell)** — reads raw JSON from S3, flattens nested `{date: {currency: rate}}` structure using **Polars**, writes Parquet/CSV to processed S3 bucket
3. **Athena** — runs a sample query on the processed data via Glue Data Catalog; results go to a dedicated S3 bucket with 1-day lifecycle TTL
4. **Lambda (Validation)** — checks Athena query status, counts rows, publishes a custom `EmptyQueryResults` CloudWatch metric

### Key Terraform Files

| File | What it defines |
|------|----------------|
| `step_function.tf` | ASL definition for the 4-stage orchestration |
| `lambda.tf` | Both Lambda functions + EventBridge daily trigger |
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
- **Processed:** `exchange_rates/exchange_rates_{BASE}_{START}_to_{END}.parquet`
- **Athena results:** `results/` (1-day TTL)
- **CloudTrail logs:** `AWSLogs/{account-id}/...`

## Tests

Tests live in `tests/` and use pytest + moto v5 + responses.

```bash
uv run pytest tests/ -v              # Run all tests
uv run pytest tests/test_lambda_ingestion.py -v  # Single file
```

- `awsglue.utils` is mocked in `conftest.py` via `sys.modules` before any import of `glue_transform`
- Module-level env vars are set in `conftest.py` before Lambda modules are imported
- `s3_mock` fixture activates `mock_aws()` and creates both S3 buckets
