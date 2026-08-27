"""Storage backend factory — picks SQLite (local dev) or Snowflake (remote VM).

All pipeline components call ``make_database(config)`` instead of constructing a backend
directly, so switching backends is a single config change (``storage.backend``).
Both backends expose the same method surface used by the pipeline.
"""

from __future__ import annotations

from src.config import AppConfig
from src.storage.database import Database


def make_database(config: AppConfig):
    backend = (config.storage.backend or "sqlite").lower()
    if backend == "snowflake":
        # Imported lazily so local (sqlite) runs don't require snowflake-connector-python.
        from src.storage.snowflake_database import SnowflakeDatabase
        return SnowflakeDatabase(config)
    return Database(config.output.database)
