import json
import sys
from pathlib import Path


from alice import tools


def test_observability_tools_are_registered():
    names = {spec["name"] for spec in tools.tool_specs()}

    assert "query_sentry_events" in names
    assert "search_transcript" in names
    assert "query_cost_log" in names
    assert "query_recent_traces" in names
    assert "query_paste_buffer_log" in names
    assert "validate_state_claim" in names
    assert "query_runtime_metrics" in names
    assert "query_observability_artifacts" in names


def test_validate_state_claim_file_exists_for_repo_file():
    result = tools.dispatch("validate_state_claim", {
        "claim_type": "file_exists",
        "path": "src/alice/tools.py",
    })

    assert result["valid"] is True
    assert result["exists"] is True


def test_query_cost_log_summarizes_recent_entries():
    result = tools.dispatch("query_cost_log", {"since_hours": 168, "limit": 3})

    assert result["path"] == "feedback/time-cost-log.jsonl"
    assert "by_task" in result
    assert isinstance(result["recent"], list)


def test_search_transcript_returns_capped_snippets():
    result = tools.dispatch("search_transcript", {
        "phrase": "Layer 7",
        "since_hours": 168,
        "limit": 5,
    })

    assert result["path"] == "feedback/telegram-history.jsonl"
    assert isinstance(result["matches"], list)
    for match in result["matches"]:
        assert len(match["snippet"]) <= 715


def test_query_sentry_events_uses_redacted_summaries(monkeypatch):
    class _FakeSentryQuery:
        @staticmethod
        def discover_project(token, cfg):
            return "org", "project", {"id": "1"}

        @staticmethod
        def query_events(token, org, project, *, query, since_minutes, limit):
            return [{
                "eventID": "abc",
                "title": "alice.security.secret_leak",
                "tags": {"where": "telegram_bot:outbound"},
                "metadata": {},
                "raw": "sk-ant-secretsecret",
            }]

    class _FakeJobCfg:
        @staticmethod
        def load():
            return {"SENTRY_AUTH_TOKEN": "token", "SENTRY_ORG": "org", "SENTRY_PROJECT": "project"}

    monkeypatch.setattr("alice.observability.sentry_query", _FakeSentryQuery, raising=False)
    monkeypatch.setattr("alice.jobcfg", _FakeJobCfg, raising=False)
    result = tools.dispatch("query_sentry_events", {"query": "x", "since_minutes": 10})

    assert result["ok"] is True
    serialized = json.dumps(result)
    assert "sk-ant-secretsecret" not in serialized
    assert "sk-ant-[REDACTED]" in serialized


def test_describe_capabilities_reports_observability_tools(monkeypatch):
    class _FakeJobCfg:
        @staticmethod
        def load():
            return {"SENTRY_ORG": "operator-llc", "SENTRY_PROJECT": "job-search"}

    monkeypatch.setattr("alice.jobcfg", _FakeJobCfg, raising=False)
    caps = tools.dispatch("describe_capabilities", {})
    names = {tool["name"] for tool in caps["tools"]}

    assert "query_sentry_events" in names
    assert "search_transcript" in names
    assert "query_runtime_metrics" in names
    assert "query_observability_artifacts" in names
    assert caps["runtime"]["observability"]["sentry_org"] == "operator-llc"
    assert "infrastructure" in caps["runtime"]
    assert caps["runtime"]["security_guardrails"]["prompt_injection_detection"] is True
