"""Focus list state management — Jordan's stated priority roles for the week.

State at `feedback/focus.json`:
{
  "roles": [{"row_idx": 2, "company": "Acme Industrial", "role": "Enterprise Client Partner",
             "added_at": "2026-05-28T..."}],
  "set_at":          "2026-05-28T...",
  "version_history": [{"ts": "...", "action": "set|add|drop|clear|auto_drop_submit",
                       "role_key": "<company>|<role>", "actor": "operator|alice"}]
}

Max focus size: 5. Auto-drop when a role moves to `submitted` status.
"""
from datetime import datetime
from pathlib import Path
from alice import repo_paths

from alice.persistence import ledger
from alice import safe_state

_FOCUS = Path(repo_paths.FEEDBACK / "focus.json")
_FOCUS_DEFAULT = {"roles": [], "set_at": None, "version_history": []}
MAX_FOCUS = 5


def _load():
    """Read current state under shared lock. Returns the default skeleton
    if the file doesn't exist."""
    return safe_state.atomic_read(_FOCUS, default=dict(_FOCUS_DEFAULT))


def current():
    """Return list of {row_idx, company, role, added_at} for current focus."""
    return _load().get("roles", [])


def is_focus(company, role):
    """Is this (company, role) currently in focus?"""
    needle = f"{company.lower()}|{role.lower()}"
    for r in current():
        if f"{r.get('company','').lower()}|{r.get('role','').lower()}" == needle:
            return True
    return False


def _row_match(substr, rows):
    """Reuse same token-based AND matching as imap_reply for consistency.
    Returns list of (row_idx, row_dict). Imported lazily to avoid circular import."""
    from alice.notify.imap_reply import _match_sheet_row
    return _match_sheet_row(substr, rows)


def set_focus(substrings, actor="operator"):
    """Replace current focus list. substrings = list of company-role substrings.

    Sheet fetch happens outside the state lock; the state mutation is atomic.
    """
    ws = ledger._ws()
    rows = ws.get_all_records()
    new_roles = []
    not_found = []
    ambiguous = []
    for s in substrings[:MAX_FOCUS]:
        hits = _row_match(s, rows)
        if not hits:
            not_found.append(s)
        elif len(hits) > 1:
            ambiguous.append((s, [(i, r.get('company',''), r.get('role','')) for i, r in hits[:3]]))
        else:
            i, r = hits[0]
            new_roles.append({
                "row_idx":  i,
                "company":  r.get("company", ""),
                "role":     r.get("role", ""),
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })

    def mutator(state):
        state = state or dict(_FOCUS_DEFAULT)
        state.setdefault("version_history", [])
        state["roles"] = new_roles
        state["set_at"] = datetime.now().isoformat(timespec="seconds")
        state["version_history"].append({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "action": "set",
            "roles":  [f"{r['company']}|{r['role']}" for r in new_roles],
            "actor":  actor,
        })
        return state, {"set": new_roles, "not_found": not_found, "ambiguous": ambiguous}

    return safe_state.atomic_update(_FOCUS, mutator, default=dict(_FOCUS_DEFAULT))


def add(substr, actor="operator"):
    """Add one role to focus. Sheet fetch outside lock; dedupe + length
    enforcement happen inside the lock (eliminates the TOCTOU window where
    two concurrent adds could both pass the length check)."""
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _row_match(substr, rows)
    if not hits:
        return {"error": f"no match for {substr!r}"}
    if len(hits) > 1:
        return {"error": f"ambiguous ({len(hits)} matches): {substr!r}",
                "candidates": [(i, r.get('company',''), r.get('role','')) for i, r in hits[:5]]}
    i, r = hits[0]
    entry = {
        "row_idx":  i,
        "company":  r.get("company", ""),
        "role":     r.get("role", ""),
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }

    def mutator(state):
        state = state or dict(_FOCUS_DEFAULT)
        state.setdefault("roles", [])
        state.setdefault("version_history", [])
 # Dedupe under lock — no TOCTOU race against a concurrent add.
        for existing in state["roles"]:
            if existing.get("row_idx") == i:
                return state, {"already": True, "company": r.get("company"), "role": r.get("role")}
        if len(state["roles"]) >= MAX_FOCUS:
            return state, {"error": f"focus list full ({MAX_FOCUS}); drop one first"}
        state["roles"].append(entry)
        state["version_history"].append({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "action": "add",
            "role":   f"{entry['company']}|{entry['role']}",
            "actor":  actor,
        })
        return state, {"added": entry}

    return safe_state.atomic_update(_FOCUS, mutator, default=dict(_FOCUS_DEFAULT))


def drop(substr, actor="operator"):
    needle = substr.lower()

    def mutator(state):
        state = state or dict(_FOCUS_DEFAULT)
        state.setdefault("roles", [])
        state.setdefault("version_history", [])
        kept = []
        dropped = None
        for r in state["roles"]:
            hay = f"{r.get('company','')} {r.get('role','')}".lower()
            if dropped is None and needle in hay:
                dropped = r
            else:
                kept.append(r)
        if dropped is None:
            return state, {"error": f"no focus role matches {substr!r}"}
        state["roles"] = kept
        state["version_history"].append({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "action": "drop",
            "role":   f"{dropped.get('company')}|{dropped.get('role')}",
            "actor":  actor,
        })
        return state, {"dropped": dropped}

    return safe_state.atomic_update(_FOCUS, mutator, default=dict(_FOCUS_DEFAULT))


def clear(actor="operator"):
    def mutator(state):
        state = state or dict(_FOCUS_DEFAULT)
        state.setdefault("version_history", [])
        state["roles"] = []
        state["set_at"] = None
        state["version_history"].append({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "action": "clear",
            "actor":  actor,
        })
        return state, {"cleared": True}

    return safe_state.atomic_update(_FOCUS, mutator, default=dict(_FOCUS_DEFAULT))


def auto_drop_submitted():
    """Called by cron — check sheet for focus roles that have moved to `submitted`
    (or any terminal state) and auto-drop them.

    Guard: if the triggering status was written by Alice WITHOUT Jordan
    authorization (per ledger.last_write_for_row), skip the drop and flag for
    review. This prevents the cascade where Alice writes 'submitted' herself,
    drops the role from focus based on her own write, then reports it as fact.

    Side effect: also returns `skipped_unauthorized` so the caller can notify
    Jordan.

    Sheet fetch + last_write lookup happen outside the state lock; only the
    state update is atomic. skip_write_if_unchanged=True preserves the
    no-noop-mtime-bump discipline.
    """
 # Snapshot to decide whether there's any work at all (cheap fast path).
    initial = _load()
    if not initial.get("roles"):
        return {"dropped": [], "skipped_unauthorized": []}

    ws = ledger._ws()
    rows = ws.get_all_records()
    rows_by_idx = {i: r for i, r in enumerate(rows, start=2)}

    TERMINAL = {"submitted", "first screen scheduled", "interviewing",
                "offer", "negotiating", "closed", "not a fit"}

 # Pre-compute the auth check per role (calls ledger; outside lock).
    role_decisions = []  # list of (role, status, action, audit_payload)
    for r in initial["roles"]:
        idx = r.get("row_idx")
        sheet_row = rows_by_idx.get(idx, {})
        status = (sheet_row.get("status") or "").strip().lower()
        if status not in TERMINAL:
            role_decisions.append((r, status, "keep", None))
            continue
        last_write = ledger.last_write_for_row(idx)
        status_was_authorized = True
        if last_write and (last_write.get("status") or "").strip().lower() == status:
            status_was_authorized = bool(last_write.get("authorized"))
        if not status_was_authorized:
            role_decisions.append((r, status, "skipped_unauthorized", {
                "trigger_status":    status,
                "last_write_source": (last_write or {}).get("source"),
                "last_write_ts":     (last_write or {}).get("ts"),
            }))
        else:
            role_decisions.append((r, status, "drop", None))

    def mutator(state):
        state = state or dict(_FOCUS_DEFAULT)
        state.setdefault("roles", [])
        state.setdefault("version_history", [])
 # The set of focus roles may have shifted between initial read and
 # lock acquisition (e.g. Jordan ran `focus add` concurrently). Filter
 # decisions to roles still present.
        present = {r.get("row_idx") for r in state["roles"]}
        kept = []
        dropped = []
        skipped_unauthorized = []
        for r, status, action, audit in role_decisions:
            if r.get("row_idx") not in present:
                continue  # already dropped by someone else
            if action == "keep":
                kept.append(r)
            elif action == "skipped_unauthorized":
                kept.append(r)
                skipped_unauthorized.append({**r, **audit})
                state["version_history"].append({
                    "ts":             datetime.now().isoformat(timespec="seconds"),
                    "action":         "auto_drop_skipped_unauthorized",
                    "role":           f"{r.get('company')}|{r.get('role')}",
                    "trigger_status": status,
                    "actor":          "alice",
                })
            elif action == "drop":
                dropped.append({**r, "auto_dropped_at_status": status})
                state["version_history"].append({
                    "ts":             datetime.now().isoformat(timespec="seconds"),
                    "action":         "auto_drop",
                    "role":           f"{r.get('company')}|{r.get('role')}",
                    "trigger_status": status,
                    "actor":          "alice",
                })
 # Add any roles that appeared after initial snapshot (concurrent add)
 # back to the kept list unchanged.
        seen_idx = {x.get("row_idx") for x in kept} | {d.get("row_idx") for d in dropped}
        for r in state["roles"]:
            if r.get("row_idx") not in seen_idx:
                kept.append(r)
        state["roles"] = kept
        return state, {"dropped": dropped, "skipped_unauthorized": skipped_unauthorized}

    return safe_state.atomic_update(
        _FOCUS, mutator,
        default=dict(_FOCUS_DEFAULT),
        skip_write_if_unchanged=True,
    )


def status_for_focus():
    """For digest rendering: return list of focus roles + current sheet status + next-move hint."""
    state = _load()
    if not state.get("roles"):
        return []
    ws = ledger._ws()
    rows = ws.get_all_records()
    rows_by_idx = {i: r for i, r in enumerate(rows, start=2)}
    out = []
    for r in state["roles"]:
        idx = r.get("row_idx")
        sheet_row = rows_by_idx.get(idx, {})
        status = (sheet_row.get("status") or "new").strip().lower()
        days_since_change = _days_since_status_change(sheet_row)
        out.append({
            "row_idx":           idx,
            "company":           r.get("company"),
            "role":              r.get("role"),
            "status":            status,
            "intent":            (sheet_row.get("intent") or "").strip().lower(),
            "days_at_status":    days_since_change,
            "next_move":         _next_move(status, days_since_change),
            "added_to_focus_at": r.get("added_at"),
        })
    return out


def _days_since_status_change(sheet_row):
    """Days since status_changed_date. Returns None if column not present or unparseable."""
    from datetime import date as _date
    raw = sheet_row.get("status_changed_date", "")
    if not raw:
        return None
    try:
 # ISO date string yyyy-mm-dd
        d = _date.fromisoformat(raw.strip().split("T")[0])
        return (_date.today() - d).days
    except Exception as _e:
        try:
            import obs; obs.capture(_e, where="focus:_days_since_status_change", payload={"raw": raw})
        except Exception:
            pass
        return None


def _next_move(status, days):
    """Human-readable next-move hint for a focus role."""
    moves = {
        "new":                    "review fit; label good fit or not a fit",
        "good fit":               "trigger prep with 'prep: <company>'",
        "materials pending":      "Alice is drafting; expect digest tomorrow",
        "submitted":              "auto-drops from focus on next cron",
        "first screen scheduled": "Alice will produce prep doc + morning reminder",
        "interviewing":           "after each round, send 'debrief: <company>'",
        "offer":                  "Alice will produce negotiation prep",
        "negotiating":            "decision pending",
        "closed":                 "auto-drops from focus on next cron",
        "not a fit":              "auto-drops from focus on next cron",
    }
    return moves.get(status, "review on sheet")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        for r in status_for_focus():
            print(f"  row {r['row_idx']}: {r['company']} - {r['role']}")
            print(f"    status: {r['status']} ({r['days_at_status']} days)")
            print(f"    next move: {r['next_move']}")
        if not status_for_focus():
            print("  (focus list is empty)")
