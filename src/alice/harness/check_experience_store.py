"""Regression tests for the experience-capture store.

What this file proves — the two NON-NEGOTIABLE verifications first:

  1. CAPTURE INTEGRITY (test_capture_integrity_*): a paraphrased verbatim
     is REJECTED at the API level; a true verbatim span is ACCEPTED. This
     is the structural enforcement that makes paraphrase impossible at
     this seam.

  2. DUAL-LANDING (test_dual_landing_*): a claim grounded in a confirmed
     experience entry PASSES Stage 3; a claim that is not grounded in
     ANY confirmed entry (or in the JD / variant / company-research)
     gets FLAGGED.

Then the rest:

  3. CONFIRMATION GATE (test_confirmation_gate_*): an unconfirmed (or
     auto-expired) candidate does NOT enter the durable store.

  4. AMBIENT DETECTOR (test_ambient_detector_*): Haiku flags a qualitative
     story that pure regex would miss.

  5. RETRIEVAL TOKEN BUDGET (test_retrieval_*): tag-based retrieval pulls
     relevant entries and respects the token budget cap.

  6. CONTEXT CACHING (test_context_caching_*): the stored entry carries
     enough context to disambiguate its referent.

Live-LLM tests (4) are gated behind ALICE_LIVE_TESTS=1 because Haiku
calls cost API $; the structural tests (1, 2, 3, 5, 6) need no network.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest


from alice.persistence import experience_store as es
from alice.pipeline import prep_pipeline as pp


# ─── shared fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """Repoint the store files to a temp dir so each test starts fresh and
    no test pollutes the real feedback/ tree."""
    history_path    = tmp_path / "telegram-history.jsonl"
    candidates_path = tmp_path / "experience-candidates.jsonl"
    store_path      = tmp_path / "experience-store.jsonl"
    monkeypatch.setattr(es, "HISTORY_PATH",    history_path)
    monkeypatch.setattr(es, "CANDIDATES_PATH", candidates_path)
    monkeypatch.setattr(es, "STORE_PATH",      store_path)
    return {
        "history":    history_path,
        "candidates": candidates_path,
        "store":      store_path,
    }


def _seed_history(history_path: Path, turns: list[dict]) -> None:
    """Write turns to the isolated history file."""
    with history_path.open("w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


# ─── VERIFICATION 1: CAPTURE INTEGRITY (critical) ────────────────────────

def test_capture_integrity_rejects_paraphrase(isolated_store):
    """The structural seam: a model-paraphrased version of a candidate turn
    must be rejected at API level, no disk write. Proves paraphrase is
    impossible at this seam."""
    ts = "2026-05-28T14:32:00"
    real_text = (
        "we got Keystone back to $14M after the contract review last "
        "quarter — that was the recovery that mattered"
    )
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": real_text},
    ])

    # A paraphrased verbatim (different word order, slight rewording).
    paraphrased = "We recovered Keystone to $14M after contract review"
    with pytest.raises(es.VerbatimMismatchError):
        es.flag_experience_candidate(
            verbatim       = paraphrased,
            source_turn_ts = ts,
            trigger        = "explicit",
            model_summary  = "Keystone recovery",
            suggested_tags = ["keystone", "recovery"],
        )

    # No file written — the rejection happened BEFORE disk.
    assert not isolated_store["candidates"].exists() or \
           isolated_store["candidates"].stat().st_size == 0


def test_capture_integrity_accepts_verbatim_substring(isolated_store):
    """The positive control: a true verbatim span IS accepted."""
    ts = "2026-05-28T14:32:00"
    real_text = (
        "we got Keystone back to $14M after the contract review last "
        "quarter — that was the recovery that mattered"
    )
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": real_text},
    ])

    verbatim = "we got Keystone back to $14M after the contract review"
    cid = es.flag_experience_candidate(
        verbatim       = verbatim,
        source_turn_ts = ts,
        trigger        = "explicit",
        model_summary  = "Keystone recovery to $14M",
        suggested_tags = ["keystone", "recovery", "renewal"],
    )
    assert cid.startswith("exp-cand-")
    candidates = es.get_pending_candidates()
    assert len(candidates) == 1
    assert candidates[0]["verbatim"] == verbatim
    assert candidates[0]["status"] == "pending"


def test_capture_integrity_rejects_missing_turn(isolated_store):
    """A verbatim cited against a ts that doesn't exist in history is
    rejected — can't attribute to a turn that isn't there."""
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": "2026-05-28T14:32:00", "text": "real text"},
    ])
    with pytest.raises(es.VerbatimMismatchError):
        es.flag_experience_candidate(
            verbatim       = "real text",
            source_turn_ts = "2026-05-28T99:99:99",  # doesn't exist
            trigger        = "explicit",
        )


def test_capture_integrity_model_summary_never_returned_in_block(isolated_store):
    """model_summary is for the candidate's eyes in the digest only. It must NEVER
    appear in the EXPERIENCE EXTRAS block delivered to writers/verifier."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we got Keystone back to $14M"},
    ])
    cid = es.flag_experience_candidate(
        verbatim       = "we got Keystone back to $14M",
        source_turn_ts = ts,
        trigger        = "explicit",
        model_summary  = "THIS_SHOULD_NEVER_LEAK_TO_WRITERS",
        suggested_tags = ["keystone"],
    )
    es.confirm_candidate(cid)
    block, used, _framing = es.retrieve_for_role(target_tags=["keystone"])
    assert "THIS_SHOULD_NEVER_LEAK_TO_WRITERS" not in block
    assert "$14M" in block  # verbatim does flow through


# ─── VERIFICATION 2: DUAL-LANDING (the seam to Stage 2 + Stage 3) ────────────

def test_dual_landing_grounded_experience_claim_passes_verifier(isolated_store):
    """A claim grounded in a confirmed experience entry must pass Stage 3.
    The verifier MUST pull experience entries into ground_blob — this is
    the closing-the-loop test for the second seam of dual-landing."""
    ts = "2026-05-28T14:32:00"
    verbatim = "we got Keystone back to $14M after the contract review"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": verbatim + " — recovery mattered"},
    ])
    cid = es.flag_experience_candidate(
        verbatim=verbatim, source_turn_ts=ts, trigger="explicit",
        suggested_tags=["keystone"],
    )
    es.confirm_candidate(cid)
    _block, used_entries, _framing = es.retrieve_for_role(target_tags=["keystone"])

    # Stage 3 inputs: ground with experience entries attached, write
    # mentioning the experience-grounded fact.
    ground = pp.GroundResult(
        jd_body                   = "Some defense industry job description.",
        operator_history              = "Jordan Avery worked at Lattice Additive.",
        company_research          = "Defense customer relationships.",
        experience_extras_entries = used_entries,
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Jordan Avery recovered $14M with a strategic intervention.",
    )
    v = pp.stage_verify(write=write, ground=ground)
    # The $14M claim must pass because the confirmed experience entry
    # contains "$14M" in its verbatim → it's in ground_blob.
    flagged_tokens = [c["token"]
                      for verdict in v.verdicts
                      for c in verdict.flagged_claims]
    assert "$14M" not in flagged_tokens, (
        f"Experience-grounded $14M claim was flagged. flagged_tokens="
        f"{flagged_tokens}. This breaks the dual-landing seam — confirmed "
        f"experience entries are not flowing into Stage 3 ground_blob."
    )


def test_dual_landing_ungrounded_claim_gets_flagged(isolated_store):
    """A claim not grounded in any source — including experience — must
    still be flagged. Confirms the verifier doesn't blanket-pass anything
    once experience entries are in the blob."""
    # Empty store — no confirmed entries.
    ground = pp.GroundResult(
        jd_body                   = "Defense industry job.",
        operator_history              = "Jordan Avery worked at Lattice Additive.",
        company_research          = "Defense customers.",
        experience_extras_entries = [],
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        # $99M is not in any source.
        cover="Jordan Avery built a $99M pipeline at Keystone.",
    )
    v = pp.stage_verify(write=write, ground=ground)
    flagged_tokens = [c["token"]
                      for verdict in v.verdicts
                      for c in verdict.flagged_claims]
    assert "$99M" in flagged_tokens


def test_dual_landing_attribution_records_experience_source(isolated_store):
    """When a claim is grounded by an experience entry, the per-claim
    attribution map must record source='experience'. Without this the
    audit trail can't distinguish JD-grounded claims from experience-
    grounded ones."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we recovered $14M at Keystone"},
    ])
    cid = es.flag_experience_candidate(
        verbatim="we recovered $14M at Keystone",
        source_turn_ts=ts, trigger="explicit",
        suggested_tags=["keystone"],
    )
    es.confirm_candidate(cid)
    _block, used_entries, _framing = es.retrieve_for_role(target_tags=["keystone"])

    ground = pp.GroundResult(
        jd_body                   = "Some unrelated JD body.",
        operator_history              = "Jordan Avery worked at Lattice Additive.",
        experience_extras_entries = used_entries,
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Jordan Avery recovered $14M for a key account.",
    )
    v = pp.stage_verify(write=write, ground=ground)
    cover_verdict = next(x for x in v.verdicts if x.artifact == "cover")
    # The grounded claim should be attributed to 'experience' (not 'jd'
    # or 'history' — neither contains $14M).
    assert "experience" in cover_verdict.attribution, (
        f"attribution map missing 'experience': {cover_verdict.attribution}"
    )


def test_dual_landing_writer_prompt_contains_experience_extras_block(
        isolated_store):
    """Stage 2 assemble_prompt must inject the EXPERIENCE EXTRAS block
    into the writer's prompt when ground has it set. This is the Stage 2
    half of dual-landing."""
    ground = pp.GroundResult(
        jd_body                   = "JD body x" * 50,
        jd_source                 = "test",
        jd_chars                  = 500,
        operator_history              = "candidate history y" * 50,
        operator_variant              = "resume-test.docx",
        experience_extras_block   = (
            "EXPERIENCE EXTRAS (CONFIRMED entries...):\n\n"
            "[exp-abc] (tags: keystone)\n"
            '  verbatim (Candidate, ...): "we got Keystone back to $14M"'
        ),
        experience_extras_entries = [{"entry_id": "exp-abc",
                                      "verbatim": "we got Keystone back to $14M",
                                      "tags": ["keystone"]}],
    )
    prompt = pp.assemble_prompt(
        stage="cover", ground=ground,
        prior_artifacts={"resume": "draft resume"},
        company="X", role="Y",
    )
    assert "EXPERIENCE EXTRAS" in prompt
    assert "$14M" in prompt
    assert "HOW TO USE EXPERIENCE EXTRAS" in prompt


# ─── VERIFICATION 3: CONFIRMATION GATE ──────────────────────────────────────

def test_confirmation_gate_unconfirmed_not_in_store(isolated_store):
    """A pending (unconfirmed) candidate must NEVER appear in the durable
    store via retrieve_for_role. Writers/verifier only see confirmed."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we recovered $14M at Keystone"},
    ])
    es.flag_experience_candidate(
        verbatim="we recovered $14M at Keystone",
        source_turn_ts=ts, trigger="explicit",
        suggested_tags=["keystone"],
    )
    block, used, _framing = es.retrieve_for_role(target_tags=["keystone"])
    assert block == ""
    assert used == []
    # Durable store file should be untouched
    assert not isolated_store["store"].exists()


def test_confirmation_gate_expires_after_threshold_digests(isolated_store):
    """Silence = rejection. After DIGEST_EXPIRY_THRESHOLD surfacings with
    no reply, the candidate's status becomes 'expired' and it stops being
    surfaced — and never enters the durable store."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we got Keystone back to $14M"},
    ])
    cid = es.flag_experience_candidate(
        verbatim="we got Keystone back to $14M",
        source_turn_ts=ts, trigger="explicit",
    )
    for _ in range(es.DIGEST_EXPIRY_THRESHOLD):
        es.mark_digest_surfaced([cid])
    cand = es.get_candidate(cid)
    assert cand["status"] == "expired"
    # And confirm refuses to elevate an expired candidate
    with pytest.raises(ValueError):
        es.confirm_candidate(cid)


def test_confirmation_gate_confirm_moves_to_store(isolated_store):
    """Happy path: confirmed candidate becomes durable entry."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we got Keystone back to $14M"},
    ])
    cid = es.flag_experience_candidate(
        verbatim="we got Keystone back to $14M",
        source_turn_ts=ts, trigger="explicit",
        suggested_tags=["keystone"],
    )
    eid = es.confirm_candidate(cid)
    assert eid.startswith("exp-")
    entries = es.get_all_entries()
    assert len(entries) == 1
    assert entries[0]["entry_id"] == eid
    assert es.get_candidate(cid)["status"] == "confirmed"


def test_confirmation_gate_reply_parser_handles_directives(isolated_store):
    """The reply parser applies confirm/reject/edit directives. The
    candidate uses this from the morning digest reply (and from chat)."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we got Keystone back to $14M"},
    ])
    cid = es.flag_experience_candidate(
        verbatim="we got Keystone back to $14M",
        source_turn_ts=ts, trigger="explicit",
    )
    result = es.parse_and_apply_reply(
        f"confirm {cid}\nsome other text here"
    )
    assert len(result["confirmed"]) == 1
    assert result["confirmed"][0]["candidate_id"] == cid
    assert es.get_candidate(cid)["status"] == "confirmed"


# ─── VERIFICATION 4: AMBIENT DETECTOR (Haiku — qualitative story) ────────────

def test_ambient_detector_explicit_trigger_regex_fires():
    """The explicit-trigger regex catches the trigger phrases the chat
    surface uses to flag immediately. Conservative — embedded 'remember'
    does NOT fire."""
    for phrase in ("remember this", "Remember that", "save this",
                   "log this", "capture this", "don't forget this",
                   "add to memory", "store this"):
        assert es.detect_explicit_trigger(phrase), f"should fire: {phrase!r}"

    for phrase in ("I remember reading that", "I'd love to capture some leads",
                   "do you log everything?", "save me some time"):
        assert not es.detect_explicit_trigger(phrase), \
            f"should NOT fire: {phrase!r}"


@pytest.mark.skipif(
    not os.environ.get("ALICE_LIVE_TESTS"),
    reason="Live Haiku call costs API $; set ALICE_LIVE_TESTS=1 to run.",
)
def test_ambient_detector_flags_qualitative_story(isolated_store):
    """Ambient detection uses Haiku rather than regex because pure regex
    misses qualitative stories without numbers or keywords. This test
    proves the Haiku path catches one.

    The turn has no dollar amount, no percentage, no years-of-experience
    pattern — only a qualitative recovery story. Regex would miss it.
    """
    qualitative = (
        "I'll never forget the Bedford situation — they were going to "
        "walk after the implementation hit week six and the production "
        "line still wasn't pulling data right. I drove out, sat in the "
        "control room with the maintenance lead for two days straight, "
        "and we tracked it down to a single firmware mismatch nobody "
        "had touched in years. That save was what got me promoted."
    )
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": qualitative},
    ])
    ids = es.ambient_review(
        [{"role": "user", "ts": ts, "text": qualitative}],
        min_score=0.3,
    )
    assert len(ids) >= 1, (
        "Haiku ambient detector failed to flag a qualitative story without "
        "numeric or keyword anchors. The override (Haiku vs regex) is what "
        "this test verifies — if this fails, the structural decision to use "
        "an LLM detector is unjustified."
    )
    # And the substring-match check held — what landed must be a substring
    # of the qualitative text.
    for cid in ids:
        c = es.get_candidate(cid)
        assert c["verbatim"] in qualitative


# ─── VERIFICATION 5: RETRIEVAL (token-budget cap) ────────────────────────────

def test_retrieval_respects_token_budget(isolated_store):
    """Token-budget cap (override on entry-count cap). Five entries seeded;
    a tight token budget pulls only the first few before stopping. Proves
    the cap is on tokens not on count."""
    ts_base = "2026-05-28T14:"
    history = []
    for i in range(5):
        ts = f"{ts_base}{30+i:02d}:00"
        # Long entries (~600 chars each) so the budget gets exhausted fast
        text = ("Jordan Avery recovered $14M at Keystone during contract renewal. "
                "X" * 500 + f" entry {i}")
        history.append({"role": "user", "ts": ts, "text": text})
    _seed_history(isolated_store["history"], history)

    cids = []
    for h in history:
        cid = es.flag_experience_candidate(
            verbatim=h["text"][:200],
            source_turn_ts=h["ts"], trigger="explicit",
            suggested_tags=["keystone"],
        )
        cids.append(cid)
    for cid in cids:
        es.confirm_candidate(cid)

    # Tight budget: ~200 tokens = ~800 chars; one formatted entry alone
    # is ~300+ chars, so we should pull 1-2 entries, not all 5.
    block, used, _framing = es.retrieve_for_role(
        target_tags=["keystone"], token_budget=200,
    )
    assert len(used) < 5, (
        f"Expected token budget to cap retrieval below 5 entries; got "
        f"{len(used)}. The cap is on tokens not on count."
    )


def test_retrieval_filters_superseded_entries(isolated_store):
    """Superseded entries are KEPT (audit) but NEVER delivered to retrieval."""
    ts = "2026-05-28T14:32:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts, "text": "we got Keystone to $14M"},
    ])
    cid = es.flag_experience_candidate(
        verbatim="we got Keystone to $14M",
        source_turn_ts=ts, trigger="explicit",
        suggested_tags=["keystone"],
    )
    eid = es.confirm_candidate(cid)
    es.supersede_entry(eid, reason="test-stale")
    block, used, _framing = es.retrieve_for_role(target_tags=["keystone"])
    assert used == []
    # But the entry is still in the file
    all_entries = es.get_all_entries(include_superseded=True)
    assert len(all_entries) == 1


def test_retrieval_tag_intersection_filters(isolated_store):
    """Tag-based: entries whose tags don't overlap target_tags are
    excluded entirely from a target-tag query."""
    ts1 = "2026-05-28T14:32:00"
    ts2 = "2026-05-28T14:33:00"
    _seed_history(isolated_store["history"], [
        {"role": "user", "ts": ts1, "text": "we got Keystone to $14M"},
        {"role": "user", "ts": ts2, "text": "Lattice Additive printed 200 plates"},
    ])
    cid1 = es.flag_experience_candidate(
        verbatim="we got Keystone to $14M", source_turn_ts=ts1,
        trigger="explicit", suggested_tags=["keystone", "recovery"],
    )
    cid2 = es.flag_experience_candidate(
        verbatim="Lattice Additive printed 200 plates", source_turn_ts=ts2,
        trigger="explicit", suggested_tags=["additive", "manufacturing"],
    )
    es.confirm_candidate(cid1)
    es.confirm_candidate(cid2)

    _block, used, _framing = es.retrieve_for_role(target_tags=["keystone"])
    assert len(used) == 1
    assert used[0]["tags"] == ["keystone", "recovery"]


# ─── VERIFICATION 6: CONTEXT CACHING ─────────────────────────────────────────

def test_context_caching_preserves_referent(isolated_store):
    """The ±2 turns context cached with each entry must travel with it
    through retrieval. A quote like 'we got it back to $14M' without
    context is unmoored — the test simulates that exact case."""
    history = [
        {"role": "user",  "ts": "2026-05-28T14:30:00",
         "text": "I was thinking about that Keystone deal we almost lost"},
        {"role": "alice", "ts": "2026-05-28T14:31:00",
         "text": "the renewal that was at risk?"},
        {"role": "user",  "ts": "2026-05-28T14:32:00",
         "text": "yeah, we got it back to $14M after the contract review"},
        {"role": "alice", "ts": "2026-05-28T14:33:00",
         "text": "got it — that was the recovery that mattered"},
    ]
    _seed_history(isolated_store["history"], history)

    cid = es.flag_experience_candidate(
        verbatim="we got it back to $14M after the contract review",
        source_turn_ts="2026-05-28T14:32:00",
        trigger="explicit",
        suggested_tags=["keystone", "recovery"],
    )
    cand = es.get_candidate(cid)
    # Context BEFORE must include the Keystone mention so the referent
    # of "we got it back" is reachable.
    before_text = " ".join(t["text"] for t in cand["context_before"])
    assert "Keystone" in before_text, (
        "Context caching failed to preserve the Keystone referent — "
        "the verbatim 'we got it back to $14M' is unmoored without it."
    )

    es.confirm_candidate(cid)
    block, _used, _framing = es.retrieve_for_role(target_tags=["keystone"])
    assert "Keystone" in block, (
        "Retrieval block dropped the context_before — the referent is "
        "missing from the writer-facing prompt."
    )


# ─── tool wiring registers ──────────────────────────────────────────────────

def test_flag_experience_candidate_tool_registers():
    from alice import tools
    names = [s["name"] for s in tools.tool_specs()]
    assert "flag_experience_candidate" in names
    assert "list_pending_experience_candidates" in names


def test_flag_experience_candidate_tool_has_guard():
    from alice import tools
    for t in tools.TOOLS_REGISTRY:
        if t["name"] == "flag_experience_candidate":
            assert t["mutating"] is True
            assert t["guard"] is not None
            return
    raise AssertionError("flag_experience_candidate not in registry")


if __name__ == "__main__":
    # Allow plain-script execution when pytest is not available
    fns = [v for k, v in globals().items()
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        # Skip fixture-using tests in script mode — they need pytest
        if "isolated_store" in fn.__code__.co_varnames:
            print(f"  [SKIP-script] {fn.__name__} (needs pytest fixture)")
            continue
        try:
            fn()
            print(f"  [OK] {fn.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERR ] {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"non-fixture tests: {failed} failed")
    sys.exit(0 if failed == 0 else 1)
