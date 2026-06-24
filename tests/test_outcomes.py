"""Outcome loop store (docs/FIT_STRATEGY_SPINE.md §5): decomposed funnel + terminal
reason, HITL-sourced. Pure."""
from pathlib import Path

from alice.persistence import outcomes


def test_record_decomposes_funnel(tmp_path, monkeypatch):
    monkeypatch.setattr(outcomes, "PATH", str(tmp_path / "outcomes.jsonl"))
    r = outcomes.record(company="Acme", role="AE", reached="interview",
                        terminal="no_offer", terminal_reason="performance",
                        channel="cold_email_decision_maker")
    # funnel decomposed: everything up to 'interview' True, 'offer' False
    assert r["funnel"] == {"applied": True, "response": True, "screen": True,
                           "interview": True, "offer": False}
    assert r["reached"] == "interview"
    assert outcomes.load(str(tmp_path / "outcomes.jsonl"))[0]["company"] == "Acme"


def test_intermediate_positive_is_not_a_fit_negative(tmp_path, monkeypatch):
    """An interview-secured / no-offer-on-performance is a POSITIVE fit+channel
    signal; the terminal reason attributes the 'no' away from fit."""
    monkeypatch.setattr(outcomes, "PATH", str(tmp_path / "o.jsonl"))
    r = outcomes.record(company="X", role="AE", reached="interview",
                        terminal="no_offer", terminal_reason="performance")
    assert r["funnel"]["interview"] is True          # reached interview = mutual-fit confirmed
    assert r["terminal_reason"] == "performance"     # not fit_reject
    assert r["terminal_reason"] != "fit_reject"


def test_validation():
    import pytest
    with pytest.raises(ValueError):
        outcomes.record(company="X", role="Y", reached="bogus_stage")
    with pytest.raises(ValueError):
        outcomes.record(company="X", role="Y", reached="applied", terminal_reason="bogus")



# reconciliation: decomposed outcome -> Phoenix status, preserving the interview-vs-fit lesson
def test_phoenix_status_preserves_decomposition():
    from alice.persistence import outcomes
    # interview reached, lost on performance -> ADVANCED (fit was right), NOT rejected
    assert outcomes._phoenix_status("interview", "performance") == "interviewing"
    assert outcomes._phoenix_status("interview", "comp") == "interviewing"
    # only an actual fit-reject grades as rejected
    assert outcomes._phoenix_status("interview", "fit_reject") == "not a fit"
    assert outcomes._phoenix_status("offer", None) == "offer"
    assert outcomes._phoenix_status("applied", None) == "submitted"


def test_record_feeds_flywheel_noop_when_tracing_off(tmp_path, monkeypatch):
    from alice.persistence import outcomes
    monkeypatch.setattr(outcomes, "PATH", str(tmp_path / "o.jsonl"))
    # tracing is off in tests -> annotate_outcome is a no-op; record must not raise
    r = outcomes.record(company="Acme", role="AE", reached="interview",
                        terminal="no_offer", terminal_reason="performance")
    assert r["reached"] == "interview"
