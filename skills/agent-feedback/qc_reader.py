#!/usr/bin/env python3
"""Self-contained QC data reader for the agent-feedback skill.

**Standalone** — no dependency on the ticket-evaluator app repo, so the skill
folder can be dropped into ``~/.claude/skills/agent-feedback/`` and used from any
Claude session without cloning anything.

Backends, chosen automatically:
- **Snowflake** (read-only) when ``SNOWFLAKE_ACCOUNT`` + reader creds are in the
  environment (from a ``.env`` next to this file, the CWD, or real env vars).
  Prefers ``SNOWFLAKE_READER_USER`` / ``SNOWFLAKE_READER_PASSWORD``; falls back to
  ``SNOWFLAKE_USER`` / ``SNOWFLAKE_PASSWORD``.
- **SQLite** for local dev, when no Snowflake creds are set and a
  ``data/evaluations.db`` (or ``QC_SQLITE``) file exists.

Exposes the small read surface the skill needs — ``get_feedback_rows``,
``list_agents``, ``get_summary_stats`` — with the same shapes as the app's DB
layer (SQLite rows / lowercased Snowflake dicts; the fetch script handles both).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


def _load_env() -> None:
    """Load .env from this skill folder and the CWD (real env vars win)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path(__file__).resolve().parent
    for p in (here / ".env", Path.cwd() / ".env"):
        if p.exists():
            load_dotenv(p, override=False)


def _default_sqlite() -> Optional[str]:
    """Look for the app's local dev DB relative to this file (repo checkout)."""
    for cand in (
        Path(__file__).resolve().parent.parent.parent / "data" / "evaluations.db",
        Path.cwd() / "data" / "evaluations.db",
    ):
        if cand.exists():
            return str(cand)
    return None


def _mask(s: str) -> str:
    s = s or ""
    return s if len(s) <= 4 else f"{s[:3]}…{s[-2:]}"


# ---------------------------------------------------------------------------
# SQL (identical logic across both backends; only the SUBSTR/params dialect differs)
# ---------------------------------------------------------------------------

_FEEDBACK_SQL = """
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

_AGENTS_SQL = """
    SELECT COALESCE(e.agent_name, t.agent_name) AS agent_name, t.group_id, COUNT(*) AS n
    FROM evaluations e
    JOIN tickets t ON t.ticket_id = e.ticket_id
    WHERE e.is_latest = 1
      AND COALESCE(e.agent_name, t.agent_name) IS NOT NULL
      AND COALESCE(e.agent_name, t.agent_name) != ''
"""


def _feedback_filters(agent_name, month, group_id, ticket_id, substr):
    """Build the shared WHERE additions + params for get_feedback_rows."""
    q, params = "", []
    if agent_name:
        q += " AND COALESCE(e.agent_name, t.agent_name) = ?"; params.append(agent_name)
    if month:
        q += f" AND {substr}(t.closed_at, 1, 7) = ?"; params.append(month)
    if group_id is not None:
        q += " AND t.group_id = ?"; params.append(group_id)
    if ticket_id is not None:
        q += " AND t.ticket_id = ?"; params.append(ticket_id)
    q += " ORDER BY t.closed_at, t.ticket_id, m.metric_id"
    return q, params


# ---------------------------------------------------------------------------
# SQLite (local dev)
# ---------------------------------------------------------------------------

class SqliteReader:
    backend = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path
        self.info = {"backend": "sqlite", "path": path}

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def describe(self) -> str:
        return f"backend: sqlite ({self.path})"

    def get_feedback_rows(self, agent_name=None, month=None, group_id=None, ticket_id=None):
        extra, params = _feedback_filters(agent_name, month, group_id, ticket_id, "substr")
        conn = self._conn()
        try:
            return conn.execute(_FEEDBACK_SQL + extra, params).fetchall()
        finally:
            conn.close()

    def list_agents(self, month=None):
        q, params = _AGENTS_SQL, []
        if month:
            q += " AND substr(t.closed_at, 1, 7) = ?"; params.append(month)
        q += " GROUP BY COALESCE(e.agent_name, t.agent_name), t.group_id ORDER BY 1"
        conn = self._conn()
        try:
            return conn.execute(q, params).fetchall()
        finally:
            conn.close()

    def get_summary_stats(self) -> dict:
        conn = self._conn()
        try:
            g = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "tickets_total": g("SELECT COUNT(*) FROM tickets"),
                "evals_total": g("SELECT COUNT(*) FROM evaluations WHERE is_latest=1"),
                "unpublished": g("SELECT COUNT(*) FROM evaluations WHERE published_to_zendesk=0 AND is_latest=1"),
            }
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Snowflake (read-only)
# ---------------------------------------------------------------------------

class SnowflakeReader:
    backend = "snowflake"

    def __init__(self, conf: dict) -> None:
        import snowflake.connector  # imported lazily so SQLite-only dev needs no connector
        from snowflake.connector import DictCursor

        snowflake.connector.paramstyle = "qmark"
        self._DictCursor = DictCursor
        self._conf = conf
        self.info = {"backend": "snowflake", "account": conf["account"], "user": conf["user"],
                     "database": conf["database"], "schema": conf["schema"], "role": conf.get("role")}
        self._conn = snowflake.connector.connect(
            account=conf["account"], user=conf["user"], password=conf["password"],
            warehouse=conf.get("warehouse") or None, database=conf.get("database") or None,
            schema=conf.get("schema") or None, role=conf.get("role") or None,
            client_session_keep_alive=True,
        )

    def describe(self) -> str:
        c = self._conf
        return (f"backend: snowflake (account={_mask(c['account'])} user={_mask(c['user'])} "
                f"db={c.get('database')} schema={c.get('schema')} role={c.get('role')})")

    def _query(self, sql: str, params) -> list:
        cur = self._conn.cursor(self._DictCursor)
        try:
            cur.execute(sql, params)
            return [{k.lower(): v for k, v in r.items()} for r in cur.fetchall()]
        finally:
            cur.close()

    def _scalar(self, sql: str):
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()

    def get_feedback_rows(self, agent_name=None, month=None, group_id=None, ticket_id=None):
        extra, params = _feedback_filters(agent_name, month, group_id, ticket_id, "SUBSTR")
        return self._query(_FEEDBACK_SQL + extra, params)

    def list_agents(self, month=None):
        q, params = _AGENTS_SQL, []
        if month:
            q += " AND SUBSTR(t.closed_at, 1, 7) = ?"; params.append(month)
        q += " GROUP BY COALESCE(e.agent_name, t.agent_name), t.group_id ORDER BY 1"
        return self._query(q, params)

    def get_summary_stats(self) -> dict:
        return {
            "tickets_total": self._scalar("SELECT COUNT(*) FROM tickets") or 0,
            "evals_total": self._scalar("SELECT COUNT(*) FROM evaluations WHERE is_latest=1") or 0,
            "unpublished": self._scalar(
                "SELECT COUNT(*) FROM evaluations WHERE published_to_zendesk=0 AND is_latest=1") or 0,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_reader(sqlite_override: Optional[str] = None):
    """Pick a backend: explicit --sqlite wins (dev), else Snowflake if creds set, else local SQLite."""
    _load_env()

    # Explicit dev override always wins, even if Snowflake creds are present in the env.
    if sqlite_override:
        if Path(sqlite_override).exists():
            return SqliteReader(sqlite_override)
        raise SystemExit(f"--sqlite path not found: {sqlite_override}")

    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_READER_USER") or os.environ.get("SNOWFLAKE_USER")
    password = os.environ.get("SNOWFLAKE_READER_PASSWORD") or os.environ.get("SNOWFLAKE_PASSWORD")

    if account and user and password:
        return SnowflakeReader({
            "account": account, "user": user, "password": password,
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", ""),
            "database": os.environ.get("SNOWFLAKE_DATABASE", ""),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA", ""),
            "role": os.environ.get("SNOWFLAKE_READER_ROLE") or "TICKET_EVALUATOR_READER",
        })

    path = os.environ.get("QC_SQLITE") or _default_sqlite()
    if path and Path(path).exists():
        return SqliteReader(path)

    raise SystemExit(
        "No data source found.\n"
        "  • For Snowflake: set SNOWFLAKE_ACCOUNT, SNOWFLAKE_READER_USER, SNOWFLAKE_READER_PASSWORD "
        "(+ WAREHOUSE/DATABASE/SCHEMA) in a .env next to this script or in your environment.\n"
        "  • For local dev: point QC_SQLITE at a data/evaluations.db, or run from the repo.\n"
        "See SETUP.md."
    )
