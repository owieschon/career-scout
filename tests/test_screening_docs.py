"""Bind the documented screening lanes to their implemented authority."""

import inspect
import re
from pathlib import Path

import pytest

from alice.pipeline import daily_delta


ROOT = Path(__file__).resolve().parents[1]


def _prose(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_screening_docs_match_rescue_cap_and_authority():
    readme = (ROOT / "README.md").read_text()
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    readme_prose = _prose(readme)
    architecture_prose = _prose(architecture)

    assert f"default is `{daily_delta.RESCUE_SAMPLE_MAX_DEFAULT}`" in readme_prose
    assert (
        f"default cap is `{daily_delta.RESCUE_SAMPLE_MAX_DEFAULT}`"
        in architecture_prose
    )
    for document in (readme_prose, architecture_prose):
        assert f"hard maximum is `{daily_delta.RESCUE_SAMPLE_MAX_HARD}`" in document
        assert "stops" in document and "source fetches" in document
        assert "configured value" in document
        assert "effective value" in document
        assert "configuration source" in document or "explicit configuration" in document

    for document in (readme, architecture):
        assert "ALICE_DROPPED_SAMPLE_MAX" in document
        assert "location_travel_gate" in document
    assert "not inserted into the main shortlist or ledger" in architecture_prose
    assert "ALICE_FIT_JUDGE=0" in readme
    assert "ALICE_FIT_JUDGE=0" in architecture

    run_source = inspect.getsource(daily_delta.run)
    assert "resolve_rescue_sample_limit" in run_source
    assert "[rescue-config]" in run_source
    assert "_apply_ledger(new_qualified)" in run_source
    assert "_write_output(today, first, new_qualified" in run_source
    assert "new_qualified.extend(rescue_candidates)" not in run_source
    assert "new_qualified += rescue_candidates" not in run_source


def test_screening_docs_split_config_values_from_engine_semantics():
    readme = _prose((ROOT / "README.md").read_text())
    architecture = _prose((ROOT / "docs" / "ARCHITECTURE.md").read_text())
    judge_source = _prose(inspect.getsource(daily_delta.fit_judge.build_judge_system))

    assert "Profile values are versioned data; judgment semantics are engine code" in readme
    assert "fixed exceptions" in readme
    assert "fixed exceptions" in architecture
    assert "stable judgment semantics encoded below" in judge_source
    assert "built entirely from the TOML" not in readme
    assert "derived ENTIRELY from the config" not in judge_source


def test_screening_docs_name_the_raw_messages_api_probe_exception():
    readme = _prose((ROOT / "README.md").read_text())
    architecture = _prose((ROOT / "docs" / "ARCHITECTURE.md").read_text())

    for document in (readme, architecture):
        assert "main application path" in document
        assert "validate_messages_api.py" in document
        assert "do not enter the cost log" in document
        assert "Every model call" not in document
        assert "Every model request" not in document


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (20, 20, "default")),
        ("7", (7, 7, "configured")),
        ("0", (0, 0, "configured")),
        ("100", (100, 100, "configured")),
    ],
)
def test_rescue_sample_limit_is_bounded(raw, expected):
    assert daily_delta.resolve_rescue_sample_limit(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "not-a-number", "101", "1000000"])
def test_invalid_rescue_sample_limit_fails_closed(raw):
    with pytest.raises(ValueError, match="ALICE_DROPPED_SAMPLE_MAX"):
        daily_delta.resolve_rescue_sample_limit(raw)


def test_invalid_rescue_config_stops_before_source_calls(monkeypatch, tmp_path):
    source_calls = []
    monkeypatch.setenv("ALICE_DROPPED_SAMPLE_MAX", "101")
    monkeypatch.setattr(daily_delta, "pull_ats", lambda: source_calls.append("ats"))

    with pytest.raises(ValueError, match="ALICE_DROPPED_SAMPLE_MAX"):
        daily_delta.run(dry_run=True, state_path=tmp_path / "state.json")

    assert source_calls == []


def test_rescue_stats_are_complete_when_inactive_or_empty():
    stats = daily_delta._initial_rescue_stats(
        {
            "active": False,
            "configured": 0,
            "configuration_source": "configured",
            "effective": 0,
            "hard_max": daily_delta.RESCUE_SAMPLE_MAX_HARD,
        }
    )

    assert stats["rescue_sample_active"] is False
    for key in (
        "fit_judge_attempted",
        "fit_judge_completed",
        "fit_judge_failed",
        "fit_judged",
        "rescue_candidates",
        "rescue_dropped_sampled",
        "rescue_dropped_total",
        "rescue_judge_attempted",
        "rescue_judge_completed",
        "rescue_judge_failed",
        "rescue_judged",
        "rescue_unjudged_no_body",
        "skip_reason_write_failures",
    ):
        assert stats[key] == 0

    assert stats["fit_judge_failure_classes"] == []
    assert stats["rescue_judge_failure_classes"] == []


def test_judge_batch_failure_records_zero_completed_and_nonzero_failed(monkeypatch):
    records = [
        {"id": "one", "body": "First listing"},
        {"id": "two", "body": "Second listing"},
    ]
    stats = daily_delta._initial_rescue_stats({"active": True})

    def fail_batch(_records, *, drop_not_fit):
        assert drop_not_fit is False
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(daily_delta.fit_judge, "judge_survivors", fail_batch)

    daily_delta._judge_batch(records, stats, lane="fit_judge")

    assert stats["fit_judge_attempted"] == 2
    assert stats["fit_judge_completed"] == 0
    assert stats["fit_judge_failed"] == 2
    assert stats["fit_judged"] == 0
    assert stats["fit_judge_failure_classes"] == ["RuntimeError", "batch_error"]
    assert stats["fit_judge_error"] == "RuntimeError: provider unavailable"
    assert {record["fit_verdict"] for record in records} == {
        "UNJUDGED-JUDGE-ERROR"
    }


def test_skip_reason_write_failure_is_visible(capsys):
    stats = {"skip_reason_write_failures": 0}

    def fail_write(_source, _ext_id, _reason):
        raise OSError("state is read-only")

    recorded = daily_delta._record_skip_reason(
        fail_write,
        {"source": "fixture", "ext_id": "job-7"},
        "role_skip",
        stats,
    )

    assert recorded is False
    assert stats["skip_reason_write_failures"] == 1
    assert "[skip-reason-write-error]" in capsys.readouterr().err


def test_readme_does_not_collapse_primary_and_recall_lanes():
    readme = (ROOT / "README.md").read_text()

    assert "only survivors reach the model" not in readme
    assert "recall lane" in readme
    assert "review candidate, not an automatic admission" in _prose(readme)


def test_prep_docs_match_verifier_authority():
    readme = _prose((ROOT / "README.md").read_text())
    architecture = _prose((ROOT / "docs" / "ARCHITECTURE.md").read_text())

    assert "ordinary findings do not withhold drafts" in readme
    assert "findings do not withhold the generated drafts in v1" in architecture
    assert "banned-framing or anonymization breach" in architecture

    from alice.pipeline import prep_pipeline

    pipeline_source = inspect.getsource(prep_pipeline.run_pipeline)
    assert "banned = verify.banned_framing_count > 0" in pipeline_source
    assert "if banned:" in pipeline_source
    assert '(pkg_dir / "resume-draft.md").write_text' in pipeline_source
