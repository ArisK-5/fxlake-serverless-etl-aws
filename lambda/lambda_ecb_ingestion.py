import json
import logging
import os
from typing import Any

import requests
from common.base import BaseIngestionHandler

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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

    def fetch_data(self, start_date: str, end_date: str) -> dict:
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
            logger.debug("Successfully fetched exchange rates from ECB API")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout fetching {url}: {e}")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error fetching {url}: status={e.response.status_code}",
                exc_info=True,
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching {url}: {e}", exc_info=True)
            raise

        # JSON decode
        try:
            raw = resp.json()
        except json.JSONDecodeError:
            logger.error(
                f"ECB API returned non-JSON response from {url}: "
                f"status={resp.status_code}, body={resp.text[:200]}",
                exc_info=True,
            )
            raise

        # SDMX structural parse
        try:
            return self._parse_ecb_response(raw)
        except (KeyError, IndexError, ValueError) as e:
            logger.error(
                f"Failed to parse ECB SDMX response from {url} "
                f"(start={start_date}, end={end_date}, status={resp.status_code}): "
                f"{type(e).__name__}: {e}. Body (first 500 chars): {resp.text[:500]}",
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
            logger.warning(
                "ECB SDMX response contained no rate observations for the requested period. "
                f"dataSets[0].series had {len(raw['dataSets'][0]['series'])} series entries. "
                "Returning empty rates dict — verify ECB API response for this date range."
            )

        return {"base": "EUR", "source": "ecb", "rates": rates}


def lambda_handler(event: dict, _context: Any) -> dict:
    return ECBHandler().run(event, _context)
