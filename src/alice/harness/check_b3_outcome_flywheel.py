"""B3 (outcome-feedback flywheel) regression — the no-op-when-off wiring that
links a prediction's span to its real outcome. Offline + hermetic: tests the
PLUMBING (store round-trip, no-op gating, the outcome map). The live annotation
(a real Phoenix span_annotation on status-change) is HELD until tracing is
greenlit — this suite proves it stays DORMANT and safe while off."""
import sys
from pathlib import Path
from alice.observability import telemetry


def test_is_on_false_when_tracing_off():
    assert telemetry.is_on() is False  # tracing off by default in tests


def test_annotate_outcome_is_noop_when_off():
    # Must not raise and must do nothing when tracing is off (the live case now).
    assert telemetry.annotate_outcome("greenhouse:arize:1", "offer") is None


def test_prediction_span_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_PRED_SPANS_PATH", str(tmp_path / "pred.jsonl"))
    telemetry.record_prediction_span("jk-1", "span-abc", task="score")
    telemetry.record_prediction_span("jk-1", "span-def", task="score")  # latest wins
    telemetry.record_prediction_span("jk-2", "span-xyz")
    assert telemetry._latest_prediction_span("jk-1") == "span-def"
    assert telemetry._latest_prediction_span("jk-2") == "span-xyz"
    assert telemetry._latest_prediction_span("absent") is None


def test_record_is_noop_on_missing_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "_PRED_SPANS_PATH", str(tmp_path / "pred.jsonl"))
    telemetry.record_prediction_span("jk", None)    # no span_id -> no write
    telemetry.record_prediction_span(None, "span")  # no job_key -> no write
    assert telemetry._latest_prediction_span("jk") is None


def test_span_id_of_noop_span_is_none():
    # A no-op span (tracing off) has no real span context -> None, never raises.
    assert telemetry.span_id_of(telemetry._NoopSpan()) is None


def test_outcome_map_grades_by_reality():
    assert telemetry._OUTCOME_MAP["offer"] == ("offer", 1.0)
    assert telemetry._OUTCOME_MAP["interviewing"][1] == 1.0
    assert telemetry._OUTCOME_MAP["rejected"][1] == 0.0
    assert telemetry._OUTCOME_MAP["submitted"][0] == "applied"
