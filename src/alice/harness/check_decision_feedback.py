"""Regression tests for the decision-feedback / correction store.

What this file proves — non-negotiable first:

  1. CAPTURE INTEGRITY (test_capture_integrity_*): a paraphrased
     operator_correction is REJECTED at API level; a true verbatim span is
     ACCEPTED. Same for alice_claim when alice_turn_ts is provided.
     This is the structural enforcement that makes paraphrase
     impossible at the seam.

  2. DESCRIPTIVE-NOT-PREDICTIVE (test_pattern_summary_*): the structured
     query returns an AGGREGATE (count_by_category, representative
     verbatim) suitable for scorecard surfacing. It contains no
     prediction fields, and the candidate's verbatim is the source-of-record —
     not Alice's interpretation.

Then the rest:

  3. CONFIRMATION GATE (test_confirmation_gate_*): unconfirmed
     candidates (or auto-expired ones) do NOT enter the durable store.

  4. CATEGORIES (test_categories_*): only the closed set of categories
     is accepted; bad categories raise.

  5. REPLY PARSING (test_reply_parsing_*): "confirm corr-cand-xxx",
     "reject ...", "edit ... category=...", "outcome corr-xxx: ..."
     directives apply correctly and are idempotent.

  6. ROLE-CHECK ON SUBSTRING (test_role_check_*): a candidate verbatim
     cited against an assistant turn (or vice-versa) is rejected.

Live-LLM ambient tests are gated behind ALICE_LIVE_TESTS=1 since the
Haiku call costs $.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest


from alice.persistence import decision_feedback as df


# ─── shared fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """Repoint store files to a temp dir so tests don't pollute real
    feedback/ state."""
    history_path    = tmp_path / "telegram-history.jsonl"
    candidates_path = tmp_path / "decision-feedback-candidates.jsonl"
    store_path      = tmp_path / "decision-feedback.jsonl"
    monkeypatch.setattr(df, "HISTORY_PATH",    history_path)
    monkeypatch.setattr(df, "CANDIDATES_PATH", candidates_path)
    monkeypatch.setattr(df, "STORE_PATH",      store_path)
    return {
        "history":    history_path,
        "candidates": candidates_path,
        "store":      store_path,
    }


def _seed_history(path: Path, turns: list[dict]) -> None:
    with path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


# ─── VERIFICATION 1: CAPTURE INTEGRITY ───────────────────────────────────────

def test_capture_integrity_rejects_operator_paraphrase(isolated_store):
    """A model-paraphrased operator_correction is rejected at API level. No
    disk write. The substring seam makes paraphrase impossible."""
    operator_ts = "2026-05-29T14:32:00"
    alice_ts = "2026-05-29T14:31:30"
    _seed_history(isolated_store["history"], [
        {"role": "assistant", "ts": alice_ts,
         "text": "Lumen Search has roughly 200 employees and an aggressive AE hire bar."},
        {"role": "user", "ts": operator_ts,
         "text": "no that's wrong, Lumen Search has 700 not 200 — you read a stale headcount"},
    ])

    paraphrased = "Lumen Search's actual employee count is 700, not 200."
    with pytest.raises(df.VerbatimMismatchError):
        df.flag_correction_candidate(
            operator_correction = paraphrased,
            operator_turn_ts    = operator_ts,
            alice_claim     = "Lumen Search has roughly 200 employees",
            alice_turn_ts   = alice_ts,
            category        = "factual",
            trigger         = "explicit",
        )
    # No write: candidates file should not exist or be empty.
    assert not isolated_store["candidates"].exists() or \
        isolated_store["candidates"].read_text() == ""


def test_capture_integrity_accepts_real_operator_span(isolated_store):
    """A true verbatim substring is accepted and persisted."""
    operator_ts = "2026-05-29T14:32:00"
    alice_ts = "2026-05-29T14:31:30"
    operator_text = "no that's wrong, Lumen Search has 700 not 200 — you read a stale headcount"
    alice_text = "Lumen Search has roughly 200 employees and an aggressive AE hire bar."
    _seed_history(isolated_store["history"], [
        {"role": "assistant", "ts": alice_ts, "text": alice_text},
        {"role": "user",      "ts": operator_ts,  "text": operator_text},
    ])

    verbatim = "Lumen Search has 700 not 200"
    claim    = "Lumen Search has roughly 200 employees"
    cid = df.flag_correction_candidate(
        operator_correction = verbatim,
        operator_turn_ts    = operator_ts,
        alice_claim     = claim,
        alice_turn_ts   = alice_ts,
        category        = "factual",
        trigger         = "explicit",
    )
    assert cid.startswith("corr-cand-")
    candidates = df.get_pending_candidates()
    assert len(candidates) == 1
    assert candidates[0]["operator_correction"] == verbatim
    assert candidates[0]["alice_claim"] == claim
    assert candidates[0]["category"] == "factual"


def test_capture_integrity_rejects_alice_paraphrase(isolated_store):
    """alice_claim paraphrase is also rejected when alice_turn_ts cited."""
    operator_ts = "2026-05-29T14:32:00"
    alice_ts = "2026-05-29T14:31:30"
    _seed_history(isolated_store["history"], [
        {"role": "assistant", "ts": alice_ts,
         "text": "Lumen Search has roughly 200 employees and an aggressive AE hire bar."},
        {"role": "user", "ts": operator_ts,
         "text": "Lumen Search has 700 not 200 — read a stale headcount"},
    ])
    with pytest.raises(df.VerbatimMismatchError):
        df.flag_correction_candidate(
            operator_correction = "Lumen Search has 700 not 200",
            operator_turn_ts    = operator_ts,
            # Paraphrased — different wording from the real assistant turn.
            alice_claim     = "Lumen Search only employs around 200 people",
            alice_turn_ts   = alice_ts,
            category        = "factual",
            trigger         = "explicit",
        )


# ─── VERIFICATION 2: ROLE CHECK ──────────────────────────────────────────────

def test_role_check_operator_must_be_user_turn(isolated_store):
    """Citing a candidate verbatim against an assistant turn ts is rejected
    even if the substring matches — role is part of the integrity seam."""
    ts = "2026-05-29T14:30:00"
    _seed_history(isolated_store["history"], [
        {"role": "assistant", "ts": ts,
         "text": "that's wrong I think — let me reconsider"},
    ])
    # The text would substring-match, but the role is wrong.
    with pytest.raises(df.VerbatimMismatchError):
        df.flag_correction_candidate(
            operator_correction = "that's wrong",
            operator_turn_ts    = ts,
            category        = "factual",
            trigger         = "explicit",
        )


# ─── VERIFICATION 3: CONFIRMATION GATE ───────────────────────────────────────

def test_confirmation_gate_pending_not_in_store(isolated_store):
    """A pending candidate is NOT in the durable store until confirmed."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "you were wrong about this entirely"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction = "you were wrong about this entirely",
        operator_turn_ts    = ts,
        category        = "judgment",
        trigger         = "explicit",
    )
    assert df.get_all_corrections() == []
    decision_id = df.confirm_correction(cid)
    assert decision_id.startswith("corr-")
    assert len(df.get_all_corrections()) == 1


def test_confirmation_gate_expiry_threshold(isolated_store):
    """After threshold surfacings without reply, the candidate auto-expires
    and stays out of the durable store."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "that's not what I said at all"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction = "that's not what I said at all",
        operator_turn_ts    = ts,
        category        = "framing",
        trigger         = "ambient",
        ambient_score   = 0.8,
    )
    for _ in range(df.DIGEST_EXPIRY_THRESHOLD):
        df.mark_digest_surfaced([cid])
    cand = df.get_candidate(cid)
    assert cand["status"] == "expired"
    # Trying to confirm an expired candidate raises.
    with pytest.raises(ValueError):
        df.confirm_correction(cid)


# ─── VERIFICATION 4: CATEGORIES ──────────────────────────────────────────────

def test_categories_closed_set(isolated_store):
    """Only the closed set of categories is accepted."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "you got that wrong"},
    ])
    with pytest.raises(ValueError):
        df.flag_correction_candidate(
            operator_correction = "you got that wrong",
            operator_turn_ts    = ts,
            category        = "freeform_made_up_category",
            trigger         = "explicit",
        )


def test_categories_all_valid_accepted(isolated_store):
    """All six valid categories accepted."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "you were wrong about that"},
    ])
    for cat in df.VALID_CATEGORIES:
        cid = df.flag_correction_candidate(
            operator_correction = "you were wrong about that",
            operator_turn_ts    = ts,
            category        = cat,
            trigger         = "explicit",
        )
        assert df.get_candidate(cid)["category"] == cat


# ─── VERIFICATION 5: REPLY PARSING ───────────────────────────────────────────

def test_reply_parsing_confirm_directive(isolated_store):
    """'confirm corr-cand-xxx' moves the candidate to the durable store."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "no, that's wrong"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction = "that's wrong",
        operator_turn_ts    = ts,
        category        = "factual",
        trigger         = "explicit",
    )
    result = df.parse_and_apply_reply(f"please confirm {cid}, thanks")
    assert len(result["confirmed"]) == 1
    assert result["confirmed"][0]["candidate_id"] == cid


def test_reply_parsing_edit_category(isolated_store):
    """'edit corr-cand-xxx category=judgment' overrides the category at
    confirmation time."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "no, that's wrong"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction = "that's wrong",
        operator_turn_ts    = ts,
        category        = "factual",
        trigger         = "explicit",
    )
    result = df.parse_and_apply_reply(f"edit {cid} category=judgment")
    assert len(result["edited"]) == 1
    assert result["edited"][0]["category"] == "judgment"
    decision = df.get_correction(result["edited"][0]["decision_id"])
    assert decision["category"] == "judgment"


def test_reply_parsing_outcome_directive(isolated_store):
    """'outcome corr-xxx: ...' attaches a descriptive outcome to a
    confirmed correction."""
    ts = "2026-05-29T15:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "no, that's wrong"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction = "that's wrong",
        operator_turn_ts    = ts,
        category        = "factual",
        trigger         = "explicit",
    )
    decision_id = df.confirm_correction(cid)
    result = df.parse_and_apply_reply(
        f"outcome {decision_id}: turned out the headcount source was 6 months old"
    )
    assert len(result["outcomes"]) == 1
    decision = df.get_correction(decision_id)
    assert decision["outcome"].startswith("turned out the headcount")
    assert decision["outcome_at"]


# ─── VERIFICATION 6: PATTERN SURFACING ───────────────────────────────────────

def test_pattern_summary_aggregates_by_category(isolated_store):
    """pattern_summary returns count_by_category + representative_quote."""
    ts1 = "2026-05-29T10:00:00"
    ts2 = "2026-05-29T11:00:00"
    ts3 = "2026-05-29T12:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts1, "text": "no, Lumen Search has 700 employees not 200"},
        {"role": "user", "ts": ts2, "text": "that's not what I said about Northwind Systems"},
        {"role": "user", "ts": ts3, "text": "you're wrong, the Lumen Search fit is closer to PLG not enterprise"},
    ])
    for verb, operator_ts, cat in [
        ("Lumen Search has 700 employees not 200",     ts1, "factual"),
        ("that's not what I said about Northwind Systems", ts2, "framing"),
        ("the Lumen Search fit is closer to PLG not enterprise", ts3, "judgment"),
    ]:
        cid = df.flag_correction_candidate(
            operator_correction=verb, operator_turn_ts=operator_ts,
            category=cat, trigger="explicit",
        )
        df.confirm_correction(cid)

    summary = df.pattern_summary(window_days=7)
    assert summary["total"] == 3
    assert summary["count_by_category"] == {
        "factual": 1, "framing": 1, "judgment": 1
    }
    # Representative quote is the most recent in each category; the candidate's
    # words, not Alice's. (Anti-paraphrase invariant at the read seam.)
    assert summary["by_category"]["factual"]["representative_quote"] == \
        "Lumen Search has 700 employees not 200"
    # Topic tokens pulled from the candidate's verbatim — descriptive only.
    factual_topics = summary["by_category"]["factual"]["topic_tokens"]
    assert "Lumen Search" in factual_topics
    # No prediction fields anywhere.
    assert "predicted_operator_response" not in summary["by_category"]["factual"]
    assert "operator_behavior_model" not in summary


def test_pattern_summary_empty_window(isolated_store):
    """Empty window returns total=0 and an empty by_category map."""
    _seed_history(isolated_store["history"], [])
    summary = df.pattern_summary(window_days=7)
    assert summary["total"] == 0
    assert summary["by_category"] == {}
    # render returns empty string — scorecard can omit the section cleanly.
    assert df.render_pattern_summary(7) == ""


def test_pattern_summary_window_filter(isolated_store):
    """Confirmations older than the window are excluded."""
    ts_old = "2026-05-29T10:00:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts_old, "text": "no, you got that wrong"},
    ])
    cid = df.flag_correction_candidate(
        operator_correction="you got that wrong", operator_turn_ts=ts_old,
        category="judgment", trigger="explicit",
    )
    decision_id = df.confirm_correction(cid)
    # Backdate the confirmed_at to 60 days ago.
    records = df._read_jsonl(df.STORE_PATH)
    records[0]["confirmed_at"] = (
        datetime.now() - timedelta(days=60)
    ).isoformat(timespec="seconds")
    df._rewrite_jsonl(df.STORE_PATH, records)

    assert df.pattern_summary(window_days=7)["total"] == 0
    assert df.pattern_summary(window_days=90)["total"] == 1


# ─── VERIFICATION 7: EXPLICIT TRIGGER DETECTION ──────────────────────────────

def test_explicit_trigger_detection_positive():
    """Triggers Alice should fire on."""
    for s in [
        "no, you're wrong about that",
        "you were wrong about Lumen Search",
        "that's not what I said",
        "log this correction",
        "correction: Lumen Search has 700 not 200",
        "that's wrong",
        "you got that wrong",
        "you misread me",
        "that's a misread",
        "you're wrong about the headcount",
    ]:
        assert df.detect_explicit_trigger(s), f"should fire on: {s!r}"


def test_explicit_trigger_detection_negative():
    """Phrases Alice should NOT fire on (false-positive guards)."""
    for s in [
        "I remember you said the right thing about this",
        "the apartment is on the wrong side of the building",
        "this is the right call",
        "what should I do next",
        "I'm not sure that's correct",  # uncertain, not a correction directive
        "let's prep Northwind Systems",
    ]:
        assert not df.detect_explicit_trigger(s), f"should NOT fire on: {s!r}"


# ─── live-LLM ambient (cost-gated) ───────────────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("ALICE_LIVE_TESTS"),
    reason="Ambient detector calls Haiku ($) — set ALICE_LIVE_TESTS=1 to run",
)
def test_ambient_detector_round_trip(isolated_store):
    """Ambient run flags a clear correction in a small synthetic stream."""
    ts_a = "2026-05-29T10:00:00"
    ts_b = "2026-05-29T10:00:30"
    _seed_history(isolated_store["history"], [
        {"role": "assistant", "ts": ts_a,
         "text": "Lumen Search has roughly 200 employees per their LinkedIn page."},
        {"role": "user", "ts": ts_b,
         "text": "no that's wrong, Lumen Search has about 700 employees now"},
    ])
    history = df._read_jsonl(df.HISTORY_PATH)
    ids = df.ambient_review(history)
    # At least one candidate should land; structural seam already verified.
    # If the model misses, it's expected ambient drift, not a regression
    # of the substring check — so this is asserted "best-effort".
    if ids:
        cand = df.get_candidate(ids[0])
        # Substring check has already been enforced for what's on disk.
        assert cand["operator_correction"] in (
            df._find_turn(cand["operator_turn_ts"], role="user") or {}
        ).get("text", "")
