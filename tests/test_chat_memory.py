"""
tests/test_chat_memory.py
─────────────────────────
Offline tests for chat-memory context assembly.

Three assertions:

  (a) MEMORY RESTORED — a Fictiv-shaped exchange where the entity is named
      once early ("Fictiv"), then referenced only by pronoun for 10+ turns.
      The assembled context at the FINAL turn must still contain "Fictiv".

  (b) CONTAMINATION GUARD INTACT — a turn containing a now-superseded
      "focus is empty" state-claim is still DROPPED by the supersession
      filter even though Alice's turns are re-included. The supersession
      guard must fire regardless.

  (c) CONVERSATION BOUNDARY — a turn from BEFORE a >30-min idle gap is NOT
      loaded; turns after the gap ARE loaded.

All tests run against the real functions imported from scripts/telegram_bot.py.
No live files are written.  focus.json reads are intercepted via a PatchedPath
subclass; history paths are redirected to tmp_path files via _HISTORY_PATH
monkeypatch on the module.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from alice import repo_paths

from alice.notify import telegram_bot as tb  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_history(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _patched_focus_path_class(focus_json: str):
    """Return a PatchedPath subclass that intercepts reads of focus.json and
    returns focus_json without touching the real file.  All other Path
    operations pass through to the real implementation.
    """
    _FOCUS = str(repo_paths.FEEDBACK / "focus.json")

    class PatchedPath(type(Path("."))):
        def exists(self):
            if str(self) == _FOCUS:
                return True
            return super().exists()

        def read_text(self, *a, **kw):
            if str(self) == _FOCUS:
                return focus_json
            return super().read_text(*a, **kw)

    return PatchedPath


# ── (a) MEMORY RESTORED ──────────────────────────────────────────────────────

def test_entity_survives_full_conversation(tmp_path):
    """An entity named once ("Fictiv") in an early turn must still appear in
    the assembled conversation context after 10+ subsequent turns that only
    reference it by pronoun.

    This is the exact failure from the real transcript: n=8 + alice-stripped
    caused the company name to scroll out of context entirely.
    """
    now = datetime(2026, 5, 31, 10, 0, 0)
    records = []

    # Turn 0 (operator): entity introduced by name
    records.append({"ts": _ts(now), "role": "user",
                    "text": "I want to apply to Fictiv. They do on-demand manufacturing."})
    now += timedelta(minutes=1)

    # Turn 1 (Alice): acknowledges, repeats entity
    records.append({"ts": _ts(now), "role": "alice",
                    "text": "Got it — Fictiv, on-demand manufacturing. I'll prep materials."})
    now += timedelta(minutes=1)

    # Turns 2-21: 10 operator + 10 Alice exchanges using only pronouns
    pronoun_pairs = [
        ("What track is this role?", "This role fits Track 4 — applied AI."),
        ("Does it need a cover letter?", "Yes, it does."),
        ("Which resume for it?", "Use the operator-builder variant for it."),
        ("Is the comp band right?", "The comp band looks right."),
        ("Any travel concerns for it?", "No travel concerns — fully remote."),
        ("What's the deadline?", "No hard deadline listed."),
        ("Should I reach out directly?", "Yes, reach out to the hiring manager."),
        ("Who's the hiring manager?", "Not listed publicly."),
        ("Draft the cover letter please.", "Drafting now."),
        ("Can you check the status?", "Status is not yet submitted."),
    ]
    for operator_msg, alice_msg in pronoun_pairs:
        records.append({"ts": _ts(now), "role": "user", "text": operator_msg})
        now += timedelta(minutes=1)
        records.append({"ts": _ts(now), "role": "alice", "text": alice_msg})
        now += timedelta(minutes=1)

    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, records)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    # Simulate the injection-site assembly (same logic as _route_message)
    assembled_lines = []
    for h in conv:
        role = h.get("role", "")
        text = (h.get("text") or "").strip()
        if role == "__truncated__":
            assembled_lines.append(text)
        elif tb._history_turn_is_superseded(h):
            pass  # dropped by guard
        elif role == "user":
            assembled_lines.append(f"Jordan Avery: {text[:600]}")
        elif role == "alice":
            assembled_lines.append(f"Alice: {text[:600]}")
    assembled = "\n\n".join(assembled_lines)

    assert "Fictiv" in assembled, (
        f"Expected 'Fictiv' in assembled context after 22-turn conversation.\n"
        f"Assembled ({len(assembled)} chars):\n{assembled[:1000]}"
    )


def test_both_roles_present_in_assembled_context(tmp_path):
    """Both the operator's AND Alice's turns must appear in the assembled context.
    The old bug stripped Alice's turns entirely.
    """
    now = datetime(2026, 5, 31, 10, 0, 0)
    records = [
        {"ts": _ts(now), "role": "user",
         "text": "What's my status on Fictiv?"},
        {"ts": _ts(now + timedelta(minutes=1)), "role": "alice",
         "text": "Fictiv is in your pipeline, not yet submitted."},
    ]
    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, records)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    roles_loaded = {r.get("role") for r in conv}
    assert "user" in roles_loaded, "The operator's turns must be included"
    assert "alice" in roles_loaded, "Alice's turns must be included (not stripped)"


# ── (b) CONTAMINATION GUARD INTACT ───────────────────────────────────────────

def test_superseded_focus_empty_claim_dropped():
    """A turn containing 'focus is empty' must be dropped by
    _history_turn_is_superseded() when focus.json was updated after the claim
    — even though Alice's turns are now re-included.

    focus.json is intercepted via PatchedPath; the real file is never touched.
    """
    claim_time = datetime(2026, 5, 31, 9, 0, 0)
    focus_set_time = datetime(2026, 5, 31, 9, 30, 0)  # 30 min later — supersedes

    focus_json = json.dumps({
        "roles": ["Fictiv Enterprise AE"],
        "set_at": focus_set_time.isoformat(),
    })

    stale_turn = {
        "ts": _ts(claim_time),
        "role": "alice",
        "text": "Your focus is empty right now — you haven't set any priority roles.",
    }

    PatchedPath = _patched_focus_path_class(focus_json)
    with patch("alice.notify.telegram_bot.Path", PatchedPath):
        result = tb._history_turn_is_superseded(stale_turn)

    assert result is True, (
        "Expected _history_turn_is_superseded() to return True for 'focus is empty' "
        f"claim superseded by a later focus.json write. Got: {result}"
    )


def test_superseded_focus_claim_via_text_is_superseded_direct():
    """Direct test of _text_is_superseded: confirms the core guard fires for
    'focus is empty' when focus.json has set_at AFTER the claim and has roles.
    """
    claim_time = datetime(2026, 5, 30, 8, 0, 0)
    focus_set_time = datetime(2026, 5, 30, 10, 0, 0)  # 2 hours later

    focus_json = json.dumps({
        "roles": ["Meridian Labs Operations AE"],
        "set_at": focus_set_time.isoformat(),
    })

    PatchedPath = _patched_focus_path_class(focus_json)
    with patch("alice.notify.telegram_bot.Path", PatchedPath):
        # Matches "focus is empty" pattern — should be superseded
        result_empty = tb._text_is_superseded(
            "Your focus list is currently empty.", claim_time
        )
        # Does NOT match — should NOT be superseded
        result_neutral = tb._text_is_superseded(
            "Your focus list has Meridian Labs in it.", claim_time
        )

    assert result_empty is True, f"Expected True for empty-focus claim. Got: {result_empty}"
    assert result_neutral is False, f"Expected False for non-empty statement. Got: {result_neutral}"


def test_guard_does_not_fire_when_focus_not_yet_set():
    """If focus.json does NOT have a set_at that's after the claim, the guard
    must NOT fire (no false positives — don't drop valid context).
    """
    claim_time = datetime(2026, 5, 31, 12, 0, 0)
    focus_set_time = datetime(2026, 5, 31, 11, 0, 0)  # BEFORE the claim — not superseded

    focus_json = json.dumps({
        "roles": ["Fictiv Enterprise AE"],
        "set_at": focus_set_time.isoformat(),
    })

    PatchedPath = _patched_focus_path_class(focus_json)
    with patch("alice.notify.telegram_bot.Path", PatchedPath):
        result = tb._text_is_superseded(
            "Your focus is empty.", claim_time
        )

    assert result is False, (
        "Guard must NOT fire when focus.json set_at is BEFORE the claim. "
        f"Got: {result}"
    )


def test_non_superseded_alice_turn_kept():
    """An Alice turn that does NOT contain a superseded claim must NOT be
    dropped — it should pass through so conversational continuity is preserved.
    """
    alice_turn = {
        "ts": _ts(datetime(2026, 5, 31, 10, 0, 0)),
        "role": "alice",
        "text": "Fictiv looks like a strong fit for Track 4. Cover letter is ready.",
    }
    result = tb._history_turn_is_superseded(alice_turn)
    assert result is False, (
        f"Expected a benign Alice turn to NOT be marked superseded. Got: {result}"
    )


# ── (c) CONVERSATION BOUNDARY ─────────────────────────────────────────────────

def test_prior_conversation_not_loaded(tmp_path):
    """Turns from BEFORE a >30-min idle gap must not appear in the loaded
    conversation; turns after the gap must all appear.
    """
    base = datetime(2026, 5, 31, 8, 0, 0)

    prior = [
        {"ts": _ts(base + timedelta(minutes=0)), "role": "user",
         "text": "OLD CONVERSATION turn 1"},
        {"ts": _ts(base + timedelta(minutes=2)), "role": "alice",
         "text": "OLD CONVERSATION turn 2"},
        {"ts": _ts(base + timedelta(minutes=4)), "role": "user",
         "text": "OLD CONVERSATION turn 3"},
    ]

    # 45-minute gap — well above the 30-min threshold
    gap_end = base + timedelta(minutes=4 + 45)

    current = [
        {"ts": _ts(gap_end + timedelta(minutes=0)), "role": "user",
         "text": "NEW CONVERSATION turn 1"},
        {"ts": _ts(gap_end + timedelta(minutes=2)), "role": "alice",
         "text": "NEW CONVERSATION turn 2"},
        {"ts": _ts(gap_end + timedelta(minutes=4)), "role": "user",
         "text": "NEW CONVERSATION turn 3"},
    ]

    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, prior + current)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    combined = " ".join(r.get("text", "") for r in conv)

    for label in ("NEW CONVERSATION turn 1", "NEW CONVERSATION turn 2",
                  "NEW CONVERSATION turn 3"):
        assert label in combined, f"Current conv turn missing: {label!r}"

    for label in ("OLD CONVERSATION turn 1", "OLD CONVERSATION turn 2",
                  "OLD CONVERSATION turn 3"):
        assert label not in combined, f"Prior conv turn leaked into current: {label!r}"


def test_conversation_within_gap_all_loaded(tmp_path):
    """All turns of a single conversation (no gap > 30 min) must be loaded —
    specifically more than the old n=8 window would have allowed.
    """
    base = datetime(2026, 5, 31, 10, 0, 0)
    records = [
        {"ts": _ts(base + timedelta(minutes=i * 2)),
         "role": "user" if i % 2 == 0 else "alice",
         "text": f"Turn {i} content"}
        for i in range(20)  # 20 turns — old n=8 would have cut this to 8
    ]

    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, records)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    loaded_texts = {r.get("text") for r in conv}
    for i in range(20):
        assert f"Turn {i} content" in loaded_texts, (
            f"Turn {i} missing — all 20 turns should be loaded (old n=8 limit removed)"
        )


def test_boundary_exactly_at_30_min_not_split(tmp_path):
    """A gap of exactly 30 minutes (= threshold) should NOT split conversations
    — the boundary is STRICTLY greater than 30 minutes.
    """
    base = datetime(2026, 5, 31, 10, 0, 0)
    records = [
        {"ts": _ts(base), "role": "user", "text": "First turn"},
        # exactly 30 min gap — NOT a boundary (must be > 30 min to split)
        {"ts": _ts(base + timedelta(minutes=30)), "role": "alice",
         "text": "Second turn"},
        {"ts": _ts(base + timedelta(minutes=32)), "role": "user",
         "text": "Third turn"},
    ]

    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, records)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    texts = {r.get("text") for r in conv}
    assert "First turn" in texts, "First turn must be included when gap == 30 min exactly"
    assert "Second turn" in texts
    assert "Third turn" in texts


def test_token_cap_truncation_marker(tmp_path):
    """When a conversation exceeds the token cap, oldest turns are dropped and
    a '[earlier conversation truncated]' sentinel is prepended.
    The most-recent turn must always survive.
    """
    base = datetime(2026, 5, 31, 10, 0, 0)
    # 20 turns × ~5000 chars = ~100K chars total
    # cap = 20_000 tokens × 3.5 chars/token = 70K char budget  →  truncation fires
    records = [
        {"ts": _ts(base + timedelta(minutes=i)),
         "role": "user" if i % 2 == 0 else "alice",
         "text": f"TURN_{i}: " + ("x" * 4990)}
        for i in range(20)
    ]

    hp = tmp_path / "telegram-history.jsonl"
    _write_history(hp, records)

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    assert conv[0]["role"] == "__truncated__", (
        "Truncation sentinel must be FIRST (oldest position) when cap exceeded"
    )
    assert "[earlier conversation truncated]" in conv[0]["text"]

    all_texts = " ".join(r.get("text", "") for r in conv)
    assert "TURN_19" in all_texts, "Most recent turn must survive truncation"


def test_empty_history_returns_empty(tmp_path):
    """_load_current_conversation must return [] gracefully when there is no
    history file.
    """
    hp = tmp_path / "nonexistent.jsonl"  # does not exist

    original = tb._HISTORY_PATH
    tb._HISTORY_PATH = hp
    try:
        conv = tb._load_current_conversation(max_tokens=20_000)
    finally:
        tb._HISTORY_PATH = original

    assert conv == [], f"Expected [], got {conv}"
