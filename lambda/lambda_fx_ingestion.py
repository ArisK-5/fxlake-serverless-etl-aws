import json
import os
from typing import Any

import requests
from common.base import BaseIngestionHandler
from common.logging import configure_logger

logger = configure_logger("frankfurter")


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

    def fetch_data(self, start_date: str, end_date: str) -> dict | None:
        """Fetch exchange rates from the Frankfurter API for the given date range."""
        api_url = f"{self.base_api_url}/{start_date}..{end_date}"
        params = {"base": self.base_currency}

        try:
            resp = requests.get(api_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            record_count = sum(len(v) for v in data.get("rates", {}).values())
            logger.info(
                "Fetched exchange rates from Frankfurter API",
                extra={
                    "url": api_url,
                    "start_date": start_date,
                    "end_date": end_date,
                    "record_count": record_count,
                },
            )
            return data
        except json.JSONDecodeError:
            logger.error(
                "API returned non-JSON response",
                extra={"url": api_url, "status_code": resp.status_code},
                exc_info=True,
            )
            raise
        except requests.exceptions.Timeout as e:
            logger.error(
                "Timeout fetching Frankfurter API",
                extra={"url": api_url, "error": str(e)},
                exc_info=True,
            )
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(
                "HTTP error fetching Frankfurter API",
                extra={"url": api_url, "status_code": e.response.status_code},
                exc_info=True,
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                "Network error fetching Frankfurter API",
                extra={"url": api_url, "error": str(e)},
                exc_info=True,
            )
            raise

    def make_filename(self, start_date: str, end_date: str) -> str:
        return f"exchange_rates_{self.base_currency}_{start_date}_to_{end_date}.json"


def lambda_handler(event: dict, _context: Any) -> dict:
    return FrankfurterHandler().run(event, _context)
