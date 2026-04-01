"""Stage 2: Evaluate tickets with LLM and patch SLA ratings."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.clients.claude_client import ClaudeClient
from src.clients.openai_client import OpenAIClient
from src.config import AppConfig
from src.models.evaluation import EvaluationResult
from src.models.ticket import RawTicket
from src.storage.database import Database
from src.storage.file_store import FileStore
from src.utils.sla import patch_sla_and_ratings
from src.utils.token_counter import estimate_tokens, truncate_ticket_json

logger = logging.getLogger(__name__)


class Evaluator:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._file_store = FileStore(config)
        self._db = Database(config.output.database)
        self._prompt = self._load_prompt()
        self._llm = self._build_llm_client()

    def _load_prompt(self) -> str:
        path = Path(self._config.evaluation.prompt_file)
        if not path.exists():
            raise FileNotFoundError(f"Evaluation prompt not found: {path}")
        return path.read_text()

    def _build_llm_client(self):
        provider = self._config.llm.provider
        cfg = self._config.llm.active
        rl = self._config.llm.rate_limit
        if provider == "claude":
            return ClaudeClient(cfg, rl)
        return OpenAIClient(cfg, rl)

    async def evaluate_all(
        self,
        ticket_data_list: list[dict],
        force: bool = False,
    ) -> list[EvaluationResult]:
        """Evaluate a list of raw ticket dicts; return successful results."""
        sem = asyncio.Semaphore(self._config.pipeline.concurrent_evaluations)
        tasks = [
            self._evaluate_one(ticket_data, sem, force)
            for ticket_data in ticket_data_list
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = []
        for ticket_data, result in zip(ticket_data_list, results):
            tid = ticket_data.get("Ticket_Metadata", {}).get("ticket", {}).get("id", "?")
            if isinstance(result, Exception):
                logger.error("Evaluation failed for ticket %s: %s", tid, result)
            else:
                successes.append(result)
        return successes

    async def _evaluate_one(
        self,
        ticket_data: dict,
        sem: asyncio.Semaphore,
        force: bool,
    ) -> EvaluationResult:
        ticket_id = ticket_data["Ticket_Metadata"]["ticket"]["id"]
        prompt_version = self._config.evaluation.prompt_version

        async with sem:
            # Skip if already evaluated with this prompt version
            if not force and self._config.evaluation.skip_if_evaluated:
                if self._db.has_evaluation(ticket_id, prompt_version):
                    logger.debug("Ticket %s already evaluated (v%s) — skipping", ticket_id, prompt_version)
                    existing = self._file_store.load_eval(ticket_id, prompt_version)
                    if existing:
                        return existing

            logger.info("Evaluating ticket %s with %s", ticket_id, self._config.llm.provider)

            # Truncate if needed
            max_tokens = self._config.llm.active.max_input_tokens
            ticket_json_str = json.dumps(ticket_data, indent=2)
            if estimate_tokens(ticket_json_str) > max_tokens:
                logger.info("Ticket %s exceeds token limit — truncating", ticket_id)
                ticket_data = truncate_ticket_json(ticket_data, max_tokens)
                ticket_json_str = json.dumps(ticket_data, indent=2)

            # Call LLM with retry on token limit
            raw_result = await self._call_llm_with_token_retry(
                ticket_json_str=ticket_json_str,
                ticket_data=ticket_data,
                max_tokens=max_tokens,
            )

            # Parse and validate
            result = EvaluationResult.model_validate(raw_result)
            result.prompt_version = prompt_version
            result.llm_provider = self._config.llm.provider
            result.llm_model = self._config.llm.active.model

            # Patch SLA ratings using authoritative Zendesk data
            raw_ticket = RawTicket(**ticket_data)
            ticket_obj = raw_ticket.get_ticket()
            metrics_obj = raw_ticket.get_metrics()
            result = patch_sla_and_ratings(result, metrics_obj, ticket_obj, self._config.evaluation)

            # Save to disk
            eval_path = self._file_store.save_eval(result, prompt_version)

            # Save to DB (mark old evals as not-latest first)
            self._db.mark_old_evaluations(ticket_id)
            self._db.insert_evaluation(result, str(eval_path))

            logger.info(
                "Ticket %s evaluated: score=%.2f band=%s confidence=%s",
                ticket_id,
                result.aggregate_score.numeric if result.aggregate_score else 0,
                result.aggregate_score.band if result.aggregate_score else "?",
                result.evaluator_confidence,
            )
            return result

    async def _call_llm_with_token_retry(
        self,
        ticket_json_str: str,
        ticket_data: dict,
        max_tokens: int,
        max_truncation_rounds: int = 3,
    ) -> dict:
        """Call LLM; if token limit exceeded, truncate further and retry."""
        from src.clients.claude_client import _TokenLimitError as ClaudeTLE
        from src.clients.openai_client import _TokenLimitError as OpenAITLE

        for round_ in range(max_truncation_rounds + 1):
            try:
                return await self._llm.evaluate(self._prompt, ticket_json_str)
            except (ClaudeTLE, OpenAITLE):
                if round_ >= max_truncation_rounds:
                    raise
                # Reduce target by 20% each round
                new_max = int(max_tokens * (0.8 ** (round_ + 1)))
                logger.warning(
                    "Token limit exceeded — re-truncating to %d tokens (round %d)",
                    new_max, round_ + 1,
                )
                ticket_data = truncate_ticket_json(ticket_data, new_max)
                ticket_json_str = json.dumps(ticket_data, indent=2)

        raise RuntimeError("Could not fit ticket within LLM token limit after truncation")
