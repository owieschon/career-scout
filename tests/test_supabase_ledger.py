"""Contract tests for the Supabase backend behind ledger.py.

These tests inject a small in-memory fake supabase client (`_FakeClient`) that
implements the subset of the supabase-py surface our adapter touches:
    client.table(name).select(...).eq(...).order(...).execute() → .data
    client.table(name).insert(...).execute() → .data
    client.table(name).upsert(rows, on_conflict=..., ignore_duplicates=...).execute()
    client.table(name).update(patch).eq(...).eq(...).execute()

The fake is intentionally minimal — it exercises the path the adapter actually
uses without booting a real Postgres. The gold-standard parity (same pipeline
data out as Sheets) is verified separately by scripts/parity_check.py against
the real services.
"""
from __future__ import annotations

import os
import importlib
import pytest

# Make scripts/ importable the same way the suite already does.
SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")


# ─── in-memory fake supabase client ─────────────────────────────────────────────

class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Fluent builder mirroring supabase-py's PostgrestFilterBuilder.

    Operations are deferred until `.execute()`; each is a side-effecting
    closure over the parent `_Table`."""

    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.filters = []
        self.order_specs = []
        self.select_cols = None
        self.on_conflict = None
        self.ignore_duplicates = False
        self.single_mode = False

    def select(self, cols):
        self.select_cols = cols
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.order_specs.append((col, desc))
        return self

    def single(self):
        self.single_mode = True
        return self

    def execute(self):
        if self.op == "select":
            rows = self.table._filtered(self.filters)
            for col, desc in reversed(self.order_specs):
                rows.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self.single_mode:
                return _Resp(rows[0] if rows else None)
            return _Resp(list(rows))
        if self.op == "insert":
            return _Resp([self.table._insert(self.payload)])
        if self.op == "upsert":
            inserted = self.table._upsert(
                self.payload, self.on_conflict, self.ignore_duplicates)
            return _Resp(inserted)
        if self.op == "update":
            updated = self.table._update(self.filters, self.payload)
            return _Resp(updated)
        raise AssertionError(f"unsupported op {self.op}")


class _Table:
    def __init__(self, name):
        self.name = name
        self.rows: list[dict] = []
        self._next_id = 1

    # builder entrypoints
    def select(self, cols):       return _Query(self, "select").select(cols)
    def insert(self, payload):    return _Query(self, "insert", payload)
    def update(self, payload):    return _Query(self, "update", payload)

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        q = _Query(self, "upsert", payload)
        q.on_conflict = on_conflict
        q.ignore_duplicates = ignore_duplicates
        return q

    # state mutators
    def _filtered(self, filters):
        out = []
        for r in self.rows:
            if all(r.get(c) == v for c, v in filters):
                out.append(dict(r))
        return out

    def _insert(self, payload):
        if isinstance(payload, list):
            inserted = [self._insert(p) for p in payload]
            return inserted[-1] if inserted else None
        row = dict(payload)
        row.setdefault("id", self._next_id)
        self._next_id += 1
        self.rows.append(row)
        return dict(row)

    def _upsert(self, payload, on_conflict, ignore_duplicates):
        if not isinstance(payload, list):
            payload = [payload]
        keys = [k.strip() for k in (on_conflict or "").split(",") if k.strip()]
        out = []
        for p in payload:
            existing = None
            if keys:
                for r in self.rows:
                    if all(r.get(k) == p.get(k) for k in keys):
                        existing = r
                        break
            if existing is not None and ignore_duplicates:
                continue
            if existing is not None:
                existing.update(p)
                out.append(dict(existing))
            else:
                row = dict(p)
                row.setdefault("id", self._next_id)
                self._next_id += 1
                self.rows.append(row)
                out.append(dict(row))
        return out

    def _update(self, filters, patch):
        out = []
        for r in self.rows:
            if all(r.get(c) == v for c, v in filters):
                r.update(patch)
                out.append(dict(r))
        return out


class _FakeClient:
    def __init__(self):
        self._tables: dict[str, _Table] = {}

    def table(self, name):
        return self._tables.setdefault(name, _Table(name))


# ─── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def sb():
    """Fresh fake client + supabase_ledger module wired to it."""
    from alice.persistence import supabase_ledger
    importlib.reload(supabase_ledger)
    fake = _FakeClient()
    supabase_ledger.set_client_for_tests(fake)
    # Make sure the adapter sees a user_id even without config.env.
    os.environ["ALICE_USER_ID"] = "test-operator"
    # Pre-seed app_users so FKs are satisfied conceptually (the fake doesn't
    # enforce FKs but we want the test data to look like the real schema).
    fake.table("app_users")._insert({"user_id": "test-operator", "handle": "operator"})
    return supabase_ledger, fake


def _seed_role(fake, *, job_key, company="Acme", role="AE", status="new",
               surfaced_date="2026-05-30", url=None, score="80"):
    fake.table("roles")._insert({
        "user_id": "test-operator",
        "job_key": job_key,
        "surfaced_date": surfaced_date,
        "company": company,
        "role": role,
        "comp": "",
        "source": "test",
        "score": score,
        "status": status,
        "notes": "",
        "url": url or f"https://example.test/{job_key}",
        "rationale": "",
        "status_changed_date": None,
        "intent": "",
    })


# ─── tests ──────────────────────────────────────────────────────────────────────

def test_available_true_with_fake_client(sb):
    mod, _ = sb
    assert mod.available() is True


def test_load_statuses_empty(sb):
    mod, _ = sb
    statuses, notfit, goodfit, total = mod.load_statuses()
    assert statuses == {} and dict(notfit) == {} and dict(goodfit) == {} and total == 0


def test_load_statuses_shapes_match_sheets_contract(sb):
    """The 4-tuple shape and the per-company suppression counters mirror
    ledger._sheets_load_statuses() exactly. Failure here means a consumer
    that calls `ledger.load_statuses()` would break after cutover."""
    mod, fake = sb
    _seed_role(fake, job_key="gh|1", company="Acme", status="not a fit")
    _seed_role(fake, job_key="gh|2", company="Acme", status="not a fit")
    _seed_role(fake, job_key="gh|3", company="Acme", status="not a fit")
    _seed_role(fake, job_key="gh|4", company="Northwind", status="good fit")
    _seed_role(fake, job_key="gh|5", company="Northwind", status="new")
    # NOTE: company stored in original case ("Other (YC)") in the Sheet today.
    # The canonical Sheets regex `\s*\((YC|VC|auto)\)\s*$` is case-SENSITIVE
    # and runs AFTER .lower(), so it actually never strips "(yc)" — a latent
    # quirk that both backends mirror byte-equivalently. We test the real
    # behavior, not the aspirational one.
    _seed_role(fake, job_key="gh|6", company="Other (YC)", status="not a fit")

    statuses, notfit, goodfit, total = mod.load_statuses()
    assert total == 6
    assert statuses["gh|1"] == "not a fit"
    assert statuses["gh|4"] == "good fit"
    assert statuses["gh|5"] == "new"
    # company-wide counters should accumulate
    assert notfit["acme"] == 3
    assert goodfit["northwind"] == 1
    # The "(YC)" suffix lives in the key under current canonical behavior;
    # see comment above. If/when the regex is fixed to be case-insensitive
    # in BOTH backends, this assertion changes to `notfit["other"] == 1`.
    assert notfit["other (yc)"] == 1


def test_insert_new_creates_role(sb):
    mod, fake = sb
    n = mod.insert_new([
        {"company": "C1", "role": "AE", "comp": "120k",
         "source": "gh", "score": 75, "url": "https://x.test/1",
         "job_key": "gh|1", "rationale": "[AE] good fit"},
    ])
    assert n == 1
    rows = fake.table("roles").rows
    assert len(rows) == 1 and rows[0]["job_key"] == "gh|1"
    assert rows[0]["status"] == "new"
    assert rows[0]["user_id"] == "test-operator"


def test_insert_new_is_idempotent_on_job_key(sb):
    """The (user_id, job_key) unique constraint should make re-inserts no-ops.
    This is what protects the daily cron when a role is re-discovered."""
    mod, fake = sb
    items = [{"company": "C1", "role": "AE", "source": "gh",
              "url": "https://x.test/1", "job_key": "gh|1"}]
    n1 = mod.insert_new(items)
    n2 = mod.insert_new(items)
    assert n1 == 1
    assert n2 == 0
    assert len(fake.table("roles").rows) == 1


def test_get_all_records_via_ws_returns_sheet_shaped_rows(sb):
    mod, fake = sb
    _seed_role(fake, job_key="gh|1", company="Acme", surfaced_date="2026-05-29")
    _seed_role(fake, job_key="gh|2", company="Bcme", surfaced_date="2026-05-30")
    ws = mod._ws()
    rows = ws.get_all_records()
    # newest surfaced_date first (Sheet's reverse-chron contract)
    assert rows[0]["company"] == "Bcme"
    assert rows[1]["company"] == "Acme"
    # Sheet-column shape preserved
    assert set(rows[0].keys()) >= {"surfaced_date", "company", "role", "status",
                                    "job_key", "intent", "status_changed_date"}


def test_update_status_writes_role_and_history(sb):
    mod, fake = sb
    _seed_role(fake, job_key="gh|1")
    ws = mod._ws()
    ws.get_all_records()  # populates row_idx -> id
    mod.update_status(ws, 2, "good fit", source="test")
    role = next(r for r in fake.table("roles").rows if r["job_key"] == "gh|1")
    assert role["status"] == "good fit"
    assert role["status_changed_date"]  # set
    hist = fake.table("status_history").rows
    assert len(hist) == 1
    assert hist[0]["status"] == "good fit"
    assert hist[0]["source"] == "test"
    assert hist[0]["authorized"] is False


def test_update_status_blocks_gated_without_authorization(sb):
    """The terminal-gated authorization fence must work the same as Sheets:
    autonomous code cannot set 'submitted' without authorized=True."""
    mod, fake = sb
    _seed_role(fake, job_key="gh|1")
    ws = mod._ws()
    ws.get_all_records()
    with pytest.raises(mod.UnauthorizedStatusWrite):
        mod.update_status(ws, 2, "submitted", source="agent")
    # role unchanged
    role = fake.table("roles").rows[0]
    assert role["status"] == "new"
    assert fake.table("status_history").rows == []


def test_update_status_authorized_gated_succeeds(sb):
    mod, fake = sb
    _seed_role(fake, job_key="gh|1")
    ws = mod._ws()
    ws.get_all_records()
    mod.update_status(ws, 2, "submitted", source="operator-cli", authorized=True)
    role = fake.table("roles").rows[0]
    assert role["status"] == "submitted"
    assert fake.table("status_history").rows[0]["authorized"] is True


def test_update_status_batch(sb):
    mod, fake = sb
    _seed_role(fake, job_key="gh|1", surfaced_date="2026-05-29")
    _seed_role(fake, job_key="gh|2", surfaced_date="2026-05-30")
    ws = mod._ws()
    ws.get_all_records()  # row 2 = gh|2 (newer), row 3 = gh|1
    mod.update_status_batch(ws, [(2, "good fit"), (3, "not a fit")],
                             source="test-batch")
    by_key = {r["job_key"]: r["status"] for r in fake.table("roles").rows}
    assert by_key == {"gh|1": "not a fit", "gh|2": "good fit"}
    assert len(fake.table("status_history").rows) == 2


def test_update_intent_writes_and_validates(sb):
    mod, fake = sb
    _seed_role(fake, job_key="gh|1")
    ws = mod._ws()
    ws.get_all_records()
    mod.update_intent(ws, 2, "active")
    role = fake.table("roles").rows[0]
    assert role["intent"] == "active"
    # invalid intent rejected
    with pytest.raises(ValueError):
        mod.update_intent(ws, 2, "nonsense")
    # clearing to '' is allowed
    mod.update_intent(ws, 2, "")
    role = fake.table("roles").rows[0]
    assert role["intent"] == ""


def test_ws_acell_and_cell_match_sheet_addressing(sb):
    """verify.py and fix_*.py reach for ws.acell()/ws.cell() — these must keep
    working against the supabase proxy so those scripts don't break post-cutover."""
    mod, fake = sb
    _seed_role(fake, job_key="gh|1", company="Acme", role="AE")
    ws = mod._ws()
    assert ws.acell("A1").value == "surfaced_date"      # header row
    assert ws.acell("B2").value == "Acme"                # row 2 col B = company
    assert ws.cell(2, 3).value == "AE"                   # row 2 col C = role
    assert ws.acell("J2").value == "gh|1"                # col J = job_key


def test_ws_batch_update_writes_back_to_rows(sb):
    """The enrich_* scripts use ws.batch_update with col-letter ranges to edit
    notes / urls / rationale. The proxy must translate those to per-role UPDATEs."""
    mod, fake = sb
    _seed_role(fake, job_key="gh|1")
    ws = mod._ws()
    ws.get_all_records()
    ws.batch_update([
        {"range": "H2", "values": [["notes-A"]]},
        {"range": "K2", "values": [["rationale-A"]]},
    ])
    role = fake.table("roles").rows[0]
    assert role["notes"] == "notes-A"
    assert role["rationale"] == "rationale-A"


# ─── router-level sanity (ensures the public ledger API dispatches correctly) ──

def test_router_default_is_sheets(monkeypatch):
    """Default behavior must remain Sheets so the live cron doesn't change
    silently after this code lands. The router reads LEDGER_BACKEND fresh each
    call so a missing env var means sheets."""
    monkeypatch.delenv("LEDGER_BACKEND", raising=False)
    # Strip from config.env too — patch jobcfg.load to return without it
    from alice import jobcfg
    monkeypatch.setattr(jobcfg, "load", lambda: {})
    from alice.persistence import ledger
    importlib.reload(ledger)
    assert ledger._backend() == "sheets"


def test_router_dispatches_to_supabase_when_configured(monkeypatch, sb):
    mod, fake = sb
    monkeypatch.setenv("LEDGER_BACKEND", "supabase")
    from alice.persistence import ledger
    importlib.reload(ledger)
    # Re-wire fake into the freshly-reloaded supabase_ledger module.
    from alice.persistence import supabase_ledger
    supabase_ledger.set_client_for_tests(fake)
    _seed_role(fake, job_key="gh|router|1", company="Routed", status="good fit")
    statuses, _, goodfit, total = ledger.load_statuses()
    assert total == 1
    assert statuses["gh|router|1"] == "good fit"
    assert goodfit["routed"] == 1


def test_router_unknown_backend_falls_back_loudly(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "mongo")  # not a thing
    from alice.persistence import ledger
    importlib.reload(ledger)
    assert ledger._backend() == "sheets"


# ─── dual backend (LEDGER_BACKEND=dual) ──────────────────────────────────────────
#
# A minimal fake gspread Worksheet so the Sheet write path runs without network.
# Rows are stored aligned to HEADERS; row 2 is records[0] (header on row 1).

import json as _json


class _FakeSheetWS:
    def __init__(self, headers):
        self._headers = headers
        self.records = []  # list[dict] keyed by header names; record[0] == row 2

    def seed(self, rows):
        self.records = [dict(r) for r in rows]

    def get_all_records(self):
        return [dict(r) for r in self.records]

    def batch_update(self, cells, value_input_option="RAW"):
        from gspread.utils import a1_to_rowcol
        for c in cells:
            row, col = a1_to_rowcol(c["range"])
            self.records[row - 2][self._headers[col - 1]] = c["values"][0][0]

    def insert_rows(self, rows, row=2, value_input_option="RAW"):
        new = []
        for r in rows:
            new.append(dict(zip(self._headers, list(r) + [""] * (len(self._headers) - len(r)))))
        self.records = new + self.records  # newest-first, like the live Sheet

    def acell(self, addr):
        from gspread.utils import a1_to_rowcol
        row, col = a1_to_rowcol(addr)
        val = (self._headers[col - 1] if row == 1
               else str(self.records[row - 2].get(self._headers[col - 1], "") or ""))
        return type("C", (), {"value": val})()


def _setup_dual(monkeypatch, tmp_path, fake, sheet_ws):
    """Reload ledger in dual mode, wire the supabase fake + a fake Sheet ws, and
    redirect every JSONL the write paths touch into tmp so tests stay hermetic."""
    monkeypatch.setenv("LEDGER_BACKEND", "dual")
    monkeypatch.setenv("ALICE_USER_ID", "test-operator")
    from alice.persistence import ledger
    from alice.persistence import supabase_ledger
    importlib.reload(ledger)
    supabase_ledger.set_client_for_tests(fake)
    monkeypatch.setattr(ledger, "_sheets_ws", lambda: sheet_ws)
    drift = tmp_path / "drift.jsonl"
    for mod in (ledger, supabase_ledger):
        monkeypatch.setattr(mod, "_WRITE_LOG", tmp_path / "write.jsonl", raising=False)
        monkeypatch.setattr(mod, "_INTENT_WRITE_LOG", tmp_path / "intent.jsonl", raising=False)
        monkeypatch.setattr(mod, "_BLOCKED_LOG", tmp_path / "blocked.jsonl", raising=False)
    monkeypatch.setattr(ledger, "_DRIFT_LOG", drift)
    return ledger, drift


def test_dual_backend_normalizes_both_alias(monkeypatch):
    monkeypatch.setenv("LEDGER_BACKEND", "both")
    from alice.persistence import ledger
    importlib.reload(ledger)
    assert ledger._backend() == "dual"


def test_dual_update_status_writes_to_both_backends_by_job_key(monkeypatch, tmp_path, sb):
    """The crux of dual mode: the two backends order rows differently, so a write
    addressed by the Supabase-read row_idx must still land on the right row in
    the Sheet (resolved by job_key), not on whatever row shares that index."""
    mod, fake = sb
    # Supabase order is surfaced_date desc, id desc → B (newer) at row 2, A at row 3.
    _seed_role(fake, job_key="jk-A", company="Acme", surfaced_date="2026-05-30", status="new")
    _seed_role(fake, job_key="jk-B", company="Beta", surfaced_date="2026-05-31", status="new")
    # The Sheet stores them in the OPPOSITE order → A at row 2, B at row 3.
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([
        {"company": "Acme", "role": "AE", "status": "new", "job_key": "jk-A",
         "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"},
        {"company": "Beta", "role": "AE", "status": "new", "job_key": "jk-B",
         "url": "https://example.test/jk-B", "surfaced_date": "2026-05-31"},
    ])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    ws = ledger._ws()
    assert isinstance(ws, ledger.DualWorksheet)
    rows = ws.get_all_records()                  # served by Supabase (preferred)
    assert ws._read_backend == "supabase"
    assert rows[0]["job_key"] == "jk-B"          # row_idx 2 == jk-B in Supabase

    ledger.update_status(ws, 2, "good fit", authorized=True, source="test")

    # Supabase: jk-B flipped, jk-A untouched.
    supa_rows = {r["job_key"]: r["status"] for r in fake.table("roles").rows}
    assert supa_rows == {"jk-A": "new", "jk-B": "good fit"}
    # Sheet: jk-B flipped at ITS row (row 3), jk-A (row 2) untouched — proves the
    # job_key remap, not a blind row_idx=2 write that would have hit Acme/jk-A.
    sheet_rows = {r["job_key"]: r["status"] for r in sheet.records}
    assert sheet_rows == {"jk-A": "new", "jk-B": "good fit"}
    # Both succeeded → no drift recorded.
    assert not drift.exists()


def test_dual_insert_new_writes_to_both(monkeypatch, tmp_path, sb):
    mod, fake = sb
    sheet = _FakeSheetWS(mod.HEADERS)
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)
    n = ledger.insert_new([{"company": "New Co", "role": "AE", "job_key": "jk-new",
                            "url": "https://example.test/jk-new", "score": "90"}])
    assert n == 1
    assert any(r["job_key"] == "jk-new" for r in fake.table("roles").rows)
    assert any(r["job_key"] == "jk-new" for r in sheet.records)
    assert not drift.exists()


def test_dual_logs_divergence_when_one_backend_fails(monkeypatch, tmp_path, sb):
    """If the Sheet write raises, the Supabase write still lands and the
    divergence is journaled to dual-backend-drift.jsonl."""
    mod, fake = sb
    _seed_role(fake, job_key="jk-A", company="Acme", surfaced_date="2026-05-30", status="new")
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "new", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    def _boom(*a, **k):
        raise RuntimeError("sheet down")
    monkeypatch.setattr(sheet, "batch_update", _boom)

    ws = ledger._ws()
    ws.get_all_records()
    ledger.update_status(ws, 2, "good fit", authorized=True, source="test")

    # Supabase still got the write.
    assert {r["job_key"]: r["status"] for r in fake.table("roles").rows} == {"jk-A": "good fit"}
    # Divergence journaled.
    assert drift.exists()
    rec = _json.loads(drift.read_text().strip().splitlines()[-1])
    assert rec["op"] == "update_status"
    assert rec["supabase_ok"] is True and rec["sheets_ok"] is False
    assert "sheet down" in rec["sheets_error"]


def test_dual_read_falls_back_to_sheet_when_supabase_unreachable(monkeypatch, tmp_path, sb):
    mod, fake = sb
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "good fit", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)

    # Make the Supabase accessor raise so the read proxy must fall back to Sheet.
    def _supa_boom():
        raise RuntimeError("supabase unreachable")
    monkeypatch.setattr(ledger, "_supabase", _supa_boom)

    ws = ledger._ws()
    rows = ws.get_all_records()
    assert ws._read_backend == "sheets"
    assert rows[0]["job_key"] == "jk-A"
    assert drift.exists()  # read fallback logged


def test_dual_gated_status_unauthorized_writes_neither(monkeypatch, tmp_path, sb):
    mod, fake = sb
    _seed_role(fake, job_key="jk-A", company="Acme", surfaced_date="2026-05-30", status="new")
    sheet = _FakeSheetWS(mod.HEADERS)
    sheet.seed([{"company": "Acme", "role": "AE", "status": "new", "job_key": "jk-A",
                 "url": "https://example.test/jk-A", "surfaced_date": "2026-05-30"}])
    ledger, drift = _setup_dual(monkeypatch, tmp_path, fake, sheet)
    ws = ledger._ws()
    ws.get_all_records()
    with pytest.raises(ledger.UnauthorizedStatusWrite):
        ledger.update_status(ws, 2, "submitted", source="test")  # gated, no authorization
    # Neither backend changed.
    assert {r["job_key"]: r["status"] for r in fake.table("roles").rows} == {"jk-A": "new"}
    assert {r["job_key"]: r["status"] for r in sheet.records} == {"jk-A": "new"}
