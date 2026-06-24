"""Integrity gate: stage_verify must catch banned framing / anonymization
breaches (CLAUDE.md HARD rules) that grounding alone does NOT — a confabulated
"design-partner relationship with one manufacturer" can be 95.7% grounded yet
be a hard violation. These must force a withhold, never ship.
Pure (no LLM)."""
from pathlib import Path

from alice.pipeline import prep_pipeline as p


# ── unit: the detector ────────────────────────────────────────────────────────
BANNED = [
    "Cadence Analytics has a design-partner relationship with one manufacturer.",  # the HANDOFF confabulation
    "I built this while at Ironclad Industrial.",                       # literal name
    "Ironclad Industrial is a Cadence Analytics pilot customer.",       # name + framing
    "Cadence Analytics's first customer is a heavy-duty parts manufacturer.",  # mfr as Cadence customer
]
ALLOWED = [
    "I built Cadence Analytics, inspired by a heavy-duty parts manufacturer I worked with.",   # inspiration, not customer
    "Your manufacturing customers expect fast turnaround; you serve manufacturers nationwide.",  # applying TO a mfr
    "I worked as a design partner with the engineering team.",          # generic design partner, no mfr
    "Cadence Analytics is a multi-agent platform where ML takes the guesswork out of revenue signals.",  # accurate Cadence
]


def test_detector_flags_banned():
    for text in BANNED:
        assert p._banned_framing_hits(text), f"should be flagged: {text!r}"


def test_detector_allows_legitimate():
    for text in ALLOWED:
        assert not p._banned_framing_hits(text), f"should NOT be flagged: {text!r}"


def test_anonymization_breach_typed_distinctly():
    hits = p._banned_framing_hits("My work at Ironclad Industrial shaped Cadence Analytics.")
    assert any(h["type"] == "anonymization_breach" for h in hits)


# ── integration: stage_verify surfaces it + blocks ─────────────────────────────
def test_stage_verify_counts_banned_and_fails():
    write = p.WriteResult(
        cover="Cadence Analytics has a design-partner relationship with one manufacturer.",
        artifacts_generated=["cover"],
    )
    ground = p.GroundResult(jd_body="x", operator_history="y")
    result = p.stage_verify(write=write, ground=ground, company="Acme")
    assert result.banned_framing_count >= 1
    assert not result.passed()  # banned framing fails verify even if grounded


def test_stage_verify_clean_draft_no_banned():
    write = p.WriteResult(
        cover="I built Cadence Analytics, a multi-agent revenue platform, after seeing the problem firsthand.",
        artifacts_generated=["cover"],
    )
    ground = p.GroundResult(jd_body="x", operator_history="y")
    result = p.stage_verify(write=write, ground=ground, company="Acme")
    assert result.banned_framing_count == 0


# ── the catch at block-time: drafts are genuinely WITHHELD from disk ──────────
def test_run_pipeline_withholds_drafts_on_banned_framing(tmp_path, monkeypatch):
    """End-to-end: a banned draft must never reach disk. Stub the two LLM stages
    so a banned cover flows into VERIFY, then assert run_pipeline writes BLOCKED.md
    and NOT the shippable drafts."""
    monkeypatch.setattr(p, "APPS_DIR", tmp_path)
    monkeypatch.setattr(p, "stage_ground",
        lambda **kw: p.GroundResult(jd_body="x" * 300, operator_history="y" * 300))
    monkeypatch.setattr(p, "stage_write",
        lambda **kw: p.WriteResult(
            resume="Built revenue ML at a heavy-duty exhaust manufacturer.",
            cover="Cadence Analytics has a design-partner relationship with one manufacturer.",
            artifacts_generated=["resume", "cover"]))

    result = p.run_pipeline(company="Acme", role="Senior AE",
                            url="http://x", archetype="AE")

    pkg = tmp_path / result.slug
    assert result.halted_at_stage == "VERIFY"
    assert result.verify.banned_framing_count >= 1
    assert (pkg / "BLOCKED.md").exists()
    # the shippable drafts must NOT exist
    assert not (pkg / "resume-draft.md").exists()
    assert not (pkg / "cover-letter-draft.md").exists()
    assert not (pkg / "outreach-targets.md").exists()
    # metadata records the withhold for the audit trail
    import json
    meta = json.loads((pkg / ".pipeline-metadata.json").read_text())
    assert meta["verify"]["drafts_withheld"] is True


def test_run_pipeline_writes_drafts_when_clean(tmp_path, monkeypatch):
    """Inverse: a clean draft DOES reach disk (the gate is not over-blocking)."""
    monkeypatch.setattr(p, "APPS_DIR", tmp_path)
    monkeypatch.setattr(p, "stage_ground",
        lambda **kw: p.GroundResult(jd_body="x" * 300, operator_history="y" * 300))
    monkeypatch.setattr(p, "stage_write",
        lambda **kw: p.WriteResult(
            resume="Built Cadence Analytics, a multi-agent revenue platform.",
            cover="I'd bring 10 years of B2B revenue experience to this role.",
            artifacts_generated=["resume", "cover"]))

    result = p.run_pipeline(company="Acme", role="Senior AE",
                            url="http://x", archetype="AE")
    pkg = tmp_path / result.slug
    assert result.halted_at_stage != "VERIFY"
    assert (pkg / "resume-draft.md").exists()
    assert not (pkg / "BLOCKED.md").exists()
