"""Hermetic integration tests for the ledger ROUTER + dual-write path.

`test_supabase_ledger.py` already covers the supabase ADAPTER directly (calling
`supabase_ledger.update_status(...)` etc.) and a few dual-write happy paths. This
file fills the remaining router-level gaps that the rest of the suite never
exercised:

  * dual fan-out drift when the SUPABASE side fails (the mirror of the existing
    sheet-fails test);
  * dual insert_new drift journaling when one backend fails;
  * dual update_intent fanning out to BOTH backends;
  * dual load_statuses preferring Supabase and falling back to the Sheet (a
    different code path from the get_all_records read-fallback already tested);
  * the write-authorization gate enforced through the ROUTER on the SUPABASE
    backend (the existing supabase gate test calls the adapter directly, not via
    ledger.update_status) — proving the safety invariant survives dispatch;
  * supabase insert idempotency through the ROUTER (on_conflict upsert).

All tests are hermetic: they reuse the in-memory `_FakeClient`, the
`_FakeSheetWS` gspread stub, and the `_setup_dual` wiring helper that the
adapter tests already define, and they monkeypatch `LEDGER_BACKEND` via the same
mechanism. No engine logic is changed.
"""
from __future__ import annotations

import importlib
import json as _json

import pytest

# Reuse the fakes + fixtures the adapter tests already define so there is exactly
# one in-memory backend implementation to maintain.
from tests.test_supabase_ledger import (  # noqa: E402
    _FakeClient,
    _FakeSheetWS,
    _setup_dual,
    _seed_role,
    sb,  # pytest fixture
)


# ─── supabase backend, exercised THROUGH the router ──────────────────────────────

def test_router_supabase_gate_blocks_unauthorized_terminal(monkeypatch, sb):
    """The terminal-gated authorization fence must hold on the SUPABASE path when
    reached via the public router, not only when the adapter is called directly.
    This proves the safety invariant is not lost in dispatch."""
    mod, fake = sb
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    from alice.persistence import ledger
    from alice.persistence import supabase_ledger
    importlib.reload(ledger)
    supabase_ledger.set_client_for_tests(fake)
    _seed_role(fake, job_key="gh|gate", status="new")

    ws = ledger._ws()
    ws.get_all_records()
    with pytest.raises(ledger.UnauthorizedStatusWrite):
        ledger.update_status(ws, 2, "submitted", source="agent")  # gated, no auth

    # Nothing changed: role stays 'new', no status_history row written.
    role = fake.table("roles").rows[0]
    assert role["status"] == "new"
    assert fake.table("status_history").rows == []


def test_router_supabase_gate_allows_authorized_terminal(monkeypatch, sb):
    """Counterpart to the block test: with authorized=True the same terminal
    status lands through the router and is recorded as authorized."""
    mod, fake = sb
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    from alice.persistence import ledger
    from alice.persistence import supabase_ledger
    importlib.reload(ledger)
    supabase_ledger.set_client_for_tests(fake)
    _seed_role(fake, job_key="gh|gate", status="new")

    ws = ledger._ws()
    ws.get_all_records()
    ledger.update_status(ws, 2, "submitted", authorized=True, source="operator-cli")

    role = fake.table("roles").rows[0]
    assert role["status"] == "submitted"
    assert fake.table("status_history").rows[-1]["authorized"] is True


def test_router_supabase_insert_is_idempotent(monkeypatch, sb):
    """insert_new through the router on the supabase backend must dedupe by
    (user_id, job_key) via the on_conflict upsert — re-discovery on the daily
    cron must not create a second row."""
    mod, fake = sb
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    from alice.persistence import ledger
    from alice.persistence import supabase_ledger
    importlib.reload(ledger)
    supabase_ledger.set_client_for_tests(fake)

    items = [{"company": "C1", "role": "AE", "source": "gh",
              "url": "https://x.test/1", "job_key": "gh|dup"}]
    n1 = ledger.insert_new(items)
    n2 = ledger.insert_new(items)
    assert n1 == 1
    assert n2 == 0
    assert len(fake.table("roles").rows) == 1


# ─── dual backend: drift when the SUPABASE side fails ────────────────────────────

def test_dual_update_status_supabase_failure_journals_drift(monkeypatch, tmp_path, sb):
    """Mirror of the existing 'sheet fails' drift test, but the SUPABASE side
    raises. The Sheet write must still land and the divergence is journaled with
    supabase_ok=False / sheets_ok=True (fail-open: one backend down never blocks
    the other)."""
    mod, fake = sb
    _seed_role(fake, job_key="jk-A", company="Acme", surfaced_date="2026-05-30", status="new")
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "new", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    # Make the supabase update_status raise; the sheet path must still succeed.
    def _boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(ledger._supabase(), "update_status", _boom)

    ws = ledger._ws()
    ws.get_all_records()
    ledger.update_status(ws, 2, "good fit", authorized=True, source="test")

    # Sheet still got the write.
    assert {r["job_key"]: r["status"] for r in sheet.records} == {"jk-A": "good fit"}
    # Supabase did NOT (the boom replaced the only mutating call).
    assert {r["job_key"]: r["status"] for r in fake.table("roles").rows} == {"jk-A": "new"}
    # Divergence journaled with the failure on the supabase side.
    assert drift.exists()
    rec = _json.loads(drift.read_text().strip().splitlines()[-1])
    assert rec["op"] == "update_status"
    assert rec["supabase_ok"] is False and rec["sheets_ok"] is True
    assert "supabase down" in rec["supabase_error"]


# ─── dual backend: insert_new drift journaling ───────────────────────────────────

def test_dual_insert_new_journals_drift_when_sheet_fails(monkeypatch, tmp_path, sb):
    """dual insert_new must fan out to both backends; if the Sheet insert raises,
    the supabase row still lands and the divergence is journaled. The returned
    count is the supabase count (used only for a 'how many new' message)."""
    mod, fake = sb
    sheet = _FakeSheetWS(mod.HEADERS)
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    def _boom(*a, **k):
        raise RuntimeError("sheet insert down")
    monkeypatch.setattr(sheet, "insert_rows", _boom)

    n = ledger.insert_new([{"company": "New Co", "role": "AE", "job_key": "jk-new",
                            "url": "https://example.test/jk-new", "score": "90"}])

    # Supabase still inserted; count reflects the supabase side.
    assert n == 1
    assert any(r["job_key"] == "jk-new" for r in fake.table("roles").rows)
    # Sheet got nothing.
    assert sheet.records == []
    # Divergence journaled with the sheet side failed.
    assert drift.exists()
    rec = _json.loads(drift.read_text().strip().splitlines()[-1])
    assert rec["op"] == "insert_new"
    assert rec["supabase_ok"] is True and rec["sheets_ok"] is False
    assert "sheet insert down" in rec["sheets_error"]


# ─── dual backend: update_intent fans out to both ────────────────────────────────

def test_dual_update_intent_writes_to_both_backends(monkeypatch, tmp_path, sb):
    """update_intent in dual mode must reach BOTH backends (resolved by job_key),
    with no drift on success. Intent is not gated, so this is the clean fan-out
    case for a non-status write."""
    mod, fake = sb
    _seed_role(fake, job_key="jk-A", company="Acme", surfaced_date="2026-05-30", status="new")
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "new", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30",
                 "intent": ""}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    ws = ledger._ws()
    ws.get_all_records()
    ledger.update_intent(ws, 2, "active", source="test")

    assert fake.table("roles").rows[0]["intent"] == "active"
    assert sheet.records[0]["intent"] == "active"
    assert not drift.exists()


# ─── dual backend: load_statuses prefers Supabase, falls back to Sheet ────────────

def test_dual_load_statuses_prefers_supabase(monkeypatch, tmp_path, sb):
    """dual load_statuses reads from Supabase when it is reachable; the Sheet is
    not consulted and no drift is recorded."""
    mod, fake = sb
    _seed_role(fake, job_key="jk-A", company="Acme", status="good fit")
    sheet = _FakeSheetWS(mod.HEADERS)
    # Seed the Sheet with a CONFLICTING value so a wrong-source read is detectable.
    sheet.seed([{"company": "Acme", "role": "AE", "status": "not a fit", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    statuses, _notfit, goodfit, total = ledger.load_statuses()
    assert total == 1
    assert statuses["jk-A"] == "good fit"          # supabase value, not the sheet's
    assert goodfit["acme"] == 1
    assert not drift.exists()


def test_dual_load_statuses_falls_back_to_sheet(monkeypatch, tmp_path, sb):
    """When Supabase is unreachable, dual load_statuses falls back to the Sheet
    and journals the read drift. This is a distinct code path from the
    DualWorksheet.get_all_records read-fallback already tested."""
    mod, fake = sb
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "good fit", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    # Force the supabase accessor used by _dual_load_statuses to raise.
    def _supa_boom():
        raise RuntimeError("supabase unreachable")
    monkeypatch.setattr(ledger, "_supabase", _supa_boom)

    statuses, _notfit, goodfit, total = ledger.load_statuses()
    assert total == 1
    assert statuses["jk-A"] == "good fit"          # served by the Sheet
    assert goodfit["acme"] == 1
    # Read fallback journaled.
    assert drift.exists()
    rec = _json.loads(drift.read_text().strip().splitlines()[-1])
    assert rec["op"] == "read"
    assert rec["detail"]["phase"] == "load_statuses"
    assert rec["supabase_ok"] is False and rec["sheets_ok"] is True
