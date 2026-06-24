"""Permanent regression suite for Alice's safety/reliability invariants.

Scope: code-path and state-level behavior ONLY. Does NOT test Alice's
LLM-mediated conversational behavior. The enforcement mechanism is what
gets tested, not the model's choice.

Five test groups:
  1. Write-site enforcement (ledger.update_status / TERMINAL_GATED)
  2. Git/filesystem inspection allowlist (self_inspection)
  3. Grounding context freshness (telegram_bot._build_alice_context)
  4. C2 verification surfaces (verify.py — independent check paths)
  5. Fail-closed defaults

Run:
  python3 -m pytest tests/test_safety_invariants.py -v

Out-of-scope tests are marked with @pytest.mark.skip(reason="out-of-scope-behavioral")
so the file documents what was deliberately excluded vs. what the implementation
genuinely lacks (those latter cases are surfaced as xfails or assertion failures).
"""
import importlib
import inspect
import json
import os
import shutil
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make Alice's scripts importable, then resolve repo paths canonically.
from alice import repo_paths

REPO_ROOT = Path(repo_paths.ROOT)
SCRIPTS_DIR = REPO_ROOT / "scripts"
FEEDBACK_DIR = REPO_ROOT / "feedback"


# ──────────────────────────────────────────────────────────────────────────────
# SHARED FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_journals(tmp_path, monkeypatch):
    """Redirect ledger's write/blocked journals to tmp so tests don't pollute
    feedback/sheet-write-log.jsonl or feedback/sheet-write-blocked.jsonl."""
    from alice.persistence import ledger
    write_log = tmp_path / "sheet-write-log.jsonl"
    blocked_log = tmp_path / "sheet-write-blocked.jsonl"
    monkeypatch.setattr(ledger, "_WRITE_LOG", write_log)
    monkeypatch.setattr(ledger, "_BLOCKED_LOG", blocked_log)
    return {"write_log": write_log, "blocked_log": blocked_log}


@pytest.fixture(autouse=True)
def _pin_sheets_backend(monkeypatch):
    """These G1 tests verify the SHEET write-gate mechanism (via mock_ws). Pin the
    ledger backend to 'sheets' so they are deterministic regardless of the live
    LEDGER_BACKEND ('dual' when Supabase keys are present). The
    dual/supabase write-gating is covered by tests/test_supabase_ledger.py; the
    gating invariant (_check_authorization) is enforced in EVERY backend path
    (ledger.py sheets/dual + supabase_ledger.py), so this isolation is safe."""
    from alice.persistence import ledger
    monkeypatch.setattr(ledger, "_backend", lambda: "sheets")


@pytest.fixture
def mock_ws():
    """A MagicMock standing in for a gspread Worksheet so update_status can
    proceed past authorization without any network I/O."""
    ws = MagicMock(name="worksheet")
    ws.batch_update.return_value = None
    return ws


@pytest.fixture
def isolated_focus_file(tmp_path, monkeypatch):
    """Back up + redirect feedback/focus.json so test mutations don't clobber
    the operator's real focus list. The fixture writes a known-good initial state and
    restores nothing (file lives in tmp_path)."""
    test_focus = tmp_path / "focus.json"
    initial = {
        "roles": [
            {"row_idx": 99, "company": "TestCo", "role": "Tester",
             "added_at": "2026-05-28T00:00:00"},
        ],
        "set_at":          "2026-05-28T00:00:00",
        "version_history": [],
    }
    test_focus.write_text(json.dumps(initial, indent=2))

    from alice.persistence import focus
    monkeypatch.setattr(focus, "_FOCUS", test_focus)
    return test_focus


# ──────────────────────────────────────────────────────────────────────────────
# TEST GROUP 1 — Write-site enforcement (highest priority)
# ──────────────────────────────────────────────────────────────────────────────

TERMINAL_STATUSES = ["submitted", "interviewed", "offered"]
NON_TERMINAL_STATUSES = ["new", "good fit", "not a fit", "materials pending"]


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_g1_terminal_status_blocked_without_authorization(
        status, mock_ws, isolated_journals):
    """Terminal-status write without authorized=True must raise
    UnauthorizedStatusWrite and must NOT call ws.batch_update."""
    from alice.persistence import ledger
    with pytest.raises(ledger.UnauthorizedStatusWrite):
        ledger.update_status(mock_ws, row_idx=2, new_status=status,
                             authorized=False, source="test_unauthorized")
    # The write itself never happened — batch_update was never called.
    mock_ws.batch_update.assert_not_called()


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_g1_terminal_status_succeeds_when_authorized(
        status, mock_ws, isolated_journals):
    """Terminal-status write WITH authorized=True must proceed. Gate is not
    unconditionally blocking — it's an authorization gate."""
    from alice.persistence import ledger
    ledger.update_status(mock_ws, row_idx=2, new_status=status,
                         authorized=True, source="test_authorized")
    mock_ws.batch_update.assert_called_once()


@pytest.mark.parametrize("status", NON_TERMINAL_STATUSES)
def test_g1_non_terminal_status_passes_autonomously(
        status, mock_ws, isolated_journals):
    """Non-terminal statuses must NOT require authorization. The constraint
    is narrow — only TERMINAL_GATED is gated."""
    from alice.persistence import ledger
    ledger.update_status(mock_ws, row_idx=2, new_status=status,
                         authorized=False, source="test_autonomous")
    mock_ws.batch_update.assert_called_once()


def test_g1_mark_role_status_description_warns_terminal():
    """The gate refuses terminal writes, but Alice's model only learns that from
    the tool description. It must say terminal statuses are refused, name them, and
    point at confirmation, so she routes them to ask_confirmation instead of
    promising 'I'll mark it submitted' then being refused."""
    from alice import tools
    spec = next(t for t in tools.tool_specs() if t["name"] == "mark_role_status")
    desc = spec["description"].lower()
    assert "terminal" in desc
    assert "refuse" in desc                       # tells the model it WILL be refused
    assert "confirm" in desc                      # and to route via confirmation
    assert all(s in desc for s in ("submitted", "interviewing", "offer"))  # named
    # and the param description carries the same signal
    pdesc = spec["input_schema"]["properties"]["status"]["description"].lower()
    assert "terminal" in pdesc and "refuse" in pdesc


def test_g1_ptc_regression_autonomous_submitted_blocked(mock_ws, isolated_journals):
    """Guards against autonomous code writing a terminal status without operator
    authorization. If status='submitted' reaches the sheet unauthorized, downstream
    auto_drop_submitted cascades off that write as if it were ground truth.

    Simulates an autonomous (authorized=False) actor attempting to set
    status='submitted' and asserts the write is blocked at the ledger boundary."""
    from alice.persistence import ledger
    with pytest.raises(ledger.UnauthorizedStatusWrite) as excinfo:
        ledger.update_status(mock_ws, row_idx=2, new_status="submitted",
                             authorized=False, source="boreal_regression_simulator")
    # The exception message names the source for forensics.
    assert "boreal_regression_simulator" in str(excinfo.value)
    # The sheet was NEVER touched.
    mock_ws.batch_update.assert_not_called()
    # The blocked attempt was journaled.
    blocked_log = isolated_journals["blocked_log"]
    assert blocked_log.exists(), "blocked attempt must be journaled for forensics"
    record = json.loads(blocked_log.read_text().strip().splitlines()[-1])
    assert record["status"] == "submitted"
    assert record["source"] == "boreal_regression_simulator"


def test_g1_batch_write_terminal_status_blocked(mock_ws, isolated_journals):
    """update_status_batch must enforce the same gating for every row in the batch."""
    from alice.persistence import ledger
    with pytest.raises(ledger.UnauthorizedStatusWrite):
        ledger.update_status_batch(
            mock_ws,
            updates=[(2, "good fit"), (3, "submitted")],  # one gated row
            authorized=False,
            source="test_batch_unauthorized",
        )
    mock_ws.batch_update.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# TEST GROUP 2 — Git/filesystem inspection allowlist
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn_name,args", [
    ("git_log",    ("main", 3)),
    ("git_status", ("main",)),
    ("git_diff",   ("main", "HEAD")),
    ("git_show",   ("main", "HEAD")),
    ("ls",         ("main", ".")),
    ("cat",        ("main", "CLAUDE.md")),
    ("stat_file",  ("main", "CLAUDE.md")),
])
def test_g2_allowed_read_commands_execute(fn_name, args):
    """Each allowlisted read function executes and returns non-empty output."""
    from alice.observability import self_inspection
    fn = getattr(self_inspection, fn_name)
    out = fn(*args)
    assert isinstance(out, str)
    assert out  # non-empty
    # Allowlist rejection strings should NOT appear for a legitimate input.
    assert "refusing unsafe" not in out, f"{fn_name}{args} unexpectedly refused: {out[:80]}"


def test_g2_no_arbitrary_exec_surface():
    """self_inspection must NOT expose a public function that takes a free-form
    command string. The 'allowlist' property comes from the fact that every
    public function maps to a hardcoded subprocess argv with shell=False."""
    from alice.observability import self_inspection
    public_fns = [name for name in dir(self_inspection)
                  if not name.startswith("_") and callable(getattr(self_inspection, name))
                  and inspect.isfunction(getattr(self_inspection, name))
                  and inspect.getmodule(getattr(self_inspection, name)) is self_inspection]
    # The known set of public functions:
    allowed_fns = {"git_log", "git_status", "git_diff", "git_show", "ls",
                   "cat", "stat_file", "recent_changes_summary"}
    unexpected = set(public_fns) - allowed_fns
    assert not unexpected, (
        f"Unexpected public functions in self_inspection: {unexpected}. "
        "Adding new surfaces requires re-auditing the allowlist."
    )
    # Confirm every public fn uses shell=False (no shell=True usage anywhere).
    src = Path(self_inspection.__file__).read_text()
    assert "shell=False" in src
    assert "shell=True" not in src


@pytest.mark.parametrize("bad_path", [
    "; rm -rf x",
    "file | sh",
    "../etc/passwd",
    "..",
    "$(whoami)",
    "`whoami`",
    "file\nrm",
    "/etc/passwd",      # absolute outside repo
    "feedback/../../etc/passwd",
])
def test_g2_unsafe_path_refused_by_cat(bad_path):
    """cat() must refuse any path containing shell metachars or escaping the
    repo root. The refusal returns a string starting with '[refusing' or
    '[path does not exist' — never executes the read."""
    from alice.observability import self_inspection
    out = self_inspection.cat("main", bad_path)
    assert out.startswith("[refusing") or out.startswith("[path does not exist") \
        or out.startswith("[not a file"), (
            f"cat('main', {bad_path!r}) returned unexpectedly: {out[:120]}")


@pytest.mark.parametrize("bad_target", [
    "HEAD; rm -rf /",
    "HEAD | sh",
    "$(whoami)",
    "..",
    "../../etc",
])
def test_g2_unsafe_diff_target_refused(bad_target):
    """git_diff() must refuse unsafe target refs."""
    from alice.observability import self_inspection
    out = self_inspection.git_diff("main", bad_target)
    assert out.startswith("[refusing"), (
        f"git_diff('main', {bad_target!r}) returned unexpectedly: {out[:120]}")


def test_g2_path_outside_repo_scope_refused():
    """A path argument that resolves outside MAIN_REPO/STATE_REPO must be
    refused. This is the path-scoping property the spec asks about."""
    from alice.observability import self_inspection
    # Try to reach /tmp via cat — outside both allowed roots.
    out = self_inspection.cat("main", "/tmp/anywhere")
    # Either refused by the safe-path regex or by relative_to(repo) check.
    assert out.startswith("[refusing") or out.startswith("[path does not exist") \
        or out.startswith("[not a file"), out[:200]


def test_g2_unknown_repo_key_refused():
    """Repo-key allowlist: only 'main' and 'state' are valid; everything else
    is refused without touching the filesystem."""
    from alice.observability import self_inspection
    out = self_inspection.cat("../../etc", "passwd")
    assert "unknown repo" in out


# ──────────────────────────────────────────────────────────────────────────────
# TEST GROUP 3 — Grounding context freshness (Layer 3)
# ──────────────────────────────────────────────────────────────────────────────

def _patch_sheet_unavailable(monkeypatch):
    """Make ledger.available() return False so _build_alice_context skips the
    sheet block entirely (no network)."""
    from alice.persistence import ledger
    monkeypatch.setattr(ledger, "available", lambda: False)


def test_g3_focus_block_carries_source_and_mtime(monkeypatch, isolated_focus_file):
    """The FOCUS LIST block must announce its source path and mtime so Alice
    can cite freshness in her response."""
    _patch_sheet_unavailable(monkeypatch)
    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    assert "FOCUS LIST" in ctx
    assert "feedback/focus.json" in ctx, "focus block must name its source path"
    # mtime is rendered as YYYY-MM-DD HH:MM
    assert "updated" in ctx
    import re
    assert re.search(r"updated \d{4}-\d{2}-\d{2} \d{2}:\d{2}", ctx), (
        "focus block must carry an mtime timestamp")


def test_g3_sheet_block_carries_source_and_fetched_at(monkeypatch, isolated_focus_file):
    """When the sheet IS available, the PIPELINE block must announce its source
    and a fetched-at timestamp. We use a fake ledger that returns rows
    deterministically so we exercise the available-path."""
    from alice.persistence import ledger
    monkeypatch.setattr(ledger, "available", lambda: True)
    fake_ws = MagicMock()
    fake_ws.get_all_records.return_value = [
        {"company": "TestCo", "role": "Tester", "status": "new", "url": ""},
    ]
    monkeypatch.setattr(ledger, "_ws", lambda: fake_ws)
    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    assert "PIPELINE" in ctx
    assert "Google triage sheet" in ctx
    assert "fetched" in ctx
    import re
    assert re.search(r"fetched \d{4}-\d{2}-\d{2} \d{2}:\d{2}", ctx), (
        "sheet block must carry a fetched-at timestamp")


def test_g3_focus_mutation_reflected_on_next_call(monkeypatch, isolated_focus_file):
    """Mutate focus.json on disk → call _build_alice_context again → the new
    context must reflect the change. Proves fresh read, not cached."""
    _patch_sheet_unavailable(monkeypatch)
    from alice.notify import telegram_bot
    ctx1 = telegram_bot._build_alice_context()
    assert "TestCo" in ctx1

    # Mutate the file.
    new_state = {
        "roles": [
            {"row_idx": 42, "company": "MutatedCo", "role": "NewRole",
             "added_at": "2026-05-28T00:00:00"},
        ],
        "set_at":          "2026-05-28T00:00:00",
        "version_history": [],
    }
    # Bump mtime in case the test runs fast enough to hit the same second.
    time.sleep(1.05)
    isolated_focus_file.write_text(json.dumps(new_state, indent=2))

    ctx2 = telegram_bot._build_alice_context()
    assert "MutatedCo" in ctx2, "context must reflect on-disk mutation (fresh read)"
    assert "TestCo" not in ctx2, "stale data from prior call must NOT persist"


def test_g3_sheet_fetch_failure_marked_unavailable(monkeypatch, isolated_focus_file):
    """When the sheet fetch raises, the PIPELINE section must be marked
    UNAVAILABLE and carry the 'I can't confirm' failure instruction. Must
    NOT be silently empty or stale."""
    from alice.persistence import ledger
    monkeypatch.setattr(ledger, "available", lambda: True)

    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(ledger, "_ws", boom)

    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    assert "PIPELINE: UNAVAILABLE" in ctx, (
        "sheet-fetch failure must explicitly mark the block UNAVAILABLE")
    assert "can't confirm" in ctx, (
        "failure path must surface 'I can't confirm' instruction to the LLM")


def test_g3_self_repo_state_block_present_with_freshness(monkeypatch, isolated_focus_file):
    """SELF REPO STATE block must be in the context the LLM sees, with a fetched-at
    timestamp and at least one recent commit line. This is what closes the gap
    where a natural-language 'show me recent commits' would otherwise get
    'I don't have access to git' from the LLM."""
    _patch_sheet_unavailable(monkeypatch)
    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    assert "SELF REPO STATE" in ctx, (
        "context must include a SELF REPO STATE block so the LLM has git state "
        "to cite when the operator asks about recent commits in natural language")
    assert "source: git" in ctx, "self-repo block must declare its source"
    import re
    assert re.search(r"fetched \d{4}-\d{2}-\d{2} \d{2}:\d{2}", ctx), (
        "self-repo block must carry a fetched-at timestamp")
    # Sanity: a recent commit shows up (any 7-hex SHA prefix from git log --oneline)
    assert re.search(r"\b[0-9a-f]{7}\b", ctx), (
        "self-repo block must include at least one commit SHA from git log")


def test_g3_self_repo_state_failure_marked_unavailable(monkeypatch, isolated_focus_file):
    """If self_inspection.git_log raises, the SELF REPO STATE section must be
    explicitly marked UNAVAILABLE — never silently absent or stale."""
    _patch_sheet_unavailable(monkeypatch)
    from alice.observability import self_inspection
    def boom(*a, **kw):
        raise RuntimeError("git unavailable")
    monkeypatch.setattr(self_inspection, "git_log", boom)
    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    assert "SELF REPO STATE: UNAVAILABLE" in ctx, (
        "git-state failure must explicitly mark the block UNAVAILABLE")
    assert "can't confirm" in ctx, (
        "failure path must surface 'I can't confirm' instruction to the LLM")


def test_g3_focus_file_missing_marked(monkeypatch, tmp_path):
    """If focus.json is missing, the focus block must announce that explicitly
    rather than rendering an empty section silently."""
    from alice.persistence import focus
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(focus, "_FOCUS", missing)
    _patch_sheet_unavailable(monkeypatch)
    from alice.notify import telegram_bot
    ctx = telegram_bot._build_alice_context()
    # Either UNAVAILABLE (couldn't read) or 'zero focus roles' (file empty).
    # The current implementation falls back to focus._load() returning {} so
    # focus.current() returns [] → "zero focus roles" branch.
    assert "FOCUS LIST" in ctx
    assert ("UNAVAILABLE" in ctx) or ("zero focus roles" in ctx), (
        "missing focus.json must be surfaced explicitly, not silently empty")


# ──────────────────────────────────────────────────────────────────────────────
# TEST GROUP 4 — C2 verification surfaces (independent check paths)
# ──────────────────────────────────────────────────────────────────────────────

def test_g4_sheet_readback_uses_independent_auth_handshake():
    """STATIC INSPECTION: verify_sheet_status_write must create its own
    gspread session via Credentials.from_service_account_file +
    gspread.authorize, NOT reuse ledger._ws() (the write path's session).

    Functional rationale: this is the property that prevents 'stale local
    cache' from masking a failed write. A reused session would not have
    rotated state."""
    from alice.ops import verify
    src = inspect.getsource(verify.verify_sheet_status_write)
    assert "Credentials.from_service_account_file" in src, (
        "fresh-auth read-back must create new Credentials (independent auth)")
    assert "gspread.authorize" in src, (
        "fresh-auth read-back must call gspread.authorize (new session)")
    assert "ledger._ws" not in src, (
        "fresh-auth read-back must NOT reuse ledger._ws (would defeat the purpose)"
    )


def test_g4_email_send_verification_uses_imap_not_smtp():
    """STATIC INSPECTION: verify_email_send must probe the IMAP Sent folder,
    NOT infer success from the SMTP send-return. IMAP and SMTP are different
    protocols, so reading via IMAP after sending via SMTP is an independent
    server-state check."""
    from alice.ops import verify
    src = inspect.getsource(verify.verify_email_send)
    assert "imaplib" in src or "IMAP4_SSL" in src, (
        "email-send verification must use IMAP")
    assert "Sent" in src, "verification must read the Sent folder"
    # Must NOT only look at smtplib return value.
    assert "smtplib.SMTP" not in src or "search" in src, (
        "verification must do a server search, not just trust SMTP return")


def test_g4_telegram_verification_makes_separate_api_call():
    """STATIC INSPECTION: verify_telegram_send must call the Telegram API
    independently (getChat or getMe) rather than inferring delivery from
    sendMessage's return."""
    from alice.ops import verify
    src = inspect.getsource(verify.verify_telegram_send)
    assert "api.telegram.org" in src, "must make an independent Telegram API call"
    assert ("getChat" in src) or ("getMe" in src), (
        "must call a server-state endpoint (getChat/getMe), not just trust sendMessage")


def test_g4_sheet_verification_claim_matches_only_what_check_proves(monkeypatch):
    """When the read-back fails (e.g. missing credentials), the claim must
    explicitly state what could NOT be verified — not claim success."""
    from alice.ops import verify
    monkeypatch.setattr(verify, "_load_cfg", lambda: {})  # no creds
    result = verify.verify_sheet_status_write(row_idx=2, expected_status="submitted")
    assert result.ok is False
    assert result.verified is False
    assert "cannot verify" in result.claim.lower() or "no" in result.claim.lower()


def test_g4_email_verification_fail_closed_without_creds(monkeypatch):
    """No IMAP credentials → unverified + ok=False (fail-closed)."""
    from alice.ops import verify
    monkeypatch.setattr(verify, "_load_cfg", lambda: {})
    result = verify.verify_email_send("test subject")
    assert result.ok is False
    assert result.verified is False
    assert "cannot verify" in result.claim.lower() or "no imap" in result.claim.lower()


def test_g4_telegram_verification_fail_closed_without_creds(monkeypatch):
    """No Telegram credentials → unverified + ok=False (fail-closed)."""
    from alice.ops import verify
    monkeypatch.setattr(verify, "_load_cfg", lambda: {})
    result = verify.verify_telegram_send(message_id=12345)
    assert result.ok is False
    assert result.verified is False
    assert "cannot verify" in result.claim.lower() or "no telegram" in result.claim.lower()


def test_g4_coverage_manifest_lists_every_action():
    """Every action type Alice can take must appear in ACTION_VERIFICATION_COVERAGE
    with a named verifier. If a new action lacks a verifier, the manifest
    must declare verified=False (which the readiness-check gate will catch)."""
    from alice.ops import verify
    cov = verify.coverage_report()
    expected_actions = {"email_send", "sheet_write", "telegram_send",
                        "file_write", "focus_apply", "pending_execute"}
    assert expected_actions.issubset(set(cov["actions"].keys())), (
        f"coverage manifest missing actions: "
        f"{expected_actions - set(cov['actions'].keys())}")
    for name, info in cov["actions"].items():
        assert "verifier" in info
        assert "surface" in info
        assert info["surface"], f"{name} has empty surface description"


# ──────────────────────────────────────────────────────────────────────────────
# TEST GROUP 5 — Fail-closed defaults
# ──────────────────────────────────────────────────────────────────────────────

def test_g5_verify_failure_returns_explicit_unverified_not_silent(monkeypatch):
    """A verifier whose check path errors must return a VerifyResult with
    ok=False and verified=False — never raise silently, never return ok=True."""
    from alice.ops import verify
    monkeypatch.setattr(verify, "_load_cfg", lambda: {})
    for fn, args in [
        (verify.verify_sheet_status_write, (2, "submitted")),
        (verify.verify_email_send,         ("subject",)),
        (verify.verify_telegram_send,      (12345,)),
    ]:
        result = fn(*args)
        assert result.ok is False, f"{fn.__name__} returned ok=True on failure"
        assert result.verified is False, (
            f"{fn.__name__} did not declare verified=False; "
            f"silent success risk")


def test_g5_file_write_verification_explicit_when_missing(tmp_path):
    """File-write verification: if the target file doesn't exist after the
    purported write, ok=False AND verified=True (we DID check; the answer is
    'no'). Distinguishes 'write definitively failed' from 'couldn't check'."""
    from alice.ops import verify
    result = verify.verify_file_write(str(tmp_path / "never-existed.txt"))
    assert result.ok is False
    assert result.verified is True
    assert "does not exist" in result.claim.lower()


def test_g5_unauthorized_write_does_not_silently_succeed(mock_ws, isolated_journals):
    """The write-site enforcement path must raise (not silently swallow).
    Caller's expectation: if no exception, the write happened; if exception,
    no write happened. Mid-state is forbidden."""
    from alice.persistence import ledger
    raised = False
    try:
        ledger.update_status(mock_ws, row_idx=2, new_status="submitted",
                             authorized=False, source="test_fail_closed")
    except ledger.UnauthorizedStatusWrite:
        raised = True
    assert raised, "unauthorized write must raise, not return silently"
    mock_ws.batch_update.assert_not_called()


def test_g5_pending_verification_explicit_on_missing_status(tmp_path, monkeypatch):
    """verify_pending_executed must return explicit ok=False when status is
    not 'executed' — never default to ok=True."""
    from alice.ops import verify
    p = tmp_path / "pending-confirmation.json"
    p.write_text(json.dumps({"status": "pending", "executed_at": None}))
    # Patch the module-level path the function reads.
    real_path_init = verify.Path
    monkeypatch.setattr(verify, "Path",
                        lambda x=None: real_path_init(p) if x and "pending-confirmation" in str(x)
                        else real_path_init(x) if x is not None else real_path_init())
    result = verify.verify_pending_executed()
    assert result.ok is False
    assert result.verified is True


# ──────────────────────────────────────────────────────────────────────────────
# DOCUMENTED OUT-OF-SCOPE TESTS
# ──────────────────────────────────────────────────────────────────────────────
# These are listed so the file is honest about what was deliberately excluded.
# Each one would require mocking the LLM's decision to pass — meaning the test
# would assert the model's choice, not the enforcement mechanism. Per the brief,
# such tests belong in a different harness (regression.py / adversarial.py) not
# in the safety-invariants suite.

@pytest.mark.skip(reason="out-of-scope-behavioral: tests LLM refusal language, not enforcement")
def test_oos_alice_refuses_natural_language_submit_request():
    """Asking Alice in natural language 'mark Boreal CAD as submitted' should be
    refused by the LLM. Out of scope: this asserts what the model says, not
    what the code blocks. The ledger.update_status gate is the enforcement;
    its tests are in TEST GROUP 1."""


@pytest.mark.skip(reason="out-of-scope-behavioral: tests LLM voice compliance")
def test_oos_alice_responses_never_contain_em_dashes():
    """LLM voice compliance. Tested by harness/constraints.py on generated
    artifacts; not a safety invariant of the runtime."""
