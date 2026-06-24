"""mark_role_status must not set a terminal/irreversible status directly
from chat — a misparse there can write a wrong 'submitted' and drop a role
from focus. Terminal statuses are refused before any sheet access;
non-terminal statuses still pass through to the write path.

Offline and hermetic: monkeypatches ledger so no live sheet is touched.
"""
import sys
from pathlib import Path
from alice import tools
from alice.persistence import ledger


# Only statuses that are BOTH terminal-gated AND settable via this tool's enum.
_TERMINAL_AND_CANONICAL = sorted(ledger.TERMINAL_GATED & tools._CANONICAL_STATUSES)


def test_terminal_statuses_refused_without_touching_the_sheet(monkeypatch):
    # If a terminal status reaches the sheet path, this boom fires — proving the
    # refuse happens BEFORE any sheet access (parse-independent, no live state).
    def _boom():
        raise AssertionError("ledger.available() reached — a terminal status "
                             "touched the sheet path (refuse is too late)")
    monkeypatch.setattr(ledger, "available", _boom)

    assert _TERMINAL_AND_CANONICAL, "expected some terminal statuses in the tool enum"
    for status in _TERMINAL_AND_CANONICAL:
        r = tools._mark_role_status(
            {"company_substring": "northwind enterprise", "status": status})
        assert r.get("ok") is False, f"{status!r}: expected refuse, got {r!r}"
        assert r.get("error") == "terminal_status_needs_confirmation", \
            f"{status!r}: wrong error: {r!r}"
        # The message must point to the working path (not a dead end).
        assert "confirmed path" in r.get("detail", "") or "confirm" in r.get("detail", "")


def test_nonterminal_status_passes_the_terminal_gate(monkeypatch):
    # Non-terminal must get PAST the terminal gate. Mock the sheet as unavailable
    # so it raises at the availability check — proving it was NOT terminal-blocked
    # (a terminal status would have returned the refuse before reaching here).
    monkeypatch.setattr(ledger, "available", lambda: False)
    for status in ["good fit", "materials pending"]:
        try:
            tools._mark_role_status({"company_substring": "x", "status": status})
            raise AssertionError(f"{status!r}: expected RuntimeError (ledger "
                                 "unavailable) — i.e. it reached the sheet path")
        except RuntimeError as e:
            assert "not available" in str(e), f"{status!r}: unexpected: {e}"


def test_canonical_validation_still_fires_first():
    # A non-canonical status is still rejected up front (unchanged behavior).
    try:
        tools._mark_role_status({"company_substring": "x", "status": "sent"})
        raise AssertionError("expected ValueError for non-canonical 'sent'")
    except ValueError as e:
        assert "not canonical" in str(e)
