"""Persistent pipeline state (Zendesk cursor and run metadata)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import AppConfig

logger = logging.getLogger(__name__)

_EMPTY: dict = {
    "zendesk_cursor": None,
    "last_run_at": None,
    "last_successful_run_at": None,
    "last_ticket_updated_at": None,
    "last_run_stats": {},
}


class RunState:
    def __init__(self, config: AppConfig) -> None:
        self._path = Path(config.state.file)
        self._initial_fetch_from = config.state.initial_fetch_from
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return {**_EMPTY, **json.loads(self._path.read_text())}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load state file: %s — starting fresh", exc)
        return {**_EMPTY}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    # ------------------------------------------------------------------
    # Zendesk cursor
    # ------------------------------------------------------------------

    @property
    def zendesk_cursor(self) -> Optional[str]:
        """The Zendesk incremental API after_cursor from the last successful run."""
        return self._data.get("zendesk_cursor")

    @property
    def initial_fetch_unix(self) -> int:
        """Unix timestamp for initial fetch (first-ever run)."""
        dt = datetime.fromisoformat(self._initial_fetch_from.replace("Z", "+00:00"))
        return int(dt.timestamp())

    def update_cursor(self, cursor: str, last_ticket_updated_at: str) -> None:
        self._data["zendesk_cursor"] = cursor
        self._data["last_ticket_updated_at"] = last_ticket_updated_at
        self._save()

    # ------------------------------------------------------------------
    # Run bookkeeping
    # ------------------------------------------------------------------

    def mark_run_started(self) -> None:
        self._data["last_run_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def mark_run_complete(self, stats: dict) -> None:
        self._data["last_successful_run_at"] = datetime.now(timezone.utc).isoformat()
        self._data["last_run_stats"] = stats
        self._save()
        logger.info("State saved: %s", stats)
