import json
import logging
import os
from typing import Any

import requests
from common.base import BaseIngestionHandler

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class FrankfurterHandler(BaseIngestionHandler):
    """Ingestion handler for the Frankfurter FX rates API."""

    def __init__(self) -> None:
        super().__init__(
            source_name="frankfurter",
            raw_bucket=os.environ["RAW_BUCKET"],
            state_table=os.getenv("STATE_TABLE"),
            start_date=os.environ["START_DATE"],
            end_date=os.environ["END_DATE"],
        )
        self.base_api_url = os.environ["BASE_API_URL"]
        self.base_currency = os.environ["BASE_CURRENCY"]

    def fetch_data(self, start_date: str, end_date: str) -> dict:
        """Fetch exchange rates from the Frankfurter API for the given date range."""
        api_url = f"{self.base_api_url}/{start_date}..{end_date}"
        params = {"base": self.base_currency}

        try:
            resp = requests.get(api_url, params=params, timeout=30)
            resp.raise_for_status()
            logger.debug("Successfully fetched exchange rates from Frankfurter API")
            return resp.json()
        except json.JSONDecodeError:
            logger.error(
                f"API returned non-JSON response from {api_url}: "
                f"status={resp.status_code}, body={resp.text[:200]}",
                exc_info=True,
            )
            raise
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout fetching {api_url}: {e}")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error fetching {api_url}: status={e.response.status_code}",
                exc_info=True,
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching {api_url}: {e}", exc_info=True)
            raise

    def make_filename(self, start_date: str, end_date: str) -> str:
        return f"exchange_rates_{self.base_currency}_{start_date}_to_{end_date}.json"


def lambda_handler(event: dict, _context: Any) -> dict:
    return FrankfurterHandler().run(event, _context)
