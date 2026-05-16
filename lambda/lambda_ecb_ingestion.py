import json
import os
from typing import Any

import requests
from common.base import BaseIngestionHandler
from common.logging import configure_logger

logger = configure_logger("ecb")

_DEFAULT_ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data"


class ECBHandler(BaseIngestionHandler):
    """Ingestion handler for the ECB Statistics Data Warehouse FX rates API.

    Fetches daily EUR-based exchange rates via the SDMX-JSON API and normalises
    the response into ``{"base": "EUR", "source": "ecb", "rates": {date: {ccy: rate}}}``.
    """

    def __init__(self) -> None:
        super().__init__(
            source_name="ecb",
            raw_bucket=os.environ["RAW_BUCKET"],
            state_table=os.getenv("STATE_TABLE"),
            start_date=os.environ["START_DATE"],
            end_date=os.environ["END_DATE"],
        )
        # ECB_BASE_URL is always set in production by Terraform; the fallback supports
        # local testing without env setup.
        self.base_url = os.getenv("ECB_BASE_URL", _DEFAULT_ECB_BASE_URL)

    def fetch_data(self, start_date: str, end_date: str) -> dict | None:
        """Fetch and normalise FX rates from the ECB SDMX-JSON API."""
        url = f"{self.base_url}/EXR/D..EUR.SP00.A"
        params = {
            "startPeriod": start_date,
            "endPeriod": end_date,
            "format": "jsondata",
        }

        # HTTP fetch — network and HTTP protocol errors
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.Timeout as e:
            logger.error(
                "Timeout fetching ECB API",
                extra={"url": url, "error": str(e)},
                exc_info=True,
            )
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(
                "HTTP error fetching ECB API",
                extra={"url": url, "status_code": e.response.status_code},
                exc_info=True,
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                "Network error fetching ECB API",
                extra={"url": url, "error": str(e)},
                exc_info=True,
            )
            raise

        # JSON decode — ECB API returns HTTP 200 with empty body when no data
        # exists for the requested period (e.g., only weekends/holidays in range)
        if not resp.content or not resp.content.strip():
            logger.info(
                "ECB API returned empty response body (no data for period)",
                extra={
                    "url": url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "status_code": resp.status_code,
                    "content_type": resp.headers.get("Content-Type"),
                    "content_length": len(resp.content),
                },
            )
            return None
        try:
            raw = resp.json()
        except json.JSONDecodeError:
            logger.error(
                "ECB API returned non-JSON response",
                extra={
                    "url": url,
                    "status_code": resp.status_code,
                    "content_length": len(resp.content),
                },
                exc_info=True,
            )
            raise

        # SDMX structural parse
        try:
            result = self._parse_ecb_response(raw)
            record_count = sum(len(v) for v in result.get("rates", {}).values())
            logger.info(
                "Fetched exchange rates from ECB API",
                extra={
                    "start_date": start_date,
                    "end_date": end_date,
                    "record_count": record_count,
                },
            )
            return result
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error(
                "Failed to parse ECB SDMX response",
                extra={
                    "url": url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
                exc_info=True,
            )
            raise

    def make_filename(self, start_date: str, end_date: str) -> str:
        return f"ecb_rates_{start_date}_to_{end_date}.json"

    def _parse_ecb_response(self, raw: dict) -> dict:
        """Parse ECB SDMX-JSON into normalised ``{date: {currency: rate}}`` form.

        The ECB returns SDMX-JSON where series are keyed by colon-delimited dimension
        indices (e.g. ``"0:2:0:0:0"``). The second colon-separated token (index [1])
        is the zero-based index into the CURRENCY dimension values array.
        Observations are keyed by TIME_PERIOD index.

        Dates with no observations (weekends, holidays) are dropped from the output.
        """
        structure = raw["structure"]["dimensions"]

        currency_dim = next(
            (d for d in structure["series"] if d["id"] == "CURRENCY"),
            None,
        )
        if currency_dim is None:
            raise ValueError(
                f"CURRENCY dimension not found in ECB SDMX structure. "
                f"Available series dimensions: {[d['id'] for d in structure['series']]}"
            )
        currencies = [v["id"] for v in currency_dim["values"]]

        time_dim = next(
            (d for d in structure["observation"] if d["id"] == "TIME_PERIOD"),
            None,
        )
        if time_dim is None:
            raise ValueError(
                f"TIME_PERIOD dimension not found in ECB SDMX observation dimensions. "
                f"Available: {[d['id'] for d in structure['observation']]}"
            )
        dates = [v["id"] for v in time_dim["values"]]

        rates: dict[str, dict[str, float]] = {d: {} for d in dates}

        for series_key, series_data in raw["dataSets"][0]["series"].items():
            currency_idx = int(series_key.split(":")[1])
            currency = currencies[currency_idx]
            for obs_idx_str, obs_values in series_data.get("observations", {}).items():
                value = obs_values[0]
                if value is not None:
                    rates[dates[int(obs_idx_str)]][currency] = value

        # Remove date slots with no observations (e.g. weekends, public holidays)
        rates = {d: r for d, r in rates.items() if r}

        if not rates:
            raise ValueError(
                "ECB SDMX response contained no rate observations for the requested period. "
                f"dataSets[0].series had {len(raw['dataSets'][0]['series'])} series entries. "
                "Verify ECB API response for this date range."
            )

        return {"base": "EUR", "source": "ecb", "rates": rates}


def lambda_handler(event: dict, _context: Any) -> dict:
    return ECBHandler().run(event, _context)
