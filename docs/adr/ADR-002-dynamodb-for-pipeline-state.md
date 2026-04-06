# ADR-002: DynamoDB for pipeline state tracking

**Status:** Accepted
**Date:** 2026-03-28

## Context

FXLake uses incremental processing — each source fetches only dates newer than the last successful run. This requires persistent state tracking of `last_processed_date` per source, with three options considered:

1. **SSM Parameter Store** — key/value store, free tier of 10,000 parameters, standard throughput. Simple get/put API.
2. **DynamoDB** — NoSQL database, pay-per-request billing, atomic writes, composite keys, TTL support.
3. **S3 marker files** — write a file like `state/frankfurter/last_processed.json` after each run. Read-after-write consistent since 2020.

Requirements:
- Store `last_processed_date` independently for each of 3+ sources
- Atomic updates (no partial writes if Lambda times out mid-write)
- Read-before-write pattern (read current state, compute date range, write new state after Glue succeeds)
- Step Functions saga pattern: state must only be committed after downstream Glue job succeeds

## Decision

Use **DynamoDB** with a single table `fxlake-pipeline-state`, composite key `(pipeline_id, source)`, pay-per-request billing.

```hcl
resource "aws_dynamodb_table" "pipeline_state" {
  name         = "fxlake-pipeline-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pipeline_id"
  range_key    = "source"
}
```

State record schema:
```json
{
  "pipeline_id": "fxlake",
  "source": "frankfurter|ecb|fred",
  "last_processed_date": "2024-01-31"
}
```

State is committed by separate `update_state` Lambda invocations (one per source) that run sequentially after Glue succeeds — the saga pattern ensures state never advances past what has been successfully transformed.

## Consequences

### Positive

- **Atomic writes** — `PutItem` is all-or-nothing; no risk of partial state on Lambda timeout
- **Composite key** — `(pipeline_id, source)` naturally partitions state per source, extensible to multiple pipelines
- **Pay-per-request** — at ~6 reads + 3 writes per daily run, cost is effectively zero (well within free tier)
- **Query flexibility** — can query all sources for a pipeline with `KeyConditionExpression`, useful for operational visibility
- **Transient error handling** — `BaseIngestionHandler.get_last_processed()` uses an allowlist of transient DynamoDB error codes (`ProvisionedThroughputExceededException`, `ThrottlingException`, etc.) for graceful fallback; permanent errors (wrong table, missing permissions) re-raise immediately

### Negative

- **Extra infrastructure** — one more AWS resource vs SSM Parameter Store (zero setup) or S3 (already exists)
- **Overkill for 3 records** — SSM Parameter Store would suffice for the current scale
- **DynamoDB-specific SDK calls** — low-level `get_item`/`put_item` with `{"S": value}` attribute typing, less ergonomic than SSM `get_parameter`

### Why Not SSM Parameter Store

SSM would work for 3 parameters but lacks composite keys — state would be spread across separate parameter names (`/fxlake/frankfurter/last_processed`, `/fxlake/ecb/last_processed`, etc.) with no way to atomically query all sources' state in one call. DynamoDB's composite key is a better fit for multi-source state that needs to be queried together.

### Why Not S3 Marker Files

S3 `PutObject` is atomic for the write itself, but the read-modify-write cycle (read current state, compute range, write new state) has no built-in concurrency control. DynamoDB supports conditional writes (`ConditionExpression`) if we ever need optimistic locking.
