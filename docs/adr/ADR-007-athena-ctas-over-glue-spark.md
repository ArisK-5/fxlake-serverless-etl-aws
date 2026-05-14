# ADR-007: Athena CTAS over Glue Spark for Iceberg writes

**Status:** Accepted
**Date:** 2026-04-22

## Context

With the adoption of Apache Iceberg (ADR-005), the v2 write path (Glue Python Shell → Polars → Parquet on S3) no longer works — Iceberg requires a write engine that understands its metadata format. Two viable options exist within the AWS ecosystem: Athena CTAS/INSERT INTO, or Glue Spark with the Iceberg connector.

FXLake processes <1 MB of data per day (~1,000 rows across three sources). The write path must be cost-effective at this scale.

## Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Athena CTAS/INSERT** | No DPU cost (pay per TB scanned), serverless, SQL-based, native Iceberg support | Limited transformation power (SQL only), $5/TB scanned pricing | **Selected** |
| **Glue Spark + Iceberg** | Full transformation power, native Iceberg connector, handles large datasets | Minimum 2 DPU ($0.44/DPU-hour) — 32x more expensive than Python Shell, Spark cold start ~1-2 min | Rejected — overkill for <1MB/day |
| **PyIceberg from Lambda** | Pure Python, no Spark/Athena dependency | Immature write support, no Glue catalog integration, untested at this scale | Rejected — too early |
| **Keep Glue Python Shell + raw Parquet** | Minimal change | Doesn't provide ACID writes or time travel | Rejected — half-measure |

## Decision

Use **Athena INSERT INTO** as the Iceberg write path, executed from `lambda/lambda_iceberg_writer.py`. The Lambda reads raw JSON from S3, runs quality checks, builds batched INSERT SQL, and executes via Athena.

**Key implementation details:**
- Batched INSERT queries stay under Athena's 262,144-byte query string limit (`_build_insert_queries`)
- Quality checks run before INSERT — CRITICAL failures quarantine to S3 and raise `ValueError`
- Athena polling: 2s intervals, max 90 attempts (3 min timeout)
- Table name validated against `^[a-zA-Z_][a-zA-Z0-9_]*$` regex (SQL injection prevention)

## Consequences

### Positive

- **Near-zero cost** — at <1 MB/day, Athena charges are effectively $0 (minimum 10 MB scan per query = $0.00005)
- **No infrastructure** — serverless, no DPU provisioning, no cluster management
- **SQL-native** — aligns with dbt-based transformation layer (ADR-006)
- **Native Iceberg support** — Athena handles metadata commits, partition management, and file layout

### Negative

- **SQL-only transformations** — complex Python logic must happen before the INSERT (in the Lambda)
- **Query string limit** — large batches require splitting across multiple INSERT queries
- **Athena concurrency limits** — default 20 concurrent DML queries per account; sufficient for current workload but may need service limit increase at scale

### Migration Path

If data volumes grow past ~100 MB/day, reassess in favor of Glue Spark with the Iceberg connector. The dbt models and quality checks are engine-agnostic — only the write path Lambda would change.

### References

- `lambda/lambda_iceberg_writer.py` — Athena INSERT implementation
- `terraform/athena.tf` — Athena workgroup and Iceberg table definitions
- `docs/planning/decision_log_v3.md` — DL-002
