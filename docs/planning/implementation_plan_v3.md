# FXLake v3 — Implementation Plan

**Date:** 2026-04-22
**Duration:** 20 days (2-4 hours/day)
**Constraint:** Solo developer, zero downtime (v2 runs on `main` while v3 develops on its own branch), incremental delivery, 95%+ test coverage, AWS Free Tier budget conscious
**Branch strategy:** All implementation sessions merge into a `v3` branch. Once validated end-to-end, `v3` merges to `main`.

---

## Phase Overview

| Phase | Days | Focus | Deliverable |
|-------|------|-------|-------------|
| **1: Operational Excellence** | 1-4 | SLA monitoring, cost attribution, dashboards, DLQ, runbooks | Production-ready observability and operability |
| **2: Storage Layer (Iceberg)** | 5-9 | Iceberg tables, write path, validation, compaction | ACID transactions, schema evolution, time travel |
| **3: Transformation (dbt)** | 10-14 | dbt Core setup, SQL models, schema contracts, lineage | Modular, testable, documented transformations |
| **4: Governance & Discovery** | 15-17 | Data catalog, schema registry, access controls, tagging | Discoverable, governed data assets |
| **5: Advanced Features** | 18-20 | Cross-source validation, anomaly detection, self-healing | Production-grade data reliability |

---

## Phase 1: Operational Excellence Foundation (Days 1-4)

### Day 1 — Cost Attribution & Resource Tagging

**Goal:** Every AWS resource is tagged for cost attribution and operational grouping.

**Session prompt:**
```
Add consistent AWS resource tagging to the FXLake Terraform configuration.

1. Add `default_tags` to the AWS provider in `terraform/versions.tf`:
   - project = "fxlake"
   - environment = "production"
   - managed_by = "terraform"

2. Add component-specific tags to each resource:
   - Lambda functions: component = "ingestion" or "validation", source = "frankfurter|ecb|fred"
   - Glue: component = "transform"
   - Step Functions: component = "orchestration"
   - DynamoDB: component = "state"
   - S3 buckets: component = "storage", layer = "raw|processed|quarantine|athena_results"
   - CloudWatch: component = "monitoring"

3. Add an AWS Budget resource in a new `terraform/budget.tf`:
   - Monthly budget: $10
   - Alert at 80% and 100% thresholds
   - SNS notification to existing topic

4. Update CI workflow to validate tag presence (terraform plan output should show tags on all resources).

Do NOT modify any Lambda code, Glue code, or test files. This is infrastructure-only.
```

**Validation:**
- [ ] `terraform plan` shows tags on all resources
- [ ] No existing tests broken (`make test-all`)
- [ ] Budget resource created with SNS notification

**Acceptance criteria:** All 65+ Terraform resources have consistent tags. Budget alarm fires at $8 and $10.

---

### Day 2 — Enhanced CloudWatch Dashboard

**Goal:** Replace singleValue-only dashboard with time-series graphs and per-source drill-down.

**Session prompt:**
```
Enhance the CloudWatch dashboard in `terraform/monitoring.tf`.

Current state: 12 singleValue widgets showing instantaneous values.

Target state: A dashboard with 4 rows:
1. **Pipeline Health** (row 1):
   - Time-series graph: Step Function execution duration (p50, p90, p99) over 30 days
   - Time-series graph: Lambda invocation count by function name over 30 days
   - Stat: Current pipeline status (last execution result)

2. **Data Quality** (row 2):
   - Time-series graph: Quality check pass/fail counts by domain (fx_rates, economic_indicators)
   - Stat: Records quarantined (last 24h)
   - Stat: Quality check failures by severity (CRITICAL vs WARNING)

3. **Data Freshness** (row 3):
   - Time-series graph: Data freshness (latest_date age in hours) over 30 days
   - Stat: Current freshness per source (Frankfurter, ECB, FRED)
   - Time-series graph: Ingestion latency per source

4. **Cost & Operations** (row 4):
   - Stat: Estimated monthly cost
   - Time-series graph: Lambda duration by function over 30 days
   - Stat: Glue job DPU-hours consumed

Keep existing alarm definitions. Only modify the dashboard resource.
Preserve the existing `aws_cloudwatch_dashboard` resource name and structure.
```

**Validation:**
- [ ] `terraform plan` shows only dashboard changes
- [ ] Dashboard renders in CloudWatch console (manual check after deploy)
- [ ] No alarms removed or modified

**Acceptance criteria:** Dashboard has 12+ widgets across 4 rows with time-series graphs.

---

### Day 3 — SLA Monitoring & Composite Alarms

**Goal:** Define pipeline SLA and create composite alarms that track compliance.

**Session prompt:**
```
Add SLA monitoring to FXLake in `terraform/monitoring.tf`.

1. Define the SLA as a composite alarm combining:
   - Pipeline execution succeeds (existing StepFunctionExecutionFailed alarm in OK state)
   - Data freshness < 48 hours (existing StaleFXData alarm in OK state)
   - No CRITICAL quality failures in last 24h (existing DataQualityChecksFailed alarm in OK state)

2. Create `aws_cloudwatch_composite_alarm` resource "pipeline_sla":
   - Alarm rule: all three component alarms must be in OK state
   - Action: SNS notification on ALARM state (SLA breach)

3. Add a custom metric `PipelineSLACompliance` that the validation Lambda publishes:
   - Value 1.0 when all conditions met, 0.0 when any condition fails
   - Namespace: FXLake/SLA
   - Dimensions: Environment=production

4. Update `lambda/lambda_validation_function.py` to publish this metric after existing validation logic.

5. Add tests for the new metric publication in `tests/test_lambda_validation.py`.

6. Add SLA compliance widget to the dashboard (time-series, 30-day view, target line at 99.5%).
```

**Validation:**
- [ ] `make test-all` passes with new tests
- [ ] `terraform plan` shows composite alarm + dashboard update
- [ ] Test coverage remains >= 95%

**Acceptance criteria:** Composite alarm triggers SNS when any SLA component breaches. Validation Lambda publishes SLA metric.

---

### Day 4 — Dead Letter Queue & Automated Recovery

**Goal:** Failed pipeline events are captured in a DLQ for replay, not lost.

**Session prompt:**
```
Add a dead letter queue mechanism for failed Step Functions executions.

1. Create `terraform/dlq.tf`:
   - SQS queue `fxlake-pipeline-dlq` with 14-day retention
   - SQS dead-letter queue policy
   - CloudWatch alarm for messages in DLQ (threshold: 1)

2. Modify Step Functions Fail states in `terraform/step_function.tf`:
   - Each Fail state should invoke a new Lambda (`lambda_dlq_publisher.py`) before failing
   - The DLQ publisher Lambda sends the failed execution context (error, cause, input, execution ARN) to SQS
   - Use a Task state before each Fail state, with Catch that still fails the execution

   Alternative (simpler): Use EventBridge rule on Step Functions execution status change (FAILED, TIMED_OUT) to route to SQS. This avoids modifying the state machine.

   Evaluate both approaches and choose the simpler one. Prefer the EventBridge approach if it captures enough context.

3. Create `lambda/lambda_dlq_publisher.py` (if needed) or EventBridge rule + SQS target.

4. Add a `make replay-dlq` command that reads messages from the DLQ and re-executes the Step Function.

5. Write tests for the DLQ mechanism.

6. Update the dashboard with a DLQ depth widget.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] `terraform plan` shows SQS queue + alarm + EventBridge rule (or Lambda)
- [ ] `make replay-dlq` command works (manual test)

**Acceptance criteria:** Failed executions are captured with full context. Replay mechanism exists.

---

## Phase 2: Storage Layer Upgrade — Apache Iceberg (Days 5-9)

### Day 5 — Iceberg Table Definitions & Terraform

**Goal:** Define Iceberg tables in Glue Data Catalog, replacing the v2 Parquet table definitions.

**Session prompt:**
```
Create Apache Iceberg table definitions in Terraform, replacing the existing Parquet-backed tables.

All v3 work is on the `v3` branch — v2 continues running on `main` unaffected.

1. Create `terraform/iceberg.tf` with two Iceberg tables:

   a. `fx_rates` — Iceberg table in Glue Data Catalog:
      - Columns: date (string), source (string), base_currency (string), target_currency (string), rate (double)
      - Table type: ICEBERG
      - Location: s3://<processed_bucket>/iceberg/fx_rates/
      - Use `open_table_format_input { iceberg_input { metadata_operation = "CREATE" } }`

   b. `economic_indicators` — Iceberg table:
      - Columns: date (string), source (string), series_id (string), value (double)
      - Location: s3://<processed_bucket>/iceberg/economic_indicators/

2. Remove or replace the existing `aws_glue_catalog_table` resources for fx_rates and
   economic_indicators in `terraform/athena.tf`. The v2 Parquet definitions are not needed
   on this branch — v2 runs on `main`.

3. Add Athena workgroup configuration for Iceberg (if needed):
   - Engine version: Athena engine version 3
   - Result configuration pointing to existing athena_results bucket

4. Add IAM permissions for Athena to write to the Iceberg table location.

Research the exact Terraform syntax for Iceberg tables in Glue Data Catalog.
Use Context7 or AWS docs to verify the `aws_glue_catalog_table` configuration for Iceberg.
```

**Validation:**
- [ ] `terraform plan` shows Iceberg table resources
- [ ] `terraform apply` creates tables visible in Glue Data Catalog
- [ ] Athena can query the (empty) Iceberg tables: `SELECT * FROM fx_rates LIMIT 1`

**Acceptance criteria:** Two Iceberg tables exist in Glue Data Catalog on the v3 branch.

---

### Day 6 — Athena CTAS Write Path (FX Rates)

**Goal:** Write FX rates data to the Iceberg table via Athena CTAS, triggered from a Lambda.

**Session prompt:**
```
Create a Lambda function that writes transformed FX rates data to the Iceberg table using Athena CTAS/INSERT INTO.

1. Create `lambda/lambda_iceberg_writer.py`:
   - Receives event with: raw_bucket, raw_key, target_table, database_name
   - Reads raw JSON from S3
   - Constructs an Athena INSERT INTO query that transforms and loads the data
   - Executes the query via Athena StartQueryExecution API
   - Polls for completion (with timeout)
   - Returns query execution ID and status

   The INSERT INTO query should:
   - Read from an external table pointing to the raw JSON (or use Athena's JSON SerDe)
   - Transform: extract date, source, base_currency, target_currency, rate from the nested JSON structure
   - Insert into the Iceberg table

2. Add the Lambda to `terraform/lambda.tf` (or use the lambda_function module).

3. Wire it into Step Functions as a new state AFTER the existing Glue job:
   - State name: "Write-FX-Iceberg"
   - Position: after Glue, before Update-FX-State
   - Catch block routes to existing error handling

4. Write tests using moto's Athena mock (or patch the Athena client).

5. Update `lambda/package_lambdas.sh` to include the new Lambda.

Follow the existing patterns in base.py for logging, error handling, and structured responses.
Use the same FX rates JSON structure that the Frankfurter and ECB handlers produce.
```

**Validation:**
- [ ] `make test-all` passes with new tests
- [ ] `make package` includes the new Lambda zip
- [ ] `terraform plan` shows new Lambda + Step Functions changes
- [ ] Test coverage >= 95%

**Acceptance criteria:** Lambda can execute Athena INSERT INTO for FX rates. Wired into Step Functions as optional step.

---

### Day 7 — Athena CTAS Write Path (Economic Indicators) & Quality Integration

**Goal:** Extend Iceberg writes to economic indicators. Integrate quality checks with the new write path.

**Session prompt:**
```
Extend the Iceberg writer to handle economic indicators and integrate quality checks.

1. Update `lambda/lambda_iceberg_writer.py` to handle both domains:
   - Accept a `domain` parameter in the event: "fx_rates" or "economic_indicators"
   - Route to the correct Iceberg table and transformation query based on domain
   - FRED data (economic domain) has a different schema: date, source, series_id, value

2. Run quality checks BEFORE writing to Iceberg:
   - Import and use the existing quality.py check functions
   - If CRITICAL failure: quarantine the raw file (existing pattern), skip Iceberg write, return failure status
   - If WARNING: log, publish metric, proceed with Iceberg write
   - This parallels the existing quality enforcement in glue_transform.py

3. Add the economic indicators write path to Step Functions:
   - State name: "Write-Economic-Iceberg"
   - Position: after "Write-FX-Iceberg", before Update-ECB-State
   - Same optional/non-blocking pattern as FX

4. Write quality report to S3 (same location as Glue currently writes: {domain}/quality_reports/).

5. Add tests for:
   - Economic indicators Iceberg write
   - Quality check integration (CRITICAL blocks write, WARNING allows write)
   - Both domains end-to-end

Reuse quality.py functions directly — do NOT rewrite quality checks.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] Quality checks run before Iceberg writes
- [ ] CRITICAL failures prevent Iceberg writes (test coverage for this path)
- [ ] Test coverage >= 95%

**Acceptance criteria:** Both domains write to Iceberg with quality gates. Quality.py reused without modification.

---

### Day 8 — Backfill Validation & Data Integrity

**Goal:** Run a historical backfill on the v3 branch and validate Iceberg table contents against known v2 output.

**Session prompt:**
```
Validate the v3 Iceberg pipeline by running a backfill and checking data integrity.

Since v3 is developed on its own branch (no dual-write), validation is done by
running a backfill for a known date range and verifying the Iceberg tables
contain the expected data.

1. Use `terraform apply` on the v3 branch:
   - Ensure Iceberg tables are created
   - Ensure the Iceberg writer Lambda is deployed

2. Run a backfill for a known date range:
   - `make backfill START=2024-01-01 END=2024-01-31`
   - This exercises the full pipeline: ingestion → Glue → Iceberg write

3. Create `lambda/lambda_data_validator.py`:
   - Runs Athena queries against the Iceberg tables
   - Validates: row counts per source, date range coverage, no nulls in required columns
   - Compares against expected values (hardcoded for the backfill range, or computed from raw JSON files in S3)
   - Publishes CloudWatch metric: DataValidation (1=pass, 0=fail)

4. Add a manual trigger:
   - `make validate-iceberg` command in Makefile
   - Invokes the validation Lambda directly via AWS CLI

5. Write tests for the validation logic (mock Athena results).

6. Validation queries:
   ```sql
   -- Row count and date range
   SELECT source, COUNT(*) as rows, MIN(date) as min_date, MAX(date) as max_date
   FROM fx_rates
   GROUP BY source;

   -- Null check
   SELECT COUNT(*) as null_rows FROM fx_rates WHERE rate IS NULL OR date IS NULL;
   ```
```

**Validation:**
- [ ] `make test-all` passes
- [ ] Backfill populates Iceberg tables with expected row counts
- [ ] Validation queries return expected results (manual check after deploy)

**Acceptance criteria:** Backfill-based validation confirms Iceberg tables contain correct data.

---

### Day 9 — Iceberg Compaction & Maintenance

**Goal:** Schedule Iceberg table maintenance (compaction, snapshot expiry) to prevent small file accumulation.

**Session prompt:**
```
Add Iceberg table maintenance automation.

1. Create `lambda/lambda_iceberg_maintenance.py`:
   - Runs Athena OPTIMIZE statements for compaction:
     `OPTIMIZE fx_rates_v3 REWRITE DATA USING BIN_PACK`
   - Runs vacuum for snapshot expiry:
     `VACUUM fx_rates_v3`
   - Handles both tables (fx_rates_v3, economic_indicators_v3)
   - Logs compaction stats (files before/after, duration)

2. Add an EventBridge rule for weekly maintenance:
   - Schedule: `cron(0 6 ? * SUN *)` (Sunday 6 AM UTC)
   - Target: the maintenance Lambda
   - Separate from the daily pipeline trigger

3. Add Terraform resources in `terraform/iceberg.tf`:
   - EventBridge rule and target
   - Lambda function (use the lambda_function module)
   - IAM permissions for Athena OPTIMIZE and VACUUM

4. Add CloudWatch alarm: MaintenanceJobFailed (threshold: 1 error in 1 evaluation period)

5. Write tests for the maintenance Lambda (mock Athena client).

6. Add maintenance widget to the CloudWatch dashboard.

Keep this Lambda simple — it's a thin wrapper around Athena SQL statements.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] `terraform plan` shows EventBridge rule + Lambda + alarm
- [ ] Maintenance Lambda can be invoked manually (`make compact-iceberg`)
- [ ] Test coverage >= 95%

**Acceptance criteria:** Weekly automated compaction. Manual compaction available via Makefile.

---

## Phase 3: Transformation Layer Modernization — dbt (Days 10-14)

### Day 10 — dbt Core Project Setup

**Goal:** Initialize a dbt project configured to run against Athena with Iceberg tables.

**Session prompt:**
```
Set up a dbt Core project for FXLake transformations.

1. Initialize dbt project structure:
   ```
   dbt/
   ├── dbt_project.yml
   ├── profiles.yml          (template — actual creds via env vars)
   ├── models/
   │   ├── staging/
   │   │   ├── stg_fx_rates.sql
   │   │   ├── stg_economic_indicators.sql
   │   │   └── schema.yml    (column descriptions, tests)
   │   └── marts/
   │       ├── fct_fx_rates.sql
   │       ├── fct_economic_indicators.sql
   │       └── schema.yml
   ├── macros/
   ├── tests/
   │   └── generic/
   └── seeds/
   ```

2. Configure dbt for Athena:
   - Use `dbt-athena-community` adapter
   - Profile points to the existing Athena workgroup
   - Database: existing Glue Data Catalog database
   - S3 staging directory for Athena query results

3. Create staging models:
   - `stg_fx_rates.sql`: SELECT from raw JSON external table, cast types, rename columns
   - `stg_economic_indicators.sql`: same pattern for economic data

4. Create mart models:
   - `fct_fx_rates.sql`: SELECT from stg_fx_rates with business logic (currency pair normalization, duplicate handling)
   - `fct_economic_indicators.sql`: SELECT from stg_economic_indicators

5. Add `schema.yml` with:
   - Column descriptions for all columns
   - not_null tests on required columns
   - accepted_values tests on source columns
   - unique tests on natural keys

6. Add dbt dependencies to `pyproject.toml` (as dev dependencies):
   - dbt-core
   - dbt-athena-community

7. Add Makefile targets:
   - `make dbt-compile`: dbt compile
   - `make dbt-run`: dbt run
   - `make dbt-test`: dbt test
   - `make dbt-docs`: dbt docs generate

Do NOT modify any existing Lambda, Glue, or Terraform code.
This is a standalone dbt project that queries existing tables.
Research the dbt-athena-community adapter configuration using Context7.
```

**Validation:**
- [ ] `make dbt-compile` succeeds
- [ ] `dbt debug` connects to Athena
- [ ] Models reference existing table structures
- [ ] schema.yml describes all columns

**Acceptance criteria:** dbt project compiles and connects to Athena. Staging and mart models defined.

---

### Day 11 — dbt Models with Quality Tests

**Goal:** Create dbt models that replicate the current Glue transformation logic and add schema-level tests.

**Session prompt:**
```
Develop dbt models that replicate and enhance the current glue_transform.py logic.

1. Enhance staging models to handle the transformation logic currently in Glue:
   - `stg_fx_rates.sql`:
     - Parse raw JSON structure (nested rates object)
     - Detect source from filename prefix or metadata (frankfurter vs ecb)
     - Flatten to: date, source, base_currency, target_currency, rate
     - Filter: exclude null rates, exclude non-positive rates
   - `stg_economic_indicators.sql`:
     - Parse FRED JSON structure
     - Flatten to: date, source, series_id, value
     - Filter: exclude null values, exclude sentinel "." values

2. Add data quality tests in `schema.yml`:
   - not_null on: date, source, rate/value
   - positive_values on: rate, value (custom generic test)
   - accepted_values on: source (frankfurter, ecb, fred)
   - unique combination: (date, source, target_currency) for FX rates
   - unique combination: (date, source, series_id) for economic indicators
   - rate_range: 0.0001 to 1000 for FX rates (custom generic test)

3. Create custom generic tests in `dbt/tests/generic/`:
   - `test_positive_values.sql`
   - `test_rate_in_range.sql`
   These replicate the quality.py checks as SQL tests.

4. Create a dbt macro for quality report generation:
   - After model run, generate a JSON quality report (similar to quality.py's build_quality_report)
   - Store in the same S3 location as current quality reports

5. Add dbt sources definition (`models/staging/sources.yml`):
   - Source: raw JSON tables in Glue Data Catalog
   - Freshness checks: warn_after 24 hours, error_after 48 hours

Map every check from quality.py's run_fx_checks() and run_economic_checks() to a dbt test.
Document in a comment which quality.py check each dbt test replaces.
```

**Validation:**
- [ ] `make dbt-run` executes models successfully
- [ ] `make dbt-test` runs all quality tests
- [ ] All 6 quality.py check categories have dbt test equivalents
- [ ] Source freshness checks work

**Acceptance criteria:** dbt models replicate Glue transformations. All quality checks have dbt equivalents.

---

### Day 12 — dbt Integration with Step Functions

**Goal:** Wire dbt into the Step Functions pipeline as a replacement for (or supplement to) the Glue job.

**Session prompt:**
```
Integrate dbt execution into the Step Functions pipeline.

1. Create `lambda/lambda_dbt_runner.py`:
   - Executes dbt commands via subprocess (dbt run, dbt test)
   - Packages the dbt project as a Lambda layer or includes it in the Lambda zip
   - Returns: run status, test results summary, model execution times
   - Handles dbt failures: CRITICAL (test failures) vs WARNING (deprecation warnings)

   Alternative approach: If Lambda packaging is too complex for dbt, use an ECS Fargate task
   or CodeBuild project to run dbt. Evaluate which is simpler.

   Simplest approach: Run dbt via Athena directly (the Lambda constructs and executes the
   SQL that dbt would generate, without actually running dbt as a subprocess). This avoids
   packaging dbt in Lambda entirely.

   Evaluate all three approaches. Choose the simplest that works.

2. Add the dbt execution to Step Functions:
   - Position: after Glue job (dual-run during transition)
   - State name: "dbt-Transform"
   - Catch/Retry following existing patterns
   - Non-blocking during dual-run period

3. Add CI integration:
   - `dbt compile` in the CI workflow (validates SQL syntax)
   - `dbt test` with a test profile (against mock/seed data if possible)

4. Write tests for the dbt runner Lambda.

5. Update `lambda/package_lambdas.sh` if needed.

Prioritize simplicity. If dbt-in-Lambda is too complex, document the alternative and implement
the simplest working approach.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] dbt execution completes in the pipeline context
- [ ] CI includes dbt validation
- [ ] Test coverage >= 95%

**Acceptance criteria:** dbt models execute in the pipeline. CI validates dbt compilation.

---

### Day 13 — dbt Lineage & Documentation

**Goal:** Generate and expose data lineage and documentation from the dbt project.

**Session prompt:**
```
Set up dbt documentation and lineage visualization for FXLake.

1. Add comprehensive documentation to dbt models:
   - Every model has a description in schema.yml
   - Every column has a description
   - Add model-level docs blocks for complex transformation logic
   - Reference upstream/downstream dependencies via ref()

2. Generate dbt docs:
   - `dbt docs generate` produces the documentation site
   - `dbt docs serve` runs locally for review
   - Add `make dbt-docs` target

3. Create a lineage diagram asset:
   - Use dbt's built-in lineage graph
   - OR create a Python script (like existing assets/cloud-architecture.py) that generates
     a lineage diagram from dbt's manifest.json
   - Show: raw sources → staging models → mart models → Iceberg tables

4. Add model tags for categorization:
   - staging: tag "staging"
   - marts: tag "marts"
   - source-specific: tag "frankfurter", "ecb", "fred"

5. Document the dbt project in the main README:
   - Add a "Transformation Layer (dbt)" section
   - List available models and their purpose
   - Link to generated docs

Do NOT modify CLAUDE.md — that will be updated separately after all phases complete.
```

**Validation:**
- [ ] `make dbt-docs` generates documentation site
- [ ] Lineage graph shows correct dependencies
- [ ] All models and columns have descriptions

**Acceptance criteria:** Complete dbt documentation with lineage. Accessible via `make dbt-docs`.

---

### Day 14 — Glue-to-dbt Migration Cutover

**Goal:** Complete the migration from Glue Python Shell to dbt. Verify data equivalence.

**Session prompt:**
```
Complete the migration from Glue to dbt transformations.

1. Validate data equivalence:
   - Run the data validator (from Day 8) comparing dbt-produced Iceberg output against expected values
   - Verify: row counts match, date ranges match, sample values match
   - Document any expected differences (e.g., column ordering, null handling)

2. Update Step Functions to make dbt the primary transformation:
   - Replace the Glue job state with dbt-Transform
   - dbt-Transform becomes the primary transformation step
   - Adjust ResultPath and error handling accordingly

3. Remove the Glue job from the pipeline:
   - Remove or disable the Glue Terraform resources (the v3 branch replaces Glue entirely)
   - Keep quality.py as a standalone module — it's used by the Iceberg writer Lambda

4. Update Athena validation query to point to the Iceberg tables.

5. Update monitoring:
   - Replace Glue-specific alarms with dbt execution alarms
   - Update dashboard widgets to reflect dbt metrics

6. Run full test suite:
   - `make test-all` for existing tests
   - `make dbt-test` for dbt tests
   - Integration test: trigger a full pipeline execution on the v3 branch

Do NOT delete glue_transform.py or quality.py source files yet — they remain in git history.
quality.py is still actively used by the Iceberg writer Lambda.
```

**Validation:**
- [ ] Data equivalence verified via validation Lambda
- [ ] Pipeline runs end-to-end with dbt as primary transformation
- [ ] Glue job removed from Step Functions
- [ ] All tests pass
- [ ] Test coverage >= 95%

**Acceptance criteria:** dbt is the primary transformation engine. Pipeline runs end-to-end on the v3 branch.

---

## Phase 4: Data Governance & Discovery (Days 15-17)

### Day 15 — Enhanced Glue Data Catalog

**Goal:** Enrich the Glue Data Catalog with descriptions, tags, and classification for data discovery.

**Session prompt:**
```
Enhance the Glue Data Catalog for data discovery and governance.

1. Update `terraform/athena.tf` (or create `terraform/catalog.tf`):
   - Add table descriptions to all Glue catalog tables
   - Add column descriptions (using `parameters` or `comment` in SerDe)
   - Add table tags: domain, source, owner, update_frequency, data_classification

2. Create a Glue Crawler for schema detection (optional, evaluate necessity):
   - If Iceberg metadata is sufficient for schema management, skip the crawler
   - If raw JSON schema needs detection, add a crawler for the raw bucket

3. Add table properties for data governance:
   - `classification = "financial-data"`
   - `owner = "fxlake-pipeline"`
   - `update_frequency = "daily"`
   - `retention_period = "365 days"`

4. Create a data dictionary document:
   - `docs/data_dictionary.md`
   - List all tables, columns, types, descriptions, sources
   - Include example queries for common use cases
   - Cross-reference with dbt schema.yml

5. Add Terraform outputs for catalog ARNs and table names:
   - Useful for cross-stack references and external consumers
```

**Validation:**
- [ ] `terraform plan` shows catalog enrichment
- [ ] Glue Data Catalog shows descriptions and tags (manual check)
- [ ] Data dictionary document is complete and accurate

**Acceptance criteria:** Glue Data Catalog enriched with descriptions and tags. Data dictionary created.

---

### Day 16 — Schema Contracts & Validation

**Goal:** Define explicit schema contracts between pipeline stages.

**Session prompt:**
```
Implement schema contracts for FXLake data interfaces.

1. Create schema contract definitions in `schemas/`:
   ```
   schemas/
   ├── raw/
   │   ├── frankfurter_response.json    (JSON Schema for Frankfurter API response)
   │   ├── ecb_response.json            (JSON Schema for ECB SDMX response)
   │   └── fred_response.json           (JSON Schema for FRED API response)
   └── processed/
       ├── fx_rates.json                (JSON Schema for processed FX rates)
       └── economic_indicators.json     (JSON Schema for processed economic indicators)
   ```

2. Add schema validation to ingestion Lambdas:
   - After fetching raw data, validate against the JSON Schema
   - Use `jsonschema` library (add to lambda/requirements.txt)
   - On schema violation: log error with details, raise ValueError
   - This catches upstream API changes early

3. Add schema validation to dbt models:
   - dbt contract tests in schema.yml (column names, types)
   - Reference the JSON Schema definitions in dbt docs

4. Create a schema versioning strategy:
   - Schema files are versioned in git
   - Breaking changes require a new schema version
   - Document the versioning policy in schemas/README.md

5. Write tests for schema validation:
   - Valid responses pass validation
   - Invalid responses (missing fields, wrong types) fail with clear errors
   - Test each source's schema

This is the "data contract" layer — it defines what each stage expects
from its upstream and guarantees to its downstream.
```

**Validation:**
- [ ] `make test-all` passes with schema validation tests
- [ ] Schema files cover all API response formats
- [ ] Ingestion Lambdas validate responses against schemas
- [ ] Test coverage >= 95%

**Acceptance criteria:** Explicit schema contracts for all data interfaces. Validation integrated into ingestion.

---

### Day 17 — Access Controls & Audit Logging

**Goal:** Add fine-grained access controls and audit logging for data governance.

**Session prompt:**
```
Add access controls and audit logging enhancements.

1. Create IAM policies for data consumers in `terraform/iam.tf`:
   - Read-only policy: Athena query + S3 read on processed bucket
   - Admin policy: full pipeline control
   - Analyst policy: Athena query only (no S3 direct access)
   - These are policy documents (not attached to users — consumers create their own roles)

2. Add S3 bucket policies:
   - Processed bucket: deny unencrypted uploads
   - Raw bucket: deny public access (belt-and-suspenders with existing block)
   - Quarantine bucket: restrict access to pipeline role only

3. Enhance CloudTrail logging:
   - Ensure data events are logged for S3 (read/write on processed bucket)
   - Add CloudTrail Insights for anomaly detection (if free tier allows)

4. Add access logging to S3 buckets:
   - Server access logging on processed and raw buckets
   - Logs go to the existing cloudtrail_logs bucket (with a different prefix)

5. Create an audit query template:
   - Athena query that joins CloudTrail logs with pipeline execution data
   - Answers: "who accessed what data, when, and from where?"
   - Save as `docs/queries/audit_trail.sql`

6. Write tests for IAM policy documents (validate JSON structure).

Keep this focused on infrastructure. Do NOT modify Lambda or Glue code.
```

**Validation:**
- [ ] `terraform plan` shows IAM policies + S3 bucket policies
- [ ] No existing permissions broken
- [ ] Audit query template returns expected results (manual test)

**Acceptance criteria:** IAM policies defined for consumer personas. S3 bucket policies hardened. Audit logging enhanced.

---

## Phase 5: Advanced Features (Days 18-20)

### Day 18 — Cross-Source Validation

**Goal:** Add validation checks that span multiple data sources (e.g., Frankfurter vs ECB rates should be close for the same currency pair on the same date).

**Session prompt:**
```
Implement cross-source data validation for FXLake.

1. Create `lambda/lambda_cross_validator.py`:
   - Runs after all sources are ingested and transformed
   - Executes Athena queries comparing data across sources:
     a. FX rate consistency: Frankfurter vs ECB rates for overlapping currency pairs
        - Alert if rate difference > 1% for the same date + currency pair
     b. Temporal consistency: all sources should have data for the same date range (±1 day)
     c. Volume consistency: record counts per source should be within expected ranges

2. Add cross-validation to Step Functions:
   - New state "Cross-Source-Validation" after Athena validation step
   - Publishes CloudWatch metrics: CrossSourceDiscrepancy (count of mismatches)
   - WARNING severity (does not block pipeline) — these are data quality insights, not gates

3. Create dbt tests for cross-source validation:
   - `tests/cross_source_rate_consistency.sql`
   - `tests/cross_source_temporal_alignment.sql`

4. Add dashboard widgets:
   - Cross-source discrepancy count (time-series)
   - Rate deviation histogram (stat widget showing max deviation)

5. Write tests for the cross-validator Lambda (mock Athena results).

These checks detect upstream data issues (e.g., ECB changes its rate methodology)
that single-source quality checks can't catch.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] Cross-validation runs without errors on existing data
- [ ] Discrepancy metrics published to CloudWatch
- [ ] Test coverage >= 95%

**Acceptance criteria:** Cross-source validation catches rate discrepancies and temporal misalignment.

---

### Day 19 — Anomaly Detection & Alerting

**Goal:** Add statistical anomaly detection to catch unusual data patterns.

**Session prompt:**
```
Implement statistical anomaly detection for FXLake data.

1. Create `lambda/lambda_anomaly_detector.py`:
   - Queries historical data from Iceberg tables (last 30 days)
   - Computes per-currency-pair statistics: mean, stddev, min, max
   - Checks today's rates against historical distribution:
     - Z-score > 3.0: ALERT (publishes metric + SNS notification)
     - Z-score > 2.0: WARNING (publishes metric only)
   - Handles cold start (< 30 days of history): skip anomaly detection, log warning

2. For economic indicators:
   - Track month-over-month change for each series
   - Alert if change exceeds 3 standard deviations from historical norm

3. Add to Step Functions:
   - State "Anomaly-Detection" after Cross-Source-Validation
   - Non-blocking: WARNING severity, pipeline continues on alert

4. CloudWatch metrics:
   - AnomalyDetected (count, by source and currency pair)
   - ZScore (max z-score observed, by source)

5. Dashboard: anomaly detection row with z-score time series

6. Write tests:
   - Normal data: no anomalies detected
   - Outlier data: anomaly correctly flagged
   - Cold start: graceful skip

Keep the statistics simple (z-score). Don't use ML libraries.
This is a lightweight early-warning system, not a forecasting engine.
```

**Validation:**
- [ ] `make test-all` passes
- [ ] Anomaly detection works on synthetic test data
- [ ] Metrics published correctly
- [ ] Test coverage >= 95%

**Acceptance criteria:** Z-score anomaly detection on FX rates and economic indicators. Alerting via CloudWatch + SNS.

---

### Day 20 — Self-Healing, Documentation & Final Polish

**Goal:** Add self-healing capabilities, update all documentation, and prepare for production handoff.

**Session prompt:**
```
Final day: self-healing, documentation updates, and production readiness.

1. Self-healing mechanisms:
   a. Auto-retry from DLQ: Lambda triggered by SQS messages in the DLQ
      - Checks if the failure is transient (API timeout, throttle)
      - If transient and < 3 retries: re-execute the Step Function with backoff
      - If permanent or max retries exceeded: alert via SNS, keep in DLQ
   b. Stale data auto-backfill: if data freshness exceeds 48 hours and pipeline last ran > 24 hours ago,
      automatically trigger a backfill execution for missing dates
      - EventBridge rule checks hourly
      - Lambda reads DynamoDB state, compares to today, triggers backfill if gap > 2 days

2. Update CLAUDE.md with v3 architecture:
   - Add Iceberg tables section
   - Add dbt section (commands, project structure)
   - Update architecture diagram description
   - Add new Makefile targets
   - Update test coverage table
   - Add new Terraform files table entries
   - Add new Lambda functions to the handler table

3. Update README.md:
   - Add v3 features section
   - Update architecture diagram (generate new one with assets/ script)
   - Add dbt commands to the usage section
   - Update the data quality section with cross-source validation

4. Generate updated architecture diagram:
   - Update `assets/cloud-architecture.py` to include Iceberg, dbt, DLQ, anomaly detection
   - Run `uv run assets/cloud-architecture.py` to generate new diagram

5. Create ADRs for v3 decisions:
   - ADR-005: Apache Iceberg for open table format
   - ADR-006: dbt Core for transformation layer
   - ADR-007: Athena CTAS over Glue Spark for Iceberg writes
   (Content is in decision_log_v3.md — expand into full ADR format)

6. Final validation:
   - `make test-all` — all tests pass, coverage >= 95%
   - `make dbt-test` — all dbt tests pass
   - `terraform plan` — no unexpected changes
   - `make lint` — no linting errors
   - Manual: trigger a full pipeline execution and verify end-to-end

Run ALL validation steps and report results.
```

**Validation:**
- [ ] `make test-all` passes, coverage >= 95%
- [ ] `make dbt-test` passes
- [ ] `terraform plan` clean
- [ ] All documentation updated
- [ ] Architecture diagram regenerated
- [ ] ADRs written
- [ ] End-to-end pipeline execution succeeds

**Acceptance criteria:** Production-ready v3 with self-healing, comprehensive documentation, and full test coverage.

---

## Risk Mitigation Matrix

| Risk | Phase | Likelihood | Mitigation | Fallback |
|------|-------|-----------|------------|----------|
| Athena CTAS too expensive at scale | 2 | Medium | Benchmark before committing. Set billing alarm at $5/month for Athena. | Migrate to Glue Spark with Iceberg connector. |
| dbt-athena-community adapter immaturity | 3 | Medium | Test with all model types before Day 14 cutover on v3 branch. | Stay with Glue Python Shell, add dbt for docs/lineage only (not execution). |
| Iceberg table creation fails in Terraform | 2 | Low | Research exact syntax on Day 5. Use AWS console as reference implementation. | Create tables via AWS CLI/console, import into Terraform state. |
| Lambda packaging too complex for dbt | 3 | Medium | Evaluate ECS Fargate or CodeBuild alternatives on Day 12. | Run dbt from CI/CD only (not in-pipeline). |
| Schema contracts break existing ingestion | 4 | Low | Add validation in WARNING mode first. Only promote to ERROR after 1 week. | Make schema validation optional via env var. |
| Self-healing creates infinite retry loops | 5 | Medium | Hard limit: 3 retries per DLQ message. Circuit breaker after 5 failures in 1 hour. | Disable auto-retry, keep manual replay only. |
| Total effort exceeds 20 days | All | Medium | Each phase is independently valuable. Cut Phase 5 (Advanced) if behind. | Ship Phases 1-3 as v3.0, defer Phases 4-5 to v3.1. |

---

## Dependencies & Prerequisites

| Day | Prerequisite | Must Be Done Before |
|-----|-------------|-------------------|
| 5 | Athena engine v3 enabled in workgroup | Day 6 (Iceberg writes) |
| 6 | Iceberg tables created (Day 5) | Day 7 (economic domain) |
| 10 | `dbt-athena-community` adapter tested locally | Day 11 (dbt models) |
| 12 | Backfill validation passing (Day 8) | Day 14 (cutover) |
| 14 | All dbt tests passing (Day 11) | Day 14 (Glue replacement) |
| 16 | dbt schema.yml complete (Day 11) | Day 16 (schema contracts) |
| 20 | All previous phases complete | Day 20 (final polish) |

---

## Daily Time Budget

Each day targets 2-4 hours of focused work:

| Activity | Time |
|----------|------|
| Session prompt + context loading | 10 min |
| Implementation (Claude Code + review) | 90-150 min |
| Testing & validation | 30-60 min |
| Commit & documentation | 15 min |
| **Total** | **2.5-4 hours** |

If a day's work takes less time, use the remainder for documentation or test hardening. If it takes longer, split the day's work into two sessions rather than rushing.
