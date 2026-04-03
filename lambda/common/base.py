import json
import logging
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
            logger.debug(f"Saved data to s3://{self.raw_bucket}/{filename}")
            return filename
        except ClientError as e:
            logger.error(
                f"Failed to write s3://{self.raw_bucket}/{filename}: "
                f"{e.response['Error']['Code']}",
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
                    f"Permanent DynamoDB error reading from table {self.state_table} "
                    f"(code={code}). Cannot safely determine incremental start date. "
                    "Check that the table exists and the Lambda execution role "
                    "has GetItem permission.",
                    exc_info=True,
                )
                raise
            logger.warning(
                f"Transient DynamoDB read error for source={self.source_name}, "
                f"table={self.state_table} (code={code}), "
                f"defaulting to start_date={self.start_date}"
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
                f"Updated DynamoDB state: table={self.state_table}, "
                f"source={self.source_name}, last_processed_date={processed_date}"
            )
        except ClientError as e:
            logger.error(
                f"Failed to update state in DynamoDB table {self.state_table} "
                f"(source={self.source_name}, processed_date={processed_date}): "
                f"{e.response['Error']['Code']}",
                exc_info=True,
            )
            raise

    # ------------------------------------------------------------------
    # Orchestration entry point
    # ------------------------------------------------------------------

    def run(self, event: dict, _context: Any) -> dict:
        """Route the Lambda event to update_state, incremental, or static ingest."""
        if event.get("action") == "update_state":
            return self._handle_update_state(event)
        if self.state_table:
            return self._incremental_ingest()
        return self._static_ingest()

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
                f"update_state called without 'end_date'. Received keys: {list(event.keys())}"
            )
            raise ValueError("Missing required field 'end_date' in update_state event")
        self.update_last_processed(end_date)
        return {"status": "state_updated", "last_processed_date": end_date}

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
                f"Already caught up (source={self.source_name}, "
                f"last_processed_date={last_processed}), no new data to fetch"
            )
            # end_date is included so Step Functions can always read Payload.end_date
            # regardless of status (both branches of Parallel-Ingestion must supply it).
            return {
                "status": "no_new_data",
                "last_processed_date": last_processed,
                "end_date": fetch_end,
            }

        data = self.fetch_data(fetch_start, fetch_end)
        filename = self.make_filename(fetch_start, fetch_end)
        self.save_to_s3(data, filename, start_date=fetch_start, end_date=fetch_end)

        logger.info(
            f"Incremental ingestion succeeded: source={self.source_name}, "
            f"{fetch_start}..{fetch_end}, file: {filename}"
        )
        return {
            "status": "ok",
            "key": filename,
            "start_date": fetch_start,
            "end_date": fetch_end,
            "source": self.source_name,
        }

    def _static_ingest(self) -> dict:
        """Fetch the full configured date range (start_date..end_date)."""
        data = self.fetch_data(self.start_date, self.end_date)
        filename = self.make_filename(self.start_date, self.end_date)
        self.save_to_s3(data, filename, start_date=self.start_date, end_date=self.end_date)

        logger.info(
            f"Static ingestion succeeded: source={self.source_name}, file: {filename}"
        )
        return {
            "status": "ok",
            "key": filename,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "source": self.source_name,
        }
