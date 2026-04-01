"""SQLite storage for tickets, evaluations, and run history."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from src.models.evaluation import EvaluationResult

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   INTEGER PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    status      TEXT,
    channel     TEXT,
    group_id    INTEGER,
    group_name  TEXT,
    agent_name  TEXT,
    created_at  TEXT,
    closed_at   TEXT,
    json_path   TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id               INTEGER NOT NULL,
    evaluated_at            TEXT NOT NULL,
    prompt_version          TEXT NOT NULL,
    llm_provider            TEXT,
    llm_model               TEXT,
    aggregate_score         REAL,
    performance_band        TEXT,
    evaluator_confidence    TEXT,
    frt_status              TEXT,
    frt_minutes             REAL,
    ttr_status              TEXT,
    ttr_minutes             REAL,
    flags                   TEXT,       -- JSON array
    eval_json_path          TEXT,
    published_to_zendesk    INTEGER DEFAULT 0,
    published_at            TEXT,
    is_latest               INTEGER DEFAULT 1,
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS metric_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id   INTEGER NOT NULL,
    metric_id       TEXT NOT NULL,
    rating          TEXT,              -- stored as TEXT to handle N/A and integers
    rating_label    TEXT,
    FOREIGN KEY (evaluation_id) REFERENCES evaluations(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    mode            TEXT,              -- incremental | re-evaluate | fetch-only
    fetched         INTEGER DEFAULT 0,
    evaluated       INTEGER DEFAULT 0,
    published       INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    cursor_used     TEXT
);

CREATE INDEX IF NOT EXISTS idx_evaluations_ticket_id ON evaluations(ticket_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_is_latest ON evaluations(is_latest);
CREATE INDEX IF NOT EXISTS idx_metric_results_eval_id ON metric_results(evaluation_id);
"""


class Database:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._connect()
        try:
            yield conn.cursor()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        logger.debug("Database schema initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    def upsert_ticket(
        self,
        ticket_id: int,
        fetched_at: str,
        status: Optional[str],
        channel: Optional[str],
        group_id: Optional[int],
        group_name: Optional[str],
        agent_name: Optional[str],
        created_at: Optional[str],
        closed_at: Optional[str],
        json_path: Optional[str],
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets
                    (ticket_id, fetched_at, status, channel, group_id, group_name,
                     agent_name, created_at, closed_at, json_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    status     = excluded.status,
                    channel    = excluded.channel,
                    agent_name = excluded.agent_name,
                    closed_at  = excluded.closed_at,
                    json_path  = excluded.json_path
                """,
                (ticket_id, fetched_at, status, channel, group_id, group_name,
                 agent_name, created_at, closed_at, json_path),
            )

    # ------------------------------------------------------------------
    # Evaluations
    # ------------------------------------------------------------------

    def has_evaluation(self, ticket_id: int, prompt_version: str) -> bool:
        """Return True if a latest evaluation exists for this ticket + prompt version."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM evaluations WHERE ticket_id=? AND prompt_version=? AND is_latest=1",
                (ticket_id, prompt_version),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def mark_old_evaluations(self, ticket_id: int) -> None:
        """Mark all existing evaluations for a ticket as not-latest."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE evaluations SET is_latest=0 WHERE ticket_id=?",
                (ticket_id,),
            )

    def insert_evaluation(self, result: EvaluationResult, eval_json_path: str) -> int:
        """Insert evaluation + metric results; returns the new evaluation row ID."""
        sla = result.sla_status
        frt = sla.first_response_time if sla else None
        ttr = sla.resolution_time if sla else None

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO evaluations (
                    ticket_id, evaluated_at, prompt_version, llm_provider, llm_model,
                    aggregate_score, performance_band, evaluator_confidence,
                    frt_status, frt_minutes, ttr_status, ttr_minutes,
                    flags, eval_json_path, is_latest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    int(result.ticket_id),
                    result.evaluation_date,
                    result.prompt_version or "",
                    result.llm_provider or "",
                    result.llm_model or "",
                    result.aggregate_score.numeric if result.aggregate_score else None,
                    result.aggregate_score.band if result.aggregate_score else None,
                    result.evaluator_confidence,
                    frt.status if frt else None,
                    frt.value_minutes if frt else None,
                    ttr.status if ttr else None,
                    ttr.value_minutes if ttr else None,
                    json.dumps(result.flags),
                    eval_json_path,
                ),
            )
            eval_id = cur.lastrowid

            for m in result.metrics:
                cur.execute(
                    "INSERT INTO metric_results (evaluation_id, metric_id, rating, rating_label) VALUES (?, ?, ?, ?)",
                    (eval_id, m.metric_id, str(m.rating), m.rating_label),
                )

        return eval_id

    def mark_published(self, eval_id: int, published_at: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE evaluations SET published_to_zendesk=1, published_at=? WHERE id=?",
                (published_at, eval_id),
            )

    def get_unpublished_evaluations(self) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM evaluations WHERE published_to_zendesk=0 AND is_latest=1"
            ).fetchall()
        finally:
            conn.close()

    def get_evaluations_for_rerun(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        ticket_ids: Optional[list[int]] = None,
    ) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            query = "SELECT DISTINCT ticket_id FROM tickets WHERE 1=1"
            params: list = []
            if from_date:
                query += " AND created_at >= ?"
                params.append(from_date)
            if to_date:
                query += " AND created_at <= ?"
                params.append(to_date)
            if ticket_ids:
                placeholders = ",".join("?" * len(ticket_ids))
                query += f" AND ticket_id IN ({placeholders})"
                params.extend(ticket_ids)
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(self, started_at: str, mode: str, cursor_used: Optional[str] = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (started_at, mode, cursor_used) VALUES (?, ?, ?)",
                (started_at, mode, cursor_used),
            )
            return cur.lastrowid

    def complete_run(
        self,
        run_id: int,
        completed_at: str,
        fetched: int,
        evaluated: int,
        published: int,
        errors: int,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE runs SET completed_at=?, fetched=?, evaluated=?, published=?, errors=?
                WHERE id=?
                """,
                (completed_at, fetched, evaluated, published, errors, run_id),
            )
