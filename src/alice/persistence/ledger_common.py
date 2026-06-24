"""Shared primitives for the ledger backends.

Supabase Postgres is the canonical ledger backend; Google Sheets is a legacy
bridge kept for migration; dual-write is the one-time cutover tool. The router
(`ledger.py`) and the Postgres adapter (`supabase_ledger.py`) both build on the
primitives here so the safety gate and the journaling format have a single
definition rather than two copies that can drift.

What lives here:

  * `UnauthorizedStatusWrite` — the exception both backends raise when a gated
    status write is attempted without operator authorization. Both modules
    re-export this name, so `ledger.UnauthorizedStatusWrite` and
    `supabase_ledger.UnauthorizedStatusWrite` resolve to this same class.
  * `_journal(path, record)` — append-one-JSON-line journaling, used for the
    write log, blocked log, intent log, and drift log. The path is always
    passed by the caller, so a module that monkeypatches its own `_WRITE_LOG`
    (etc.) still routes through the patched path.
  * `_check_authorization(...)` — the SAFETY-CRITICAL gate that refuses to write
    a TERMINAL_GATED status without authorization. The control flow is shared;
    each backend supplies its own gated set, blocked-log path, journal function,
    and message authority-phrase so the per-module behavior (including
    test-time monkeypatching of those globals) is preserved exactly.
  * `Backend` — a typing.Protocol documenting the semantic operations a ledger
    backend provides. Structure/documentation only; the existing backends are
    not required to formally subclass it.
"""
from __future__ import annotations

import datetime
import traceback
import json
from typing import Protocol, Any, Optional


class UnauthorizedStatusWrite(Exception):
    """Raised when autonomous code tries to set a gated status without the operator authorization."""


def _journal(path, record):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _check_authorization(new_status, authorized, source, *,
                         terminal_gated, blocked_log, journal, authority_phrase):
    """Raise UnauthorizedStatusWrite if a gated status is being set without authorization.

    `source` is a short string naming the call site (e.g. 'confirm_and_execute',
    'imap_reply.drop', 'manual_cli').

    The gating control flow is identical across backends; the keyword args carry
    the per-backend pieces so each module keeps its exact current behavior:
        terminal_gated   — the module's TERMINAL_GATED set (resolved at call time)
        blocked_log      — the module's _BLOCKED_LOG path (monkeypatchable)
        journal          — the module's _journal function
        authority_phrase — trailing sentence's authorizing-party wording
    """
    canon = (new_status or "").strip().lower()
    if canon in terminal_gated and not authorized:
        record = {
            "ts":          datetime.datetime.now().isoformat(timespec="seconds"),
            "status":      canon,
            "source":      source,
            "stack":       "".join(traceback.format_stack(limit=6)),
        }
        journal(blocked_log, record)
        raise UnauthorizedStatusWrite(
            f"Refusing to set gated status {canon!r} from {source!r} without authorization. "
            f"TERMINAL_GATED={sorted(terminal_gated)}. Pass authorized=True only when {authority_phrase} "
            f"explicitly instructed this status change."
        )


class Backend(Protocol):
    """The semantic operations a ledger backend provides.

    Documentation/structure only — this records the shape the router dispatches
    to. The concrete backends (the Sheets functions in `ledger.py` and the
    `supabase_ledger` module) are NOT required to formally subclass this; their
    signatures already match it.

    Row addressing: callers read with `_ws()` then enumerate rows starting at
    index 2 (a Sheet's header is row 1), and pass that 1-based `row_idx` to the
    mutators. Each backend resolves `row_idx` to its native key.
    """

    def available(self) -> bool:
        """True iff this backend is configured well enough to serve."""
        ...

    def insert_new(self, items: list[dict]) -> int:
        """Insert new role rows (newest-first). Returns the count inserted."""
        ...

    def update_status(self, ws: Any, row_idx: int, new_status: str,
                      also_set_date: bool = True, *,
                      authorized: bool = False, source: str = "unspecified") -> None:
        """Set a row's status (gated for TERMINAL_GATED statuses)."""
        ...

    def update_status_batch(self, ws: Any, updates: list[tuple[int, str]], *,
                           authorized: bool = False, source: str = "unspecified") -> None:
        """Apply many (row_idx, new_status) updates with the same gating."""
        ...

    def update_intent(self, ws: Any, row_idx: int, intent: str, *,
                     source: str = "unspecified") -> None:
        """Set a row's operator-declared intent (not gated)."""
        ...

    def load_statuses(self) -> tuple[dict, Any, Any, int]:
        """Return (statuses_by_key, notfit_counts, goodfit_counts, total_rows)."""
        ...

    def last_write_for_row(self, row_idx: int) -> Optional[dict]:
        """Most recent journal entry for this row_idx, or None."""
        ...
