"""Apply migrations/supabase/0001_init.sql against the configured Supabase project.

Idempotent: safe to re-run. The migration uses CREATE TABLE IF NOT EXISTS and
ON CONFLICT DO NOTHING throughout, plus a `schema_versions` marker so a second
run is a no-op.

Usage:
    python3 scripts/apply_supabase_schema.py --dry-run   # print, don't execute
    python3 scripts/apply_supabase_schema.py             # apply

Config (~/.config/job-search/config.env, mode 600):
    SUPABASE_DB_URL=postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres

The DB URL is the *direct* Postgres connection string (transaction-pooler or
session-pooler). It's only used by this script and the migration script; the
runtime daemon uses the REST/PostgREST surface via supabase-py and never sees
the password.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from alice import repo_paths

REPO_ROOT = repo_paths.ROOT
MIGRATION_PATH = REPO_ROOT / "migrations" / "supabase" / "0001_init.sql"


def _load_cfg():
    from alice.jobcfg import load
    return load()


def _connect(db_url: str):
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: python3 -m pip install psycopg2-binary",
              file=sys.stderr)
        sys.exit(2)
    return psycopg2.connect(db_url)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print the SQL that would be applied; do not execute.")
    args = p.parse_args()

    if not MIGRATION_PATH.exists():
        print(f"ERROR: migration not found at {MIGRATION_PATH}", file=sys.stderr)
        sys.exit(2)

    sql = MIGRATION_PATH.read_text()

    if args.dry_run:
        print(f"-- {MIGRATION_PATH}")
        print(sql)
        print(f"\n-- ({sum(1 for _ in sql.splitlines())} lines, would be applied)")
        return

    cfg = _load_cfg()
    db_url = cfg.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL not set in ~/.config/job-search/config.env. "
              "Add it (with mode 600) before applying.", file=sys.stderr)
        sys.exit(2)

    conn = _connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("select version, applied_at, note from schema_versions order by version")
                versions = cur.fetchall()
        print("Applied OK. schema_versions:")
        for v in versions:
            print(f"  v{v[0]}  applied_at={v[1]}  {v[2]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
