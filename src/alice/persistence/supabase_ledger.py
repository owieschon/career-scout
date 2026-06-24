"""Supabase Postgres adapter for Alice's pipeline ledger.

This is the canonical ledger backend. It exposes the same API as the router in
`ledger.py` so the ~20 call sites (daily_delta, imap_reply, telegram_bot, tools,
prep_*, focus, …) don't have to change. The legacy Google Sheets backend lives
in `ledger.py`; dual-write is the one-time cutover tool that mirrors writes to
both during migration.

Tables (created by migrations/supabase/0001_init.sql):
    app_users / roles / status_history / fit_verdicts / sources / oos_review

Identity model:
    Sheet rows are addressed by 1-based row index. Supabase rows are addressed
    by bigserial `id`. The adapter exposes a `SupabaseWorksheet` proxy that
    keeps a per-call list of role ids ordered the same way the Sheet returns
    rows (newest surfaced_date first, like the Sheet's reverse-chron). Callers
    that do `for i, r in enumerate(rows, start=2): ...` and then
    `update_status(ws, i, ...)` keep working byte-identically.

Auth:
    The daemon authenticates with the SUPABASE_SERVICE_ROLE_KEY (bypasses RLS).
    Per-user JWTs are not yet wired; the schema's RLS policies are already in
    place so adding them is a config flip, not a migration.

Required config (~/.config/job-search/config.env, mode 600):
    SUPABASE_URL=https://<ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=<service_role key>
    SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
    ALICE_USER_ID=javery                        (the user_id this daemon represents)

Lazy import of `supabase` so installing the SDK is not required to run the
existing test suite. Tests inject a fake client via `set_client_for_tests`.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter
from pathlib import Path
from alice import repo_paths

# Shared primitives (single definition for both backends). Re-exported below so
# `supabase_ledger.UnauthorizedStatusWrite` / `supabase_ledger._journal` resolve.
from alice.persistence.ledger_common import UnauthorizedStatusWrite, _journal, Backend
from alice.persistence import ledger_common

# Mirror ledger.py's constants so importers can use the same source-of-truth
# regardless of backend.
HEADERS = ["surfaced_date", "company", "role", "comp", "source", "score",
           "status", "notes", "url", "job_key", "rationale", "status_changed_date",
           "intent"]
TERMINAL = {"not a fit", "submitted", "closed", "rejected"}
STATUS_OPTIONS = ["new", "good fit", "potential fit", "not a fit", "materials pending",
                  "submitted", "first screen scheduled", "interviewing", "offer",
                  "negotiating", "closed"]
INTENT_OPTIONS = ["active", "deliberating", "holding", "waiting", "done"]
TERMINAL_GATED = {"submitted", "interviewed", "interviewing", "offer", "offered",
                  "negotiating", "rejected", "rejected-by-us", "withdrawn", "closed"}

_WRITE_LOG = Path(repo_paths.FEEDBACK / "sheet-write-log.jsonl")
_BLOCKED_LOG = Path(repo_paths.FEEDBACK / "sheet-write-blocked.jsonl")
_INTENT_WRITE_LOG = Path(repo_paths.FEEDBACK / "intent-write-log.jsonl")


# --- client management ----------------------------------------------------------

_client = None
_client_is_fake = False


def set_client_for_tests(fake):
    """Inject an in-memory fake client (tests/test_supabase_ledger.py uses this).
    The fake must implement the small protocol the adapter relies on; see
    `_FakeClient` in the test module."""
    global _client, _client_is_fake
    _client = fake
    _client_is_fake = True


def _get_client():
    """Lazy-construct the supabase-py client. Imports are inside the function so
    the SDK is not required to import this module (the suite uses a fake)."""
    global _client
    if _client is not None:
        return _client
    from alice.jobcfg import load
    cfg = load()
    url = cfg.get("SUPABASE_URL")
    key = cfg.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        raise RuntimeError(
            "supabase_ledger: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be set "
            "in ~/.config/job-search/config.env before using the supabase backend"
        )
    try:
        from supabase import create_client
    except ImportError as e:
        raise RuntimeError(
            "supabase_ledger: the 'supabase' package is not installed. "
            "Run: python3 -m pip install supabase"
        ) from e
    _client = create_client(url, key)
    return _client


def _user_id() -> str:
    from alice.jobcfg import load
    uid = load().get("ALICE_USER_ID") or os.environ.get("ALICE_USER_ID") or "javery"
    return uid


def available() -> bool:
    """Mirror of ledger.available(): returns True iff config + (test) client suffice."""
    if _client_is_fake:
        return True
    from alice.jobcfg import load
    cfg = load()
    return bool(cfg.get("SUPABASE_URL")) and bool(cfg.get("SUPABASE_SERVICE_ROLE_KEY"))


# --- journaling (same JSONL paths as Sheets backend, so consumers like
# focus.auto_drop keep working unchanged) ----------------------------------
# `_journal` is re-exported from ledger_common; the path is always passed by the
# caller, so this module's monkeypatchable _WRITE_LOG/_BLOCKED_LOG still route
# through the patched path.


def _check_authorization(new_status, authorized, source):
    """Thin wrapper over ledger_common._check_authorization that binds THIS
    module's TERMINAL_GATED / _BLOCKED_LOG / _journal at call time, preserving
    per-module monkeypatching and this backend's exact message wording."""
    ledger_common._check_authorization(
        new_status, authorized, source,
        terminal_gated=TERMINAL_GATED, blocked_log=_BLOCKED_LOG,
        journal=_journal, authority_phrase="Jordan Avery")


# --- the worksheet proxy --------------------------------------------------------

class SupabaseWorksheet:
    """Duck-types the gspread Worksheet surface the call sites depend on.

    Methods supported (audited from grep over all consumers in scripts/):
        get_all_records()       — primary read path, 90% of consumers
        acell(addr)             — col-letter+row reads (verify.py, fix_*.py)
        cell(row, col)          — row,col reads (fix_ptc_onshape_status.py)
        col_values(col)         — column reads (verify.py)
        batch_update(cells, …)  — write paths in enrich_*, backfill_urls, migrate_sheet
        update_acell, update    — only in init paths; not needed for daemon ops
        insert_rows(rows, row=) — only used by ledger.insert_new (internal)
        id, spreadsheet         — only oos_eval.py reaches for these
    """

    def __init__(self, client, user_id: str):
        self._client = client
        self._user_id = user_id
 # Populated lazily by get_all_records() so row_idx -> role_id lookups
 # in update_status(ws, row_idx, …) work after a read.
        self._row_idx_to_id: dict[int, int] = {}
 # Cached row order so re-callers see a stable view within one call.
        self._rows_cache: list[dict] | None = None

    def _select_roles_ordered(self) -> list[dict]:
        """Pull all roles for this tenant in reverse-chron (Sheet's natural order).

        Returns a list of dicts shaped like the Sheet's column names so existing
        consumers do not need code changes.
        """
        resp = (
            self._client
            .table("roles")
            .select("id, surfaced_date, company, role, comp, source, score, status, "
                    "notes, url, job_key, rationale, status_changed_date, intent")
            .eq("user_id", self._user_id)
            .order("surfaced_date", desc=True)
            .order("id", desc=True)             # tiebreak so order is deterministic
            .execute()
        )
        rows = resp.data or []
        out = []
        self._row_idx_to_id.clear()
        for i, r in enumerate(rows, start=2):   # match Sheet's 1-based with header on row 1
            self._row_idx_to_id[i] = r["id"]
            out.append({
                "surfaced_date":       _date_str(r.get("surfaced_date")),
                "company":             r.get("company") or "",
                "role":                r.get("role") or "",
                "comp":                r.get("comp") or "",
                "source":              r.get("source") or "",
                "score":               r.get("score") or "",
                "status":              r.get("status") or "",
                "notes":               r.get("notes") or "",
                "url":                 r.get("url") or "",
                "job_key":             r.get("job_key") or "",
                "rationale":           r.get("rationale") or "",
                "status_changed_date": _date_str(r.get("status_changed_date")),
                "intent":              r.get("intent") or "",
            })
        return out

    def get_all_records(self):
        if self._rows_cache is None:
            self._rows_cache = self._select_roles_ordered()
        return list(self._rows_cache)

    def _id_for_row_idx(self, row_idx: int) -> int:
        """Resolve a 1-based Sheet-style row_idx to a Postgres role.id.

        Forces a read if get_all_records() hasn't been called this session; that
        matches the Sheets contract where update_status assumes the caller knows
        which row it is updating.
        """
        if not self._row_idx_to_id:
            self.get_all_records()
        rid = self._row_idx_to_id.get(row_idx)
        if rid is None:
            raise ValueError(
                f"supabase_ledger: row_idx={row_idx} does not resolve to a role. "
                f"Call get_all_records() before update_status."
            )
        return rid

 # --- gspread-compat methods used by long-tail consumers --------------------

    def acell(self, addr):
        """A1-notation single-cell read. Returns a tiny shim with `.value`."""
        col_letter, row_str = _parse_a1(addr)
        col_idx = _col_letter_to_idx(col_letter)
        return _Cell(self._value_at(int(row_str), col_idx))

    def cell(self, row, col):
        return _Cell(self._value_at(row, col))

    def col_values(self, col_idx: int) -> list[str]:
        rows = self.get_all_records()
        col_name = HEADERS[col_idx - 1]
        return [HEADERS[col_idx - 1]] + [str(r.get(col_name) or "") for r in rows]

    def _value_at(self, row_idx: int, col_idx: int) -> str:
        if row_idx == 1:
            return HEADERS[col_idx - 1] if col_idx - 1 < len(HEADERS) else ""
        rows = self.get_all_records()
        if row_idx - 2 < 0 or row_idx - 2 >= len(rows):
            return ""
        col_name = HEADERS[col_idx - 1]
        v = rows[row_idx - 2].get(col_name)
        return "" if v is None else str(v)

    def batch_update(self, cells, value_input_option="RAW"):
        """Generic write: list of {"range": "<A1>", "values": [["<val>"]]}.

        Maps each cell back to its (role_id, column) and issues per-role UPDATEs.
        This is the path enrich_hypotheses / enrich_manual / backfill_urls use
        to edit notes, urls, rationale, etc.
        """
        by_role: dict[int, dict] = {}
        for c in cells:
            a1 = c["range"]
            val = c["values"][0][0]
            col_letter, row_str = _parse_a1(a1)
            row_idx = int(row_str)
            col_idx = _col_letter_to_idx(col_letter)
            if col_idx - 1 >= len(HEADERS):
                continue
            col_name = HEADERS[col_idx - 1]
            rid = self._id_for_row_idx(row_idx)
            by_role.setdefault(rid, {})[col_name] = val
        for rid, patch in by_role.items():
            (self._client.table("roles")
                 .update(patch)
                 .eq("user_id", self._user_id)
                 .eq("id", rid)
                 .execute())
 # Invalidate the local cache so a subsequent get_all_records() sees the write.
        self._rows_cache = None

    def insert_rows(self, rows, row=2, value_input_option="RAW"):
        """Compat for the Sheets `insert_rows` call. Each row is a list aligned
        with HEADERS. Routes through `_insert_role_dicts` for one consistent path."""
        items = []
        for r in rows:
            d = dict(zip(HEADERS, r + [""] * (len(HEADERS) - len(r))))
            items.append({
                "company":   d.get("company", ""),
                "role":      d.get("role", ""),
                "comp":      d.get("comp", ""),
                "source":    d.get("source", ""),
                "score":     d.get("score", ""),
                "url":       d.get("url", ""),
                "job_key":   d.get("job_key", ""),
                "rationale": d.get("rationale", ""),
            })
        return _insert_role_dicts(self._client, self._user_id, items)

 # --- attrs reached by oos_eval.py — raise on access so a stray
 # dereference fails loudly instead of silently misbehaving.
    @property
    def spreadsheet(self):
        raise NotImplementedError(
            "supabase_ledger: SupabaseWorksheet.spreadsheet is not implemented. "
            "OOS data lives in the oos_review table."
        )

    @property
    def id(self):
        return None


class _Cell:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


# --- A1 helpers (no gspread import; we keep this module dependency-light) -------

_A1_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _parse_a1(addr: str) -> tuple[str, str]:
    m = _A1_RE.match(addr)
    if not m:
        raise ValueError(f"supabase_ledger: bad A1 address {addr!r}")
    return m.group(1).upper(), m.group(2)


def _col_letter_to_idx(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _date_str(v) -> str:
    """Render a date/datetime as YYYY-MM-DD to match the Sheet's string form."""
    if v is None or v == "":
        return ""
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()[:10]
    s = str(v)
    return s[:10]


# --- the ledger-API mirror ------------------------------------------------------

def _ws():
    """Mirror of ledger._ws(): returns a worksheet-shaped handle for this tenant."""
    return SupabaseWorksheet(_get_client(), _user_id())


def load_statuses():
    """Same return shape as ledger.load_statuses():
        (statuses_by_key, notfit_counts, goodfit_counts, total_rows)"""
    client = _get_client()
    uid = _user_id()
    resp = (
        client.table("roles")
              .select("job_key, url, status, company")
              .eq("user_id", uid)
              .execute()
    )
    rows = resp.data or []
    statuses, notfit, goodfit = {}, Counter(), Counter()
    for r in rows:
        key = str(r.get("job_key") or r.get("url") or "")
        st = (r.get("status") or "").strip().lower()
        if key:
            statuses[key] = st
        comp = (r.get("company") or "").strip().lower()
        comp = re.sub(r"\s*\((YC|VC|auto)\)\s*$", "", comp)
        if st == "not a fit" and comp:
            notfit[comp] += 1
        elif st == "good fit" and comp:
            goodfit[comp] += 1
    return statuses, notfit, goodfit, len(rows)


def _insert_role_dicts(client, user_id: str, items: list[dict]) -> int:
    """Shared insert path. Idempotent via the (user_id, job_key) unique index:
    duplicate job_keys are skipped (count returned reflects net new rows)."""
    if not items:
        return 0
    today = datetime.date.today().isoformat()
    rows_to_upsert = []
    for it in items:
        rows_to_upsert.append({
            "user_id":       user_id,
            "job_key":       it.get("job_key", ""),
            "surfaced_date": today,
            "company":       it.get("company", ""),
            "role":          it.get("role", ""),
            "comp":          it.get("comp", ""),
            "source":        it.get("source", ""),
            "score":         str(it.get("score", "")) if it.get("score") not in ("", None) else "",
            "status":        "new",
            "notes":         "",
            "url":           it.get("url", ""),
            "rationale":     it.get("rationale", ""),
            "intent":        "",
        })
 # `upsert` with the unique constraint name skips duplicates without raising.
    resp = (
        client.table("roles")
              .upsert(rows_to_upsert,
                      on_conflict="user_id,job_key",
                      ignore_duplicates=True)
              .execute()
    )
    return len(resp.data or [])


def insert_new(items):
    return _insert_role_dicts(_get_client(), _user_id(), items)


def update_status(ws, row_idx, new_status, also_set_date=True, *,
                  authorized=False, source="unspecified"):
    """Same signature as ledger.update_status. `ws` is the SupabaseWorksheet
    proxy (returned by _ws()); row_idx is resolved through the proxy's
    row_idx -> role.id map."""
    _check_authorization(new_status, authorized, source)
    rid = ws._id_for_row_idx(row_idx)
    today = datetime.date.today().isoformat()
    patch: dict = {"status": new_status}
    if also_set_date:
        patch["status_changed_date"] = today
    (ws._client.table("roles")
         .update(patch)
         .eq("user_id", ws._user_id)
         .eq("id", rid)
         .execute())
    (ws._client.table("status_history")
         .insert({"role_id": rid, "user_id": ws._user_id,
                  "status": new_status, "authorized": bool(authorized),
                  "source": source})
         .execute())
    _journal(_WRITE_LOG, {
        "ts":         datetime.datetime.now().isoformat(timespec="seconds"),
        "row_idx":    row_idx,
        "status":     new_status,
        "authorized": bool(authorized),
        "source":     source,
    })
    ws._rows_cache = None
    _annotate_outcome_safe(rid, new_status)


def update_status_batch(ws, updates, *, authorized=False, source="unspecified"):
    for _, new_status in updates:
        _check_authorization(new_status, authorized, source)
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for row_idx, new_status in updates:
        rid = ws._id_for_row_idx(row_idx)
        (ws._client.table("roles")
             .update({"status": new_status, "status_changed_date": today})
             .eq("user_id", ws._user_id)
             .eq("id", rid)
             .execute())
        (ws._client.table("status_history")
             .insert({"role_id": rid, "user_id": ws._user_id,
                      "status": new_status, "authorized": bool(authorized),
                      "source": source})
             .execute())
        _journal(_WRITE_LOG, {
            "ts":         now,
            "row_idx":    row_idx,
            "status":     new_status,
            "authorized": bool(authorized),
            "source":     source,
        })
        _annotate_outcome_safe(rid, new_status)
    ws._rows_cache = None


def update_intent(ws, row_idx, intent, *, source="unspecified"):
    canon = (intent or "").strip().lower()
    if canon and canon not in INTENT_OPTIONS:
        raise ValueError(
            f"update_intent: intent must be '' (clear) or one of {INTENT_OPTIONS}; "
            f"got {intent!r}"
        )
    rid = ws._id_for_row_idx(row_idx)
    (ws._client.table("roles")
         .update({"intent": canon})
         .eq("user_id", ws._user_id)
         .eq("id", rid)
         .execute())
    _journal(_INTENT_WRITE_LOG, {
        "ts":      datetime.datetime.now().isoformat(timespec="seconds"),
        "row_idx": row_idx,
        "intent":  canon,
        "source":  source,
    })
    ws._rows_cache = None


def last_write_for_row(row_idx):
    """Tail of the JSONL write log for this row_idx. Mirrors ledger.last_write_for_row
    exactly: same path, same JSON shape, same return contract."""
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


def _annotate_outcome_safe(role_id, new_status):
    """Phoenix outcome→prediction annotation (mirrors ledger._annotate_outcome_safe).
    Best-effort; never raises (must not break a ledger write)."""
    try:
        from alice.observability import telemetry
        if not telemetry.is_on():
            return
 # Pull job_key cheaply for the annotate call.
        client = _get_client()
        resp = (client.table("roles").select("job_key")
                      .eq("user_id", _user_id())
                      .eq("id", role_id).single().execute())
        jk = (resp.data or {}).get("job_key", "")
        telemetry.annotate_outcome(jk, new_status)
    except Exception:
        pass
