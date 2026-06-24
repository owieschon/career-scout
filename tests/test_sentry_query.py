from pathlib import Path


from alice.observability import sentry_query as sq


def test_dsn_project_id_extracts_numeric_id():
    assert sq.dsn_project_id("https://public@example.ingest.sentry.io/12345") == "12345"


def test_summarize_event_omits_extra_payloads():
    event = {
        "eventID": "abc",
        "title": "alice.security.secret_leak",
        "level": "warning",
        "extra": {"secret": "sk-ant-should-not-appear"},
        "tags": [
            {"key": "where", "value": "telegram_bot:outbound"},
            {"key": "irrelevant", "value": "ignored"},
        ],
        "metadata": {"type": "Message", "value": "short"},
    }

    summary = sq.summarize_event(event)

    assert summary["eventID"] == "abc"
    assert summary["tags"] == {"where": "telegram_bot:outbound"}
    assert "extra" not in summary
