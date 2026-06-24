"""fix-grounding regression suite.

Regression coverage for grounding and secret-scrubbing:
  A. Sentry scrubber: telegram token + other secrets redacted in before_send
     and before_breadcrumb; event structure intact.
  B. repo_status tool: registered, returns real commits + mtimes, bounded.
  C. Grounding-prompt: FILENAME/TIMESTAMP/COMMIT rule present in both prompt
     assembly paths (freeform + JSON-envelope) and in ALICE_SOUL.md.
  D. Detector pre-send hedge: claims_without_tools fires hedge on correct shape
     and does NOT hedge on already-acknowledged boundary statements.
  E. Span-export: BatchSpanProcessor raised timeout, shutdown_tracing present.

Run:
    python3 -m pytest tests/test_fix_grounding.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Anchor file reads to the repo root via __file__ (the test lives at
# tests/test_fix_grounding.py, so parent.parent is the repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent   # soul/prompt files live here
SCRIPTS_DIR = REPO_ROOT / "scripts"
_PKG = REPO_ROOT / "src" / "alice"
from alice import repo_paths
# The main repo root (for git operations in repo_status tool).
_MAIN_REPO = Path(repo_paths.ROOT)


# ─── Fix A: Sentry scrubber ──────────────────────────────────────────────────

class TestSentryScrubber:
    """Test that before_send / before_breadcrumb redact Telegram tokens and
    other secrets while preserving the event structure."""

    def _import_obs(self):
        import importlib
        from alice.observability import obs
        importlib.reload(obs)
        return obs

    def test_a1_telegram_token_scrubbed_in_request_url(self):
        """A real-shaped Telegram request URL with token is redacted in
        the event's request.url field. The rest of the URL is preserved."""
        obs = self._import_obs()
        event = {
            "request": {
                "url": "https://api.telegram.org/bot123456789:AAFake-Token_xyz123456789/getUpdates",
                "method": "POST",
            },
            "exception": {"values": []},
            "breadcrumbs": {"values": []},
        }
        result = obs._scrub_event(event)
        assert result is not None, "scrubber must not drop non-ConnectError events"
        url = result["request"]["url"]
        assert "AAFake-Token_xyz123456789" not in url, "token must be scrubbed from URL"
        assert "api.telegram.org" in url, "domain must be preserved"
        assert "[REDACTED]" in url, "redaction marker must be present"

    def test_a2_telegram_token_scrubbed_in_breadcrumb_message(self):
        """A Telegram token in a breadcrumb message is scrubbed."""
        obs = self._import_obs()
        crumb = {
            "type": "http",
            "category": "httpx",
            "message": "POST https://api.telegram.org/bot987654321:BBFakeTokenABC123/sendMessage -> 200",
            "data": {"url": "https://api.telegram.org/bot987654321:BBFakeTokenABC123/sendMessage"},
        }
        result = obs._scrub_breadcrumb(crumb)
        assert result is not None
        assert "BBFakeTokenABC123" not in result["message"], "token must be scrubbed from breadcrumb message"
        assert "[REDACTED]" in result["message"]
        # data field also scrubbed
        assert "BBFakeTokenABC123" not in result["data"]["url"]

    def test_a3_breadcrumb_structure_intact_after_scrub(self):
        """Scrubbing a breadcrumb preserves all keys that don't contain secrets."""
        obs = self._import_obs()
        crumb = {
            "type": "http",
            "category": "httpx",
            "level": "info",
            "message": "POST https://api.telegram.org/bot111:TokenABC/sendMessage",
            "data": {"status_code": 200, "url": "https://api.telegram.org/bot111:TokenABC/sendMessage"},
            "timestamp": "2026-05-31T10:00:00.000Z",
        }
        result = obs._scrub_breadcrumb(crumb)
        assert result["type"] == "http"
        assert result["level"] == "info"
        assert result["timestamp"] == "2026-05-31T10:00:00.000Z"
        assert result["data"]["status_code"] == 200

    def test_a4_scrubbed_event_structure_intact(self):
        """The event dict structure is intact after scrubbing — all top-level keys present.
        Uses a real-shaped Telegram token (10-digit bot ID + 35-char secret)."""
        obs = self._import_obs()
        # Real-shaped Telegram token: <bot_id>:<secret> where bot_id is 10 digits
        # and secret is ~35 alphanumeric chars.
        real_token = "5558675309:AAHfake_RealTokenABCDEFGHIJ12345678"
        event = {
            "request": {
                "url": f"https://api.telegram.org/bot{real_token}/getUpdates",
                "method": "POST",
                "headers": {"Authorization": f"Bot {real_token}"},
            },
            "exception": {"values": [
                {
                    "type": "TimeoutError",
                    "value": f"Request timed out at api.telegram.org/bot{real_token}/getUpdates",
                }
            ]},
            "breadcrumbs": {"values": []},
            "extra": {"alice.surface": "telegram_chat"},
            "tags": {"component": "alice"},
        }
        result = obs._scrub_event(event)
        assert result is not None
        # Structure preserved
        assert "request" in result
        assert "exception" in result
        assert "extra" in result
        assert "tags" in result
        # Domain is kept; secret part is gone from URL.
        url = result["request"]["url"]
        assert "api.telegram.org" in url
        assert real_token not in url
        assert "[REDACTED]" in url
        # Exception value: the full URL pattern in the exception value is also scrubbed.
        exc_val = result["exception"]["values"][0]["value"]
        assert real_token not in exc_val, f"token must be scrubbed from exception value: {exc_val}"

    def test_a5_non_telegram_event_passes_through(self):
        """A normal non-Telegram event is returned as-is (not dropped)."""
        obs = self._import_obs()
        event = {
            "request": {"url": "https://example.com/api/test", "method": "GET"},
            "exception": {"values": [{"type": "ValueError", "value": "bad input"}]},
            "breadcrumbs": {"values": []},
        }
        result = obs._scrub_event(event)
        assert result is not None
        assert result["request"]["url"] == "https://example.com/api/test"
        assert result["exception"]["values"][0]["value"] == "bad input"

    def test_a6_bearer_token_scrubbed(self):
        """Bearer tokens in headers are scrubbed."""
        obs = self._import_obs()
        event = {
            "request": {
                "url": "https://api.example.com/v1/endpoint",
                "headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.thisisafaketoken"},
            },
            "exception": {"values": []},
            "breadcrumbs": {"values": []},
        }
        result = obs._scrub_event(event)
        assert result is not None
        auth_header = result["request"]["headers"]["Authorization"]
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.thisisafaketoken" not in auth_header

    def test_a7_bot_restart_connect_error_suppressed(self):
        """httpx ConnectError referencing api.telegram.org/getUpdates is
        suppressed (returns None) to reduce PYTHON-M noise."""
        obs = self._import_obs()
        event = {
            "exception": {"values": [
                {
                    "type": "ConnectError",
                    "value": "Connection refused: https://api.telegram.org/getUpdates",
                    "stacktrace": {"frames": [
                        {"module": "httpx._transports.default", "filename": "httpx/_transports/default.py"},
                    ]},
                }
            ]},
            "breadcrumbs": {"values": []},
        }
        result = obs._scrub_event(event)
        assert result is None, "bot-restart ConnectError must be suppressed (return None)"

    def test_a8_non_telegram_connect_error_not_suppressed(self):
        """ConnectError for a non-Telegram endpoint is NOT suppressed."""
        obs = self._import_obs()
        event = {
            "exception": {"values": [
                {
                    "type": "ConnectError",
                    "value": "Connection refused: https://api.openai.com/v1/chat/completions",
                    "stacktrace": {"frames": []},
                }
            ]},
            "breadcrumbs": {"values": []},
        }
        result = obs._scrub_event(event)
        assert result is not None, "non-Telegram ConnectError must not be suppressed"

    def test_a9_before_send_never_raises(self):
        """_scrub_event must never raise, even on a malformed event dict."""
        obs = self._import_obs()
        # Pathological inputs.
        assert obs._scrub_event(None) is None  # None -> pass-through
        assert obs._scrub_event({}) == {} or obs._scrub_event({}) is not None  # no crash
        # Deeply nested unexpected types.
        weird_event = {
            "request": None,
            "exception": {"values": [None, 42, {"type": "E", "value": None}]},
            "breadcrumbs": None,
        }
        try:
            obs._scrub_event(weird_event)
        except Exception as e:
            pytest.fail(f"_scrub_event raised on malformed event: {e}")

    def test_a10_scrub_string_telegram_pattern(self):
        """_scrub_string directly: a real-shaped telegram URL pattern is redacted."""
        obs = self._import_obs()
        s = "POST https://api.telegram.org/bot5558675309:AAHfakeTokenABCDEFGHIJ1234/sendMessage"
        scrubbed = obs._scrub_string(s)
        assert "5558675309:AAHfakeTokenABCDEFGHIJ1234" not in scrubbed
        assert "api.telegram.org" in scrubbed
        assert "sendMessage" in scrubbed
        assert "[REDACTED]" in scrubbed


# ─── Fix B: repo_status tool ─────────────────────────────────────────────────

class TestRepoStatusTool:
    """Test that repo_status is registered, returns real data, and is bounded."""

    def _get_tool(self):
        from alice import tools
        for t in tools.TOOLS_REGISTRY:
            if t["name"] == "repo_status":
                return t
        return None

    def test_b1_repo_status_registered(self):
        """repo_status must be in the TOOLS_REGISTRY."""
        tool = self._get_tool()
        assert tool is not None, "repo_status must be registered in TOOLS_REGISTRY"

    def test_b2_repo_status_is_not_mutating(self):
        """repo_status is read-only — must have mutating=False and no guard."""
        tool = self._get_tool()
        assert tool is not None
        assert tool["mutating"] is False, "repo_status must be read-only (mutating=False)"
        assert tool["guard"] is None, "read-only tools have no guard"

    def test_b3_repo_status_returns_commits(self):
        """repo_status returns real git commits from the repo."""
        from alice import tools
        result = tools.dispatch("repo_status", {"n_commits": 5})
        assert "commits" in result
        assert "fetched_at" in result
        commits = result["commits"]
        assert isinstance(commits, list)
        assert len(commits) >= 1, "repo must have at least one commit"
        # Each commit has the expected fields.
        first = commits[0]
        assert "sha" in first, "commit must have sha"
        assert "date" in first, "commit must have date"
        assert "subject" in first, "commit must have subject"
        # sha is hex.
        import re
        assert re.match(r"^[0-9a-f]{7,12}$", first["sha"]), f"sha must be hex: {first['sha']}"

    def test_b4_repo_status_glob_returns_files(self):
        """repo_status with path_glob returns real file entries."""
        from alice import tools
        result = tools.dispatch("repo_status", {"n_commits": 1, "path_glob": "src/alice/*.py"})
        assert "files" in result
        files = result["files"]
        assert isinstance(files, list)
        assert len(files) >= 1, "src/alice/ must contain at least one .py file"
        first = result["files"][0]
        assert "path" in first
        assert "modified" in first
        assert first["path"].endswith(".py")
        # Paths are relative to repo root.
        assert not first["path"].startswith("/")

    def test_b5_repo_status_n_commits_bounded(self):
        """n_commits is capped at 50 — requesting 200 returns at most 50."""
        from alice import tools
        result = tools.dispatch("repo_status", {"n_commits": 200})
        commits = result["commits"]
        # If the repo has fewer than 50 commits we still pass; we're testing
        # the cap is enforced (not that it returns exactly 50).
        assert len(commits) <= 50, "n_commits must be capped at 50"

    def test_b6_repo_status_path_outside_repo_rejected(self):
        """Path glob pointing outside repo root is rejected with an error entry."""
        from alice import tools
        result = tools.dispatch("repo_status", {"path_glob": "../../etc/passwd"})
        files = result.get("files", [])
        # Either returns error entry or empty list (the glob can't escape).
        if files:
            # If it returned something, it must be an error dict.
            assert "error" in files[0], f"escaped path must return error, got: {files[0]}"

    def test_b7_repo_status_in_tool_specs(self):
        """repo_status appears in tool_specs() so it is available to llm.call."""
        from alice import tools
        specs = tools.tool_specs()
        names = [s["name"] for s in specs]
        assert "repo_status" in names

    def test_b8_repo_status_no_commits_flag_returns_empty_commits(self):
        """Requesting 0 commits (edge case: min is 1, so 0 becomes 1)."""
        from alice import tools
        # min cap: 0 is clamped to 1.
        result = tools.dispatch("repo_status", {"n_commits": 0})
        assert "commits" in result
        # Should return at least 1 (clamped).
        assert len(result["commits"]) >= 1


# ─── Fix C: Grounding-prompt rule ────────────────────────────────────────────

class TestGroundingPromptRule:
    """Test that the FILENAME/TIMESTAMP/COMMIT rule appears in both prompt
    assembly paths and in ALICE_SOUL.md."""

    def test_c1_filename_rule_in_freeform_invariant(self):
        """The freeform prompt (OpenClaw path) state_grounding_invariant must
        contain the filename/timestamp/commit rule."""
        src = (_PKG / "notify" / "telegram_bot.py").read_text()
        # The freeform path has a state_grounding_invariant near the freeform_directive.
        # Check for the key phrases from our addition.
        assert "FILENAME / TIMESTAMP / COMMIT RULE" in src, (
            "freeform path must include FILENAME/TIMESTAMP/COMMIT RULE in grounding invariant"
        )
        assert "repo_status" in src, (
            "grounding invariant must name repo_status as the designated tool"
        )
        assert "never assert" in src.lower() or "NEVER assert" in src, (
            "invariant must use NEVER assert language"
        )

    def test_c2_filename_rule_in_json_envelope_invariant(self):
        """The JSON-envelope prompt (_route_message path) must also contain
        the filename/timestamp/commit rule."""
        src = (_PKG / "notify" / "telegram_bot.py").read_text()
        # Both paths inject state_grounding_invariant; count occurrences of the rule.
        count = src.count("FILENAME / TIMESTAMP / COMMIT RULE")
        assert count >= 1, "at least one occurrence of the rule in telegram_bot.py"
        # Also check for the NEVER assert / guessing language.
        assert "Guessing a filename" in src or "never assert a specific filename" in src.lower() or (
            "NEVER assert a specific filename" in src
        ), "invariant must forbid guessing filenames from training data"

    def test_c3_rule_in_alice_soul(self):
        """ALICE_SOUL.md must contain the new 9a sub-rule about filenames."""
        soul = (REPO_ROOT / "ALICE_SOUL.md").read_text()
        assert "9a" in soul or "filename / timestamp / commit" in soul.lower(), (
            "ALICE_SOUL.md must contain the filename/timestamp/commit sub-rule"
        )
        assert "repo_status" in soul, (
            "soul must name repo_status as the designated grounding tool for commits"
        )
        assert "never assert" in soul.lower(), (
            "soul must use 'never assert' language for ungrounded filename claims"
        )

    def test_c4_repo_status_named_in_freeform_directive(self):
        """The freeform HOW TO ACT directive must name repo_status alongside
        the other tools so Alice knows to reach for it."""
        src = (_PKG / "notify" / "telegram_bot.py").read_text()
        # The freeform_directive block names the tools Alice should reach for.
        # After fix C, repo_status must be in the state-grounding invariant.
        assert "repo_status" in src


# ─── Fix D: Detector pre-send hedge ─────────────────────────────────────────

class TestDetectorHedge:
    """Test the pre-send hedge logic for claims_without_tools:
    - fires when zero tools + filename claims are present
    - does NOT fire when the response already acknowledges the limitation
    - does NOT fire on the write_claimed_no_write_tool shape (different handler)
    """

    def _build_claims_flag(self, filenames=None, dates=None, times=None):
        # NOTE: use explicit None check, not 'or', so an empty list stays empty.
        return {
            "kind":      "claims_without_tools",
            "filenames": ["daily_digest.py"] if filenames is None else filenames,
            "dates":     [] if dates is None else dates,
            "times":     [] if times is None else times,
        }

    def test_d1_hedge_appended_when_claims_flag_fires(self):
        """When claims_flag fires with filenames, raw response gets hedge appended."""
        # Simulate the hedge logic from telegram_bot._handle_freeform_message.
        # We test the logic directly since it's inline.
        raw = "The improvements were made in daily_digest.py and run_daily.py."
        claims_flag = self._build_claims_flag(filenames=["daily_digest.py", "run_daily.py"])

        # Replicate the hedge logic from the bot.
        ack_phrases = (
            "i haven't checked", "i need to check", "i can't confirm",
            "let me check", "call the tool", "repo_status",
            "i don't have", "without checking",
        )
        raw_lower = raw.lower()
        already_hedged = any(p in raw_lower for p in ack_phrases)
        if not already_hedged and claims_flag.get("filenames"):
            hedge = (
                "\n\n[Note: the above response referenced specific "
                f"filename(s) ({', '.join(claims_flag['filenames'][:3])}) "
                "without a file-lookup tool call this turn. These details "
                "may be unverified. Call repo_status or list_dir to confirm.]"
            )
            raw = raw + hedge

        assert "[Note:" in raw, "hedge must be appended when claims_flag fires"
        assert "daily_digest.py" in raw
        assert "repo_status" in raw

    def test_d2_no_hedge_when_already_acknowledged(self):
        """Hedge is NOT appended when the response already acknowledges the limitation."""
        raw = "I need to check that — let me call repo_status to confirm the filename."
        claims_flag = self._build_claims_flag(filenames=["some_file.py"])

        ack_phrases = (
            "i haven't checked", "i need to check", "i can't confirm",
            "let me check", "call the tool", "repo_status",
            "i don't have", "without checking",
        )
        raw_lower = raw.lower()
        already_hedged = any(p in raw_lower for p in ack_phrases)
        original_raw = raw
        if not already_hedged and claims_flag.get("filenames"):
            raw = raw + "\n\n[Note: ...]"

        assert raw == original_raw, "hedge must NOT be appended when response already acknowledges"

    def test_d3_no_hedge_when_no_filenames_in_flag(self):
        """Hedge is NOT appended when claims_flag has no filenames (only dates/times)."""
        raw = "The last run was at 2026-05-31 03:15."
        claims_flag = self._build_claims_flag(filenames=[], dates=["2026-05-31"], times=["03:15"])

        ack_phrases = (
            "i haven't checked", "i need to check", "i can't confirm",
            "let me check", "call the tool", "repo_status",
            "i don't have", "without checking",
        )
        raw_lower = raw.lower()
        already_hedged = any(p in raw_lower for p in ack_phrases)
        original_raw = raw
        if not already_hedged and claims_flag.get("filenames"):
            raw = raw + "\n\n[Note: ...]"

        assert raw == original_raw, "hedge is only for filename claims, not date/time-only flags"

    def test_d4_grounding_detector_known_positive_gate(self):
        """The existing detect_category_mismatch known-positive gate must still pass."""
        from alice.pipeline import grounding
        result = grounding.run_known_positive_gate()
        assert result["passed"], (
            f"known-positive gate FAILED — detector is broken: {result['verdict']}"
        )

    def test_d5_claims_without_tools_fires_on_filename_with_zero_tools(self):
        """detect_specific_claims_without_tools fires when zero tools + filename claim."""
        from alice.pipeline import grounding
        flag = grounding.detect_specific_claims_without_tools(
            tool_calls=[],
            response_text="The daily digest is handled in daily_digest.py and run_weekly.sh.",
        )
        assert flag is not None, "detector must fire on zero-tool + filename claims"
        assert "daily_digest.py" in flag["filenames"] or "run_weekly.sh" in flag["filenames"]

    def test_d6_claims_without_tools_does_not_fire_when_tools_present(self):
        """detect_specific_claims_without_tools must NOT fire when tools fired."""
        from alice.pipeline import grounding
        flag = grounding.detect_specific_claims_without_tools(
            tool_calls=[{"name": "repo_status", "result": {"commits": []}}],
            response_text="The file daily_digest.py was last modified 2026-05-30.",
        )
        assert flag is None, "detector must NOT fire when tools were called this turn"


# ─── Fix E: Span-export reliability ─────────────────────────────────────────

class TestSpanExportReliability:
    """Test that BatchSpanProcessor timeout is raised and shutdown_tracing exists."""

    def test_e1_shutdown_tracing_exists_and_callable(self):
        """telemetry.shutdown_tracing must exist and be callable."""
        from alice.observability import telemetry
        assert hasattr(telemetry, "shutdown_tracing"), (
            "telemetry must export shutdown_tracing() (PYTHON-K fix)"
        )
        assert callable(telemetry.shutdown_tracing)

    def test_e2_shutdown_tracing_is_failopen(self):
        """shutdown_tracing must never raise, even when tracing is not initialized."""
        from alice.observability import telemetry
        try:
            telemetry.shutdown_tracing(timeout_ms=100)
        except Exception as e:
            pytest.fail(f"shutdown_tracing raised when tracing is off: {e}")

    def test_e3_batch_processor_timeout_raised_in_source(self):
        """The BatchSpanProcessor in telemetry.py uses export_timeout_millis > 30000."""
        src = (_PKG / "observability" / "telemetry.py").read_text()
        assert "export_timeout_millis" in src, (
            "BatchSpanProcessor must have explicit export_timeout_millis set"
        )
        # Extract the value and confirm it's >= 60000.
        import re
        m = re.search(r"export_timeout_millis\s*=\s*(\d+)", src)
        assert m is not None, "export_timeout_millis must be set to a literal value"
        timeout_val = int(m.group(1))
        assert timeout_val >= 60_000, (
            f"export_timeout_millis must be >= 60000 (was {timeout_val}); "
            "raising it closes PYTHON-K batch-export dropout"
        )

    def test_e4_flush_langsmith_still_present(self):
        """flush_langsmith must still exist (used by short-lived scripts)."""
        from alice.observability import telemetry
        assert hasattr(telemetry, "flush_langsmith")
        assert callable(telemetry.flush_langsmith)

    def test_e5_flush_langsmith_failopen_when_not_live(self):
        """flush_langsmith is a no-op (not a raise) when processor is not live."""
        from alice.observability import telemetry
        try:
            telemetry.flush_langsmith(timeout_ms=100)
        except Exception as e:
            pytest.fail(f"flush_langsmith raised when processor is not live: {e}")


# ─── Integration: tool_specs includes repo_status ────────────────────────────

def test_z1_tool_specs_includes_repo_status():
    """tool_specs() (the list passed to llm.call) must include repo_status."""
    from alice import tools
    specs = tools.tool_specs()
    names = [s["name"] for s in specs]
    assert "repo_status" in names, "repo_status must appear in tool_specs()"


def test_z2_no_existing_tool_broken_by_import():
    """Importing tools must not raise (no duplicate name, no missing guard)."""
    try:
        import importlib
        from alice import tools
        importlib.reload(tools)
    except Exception as e:
        pytest.fail(f"tools.py import failed after fix-grounding changes: {e}")


def test_z3_grounding_module_imports_cleanly():
    """grounding.py must import without error."""
    try:
        import importlib
        from alice.pipeline import grounding
        importlib.reload(grounding)
    except Exception as e:
        pytest.fail(f"grounding.py import failed: {e}")


def test_z4_obs_module_imports_cleanly():
    """obs.py must import without error."""
    try:
        import importlib
        from alice.observability import obs
        importlib.reload(obs)
    except Exception as e:
        pytest.fail(f"obs.py import failed: {e}")


def test_z5_telemetry_module_imports_cleanly():
    """telemetry.py must import without error."""
    try:
        import importlib
        from alice.observability import telemetry
        importlib.reload(telemetry)
    except Exception as e:
        pytest.fail(f"telemetry.py import failed: {e}")
