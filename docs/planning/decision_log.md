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

## Day 2 Session 2C Decisions (2026-03-31)

### D30: EventBridge targets Step Functions, not the ingestion Lambda

**Context:** The original `aws_cloudwatch_event_target` pointed at `aws_lambda_function.api_ingest.arn`. This meant the daily schedule only ran ingestion — Glue, Athena, and Validation never fired automatically.
**Decision:** Retarget EventBridge to `aws_sfn_state_machine.etl.arn` with a dedicated IAM role (`states:StartExecution` only).
**Rationale:** The whole point of Step Functions is to orchestrate the full pipeline. Having EventBridge bypass it and call the Lambda directly defeats the purpose and means the pipeline never ran end-to-end on schedule. The dedicated role follows least-privilege: EventBridge only gets `StartExecution` on this one state machine.
**What changed in Terraform:** `invoke_lambda` target removed, `invoke_step_function` added, `allow_eventbridge` Lambda permission removed, new `eventbridge_sfn_invoke_role` + `eventbridge_sfn_invoke_policy`.

## Day 2 Session 2B Decisions (2026-03-31)

### D25: Partition projection over MSCK REPAIR TABLE

**Context:** Athena requires partition metadata to query Hive-partitioned data. Two options: `MSCK REPAIR TABLE` (manual or scheduled) or partition projection (Athena-native, automatic).
**Decision:** Partition projection with integer type and `digits=2` for month and day.
**Rationale:** Partition projection eliminates all partition management — no Lambda to run MSCK, no Glue crawler, no stale partition metadata. The `digits=2` setting ensures Athena generates zero-padded paths (`month=01`) matching the S3 layout. Interview talking point: "I know when to use each Athena partition strategy and why projection is preferred for predictable date ranges."

### D26: One Parquet file per date per source file (not one file per date globally)

**Context:** When partitioning by date, one option is to merge all source records for a date into one file; another is to keep source-file granularity within each partition.
**Decision:** `exchange_rates/year=YYYY/month=MM/day=DD/{source_stem}.parquet` — the source filename is preserved within each partition directory.
**Rationale:** Avoids S3 read-modify-write for incremental appends. Each daily Lambda run produces a new source file; the Glue job writes it into the correct partition without touching existing files. Enables per-run traceability and idempotent reprocessing.

### D27: OIDC for AWS authentication in CI, no long-lived keys

**Context:** GitHub Actions needs AWS credentials to run Terraform. Options: IAM user keys stored as secrets, or OIDC token exchange.
**Decision:** OIDC via `aws-actions/configure-aws-credentials@v4` with `role-to-assume`. The role ARN is the only secret stored in GitHub.
**Rationale:** OIDC tokens are short-lived (15 min), scoped to a specific repo and branch, and rotate automatically. No rotation policy required, no key leakage risk. This is the AWS-recommended pattern for CI/CD. Interview talking point: "I used OIDC rather than IAM keys because there's no secret to rotate or leak."

### D28: GitHub secrets bound to `env:` vars before use in `run:` steps

**Context:** The deploy workflow must write a `terraform.tfvars` file from GitHub secrets. A naive approach inlines `${{ secrets.* }}` directly in the `run:` shell command, which can enable injection if a secret contains shell metacharacters.
**Decision:** Bind all secrets to job-level `env:` variables first; reference them as `$VAR` (shell variable, not GitHub expression) inside the heredoc.
**Rationale:** Shell variables in a heredoc are just string substitutions — no expression evaluation, no injection surface. GitHub expressions `${{ ... }}` are template-expanded before the runner sees the shell, so they can inject if the value contains backticks, semicolons, etc. This is the pattern recommended by GitHub's security guide.

### D29: Glue `process_key` returns `List[str]` instead of `str`

**Context:** With partitioned output, one input file produces N output files (one per date). The original `str` return type was wrong.
**Decision:** Return `List[str]`, empty list for no-data input.
**Rationale:** Callers (`main()` currently ignores the return; future callers like a manifest writer would need all output keys). `[]` for empty input is cleaner than writing an empty file to a catch-all path. Existing `main()` loop required no change.

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

---

## Day 2 Implementation Decisions (2026-03-31)

### D21: `os.getenv` for STATE_TABLE, `os.environ[]` for all other env vars

**Context:** Incremental mode requires a new `STATE_TABLE` env var. The existing convention uses `os.environ["VAR"]` at module level (fails fast at cold start).
**Decision:** Use `os.getenv("STATE_TABLE")` — returns `None` if absent, enabling static fallback without requiring a DynamoDB table.
**Rationale:** STATE_TABLE is genuinely optional: backfills, local testing, and the static mode all work without it. Using `os.environ[]` would break all existing deployments and tests without any benefit. The distinction (`os.environ` = required, `os.getenv` = optional) becomes an explicit contract visible at module level.

### D22: Private `_incremental_ingest` and `_static_ingest` helpers over branching in lambda_handler

**Context:** Adding incremental logic to `lambda_handler` would push it toward 40+ lines with 4 nesting levels.
**Decision:** Extract `_incremental_ingest()` and `_static_ingest()` as private helpers; `lambda_handler` only branches and handles the outer try/except.
**Rationale:** Each helper is under 20 lines and tests a single concern. The outer handler stays readable. Interview talking point: "separation of orchestration from business logic."

### D23: DYNAMODB client initialized conditionally at module level

**Context:** `boto3.client("dynamodb")` at module level would require moto's DynamoDB to be active for every test, even those that never touch DynamoDB.
**Decision:** `DYNAMODB = boto3.client("dynamodb") if STATE_TABLE else None` — client is `None` when STATE_TABLE is absent.
**Rationale:** Tests that patch `STATE_TABLE` also patch `DYNAMODB` directly via `patch.object`. This avoids polluting the existing test fixtures and keeps the S3-only tests independent.

### D24: Step Functions Choice state checks `$.ingestion.Payload.status`

**Context:** Lambda invoked via `arn:aws:states:::lambda:invoke` wraps the function response in a `Payload` envelope. The Lambda returns `{status: "no_new_data"}` or `{status: "ok"}`. After D26 added `ResultPath = "$.ingestion"` to the ingestion state, the result is stored at `$.ingestion` rather than overwriting `$`.
**Decision:** Choice state checks `$.ingestion.Payload.status == "no_new_data"` to route to `Pipeline-Already-Up-To-Date` (Succeed); default continues to Glue.
**Rationale:** Using `ResultPath` on the ingestion state preserves `$.ingestion.Payload.end_date` through Glue and into the `Lambda-Update-State` step. The Succeed state (not End) correctly marks the execution as successful — "no new data" is not a failure.

### D26: DynamoDB state commit moved to post-Glue Lambda-Update-State step

**Context:** The original design updated DynamoDB at the end of the ingestion Lambda, before Step Functions proceeded to Glue. If Glue failed after ingestion returned `ok`, DynamoDB would record `fetch_end` as already processed — causing the next daily run to skip the range that was never transformed into Parquet.
**Decision:** Remove `update_last_processed_date()` from the ingestion Lambda. Add a `Lambda-Update-State` Step Functions state between Glue and Athena that invokes the ingestion Lambda with `{"action": "update_state", "end_date": "..."}`. State is only committed when Glue has confirmed success.
**Implementation:** `ResultPath = "$.ingestion"` on the ingestion state preserves `end_date` across subsequent states (Glue, Athena each use their own `ResultPath`). The validation Lambda reads `$.athena.QueryExecution.QueryExecutionId`.
**Rationale:** Saga/checkpoint pattern: state ownership belongs to the step that confirms completion. This eliminates the silent data-gap failure mode where Glue failures produce permanent gaps in Athena-queryable data.

### D27: get_last_processed_date() re-raises on infrastructure errors

**Context:** Original code caught all `ClientError` and fell back to `START_DATE`. This masked `AccessDeniedException` and `ResourceNotFoundException` — infrastructure misconfigurations that would cause every run to re-fetch the full date range and duplicate raw S3 data silently.
**Decision:** Re-raise immediately on `ResourceNotFoundException` and `AccessDeniedException`. Reserve the fallback for transient errors (`ProvisionedThroughputExceededException`, `InternalServerError`, etc.) where a single-run fallback is a reasonable degradation.
**Rationale:** Infrastructure misconfiguration must be surfaced as a pipeline failure, not hidden as a quiet mode change.

### D28: CI uses stub Lambda zips for terraform validate

**Context:** `lambda.tf` uses `filebase64sha256("../lambda/*.zip")` to detect source changes. The deploy workflow builds real zips via `make package`. The CI validate job (`-backend=false`) does not run a build step, so the files don't exist and `filebase64sha256()` raises an error at plan/validate time.
**Decision:** Add `touch lambda/lambda_ingestion_function.zip lambda/lambda_validation_function.zip` as a CI step before `terraform init`. Empty files satisfy `filebase64sha256()` (returns a valid hash) without requiring a build.
**Why not use `try()` in Terraform:** `try(filebase64sha256(...), null)` would suppress the error but would also silently pass deploy with a null hash — defeating the purpose of the hash check. The stub approach keeps production behaviour unchanged.

### D25: Step Functions Lambda state TimeoutSeconds set to 90s

**Context:** `Lambda-API-Ingestion` state had `TimeoutSeconds = 30`, which is less than the Lambda function's own `timeout = 60s`. If the Lambda ran for 40–60 seconds, Step Functions would abort it with `States.Timeout` before it could complete.
**Decision:** Set `TimeoutSeconds = 90` (60s Lambda timeout + 30s buffer for Step Functions overhead and cold start).
**Rationale:** The Step Functions timeout must always exceed the Lambda timeout. A 30s Step Functions timeout on a 60s Lambda is a guaranteed failure for slow API responses. Buffer of 30s accounts for cold starts and SDK retry jitter.

### D29: _handle_update_state guard for DYNAMODB=None

**Context:** The `Lambda-Update-State` Step Functions state calls the ingestion Lambda with `{"action": "update_state"}`. If the Lambda was deployed without `STATE_TABLE` set (e.g., backfill deployment or misconfigured environment), `DYNAMODB` is `None` at module level. Without a guard, `DYNAMODB.put_item()` would raise `AttributeError: 'NoneType' object has no attribute 'put_item'` — an opaque error in execution history.
**Decision:** Add `if DYNAMODB is None or STATE_TABLE is None: raise RuntimeError("update_state action requires STATE_TABLE env var — Lambda is not configured for incremental mode")` at the top of `_handle_update_state`.
**Rationale:** Fail-fast with a descriptive error is more debuggable than a mid-call `AttributeError`. The message names the misconfiguration cause directly.

### D30: States.TaskFailed added to Lambda-Update-State retry

**Context:** DynamoDB throttles (`ProvisionedThroughputExceededException`) are surfaced by Step Functions as `States.TaskFailed` when the Lambda raises them, not as `Lambda.ServiceException`. The original retry only covered `Lambda.ServiceException`/`Lambda.AWSLambdaException`/`Lambda.TooManyRequestsException` — DynamoDB throttles at the state commit step were not retried, causing unnecessary pipeline failures during brief capacity spikes.
**Decision:** Add `"States.TaskFailed"` to the `Lambda-Update-State` Retry `ErrorEquals` list. Increase `MaxAttempts` to 3 (from 2) because a DynamoDB throttle at this final step — after Glue has already succeeded — is worth a third retry attempt before failing the whole execution.
**Rationale:** Unlike earlier states (ingestion, Glue), state commit failure at the Lambda-Update-State step wastes the work already done. An extra retry attempt has asymmetric value here.

### D31: _write_partition split into serialization and write phases

**Context:** A single try-block in `_write_partition` caught both Polars/PyArrow serialization errors and `boto3.client.put_object` (S3) errors, logging them both as "S3 errors." This made Polars type errors or Arrow conversion failures hard to distinguish from genuine S3 connectivity issues in CloudWatch logs.
**Decision:** Split into two sequential try-blocks: Phase 1 catches `(PolarsError, ArrowException, ValueError, OSError)` (serialization) and logs `format={output_format}` plus exception type/message; Phase 2 catches `ClientError` only (S3 write) and logs the AWS error code.
**Rationale:** Correct error attribution speeds up debugging. A `ArrowInvalid` logged as "S3 error writing partition" wastes time investigating S3 permissions when the issue is actually a schema problem.

### D32: get_last_processed_date uses allowlist for transient DynamoDB errors

**Context:** The original implementation used a denylist (re-raise on `ResourceNotFoundException`/`AccessDeniedException`, fall back on everything else). This silently treated `ValidationException`, `SerializationException`, `ItemCollectionSizeLimitExceededException`, and any future unknown AWS error codes as "transient" — causing the pipeline to quietly re-fetch from `START_DATE` and duplicate raw S3 data on every run without any alert.
**Decision:** Invert to an allowlist (`_TRANSIENT_DYNAMODB_READ_CODES` frozenset). Only `ProvisionedThroughputExceededException`, `RequestLimitExceeded`, `ThrottlingException`, and `InternalServerError` trigger the fallback. All other error codes re-raise with a `logger.error`.
**Rationale:** The failure mode of silent fallback (data duplication) is worse than the failure mode of pipeline failure (observable, alertable). An unknown error code is far more likely to be a misconfiguration than a new transient error AWS added without notice. Allowlists are safer than denylists for safety-critical fallbacks.

### D33: Lambda-Validation-Query was missing Lambda.AWSLambdaException in Retry

**Context:** All other Lambda Task states in the ASL (`Lambda-API-Ingestion`, `Lambda-Update-State`) include `Lambda.AWSLambdaException` in their Retry `ErrorEquals`. `Lambda-Validation-Query` was missing it, creating an inconsistency with the documented retry policy in CLAUDE.md.
**Decision:** Add `Lambda.AWSLambdaException` to `Lambda-Validation-Query` Retry.
**Rationale:** A transient unhandled exception in the validation Lambda (the last step, after all data work is complete) would go directly to `Validation-Failed` with no retry, creating alert noise without the pipeline data being affected. Consistent retry policy also makes the ASL easier to audit.

### D34: BaseIngestionHandler uses make_filename as second abstract method

**Context:** `save_to_s3(data, filename)` takes a pre-computed filename. The prompt listed only `fetch_data` as abstract, but each source needs a different naming convention: Frankfurter uses `exchange_rates_{CURRENCY}_{START}_to_{END}.json`, ECB uses `ecb_rates_{START}_to_{END}.json`. Without abstracting filename generation, the base class orchestration would need source-specific conditionals or callers would need to compute filenames themselves outside the class.
**Decision:** Add `make_filename(start_date, end_date) -> str` as a second abstract method alongside `fetch_data`. The base `_incremental_ingest` and `_static_ingest` call `self.fetch_data(...)` then `self.make_filename(...)` then `self.save_to_s3(data, filename)`.
**Rationale:** The template method pattern — subclasses fill in the steps, base class owns the sequence — is the right abstraction here. Adding a second abstract method is minimal overhead and removes all conditional branching from the base class.

### D35: ECB SDMX-JSON parsed into normalised FXLake format

**Context:** The ECB Statistics Data Warehouse returns SDMX-JSON — a generic statistical data format where series are keyed by dimension indices (`"0:2:0:0:0"`) and observations by time-period index. Storing raw SDMX-JSON would require the Glue transform to understand two completely different formats.
**Decision:** `ECBHandler._parse_ecb_response` normalises the SDMX-JSON into `{"base": "EUR", "source": "ecb", "rates": {date: {ccy: rate}}}` before writing to S3. The Glue transform only needs one format to process.
**Rationale:** The ingestion layer is the right place to normalise — raw S3 data should be source-specific but format-consistent. Pushing SDMX parsing into Glue would require Glue to know about ECB's data model, coupling the transform to the source.

### D36: Base class save_to_s3 drops source-specific S3 metadata

**Context:** The original Frankfurter-specific `save_to_s3` stored `start_date`, `end_date`, and `base_currency` in S3 object metadata. The base class `save_to_s3(data, filename)` only has access to `source_name` — it doesn't know the currency or date params without receiving them as arguments.
**Decision:** Base class metadata includes only `{"source": self.source_name}`. Dates are already encoded in the filename. `base_currency` is Frankfurter-specific and not part of the base interface.
**Rationale:** S3 object metadata is informational, not load-bearing — the pipeline uses the filename and object content, not metadata. Simplifying to `source` keeps the base class free of source-specific fields. If per-source metadata is needed later, `save_to_s3` can be overridden.

### D37: Incremental test strategy: monkeypatch.setenv replaces patch.object

**Context:** The old incremental tests used `patch.object(ingestion, "STATE_TABLE", TEST_STATE_TABLE)` and `patch.object(ingestion, "DYNAMODB", aws_mock["dynamodb"])` to inject state into module-level variables. After refactoring, there are no module-level variables — `state_table` and `_dynamodb` are set in `__init__` by reading `os.getenv("STATE_TABLE")` and calling `boto3.client("dynamodb")`.
**Decision:** Tests use `monkeypatch.setenv("STATE_TABLE", TEST_STATE_TABLE)` before calling `lambda_handler`. Since `mock_aws()` is active via the `aws_mock` fixture, `boto3.client("dynamodb")` inside `__init__` is automatically intercepted by moto.
**Rationale:** `monkeypatch.setenv` is the correct seam — it's what the production code actually reads. `patch.object` on module-level vars was a testing workaround for the old module-level design. The new approach is simpler and doesn't require the test to know about internal implementation details.

### D38: Parallel state Retry placement — branch-level only, not on Parallel state itself

**Context:** The `Parallel-Ingestion` state runs two Lambda invocations concurrently. Each branch can fail due to transient Lambda errors.
**Decision:** Retry blocks are placed on each branch's individual Lambda Task, not on the Parallel state itself.
**Rationale:** Adding retries at both levels would cause double-retry for transient Lambda errors: the branch retries first, then if it still fails, the Parallel state retries the entire parallel execution (re-running the successful branch unnecessarily). Branch-level retries are sufficient and correctly scoped.

### D39: `end_date` always present in `no_new_data` response

**Context:** The `Parallel-Ingestion` state's two branches can independently return `no_new_data` or `ok`. The `Lambda-Update-FX-State` and `Lambda-Update-ECB-State` steps always read `$.parallel_results.fx.Payload.end_date` and `$.parallel_results.ecb.Payload.end_date` respectively — even when only one source had new data.
**Decision:** `_incremental_ingest` always includes `end_date` in the response dict, including when `status: "no_new_data"`.
**Rationale:** If a source returns `no_new_data` without `end_date`, the corresponding Update-State step would fail with a States.Runtime `$.parallel_results.*.Payload.end_date` path error. The `end_date` in a `no_new_data` response is the current `fetch_end` value — the state commit is idempotent (writing the same date that's already stored), so there is no harm in always including it.

### D20: Test coverage significantly exceeded plan

**Context:** Day 1 planned ~20 tests with 80%+ coverage as nice-to-have.
**Delivered:** 37 tests, 96% coverage.
**Reason:** Three rounds of iterative PR review (`/pr-review-toolkit:review-pr`) surfaced edge cases that were genuine gaps, not test padding. Each test covers a real failure mode (non-JSON API response, connection errors vs timeouts, `ClientError` vs `KeyError` distinction, parametrized Athena states). The coverage increase was organic, not targeted.


---

## Session 4A Decisions (2026-04-03)

### D40: Hybrid Glue schema — two output domains instead of one unified schema

**Context:** FRED economic indicators (GDP in billions, unemployment rate %) share no meaningful schema with FX rate pairs. Forcing them into `{base_currency, target_currency, rate}` would require nullable columns and break any downstream query that assumes FX semantics.
**Decision:** Route by filename prefix: `fred_*` → `economic_indicators/` with `{date, source, series_id, value}`; all others → `fx_rates/` with `{date, source, base_currency, target_currency, rate}`.
**Rationale:** Adding a new FX source (e.g. OpenExchangeRates) requires zero transform code changes. Adding a new data domain is a deliberate, explicit decision — by design, not by accident.

### D41: FX output path renamed exchange_rates/ → fx_rates/

**Context:** Original Glue output used `exchange_rates/` as the S3 prefix. With two domains, clarity requires the FX prefix to match the Athena table name `fx_rates`.
**Decision:** Rename output prefix to `fx_rates/` in both Glue transform and athena.tf. No migration needed — this is a development branch with no production data.
**Rationale:** Consistent naming between S3 path, Athena table, and Glue code reduces cognitive load and prevents misrouted partition projection.

### D42: FRED source detection via payload field, not filename prefix

**Context:** ECB payload includes `"source": "ecb"` (set by ECBHandler). Frankfurter raw API response has no source field. Both share the FX schema.
**Decision:** In `_detect_fx_source`: check `payload["source"]` first; fall back to filename prefix inspection (`ecb_` → "ecb", else → "frankfurter").
**Rationale:** Payload-first detection is forward-compatible — any future source that sets the field works automatically. Filename fallback is the explicit escape hatch for sources that can't annotate their own payload.

### D43: FRED "." sentinel dropped silently, empty-after-filter raises ValueError

**Context:** FRED API uses `"."` for missing/unreleased data (e.g. not-yet-published unemployment figures). These are common at the tail of a date range.
**Decision:** Drop `"."` values silently in `_parse_fred_response`. Raise `ValueError` only when the entire response consists of missing values — this prevents state from advancing on a completely empty ingest.
**Rationale:** Partial missing data is normal and expected; total absence indicates a configuration error (wrong series ID, wrong date range) that should fail loudly.

### D44: FRED API key excluded from error tracebacks via exc_info omission

**Context:** FRED API requests pass the API key as a query parameter (`?api_key=...`). Python's `requests` library stores the full URL (including query params) on the exception object as `e.request.url`. If `exc_info=True` is set on the HTTPError or RequestException logger call, CloudWatch Logs captures the full exception traceback, which includes the URL with the API key embedded.
**Decision:** Omit `exc_info=True` from `HTTPError` and `RequestException` error handlers in `FREDHandler.fetch_data`. Keep `exc_info=True` only on `Timeout` (which doesn't attach a response URL). Add `safe_url = url` (base URL, no params) before the request block and use it in all log messages.
**Rationale:** `safe_url = url` documents the intent explicitly — the base URL is safe to log; the params dict (which contains the key) is not. Removing `exc_info` from HTTP/network handlers prevents the requests library from leaking the full URL via `PreparedRequest` in the traceback. Interview talking point: "I audited every log statement in code that handles API keys to ensure nothing reaches CloudWatch."

### D45: Split `except (JSONDecodeError, KeyError, ValueError)` into separate blocks in Glue transform

**Context:** `_process_fx_key` and `_process_economic_key` both had a single `except (json.JSONDecodeError, KeyError, ValueError)` catch with a generic message. This made it impossible to distinguish in CloudWatch Logs whether a failure was a parse error, a missing field, or a type conversion error — all three have different root causes and different fixes.
**Decision:** Split into three separate `except` blocks, each with a distinct log message: "Invalid JSON", "Missing required field", "Invalid value".
**Rationale:** Correct error attribution is the first step to a correct fix. A `KeyError` logged as "JSON decode error" sends an operator to investigate the S3 object format when the problem is actually a missing field in a well-formed JSON. The extra 6 lines of code pay for themselves the first time the logs are searched during an incident.

### D46: Cross-domain isolation verified by dedicated tests

**Context:** The Glue `process_key` router uses a filename prefix heuristic (`fred_*` → economic domain, all others → FX domain). A typo in the prefix string or a change to the file naming convention in one of the Lambda handlers could silently route files to the wrong domain — writing FRED data into `fx_rates/` or FX data into `economic_indicators/`.
**Decision:** Added `TestCrossDomainIsolation` class in `test_glue_transform.py` with 3 tests: mixed-domain `main()` call asserts 2 keys per domain; FRED-only run asserts `fx_rates/` keys == []; FX-only run asserts `economic_indicators/` keys == [].
**Rationale:** Schema misrouting is a silent failure — Parquet writes succeed, Athena queries return wrong results, and no error is raised. The tests catch this at the routing layer before any downstream query is affected.

## Session 5A Decisions

### D47: Pure functions over class-based quality checks

**Context:** The planned design called for a `DataQualityChecker` class with methods. Quality checks have no shared mutable state — each check takes a DataFrame and returns a result.
**Decision:** Implemented as pure functions (`check_no_nulls`, `check_positive_values`, etc.) with a frozen `QualityResult` dataclass and `CheckLevel` enum, rather than a stateful class.
**Rationale:** Pure functions are simpler to test (no setup/teardown), compose better in domain runners (`run_fx_checks`, `run_economic_checks`), and enforce immutability. The module has zero AWS dependencies, making it testable without moto.

### D48: Quality module in glue/ with --extra-py-files, not lambda/common/

**Context:** Plan suggested `lambda/common/quality.py`. Quality checks run inside the Glue Python Shell job, not Lambda. Glue Python Shell doesn't have the Lambda layer's `sys.path` setup.
**Decision:** Placed `quality.py` in `glue/` and uploaded to S3 via `aws_s3_object`. Referenced via `--extra-py-files` in the Glue job's `default_arguments`.
**Rationale:** `--extra-py-files` is the standard Glue mechanism for additional modules. Keeping quality.py in `glue/` next to `glue_transform.py` matches the existing `pythonpath` config in `pyproject.toml` and ensures local tests import it the same way.

### D49: CRITICAL vs WARNING severity routing

**Context:** Not all quality failures should block the pipeline. Duplicate date+currency pairs are data quality issues but not data corruption — the downstream Athena query still returns valid results.
**Decision:** `CheckLevel.CRITICAL` (null dates, null values, non-positive rates) → quarantine full DataFrame + raise `ValueError`. `CheckLevel.WARNING` (duplicates, out-of-range rates) → publish CloudWatch metric + continue processing.
**Rationale:** CRITICAL failures indicate data that would produce incorrect query results (nulls in partition keys, negative exchange rates). WARNING failures are anomalies worth alerting on but safe to process. This prevents pipeline halts on benign issues while catching genuine data corruption.

### D50: Quality report JSON written for every file, not just failures

**Context:** Could write quality reports only on failure to reduce S3 writes.
**Decision:** Always write `{domain}/quality_reports/{stem}_quality.json` to the processed bucket, even when all checks pass.
**Rationale:** Passing reports provide audit trail for compliance and enable trend analysis (e.g. "which files had zero warnings vs many"). The S3 cost of a small JSON per file is negligible.

### D51: Per-domain quality alarms rather than single aggregate

**Context:** Could create one `DataQualityChecksFailed` alarm without a Domain dimension, or one per domain.
**Decision:** Two separate alarms: `fxlake-data-quality-checks-failed` (Domain=fx_rates) and `fxlake-data-quality-checks-failed-econ` (Domain=economic_indicators). Single `RecordsQuarantined` alarm without domain dimension.
**Rationale:** Per-domain alarms let operators immediately identify which data source is failing. Quarantine is rare and always urgent, so a single alarm suffices — the CloudWatch metric already carries the Domain dimension for drill-down.

### D52: Data freshness query replaces SELECT * LIMIT 100

**Context:** The original Athena validation query (`SELECT * FROM fx_rates LIMIT 100`) only verified that *some* data existed but not whether it was *recent*.
**Decision:** Replaced with `SELECT MAX(date) AS latest_date, COUNT(*) AS total_records FROM fx_rates`. Validation Lambda parses these values, checks if `latest_date` is within 2 days, and publishes a `StaleFXData` CloudWatch metric.
**Rationale:** Freshness is the primary concern for a daily pipeline — stale data means ingestion or transform silently failed. The 2-day threshold accounts for weekends/holidays when FX markets are closed. The new query is cheaper (single aggregate scan vs full row fetch) and provides actionable signal.

### D53: Skip optional quality report Lambda

**Context:** Session 6A plan included an optional Lambda to read quality report JSONs from S3 and publish a summary to SNS.
**Decision:** Skipped. Quality reports are already written by the Glue job for every file. The existing CloudWatch alarms (DataQualityChecksFailed, RecordsQuarantined) provide real-time alerting.
**Rationale:** A summary Lambda would duplicate alerting already handled by CloudWatch alarms + SNS. Adding another Lambda increases maintenance cost without proportional value. Operators can inspect quality reports directly in S3 when investigating an alarm.

---

## Session 5B Decisions (2026-04-05) — PR Review Fixes

### D54: QualityResult `__post_init__` invariant validation

**Context:** `QualityResult` is a frozen dataclass, but nothing prevented constructing an inconsistent instance (e.g., `passed=True` with `failing_row_count=5`). A bug in a check function could produce a result that claims success while reporting failures.
**Decision:** Added `__post_init__` that raises `ValueError` for `passed=True, failing_row_count>0` and `passed=False, failing_row_count<=0`.
**Rationale:** Frozen dataclasses guarantee no mutation after construction, but not validity at construction. `__post_init__` closes the gap — invalid states become unrepresentable. Two tests added to `test_data_quality.py` to verify.

### D55: `_enforce_quality` parameter typed as `Callable` instead of `object`

**Context:** The `run_checks_fn` parameter in `_enforce_quality` was typed as `object`, bypassing type checker validation.
**Decision:** Changed to `Callable[[pl.DataFrame], List[QualityResult]]`, added `Callable` and `QualityResult` imports.
**Rationale:** Precise type annotation lets mypy/pyright catch misuse at static analysis time rather than at runtime.

### D56: StaleFXData CloudWatch alarm added to monitoring.tf

**Context:** The validation Lambda publishes `StaleFXData` metric when data is >2 days old, but no alarm was configured — the metric was emitted but never acted on.
**Decision:** Added `aws_cloudwatch_metric_alarm.stale_fx_data` with 5-minute period, Sum statistic, threshold >0. Added corresponding dashboard widget.
**Rationale:** A metric without an alarm is invisible. Stale data is the primary failure mode for a daily pipeline — if ingestion or transform silently fails, stale data is the symptom. The alarm closes the monitoring loop.

### D57: Quarantine bucket public access block

**Context:** All other S3 buckets relied on account-level public access settings, but the quarantine bucket holds potentially sensitive failed-quality data and had no explicit public access block.
**Decision:** Added `aws_s3_bucket_public_access_block.quarantine` with all four block flags enabled.
**Rationale:** Defense in depth — if account-level settings are accidentally modified, the bucket-level block prevents public exposure. Quarantine data may contain PII or financial data that failed validation, making it a higher-risk target.

### D58: Defensive `.get("Data", [])` guard in validation Lambda

**Context:** `_parse_freshness_result` assumed Athena result rows always contain a `"Data"` key. A malformed or empty row (e.g., from a query timeout or partial result) would raise `KeyError`.
**Decision:** Changed `rows[1]["Data"]` to `rows[1].get("Data", [])` with a length check, treating missing data as empty results.
**Rationale:** Athena results are an external boundary — defensive parsing prevents a `KeyError` from masking the real issue (empty/stale data). Added test `test_malformed_athena_row_returns_empty` to verify.

### D59: Metric publish error logging includes exception type

**Context:** `_publish_metric` in `glue_transform.py` logged `f"Failed to publish metric: {e}"` — for non-`ClientError` exceptions, the message lacked the exception class name.
**Decision:** Changed to `f"Failed to publish metric {metric_name}: {type(e).__name__}: {e}"` with `exc_info=True`.
**Rationale:** `type(e).__name__` distinguishes `TypeError` from `ClientError` without needing to read the full traceback. `exc_info=True` preserves the stack trace in CloudWatch for debugging.
