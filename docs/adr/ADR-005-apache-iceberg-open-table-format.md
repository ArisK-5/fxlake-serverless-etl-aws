# ADR-005: Apache Iceberg for open table format

**Status:** Accepted
**Date:** 2026-04-22
**Supersedes:** ADR-001 (Polars over PySpark) — Glue Python Shell removed; Iceberg tables replace plain Parquet

## Context

v2 uses plain Parquet files managed by partition projection in Athena. This lacks ACID transactions, schema evolution, time travel, and file-level pruning. As data volume grows and the pipeline matures, these gaps become production risks:

- **No ACID writes** — concurrent or failed writes can leave partial data visible to queries
- **No schema evolution** — adding a column requires rewriting all existing Parquet files
- **No time travel** — debugging requires S3 versioning and manual reconstruction
- **Small file problem** — daily appends create many small Parquet files with no compaction strategy

An open table format is needed to close these gaps while preserving Athena as the primary query engine.

## Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Apache Iceberg** | Native Athena GA support, Terraform-native via `aws_glue_catalog_table`, hidden partitioning, strong community momentum | Write path requires Athena CTAS or Spark (not Glue Python Shell) | **Selected** |
| **Delta Lake** | Mature ecosystem, delta-rs Python library, OPTIMIZE compaction | Not natively supported in Athena (requires manifest symlinks), Databricks-centric governance | Rejected — Athena is our primary query engine |
| **Apache Hudi** | Built-in merge-on-read, incremental queries | Declining community momentum vs Iceberg, limited Athena support (read-only), complex configuration | Rejected — poor AWS-native fit |
| **Stay with plain Parquet** | Zero migration effort, current system works | No ACID, no schema evolution, small file problem will worsen | Rejected — doesn't address production gaps |

## Decision

Use **Apache Iceberg** as the open table format for both `fx_rates` and `economic_indicators` tables. Tables are defined in the Glue Data Catalog via Terraform (`terraform/athena.tf`) with Iceberg-specific properties (`table_type = "ICEBERG"`).

Write path: `lambda/lambda_iceberg_writer.py` reads raw JSON from S3, runs quality checks via `common/quality.py`, and writes to Iceberg tables through batched Athena `INSERT INTO` queries (see ADR-007).

## Consequences

### Positive

- **File-level pruning** — Athena queries use Iceberg metadata to skip irrelevant data files, reducing scan cost
- **Schema evolution** — additive changes (add column, rename, reorder) without rewriting history
- **Time travel** — `AS OF` queries enable auditing and debugging without S3 versioning hacks
- **ACID transactions** — concurrent writes and failed writes are safe; readers never see partial data
- **Compaction** — `lambda/lambda_iceberg_maintenance.py` scheduled to compact small files

### Negative

- **Write path complexity** — Iceberg writes require Athena SQL, not direct Parquet writes
- **Compaction overhead** — must schedule and monitor `OPTIMIZE` and `VACUUM` operations
- **Glue catalog dependency** — Iceberg metadata lives in Glue, adding a single point of failure

### References

- `terraform/athena.tf` — Iceberg table definitions with partition projection
- `lambda/lambda_iceberg_writer.py` — write path implementation
- `lambda/lambda_iceberg_maintenance.py` — compaction and vacuum scheduling
- Decision log DL-001 (see git history: `docs/planning/decision_log_v3.md`)
