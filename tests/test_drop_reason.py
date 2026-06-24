"""seen_jobs records why a role was gate-dropped (skip_reason), so a gate change
can trigger a surgical per-cohort re-judge instead of a blind backlog re-judge.
Covers both backends and the idempotent migration of an existing db."""
import json
import sqlite3
from pathlib import Path

from alice.pipeline import daily_delta as dd


def test_db_init_creates_skip_reason_column():
    conn = sqlite3.connect(":memory:")
    dd.db_init(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_jobs)").fetchall()}
    assert "skip_reason" in cols


def test_migration_adds_skip_reason_to_old_db():
    """An old pipeline.db without skip_reason gets the column added in place,
    existing rows keep their data with NULL skip_reason (cohort-0)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seen_jobs (source TEXT NOT NULL, external_id TEXT "
                 "NOT NULL, company TEXT, title TEXT, url TEXT, last_seen TEXT, "
                 "PRIMARY KEY (source, external_id))")
    conn.execute("INSERT INTO seen_jobs (source, external_id, company) VALUES "
                 "('ats','old-1','LegacyCo')")
    conn.commit()
    dd.db_init(conn)  # idempotent migration
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_jobs)").fetchall()}
    assert "skip_reason" in cols
    row = conn.execute("SELECT company, skip_reason FROM seen_jobs WHERE "
                       "external_id='old-1'").fetchone()
    assert row == ("LegacyCo", None)  # data preserved, skip_reason NULL = cohort-0


def test_sqlite_set_skip_seen_records_reason():
    conn = sqlite3.connect(":memory:")
    dd.db_init(conn)
    dd.mark_seen(conn, "ats", "j1", "Acme", "Senior AE", "http://x")
    dd.mark_seen(conn, "ats", "j2", "Beta", "RevOps", "http://y")
    dd.set_skip_seen(conn, "ats", "j1", "domain_skip")
    # j1 records the reason; j2 (passed all gates) stays NULL
    assert conn.execute("SELECT skip_reason FROM seen_jobs WHERE external_id='j1'"
                        ).fetchone()[0] == "domain_skip"
    assert conn.execute("SELECT skip_reason FROM seen_jobs WHERE external_id='j2'"
                        ).fetchone()[0] is None


def test_jsonstate_set_skip_records_reason(tmp_path):
    p = tmp_path / "seen.json"
    st = dd.JsonState(str(p))
    st.mark("ats", "j1", "Acme", "Senior AE", "http://x")
    st.set_skip("ats", "j1", "remote_skip")
    assert st.data["ats|j1"]["skip_reason"] == "remote_skip"
    # set_skip on an unseen key is a no-op (no crash, no phantom row)
    st.set_skip("ats", "never-seen", "killed")
    assert "ats|never-seen" not in st.data
