import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from common.logging import configure_logger

logger = configure_logger("schema_validation")

def _resolve_schemas_dir() -> Path:
    """Resolve schemas directory — bundled in Lambda package, or repo root for local dev."""
    base = Path(__file__).resolve().parent.parent
    package_path = base / "schemas"
    if package_path.is_dir():
        return package_path
    repo_path = base.parent / "schemas"
    if repo_path.is_dir():
        return repo_path
    return package_path


_SCHEMAS_DIR = _resolve_schemas_dir()


class SchemaValidationError(Exception):
    """Raised when data fails JSON Schema validation."""

    def __init__(self, message: str, schema_name: str, errors: list[str]) -> None:
        super().__init__(message)
        self.schema_name = schema_name
        self.errors = errors


@lru_cache(maxsize=4)
def load_schema(schema_path: str) -> dict[str, Any]:
    """Load and cache a JSON Schema file.

    Args:
        schema_path: Relative path within the schemas directory
                     (e.g. "processed/fx_rates.json").
    """
    full_path = _SCHEMAS_DIR / schema_path
    with open(full_path) as f:
        return json.load(f)


def _detect_schema(data: dict[str, Any]) -> str:
    """Determine which processed schema to use based on data shape."""
    if "rates" in data:
        return "processed/fx_rates.json"
    if "observations" in data:
        return "processed/economic_indicators.json"
    raise SchemaValidationError(
        "Cannot detect schema: data has neither 'rates' nor 'observations' key",
        schema_name="unknown",
        errors=["Unrecognisable data shape"],
    )


def validate_data(data: dict[str, Any]) -> None:
    """Validate normalised data against the appropriate processed schema.

    Auto-detects the schema based on data structure. Disabled when the
    SCHEMA_VALIDATION_ENABLED env var is set to "false".

    Raises:
        SchemaValidationError: If validation fails.
    """
    if os.getenv("SCHEMA_VALIDATION_ENABLED", "true").lower() == "false":
        logger.info("Schema validation disabled via SCHEMA_VALIDATION_ENABLED")
        return

    schema_path = _detect_schema(data)
    schema = load_schema(schema_path)

    validator = jsonschema.Draft202012Validator(schema)
    validation_errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))

    if not validation_errors:
        logger.info(
            "Schema validation passed",
            extra={"schema": schema_path},
        )
        return

    error_messages = [
        f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in validation_errors
    ]

    logger.error(
        "Schema validation failed",
        extra={
            "schema": schema_path,
            "error_count": len(error_messages),
            "errors": error_messages[:10],
        },
    )

    raise SchemaValidationError(
        f"Data failed validation against {schema_path}: "
        f"{len(error_messages)} error(s)",
        schema_name=schema_path,
        errors=error_messages,
    )
