"""Unit 1 regression tests — travel-regex negation awareness.

Acceptance bar: the two-column test matrix below must pass completely.
MUST NOT match (negated / no-travel phrases) AND
MUST STILL MATCH (real travel phrases — do NOT over-correct into passing travel-kills).

Issue: TRAVEL regex matched "travel required" inside negation e.g.
"No travel required" and "no overnight travel required", causing
recall-001 (Supabase, genuinely no-travel) to be travel_skipped.

Fix: _travel_match() adds a negation-token window check (5 words before
the match start) so negated travel phrases are correctly skipped, while
real travel phrases continue to match.

Run:
  python3 -m pytest tests/test_travel_negation.py -v
"""
from pathlib import Path


from alice.pipeline.daily_delta import _travel_flags, _travel_match  # noqa: E402


# ---------------------------------------------------------------------------
# Column A: MUST NOT match — negated / no-travel phrases
# ---------------------------------------------------------------------------

class TestTravelNegationMustNotMatch:
    """Every case here must return a falsy travel_flag (empty string) from
    _travel_flags().  A match here is a false positive — a no-travel role
    would be incorrectly travel_skipped."""

    def test_no_travel_required_adjacent(self):
        """Classic: negation token immediately before 'travel'."""
        tr, _ = _travel_flags("No travel required")
        assert tr == "", f"false positive: 'No travel required' triggered TRAVEL match {tr!r}"

    def test_no_overnight_travel_required(self):
        """Negation 2 words before 'travel' — must still be caught."""
        tr, _ = _travel_flags("no overnight travel required")
        assert tr == "", f"false positive: 'no overnight travel required' triggered TRAVEL match {tr!r}"

    def test_no_travel_required_in_sentence(self):
        """Negation sentence prefix."""
        tr, _ = _travel_flags("No travel required for this position.")
        assert tr == "", f"false positive: negation sentence triggered TRAVEL match {tr!r}"

    def test_travel_is_not_required(self):
        """Negation token AFTER 'travel' — old regex never matched this form;
        _travel_match must also not match (different structure, 'required' not
        immediately after 'travel')."""
        tr, _ = _travel_flags("travel is not required")
        assert tr == "", f"false positive: 'travel is not required' triggered TRAVEL match {tr!r}"

    def test_zero_travel(self):
        """'zero' is a negation token; 'zero travel' has no percentage, not matched
        by old regex either — confirm no regression."""
        tr, _ = _travel_flags("zero travel")
        assert tr == "", f"false positive: 'zero travel' triggered TRAVEL match {tr!r}"

    def test_travel_none(self):
        """'Travel: none' — not a TRAVEL_RAW pattern; confirm no regression."""
        tr, _ = _travel_flags("Travel: none")
        assert tr == "", f"false positive: 'Travel: none' triggered TRAVEL match {tr!r}"

    def test_no_travel_bare(self):
        """Bare 'no travel' phrase."""
        tr, _ = _travel_flags("no travel")
        assert tr == "", f"false positive: 'no travel' triggered TRAVEL match {tr!r}"

    def test_without_travel(self):
        """'without' is a negation token."""
        tr, _ = _travel_flags("without travel")
        assert tr == "", f"false positive: 'without travel' triggered TRAVEL match {tr!r}"

    def test_no_travel_full_sentence(self):
        """Full sentence as it would appear in a real JD body (the Supabase case)."""
        body = (
            "Fully remote (US eligible). No travel required. "
            "Compensation: $130,000 - $170,000 base + equity."
        )
        tr, _ = _travel_flags(body)
        assert tr == "", (
            f"false positive: Supabase-style 'No travel required' JD body triggered "
            f"TRAVEL match {tr!r} — recall-001 would be incorrectly travel_skipped"
        )


# ---------------------------------------------------------------------------
# Column B: MUST STILL MATCH — real travel phrases (over-correction guard)
# ---------------------------------------------------------------------------

class TestTravelNegationMustStillMatch:
    """Every case here must return a NON-EMPTY travel_flag from _travel_flags().
    A no-match here is a false negative — a real travel role would slip through
    the travel gate and be surfaced to the operator despite the travel requirement."""

    def test_travel_required_standalone(self):
        """Standalone 'travel required' with no negation context."""
        tr, _ = _travel_flags("travel required")
        assert tr, "false negative: 'travel required' did not trigger TRAVEL match"

    def test_travel_extensively(self):
        tr, _ = _travel_flags("travel extensively")
        assert tr, "false negative: 'travel extensively' did not trigger TRAVEL match"

    def test_travel_frequently(self):
        tr, _ = _travel_flags("travel frequently")
        assert tr, "false negative: 'travel frequently' did not trigger TRAVEL match"

    def test_travel_up_to_50_percent(self):
        tr, _ = _travel_flags("travel up to 50%")
        assert tr, "false negative: 'travel up to 50%' did not trigger TRAVEL match"

    def test_30_percent_travel(self):
        tr, _ = _travel_flags("30% travel")
        assert tr, "false negative: '30% travel' did not trigger TRAVEL match"

    def test_must_be_willing_to_travel_up_to_25_percent(self):
        tr, _ = _travel_flags("must be willing to travel up to 25%")
        assert tr, "false negative: 'must be willing to travel up to 25%' did not trigger TRAVEL match"

    def test_travel_required_in_longer_sentence(self):
        """'travel required' embedded in a sentence (role description context)."""
        tr, _ = _travel_flags(
            "This is a field-based role. Travel required to support customer sites."
        )
        assert tr, "false negative: 'Travel required' in sentence did not trigger TRAVEL match"

    def test_travel_extensively_in_sentence(self):
        tr, _ = _travel_flags(
            "Candidates must be comfortable traveling; you will travel extensively "
            "to client and partner sites across the US."
        )
        assert tr, "false negative: 'travel extensively' in sentence did not trigger TRAVEL match"

    def test_travel_frequently_in_sentence(self):
        tr, _ = _travel_flags(
            "In this role you will travel frequently to manufacturing partner locations."
        )
        assert tr, "false negative: 'travel frequently' in sentence did not trigger TRAVEL match"

    def test_10_percent_travel(self):
        tr, _ = _travel_flags("10% travel required")
        assert tr, "false negative: '10% travel' did not trigger TRAVEL match"

    def test_travel_required_preceded_by_unrelated_positive_word(self):
        """'essential travel required' — 'essential' is not a negation token."""
        tr, _ = _travel_flags("essential travel required for this role")
        assert tr, "false negative: 'essential travel required' did not trigger TRAVEL match"

    def test_travel_required_role_prefixed_with_yes(self):
        """The role says 'Yes, travel required' — 'yes' is not a negation token."""
        tr, _ = _travel_flags("Yes, travel required to customer sites.")
        assert tr, "false negative: 'Yes, travel required' did not trigger TRAVEL match"


# ---------------------------------------------------------------------------
# Direct _travel_match tests (the inner helper, not just through _travel_flags)
# ---------------------------------------------------------------------------

class TestTravelMatchHelper:
    """Direct unit tests on _travel_match() to confirm the negation window logic."""

    def test_negation_adjacent(self):
        assert _travel_match("No travel required") is None

    def test_negation_one_word_gap(self):
        assert _travel_match("no overnight travel required") is None

    def test_negation_two_word_gap(self):
        assert _travel_match("no significant overnight travel required") is None

    def test_negation_beyond_5_word_window_does_not_suppress(self):
        """If negation is more than 5 words before 'travel', the match should
        still fire — we do not suppress travel 6+ words after a 'no'."""
        text = "no exception here but this role requires travel required to sites"
        # 'no' is 9 words before 'travel'; window = 5 so it should NOT suppress
        m = _travel_match(text)
        assert m is not None, (
            "negation >5 words before travel should NOT suppress the match"
        )

    def test_without_negation_matches(self):
        assert _travel_match("travel required") is not None
        assert _travel_match("travel up to 25%") is not None
        assert _travel_match("25% travel") is not None


def test_hidden_travel_no_substring_at_false_positive():
    # regression: 'present...(at)' must not match 'at' inside 'that' (killed Flowstate AE)
    from alice.pipeline import source_deep as sd
    assert not sd.HIDDEN_TRAVEL.search("build presentations that help customers")
    assert not sd.HIDDEN_TRAVEL.search("creating dashboards and reports")
    # genuine travel still caught
    assert sd.HIDDEN_TRAVEL.search("you will present at the annual conference")
    assert sd.HIDDEN_TRAVEL.search("represent us at industry events")
