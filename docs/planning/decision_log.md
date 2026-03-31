# FXLake — Decision Log

**Date:** 2026-03-28

---

## Key Decisions Made During Analysis

### D1: Reordered priorities — Testing before features

**Original assumption:** Start with multi-source ingestion or IaC.
**Actual finding:** Zero tests exist. Any refactoring without tests is blind.
**Decision:** Day 1 is testing framework + Step Function resilience. All feature work requires test coverage first.
**Rationale:** A refactoring mistake in the ingestion Lambda or Glue job with no tests = undetectable regression. The 286 lines of source code are small enough to achieve high coverage in one session.

### D2: Terraform IaC is NOT a gap — deprioritized

**Original assumption:** IaC might be zero (manual AWS Console setup).
**Actual finding:** 47 Terraform resources across 11 files. Comprehensive coverage of all AWS services.
**Decision:** Deprioritize Terraform from CRITICAL to MEDIUM. Move module refactoring to Day 7.
**Impact:** Frees Days 1-3 for higher-value extensions.

### D3: Monitoring is NOT a gap — deprioritized

**Original assumption:** Monitoring might need significant work.
**Actual finding:** 7 CloudWatch alarms, custom metrics, dashboard, CloudTrail, SNS alerting. Solid foundation.
**Decision:** Deprioritize to Day 7-8 polish. Only enhancement: structured logging + data quality metrics.
**Impact:** Monitoring work reduces from potential 2 days to 0.5 days of enhancement.

### D4: CI/CD added as new extension (not in original plan)

**Original assumption:** Not mentioned in original 10-day plan.
**Actual finding:** Zero CI/CD. Manual `make deploy` only.
**Decision:** Add GitHub Actions CI/CD as Day 2 afternoon task. Pairs naturally with test framework.
**Rationale:** CI/CD is table stakes for EU Data Engineer roles. Every company will ask about deployment automation. Also prevents regression during the 10-day build-out.

### D5: EventBridge target discrepancy — flagged for verification

**Finding:** EventBridge daily rule triggers the ingestion Lambda directly, NOT the Step Function state machine.
**Implication:** The daily schedule only runs ingestion. The full pipeline (Glue → Athena → Validation) does NOT run automatically.
**Decision:** Flag for Day 2 fix. Change EventBridge target to Step Function ARN so the full pipeline runs daily.
**Risk:** This may be intentional design (decouple ingestion frequency from transform frequency). User should confirm intent.

### D6: Polars version upgrade deferred

**Finding:** Glue job uses `polars==0.18.8`. Current Polars is 1.x with breaking API changes.
**Decision:** Do NOT upgrade Polars during the 10-day sprint. Version is pinned in Terraform `default_arguments`.
**Rationale:** Polars 0.18 → 1.x migration is non-trivial (API changes to `DataFrame` constructor, `write_csv`, `to_arrow`). This is pure technical debt with zero hiring signal. Upgrade after the sprint if deploying to production.

### D7: DynamoDB chosen over SSM Parameter Store for state tracking

**Alternatives considered:**
- SSM Parameter Store: Simple key-value, but no atomic conditional updates, 10K parameter limit
- S3 marker files: Simple but race-condition-prone, no query capability
- DynamoDB: Atomic writes, conditional updates, query by pipeline/source

**Decision:** DynamoDB for pipeline state.
**Rationale:** Demonstrates understanding of state management patterns in serverless architectures. Supports multi-source state tracking with sort key per source. Standard pattern for AWS Step Functions pipelines.

### D8: Three data sources chosen for maximum signal

**Sources selected:**
1. **Frankfurter API** (existing) — FX rates, free, no auth
2. **ECB Statistical Data Warehouse** — institutional data, free, no auth, XML/JSON SDMX format
3. **FRED (Federal Reserve Economic Data)** — macroeconomic data, free API key, different schema

**Rationale:** Each source demonstrates a different skill:
- Frankfurter: REST JSON (baseline)
- ECB: SDMX/XML parsing (EU-relevant, shows domain knowledge)
- FRED: API key management, different data model (time series vs cross-section)

**Alternative rejected:** Alpha Vantage (requires paid API key for reasonable rate limits).

### D9: Data quality framework is custom, not Great Expectations

**Original assumption:** Use Great Expectations.
**Decision:** Build lightweight custom quality framework with Polars.
**Rationale:**
- Great Expectations is heavy (many dependencies) and doesn't integrate well with Glue Python Shell (0.0625 DPU)
- Polars-native checks are faster and lighter
- Demonstrates understanding of quality concepts without framework dependency
- Custom framework can publish CloudWatch metrics directly
- Interview talking point: "I chose not to use GE because..." shows judgment

### D10: Architecture Decision Records added

**Not in original plan.**
**Decision:** Add ADRs on Day 9 as documentation artifact.
**Rationale:** ADRs are increasingly expected in EU engineering teams. They demonstrate architectural thinking and decision-making ability. Writing them after implementation ensures they reflect actual decisions, not hypothetical ones.

---

## Assumptions That Differ From Original Plan

| # | Original Assumption | Reality | Impact |
|---|---------------------|---------|--------|
| 1 | IaC might be zero | 47 TF resources exist | Freed 2+ days |
| 2 | Monitoring might be minimal | 7 alarms + dashboard | Freed 1+ day |
| 3 | Step Functions might have Parallel state | Linear only, no error handling | Must add both |
| 4 | ASL JSON might be in separate file | Inline in Terraform `jsonencode()` | Edit .tf directly |
| 5 | Glue uses PySpark | Glue Python Shell + Polars | Different test approach |
| 6 | Pipeline runs daily end-to-end | Only ingestion Lambda is scheduled | Fix EventBridge target |
| 7 | Code might have TODOs/roadmap | Zero TODOs in source | Clean slate |
| 8 | Testing framework might exist | Zero tests | Day 1 priority |

---

## Recommendations: Keep / Modify / Drop

| Extension | Verdict | Notes |
|-----------|---------|-------|
| Multi-source ingestion | **KEEP** | Marquee feature, highest hiring signal |
| Data quality framework | **KEEP** | Critical for "production-grade" claim |
| Incremental processing | **KEEP** | Table stakes for real ETL |
| Testing | **ADD** | Was not in original plan but is prerequisite |
| CI/CD | **ADD** | Was not in original plan but is table stakes |
| Terraform refactoring | **MODIFY** | Reduce scope: remote state + demo Lambda module only (no full migration) |
| Monitoring | **MODIFY** | Reduce scope: structured logging + quality metrics only |
| Backfill mode | **KEEP** | Demonstrates operational maturity |
| ADRs | **ADD** | High signal-to-effort ratio for interviews |
| Polars upgrade | **DROP** | Pure tech debt, zero hiring signal, risk of bugs |

---

## External Review Validation (2026-03-28)

Sonnet 4.5 review of the plan raised 5 concerns. Validated against actual codebase by Opus 4.6.

### D11: EventBridge → Step Functions fix (Concern #1)

**Assessment: VALID.** `terraform/lambda.tf:45-49` confirms EventBridge targets `aws_lambda_function.api_ingest.arn`, not Step Functions. The full pipeline never ran on schedule — only ingestion.
**Action:** Session 2C added to plan. Also clean up orphaned `aws_lambda_permission "allow_eventbridge"`.

### D12: ECB uses format=jsondata, not raw SDMX (Concern #2)

**Assessment: VALID.** SDMX XML with nested namespaces is disproportionate complexity for this project scope. ECB API supports `?format=jsondata` natively.
**Action:** Plan already uses simplified endpoint. Hiring signal comes from integrating an institutional EU data source, not from XML parsing.

### D13: "Days" are work units, not calendar days (Concern #3)

**Assessment: PARTIAL.** Raw hours (7.5-8.5h for Day 2) exceed a single sitting, but "renumber Days 3-11" adds confusion. The user works 2-3h/day.
**Action:** Added pacing note to plan header. Sessions within a Day are independently completable. No renumbering.

### D14: Hybrid schema — unified FX + separate economic indicators (Concern #4)

**Assessment: PARTIAL.** Reviewer's unified schema forces GDP (billions USD) into `rate` column — semantically wrong. But source-specific branching per source is also over-engineered.
**Action:** Two data domains: `fx_rates` (Frankfurter+ECB unified) and `economic_indicators` (FRED separate). Adding a new FX source requires zero transform code. Different data domain is a deliberate, documented decision.

### D15: Terraform modules dropped, remote state + demo module only (Concern #5)

**Assessment: VALID.** Migrating 47 resources into modules requires `moved` blocks for each, high risk of destroy+recreate. Current 11 files organized by service domain are already clean. Interviewers don't ask about Terraform modules.
**Action:** Day 7 reduced to: S3 remote state backend + bootstrap, reusable `modules/lambda_function/` for new sources only. Existing resources untouched. Interview talking point: "I'd migrate the rest in a separate PR with `moved` blocks."

---

## Day 1 Implementation Decisions (2026-03-30)

### D16: Broad `except Exception` justified in `publish_custom_metric`

**Context:** PR review recommended narrowing `except Exception` to `except ClientError` in `publish_custom_metric()`. A follow-up review reversed this.
**Decision:** Keep `except Exception` — this is the ONE place where broad catching is correct.
**Rationale:** CloudWatch metric publishing is non-critical. A `BotoCoreError` (connection-level), `EndpointConnectionError`, or even a `TypeError` from bad metric data must NOT crash the validation handler. `ClientError` only covers AWS API errors — it misses the entire `BotoCoreError` hierarchy. The function's explicit purpose is "log and continue regardless."
**Interview talking point:** "I can explain why every except clause has the scope it has."

### D17: Lambda handler outer try/except restored with structured context

**Context:** First review said to remove the outer `try/except` in `lambda_handler()` (duplicate logging). Third review said removal went too far.
**Decision:** Restore with `exc_info=True` + structured `extra` context (start_date, end_date, base_currency).
**Rationale:** Without the outer catch, an unexpected exception between `fetch_exchange_rates()` and `save_to_s3()` would produce zero application-level log context — only a raw Python traceback. The `extra` dict provides structured context for CloudWatch Logs Insights queries.

### D18: Service-specific retry errors in Step Functions

**Context:** Original plan specified generic `States.TaskFailed` for Glue/Athena retries and only `Lambda.ServiceException` + `Lambda.TooManyRequestsException` for Lambda.
**Decision:** Use service-specific error classes:
- Lambda: `ServiceException`, `AWSLambdaException`, `TooManyRequestsException`
- Glue: `ConcurrentRunsExceededException`, `States.HeartbeatTimeout`
- Athena: `Athena.InternalServerException`, `Athena.TooManyRequestsException`
**Rationale:** Generic `States.TaskFailed` retries ALL failures including data-level errors (bad JSON, missing field) that will never succeed on retry. Service-specific errors ensure retries only fire on transient infrastructure issues.

### D19: Dynamic `ErrorPath`/`CausePath` in Fail states

**Context:** Original plan specified static `Error`/`Cause` strings on Fail states.
**Decision:** Use `ErrorPath: "$.errorInfo.Error"` and `CausePath: "$.errorInfo.Cause"` with `ResultPath: "$.errorInfo"` on Catch blocks.
**Rationale:** Static strings lose the actual error detail. Dynamic paths pass through the real AWS error message, making CloudWatch Events and execution history useful for debugging without checking CloudWatch Logs.

### D20: Test coverage significantly exceeded plan

**Context:** Day 1 planned ~20 tests with 80%+ coverage as nice-to-have.
**Delivered:** 37 tests, 96% coverage.
**Reason:** Three rounds of iterative PR review (`/pr-review-toolkit:review-pr`) surfaced edge cases that were genuine gaps, not test padding. Each test covers a real failure mode (non-JSON API response, connection errors vs timeouts, `ClientError` vs `KeyError` distinction, parametrized Athena states). The coverage increase was organic, not targeted.
