# ADR-001: Use Polars over PySpark for transformation

**Status:** Superseded by Athena CTAS + Lambda (see ADR-007)
**Date:** 2026-03-28
**Superseded:** 2026-04-22 — Glue Python Shell removed in v3; writes now handled by Iceberg writer Lambda via Athena INSERT INTO.

## Context

FXLake's Glue transform job reads raw JSON from S3 and writes partitioned Parquet. AWS Glue offers two runtime options:

- **Spark (PySpark):** Distributed processing engine. Minimum 2 DPU ($0.44/DPU-hour). Designed for datasets in the GB-TB range. Cold start ~1-2 minutes for Glue 3.0+.
- **Python Shell:** Single-node Python runtime. Minimum 0.0625 DPU ($0.44/DPU-hour, so ~$0.0275/hour at 0.0625 DPU). Cold start ~10-20 seconds.

FXLake processes daily FX rate files (typically <1 MB each, a few hundred rows per source). The total dataset grows by ~1,000 rows/day across all sources. Even after a year of backfill, the per-execution working set is small.

## Decision

Use **Glue Python Shell** at **0.0625 DPU** with **Polars 0.18.8** and **PyArrow** for all transformation work.

The Glue job is configured as:
```hcl
command {
  name            = "pythonshell"
  python_version  = "3.9"
}
max_capacity = 0.0625
```

Polars handles JSON parsing, column projection, data quality checks (via `quality.py`), and Parquet/CSV output with Hive-style partitioning.

## Consequences

### Positive

- **32x cost reduction** vs minimum Spark job (0.0625 vs 2 DPU)
- **Faster cold start** (~10-20s vs ~1-2 min for Spark)
- **Simpler dependency management** — Polars + PyArrow installed via `--additional-python-modules`, no Spark cluster configuration
- **Polars API is expressive** — lazy evaluation, zero-copy operations, Rust-backed performance far exceeding pandas for columnar operations
- **Quality checks run in the same process** — `quality.py` loaded via `--extra-py-files`, no network overhead

### Negative

- **Single-node ceiling** — if daily data volume grows beyond what fits in memory (~4 GB at 0.0625 DPU), must upgrade to 1 DPU or switch to Spark
- **Python 3.9 constraint** — Glue Python Shell pins the runtime version; Polars 0.18.8 is the last version supporting Python 3.8/3.9 (newer Polars requires 3.9+)
- **No distributed shuffle** — joins across very large tables would need a different approach

### Migration Path

If data volume exceeds single-node capacity: bump `max_capacity` to 1 (16x headroom, still cheaper than Spark minimum), or migrate to Glue Spark with PySpark + PyArrow.
