# FXLake v3 — Architecture Analysis Summary

**Date:** 2026-04-22
**Author:** Architecture Review (Claude Code)
**Scope:** Forensic analysis of v2 architecture, scalability assessment, production readiness gaps, open table format evaluation, and target v3 architecture vision.

---

## 1. Executive Summary

FXLake v2 is a well-engineered serverless ETL pipeline that achieves 97% test coverage across 253 tests, handles 3 independent data sources with parallel ingestion, and costs effectively nothing at current scale. The architecture makes excellent use of Polars on Glue Python Shell (32x cheaper than Spark), DynamoDB-backed incremental watermarks with a saga pattern, and a pure-function data quality framework.

However, v2 was designed for a portfolio demonstration, not production operations at scale. This analysis identifies five categories of gaps: **storage layer limitations** (no ACID, no schema evolution, no time travel), **transformation rigidity** (imperative Python, no lineage, no modularity), **operational blind spots** (no SLA tracking, no dead letter queues, no cost attribution), **governance absence** (no schema registry, no data contracts, no PII detection), and **scalability ceilings** (Glue Python Shell single-node memory limit, monolithic transform job).

The recommended evolution path is a **hybrid migration** over 20 days: keep the serverless orchestration (Step Functions + Lambda + EventBridge) that works well, upgrade the storage layer to **Apache Iceberg** for ACID transactions and time travel, introduce **dbt** for transformation modularity and lineage, and add operational/governance layers incrementally. Each phase delivers independently deployable value.

---

## 2. Current Architecture Strengths

Before identifying gaps, it's important to acknowledge what v2 does well — these are load-bearing decisions that v3 should preserve:

| Strength | Evidence |
|----------|----------|
| **Cost efficiency** | Glue Python Shell at 0.0625 DPU (~$0.003/run) — ADR-001 documents 32x savings vs Spark minimum |
| **Incremental processing** | DynamoDB watermarks with saga pattern — state only advances after Glue succeeds (base.py L210-245) |
| **Data quality as code** | Pure-function checks in quality.py (268 lines, 100% coverage), CRITICAL/WARNING severity, quarantine flow |
| **Error observability** | 11 CloudWatch alarms, structured JSON logging, X-Ray tracing, ErrorPath/CausePath in Step Functions |
| **Test discipline** | 97% coverage, integration tests exercising full pipeline flow, moto+responses for AWS mocking |
| **Infrastructure as code** | 65+ Terraform resources, OIDC-based CI/CD, pinned provider versions, remote state with locking |
| **Parallel ingestion** | Step Functions Parallel state with ResultSelector — ADR-003 documents 3x latency reduction |

---

## 3. Scalability Assessment

### 3.1 Scalability Matrix

| Component | Current Load | 10x | 100x | 1000x | Breaking Point |
|-----------|-------------|-----|------|-------|----------------|
| **Lambda ingestion** | 3 sources, ~1KB-50KB/call | 30 sources | 300 sources | 3000 sources | Step Functions Parallel state has 256-branch limit. Lambda concurrency limit (1000 default) becomes relevant. Cost still negligible. |
| **Glue Python Shell** | 1 job, <100KB input, <4GB memory | 1MB input | 10MB input | 100MB+ input | **Single-node ceiling at ~4GB memory.** Polars 0.18.8 is efficient but monolithic `process_all_files()` loads all files into memory. At ~10-50MB raw input, memory pressure becomes real. |
| **S3 raw storage** | ~50KB/day (3 JSON files) | 500KB/day | 5MB/day | 50MB/day | No practical limit. Hive-style partitioning works well. Cost negligible even at 1000x. |
| **S3 processed (Parquet)** | ~10KB/day | 100KB/day | 1MB/day | 10MB/day | Small file problem emerges at 100x+. One Parquet file per source per date = many tiny files, degrading Athena scan performance. |
| **Athena queries** | 1 validation query/day | 10/day | 100/day | 1000/day | Partition projection handles partition growth. But query cost scales linearly with data scanned — no file-level pruning without Iceberg/Delta metadata. |
| **DynamoDB state** | 3 records, ~6 reads + 3 writes/day | 30 records | 300 records | 3000 records | No practical limit. Pay-per-request billing, single-digit ms latency. |
| **Step Functions** | 1 execution/day, ~15 state transitions | 10/day | 100/day | 1000/day | $0.025/1000 transitions. Cost stays under $1/month even at 1000x. State machine complexity is the real limit. |
| **CloudWatch** | 11 alarms, ~20 metrics | 110 alarms | 1100 alarms | 11000 alarms | Alarm cost ($0.10/alarm/month) becomes non-trivial at 100x+. Dashboard doesn't scale beyond ~20 widgets usefully. |

### 3.2 Critical Breaking Points

1. **Glue Python Shell memory (100x):** The monolithic `process_all_files()` in glue_transform.py processes all raw files in a single invocation. With 300+ sources or large datasets, the 4GB ceiling will cause OOM. Mitigation: file-level iteration (already partially in place) or migration to Glue Spark/Athena CTAS.

2. **Small file problem (100x):** One Parquet file per source per date creates many tiny files. At 300 sources x 365 days = 109,500 files. Athena scans degrade because each file has fixed overhead regardless of size. Mitigation: compaction (Iceberg's rewrite_data_files), or daily roll-up.

3. **Monolithic transform (10x):** A single Glue job handling all sources means one source's CRITICAL quality failure blocks all others. The current filename-prefix routing (glue_transform.py L85-95) doesn't support per-source isolation.

4. **No dead letter queue (any scale):** Failed ingestion events are caught by Step Functions Catch blocks and routed to Fail states, but there's no mechanism to replay failed events. Manual re-runs via the console or `make backfill` are the only recovery path.

---

## 4. Production Readiness Gap Analysis

### 4.1 Gap Categories

#### Category A: Operational Excellence (HIGH priority)

| Gap | Impact | Current State |
|-----|--------|---------------|
| **No SLA monitoring** | Can't prove data freshness guarantees to consumers | Validation Lambda checks staleness but doesn't track latency trends or SLA compliance over time |
| **No dead letter queue** | Failed events require manual intervention | Step Functions Catch routes to Fail states; no automatic retry or alerting on specific failure types |
| **No cost attribution** | Can't track cost per source or per pipeline stage | All resources share a single AWS account with no tagging strategy |
| **No runbook/playbook** | Incident response depends on developer memory | README covers setup, not operations |
| **Limited dashboard** | 12 singleValue widgets show current state, not trends | No time-series graphs, no per-source drill-down |

#### Category B: Storage Layer (HIGH priority)

| Gap | Impact | Current State |
|-----|--------|---------------|
| **No ACID transactions** | Partial writes on Glue failure leave inconsistent state in S3 | Saga pattern protects DynamoDB state but not S3 files — a failed Glue run may leave partial Parquet files |
| **No schema evolution** | Adding a column requires rewriting all historical data or breaking backward compatibility | Parquet schema is implicit (whatever Polars writes). Athena table DDL must be manually updated |
| **No time travel** | Can't query "what did the data look like yesterday?" | No versioning. S3 versioning is disabled. |
| **No file-level pruning** | Athena scans all files in matching partitions | Partition projection narrows by date, but within a partition all files are scanned |
| **Small file problem** | Tiny Parquet files accumulate without compaction | One file per source per date, no merge/compaction mechanism |

#### Category C: Transformation Layer (MEDIUM priority)

| Gap | Impact | Current State |
|-----|--------|---------------|
| **No transformation lineage** | Can't trace which raw files produced which processed files | glue_transform.py logs filenames but doesn't record lineage metadata |
| **No transformation modularity** | All transform logic in one 404-line file | Domain routing, quality checks, partitioning, metric publishing all in glue_transform.py |
| **No data contracts** | Upstream schema changes break downstream silently | Quality checks validate values but not schema compatibility |
| **Polars version pinned** | 0.18.8 is 3+ years old; missing performance improvements and API features | Glue Python Shell 3.9 constraint prevents upgrade |

#### Category D: Governance (LOW priority for solo developer, HIGH for production)

| Gap | Impact | Current State |
|-----|--------|---------------|
| **No schema registry** | No single source of truth for dataset schemas | Schema defined implicitly by code + Athena DDL |
| **No PII detection** | Financial data may inadvertently contain PII in extended datasets | No scanning or classification |
| **No access controls** | No row/column-level security | Athena + S3 access is all-or-nothing per IAM role |
| **No data catalog** | Consumers must read code to understand available datasets | Glue Data Catalog has table definitions but no descriptions, tags, or ownership |

### 4.2 Production Readiness Scorecard

| Dimension | Score | Target |
|-----------|-------|--------|
| Reliability | 7/10 | 9/10 — need DLQ, ACID writes, better failure isolation |
| Observability | 6/10 | 9/10 — need SLA tracking, trend dashboards, per-source metrics |
| Security | 7/10 | 8/10 — need tagging, access controls, PII scanning |
| Cost Management | 8/10 | 9/10 — need cost attribution and budget alerts |
| Operability | 5/10 | 8/10 — need runbooks, automated recovery, better dashboards |
| Data Quality | 8/10 | 9/10 — need schema contracts, cross-source validation |

---

## 5. Open Table Format Evaluation

### 5.1 Candidates

| Criterion | Apache Iceberg | Delta Lake | Apache Hudi |
|-----------|---------------|------------|-------------|
| **AWS Athena support** | Native (GA since 2023, Athena v3) | Via manifest symlink (not native) | Limited (read-only, specific versions) |
| **Glue Data Catalog** | Native integration (table type = ICEBERG) | Requires Unity Catalog or manual sync | Requires Hudi-specific sync |
| **Schema evolution** | Full (add, drop, rename, reorder columns) | Add/rename columns | Add columns only |
| **Time travel** | Snapshot-based, configurable retention | Version-based | Timeline-based |
| **Hidden partitioning** | Yes — partition spec decoupled from schema | No — partition columns in schema | No — partition columns in schema |
| **File-level pruning** | Via manifest metadata (min/max stats) | Via transaction log | Via timeline metadata |
| **Compaction** | `rewrite_data_files` action | `OPTIMIZE` command | Built-in compaction |
| **Merge-on-read** | Supported (delete files + equality deletes) | Supported (deletion vectors) | Core feature (copy-on-write + merge-on-read) |
| **Terraform support** | `aws_glue_catalog_table` with `open_table_format_input` | No native Terraform resource | No native Terraform resource |
| **Spark dependency** | Optional (Athena/Trino can read/write natively) | Required for writes | Required for writes |
| **Community momentum** | Highest (Snowflake, Databricks adopting, Apache TLP) | Strong (Databricks-led) | Declining relative to Iceberg |
| **Python SDK** | PyIceberg (read/write, catalog integration) | delta-rs (Rust-backed, Python bindings) | Limited Python support |
| **Glue Python Shell** | Not supported (needs Spark or Athena CTAS) | Not supported (needs Spark) | Not supported (needs Spark) |

### 5.2 Recommendation: Apache Iceberg

**Apache Iceberg** is the clear choice for FXLake v3 based on three decisive factors:

1. **Native Athena integration:** Athena v3 reads and writes Iceberg tables natively — no manifest files, no symlinks, no external metastore. `CREATE TABLE ... TBLPROPERTIES ('table_type' = 'ICEBERG')` just works. This means the existing Athena validation query can be migrated with minimal changes.

2. **Terraform-native:** `aws_glue_catalog_table` supports `open_table_format_input { iceberg_input { ... } }` directly. No custom resources or external tools needed.

3. **Hidden partitioning:** Iceberg decouples the partition spec from the table schema. Consumers query `WHERE date = '2024-01-15'` without knowing the physical layout. This eliminates the current partition projection configuration and simplifies schema evolution.

**Write path change:** The current Glue Python Shell job cannot write Iceberg format directly (requires Spark or Athena CTAS). The recommended approach is to use **Athena CTAS/INSERT INTO** for Iceberg writes, replacing the Glue Polars-based write with an Athena-based write. The Polars transformation logic remains for data manipulation; only the final write changes. Alternatively, migrate to Glue Spark with IcebergConnector — but this increases DPU cost significantly.

### 5.3 Migration Strategy

**Branch-based migration (no dual-write):**

There are no downstream consumers of the current tables, so dual-write complexity is unnecessary. All v3 work happens on a dedicated `v3` branch:

1. Create `v3` branch from `main` — v2 pipeline continues running on `main` unaffected
2. Replace Parquet table definitions with Iceberg tables on the `v3` branch
3. Build and validate new write path (Athena CTAS) on the `v3` branch
4. Run a backfill on `v3` and validate data against known v2 output
5. Merge `v3` to `main` — one `terraform apply` switches the storage layer
6. Schedule compaction

Rollback: revert the merge commit and re-apply v2 Terraform.

---

## 6. Target v3 Architecture Vision

### 6.1 Architecture Evolution: Hybrid Migration (Option B)

The recommended approach preserves the serverless orchestration that works well (Step Functions, Lambda, EventBridge, DynamoDB) while upgrading the storage and transformation layers:

```
EventBridge (daily)
    |
    v
Step Functions (orchestrator — preserved from v2)
    |
    +---> Parallel Ingestion (Lambda x N — preserved, extensible)
    |         |
    |         v
    |     S3 Raw (JSON — preserved)
    |
    +---> dbt on Athena/Glue (NEW — replaces glue_transform.py)
    |         |
    |         v
    |     Iceberg Tables (NEW — replaces raw Parquet)
    |         |-- fx_rates (Iceberg, hidden partitioning)
    |         |-- economic_indicators (Iceberg, hidden partitioning)
    |         |-- staging_* (Iceberg, ephemeral)
    |         +-- quality_reports (Iceberg or Parquet)
    |
    +---> Data Quality (preserved, enhanced with dbt tests)
    |
    +---> Athena Validation (preserved, queries Iceberg tables)
    |
    +---> State Management (DynamoDB saga — preserved)
    |
    +---> Monitoring (CloudWatch — enhanced with SLA tracking)
    |
    +---> Governance Layer (NEW)
              |-- Glue Data Catalog (enhanced with tags, descriptions)
              |-- Schema contracts (dbt schema.yml)
              +-- Cost attribution (AWS tags)
```

### 6.2 What Changes vs. What Stays

| Component | v2 | v3 | Rationale |
|-----------|----|----|-----------|
| Orchestration | Step Functions | Step Functions | Works well, no reason to change |
| Ingestion | Lambda (Python 3.12) | Lambda (Python 3.12) | Works well, extensible via base class |
| Raw storage | S3 JSON | S3 JSON | Immutable raw layer should stay format-agnostic |
| Transform | Glue Python Shell (Polars) | dbt on Athena or Glue Spark | Modularity, lineage, testability |
| Processed storage | S3 Parquet (plain) | S3 Parquet (Iceberg-managed) | ACID, schema evolution, time travel |
| Query engine | Athena (partition projection) | Athena (Iceberg-native) | Hidden partitioning, file pruning |
| Quality | quality.py (pure functions) | dbt tests + quality.py | Preserve existing checks, add schema contracts |
| State | DynamoDB (saga) | DynamoDB (saga) | Works well, battle-tested |
| Monitoring | 11 alarms + dashboard | 15+ alarms + SLA dashboard | Add latency tracking, per-source metrics |
| CI/CD | GitHub Actions (OIDC) | GitHub Actions (OIDC) | Add dbt compile/test to CI |

### 6.3 Key Design Principles for v3

1. **Incremental delivery:** Each phase merges into the `v3` branch independently. Final merge to `main` delivers all changes.
2. **Zero downtime:** v2 runs on `main` while v3 is developed on its own branch. Cutover is a single merge.
3. **Preserve what works:** The ingestion layer, state management, and quality framework are battle-tested. Don't rewrite them.
4. **Test-first:** Maintain 95%+ coverage. Every new component gets tests before implementation.
5. **Cost conscious:** Prefer Athena CTAS over Glue Spark where possible. Monitor DPU usage.

---

## 7. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Athena CTAS cost exceeds Glue Python Shell** | Medium | Medium | Benchmark before committing. Athena charges $5/TB scanned; at current data volumes (<1MB/day) this is negligible. Set billing alarm. |
| **dbt adds complexity without enough sources to justify it** | Medium | Low | Start with dbt Core (free), 2 models. If overhead exceeds value at 3 sources, defer to 10+ sources. |
| **Iceberg metadata overhead for small tables** | Low | Low | Iceberg metadata is tiny relative to data. Compaction frequency matters more than metadata size. |
| **Glue Spark DPU cost if Athena CTAS is insufficient** | Low | Medium | Benchmark Athena CTAS first. Only migrate to Spark if data volumes require in-memory transformation. |
| **Polars→dbt migration breaks quality checks** | Medium | High | Preserve quality.py as a standalone module. Run dual validation during transition. |
| **Scope creep from governance features** | High | Medium | Governance is Phase 5 (last). Cut it if earlier phases take longer than planned. |

---

## 8. Conclusion

FXLake v2 is a strong foundation with excellent test coverage, cost efficiency, and clean architecture. The v3 evolution should be surgical: upgrade the storage layer to Iceberg for ACID and schema evolution, introduce dbt for transformation modularity and lineage, enhance operational tooling for SLA monitoring, and add governance incrementally. The 20-day plan in `implementation_plan_v3.md` delivers these improvements in five phases, each independently valuable and deployable.
