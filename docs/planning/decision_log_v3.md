# FXLake v3 — Decision Log

**Date:** 2026-04-22
**Purpose:** Document key architectural decisions for v3 with rationale, alternatives considered, and consequences.

---

## DL-001: Apache Iceberg over Delta Lake and Hudi

**Decision:** Use Apache Iceberg as the open table format for FXLake v3.

**Context:** v2 uses plain Parquet files managed by partition projection in Athena. This lacks ACID transactions, schema evolution, time travel, and file-level pruning. An open table format is needed to close these gaps.

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Apache Iceberg** | Native Athena GA support, Terraform-native via `aws_glue_catalog_table`, hidden partitioning, strong community momentum | Write path requires Spark or Athena CTAS (not Glue Python Shell) | **Selected** |
| **Delta Lake** | Mature ecosystem, delta-rs Python library, OPTIMIZE compaction | Not natively supported in Athena (requires manifest symlinks), Databricks-centric governance | Rejected — Athena is our primary query engine |
| **Apache Hudi** | Built-in merge-on-read, incremental queries | Declining community momentum vs Iceberg, limited Athena support (read-only), complex configuration | Rejected — poor AWS-native fit |
| **Stay with plain Parquet** | Zero migration effort, current system works | No ACID, no schema evolution, small file problem will worsen | Rejected — doesn't address production gaps |

**Consequences:**
- Athena queries gain file-level pruning via Iceberg metadata (faster, cheaper at scale)
- Schema changes become additive (add column, rename, reorder) without rewriting history
- Time travel enables "as-of" queries for auditing and debugging
- Write path must change from Polars→S3 to Athena CTAS or Glue Spark→Iceberg
- Compaction must be scheduled (Iceberg doesn't compact automatically)

---

## DL-002: Athena CTAS over Glue Spark for Iceberg writes

**Decision:** Use Athena CTAS/INSERT INTO statements as the primary write path for Iceberg tables, rather than migrating to Glue Spark.

**Context:** Glue Python Shell (current) cannot write Iceberg format. Two options: (1) Athena CTAS reads raw JSON from S3 and writes directly to Iceberg tables, or (2) Glue Spark job with IcebergConnector handles the transformation.

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Athena CTAS/INSERT** | No DPU cost (pay per TB scanned), serverless, SQL-based, native Iceberg support | Limited transformation power (SQL only), $5/TB scanned pricing | **Selected** (for current data volumes) |
| **Glue Spark + Iceberg** | Full transformation power, native Iceberg connector, handles large datasets | Minimum 2 DPU ($0.44/DPU-hour) — 32x more expensive than Python Shell, Spark cold start | Rejected — overkill for <1MB/day |
| **PyIceberg from Lambda** | Pure Python, no Spark/Athena dependency | Immature write support, no Glue catalog integration, untested at this scale | Rejected — too early |
| **Keep Glue Python Shell + raw Parquet, Iceberg only for catalog** | Minimal change | Doesn't actually provide ACID writes or time travel | Rejected — half-measure |

**Consequences:**
- Transformation logic migrates from imperative Python (Polars) to SQL (dbt models on Athena)
- Quality checks need a new integration point (dbt tests + preserved quality.py for complex checks)
- Cost model changes: per-TB-scanned instead of per-DPU-hour (favorable at current volumes)
- If data volumes grow past ~100MB/day, reassess in favor of Glue Spark
- Polars transformation logic (source detection, domain routing) must be re-expressed in SQL

---

## DL-003: dbt Core over imperative Polars transforms

**Decision:** Introduce dbt Core for transformation modularity, replacing the monolithic glue_transform.py with SQL-based models.

**Context:** glue_transform.py is 404 lines handling domain routing, quality enforcement, partitioning, and metric publishing. It works but doesn't support lineage, isn't modular, and couples all sources into one failure domain.

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **dbt Core (free)** | SQL-based models, built-in lineage, schema tests, documentation generation, large ecosystem | Learning curve, additional tool in the stack, SQL-only transformations | **Selected** |
| **dbt Cloud** | Managed scheduling, IDE, CI integration | $100+/month, overkill for solo developer, duplicate orchestration with Step Functions | Rejected — cost and complexity |
| **Refactor glue_transform.py** | No new tools, Python expertise preserved | Still monolithic at scale, no lineage, no built-in testing framework | Rejected — doesn't address root causes |
| **AWS Glue DataBrew** | Visual, no-code transformations | Limited customization, vendor lock-in, no lineage, no version control | Rejected — poor fit for engineering workflow |

**Consequences:**
- Each source/domain gets its own dbt model (modular, independently testable)
- Lineage is automatically tracked via dbt's ref() function
- Schema contracts via schema.yml replace implicit schema definitions
- dbt tests supplement quality.py checks (not replace — complex statistical checks stay in Python)
- CI must include `dbt compile` and `dbt test` steps
- quality.py remains as a standalone module for CRITICAL/WARNING checks that can't be expressed as dbt tests

---

## DL-004: Preserve Step Functions orchestration

**Decision:** Keep Step Functions as the pipeline orchestrator for v3 rather than migrating to Airflow, Dagster, or Prefect.

**Context:** Step Functions orchestrates the 9-stage pipeline with parallel ingestion, conditional branching, retry/catch, and saga-pattern state management. Some v3 planning considered whether a more feature-rich orchestrator would be needed.

**Rationale for preserving:**
- Step Functions handles the current orchestration pattern well (parallel fan-out, conditional routing, error handling)
- $0.025/1000 state transitions — effectively free for a daily pipeline
- Native AWS integration (Lambda, Glue, Athena, EventBridge) without adapters
- Visual execution history for debugging
- Saga pattern for DynamoDB state management is already implemented and tested
- Migrating to Airflow/Dagster would require running a scheduler (EC2/ECS/Fargate), adding cost and operational burden

**When to reconsider:**
- If the pipeline needs cross-pipeline dependencies (DAG of DAGs)
- If the pipeline needs human-in-the-loop approval steps beyond what Step Functions provides
- If the number of sources exceeds 50+ and the ASL definition becomes unmanageable

---

## DL-005: Direct Iceberg migration on a v3 branch (no dual-write)

**Decision:** Build the Iceberg storage layer on a dedicated `v3` branch and replace v2 Parquet tables directly upon merge to main — no dual-write period.

**Context:** There are no downstream consumers of the current Parquet-backed Athena tables. The v2 pipeline runs on `main` and remains fully operational while all v3 work happens on a separate branch. Individual implementation sessions merge into the `v3` branch; once validated end-to-end, `v3` merges to `main`.

**Rationale:**
- No consumers means no migration risk — nothing reads the v2 tables externally
- Branch-based isolation gives the same rollback safety as dual-write (revert the merge or keep running `main`)
- Avoids the complexity and cost of maintaining two write paths simultaneously
- Simpler CI: only one table format to validate per branch

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Dual-write** | Safe consumer migration, side-by-side validation | No consumers to migrate, doubles storage cost and CI complexity for no benefit | Rejected |
| **Direct replacement on main** | Simplest | Blocks main during development, no rollback if Iceberg has issues | Rejected |
| **v3 branch with direct replacement** | Clean isolation, zero impact on v2, simple merge when ready | Must validate thoroughly before merge | **Selected** |

**Consequences:**
- All v3 Terraform, Lambda, dbt, and test changes live on the `v3` branch until ready
- v2 Parquet table definitions are replaced (not kept alongside) by Iceberg table definitions in the `v3` branch
- Merge to `main` is the cutover point — one `terraform apply` switches the storage layer
- Rollback: revert the merge commit and re-apply v2 Terraform
- Day 8 validation compares a backfill on the `v3` branch against known v2 output, not a live dual-write

---

## DL-006: SLA monitoring via CloudWatch composite alarms

**Decision:** Implement SLA monitoring using CloudWatch composite alarms and custom metrics rather than an external monitoring service.

**Context:** v2 has 11 alarms monitoring individual failure conditions but no unified SLA view (e.g., "data freshness < 24 hours, 99.5% of the time").

**Alternatives considered:**

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **CloudWatch composite alarms** | Native AWS, no additional cost for composites, integrates with existing alarms | Limited SLA reporting (no SLI/SLO dashboards natively) | **Selected** |
| **Datadog** | Rich SLO tracking, APM integration | $15/host/month minimum, overkill for serverless | Rejected — cost |
| **Grafana Cloud (free tier)** | Good dashboarding, SLO support | Requires CloudWatch data export, additional integration | Rejected — complexity for solo dev |

**Consequences:**
- Composite alarm combines: pipeline execution success + data freshness + quality pass rate
- Custom metric `PipelineSLA` published after each execution with dimensions for drill-down
- Dashboard enhanced with time-series graphs (not just singleValue widgets)
- SNS topic for SLA breach notification (reuses existing topic)

---

## DL-007: Cost attribution via AWS resource tags

**Decision:** Implement cost attribution using consistent AWS resource tags (`project`, `environment`, `component`, `source`) rather than separate AWS accounts or AWS Organizations.

**Context:** v2 resources have no tagging strategy. All costs appear as undifferentiated charges in the AWS bill.

**Rationale:**
- Tags are free, require no infrastructure changes
- AWS Cost Explorer supports tag-based filtering and grouping
- Terraform `default_tags` in the provider block applies tags to all resources automatically
- Budget alerts can be scoped to tagged resources

**Consequences:**
- All Terraform resources get `project = "fxlake"`, `environment = "production"`, `component = "<service>"` tags
- Per-source cost visibility via `source` tag on Lambda functions and Glue jobs
- Monthly cost report can be generated from Cost Explorer
- Budget alarm set at $10/month (generous for current usage, catches runaway costs)
