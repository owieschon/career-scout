"""Analytical reports over the pipeline ledger.

Reporting is aggregation, so it belongs in SQL, not in Python row loops. The
queries live as ``.sql`` files in ``sql/`` next to this module (so they read as
SQL, not as strings buried in Python); each function loads one, runs it through
a DB-API cursor, and returns a list of dict rows. The queries target the ledger
tables defined in ``migrations/supabase/0001_init.sql`` (roles, fit_verdicts,
status_history) and are exercised against SQLite in ``tests/test_reporting.py``;
the same logic ships as Postgres views in ``migrations/supabase/0002_reporting_views.sql``
for the Supabase backend, queried there through PostgREST.

Style: CTEs over nested subqueries, conditional aggregation with FILTER instead
of Python counters, and a window function for transition analysis. The why lives
in each ``.sql`` file as a header comment.
"""
from __future__ import annotations

from pathlib import Path

_SQL_DIR = Path(__file__).resolve().parent / "sql"


def _query(name: str) -> str:
    return (_SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def _rows(cur, sql: str, params: dict) -> list[dict]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def pipeline_funnel(cur, user_id: str) -> list[dict]:
    """Role counts by status, ordered along the funnel."""
    return _rows(cur, _query("pipeline_funnel"), {"uid": user_id})


def company_suppression(cur, user_id: str) -> list[dict]:
    """Per-company not-fit / good-fit tallies driving company suppression."""
    return _rows(cur, _query("company_suppression"), {"uid": user_id})


def judge_verdict_distribution(cur, user_id: str) -> list[dict]:
    """Per-model verdict mix and self-consistency (agreement) rate."""
    return _rows(cur, _query("judge_verdict_distribution"), {"uid": user_id})


def status_transitions(cur, user_id: str) -> list[dict]:
    """Observed (from_status -> to_status) transition frequencies."""
    return _rows(cur, _query("status_transitions"), {"uid": user_id})
