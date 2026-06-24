import json
from pathlib import Path


from alice.observability import observability_artifacts as artifacts


def test_persist_healthcheck_and_incident(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "INCIDENT_DIR", tmp_path / "incidents")
    monkeypatch.setattr(artifacts, "LATEST_HEALTHCHECK", tmp_path / "latest.json")

    report = {
        "ts": "2026-06-03T12:00:00",
        "run_key": "rk",
        "checks": [{"name": "x", "status": "fail", "detail": "bad", "data": {"a": 1}}],
    }

    artifacts.persist_healthcheck_report(report)
    incident = artifacts.create_incident_report(report)

    assert json.loads((tmp_path / "latest.json").read_text())["run_key"] == "rk"
    assert incident is not None
    assert "Alice Incident rk" in incident.read_text()


def test_render_prometheus_contains_core_metrics():
    metrics = {
        "runtime": {
            "metrics": {
                "services": {"telegram": {"rss_mb": 12.5, "cpu_pct": 1.0, "runs": 2}},
                "disk": {"free_gb": 99},
            }
        },
        "latest_healthcheck": {"failed_checks": 0, "warn_checks": 1},
        "slo": {"pass_rate": 1.0},
        "judged_eval": {"summary": {"pass_rate": 0.8, "failed": 1}},
        "enterprise_readiness": {"score_local_single_user": 10, "blocking_controls": []},
        "alerting": {"external_route_configured": True},
        "cost_today_usd": 0.123,
    }

    text = artifacts.render_prometheus(metrics)

    assert "alice_healthcheck_failed_checks 0" in text
    assert "alice_telegram_rss_mb 12.5" in text
    assert "alice_cost_today_usd 0.123" in text
    assert "alice_judged_eval_pass_rate 0.8" in text
    assert "alice_alert_external_routes_configured 1" in text
    assert "alice_enterprise_readiness_local_score 10" in text
    assert "alice_enterprise_readiness_blocking_controls 0" in text


def test_retention_dry_run_counts_old_rows(tmp_path, monkeypatch):
    target = tmp_path / "log.jsonl"
    target.write_text(
        json.dumps({"ts": "2020-01-01T00:00:00", "text": "old"}) + "\n"
        + json.dumps({"ts": "2999-01-01T00:00:00", "text": "new"}) + "\n"
    )
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path / "obs")
    monkeypatch.setattr(artifacts, "RETENTION_JSON", tmp_path / "obs" / "retention.json")
    monkeypatch.setattr(artifacts, "RETENTION_MD", tmp_path / "obs" / "retention.md")
    monkeypatch.setattr(artifacts, "RETENTION_TARGETS", {
        target: {"days": 30, "kind": "jsonl", "ts_key": "ts", "raw_text": True}
    })

    report = artifacts.enforce_retention(dry_run=True)

    assert report["targets"][0]["dropped"] == 1
    assert len(target.read_text().splitlines()) == 2


def test_eval_summary_counts_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "EVAL_JSON", tmp_path / "eval.json")
    monkeypatch.setattr(artifacts, "EVAL_MD", tmp_path / "eval.md")

    summary = artifacts.generate_eval_summary()

    assert summary["cases"] > 0
    assert (tmp_path / "eval.json").exists()


def test_judged_eval_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "JUDGED_EVAL_JSON", tmp_path / "judged.json")
    monkeypatch.setattr(artifacts, "JUDGED_EVAL_MD", tmp_path / "judged.md")

    payload = artifacts.generate_judged_eval(enabled=False)

    assert payload["summary"]["status"] == "skipped"
    assert payload["summary"]["cases_run"] == 0
    assert (tmp_path / "judged.json").exists()


def test_judged_eval_runs_cases_with_stubbed_judge(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "JUDGED_EVAL_JSON", tmp_path / "judged.json")
    monkeypatch.setattr(artifacts, "JUDGED_EVAL_MD", tmp_path / "judged.md")
    monkeypatch.setattr(artifacts, "_judge_eval_case", lambda case: {
        "id": case["id"],
        "layer": case["metadata"]["layer"],
        "status": "pass",
        "reason": "ok",
    })

    payload = artifacts.generate_judged_eval(enabled=True, max_cases=2)

    assert payload["summary"]["status"] == "pass"
    assert payload["summary"]["cases_run"] == 2
    assert payload["summary"]["pass_rate"] == 1.0


def test_behavior_regression_skips_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "BEHAVIOR_REGRESSION_JSON", tmp_path / "behavior.json")
    monkeypatch.setattr(artifacts, "BEHAVIOR_REGRESSION_MD", tmp_path / "behavior.md")

    payload = artifacts.generate_behavior_regression(enabled=False)

    assert payload["status"] == "skipped"
    assert payload["cases"] == 0
    assert (tmp_path / "behavior.json").exists()


def test_behavior_regression_runs_with_stubbed_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "BEHAVIOR_REGRESSION_JSON", tmp_path / "behavior.json")
    monkeypatch.setattr(artifacts, "BEHAVIOR_REGRESSION_MD", tmp_path / "behavior.md")

    from alice.ops import alice_behavior_regression
    monkeypatch.setattr(alice_behavior_regression, "run_cases", lambda max_cases=None: {
        "ts": "2026-06-03T00:00:00",
        "cases": 1,
        "passed": 1,
        "failed": 0,
        "pass_rate": 1.0,
        "status": "pass",
        "results": [],
    })

    payload = artifacts.generate_behavior_regression(enabled=True)

    assert payload["status"] == "pass"
    assert payload["pass_rate"] == 1.0


def test_enterprise_readiness_separates_local_from_saas(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "OBS_DIR", tmp_path)
    monkeypatch.setattr(artifacts, "LATEST_HEALTHCHECK", tmp_path / "latest.json")
    monkeypatch.setattr(artifacts, "SLO_JSON", tmp_path / "slo.json")
    monkeypatch.setattr(artifacts, "EVAL_JSON", tmp_path / "eval.json")
    monkeypatch.setattr(artifacts, "JUDGED_EVAL_JSON", tmp_path / "judged.json")
    monkeypatch.setattr(artifacts, "BEHAVIOR_REGRESSION_JSON", tmp_path / "behavior.json")
    monkeypatch.setattr(artifacts, "RETENTION_JSON", tmp_path / "retention.json")
    monkeypatch.setattr(artifacts, "ESCALATION_POLICY_JSON", tmp_path / "escalation.json")
    monkeypatch.setattr(artifacts, "AUDIT_EVIDENCE_JSON", tmp_path / "audit.json")
    monkeypatch.setattr(artifacts, "ENTERPRISE_READINESS_JSON", tmp_path / "enterprise.json")
    monkeypatch.setattr(artifacts, "ENTERPRISE_READINESS_MD", tmp_path / "enterprise.md")
    monkeypatch.setattr(artifacts, "METRICS_PROM", tmp_path / "metrics.prom")
    monkeypatch.setattr(artifacts, "PROMETHEUS_RULES", tmp_path / "alerts.yml")
    monkeypatch.setattr(artifacts, "TIME_COST_LOG", tmp_path / "cost.jsonl")

    (tmp_path / "metrics.prom").write_text("alice_healthcheck_failed_checks 0\n")
    (tmp_path / "alerts.yml").write_text("groups: []\n")
    (tmp_path / "cost.jsonl").write_text("{}\n")
    (tmp_path / "latest.json").write_text(json.dumps({
        "checks": [
            {"name": "sentry.remote_readback", "status": "pass"},
            {"name": "langsmith.span_delivery", "status": "pass"},
            {"name": "local.cost_log", "status": "pass"},
        ]
    }))
    (tmp_path / "slo.json").write_text(json.dumps({"pass_rate": 1.0}))
    (tmp_path / "eval.json").write_text(json.dumps({"status": "pass"}))
    (tmp_path / "judged.json").write_text(json.dumps({"summary": {"status": "pass"}}))
    (tmp_path / "behavior.json").write_text(json.dumps({"status": "pass"}))
    (tmp_path / "retention.json").write_text(json.dumps({"targets": [{"before": 1, "unknown_ts": 0}]}))
    (tmp_path / "escalation.json").write_text(json.dumps({"external_route_configured": True}))
    (tmp_path / "audit.json").write_text(json.dumps({"status": "pass"}))

    payload = artifacts.generate_enterprise_readiness()

    assert payload["local_status"] == "pass"
    assert payload["score_local_single_user"] == 10
    assert payload["enterprise_saas_status"] == "external_required"
    assert payload["external_required"]
