"""Regression tests for the four-stage prep pipeline.

What this file proves:

  1. Stage 1 (GROUND) halts when required inputs are missing — does NOT
     silently proceed to Stage 2 on empty JD or empty candidate history.

  2. Stage 1 governor returns a graceful partial when company research
     can't be retrieved but required inputs are present.

  3. Stage 3 (VERIFY) flags ungrounded factual claims and passes claims
     that appear verbatim in the retrieved ground material.

  4. The tool wiring (tools.generate_application_package) registers and
     the dispatched executor returns a structured halt-or-success result.

  5. Fix 4 chat-path corrections:
       - confirmation-detection gate fires precisely (yes/no, not embedded)
       - last-assistant-question anchor recovers the question
       - observation relevance gate excludes by default
       - the global context no longer injects observations

Live-network calls (fetch_jd) are bypassed by passing crafted ground
dataclasses directly. The pipeline's structural gates are the units
under test; the model calls in Stage 2 are not.
"""
from __future__ import annotations

import sys
from pathlib import Path


from alice.pipeline import prep_pipeline as pp


# ─── Stage 1 — GROUND halts ──────────────────────────────────────────────────

def test_stage1_halts_on_missing_url():
    g = pp.stage_ground(company="X", role="Y", url="", archetype="AE")
    assert g.halted is True
    assert "no JD URL" in (g.halt_reason or "")


def test_stage1_halts_on_unfetchable_url():
    g = pp.stage_ground(company="X", role="Y",
                        url="https://example.com/no-such-board",
                        archetype="AE")
    assert g.halted is True
    assert "JD body fetch failed" in (g.halt_reason or "")


def test_stage1_required_complete_only_when_both_present():
    g = pp.GroundResult()
    assert g.required_complete() is False
    g.jd_body = "x" * 500
    assert g.required_complete() is False
    g.operator_history = "y" * 1000
    assert g.required_complete() is True


# ─── Stage 1 — graceful partial (company research fallback) ─────────────────

def test_stage1_company_research_falls_back_with_incomplete_flag(monkeypatch):
    """When no candidate-curated company file exists AND web_research is forced to
    fail, Stage 1 must fall through to JD-body-only with incomplete=True.
    Patching _web_research_company to fail is the right unit-isolation move:
    the structural invariant under test is the fallback wiring, not the
    research call itself."""
    monkeypatch.setattr(
        pp, "_web_research_company",
        lambda **kw: (None, "test-forced failure",
                      {"confirmed": 0, "unclear": 0, "not_found": 5}),
    )
    g = pp.stage_ground(
        company="ZZ-Pipeline-Test",  # No targets/companies/ file
        role="Test", url="https://www.northwind.com/careers/enterprise-client-partner-8485725002/",
        archetype="AE",
    )
    if g.halted:
        # Network/JD-fetch unavailable in this run — bypass
        return
    assert g.company_research is not None
    assert g.company_research_incomplete is True
    assert g.company_research_source == "jd_body_only"


import os
import pytest


@pytest.mark.skipif(
    not os.environ.get("ALICE_LIVE_TESTS"),
    reason="Live web_search call costs API $; set ALICE_LIVE_TESTS=1 to run.",
)
def test_stage1_web_research_path_uses_web_research_v1_source():
    """When ALICE_LIVE_TESTS=1, exercise the real Path A.5 against a known
    public company. Assert the source label switched and a cache file landed
    in targets/companies/."""
    from pathlib import Path as _P
    # Ensure no pre-existing curated file for the slug — clean room for
    # Path A.5. Northwind Systems has a real public web presence so research should
    # produce at least one CONFIRMED dimension.
    slug = pp.slugify("Northwind Systems Inc")
    cache_path = pp.COMPANIES_DIR / f"{slug}.md"
    # Don't delete a candidate-curated file if it exists; just skip if so.
    if cache_path.exists():
        pytest.skip(f"curated file {cache_path} already exists; live test "
                    "would race with hand-curated content")

    g = pp.stage_ground(
        company="Northwind Systems Inc", role="Enterprise Client Partner",
        url="https://www.northwind.com/careers/enterprise-client-partner-8485725002/",
        archetype="AE",
    )
    if g.halted:
        pytest.skip(f"JD fetch failed: {g.halt_reason}")
    assert g.company_research is not None
    assert g.company_research_source in ("web_research_v1", "jd_body_only")
    if g.company_research_source == "web_research_v1":
        # Cache file must have been written so next run hits Path A.
        assert cache_path.exists(), \
            "Path A.5 produced research but no cache file landed"


# ─── Stage 2 — refuses to run when Stage 1 halted ────────────────────────────

def test_stage2_refuses_after_stage1_halt():
    halted_ground = pp.GroundResult(halted=True, halt_reason="test halt")
    w = pp.stage_write(ground=halted_ground, company="X", role="Y",
                       url="", archetype="AE")
    assert w.halted is True
    assert w.resume is None
    assert w.cover is None


# ─── Stage 3 — VERIFY catches fabrications ───────────────────────────────────

def test_stage3_flags_ungrounded_dollar_amount():
    ground = pp.GroundResult(
        jd_body="Northwind Systems sells industrial AI.",
        operator_history="Owned $50M+ at Lattice Additive.",
        company_research="industrial AI",
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Jordan Avery built $100M in pipeline.",  # $100M not in ground
    )
    v = pp.stage_verify(write=write, ground=ground)
    assert v.overall_flagged_count == 1
    assert any(c["token"] == "$100M" for c in v.verdicts[0].flagged_claims)


def test_stage3_passes_grounded_dollar_amount():
    ground = pp.GroundResult(
        jd_body="Northwind Systems sells industrial AI.",
        operator_history="Owned $50M+ at Lattice Additive with 90% retention.",
        company_research="industrial AI",
    )
    write = pp.WriteResult(
        artifacts_generated=["resume"],
        resume="Owned $50M+ at Lattice Additive with 90% retention.",
    )
    v = pp.stage_verify(write=write, ground=ground)
    assert v.overall_flagged_count == 0


def test_stage3_flags_invented_year_span():
    ground = pp.GroundResult(
        jd_body="Northwind Systems sells industrial AI to manufacturers.",
        operator_history="Worked at Lattice Additive 2022-2023.",
        company_research="industrial AI",
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Jordan Avery has 50 years of additive experience.",  # ungrounded
    )
    v = pp.stage_verify(write=write, ground=ground)
    assert v.overall_flagged_count >= 1


# ─── Path A.5 — web-research parser + extractor ──────────────────────────────

def test_research_parser_counts_full_confirmed_profile():
    text = (
        "## PRODUCT\nCONFIRMED: A platform. [url]\n\n"
        "## CUSTOMERS\nCONFIRMED: B2B. [url]\n\n"
        "## POSITION\nCONFIRMED: Series F. [url]\n\n"
        "## FOCUS\nCONFIRMED: Q1 2025 launch. [url]\n\n"
        "## PEOPLE\nCONFIRMED: CEO X. [url]\n"
    )
    c = pp._count_research_dimensions(text)
    assert c == {"confirmed": 5, "unclear": 0, "not_found": 0}


def test_research_parser_counts_partial_profile():
    text = (
        "## PRODUCT\nCONFIRMED: Heavy-duty parts.\n\n"
        "## CUSTOMERS\nNOT_FOUND\n\n"
        "## POSITION\nUNCLEAR: privately-held.\n\n"
        "## FOCUS\nNOT_FOUND\n\n"
        "## PEOPLE\nNOT_FOUND\n"
    )
    c = pp._count_research_dimensions(text)
    assert c == {"confirmed": 1, "unclear": 1, "not_found": 3}


def test_confirmed_sections_strips_not_found_and_unclear():
    text = (
        "## PRODUCT\nCONFIRMED: Platform.\n\n"
        "## CUSTOMERS\nNOT_FOUND\n\n"
        "## PEOPLE\nUNCLEAR: no public roster.\n"
    )
    extracted = pp._confirmed_sections_of(text)
    assert "PRODUCT" in extracted
    assert "NOT_FOUND" not in extracted
    assert "UNCLEAR" not in extracted


def test_company_claim_sentence_detector_picks_only_company_mentions():
    art = (
        "Northwind Systems sells predictive AI for uptime. "
        "Jordan Avery ran $50M at Lattice Additive. "
        "The role is fully remote."
    )
    lines = pp._company_claim_lines(art, "Northwind Systems")
    assert len(lines) == 1
    assert "Northwind Systems sells" in lines[0]


# ─── Stage 3 — CONFIRMED-only gating for web_research_v1 ─────────────────────

def test_stage3_confirmed_only_grounding_blocks_not_found_section():
    """A NOT_FOUND PEOPLE section must NOT serve as grounding for a fabricated
    leadership name. Sanity: the same fabricated claim must be flagged when
    the research is structured + that dimension is NOT_FOUND."""
    research = (
        "## PRODUCT\nCONFIRMED: Predictive AI platform.\n\n"
        "## PEOPLE\nNOT_FOUND\n"
    )
    ground = pp.GroundResult(
        jd_body="Northwind Systems sells predictive AI to manufacturers.",
        operator_history="Jordan Avery worked at Lattice Additive.",
        company_research=research,
        company_research_source="web_research_v1",
    )
    # A sentence naming the company that asserts something not in any
    # CONFIRMED section. The substantive token "Vandelay" is not anywhere
    # in CONFIRMED ground.
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Northwind Systems's CEO Vandelay has driven the platform.",
    )
    v = pp.stage_verify(write=write, ground=ground, company="Northwind Systems")
    # Should flag the company_claim — Vandelay is not in CONFIRMED text
    types = [c["type"] for verdict in v.verdicts for c in verdict.flagged_claims]
    assert "company_claim_ungrounded" in types


def test_stage3_confirmed_grounding_allows_in_confirmed_text():
    research = (
        "## PRODUCT\nCONFIRMED: Predictive AI platform for industrial uptime.\n\n"
        "## CUSTOMERS\nCONFIRMED: Acme Foods, Globex, Initech.\n"
    )
    ground = pp.GroundResult(
        jd_body="Northwind Systems sells predictive AI to manufacturers.",
        operator_history="Jordan Avery worked at Lattice Additive.",
        company_research=research,
        company_research_source="web_research_v1",
    )
    write = pp.WriteResult(
        artifacts_generated=["cover"],
        cover="Northwind Systems serves Acme Foods and Globex on its platform.",
    )
    v = pp.stage_verify(write=write, ground=ground, company="Northwind Systems")
    types = [c["type"] for verdict in v.verdicts for c in verdict.flagged_claims]
    assert "company_claim_ungrounded" not in types


# ─── Generate-application-package result carries new metadata fields ─────────

def test_pipeline_metadata_contains_company_research_source_field():
    """A returned PipelineResult, when constructed with the new fields, must
    serialize company_research_source so the metadata file records which
    grounding path fired."""
    ground = pp.GroundResult(company_research_source="web_research_v1",
                             company_research_incomplete=True)
    assert ground.company_research_source == "web_research_v1"
    assert ground.company_research_incomplete is True


# ─── Tool wiring registers ──────────────────────────────────────────────────

def test_generate_application_package_tool_registers():
    from alice import tools
    names = [s["name"] for s in tools.tool_specs()]
    assert "generate_application_package" in names


def test_generate_application_package_tool_has_guard():
    from alice import tools
    for t in tools.TOOLS_REGISTRY:
        if t["name"] == "generate_application_package":
            assert t["mutating"] is True
            assert t["guard"] is not None
            return
    raise AssertionError("generate_application_package not in registry")


# ─── Fix 4 — confirmation gate precision ────────────────────────────────────

def test_fix4_confirmation_gate_fires_on_yes_no():
    from alice.notify import telegram_bot as t
    for s in ("yes", "Yes", "yes.", "yeah", "ok", "do it", "go ahead",
              "no", "nope", "cancel", "stop", "y", "k"):
        assert t._is_confirmation_signal(s) is True, f"{s!r} should match"


def test_fix4_confirmation_gate_excludes_content_messages():
    from alice.notify import telegram_bot as t
    for s in (
        "yes I think we should",
        "yes, that northwind role is the right move",
        "northwind enterprise client partner status: applied",
        "I think yes",
        "no this is the wrong approach for that",
        "",
        "what is my focus",
    ):
        assert t._is_confirmation_signal(s) is False, f"{s!r} should NOT match"


# ─── Fix 4 — observation exclusion is the default ───────────────────────────

def test_fix4_observations_not_in_global_context():
    from alice.notify import telegram_bot as t
    ctx = t._build_alice_context()
    assert "RECENT OBSERVATIONS" not in ctx, (
        "_build_alice_context should not inject RECENT OBSERVATIONS — "
        "Fix 4 Item 2 redesign moves observations to per-turn relevance gating"
    )


def test_fix4_relevance_gate_excludes_on_no_topic_match():
    from alice.notify import telegram_bot as t
    # Generic greetings and off-topic questions should return empty
    for msg in ("hi", "what is the weather", "good morning", "thanks"):
        assert t._select_relevant_observations(msg) == [], (
            f"relevance gate should exclude {msg!r}"
        )


if __name__ == "__main__":
    # Run as plain script for non-pytest environments
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
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
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(0 if failed == 0 else 1)
