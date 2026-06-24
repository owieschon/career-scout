"""Durable starvation/over-pass detector for the sourcing gates, so
'0 qualified' is never indistinguishable from 'a gate is silently starving the
pipeline'. Aggregate signal (the per-row trail is seen_jobs.skip_reason)."""
from pathlib import Path

from alice.pipeline import daily_delta as dd


def test_flags_single_gate_starvation():
    a = dd._gate_anomalies({"new_ids": 100, "domain_skip": 95, "qualified": 2})
    assert any("off-domain" in s and "95" in s for s in a)


def test_flags_over_pass():
    a = dd._gate_anomalies({"new_ids": 100, "qualified": 95})
    assert any("under-filtering" in s for s in a)


def test_flags_zero_qualified():
    a = dd._gate_anomalies({"new_ids": 50, "domain_skip": 30, "qualified": 0})
    assert any("0 qualified" in s for s in a)


def test_healthy_run_has_no_anomaly():
    a = dd._gate_anomalies({"new_ids": 100, "domain_skip": 40, "remote_skip": 20,
                            "killed": 10, "travel_skip": 5, "qualified": 25})
    assert a == []


def test_small_batch_not_judged():
    # below min_denom: a quiet run must not raise a false starvation alarm
    a = dd._gate_anomalies({"new_ids": 10, "domain_skip": 10, "qualified": 0})
    assert a == []
