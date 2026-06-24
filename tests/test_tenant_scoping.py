"""Static guard: every tenant-scoped Supabase query filters by user_id.

The daemon authenticates with the service_role key, which bypasses Postgres
row-level security. So the per-tenant RLS policies in
migrations/supabase/0001_init.sql do NOT run on the live path — tenant isolation
is enforced in application code by the `.eq("user_id", ...)` filter (for reads,
updates, deletes) and the `user_id` column in insert payloads. This test makes
that invariant explicit and regression-proof: it scans supabase_ledger.py and
fails if any query against a tenant-owned table reaches `.execute()` without a
`user_id` reference in the same call chain. A future query that forgets its
user_id scoping is a cross-tenant read/write with no database backstop, so it
should fail CI rather than ship.
"""
import re
from pathlib import Path

# Tables that carry a user_id column and must always be tenant-scoped.
TENANT_TABLES = {"roles", "status_history", "fit_verdicts", "sources", "oos_review"}

_SRC = (
    Path(__file__).resolve().parent.parent
    / "src" / "alice" / "persistence" / "supabase_ledger.py"
).read_text()


def _query_spans(source: str):
    """Yield (table, snippet) for each `.table("<tenant>")` ... `.execute()` chain."""
    for m in re.finditer(r'\.table\(\s*["\'](\w+)["\']\s*\)', source):
        table = m.group(1)
        if table not in TENANT_TABLES:
            continue
        tail = source[m.start():]
        end = tail.find(".execute(")
        # Chain to its terminator; fall back to a generous window if not found.
        snippet = tail[: end + len(".execute(")] if end != -1 else tail[:600]
        yield table, snippet


def test_every_tenant_query_is_user_scoped():
    def _scoped(snip):
        # a real tenant filter, not just the substring appearing in a select list
        return ('eq("user_id"' in snip or "eq('user_id'" in snip            # read/update/delete filter
                or '"user_id":' in snip or "'user_id':" in snip              # insert payload key
                or 'on_conflict="user_id' in snip or "on_conflict='user_id" in snip)  # upsert keyed on user_id
    unscoped = [
        (table, snippet.splitlines()[0])
        for table, snippet in _query_spans(_SRC)
        if not _scoped(snippet)
    ]
    assert not unscoped, (
        "tenant-table queries missing user_id scoping (cross-tenant risk; the "
        "service_role key bypasses RLS, so this is the only backstop):\n  "
        + "\n  ".join(f"{t}: {line.strip()}" for t, line in unscoped)
    )


def test_guard_actually_inspects_queries():
    # Sanity: the scan must find the tenant queries it is meant to guard, or the
    # regex has drifted and the guard above is silently vacuous.
    tables_seen = {t for t, _ in _query_spans(_SRC)}
    assert "roles" in tables_seen, "guard found no roles queries — regex drifted?"
