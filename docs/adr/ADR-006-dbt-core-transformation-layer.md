# ADR-006: dbt Core for transformation layer

**Status:** Accepted
**Date:** 2026-04-22
**Supersedes:** ADR-004 (Data quality in Glue) — dbt tests replace most quality.py checks; quality.py preserved for CRITICAL pre-write gates

## Context

v2's `glue_transform.py` (404 lines) handles domain routing, quality enforcement, partitioning, and metric publishing in a single monolithic file. It works but doesn't support lineage, isn't modular, and couples all sources into one failure domain. As the pipeline adds more sources and transformations, this monolith becomes harder to maintain and test.

## Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **dbt Core (free)** | SQL-based models, built-in lineage, schema tests, documentation generation, large ecosystem | Learning curve, additional tool in the stack, SQL-only transformations | **Selected** |
| **dbt Cloud** | Managed scheduling, IDE, CI integration | $100+/month, overkill for solo developer, duplicate orchestration with Step Functions | Rejected — cost and complexity |
| **Refactor glue_transform.py** | No new tools, Python expertise preserved | Still monolithic at scale, no lineage, no built-in testing framework | Rejected — doesn't address root causes |
| **AWS Glue DataBrew** | Visual, no-code transformations | Limited customization, vendor lock-in, no lineage, no version control | Rejected — poor fit for engineering workflow |

## Decision

Use **dbt Core 1.11.8** with the `dbt-athena-community 1.10.0` adapter. dbt runs via CodeBuild (`.sync` integration with Step Functions) in production and locally for development.

**Project structure:**
- `dbt/models/staging/` — source-of-truth for deduplication (ROW_NUMBER window functions), materialized as views
- `dbt/models/marts/` — thin selects from staging with derived columns, materialized as Iceberg tables
- `dbt/tests/generic/` — custom tests (e.g., `positive_values`) supplementing built-in `not_null`, `accepted_values`, `unique_combination_of_columns`

**Quality check migration:**

| quality.py check | dbt equivalent | Level |
|---|---|---|
| `check_no_nulls` | `not_null` (built-in) | CRITICAL |
| `check_positive_values` | `positive_values` (custom generic test) | CRITICAL |
| `check_value_in_set` | `accepted_values` (built-in, severity: warn) | WARNING |
| `check_duplicates` | `dbt_utils.unique_combination_of_columns` | CRITICAL |
| `check_rate_range` | Removed — currencies like KRW/IDR legitimately exceed 1000 | — |
| `check_required_columns` | Enforced by Iceberg schema at compile time | — |

## Consequences

### Positive

- **Modular models** — each source/domain gets its own dbt model, independently testable and versionable
- **Automatic lineage** — `ref()` function tracks dependencies between models
- **Schema contracts** — `schema.yml` replaces implicit schema definitions with declarative tests
- **Documentation generation** — `dbt docs generate` produces a navigable data catalog
- **CI integration** — `dbt compile` and `dbt test` run in CI via CodeBuild

### Negative

- **Additional tool** — developers must learn dbt's SQL-first paradigm alongside Python
- **SQL-only transforms** — complex statistical checks remain in Python (`common/quality.py`)
- **CodeBuild overhead** — dbt runs add ~2-3 minutes to pipeline execution vs inline Python

### References

- `dbt/` — full dbt project (models, tests, macros, profiles)
- `terraform/codebuild.tf` — CodeBuild project for dbt execution
- `lambda/common/quality.py` — preserved for CRITICAL pre-write quality gates
- `docs/planning/decision_log_v3.md` — DL-003
