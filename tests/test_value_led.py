from pathlib import Path
from alice.pipeline import prep_pipeline as p


def test_flags_tool_led_opening():
    h = p._leads_with_tools("Deployed XGBoost churn model (0.99 AUC) and Random Survival Forest.", "resume")
    assert h and h["type"] == "leads_with_tools" and "xgboost" in h["token"].lower()


def test_value_led_opening_passes():
    h = p._leads_with_tools("Surfaced $500K in at-risk revenue, then shipped the XGBoost model behind it.", "resume")
    assert h is None  # value leads, tool is credibility


def test_only_resume_and_cover_checked():
    assert p._leads_with_tools("Built with React and Supabase.", "strategy") is None
    assert p._leads_with_tools("Built with React and Supabase.", "cover") is not None


def test_warning_not_a_withhold(monkeypatch):
    w = p.WriteResult(resume="Deployed XGBoost and React + Supabase across the stack.", artifacts_generated=["resume"])
    g = p.GroundResult(jd_body="x", operator_history="y")
    r = p.stage_verify(write=w, ground=g, company="Acme")
    assert r.value_led_warnings >= 1
    assert r.banned_framing_count == 0   # not a banned-framing hit -> no withhold
