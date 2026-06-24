"""Regression tests locking two grounding behaviors against refactors.

  - The two grounding detectors must fire on the failure shapes that
    prompted them, and stay silent on the look-alike non-failures.
  - describe_capabilities must report the live routed model (sourced from
    llm.MODEL_FOR_TASK), never a hardcoded or confabulated string.

Pure-function + registry-read tests; deterministic, no network, hermetic.
"""
import sys
from pathlib import Path
from alice.pipeline import grounding as g
from alice import tools
from alice.llm import llm


# ─── detect_truncated_completion ─────────────────────────────────────────────
# Fires only when the turn was cut at max_tokens AND still claims completion —
# the structurally-suspect "I'm done" on output that was sliced mid-stream.

def test_truncated_completion_fires_on_maxtokens_plus_claim():
    f = g.detect_truncated_completion(
        stop_reason="max_tokens",
        response_text="Wrote the first files. The resume is ready.")
    assert f is not None and f["kind"] == "truncated_completion"

def test_truncated_completion_silent_when_not_truncated():
    # Same completion language, but the turn ended cleanly -> not suspect.
    assert g.detect_truncated_completion(
        stop_reason="end_turn",
        response_text="The resume is ready.") is None

def test_truncated_completion_silent_when_truncated_without_claim():
    # Cut off, but makes no done-claim -> nothing to flag.
    assert g.detect_truncated_completion(
        stop_reason="max_tokens",
        response_text="Let me work through the next step before") is None


# ─── detect_write_claimed_no_write_tool ──────────────────────────────────────
# Fires when a turn fired only reads yet claimed to have written a file.

def test_write_claimed_fires_when_only_reads_fired():
    f = g.detect_write_claimed_no_write_tool(
        tool_calls=[{"name": "read_file"}, {"name": "list_dir"}],
        response_text="I created the resume file for you.")
    assert f is not None and f["kind"] == "write_claimed_no_write_tool"

def test_write_claimed_silent_when_write_tool_fired():
    assert g.detect_write_claimed_no_write_tool(
        tool_calls=[{"name": "write_file"}],
        response_text="I created the resume file.") is None

def test_write_claimed_silent_without_a_write_claim():
    assert g.detect_write_claimed_no_write_tool(
        tool_calls=[{"name": "read_file"}],
        response_text="Here is some analysis of the role.") is None


# ─── describe_capabilities reports the LIVE routed model ─────────────────────

def test_describe_capabilities_reports_live_routed_model():
    rt = tools._describe_capabilities({}).get("runtime", {})
    conv = rt.get("conversational_model", "")
    # Must equal what the router will actually pick for telegram_chat — sourced
    # from llm.MODEL_FOR_TASK, not a hardcoded string.
    expected = llm.MODEL_FOR_TASK.get("telegram_chat", llm.TIER_CHEAP["model"])
    assert conv == expected, f"reported {conv!r} != routed {expected!r}"

def test_describe_capabilities_model_is_real_not_confabulated():
    # Guards against reporting a model string not in the registry (e.g. a
    # confabulated 'Claude 3.5 Sonnet').
    rt = tools._describe_capabilities({}).get("runtime", {})
    conv = rt.get("conversational_model", "")
    known = {llm.TIER_CHEAP["model"], llm.TIER_MID["model"], llm.TIER_HEAVY["model"]} \
        if all(hasattr(llm, t) for t in ("TIER_CHEAP", "TIER_MID", "TIER_HEAVY")) \
        else set(llm.MODEL_FOR_TASK.values()) | {llm.TIER_CHEAP["model"]}
    assert conv in known, f"reported model {conv!r} is not a registry model"
    assert "3.5" not in conv, "reported the confabulated 3.5 string"
