"""Migration idempotency tests.

The data migration must be safe to re-run: the script's `--apply` path can be
invoked twice with the same Sheet and produces the same end state. This test
wires a fake supabase client and a stub sheets worksheet, then calls the
migrator twice.
"""
from __future__ import annotations

import os
import importlib
import pytest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")


# Reuse the fake client from the adapter tests.
from tests.test_supabase_ledger import _FakeClient  # noqa: E402


class _StubSheetWS:
    """Minimal duck-typed gspread worksheet returning the seeded rows.
    The migration script only calls .get_all_records() and .spreadsheet on it;
    we stub both."""

    def __init__(self, rows, oos_rows=None):
        self._rows = rows
        self._oos_rows = oos_rows or []

    def get_all_records(self):
        return list(self._rows)

    class _SS:
        def __init__(self, oos_rows):
            self._oos_rows = oos_rows

        def worksheet(self, name):
            if name == "OOS Review (Alice)":
                return _StubSheetWS(self._oos_rows)
            raise RuntimeError(f"no tab {name!r}")

    @property
    def spreadsheet(self):
        return self._SS(self._oos_rows)


@pytest.fixture
def wired():
    """Wire fake supabase + stub sheet into the migrator."""
    from alice.persistence import supabase_ledger
    importlib.reload(supabase_ledger)
    fake = _FakeClient()
    supabase_ledger.set_client_for_tests(fake)
    os.environ["ALICE_USER_ID"] = "test-operator"
    fake.table("app_users")._insert({"user_id": "test-operator", "handle": "operator"})

    from alice.persistence import migrate_sheet_to_supabase as mig
    importlib.reload(mig)
    return mig, fake


def _sheet_rows():
    return [
        {"surfaced_date": "2026-05-30", "company": "Acme", "role": "AE",
         "comp": "", "source": "gh", "score": 75, "status": "good fit",
         "notes": "", "url": "https://x.test/1", "job_key": "gh|1",
         "rationale": "[AE] good", "status_changed_date": "2026-05-30", "intent": "active"},
        {"surfaced_date": "2026-05-29", "company": "Bcme", "role": "RevOps",
         "comp": "", "source": "ashby", "score": 80, "status": "new",
         "notes": "", "url": "https://x.test/2", "job_key": "ashby|2",
         "rationale": "[RevOps]", "status_changed_date": "", "intent": ""},
        # row with empty job_key+url should be skipped (no stable key)
        {"surfaced_date": "2026-05-28", "company": "Skip", "role": "X",
         "comp": "", "source": "", "score": "", "status": "new",
         "notes": "", "url": "", "job_key": "",
         "rationale": "", "status_changed_date": "", "intent": ""},
    ]


def _oos_rows():
    return [
        {"found_date": "2026-05-30", "company": "OOS-A", "role": "AE",
         "verdict": "FIT", "consistent": "yes", "score": 82,
         "url": "https://oos.test/a", "judge_reason": "fits", "operator_decision": ""},
    ]


def test_migrate_apply_writes_rows(wired):
    mig, fake = wired
    sheet_ws = _StubSheetWS(_sheet_rows(), _oos_rows())
    from alice.persistence import supabase_ledger
    client = supabase_ledger._get_client()
    mig._ensure_user(client, "test-operator")
    n = mig._migrate_roles(sheet_ws, client, "test-operator", apply=True)
    assert n == 2  # the third row is skipped (empty job_key)
    keys = {r["job_key"] for r in fake.table("roles").rows}
    assert keys == {"gh|1", "ashby|2"}

    n_oos = mig._migrate_oos_review(sheet_ws, client, "test-operator", apply=True)
    assert n_oos == 1
    assert fake.table("oos_review").rows[0]["url"] == "https://oos.test/a"


def test_migrate_is_idempotent(wired):
    """The DoD claim: re-running the migration must NOT create duplicates."""
    mig, fake = wired
    sheet_ws = _StubSheetWS(_sheet_rows(), _oos_rows())
    from alice.persistence import supabase_ledger
    client = supabase_ledger._get_client()
    mig._ensure_user(client, "test-operator")

    mig._migrate_roles(sheet_ws, client, "test-operator", apply=True)
    mig._migrate_oos_review(sheet_ws, client, "test-operator", apply=True)
    first = len(fake.table("roles").rows), len(fake.table("oos_review").rows)

    mig._migrate_roles(sheet_ws, client, "test-operator", apply=True)
    mig._migrate_oos_review(sheet_ws, client, "test-operator", apply=True)
    second = len(fake.table("roles").rows), len(fake.table("oos_review").rows)

    assert first == second == (2, 1)


def test_migrate_dry_run_writes_nothing(wired):
    mig, fake = wired
    sheet_ws = _StubSheetWS(_sheet_rows(), _oos_rows())
    from alice.persistence import supabase_ledger
    client = supabase_ledger._get_client()
    mig._ensure_user(client, "test-operator")
    n = mig._migrate_roles(sheet_ws, client, "test-operator", apply=False)
    assert n == 2  # eligible count returned
    assert fake.table("roles").rows == []  # but nothing written


def test_migrate_carries_status_and_intent(wired):
    """The operator's status labels + intent are operationally critical — these are
    the columns the matcher uses to suppress/boost. Migration must preserve them."""
    mig, fake = wired
    sheet_ws = _StubSheetWS(_sheet_rows())
    from alice.persistence import supabase_ledger
    client = supabase_ledger._get_client()
    mig._ensure_user(client, "test-operator")
    mig._migrate_roles(sheet_ws, client, "test-operator", apply=True)
    by_key = {r["job_key"]: r for r in fake.table("roles").rows}
    assert by_key["gh|1"]["status"] == "good fit"
    assert by_key["gh|1"]["intent"] == "active"
    assert by_key["ashby|2"]["status"] == "new"
    assert by_key["ashby|2"]["intent"] == ""
