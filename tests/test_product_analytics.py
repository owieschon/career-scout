import sys
import types
from pathlib import Path


from alice.observability import product_analytics


def test_status_reports_disabled_without_flag(monkeypatch):
    monkeypatch.setattr(product_analytics, "_load_cfg", lambda: {})

    status = product_analytics.status()

    assert status["enabled_flag"] is False
    assert status["live"] is False


def test_init_does_not_enable_without_key(monkeypatch):
    monkeypatch.setattr(product_analytics, "_INITIALIZED", False)
    monkeypatch.setattr(product_analytics, "_ENABLED", False)
    monkeypatch.setattr(product_analytics, "_CLIENT", None)
    monkeypatch.setattr(product_analytics, "_load_cfg", lambda: {"ALICE_POSTHOG": "1"})

    assert product_analytics.init("test") is False
    assert product_analytics.status()["live"] is False


def test_capture_sanitizes_raw_text_and_tokens(monkeypatch):
    captured = {}

    def fake_direct(event, properties, distinct_id):
        captured.update({"event": event, "properties": properties, "distinct_id": distinct_id})
        return True

    monkeypatch.setattr(product_analytics, "_ENABLED", True)
    monkeypatch.setattr(product_analytics, "_direct_capture", fake_direct)

    ok = product_analytics.capture(
        "evt",
        {
            "message_text": "raw chat text",
            "tool_name": "query_sentry_events",
            "long_value": "x" * 120,
        },
    )

    assert ok is True
    props = captured["properties"]
    assert props["message_text"] == "[REDACTED]"
    assert props["tool_name"] == "query_sentry_events"
    assert props["long_value"] == "[REDACTED_LONG_TEXT]"


def test_init_uses_posthog_client_when_enabled(monkeypatch):
    calls = {}

    class FakePosthog:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def capture(self, **kwargs):
            calls["capture"] = kwargs
            return "event-id"

    fake_module = types.SimpleNamespace(Posthog=FakePosthog)
    monkeypatch.setitem(sys.modules, "posthog", fake_module)
    monkeypatch.setattr(product_analytics, "_INITIALIZED", False)
    monkeypatch.setattr(product_analytics, "_ENABLED", False)
    monkeypatch.setattr(product_analytics, "_CLIENT", None)
    monkeypatch.setattr(product_analytics, "_load_cfg", lambda: {
        "ALICE_POSTHOG": "1",
        "POSTHOG_API_KEY": "phc_test",
        "POSTHOG_HOST": "https://us.i.posthog.com",
    })
    monkeypatch.setattr(product_analytics, "_direct_capture", lambda event, properties, distinct_id: True)

    assert product_analytics.init("test") is True
    assert calls["init"]["project_api_key"] == "phc_test"
    assert calls["init"]["privacy_mode"] is True


def test_query_events_reports_missing_readback_credentials(monkeypatch):
    monkeypatch.setattr(product_analytics, "_load_cfg", lambda: {"POSTHOG_API_KEY": "phc_test"})

    result = product_analytics.query_events("evt")

    assert result["ok"] is False
    assert result["missing"] == ["POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID"]


# review fix 3: allow-key freeform values must still be scrubbed for emails/secrets
def test_allowkey_value_scrubbed():
    from alice.observability import product_analytics as pa
    s = pa._sanitize_properties({"reason": "applied via john.doe@stripe.com",
                                 "tool_error": "auth failed key sk-ant-abcdefghij1234567890abcdef"})
    assert "@stripe.com" not in s["reason"] and "[EMAIL]" in s["reason"]
    assert "sk-ant-" not in s["tool_error"] and "[SECRET]" in s["tool_error"]
