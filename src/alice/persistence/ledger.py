"""Opportunity ledger — the reverse-chron record + feedback surface.

This module is the ROUTER over the ledger backends. Supabase Postgres is the
canonical backend; the Google Sheets implementation is a legacy bridge kept for
migration; dual-write is the one-time cutover tool that mirrors writes to both.

  * "supabase" — the canonical Postgres backend (`supabase_ledger.py`).
  * "sheets"   — the legacy Google Sheets backend, kept for migration.
  * "dual"     — (alias "both") the cutover tool: writes to BOTH backends,
                 reads prefer Supabase.

Selection: LEDGER_BACKEND in ~/.config/job-search/config.env (or env var).

Dual mode (LEDGER_BACKEND=dual) is the cutover bridge: while both the Sheet and
Supabase are kept in sync, every write (insert_new, update_status,
update_status_batch, update_intent) fans out to BOTH backends; a failure in one
is logged but never blocks the other. Reads prefer Supabase (faster, no Google
token-refresh latency) and fall back to the Sheet if Supabase is unreachable.
Because row addressing is backend-relative (Sheet uses 1-based row index,
Supabase orders by surfaced_date/id), the dual proxy resolves each write's
target row by `job_key` so a write lands on the right row in each backend even
if their orderings have drifted. Any write-side divergence (one backend
succeeded, the other failed — or a row that exists in one backend but not the
other) is journaled to feedback/dual-backend-drift.jsonl so parity gaps surface.

Columns: surfaced_date | company | role | comp | source | score | status | notes | url | job_key
`status` is a dropdown you edit on the Sheets phone app:
    new | good fit | not a fit | materials pending | submitted | closed
Each run reads statuses to trim + tune, then inserts new finds at the top (newest first).

Auth (sheets backend): a Google service account (headless). In config.env:
    GOOGLE_SA_JSON=/path/to/google-sa.json
    LEDGER_SHEET_ID=<the sheet id from its URL>
The sheet must be shared (Editor) with the service account's client_email.

Auth (supabase backend): service-role key + a per-tenant user_id. In config.env:
    SUPABASE_URL=...
    SUPABASE_SERVICE_ROLE_KEY=...
    ALICE_USER_ID=operator

Write-site enforcement:
    TERMINAL_GATED statuses require operator-authorization at call site. Alice's
    autonomous code cannot set these without an explicit `authorized=True` arg
    carrying the source of authorization. Every status write is journaled to
    feedback/sheet-write-log.jsonl so auto_drop and other consumers can
    distinguish authorized writes from unauthorized ones.
"""
import datetime
import json
import os
import re
from pathlib import Path
from alice import repo_paths

from alice.jobcfg import load
# Shared primitives (single definition for both backends). Re-exported below so
# `ledger.UnauthorizedStatusWrite` / `ledger._journal` keep resolving.
from alice.persistence.ledger_common import UnauthorizedStatusWrite, _journal, Backend
from alice.persistence import ledger_common

HEADERS = ["surfaced_date", "company", "role", "comp", "source", "score",
           "status", "notes", "url", "job_key", "rationale", "status_changed_date",
           "intent"]
TERMINAL = {"not a fit", "submitted", "closed", "rejected"}
STATUS_OPTIONS = ["new", "good fit", "potential fit", "not a fit", "materials pending", "submitted",
                  "first screen scheduled", "interviewing", "offer", "negotiating", "closed"]

# The intent column (col M) — the operator's declared STATE about a role,
# distinct from pipeline-position `status`. Blank = undeclared (heuristic
# fallback). The five states are the intent vocabulary (see operator_intent).
INTENT_OPTIONS = ["active", "deliberating", "holding", "waiting", "done"]

# Statuses Alice CANNOT set autonomously. Authorization must come from the operator
# (email reply, Telegram message, or explicit CLI invocation by the operator).
TERMINAL_GATED = {"submitted", "interviewed", "interviewing", "offer", "offered",
                  "negotiating", "rejected", "rejected-by-us", "withdrawn", "closed"}

_WRITE_LOG = Path(repo_paths.FEEDBACK / "sheet-write-log.jsonl")
_BLOCKED_LOG = Path(repo_paths.FEEDBACK / "sheet-write-blocked.jsonl")
_INTENT_WRITE_LOG = Path(repo_paths.FEEDBACK / "intent-write-log.jsonl")
# Dual mode: write-side divergence between the two backends lands here so parity
# gaps are visible (one backend wrote, the other raised; or a row missing in one).
_DRIFT_LOG = Path(repo_paths.FEEDBACK / "dual-backend-drift.jsonl")

# Process-cached so _ensure_intent_column hits the network at most once per run.
_intent_col_ready = False


def _capture(exc, where):
    """Best-effort obs.capture that never raises (obs may be unimportable)."""
    try:
        import obs; obs.capture(exc, where=where)
    except Exception:
        pass


def _check_authorization(new_status, authorized, source):
    """Raise UnauthorizedStatusWrite if a gated status is being set without authorization.
    `source` is a short string naming the call site (e.g. 'confirm_and_execute',
    'imap_reply.drop', 'manual_cli').

    Thin wrapper over ledger_common._check_authorization that binds THIS module's
    TERMINAL_GATED / _BLOCKED_LOG / _journal at call time, so per-module
    monkeypatching of those globals keeps working."""
    ledger_common._check_authorization(
        new_status, authorized, source,
        terminal_gated=TERMINAL_GATED, blocked_log=_BLOCKED_LOG,
        journal=_journal, authority_phrase="the operator")


def _sheets_available():
    cfg = load()
    return bool(cfg.get("GOOGLE_SA_JSON")) and bool(cfg.get("LEDGER_SHEET_ID"))


def _sheets_ws():
    import gspread
    from google.oauth2.service_account import Credentials
    cfg = load()
    creds = Credentials.from_service_account_file(
        cfg["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(cfg["LEDGER_SHEET_ID"])
    ws = sh.sheet1
    if (ws.acell("A1").value or "") != "surfaced_date":
        ws.update("A1", [HEADERS])
        try:
            ws.freeze(rows=1)
            _apply_status_dropdown(ws)
        except Exception as _e:
            try:
                import obs; obs.capture(_e, where="ledger:_ws:freeze_or_dropdown")
            except Exception:
                pass
    _ensure_intent_column(ws)
    return ws


def _ensure_intent_column(ws):
    """Idempotently ensure col M is the 'intent' header with a dropdown.

    The A1-guard above only writes HEADERS on first init; existing sheets keep
    their old 12-column header, so the intent column is added here separately
    (self-applying migration). Process-cached via _intent_col_ready so we hit
    the network at most once per run. Additive: never touches columns A-L."""
    global _intent_col_ready
    if _intent_col_ready:
        return
    try:
        if (ws.acell("M1").value or "") != "intent":
            ws.update_acell("M1", "intent")
        ws.spreadsheet.batch_update({"requests": [{
            "setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2000,
                          "startColumnIndex": 12, "endColumnIndex": 13},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": o} for o in INTENT_OPTIONS]},
                         "showCustomUi": True, "strict": False}}}]})
        _intent_col_ready = True
    except Exception as _e:
        try:
            import obs; obs.capture(_e, where="ledger:_ensure_intent_column")
        except Exception:
            pass


def _apply_status_dropdown(ws):
    ws.spreadsheet.batch_update({"requests": [{
        "setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": 6, "endColumnIndex": 7},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": o} for o in STATUS_OPTIONS]},
                     "showCustomUi": True, "strict": False}}}]})


def _annotate_outcome_safe(ws, row_idx, new_status):
    """Annotate the prediction span for this row's job with the real outcome,
    so predictions can be graded against reality. No-op when tracing is off.
    Reads job_key (col J) only when tracing is on. Never raises (must not break
    a ledger write)."""
    try:
        from alice.observability import telemetry
        if not telemetry.is_on():
            return
        from gspread.utils import rowcol_to_a1
        job_key = (ws.acell(rowcol_to_a1(row_idx, 10)).value or "").strip()
        telemetry.annotate_outcome(job_key, new_status)
    except Exception:
        pass


def _sheets_update_status(ws, row_idx, new_status, also_set_date=True, *,
                          authorized=False, source="unspecified"):
    """Atomic helper: write column G (status) + column L (status_changed_date).
    Use this anywhere status changes so the date column stays in sync.

    For statuses in TERMINAL_GATED, callers must pass authorized=True with a
    `source` string identifying the user instruction that authorized it.
    Unauthorized attempts raise UnauthorizedStatusWrite and are journaled."""
    from gspread.utils import rowcol_to_a1
    _check_authorization(new_status, authorized, source)
    today = datetime.date.today().isoformat()
    cells = [{"range": rowcol_to_a1(row_idx, 7), "values": [[new_status]]}]
    if also_set_date:
        cells.append({"range": rowcol_to_a1(row_idx, 12), "values": [[today]]})
    ws.batch_update(cells, value_input_option="RAW")
    _journal(_WRITE_LOG, {
        "ts":         datetime.datetime.now().isoformat(timespec="seconds"),
        "row_idx":    row_idx,
        "status":     new_status,
        "authorized": bool(authorized),
        "source":     source,
    })
    _annotate_outcome_safe(ws, row_idx, new_status)


def _sheets_update_status_batch(ws, updates, *, authorized=False, source="unspecified"):
    """Batch update: updates = list of (row_idx, new_status) tuples.

    Same gating as update_status: any gated status in the batch requires
    authorized=True. Logs each row separately to the write journal."""
    from gspread.utils import rowcol_to_a1
    for _, new_status in updates:
        _check_authorization(new_status, authorized, source)
    today = datetime.date.today().isoformat()
    cells = []
    for row_idx, new_status in updates:
        cells.append({"range": rowcol_to_a1(row_idx, 7), "values": [[new_status]]})
        cells.append({"range": rowcol_to_a1(row_idx, 12), "values": [[today]]})
    if cells:
        ws.batch_update(cells, value_input_option="RAW")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for row_idx, new_status in updates:
        _journal(_WRITE_LOG, {
            "ts":         now,
            "row_idx":    row_idx,
            "status":     new_status,
            "authorized": bool(authorized),
            "source":     source,
        })
        _annotate_outcome_safe(ws, row_idx, new_status)


def _sheets_update_intent(ws, row_idx, intent, *, source="unspecified"):
    """Write the intent column (col M / index 13) for a row.

    UNLIKE update_status, intent is NOT terminal-gated and has NO authorization
    check: it is the operator's own declaration about their own state, so there is nothing
    to authorize. `intent=""` clears it (back to undeclared/heuristic-fallback).
    Journaled to feedback/intent-write-log.jsonl for parity with status writes."""
    from gspread.utils import rowcol_to_a1
    canon = (intent or "").strip().lower()
    if canon and canon not in INTENT_OPTIONS:
        raise ValueError(
            f"update_intent: intent must be '' (clear) or one of {INTENT_OPTIONS}; "
            f"got {intent!r}"
        )
    ws.batch_update(
        [{"range": rowcol_to_a1(row_idx, 13), "values": [[canon]]}],
        value_input_option="RAW",
    )
    _journal(_INTENT_WRITE_LOG, {
        "ts":      datetime.datetime.now().isoformat(timespec="seconds"),
        "row_idx": row_idx,
        "intent":  canon,
        "source":  source,
    })


def _sheets_load_statuses():
    """Return (statuses_by_key, notfit_counts, goodfit_counts, total_rows).
    Counts are per-company so company-wide suppression requires multiple labels."""
    from collections import Counter
    ws = _sheets_ws()
    rows = ws.get_all_records()
    statuses, notfit, goodfit = {}, Counter(), Counter()
    for r in rows:
        key = str(r.get("job_key") or r.get("url") or "")
        st = (r.get("status") or "").strip().lower()
        if key:
            statuses[key] = st
        comp = (r.get("company") or "").strip().lower()
 # strip "(YC)" / "(VC)" / "(auto)" suffixes so the same company across sources matches
        comp = re.sub(r"\s*\((YC|VC|auto)\)\s*$", "", comp)
        if st == "not a fit" and comp:
            notfit[comp] += 1
        elif st == "good fit" and comp:
            goodfit[comp] += 1
    return statuses, notfit, goodfit, len(rows)


def _sheets_last_write_for_row(row_idx):
    """Return the most recent journal entry for this row, or None.
    Consumers (focus.auto_drop) check `authorized` to avoid cascading errors
    from unauthorized Alice writes."""
    if not _WRITE_LOG.exists():
        return None
    last = None
    try:
        with _WRITE_LOG.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("row_idx") == row_idx:
                        last = rec
                except Exception:
                    continue
    except Exception:
        return None
    return last


def _sheets_insert_new(items):
    """items: dicts with company, role, comp, source, score, url, job_key.
    Inserts at row 2 (newest-first / reverse-chron). Returns count inserted.
    Column L (status_changed_date) left empty for new rows; only populated
    once status moves away from 'new'."""
    if not items:
        return 0
    ws = _sheets_ws()
    today = datetime.date.today().isoformat()
    rows = [[today, it.get("company", ""), it.get("role", ""), it.get("comp", ""),
             it.get("source", ""), it.get("score", ""), "new", "",
             it.get("url", ""), it.get("job_key", ""), it.get("rationale", ""), "",
             ""]  # col M intent: blank for new rows (undeclared -> heuristic)
            for it in items]
    ws.insert_rows(rows, row=2, value_input_option="RAW")
    return len(rows)


# ─── Backend router ─────────────────────────────────────────────────────────────

# The public API below dispatches to either the Sheets implementation above or
# the Supabase implementation in supabase_ledger.py. Selection is via
# LEDGER_BACKEND (config.env or env var); default is "sheets" so existing
# production behavior is unchanged until the operator explicitly flips.

_VALID_BACKENDS = {"sheets", "supabase", "dual"}


def _backend() -> str:
    """Read LEDGER_BACKEND fresh each call so tests + the operator can flip it without
    a process restart. "both" is an accepted alias for "dual". Unknown values
    fall back to "sheets" loudly."""
    val = (load().get("LEDGER_BACKEND") or os.environ.get("LEDGER_BACKEND") or "sheets").strip().lower()
    if val == "both":
        val = "dual"
    if val not in _VALID_BACKENDS:
 # Loud-not-silent: write a one-line warning then default to sheets.
        try:
            import obs; obs.capture(
                ValueError(f"LEDGER_BACKEND={val!r} not in {_VALID_BACKENDS}; using 'sheets'"),
                where="ledger:_backend")
        except Exception:
            pass
        return "sheets"
    return val


def _supabase():
    """Lazy import of the supabase adapter — we don't want to import it (and
    therefore pull jobcfg/etc.) when the sheets backend is active."""
    from alice.persistence import supabase_ledger  # noqa: WPS433 (intentional lazy import)
    return supabase_ledger


# ─── Dual backend (LEDGER_BACKEND=dual / "both") ─────────────────────────────────

# Both Sheet and Supabase are canonical. The DualWorksheet proxy is what the
# call sites receive from _ws(): it reads from the preferred backend (Supabase,
# Sheet fallback) and remembers each row's job_key so the dual-write functions
# can target the correct row in EACH backend even if their orderings diverge.


def _record_drift(op, detail, *, supabase_ok, supabase_error, sheets_ok, sheets_error):
    """Journal a write-side divergence to feedback/dual-backend-drift.jsonl.
    Called whenever the two backends did not both succeed for a single write."""
    _journal(_DRIFT_LOG, {
        "ts":             datetime.datetime.now().isoformat(timespec="seconds"),
        "op":             op,
        "detail":         detail,
        "supabase_ok":    supabase_ok,
        "supabase_error": supabase_error,
        "sheets_ok":      sheets_ok,
        "sheets_error":   sheets_error,
    })


class DualWorksheet:
    """Worksheet proxy for dual mode.

    Reads prefer Supabase (its SupabaseWorksheet) and fall back to the live
    gspread worksheet. get_all_records() records a row_idx -> job_key map keyed
    the same way callers enumerate (start=2), so a write addressed by the read's
    row_idx can be re-resolved to each backend's native row by job_key."""

    def __init__(self):
        self._supa_ws = None       # supabase_ledger.SupabaseWorksheet (lazy)
        self._sheets_ws = None     # gspread Worksheet (lazy)
        self._read_backend = None  # "supabase" | "sheets" — which served the read
        self._row_idx_to_job_key: dict[int, str] = {}
        self._sheet_jobkey_to_idx = None  # lazy job_key -> sheet row_idx index

 # --- lazy backend handles ---------------------------------------------------

    def _ensure_supa(self):
        if self._supa_ws is None:
            self._supa_ws = _supabase()._ws()
        return self._supa_ws

    def _ensure_sheets(self):
        if self._sheets_ws is None:
            self._sheets_ws = _sheets_ws()
        return self._sheets_ws

 # --- reads (prefer Supabase, fall back to Sheet) ----------------------------

    def get_all_records(self):
        rows = None
        try:
            rows = self._ensure_supa().get_all_records()
            self._read_backend = "supabase"
        except Exception as e:
            _capture(e, "ledger:dual:get_all_records:supabase_fallback")
            _record_drift("read", {"phase": "get_all_records"},
                          supabase_ok=False, supabase_error=repr(e),
                          sheets_ok=True, sheets_error=None)
            rows = self._ensure_sheets().get_all_records()
            self._read_backend = "sheets"
        self._row_idx_to_job_key = {}
        for i, r in enumerate(rows, start=2):
            self._row_idx_to_job_key[i] = str(r.get("job_key") or r.get("url") or "")
        return rows

    def acell(self, addr):
        try:
            return self._ensure_supa().acell(addr)
        except Exception:
            return self._ensure_sheets().acell(addr)

    def cell(self, row, col):
        try:
            return self._ensure_supa().cell(row, col)
        except Exception:
            return self._ensure_sheets().cell(row, col)

    def col_values(self, col_idx):
        try:
            return self._ensure_supa().col_values(col_idx)
        except Exception:
            return self._ensure_sheets().col_values(col_idx)

 # --- row resolution ---------------------------------------------------------

    def _job_key_for_row_idx(self, row_idx):
        if not self._row_idx_to_job_key:
            self.get_all_records()
        return self._row_idx_to_job_key.get(row_idx, "")

    def _sheet_index(self):
        """Lazy {job_key -> sheet row_idx} built from one Sheet read, cached for
        the life of this proxy so repeated writes don't re-read the Sheet."""
        if self._sheet_jobkey_to_idx is None:
            recs = self._ensure_sheets().get_all_records()
            idx = {}
            for i, r in enumerate(recs, start=2):
                k = str(r.get("job_key") or r.get("url") or "")
                if k:
                    idx[k] = i
            self._sheet_jobkey_to_idx = idx
        return self._sheet_jobkey_to_idx

    def _supa_native_idx(self, row_idx, job_key):
        """Resolve the Supabase-native row_idx for a write. If the read came from
        Supabase, row_idx is already native; otherwise locate by job_key."""
        if self._read_backend == "supabase":
            return row_idx
        ws = self._ensure_supa()
        for i, r in enumerate(ws.get_all_records(), start=2):
            if str(r.get("job_key") or r.get("url") or "") == (job_key or ""):
                return i
        return None

    def _sheets_native_idx(self, row_idx, job_key):
        """Resolve the Sheet-native row_idx for a write. If the read came from the
        Sheet, row_idx is already native; otherwise look up by job_key (falling
        back to row_idx when the row carries no job_key to map on)."""
        if self._read_backend == "sheets":
            return row_idx
        if not job_key:
            return row_idx
        return self._sheet_index().get(job_key)


def _dual_update_status(ws, row_idx, new_status, also_set_date, authorized, source):
 # Authorization is a structural guard, not a backend failure — check once and
 # let it raise before either backend is touched (both would raise identically).
    _check_authorization(new_status, authorized, source)
    job_key = ws._job_key_for_row_idx(row_idx)
    supa_ok, sheets_ok = False, False
    supa_err = sheets_err = None
    try:
        sidx = ws._supa_native_idx(row_idx, job_key)
        if sidx is None:
            raise ValueError(f"row not found in supabase for job_key={job_key!r}")
        _supabase().update_status(ws._ensure_supa(), sidx, new_status, also_set_date,
                                  authorized=authorized, source=source)
        supa_ok = True
    except Exception as e:
        supa_err = repr(e)
        _capture(e, "ledger:dual:update_status:supabase")
    try:
        hidx = ws._sheets_native_idx(row_idx, job_key)
        if hidx is None:
            raise ValueError(f"row not found in sheet for job_key={job_key!r}")
        _sheets_update_status(ws._ensure_sheets(), hidx, new_status, also_set_date,
                              authorized=authorized, source=source)
        sheets_ok = True
    except Exception as e:
        sheets_err = repr(e)
        _capture(e, "ledger:dual:update_status:sheets")
    if not (supa_ok and sheets_ok):
        _record_drift("update_status",
                      {"row_idx": row_idx, "job_key": job_key, "status": new_status},
                      supabase_ok=supa_ok, supabase_error=supa_err,
                      sheets_ok=sheets_ok, sheets_error=sheets_err)


def _dual_update_status_batch(ws, updates, authorized, source):
    for _, ns in updates:
        _check_authorization(ns, authorized, source)
    job_keys = {ri: ws._job_key_for_row_idx(ri) for ri, _ in updates}
    supa_ok, sheets_ok = False, False
    supa_err = sheets_err = None
    try:
        supa_updates = []
        for ri, ns in updates:
            sidx = ws._supa_native_idx(ri, job_keys[ri])
            if sidx is None:
                raise ValueError(f"row not found in supabase for job_key={job_keys[ri]!r}")
            supa_updates.append((sidx, ns))
        _supabase().update_status_batch(ws._ensure_supa(), supa_updates,
                                        authorized=authorized, source=source)
        supa_ok = True
    except Exception as e:
        supa_err = repr(e)
        _capture(e, "ledger:dual:update_status_batch:supabase")
    try:
        sheet_updates = []
        for ri, ns in updates:
            hidx = ws._sheets_native_idx(ri, job_keys[ri])
            if hidx is None:
                raise ValueError(f"row not found in sheet for job_key={job_keys[ri]!r}")
            sheet_updates.append((hidx, ns))
        _sheets_update_status_batch(ws._ensure_sheets(), sheet_updates,
                                    authorized=authorized, source=source)
        sheets_ok = True
    except Exception as e:
        sheets_err = repr(e)
        _capture(e, "ledger:dual:update_status_batch:sheets")
    if not (supa_ok and sheets_ok):
        _record_drift("update_status_batch",
                      {"updates": [[ri, ns] for ri, ns in updates]},
                      supabase_ok=supa_ok, supabase_error=supa_err,
                      sheets_ok=sheets_ok, sheets_error=sheets_err)


def _dual_update_intent(ws, row_idx, intent, source):
    job_key = ws._job_key_for_row_idx(row_idx)
    supa_ok, sheets_ok = False, False
    supa_err = sheets_err = None
    try:
        sidx = ws._supa_native_idx(row_idx, job_key)
        if sidx is None:
            raise ValueError(f"row not found in supabase for job_key={job_key!r}")
        _supabase().update_intent(ws._ensure_supa(), sidx, intent, source=source)
        supa_ok = True
    except Exception as e:
        supa_err = repr(e)
        _capture(e, "ledger:dual:update_intent:supabase")
    try:
        hidx = ws._sheets_native_idx(row_idx, job_key)
        if hidx is None:
            raise ValueError(f"row not found in sheet for job_key={job_key!r}")
        _sheets_update_intent(ws._ensure_sheets(), hidx, intent, source=source)
        sheets_ok = True
    except Exception as e:
        sheets_err = repr(e)
        _capture(e, "ledger:dual:update_intent:sheets")
    if not (supa_ok and sheets_ok):
        _record_drift("update_intent",
                      {"row_idx": row_idx, "job_key": job_key, "intent": intent},
                      supabase_ok=supa_ok, supabase_error=supa_err,
                      sheets_ok=sheets_ok, sheets_error=sheets_err)


def _dual_insert_new(items):
    """Insert into both backends. Returns the Supabase count when available, else
    the Sheet count — the caller only uses it for a 'how many new' message."""
    if not items:
        return 0
    supa_count = sheet_count = None
    supa_err = sheets_err = None
    try:
        supa_count = _supabase().insert_new(items)
    except Exception as e:
        supa_err = repr(e)
        _capture(e, "ledger:dual:insert_new:supabase")
    try:
        sheet_count = _sheets_insert_new(items)
    except Exception as e:
        sheets_err = repr(e)
        _capture(e, "ledger:dual:insert_new:sheets")
    if supa_err is not None or sheets_err is not None:
        _record_drift("insert_new",
                      {"n_items": len(items), "supabase_count": supa_count,
                       "sheet_count": sheet_count},
                      supabase_ok=supa_err is None, supabase_error=supa_err,
                      sheets_ok=sheets_err is None, sheets_error=sheets_err)
    if supa_count is not None:
        return supa_count
    if sheet_count is not None:
        return sheet_count
    return 0


def _dual_load_statuses():
    """Read prefers Supabase; fall back to the Sheet if Supabase is unreachable."""
    try:
        return _supabase().load_statuses()
    except Exception as e:
        _capture(e, "ledger:dual:load_statuses:supabase_fallback")
        _record_drift("read", {"phase": "load_statuses"},
                      supabase_ok=False, supabase_error=repr(e),
                      sheets_ok=True, sheets_error=None)
        return _sheets_load_statuses()


def available():
    backend = _backend()
    if backend == "supabase":
        return _supabase().available()
    if backend == "dual":
 # Available if EITHER backend can serve — dual write degrades to single
 # write (and logs drift) rather than going dark when one is down.
        supa = False
        try:
            supa = _supabase().available()
        except Exception:
            supa = False
        return bool(supa) or _sheets_available()
    return _sheets_available()


def _ws():
    backend = _backend()
    if backend == "supabase":
        return _supabase()._ws()
    if backend == "dual":
        return DualWorksheet()
    return _sheets_ws()


def update_status(ws, row_idx, new_status, also_set_date=True, *,
                  authorized=False, source="unspecified"):
    backend = _backend()
    if backend == "supabase":
        return _supabase().update_status(
            ws, row_idx, new_status, also_set_date,
            authorized=authorized, source=source)
    if backend == "dual":
        return _dual_update_status(
            ws, row_idx, new_status, also_set_date, authorized, source)
    return _sheets_update_status(
        ws, row_idx, new_status, also_set_date,
        authorized=authorized, source=source)


def update_status_batch(ws, updates, *, authorized=False, source="unspecified"):
    backend = _backend()
    if backend == "supabase":
        return _supabase().update_status_batch(
            ws, updates, authorized=authorized, source=source)
    if backend == "dual":
        return _dual_update_status_batch(ws, updates, authorized, source)
    return _sheets_update_status_batch(
        ws, updates, authorized=authorized, source=source)


def update_intent(ws, row_idx, intent, *, source="unspecified"):
    backend = _backend()
    if backend == "supabase":
        return _supabase().update_intent(ws, row_idx, intent, source=source)
    if backend == "dual":
        return _dual_update_intent(ws, row_idx, intent, source)
    return _sheets_update_intent(ws, row_idx, intent, source=source)


def load_statuses():
    backend = _backend()
    if backend == "supabase":
        return _supabase().load_statuses()
    if backend == "dual":
        return _dual_load_statuses()
    return _sheets_load_statuses()


def last_write_for_row(row_idx):
 # Both backends journal to the same JSONL path, so the Sheets reader is
 # authoritative for all modes (including dual).
    if _backend() == "supabase":
        return _supabase().last_write_for_row(row_idx)
    return _sheets_last_write_for_row(row_idx)


def insert_new(items):
    backend = _backend()
    if backend == "supabase":
        return _supabase().insert_new(items)
    if backend == "dual":
        return _dual_insert_new(items)
    return _sheets_insert_new(items)
