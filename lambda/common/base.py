import json
import os
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from common.logging import Timer, configure_logger, inject_request_id

# X-Ray tracing — instruments boto3 and requests when running in Lambda.
# Skipped gracefully in local/test environments where aws-xray-sdk is absent.
if os.getenv("AWS_XRAY_DAEMON_ADDRESS"):
    try:
        from aws_xray_sdk.core import patch_all

        patch_all()
    except ImportError:
        pass

logger = configure_logger("fxlake-ingestion")

PIPELINE_ID = "fxlake"

# Known-transient DynamoDB error codes that warrant a graceful fallback to start_date.
# All other ClientError codes are re-raised — an unknown code is more likely a
# misconfiguration (wrong table name, bad key schema, missing permission) than a
# transient issue, and silent fallback would cause the pipeline to re-process from
# start_date silently duplicating data.
_TRANSIENT_DYNAMODB_READ_CODES = frozenset({
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "ThrottlingException",
    "InternalServerError",
})


class BaseIngestionHandler(ABC):
    """Abstract base for all FXLake ingestion Lambda handlers.

    Subclasses must implement ``fetch_data`` and ``make_filename``.
    All DynamoDB state management and Step Functions orchestration logic
    lives here so every source shares the same saga/checkpoint pattern.
    """

    def __init__(
        self,
        source_name: str,
        raw_bucket: str,
        state_table: str | None,
        start_date: str,
        end_date: str,
    ) -> None:
        self.source_name = source_name
        self.raw_bucket = raw_bucket
        self.state_table = state_table
        self.start_date = start_date
        self.end_date = end_date
        self._s3 = boto3.client("s3")
        self._dynamodb = boto3.client("dynamodb") if state_table else None

    # ------------------------------------------------------------------
    # Abstract interface — implemented by each source handler
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_data(self, start_date: str, end_date: str) -> dict:
        """Fetch raw data from the source for the given date range."""

    @abstractmethod
    def make_filename(self, start_date: str, end_date: str) -> str:
        """Return the S3 key to use for the given date range."""

    # ------------------------------------------------------------------
    # Concrete shared methods
    # ------------------------------------------------------------------

    def save_to_s3(
        self, data: dict, filename: str, start_date: str = "", end_date: str = ""
    ) -> str:
        """Serialise *data* as JSON and write it to S3 with ContentType 'application/json'
        and a 'source' metadata tag. Returns the filename.

        *start_date* and *end_date* are optional; when provided they are written as
        S3 object metadata so operators can use ``s3api head-object`` to determine the
        date range without downloading the file.
        """
        body = json.dumps(data)
        metadata: dict[str, str] = {"source": self.source_name}
        if start_date:
            metadata["start_date"] = start_date
        if end_date:
            metadata["end_date"] = end_date
        try:
            self._s3.put_object(
                Bucket=self.raw_bucket,
                Key=filename,
                Body=body,
                ContentType="application/json",
                Metadata=metadata,
            )
            logger.debug(
                "Saved data to S3",
                extra={"bucket": self.raw_bucket, "key": filename},
            )
            return filename
        except ClientError as e:
            logger.error(
                "Failed to write to S3",
                extra={
                    "bucket": self.raw_bucket,
                    "key": filename,
                    "error_code": e.response["Error"]["Code"],
                },
                exc_info=True,
            )
            raise

    def get_last_processed(self) -> str:
        """Read last_processed_date from DynamoDB; returns start_date if no entry exists.

        On transient DynamoDB errors falls back to start_date with a warning so the
        pipeline can still run. "Transient" is defined by the ``_TRANSIENT_DYNAMODB_READ_CODES``
        allowlist (module-level constant) — any error code not in that set re-raises
        immediately, including ``ResourceNotFoundException``, ``AccessDeniedException``,
        and any unrecognised code. Unknown codes are treated as permanent to prevent
        silent data duplication.
        """
        try:
            resp = self._dynamodb.get_item(
                TableName=self.state_table,
                Key={
                    "pipeline_id": {"S": PIPELINE_ID},
                    "source": {"S": self.source_name},
                },
            )
            item = resp.get("Item")
            if item:
                return item["last_processed_date"]["S"]
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in _TRANSIENT_DYNAMODB_READ_CODES:
                logger.error(
                    "Permanent DynamoDB error reading state",
                    extra={
                        "table": self.state_table,
                        "source": self.source_name,
                        "error_code": code,
                    },
                    exc_info=True,
                )
                raise
            logger.warning(
                "Transient DynamoDB read error, falling back to start_date",
                extra={
                    "source": self.source_name,
                    "table": self.state_table,
                    "error_code": code,
                    "fallback_date": self.start_date,
                },
            )
        return self.start_date

    def update_last_processed(self, processed_date: str) -> None:
        """Write last_processed_date to DynamoDB state table."""
        try:
            self._dynamodb.put_item(
                TableName=self.state_table,
                Item={
                    "pipeline_id": {"S": PIPELINE_ID},
                    "source": {"S": self.source_name},
                    "last_processed_date": {"S": processed_date},
                },
            )
            logger.info(
                "Updated DynamoDB state",
                extra={
                    "table": self.state_table,
                    "source": self.source_name,
                    "last_processed_date": processed_date,
                },
            )
        except ClientError as e:
            logger.error(
                "Failed to update DynamoDB state",
                extra={
                    "table": self.state_table,
                    "source": self.source_name,
                    "processed_date": processed_date,
                    "error_code": e.response["Error"]["Code"],
                },
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Orchestration entry point
    # ------------------------------------------------------------------

    def run(self, event: dict, context: Any) -> dict:
        """Route the Lambda event to update_state, backfill, incremental, or static ingest."""
        inject_request_id(logger, context)
        with Timer() as timer:
            if event.get("action") == "update_state":
                result = self._handle_update_state(event)
            elif event.get("mode") == "backfill":
                result = self._handle_backfill(event)
            elif self.state_table:
                result = self._incremental_ingest()
            else:
                result = self._static_ingest()
        logger.info(
            "Lambda execution complete",
            extra={
                "source": self.source_name,
                "action": event.get("action", "ingest"),
                "status": result.get("status"),
                "duration_ms": timer.duration_ms,
            },
        )
        return result

    def _handle_update_state(self, event: dict) -> dict:
        """Commit last_processed_date to DynamoDB.

        Invoked by Step Functions after Glue succeeds (FX source) or after the FX state
        update succeeds (ECB source). Each source handler instance only updates its own
        DynamoDB record — keyed by ``(pipeline_id="fxlake", source=<source_name>)``.

        Args:
            event: must contain key ``end_date`` (ISO date string, e.g. "2024-01-31").

        Returns:
            ``{"status": "state_updated", "last_processed_date": <end_date>}``

        Raises:
            RuntimeError: if state_table is not configured (handler not in incremental mode).
            ValueError: if ``end_date`` is absent or empty in the event payload.
        """
        if self._dynamodb is None or self.state_table is None:
            raise RuntimeError(
                "update_state action requires STATE_TABLE env var — "
                "handler is not configured for incremental mode"
            )
        end_date = event.get("end_date")
        if not end_date:
            logger.error(
                "update_state called without end_date",
                extra={"received_keys": list(event.keys())},
            )
            raise ValueError("Missing required field 'end_date' in update_state event")
        self.update_last_processed(end_date)
        return {"status": "state_updated", "last_processed_date": end_date}

    def _handle_backfill(self, event: dict) -> dict:
        """Validate backfill event and delegate to _perform_ingest.

        Args:
            event: must contain ``mode: "backfill"``, ``start_date``, and ``end_date``
                   (ISO 8601 format, e.g. ``"2023-01-01"``).

        Raises:
            ValueError: if ``start_date`` or ``end_date`` is missing, whitespace-only,
                        not valid ISO 8601, or if ``start_date > end_date``.
        """
        start_date = event.get("start_date")
        end_date = event.get("end_date")
        if not (start_date and start_date.strip()):
            raise ValueError("Backfill mode requires 'start_date' in event")
        if not (end_date and end_date.strip()):
            raise ValueError("Backfill mode requires 'end_date' in event")
        start_date = start_date.strip()
        end_date = end_date.strip()
        try:
            parsed_start = date.fromisoformat(start_date)
            parsed_end = date.fromisoformat(end_date)
        except ValueError as e:
            raise ValueError(f"Invalid ISO date format in backfill event: {e}") from e
        if parsed_start > parsed_end:
            raise ValueError(
                f"start_date ({start_date}) must be <= end_date ({end_date})"
            )
        return self._perform_ingest(start_date, end_date, mode="backfill")

    def _incremental_ingest(self) -> dict:
        """Fetch only dates newer than last_processed_date in DynamoDB.

        Returns ``{"status": "no_new_data", ...}`` when already caught up — the Step
        Functions ``Check-New-Data`` Choice state routes to ``Pipeline-Already-Up-To-Date``
        (Succeed state) when it sees this status value.
        Returns ``{"status": "ok", "end_date": ..., ...}`` on a successful fetch — the
        ``end_date`` key is consumed by the ``Lambda-Update-FX-State`` or
        ``Lambda-Update-ECB-State`` step post-Glue.
        Does NOT commit state to DynamoDB; that is deferred to Lambda-Update-FX-State /
        Lambda-Update-ECB-State.
        """
        last_processed = self.get_last_processed()
        fetch_start = (date.fromisoformat(last_processed) + timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        fetch_end = min(today, self.end_date)  # ISO format: string comparison == date comparison

        if fetch_start > fetch_end:
            logger.info(
                "Already caught up, no new data to fetch",
                extra={
                    "source": self.source_name,
                    "last_processed_date": last_processed,
                },
            )
            # end_date is included so Step Functions can always read Payload.end_date
            # regardless of status (both branches of Parallel-Ingestion must supply it).
            return {
                "status": "no_new_data",
                "last_processed_date": last_processed,
                "end_date": fetch_end,
            }

        return self._perform_ingest(fetch_start, fetch_end, mode="incremental")

    def _static_ingest(self) -> dict:
        """Fetch the full configured date range (start_date..end_date)."""
        return self._perform_ingest(self.start_date, self.end_date, mode="static")

    def _perform_ingest(self, start_date: str, end_date: str, mode: str = "static") -> dict:
        """Execute the core ingestion workflow: fetch, filename, save, log, return.

        This shared method consolidates the fetch → make_filename → save_to_s3 →
        log → return pattern used by backfill, incremental, and static modes.

        Args:
            start_date: ISO date string for the range start.
            end_date: ISO date string for the range end.
            mode: one of "backfill", "incremental", or "static" (for logging and response).

        Returns:
            Response dict with status "ok" and metadata (key, start_date, end_date, source,
            and "mode" only for backfill).
        """
        data = self.fetch_data(start_date, end_date)
        filename = self.make_filename(start_date, end_date)
        self.save_to_s3(data, filename, start_date=start_date, end_date=end_date)

        logger.info(
            f"{mode.capitalize()} ingestion succeeded",
            extra={
                "source": self.source_name,
                "start_date": start_date,
                "end_date": end_date,
                "key": filename,
            },
        )

        result = {
            "status": "ok",
            "key": filename,
            "start_date": start_date,
            "end_date": end_date,
            "source": self.source_name,
        }
        if mode == "backfill":
            result["mode"] = "backfill"
        return result
