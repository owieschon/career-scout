import json
from pathlib import Path


from alice.notify import alert_routing


def test_alert_routing_file_route(tmp_path, monkeypatch):
    monkeypatch.setattr(alert_routing, "ALERT_LOG", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(alert_routing, "_cfg", lambda: {})

    result = alert_routing.route_alert(
        severity="critical",
        title="test",
        summary="summary",
        dry_run=True,
    )

    assert result["ok"] is True
    rows = [json.loads(line) for line in (tmp_path / "alerts.jsonl").read_text().splitlines()]
    assert rows[0]["title"] == "test"


def test_alert_routing_health_reports_external_route(monkeypatch):
    monkeypatch.setattr(alert_routing, "_cfg", lambda: {"ALICE_ALERT_WEBHOOK_URL": "https://example.invalid/hook"})

    health = alert_routing.healthcheck()

    assert "webhook" in health["routes"]
    assert health["external_route_configured"] is True
