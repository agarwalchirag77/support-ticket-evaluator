"""JSON file storage for raw tickets and evaluation results."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import AppConfig
from src.models.evaluation import EvaluationResult

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class FileStore:
    def __init__(self, config: AppConfig) -> None:
        self.tickets_dir = Path(config.output.tickets_dir)
        self.evals_dir = Path(config.output.evaluations_dir)

    def ticket_path(self, ticket_id: int, date: Optional[str] = None) -> Path:
        d = date or _today()
        return self.tickets_dir / d / f"Ticket_{ticket_id}.json"

    def eval_path(self, ticket_id: int, prompt_version: str, date: Optional[str] = None) -> Path:
        d = date or _today()
        return self.evals_dir / d / f"eval_{ticket_id}_{prompt_version}.json"

    def save_ticket(self, ticket_id: int, data: dict, date: Optional[str] = None) -> Path:
        path = self.ticket_path(ticket_id, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.debug("Saved ticket %s → %s", ticket_id, path)
        return path

    def load_ticket(self, ticket_id: int, date: Optional[str] = None) -> Optional[dict]:
        """Try today's date first, then fall back to searching all date dirs."""
        path = self.ticket_path(ticket_id, date)
        if path.exists():
            return json.loads(path.read_text())
        # Search all date subdirectories
        for p in sorted(self.tickets_dir.glob(f"*/Ticket_{ticket_id}.json"), reverse=True):
            return json.loads(p.read_text())
        return None

    def save_eval(
        self, result: EvaluationResult, prompt_version: str, date: Optional[str] = None
    ) -> Path:
        path = self.eval_path(int(result.ticket_id), prompt_version, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2))
        logger.debug("Saved eval %s → %s", result.ticket_id, path)
        return path

    def load_eval(
        self, ticket_id: int, prompt_version: str, date: Optional[str] = None
    ) -> Optional[EvaluationResult]:
        path = self.eval_path(ticket_id, prompt_version, date)
        if path.exists():
            return EvaluationResult.model_validate_json(path.read_text())
        for p in sorted(
            self.evals_dir.glob(f"*/eval_{ticket_id}_{prompt_version}.json"), reverse=True
        ):
            return EvaluationResult.model_validate_json(p.read_text())
        return None

    def iter_ticket_paths(self, from_date: Optional[str] = None, to_date: Optional[str] = None):
        """Yield all ticket JSON paths, optionally filtered by date range."""
        for date_dir in sorted(self.tickets_dir.iterdir()):
            if not date_dir.is_dir():
                continue
            d = date_dir.name
            if from_date and d < from_date[:10]:
                continue
            if to_date and d > to_date[:10]:
                continue
            yield from sorted(date_dir.glob("Ticket_*.json"))
