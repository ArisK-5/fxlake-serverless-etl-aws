import json
import logging
import os
from typing import Any

import requests
from common.base import BaseIngestionHandler

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DEFAULT_FRED_BASE_URL = "https://api.stlouisfed.org/fred"


class FREDHandler(BaseIngestionHandler):
    """Ingestion handler for the FRED (Federal Reserve Economic Data) API.

    Fetches economic observations for a single configurable series and normalises
    the response into ``{"source": "fred", "series_id": "...", "observations": {date: value}}``.

    FRED uses ``"."`` as a sentinel for missing or unreleased data points. These
    are silently dropped from the observations dict so downstream transforms only
    see numeric values.
    """

    def __init__(self) -> None:
        super().__init__(
            source_name="fred",
            raw_bucket=os.environ["RAW_BUCKET"],
            state_table=os.getenv("STATE_TABLE"),
            start_date=os.environ["START_DATE"],
            end_date=os.environ["END_DATE"],
        )
        # FRED_BASE_URL is set by Terraform in production; the default supports
        # local testing and direct invocations without full env setup.
        self.fred_base_url = os.getenv("FRED_BASE_URL", _DEFAULT_FRED_BASE_URL)
        self.api_key = os.environ["FRED_API_KEY"]
        self.series_id = os.environ["FRED_SERIES"]

    def fetch_data(self, start_date: str, end_date: str) -> dict:
        """Fetch and normalise economic observations from the FRED API."""
        url = f"{self.fred_base_url}/series/observations"
        params = {
            "series_id": self.series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": start_date,
            "observation_end": end_date,
        }

        # safe_url omits query params (which carry the API key) to prevent
        # credential leakage in CloudWatch logs and any log-shipping destinations.
        safe_url = url

        # HTTP fetch — network and HTTP protocol errors
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            logger.debug(
                f"Successfully fetched {self.series_id} observations from FRED API"
            )
        except requests.exceptions.Timeout as e:
            logger.error(
                f"Timeout fetching {safe_url}: series={self.series_id}, {e}",
                exc_info=True,
            )
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error fetching {safe_url}: status={e.response.status_code}",
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Network error fetching {safe_url}: {type(e).__name__}: {e}",
            )
            raise

        # JSON decode
        try:
            raw = resp.json()
        except json.JSONDecodeError:
            logger.error(
                f"FRED API returned non-JSON response from {safe_url}: "
                f"status={resp.status_code}, body={resp.text[:200]}",
                exc_info=True,
            )
            raise

        # Structural parse
        try:
            return self._parse_fred_response(raw)
        except (KeyError, TypeError, ValueError) as e:
            logger.error(
                f"Failed to parse FRED response from {safe_url} "
                f"(series={self.series_id}, start={start_date}, end={end_date}): "
                f"{type(e).__name__}: {e}. Body (first 500 chars): {resp.text[:500]}",
                exc_info=True,
            )
            raise

    def _parse_fred_response(self, raw: dict) -> dict:
        """Parse FRED API JSON into normalised ``{date: value}`` observations.

        FRED uses ``"."`` as a sentinel for missing or unreleased data points —
        these are dropped so the output contains only numeric float values.

        Args:
            raw: Parsed JSON from FRED ``/series/observations`` endpoint.

        Returns:
            ``{"source": "fred", "series_id": <series>, "observations": {date: float}}``.

        Raises:
            ValueError: If the response has no ``observations`` key, or all
                observations are missing values (all ``"."``).
            KeyError: If individual observation objects lack ``date`` or ``value``.
        """
        observations_list = raw.get("observations")
        if observations_list is None:
            raise ValueError(
                "FRED response missing 'observations' key. "
                f"Available keys: {list(raw.keys())}"
            )

        observations: dict[str, float] = {}
        for obs in observations_list:
            value_str = obs["value"]
            if value_str == ".":
                # FRED sentinel for missing/unreleased data — skip silently
                continue
            observations[obs["date"]] = float(value_str)

        if not observations:
            raise ValueError(
                f"FRED response contained no valid observations for series={self.series_id}. "
                f"All {len(observations_list)} observations had missing values ('.'). "
                "Verify the series ID and date range."
            )

        return {
            "source": "fred",
            "series_id": self.series_id,
            "observations": observations,
        }

    def make_filename(self, start_date: str, end_date: str) -> str:
        series_lower = self.series_id.lower()
        return f"fred_{series_lower}_{start_date}_to_{end_date}.json"


def lambda_handler(event: dict, _context: Any) -> dict:
    return FREDHandler().run(event, _context)
