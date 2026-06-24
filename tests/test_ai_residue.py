from pathlib import Path
from alice.pipeline import prep_pipeline as p


def test_residue_flagged_on_live_verify_path(monkeypatch):
    # Consulting-speak in a generated artifact is caught by the Layer-1 voice
    # gate wired into stage_verify (the live path), not just offline evals.
    w = p.WriteResult(
        cover="I am passionate about helping you leverage synergies at scale.",
        artifacts_generated=["cover"],
    )
    g = p.GroundResult(jd_body="x", operator_history="y")
    r = p.stage_verify(write=w, ground=g, company="Acme")
    assert r.voice_residue_warnings >= 1
    assert r.banned_framing_count == 0   # residue is a warning, never a withhold


def test_clean_prose_has_no_residue():
    w = p.WriteResult(
        cover="I cut order-entry time 40% by shipping a model your reps actually use.",
        artifacts_generated=["cover"],
    )
    g = p.GroundResult(jd_body="x", operator_history="y")
    r = p.stage_verify(write=w, ground=g, company="Acme")
    assert r.voice_residue_warnings == 0
