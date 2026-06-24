"""Failing-direction tests for the two-layer A/B disambiguation detector.

A multi-option question that the detector misreads as a single yes/no leads
the assistant to silently default to the first option and act, so the
detector is biased toward flagging ambiguity. These tests cover both layers
separately and combined.

  Layer 1 (regex) — _is_ab_question(q, use_semantic_backstop=False):
    Catches comma-disjunctions, " or <option-word>", either/or, bulleted
    lists, numbered lists. Deterministic — these tests do NOT call Haiku.

  Layer 2 (semantic backstop) — _is_ab_question(q) with default True:
    For questions Layer 1 does NOT flag, _is_ab_question_semantic() asks
    Haiku (biased toward AMBIGUOUS). Tests marked LIVE call the real API.

Anchor extension (_last_assistant_question):
  Bulleted/numbered list questions are returned as the FULL list-shaped
  block (intro through last "?"), not just the trailing sentence. Verified
  with a synthetic history fixture.

Run: python3 scripts/harness/check_ab_disambiguation.py
     python3 scripts/harness/check_ab_disambiguation.py --no-live   # skip Haiku
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

from alice.notify import telegram_bot as tb  # noqa: E402


# ─── Layer 1: regex-only (use_semantic_backstop=False) ──────────────────────

REGEX_CASES = [
    # The shapes the regex MUST catch deterministically.
    ("regex-comma",
     "Want me to start prep, or keep it in focus?",
     True,
     "comma-disjunction is the canonical A/B shape"),
    ("regex-or-optionword",
     "should I prep Northwind Systems or wait on it",
     True,
     "' or wait' triggers the option-word pattern"),
    ("regex-bulleted",
     "Are you asking whether to:\n- Start prep?\n- Drop from focus?\n- Something else?",
     True,
     "bulleted list of 3 items — the shape regex-only missed"),
    ("regex-numbered-dot",
     "Which do you prefer?\n1. Queue prep now\n2. Wait for more info",
     True,
     "numbered list with '.' separator"),
    ("regex-numbered-paren",
     "Pick one:\n1) Option Alpha\n2) Option Beta",
     True,
     "numbered list with ')' separator"),
    ("regex-bullets-stars",
     "What next?\n* Apply to Northwind Systems\n* Defer Northwind Systems",
     True,
     "bulleted list with '*' marker"),
    ("regex-either-or",
     "Should we either ship now or wait for review?",
     True,
     "either/or construction"),
    # The shapes the regex MUST NOT flag (single yes/no proposals).
    ("regex-clean-yesno",
     "Want me to start prep on Northwind Systems?",
     False,
     "single yes/no proposal — must not over-fire"),
    ("regex-confirm",
     "Confirm you want me to add Watershed to focus?",
     False,
     "confirmation phrasing — cleanly yes/no"),
    ("regex-single-question",
     "Should I proceed?",
     False,
     "minimal yes/no — must not over-fire"),
]


def run_regex_layer() -> tuple[int, int]:
    """Layer 1 unit tests. No Haiku call — use_semantic_backstop=False
    isolates the regex behavior so a backstop hiccup never masks a
    regex regression."""
    print("\n=== Layer 1: regex-only ===")
    passed = 0
    for name, q, expected, why in REGEX_CASES:
        got = tb._is_ab_question(q, use_semantic_backstop=False)
        status = "PASS" if got == expected else "FAIL"
        marker = "✓" if got == expected else "✗"
        print(f"  [{status}] {marker} {name}: expected={expected} got={got}")
        print(f"         q: {q!r}")
        print(f"         why: {why}")
        if got == expected:
            passed += 1
    print(f"\n  Layer 1 summary: {passed}/{len(REGEX_CASES)} passed")
    return passed, len(REGEX_CASES)


# ─── Layer 2: semantic backstop (live Haiku) ────────────────────────────────

SEMANTIC_CASES = [
    # Backstop-must-catch — semantic multi-option that regex did NOT flag:
    # two role names, no comma, no option-word after "or" (Northwind
    # Systems isn't in the option-word list).
    ("semantic-3-roles-no-comma",
     "Do you want me to start Lumen Search Meridian or Northwind Systems",
     True,
     "two distinct role names — regex doesn't see this (no comma, no "
     "option-word after 'or'); backstop must flag AMBIGUOUS"),
    # Backstop-must-NOT-fire — cleanly single yes/no.
    ("semantic-clean-confirm",
     "Want me to start prep on Northwind Systems?",
     False,
     "single proposal, clearly yes/no — backstop must return CLEAR"),
    ("semantic-clean-ready",
     "Ready for me to send the outreach draft?",
     False,
     "single proposal — backstop must return CLEAR"),
]


def run_semantic_layer() -> tuple[int, int]:
    """Layer 2 live Haiku tests. Each case calls the real API via
    _is_ab_question_semantic. Expects ANTHROPIC_API_KEY available in
    config.env."""
    print("\n=== Layer 2: semantic backstop (LIVE Haiku) ===")
    passed = 0
    for name, q, expected, why in SEMANTIC_CASES:
        got = tb._is_ab_question_semantic(q)
        status = "PASS" if got == expected else "FAIL"
        marker = "✓" if got == expected else "✗"
        print(f"  [{status}] {marker} {name}: expected={expected} got={got}")
        print(f"         q: {q!r}")
        print(f"         why: {why}")
        if got == expected:
            passed += 1
    print(f"\n  Layer 2 summary: {passed}/{len(SEMANTIC_CASES)} passed")
    return passed, len(SEMANTIC_CASES)


# ─── Combined: full _is_ab_question (regex + backstop) ──────────────────────

COMBINED_CASES = [
    # Each case must end up as ambiguous=True except the clean yes/no.
    ("combined-bulleted",
     "Are you asking whether to:\n- Start prep?\n- Drop from focus?\n- Something else?",
     True),
    ("combined-numbered",
     "Which do you prefer?\n1. Queue prep now\n2. Wait for more info",
     True),
    ("combined-prose-multi-no-comma",
     "should I prep Northwind Systems or wait on it",
     True),
    ("combined-comma-disjunction",
     "Want me to start prep, or keep it in focus?",
     True),
    ("combined-clean-yesno",
     "Want me to start prep on Northwind Systems?",
     False),
    ("combined-3-roles",
     "Do you want me to start Lumen Search Meridian or Northwind Systems",
     True),
]


def run_combined_layer() -> tuple[int, int]:
    """Combined regex + semantic backstop. The end-to-end behavior the
    chat path actually sees."""
    print("\n=== Combined: regex + semantic backstop (the integrated detector) ===")
    passed = 0
    for name, q, expected in COMBINED_CASES:
        got = tb._is_ab_question(q)
        status = "PASS" if got == expected else "FAIL"
        marker = "✓" if got == expected else "✗"
        print(f"  [{status}] {marker} {name}: expected={expected} got={got}")
        print(f"         q: {q!r}")
        if got == expected:
            passed += 1
    print(f"\n  Combined summary: {passed}/{len(COMBINED_CASES)} passed")
    return passed, len(COMBINED_CASES)


# ─── Anchor: _last_assistant_question captures bulleted blocks ──────────────

def run_anchor_capture() -> tuple[int, int]:
    """Verify _last_assistant_question returns the FULL bulleted question
    block, not just the trailing sentence — a single-sentence extraction
    would hide the multi-option structure from _is_ab_question.

    Uses monkey-patched _load_history to inject a synthetic last-alice
    turn — no I/O, no daemon, no chat path."""
    print("\n=== Anchor: _last_assistant_question captures bulleted blocks ===")
    original_load_history = tb._load_history
    passed = 0
    cases = [
        ("anchor-bulleted",
         "OK, hearing you on Northwind Systems. Are you asking whether to:\n"
         "- Start prep?\n"
         "- Drop from focus?\n"
         "- Something else?",
         True,
         "captured block must contain at least one bullet line"),
        ("anchor-numbered",
         "Which path?\n"
         "1. Queue prep now\n"
         "2. Wait for the recruiter ping",
         True,
         "captured block must contain a numbered item; '?' on intro line"),
        ("anchor-single-sentence",
         "Some prior context. Want me to start prep on Northwind Systems?",
         False,
         "single-sentence question — bullets must NOT appear in capture"),
    ]
    for name, alice_text, expect_bullets, why in cases:
        # Inject a synthetic history with one alice turn that ends with
        # the question shape under test.
        tb._load_history = lambda n=12, _t=alice_text: [
            {"role": "user", "text": "earlier candidate turn", "ts": "t1"},
            {"role": "alice", "text": _t, "ts": "t2"},
        ]
        captured = tb._last_assistant_question()
        has_bullets = captured is not None and (
            "- " in captured or "* " in captured
            or "1." in captured or "1)" in captured
            or "2." in captured or "2)" in captured
        )
        ok = (has_bullets == expect_bullets)
        status = "PASS" if ok else "FAIL"
        marker = "✓" if ok else "✗"
        print(f"  [{status}] {marker} {name}: expect_bullets={expect_bullets} "
              f"got_bullets={has_bullets}")
        print(f"         captured: {captured!r}")
        print(f"         why: {why}")
        if ok:
            passed += 1
    tb._load_history = original_load_history
    print(f"\n  Anchor summary: {passed}/{len(cases)} passed")
    return passed, len(cases)


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-live", action="store_true",
        help="Skip the Layer 2 live-Haiku tests (regex + anchor still run).",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("A/B DISAMBIGUATION — failing-direction tests")
    print("Fail-safe bias: detector must err toward AMBIGUOUS, never toward")
    print("'guess and act'. False-positive = mild annoyance; false-negative")
    print("= the dangerous bug class.")
    print("=" * 70)

    r1_pass, r1_total = run_regex_layer()
    a_pass, a_total = run_anchor_capture()

    if args.no_live:
        print("\n(skipping Layer 2 and combined live-Haiku tests per --no-live)")
        l2_pass = l2_total = 0
        c_pass = c_total = 0
    else:
        l2_pass, l2_total = run_semantic_layer()
        c_pass, c_total = run_combined_layer()

    total_pass = r1_pass + a_pass + l2_pass + c_pass
    total = r1_total + a_total + l2_total + c_total

    print("\n" + "=" * 70)
    print(f"FINAL: {total_pass}/{total} passed")
    print(f"  Layer 1 (regex):           {r1_pass}/{r1_total}")
    print(f"  Anchor (block capture):    {a_pass}/{a_total}")
    if not args.no_live:
        print(f"  Layer 2 (semantic Haiku):  {l2_pass}/{l2_total}")
        print(f"  Combined (end-to-end):     {c_pass}/{c_total}")
    print("=" * 70)
    return 0 if total_pass == total else 1


if __name__ == "__main__":
    sys.exit(main())
