# ADR-003: Parallel ingestion with Step Functions

**Status:** Accepted
**Date:** 2026-03-28

## Context

FXLake ingests data from three independent sources:

| Source | API | Typical latency |
|--------|-----|----------------|
| Frankfurter | REST (frankfurter.dev) | 200-500ms |
| ECB | SDMX-JSON (sdw-wsrest.ecb.europa.eu) | 500ms-2s |
| FRED | REST (api.stlouisfed.org) | 300-800ms |

These sources have no data dependencies — each can be fetched independently. Two orchestration patterns were considered:

1. **Sequential:** Lambda-FX → Lambda-ECB → Lambda-FRED → Glue → ... Total latency = sum of all API calls.
2. **Parallel:** All three Lambdas run concurrently within a Step Functions `Parallel` state. Total latency = max of the three.

## Decision

Use a Step Functions **Parallel state** (`Parallel-Ingestion`) with three branches, one per source. Each branch invokes its Lambda independently. The Parallel state's output is reshaped by `ResultSelector` into named keys:

```json
{
  "fx.$":   "$[0]",
  "ecb.$":  "$[1]",
  "fred.$": "$[2]"
}
```

`ResultPath = "$.parallel_results"` merges the output into the execution context so downstream states (Check-New-Data, Update-*-State) can reference `$.parallel_results.fx.Payload.end_date`, etc.

A `Choice` state (`Check-New-Data`) follows: if **all three** sources return `status: "no_new_data"`, the pipeline short-circuits to `Pipeline-Already-Up-To-Date` (Succeed). Otherwise, it proceeds to Glue.

Execution input is forwarded to each Lambda via `"Payload.$" = "$"`, enabling backfill mode (`mode: "backfill"`) to pass dates through without changing the state machine definition.

## Consequences

### Positive

- **~3x faster ingestion** — parallel execution reduces wall-clock time from sum to max of API latencies (typically 2s vs 500ms-2s)
- **Independent failure handling** — each branch has its own Retry policy (3 retries, exponential backoff on `Lambda.ServiceException`, `Lambda.TooManyRequestsException`). One source's transient failure doesn't block others
- **Clean data routing** — `ResultSelector` gives each source a named key, making downstream JSONPath references readable (`$.parallel_results.ecb.Payload.end_date` vs `$.parallel_results[1].Payload.end_date`)
- **Short-circuit on no-op** — the `Check-New-Data` Choice avoids running Glue when all sources are caught up, saving ~$0.003/run
- **Extensible** — adding a new source means adding a branch to the Parallel state and a corresponding Update-State step

### Negative

- **All-or-nothing failure** — if any branch fails after retries, the entire Parallel state fails (caught by `Ingestion-Failed`). There's no partial-success path where Glue processes only the sources that succeeded
- **State machine complexity** — the ASL definition is 350+ lines. Adding error handling (7 Fail states, Retry/Catch on every Task) increases the maintenance surface
- **Ordering constraint on state updates** — after Glue, the three Update-*-State steps run sequentially (FX → ECB → FRED) to maintain a clear rollback point. True parallelism would risk partial state commits on failure

### Why Not EventBridge + Separate State Machines

Running three independent state machines (one per source) would give true isolation but would require a separate aggregation mechanism to determine when all sources have completed before triggering Glue. Step Functions Parallel state handles this natively with built-in fan-out/fan-in.

### Why Not Lambda-Only (No Step Functions)

A single orchestrator Lambda could invoke the three ingestion Lambdas concurrently (via `asyncio` or threads), but this loses Step Functions' built-in retry, catch, visual execution history, and the ability to pause for manual approval. The $0.025/1000 state transitions cost is negligible for a once-daily pipeline.
