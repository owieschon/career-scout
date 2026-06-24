"""Tests for the SQL reporting layer (scripts/reporting.py).

Runs the analytical queries against an in-memory SQLite built to mirror the
ledger tables in migrations/supabase/0001_init.sql, with synthetic rows for two
users so the per-user scoping is exercised too. No network, no real database.
"""
import sqlite3

import pytest

from alice.persistence import reporting


@pytest.fixture
def cur():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE roles (
            id INTEGER PRIMARY KEY, user_id TEXT, company TEXT, status TEXT
        );
        CREATE TABLE fit_verdicts (
            id INTEGER PRIMARY KEY, user_id TEXT, judge_model TEXT,
            verdict TEXT, consistent INTEGER
        );
        CREATE TABLE status_history (
            id INTEGER PRIMARY KEY, role_id INTEGER, user_id TEXT,
            status TEXT, changed_at TEXT
        );
        """
    )
    roles = [
        # u1: Acme rejected twice, Beta a good fit, two still 'new'
        (1, "u1", "Acme", "not a fit"), (2, "u1", "Acme", "not a fit"),
        (3, "u1", "Beta", "good fit"), (4, "u1", "Gamma", "new"),
        (5, "u1", "Delta", "new"),
        # u2: scoping noise that must never appear in u1's reports
        (6, "u2", "Acme", "not a fit"), (7, "u2", "Zeta", "submitted"),
    ]
    conn.executemany("INSERT INTO roles VALUES (?,?,?,?)", roles)
    verdicts = [
        (1, "u1", "gemini-2.5-flash", "FIT", 1),
        (2, "u1", "gemini-2.5-flash", "FIT", 1),
        (3, "u1", "gemini-2.5-flash", "NOT-FIT", 0),  # the one disagreement
        (4, "u2", "gemini-2.5-flash", "REACH", 1),
    ]
    conn.executemany("INSERT INTO fit_verdicts VALUES (?,?,?,?,?)", verdicts)
    history = [
        (1, 1, "u1", "new", "2026-06-01"),
        (2, 1, "u1", "good fit", "2026-06-02"),
        (3, 1, "u1", "submitted", "2026-06-05"),
        (4, 3, "u1", "new", "2026-06-01"),
        (5, 3, "u1", "good fit", "2026-06-03"),
        (6, 7, "u2", "new", "2026-06-01"),       # scoping noise
        (7, 7, "u2", "submitted", "2026-06-02"),
    ]
    conn.executemany("INSERT INTO status_history VALUES (?,?,?,?,?)", history)
    conn.commit()
    return conn.cursor()


def test_pipeline_funnel_counts_and_order(cur):
    rows = reporting.pipeline_funnel(cur, "u1")
    # only u1's five roles, ordered along the funnel (new -> good fit -> not a fit)
    assert [(r["status"], r["n"]) for r in rows] == [
        ("new", 2), ("good fit", 1), ("not a fit", 2)
    ]


def test_company_suppression_conditional_aggregation(cur):
    rows = {r["company"]: r for r in reporting.company_suppression(cur, "u1")}
    assert rows["Acme"]["not_fit"] == 2 and rows["Acme"]["good_fit"] == 0
    assert rows["Beta"]["good_fit"] == 1 and rows["Beta"]["not_fit"] == 0
    # Gamma/Delta are only 'new' — no suppression signal, excluded by HAVING
    assert "Gamma" not in rows and "Delta" not in rows


def test_judge_distribution_and_agreement(cur):
    rows = reporting.judge_verdict_distribution(cur, "u1")
    assert len(rows) == 1
    r = rows[0]
    assert r["verdicts"] == 3 and r["fit"] == 2 and r["not_fit"] == 1
    assert r["agreement_pct"] == pytest.approx(66.7, abs=0.1)  # 2 of 3 consistent


def test_status_transitions_via_window(cur):
    rows = {(r["from_status"], r["to_status"]): r["n"]
            for r in reporting.status_transitions(cur, "u1")}
    # role 1: new->good fit->submitted ; role 3: new->good fit
    assert rows[("new", "good fit")] == 2
    assert rows[("good fit", "submitted")] == 1
    # u2's new->submitted must not leak into u1's report
    assert ("new", "submitted") not in rows


def test_reports_are_user_scoped(cur):
    # u2 has its own, independent rows
    assert {r["status"] for r in reporting.pipeline_funnel(cur, "u2")} == {
        "not a fit", "submitted"
    }


def test_agreement_pct_excludes_null_consistent(cur):
    """A NULL 'consistent' (judge ran once, no second pass) must not count as a
    disagreement: agreement is over rows that actually have a consistency check."""
    cur.executemany("INSERT INTO fit_verdicts VALUES (?,?,?,?,?)",
                    [(10, "u3", "m", "FIT", 1), (11, "u3", "m", "FIT", None)])
    rows = reporting.judge_verdict_distribution(cur, "u3")
    assert rows[0]["agreement_pct"] == 100.0  # 1 consistent of 1 non-null; NULL ignored
