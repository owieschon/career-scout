import sys
from pathlib import Path
import types


from alice.observability import observability_healthcheck as healthcheck


def test_langsmith_run_id_for_span_zero_pads_to_uuid():
    assert (
        healthcheck.langsmith_run_id_for_span("42f1400b690e482e")
        == "00000000-0000-0000-42f1-400b690e482e"
    )


def test_span_id_from_phoenix_span_handles_dict_context():
    span = {"context": {"span_id": "abc123"}}

    assert healthcheck._span_id_from_phoenix_span(span) == "abc123"


def test_span_id_from_phoenix_span_handles_phoenix_short_hex():
    span = {"context": {"span_id": "08a145801b00b082"}}

    assert healthcheck._span_id_from_phoenix_span(span).zfill(32) == "000000000000000008a145801b00b082"


def test_check_records_do_not_include_none_values():
    check = healthcheck._check("x", "pass", event_id=None, present=True)

    assert check.data == {"present": True}


def test_parse_naive_timestamps_as_local_aware():
    parsed = healthcheck._parse_ts("2026-06-03T08:05:22")

    assert parsed is not None
    assert parsed.tzinfo is not None


def test_failing_checks_filters_only_failures():
    checks = [
        healthcheck._check("ok", "pass"),
        healthcheck._check("skip", "warn"),
        healthcheck._check("bad", "fail"),
    ]

    assert [check.name for check in healthcheck.failing_checks(checks)] == ["bad"]


def test_sentry_readback_warns_without_token(monkeypatch):
    monkeypatch.setattr("alice.jobcfg", types.SimpleNamespace(load=lambda: {"SENTRY_DSN": "https://x@y/123"}), raising=False)
    monkeypatch.setattr("alice.observability.sentry_query", types.SimpleNamespace(dsn_project_id=lambda dsn: "123"), raising=False)
    check = healthcheck.check_sentry_readback("rk")

    assert check.name == "sentry.remote_readback"
    assert check.status == "warn"
    assert check.data["dsn_project_id"] == "123"


def test_sentry_readback_passes_when_canary_found(monkeypatch):
    fake_query = types.SimpleNamespace(
        discover_project=lambda token, cfg: ("org", "project", {"id": "1"}),
        query_events=lambda token, org, project, query, since_minutes, limit: [{"eventID": "evt", "title": "alice.observability.canary rk"}],
    )
    monkeypatch.setattr("alice.jobcfg", types.SimpleNamespace(load=lambda: {"SENTRY_AUTH_TOKEN": "tok"}), raising=False)
    monkeypatch.setattr("alice.observability.sentry_query", fake_query, raising=False)
    check = healthcheck.check_sentry_readback("rk")

    assert check.status == "pass"
    assert check.data["event_id"] == "evt"


def test_posthog_warns_when_not_enabled(monkeypatch):
    fake = types.SimpleNamespace(status=lambda: {"enabled_flag": False, "api_key_configured": False})
    monkeypatch.setattr("alice.observability.product_analytics", fake, raising=False)
    check = healthcheck.check_posthog()

    assert check.name == "posthog.product_analytics"
    assert check.status == "warn"


def test_posthog_passes_when_capture_emits(monkeypatch):
    fake = types.SimpleNamespace(
        status=lambda: {"enabled_flag": True, "api_key_configured": True, "sdk_importable": True, "live": True},
        init=lambda component: True,
        capture=lambda event, props: True,
        flush=lambda: True,
    )
    monkeypatch.setattr("alice.observability.product_analytics", fake, raising=False)
    check = healthcheck.check_posthog()

    assert check.name == "posthog.product_analytics"
    assert check.status == "pass"


def test_posthog_readback_warns_without_api_credentials(monkeypatch):
    fake = types.SimpleNamespace(
        status=lambda: {"personal_api_key_configured": False, "project_id_configured": False},
    )
    monkeypatch.setattr("alice.observability.product_analytics", fake, raising=False)
    check = healthcheck.check_posthog_readback()

    assert check.name == "posthog.remote_readback"
    assert check.status == "warn"


def test_infra_runtime_maps_runtime_metrics_checks(monkeypatch):
    fake = types.SimpleNamespace(summary=lambda: {
        "checks": [{
            "name": "infra.telegram_running",
            "status": "pass",
            "detail": "ok",
            "data": {"pid": 123},
        }]
    })
    monkeypatch.setattr("alice.observability.runtime_metrics", fake, raising=False)
    checks = healthcheck.check_infra_runtime()

    assert checks[0].name == "infra.telegram_running"
    assert checks[0].status == "pass"
    assert checks[0].data["pid"] == 123
