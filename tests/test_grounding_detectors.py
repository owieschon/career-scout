"""Negative-case coverage for the two grounding detectors
(detect_category_mismatch, detect_specific_claims_without_tools). These are the
verification spine — an untested detector can blind-trigger (false positive,
crying wolf) or miss silently. The negative cases (correctly not firing out of
scope) matter as much as the positives. Pure (no LLM)."""
from pathlib import Path

from alice.pipeline import grounding as g


# ── detect_category_mismatch ──────────────────────────────────────────────────
def _list_dir(*names):
    return [{"name": "list_dir",
             "result": {"entries": [{"name": n, "is_file": True} for n in names]}}]


def test_category_mismatch_fires_on_known_positive():
    """user asked pdf, tools returned only docx, response claims pdf -> FLAG."""
    r = g.detect_category_mismatch(
        user_text="do I have any pdf files?",
        tool_calls_with_results=_list_dir("resume.docx"),
        response_text="I found 2 pdf files for you.")
    assert r is not None and r["mismatched"] == ["pdf"]


def test_category_mismatch_silent_when_claim_is_observed():
    """The claimed type WAS actually returned by the tools -> grounded, no flag."""
    r = g.detect_category_mismatch(
        user_text="any pdf files?",
        tool_calls_with_results=_list_dir("report.pdf"),
        response_text="I found 1 pdf file.")
    assert r is None


def test_category_mismatch_silent_when_no_filetype_asked():
    """No file-type in the user's question -> out of scope, no flag."""
    r = g.detect_category_mismatch(
        user_text="how is the search going?",
        tool_calls_with_results=_list_dir("resume.docx"),
        response_text="I found 2 pdf files.")
    assert r is None


def test_category_mismatch_silent_when_no_claim_in_response():
    r = g.detect_category_mismatch(
        user_text="any pdf files?",
        tool_calls_with_results=_list_dir("resume.docx"),
        response_text="Here is a summary of what I found.")
    assert r is None


def test_category_mismatch_silent_when_claim_not_among_asked():
    """response claims a type the user never asked about -> not this detector's
    scope (only asked-but-unobserved is the mismatch shape)."""
    r = g.detect_category_mismatch(
        user_text="any docx files?",
        tool_calls_with_results=_list_dir("notes.docx"),
        response_text="I found 2 pdf files.")
    assert r is None


# ── detect_specific_claims_without_tools ──────────────────────────────────────
def test_specific_claims_fires_on_filename_without_tools():
    r = g.detect_specific_claims_without_tools(
        tool_calls=[], response_text="I saved it as resume-master-vc.pdf.")
    assert r is not None and "resume-master-vc.pdf" in r["filenames"]


def test_specific_claims_fires_on_date_without_tools():
    r = g.detect_specific_claims_without_tools(
        tool_calls=[], response_text="Your interview is on 2026-06-15.")
    assert r is not None and "2026-06-15" in r["dates"]


def test_specific_claims_silent_when_tools_fired():
    """Tools fired -> a different failure class; this detector must NOT fire."""
    r = g.detect_specific_claims_without_tools(
        tool_calls=_list_dir("resume.pdf"),
        response_text="I saw resume.pdf in your folder.")
    assert r is None


def test_specific_claims_silent_on_generic_prose():
    """No concrete filename/date/time -> a boundary/generic answer is correct
    no-tool behavior, not a fabrication."""
    r = g.detect_specific_claims_without_tools(
        tool_calls=[], response_text="I can help you tailor your applications.")
    assert r is None


def test_specific_claims_silent_on_empty_response():
    assert g.detect_specific_claims_without_tools(tool_calls=[], response_text="") is None
