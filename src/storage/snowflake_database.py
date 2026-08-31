"""Snowflake storage backend — drop-in replacement for the SQLite ``Database``.

Implements the same method surface the pipeline uses (see src/storage/database.py):
upsert_ticket, has_evaluation, mark_old_evaluations, insert_evaluation, mark_published,
get_ticket_eval_rows, delete_ticket_data, get_unpublished_evaluations, get_summary_stats,
get_audit_data, get_evaluations_in_range, start_run, complete_run — plus get_state/set_state
for the Zendesk cursor + run metadata (which live in Snowflake, not a local file, on the VM).

Design notes / SQLite→Snowflake parity:
- Timestamps and ``flags`` are stored as VARCHAR (ISO strings / JSON text) so every existing
  date-range predicate (``closed_at >= ?`` and the ``+ "T23:59:59Z"`` upper bound) works unchanged.
- Booleans are NUMBER(1) 0/1 (``=0``/``=1`` predicates unchanged).
- Auto-increment ids come from SEQUENCEs (``SELECT seq.NEXTVAL``) because the Snowflake connector
  has no ``lastrowid``.
- ``upsert_ticket`` uses MERGE (no ``ON CONFLICT``).
- Result rows are returned as dicts with **lowercase** keys (Snowflake upper-cases unquoted
  identifiers) so callers' ``row["id"]`` / ``dict(row)`` behavior matches the SQLite path.
- A single connection is reused and all statements are guarded by a lock (the evaluator issues
  concurrent DB calls; one Snowflake connection is not safe for concurrent cursors).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

import snowflake.connector
from snowflake.connector import DictCursor

from src.config import AppConfig
from src.models.evaluation import EvaluationResult

# Reuse the existing ``?`` placeholders across the codebase.
snowflake.connector.paramstyle = "qmark"

logger = logging.getLogger(__name__)

_DDL = [
    """CREATE TABLE IF NOT EXISTS tickets (
        ticket_id   NUMBER PRIMARY KEY,
        fetched_at  VARCHAR,
        status      VARCHAR,
        channel     VARCHAR,
        group_id    NUMBER,
        group_name  VARCHAR,
        agent_name  VARCHAR,
        created_at  VARCHAR,
        closed_at   VARCHAR,
        json_path   VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS evaluations (
        id                    NUMBER PRIMARY KEY,
        ticket_id             NUMBER NOT NULL,
        evaluated_at          VARCHAR,
        prompt_version        VARCHAR,
        llm_provider          VARCHAR,
        llm_model             VARCHAR,
        aggregate_score       FLOAT,
        performance_band      VARCHAR,
        evaluator_confidence  VARCHAR,
        agent_name            VARCHAR,
        ticket_summary        VARCHAR,
        frt_status            VARCHAR,
        frt_minutes           FLOAT,
        ttr_status            VARCHAR,
        ttr_minutes           FLOAT,
        flags                 VARCHAR,
        eval_json_path        VARCHAR,
        published_to_zendesk  NUMBER(1) DEFAULT 0,
        published_at          VARCHAR,
        is_latest             NUMBER(1) DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS metric_results (
        id                NUMBER AUTOINCREMENT PRIMARY KEY,
        evaluation_id     NUMBER NOT NULL,
        metric_id         VARCHAR,
        metric_name       VARCHAR,
        rating            VARCHAR,
        rating_label      VARCHAR,
        evidence          VARCHAR,
        reasoning         VARCHAR,
        improvement_note  VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id            NUMBER PRIMARY KEY,
        started_at    VARCHAR,
        completed_at  VARCHAR,
        mode          VARCHAR,
        fetched       NUMBER DEFAULT 0,
        evaluated     NUMBER DEFAULT 0,
        published     NUMBER DEFAULT 0,
        errors        NUMBER DEFAULT 0,
        excluded      NUMBER DEFAULT 0,
        cursor_used   VARCHAR,
        window_from   VARCHAR,
        window_to     VARCHAR,
        error_details VARCHAR,
        host          VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS pipeline_state (
        state_key    VARCHAR PRIMARY KEY,
        state_value  VARCHAR,
        updated_at   VARCHAR
    )""",
    "CREATE SEQUENCE IF NOT EXISTS evaluations_id_seq START = 1 INCREMENT = 1",
    "CREATE SEQUENCE IF NOT EXISTS runs_id_seq START = 1 INCREMENT = 1",
]


def _lower(rows: list[dict]) -> list[dict]:
    """Snowflake returns UPPER-cased keys for unquoted identifiers; callers expect lowercase."""
    return [{k.lower(): v for k, v in r.items()} for r in rows]


class SnowflakeDatabase:
    def __init__(self, config: AppConfig) -> None:
        self._cfg = config.snowflake
        self._conn = None
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connection(self):
        conn = self._conn
        if conn is not None:
            try:
                if not conn.is_closed():
                    return conn
            except Exception:
                pass
        c = self._cfg
        self._conn = snowflake.connector.connect(
            account=c.account, user=c.user, password=c.password,
            warehouse=c.warehouse, database=c.database, schema=c.schema,
            role=c.role or None, client_session_keep_alive=True,
        )
        return self._conn

    def _execute(self, sql: str, params: tuple | list = ()):  # write; returns cursor
        with self._lock:
            cur = self._connection().cursor()
            cur.execute(sql, params)
            return cur

    def _scalar(self, sql: str, params: tuple | list = ()):
        with self._lock:
            cur = self._connection().cursor()
            try:
                cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
            finally:
                cur.close()

    def _query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            cur = self._connection().cursor(DictCursor)
            try:
                cur.execute(sql, params)
                return _lower(cur.fetchall())
            finally:
                cur.close()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connection()
            for stmt in _DDL:
                conn.cursor().execute(stmt)
            # Idempotent migration: feedback-narrative columns on pre-existing tables.
            for stmt in (
                "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS agent_name VARCHAR",
                "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS ticket_summary VARCHAR",
                "ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS metric_name VARCHAR",
                "ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS evidence VARCHAR",
                "ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS reasoning VARCHAR",
                "ALTER TABLE metric_results ADD COLUMN IF NOT EXISTS improvement_note VARCHAR",
            ):
                conn.cursor().execute(stmt)
        logger.debug("Snowflake schema initialised (db=%s schema=%s)", self._cfg.database, self._cfg.schema)

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    def upsert_ticket(self, ticket_id, fetched_at, status, channel, group_id, group_name,
                      agent_name, created_at, closed_at, json_path) -> None:
        # MERGE preserves SQLite behavior: group_id/group_name/created_at are only set on INSERT.
        self._execute(
            """
            MERGE INTO tickets t
            USING (SELECT ? AS ticket_id, ? AS fetched_at, ? AS status, ? AS channel,
                          ? AS group_id, ? AS group_name, ? AS agent_name,
                          ? AS created_at, ? AS closed_at, ? AS json_path) s
            ON t.ticket_id = s.ticket_id
            WHEN MATCHED THEN UPDATE SET
                fetched_at = s.fetched_at, status = s.status, channel = s.channel,
                agent_name = s.agent_name, closed_at = s.closed_at, json_path = s.json_path
            WHEN NOT MATCHED THEN INSERT
                (ticket_id, fetched_at, status, channel, group_id, group_name,
                 agent_name, created_at, closed_at, json_path)
                VALUES (s.ticket_id, s.fetched_at, s.status, s.channel, s.group_id, s.group_name,
                        s.agent_name, s.created_at, s.closed_at, s.json_path)
            """,
            (ticket_id, fetched_at, status, channel, group_id, group_name,
             agent_name, created_at, closed_at, json_path),
        )

    # ------------------------------------------------------------------
    # Evaluations
    # ------------------------------------------------------------------

    def has_evaluation(self, ticket_id: int, prompt_version: str) -> bool:
        n = self._scalar(
            "SELECT COUNT(*) FROM evaluations WHERE ticket_id=? AND prompt_version=? AND is_latest=1",
            (ticket_id, prompt_version),
        )
        return bool(n)

    def mark_old_evaluations(self, ticket_id: int) -> None:
        self._execute("UPDATE evaluations SET is_latest=0 WHERE ticket_id=?", (ticket_id,))

    def insert_evaluation(self, result: EvaluationResult, eval_json_path: str) -> int:
        sla = result.sla_status
        frt = sla.first_response_time if sla else None
        ttr = sla.resolution_time if sla else None
        with self._lock:
            eval_id = int(self._scalar("SELECT evaluations_id_seq.NEXTVAL"))
            self._execute(
                """
                INSERT INTO evaluations (
                    id, ticket_id, evaluated_at, prompt_version, llm_provider, llm_model,
                    aggregate_score, performance_band, evaluator_confidence, agent_name, ticket_summary,
                    frt_status, frt_minutes, ttr_status, ttr_minutes,
                    flags, eval_json_path, is_latest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    eval_id, int(result.ticket_id), result.evaluation_date,
                    result.prompt_version or "", result.llm_provider or "", result.llm_model or "",
                    result.aggregate_score.numeric if result.aggregate_score else None,
                    result.aggregate_score.band if result.aggregate_score else None,
                    result.evaluator_confidence, result.agent_name or "", result.ticket_summary or "",
                    frt.status if frt else None, frt.value_minutes if frt else None,
                    ttr.status if ttr else None, ttr.value_minutes if ttr else None,
                    json.dumps(result.flags), eval_json_path,
                ),
            )
            rows = [
                (eval_id, m.metric_id, m.metric_name, str(m.rating), m.rating_label,
                 m.evidence, m.reasoning, m.improvement_note)
                for m in result.metrics
            ]
            if rows:
                cur = self._connection().cursor()
                cur.executemany(
                    """
                    INSERT INTO metric_results (
                        evaluation_id, metric_id, metric_name, rating, rating_label,
                        evidence, reasoning, improvement_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return eval_id

    def mark_published(self, eval_id: int, published_at: str) -> None:
        self._execute(
            "UPDATE evaluations SET published_to_zendesk=1, published_at=? WHERE id=?",
            (published_at, eval_id),
        )

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def get_ticket_eval_rows(self, ticket_id: int) -> list[dict]:
        return self._query(
            "SELECT id, published_to_zendesk, eval_json_path FROM evaluations WHERE ticket_id=?",
            (ticket_id,),
        )

    def delete_ticket_data(self, ticket_id: int) -> dict:
        with self._lock:
            c1 = self._execute(
                "DELETE FROM metric_results WHERE evaluation_id IN "
                "(SELECT id FROM evaluations WHERE ticket_id=?)", (ticket_id,))
            metric_rows = c1.rowcount or 0
            c2 = self._execute("DELETE FROM evaluations WHERE ticket_id=?", (ticket_id,))
            eval_rows = c2.rowcount or 0
            c3 = self._execute("DELETE FROM tickets WHERE ticket_id=?", (ticket_id,))
            ticket_rows = c3.rowcount or 0
        return {"metric_results": metric_rows, "evaluations": eval_rows, "tickets": ticket_rows}

    # ------------------------------------------------------------------
    # Queries / reporting
    # ------------------------------------------------------------------

    def get_unpublished_evaluations(self, from_date=None, to_date=None) -> list[dict]:
        query = ("SELECT e.* FROM evaluations e JOIN tickets t ON t.ticket_id = e.ticket_id "
                 "WHERE e.published_to_zendesk=0 AND e.is_latest=1")
        params: list = []
        if from_date:
            query += " AND t.closed_at >= ?"; params.append(from_date)
        if to_date:
            query += " AND t.closed_at <= ?"; params.append(to_date + "T23:59:59Z")
        return self._query(query, params)

    def get_summary_stats(self) -> dict:
        tickets_total = self._scalar("SELECT COUNT(*) FROM tickets") or 0
        evals_total = self._scalar("SELECT COUNT(*) FROM evaluations WHERE is_latest=1") or 0
        unpublished = self._scalar(
            "SELECT COUNT(*) FROM evaluations WHERE published_to_zendesk=0 AND is_latest=1") or 0
        recent_runs = self._query("SELECT * FROM runs ORDER BY started_at DESC LIMIT 5")
        return {
            "tickets_total": tickets_total,
            "evals_total": evals_total,
            "unpublished": unpublished,
            "recent_runs": recent_runs,
        }

    def get_audit_data(self, from_date=None, to_date=None) -> list[dict]:
        query = """
            SELECT t.ticket_id, t.closed_at, t.channel, t.agent_name,
                   CASE WHEN e.id IS NULL THEN 0 ELSE 1 END AS evaluated,
                   CASE WHEN e.published_to_zendesk=1 THEN 1 ELSE 0 END AS published,
                   e.aggregate_score, e.performance_band, e.prompt_version
            FROM tickets t
            LEFT JOIN evaluations e ON e.ticket_id = t.ticket_id AND e.is_latest=1
            WHERE 1=1
        """
        params: list = []
        if from_date:
            query += " AND t.closed_at >= ?"; params.append(from_date)
        if to_date:
            query += " AND t.closed_at <= ?"; params.append(to_date + "T23:59:59Z")
        query += " ORDER BY t.closed_at DESC"
        return self._query(query, params)

    def get_feedback_rows(self, agent_name=None, month=None, group_id=None, ticket_id=None) -> list[dict]:
        """Flat (ticket × metric) rows for the latest evaluations — see SQLite Database.get_feedback_rows."""
        query = """
            SELECT
                t.ticket_id, t.closed_at, t.group_id, t.group_name,
                COALESCE(e.agent_name, t.agent_name) AS agent_name, t.channel,
                e.id AS evaluation_id, e.aggregate_score, e.performance_band,
                e.evaluator_confidence, e.flags, e.ticket_summary, e.evaluated_at,
                m.metric_id, m.metric_name, m.rating, m.rating_label,
                m.evidence, m.reasoning, m.improvement_note
            FROM evaluations e
            JOIN tickets t ON t.ticket_id = e.ticket_id
            LEFT JOIN metric_results m ON m.evaluation_id = e.id
            WHERE e.is_latest = 1
        """
        params: list = []
        if agent_name:
            query += " AND COALESCE(e.agent_name, t.agent_name) = ?"; params.append(agent_name)
        if month:
            query += " AND SUBSTR(t.closed_at, 1, 7) = ?"; params.append(month)
        if group_id is not None:
            query += " AND t.group_id = ?"; params.append(group_id)
        if ticket_id is not None:
            query += " AND t.ticket_id = ?"; params.append(ticket_id)
        query += " ORDER BY t.closed_at, t.ticket_id, m.metric_id"
        return self._query(query, params)

    def list_agents(self, month=None) -> list[dict]:
        """Distinct agent names (with ticket counts) among latest evaluations."""
        query = """
            SELECT COALESCE(e.agent_name, t.agent_name) AS agent_name, t.group_id, COUNT(*) AS n
            FROM evaluations e
            JOIN tickets t ON t.ticket_id = e.ticket_id
            WHERE e.is_latest = 1 AND COALESCE(e.agent_name, t.agent_name) IS NOT NULL
                  AND COALESCE(e.agent_name, t.agent_name) != ''
        """
        params: list = []
        if month:
            query += " AND SUBSTR(t.closed_at, 1, 7) = ?"; params.append(month)
        query += " GROUP BY COALESCE(e.agent_name, t.agent_name), t.group_id ORDER BY agent_name"
        return self._query(query, params)

    def get_evaluations_in_range(self, from_date=None, to_date=None) -> list[dict]:
        query = ("SELECT e.eval_json_path FROM evaluations e JOIN tickets t ON t.ticket_id = e.ticket_id "
                 "WHERE e.is_latest=1")
        params: list = []
        if from_date:
            query += " AND t.closed_at >= ?"; params.append(from_date)
        if to_date:
            query += " AND t.closed_at <= ?"; params.append(to_date + "T23:59:59Z")
        query += " ORDER BY t.closed_at DESC"
        return self._query(query, params)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(self, started_at: str, mode: str, cursor_used: Optional[str] = None) -> int:
        with self._lock:
            run_id = int(self._scalar("SELECT runs_id_seq.NEXTVAL"))
            self._execute(
                "INSERT INTO runs (id, started_at, mode, cursor_used) VALUES (?, ?, ?, ?)",
                (run_id, started_at, mode, cursor_used),
            )
        return run_id

    def complete_run(self, run_id: int, completed_at: str, fetched: int, evaluated: int,
                     published: int, errors: int, excluded: int = 0,
                     window_from: Optional[str] = None, window_to: Optional[str] = None,
                     error_details: Optional[str] = None, host: Optional[str] = None) -> None:
        self._execute(
            """
            UPDATE runs SET completed_at=?, fetched=?, evaluated=?, published=?, errors=?,
                            excluded=?, window_from=?, window_to=?, error_details=?, host=?
            WHERE id=?
            """,
            (completed_at, fetched, evaluated, published, errors, excluded,
             window_from, window_to, error_details, host, run_id),
        )

    # ------------------------------------------------------------------
    # Pipeline state (Zendesk cursor + run metadata) — replaces state.json
    # ------------------------------------------------------------------

    def get_state(self, key: str) -> Optional[str]:
        rows = self._query("SELECT state_value FROM pipeline_state WHERE state_key=?", (key,))
        return rows[0]["state_value"] if rows else None

    def set_state(self, key: str, value: Optional[str]) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            """
            MERGE INTO pipeline_state p
            USING (SELECT ? AS state_key, ? AS state_value, ? AS updated_at) s
            ON p.state_key = s.state_key
            WHEN MATCHED THEN UPDATE SET state_value = s.state_value, updated_at = s.updated_at
            WHEN NOT MATCHED THEN INSERT (state_key, state_value, updated_at)
                VALUES (s.state_key, s.state_value, s.updated_at)
            """,
            (key, value, now),
        )
