"""Deploy guard — make 'running code != deployed code' impossible to miss.

WHY THIS EXISTS
---------------
Python does not hot-reload: a long-running daemon keeps executing the code it
loaded at startup even after newer commits land. A verification session can
then pass against phantom code — the running process is testing what it loaded,
not what's on disk. This module is the backstop so "verified live" actually
means what it claims.

DESIGN
------
Two surfaces, separate concerns:

1. DETECTION (this module is the source of truth — always runs):
   - `record_startup_commit()` snapshots git HEAD at process start and persists
     PID/start-time/commit to a state file. Called once from telegram_bot.main.
   - `check_for_stale_code()` re-reads current HEAD on demand (cheap), compares
     against the recorded startup commit, returns a dict the caller can route
     into logs, prompts, or notifications.
   - `verification_preflight()` is the hook for verify scripts: compares the
     running bot's PID start time against the most recent commit time. Returns
     `should_block=True` if the PID predates the most recent commit — i.e.
     whatever you're about to "verify live" was written after the bot started.
   - `format_stale_warning()` renders the check dict as a multi-line block
     for injection at the top of the assembled LLM context. This is a
     BEST-EFFORT SIGNAL only. The guard does NOT rely on the model attending
     to or acting on this block — models routinely under-attend to context
     instructions, and we have not verified that Haiku changes its answer
     when this block is present. Treat it as a bonus that MAY help; the
     guarantee does not depend on it.

   What the guarantee actually rests on (deterministic, observable surfaces):
     a. `verification_preflight()` — hard block before any "live tests pass"
        claim. Refuses to certify when PID predates HEAD.
     b. `maybe_notify_operator_once()` — one-shot Telegram ping to Jordan the
        first time a PID goes stale. Out-of-band, does not depend on the
        LLM noticing anything.
     c. `/version` — chat command that prints the full deploy-guard state
        (loaded commit, HEAD, PID, start time, status). Jordan can ask.
     d. stderr WARN + `obs.capture_message` (Sentry) — emitted on every
        detection in `_build_alice_context`; visible in logs and dashboards
        whether or not the LLM does anything with the warning text.

2. PREVENTION — DELIBERATELY OMITTED.
   The launchd unit (com.operator.jobsearch.telegram.plist) has KeepAlive=true:
   if the daemon exits, launchd restarts it after ThrottleInterval=30s. We
   COULD have the daemon SIGTERM itself on detected divergence — but the bot
   may be mid-LLM-call, mid-sheet-write, mid-send when the check runs. A naive
   SIGTERM would corrupt those flows (no atomicity around the Google Sheet
   write, no resume token on the Anthropic streaming API). Building a graceful
   "drain in-flight work then exit" is a real piece of engineering that is
   separate from this guard's purpose. Detection is the FLOOR. Restarts stay
   manual until the drain machinery exists.

USAGE
-----
At bot startup, before run_polling::

    from alice.ops import deploy_guard
    deploy_guard.record_startup_commit()

In _build_alice_context, at the top of the assembled sections::

    warning = deploy_guard.format_stale_warning(deploy_guard.check_for_stale_code())
    if warning:
        sections.insert(0, warning)

In any verification script, before asserting "live tests pass"::

    pre = deploy_guard.verification_preflight()
    if pre["should_block"]:
        raise SystemExit(pre["message"])
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from alice import repo_paths

REPO_ROOT = Path(repo_paths.ROOT)
STATE_DIR = REPO_ROOT / "state"
STARTUP_FILE = STATE_DIR / "deploy-guard-startup.json"
NOTIFY_FLAG = STATE_DIR / "deploy-guard-notified.flag"

# Module-level snapshot of the commit loaded at startup. Set by
# record_startup_commit(); read-only thereafter. The state file is the
# durable copy — this is the fast path used on every message.
_LOADED_COMMIT: str | None = None
_LOADED_AT: str | None = None
_PID: int | None = None


# ─── git helpers (small, no shell) ────────────────────────────────────────────


def _git(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run git args in the main repo. No shell, hard timeout, captured output."""
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        out = (res.stdout or "").strip()
        return res.returncode, out
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""


def _current_head() -> str | None:
    """Current `git rev-parse HEAD` of the main repo, or None on error."""
    code, out = _git(["rev-parse", "HEAD"])
    if code != 0 or not out:
        return None
    return out


def _commits_behind(loaded: str, head: str) -> int:
    """How many commits are between `loaded` (exclusive) and `head` (inclusive).
    0 if loaded == head OR if loaded is not an ancestor of head (e.g. branch swap)."""
    if loaded == head:
        return 0
    code, out = _git(["rev-list", f"{loaded}..{head}", "--count"])
    if code != 0 or not out:
        return 0
    try:
        return int(out)
    except ValueError:
        return 0


def _commit_iso_time(rev: str) -> str | None:
    """Author-date ISO timestamp for `rev`, or None on error."""
    code, out = _git(["show", "-s", "--format=%aI", rev])
    if code != 0 or not out:
        return None
    return out


def _pid_start_epoch(pid: int) -> float | None:
    """Return PID start time as epoch seconds, or None if `ps` failed."""
    try:
 # `ps -o lstart=` is portable on macOS; parse with strptime.
        res = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3, shell=False,
        )
        raw = (res.stdout or "").strip()
        if not raw:
            return None
 # Format: "Thu May 29 16:57:14 2026"
        return time.mktime(time.strptime(raw, "%a %b %d %H:%M:%S %Y"))
    except Exception:
        return None


# ─── public API ───────────────────────────────────────────────────────────────


def record_startup_commit() -> dict:
    """Snapshot git HEAD at process startup. Stores in module globals AND a
    state file with PID + start time + commit + iso timestamp.

    Idempotent within a process: calling again returns the existing snapshot
    rather than re-reading HEAD (the whole point is that the FIRST call wins
    so we detect divergence later).

    Returns the snapshot dict. If git fails entirely, the dict reports it but
    the module still functions in "unknown" mode — check_for_stale_code will
    return stale=False and the warning will explain why.
    """
    global _LOADED_COMMIT, _LOADED_AT, _PID
    if _LOADED_COMMIT is not None:
        return _read_state() or {
            "loaded_commit": _LOADED_COMMIT,
            "loaded_at":     _LOADED_AT,
            "pid":           _PID,
        }

    head = _current_head()
    now_iso = datetime.now().isoformat(timespec="seconds")
    pid = os.getpid()

    _LOADED_COMMIT = head
    _LOADED_AT = now_iso
    _PID = pid

    snapshot = {
        "loaded_commit": head,           # may be None if git unavailable
        "loaded_at":     now_iso,
        "pid":           pid,
        "repo_root":     str(REPO_ROOT),
    }

 # Clear the "already notified" flag from the prior process — a fresh PID
 # gets to re-notify once if it later goes stale.
    try:
        if NOTIFY_FLAG.exists():
            NOTIFY_FLAG.unlink()
    except Exception:
        pass

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STARTUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        tmp.replace(STARTUP_FILE)
    except Exception as e:
 # State file is convenience — module globals are still valid.
        print(f"[deploy_guard: failed to write {STARTUP_FILE}: {e}]", file=sys.stderr)

    print(
        f"[deploy_guard: startup snapshot — pid={pid} "
        f"loaded_commit={(head or 'UNKNOWN')[:12]} at={now_iso}]"
    )
    return snapshot


def _read_state() -> dict | None:
    """Read the persisted startup snapshot, or None if missing."""
    try:
        if not STARTUP_FILE.exists():
            return None
        return json.loads(STARTUP_FILE.read_text())
    except Exception:
        return None


def loaded_commit() -> str | None:
    """Return the commit recorded at startup (module-global preferred,
    state file as fallback for tools spawned by the daemon)."""
    if _LOADED_COMMIT:
        return _LOADED_COMMIT
    st = _read_state()
    return (st or {}).get("loaded_commit")


def _loaded_at() -> str | None:
    """Same fallback pattern as loaded_commit()."""
    if _LOADED_AT:
        return _LOADED_AT
    st = _read_state()
    return (st or {}).get("loaded_at")


def _daemon_import_set() -> set:
    """The set of scripts/*.py files the telegram_bot daemon transitively
    imports — its static AND lazy (function-level) import closure. A commit that
    touches NONE of these cannot change daemon behavior, so it should not trigger
    a STALE warning (a docs- or standalone-script commit is not daemon code).

    Approach: import-graph (precise), AST-walking every Import/ImportFrom node so
    it catches lazy `import llm` inside handlers, not just top-level imports.
    Boundaries: does NOT resolve dynamic/importlib-by-string imports (Alice uses
    normal imports), and treats non-.py files as non-daemon-code (correct for a
    code-staleness check). Fail-safe: on any parse/IO error the module is simply
    not added — combined with the conservative-warn fallback below, uncertainty
    biases toward warning, never toward silencing a real change."""
    import ast
    scripts_dir = REPO_ROOT / "scripts"
    local = {p.stem: p for p in scripts_dir.glob("*.py")}
    seen, stack = set(), ["telegram_bot"]
    while stack:
        mod = stack.pop()
        if mod in seen or mod not in local:
            continue
        seen.add(mod)
        try:
            tree = ast.parse(local[mod].read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    b = n.name.split(".")[0]
                    if b in local:
                        stack.append(b)
            elif isinstance(node, ast.ImportFrom) and node.module:
                b = node.module.split(".")[0]
                if b in local:
                    stack.append(b)
    return {str(local[m].relative_to(REPO_ROOT)) for m in seen}


def _changed_files(loaded: str, head: str) -> list:
    """Files changed in loaded..head (repo-relative paths), or [] on git error."""
    code, out = _git(["diff", "--name-only", f"{loaded}..{head}"])
    if code != 0 or not out:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def check_for_stale_code() -> dict:
    """Compare the recorded startup commit against current HEAD.

    Returns a dict with::

      stale            : bool   — True iff loaded != head AND both known
      loaded_commit    : str | None
      current_head     : str | None
      commits_behind   : int    — 0 if not stale or unknown
      loaded_at        : str | None  — when record_startup_commit fired
      head_commit_time : str | None  — author date of current HEAD
      reason           : str    — human-readable status

    Cheap enough to call on every message (one local `git rev-parse`).
    """
    loaded = loaded_commit()
    head = _current_head()

    if loaded is None:
        return {
            "stale":            False,
            "loaded_commit":    None,
            "current_head":     head,
            "commits_behind":   0,
            "loaded_at":        _loaded_at(),
            "head_commit_time": _commit_iso_time(head) if head else None,
            "reason":           "no startup snapshot — call record_startup_commit()",
        }
    if head is None:
        return {
            "stale":            False,
            "loaded_commit":    loaded,
            "current_head":     None,
            "commits_behind":   0,
            "loaded_at":        _loaded_at(),
            "head_commit_time": None,
            "reason":           "git rev-parse HEAD failed — cannot compare",
        }
    if loaded == head:
        return {
            "stale":            False,
            "loaded_commit":    loaded,
            "current_head":     head,
            "commits_behind":   0,
            "loaded_at":        _loaded_at(),
            "head_commit_time": _commit_iso_time(head),
            "reason":           "loaded == HEAD",
        }
 # Ahead of HEAD — but only STALE if the ahead-commits touch a file the daemon
 # actually imports/runs. Doc / standalone-script commits cannot change daemon
 # behavior, so they must not warn. Fail-safe: if the diff can't be computed
 # (changed == [], e.g. branch swap / git error) we WARN conservatively rather
 # than silence — never fix the false alarm by disabling the real one.
    changed = _changed_files(loaded, head)
    daemon_files = _daemon_import_set()
    daemon_changed = sorted(f for f in changed if f in daemon_files)
    base = {
        "loaded_commit":    loaded,
        "current_head":     head,
        "commits_behind":   _commits_behind(loaded, head),
        "loaded_at":        _loaded_at(),
        "head_commit_time": _commit_iso_time(head),
    }
    if changed and not daemon_changed:
        preview = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
        return {**base, "stale": False, "daemon_files_changed": [],
                "reason": f"ahead of HEAD but no daemon-imported file changed ({preview}) "
                          "— not stale (docs/standalone-scripts only)"}
    return {**base, "stale": True, "daemon_files_changed": daemon_changed,
            "reason": ("loaded commit differs from HEAD; daemon-imported files changed: "
                       + (", ".join(daemon_changed) if daemon_changed
                          else "(could not diff — warning conservatively)"))}


def format_stale_warning(check: dict | None = None) -> str:
    """Render a stale-code warning block for injection at the top of the
    assembled LLM context. BEST-EFFORT SIGNAL — not a guarantee.

    The guard does NOT rely on the model attending to or acting on this
    block. We have not verified that the model changes its answer when it
    is present, and models routinely under-attend to context instructions.
    The actual guarantees live in `verification_preflight()` (hard block),
    `maybe_notify_operator_once()` (Telegram ping), `/version` (chat command),
    and the stderr WARN + `obs.capture_message` emitted on detection.

    Returns the empty string when not stale, so the caller can do
    `sections.insert(0, format_stale_warning(...))` unconditionally.

    The block names this guard's source file so a confused operator can
    audit the warning rather than ignoring it.
    """
    if check is None:
        check = check_for_stale_code()
    if not check.get("stale"):
        return ""
    loaded = (check.get("loaded_commit") or "?")[:12]
    head = (check.get("current_head") or "?")[:12]
    behind = check.get("commits_behind", 0)
    loaded_at = check.get("loaded_at") or "?"
    head_time = check.get("head_commit_time") or "?"
    return (
        "STALE-CODE WARNING (source: scripts/deploy_guard.py):\n"
        f"  This daemon was started at {loaded_at} on commit {loaded}.\n"
        f"  Current git HEAD is {head} ({head_time}).\n"
        f"  The running process is {behind} commit(s) behind HEAD.\n"
        "  Python does NOT hot-reload — any 'live' behavior you observe "
        "in this conversation reflects the OLD code. If Jordan asks about a "
        "recent fix or commit, say so explicitly: the daemon must be "
        "restarted before that change is actually loaded."
    )


def maybe_notify_operator_once(check: dict | None = None) -> bool:
    """Send Jordan a one-shot Telegram heads-up the FIRST time this PID goes
    stale. The notify flag is cleared on record_startup_commit, so each
    fresh daemon process gets one notification per stale window.

    Returns True iff a notification was sent this call.
    """
    if check is None:
        check = check_for_stale_code()
    if not check.get("stale"):
        return False
    try:
        if NOTIFY_FLAG.exists():
            return False
 # Best-effort: import lazily to avoid loading telegram creds in
 # contexts that don't need them (tests, verification helpers).
        from alice.notify import notify_telegram
        if not notify_telegram.available():
            return False
        loaded = (check.get("loaded_commit") or "?")[:12]
        head = (check.get("current_head") or "?")[:12]
        behind = check.get("commits_behind", 0)
        msg = (
            "⚠️ Alice daemon STALE\n"
            f"loaded {loaded} → HEAD {head} ({behind} commit(s) behind)\n"
            f"PID {os.getpid()} started {_loaded_at()}\n"
            "Restart with: launchctl kickstart -k "
            "gui/$(id -u)/com.operator.jobsearch.telegram"
        )
        sent = notify_telegram.send(msg)
        if sent:
            try:
                NOTIFY_FLAG.parent.mkdir(parents=True, exist_ok=True)
                NOTIFY_FLAG.write_text(datetime.now().isoformat(timespec="seconds"))
            except Exception:
                pass
            return True
        return False
    except Exception as e:
        print(f"[deploy_guard: notify failed: {e}]", file=sys.stderr)
        return False


def verification_preflight(daemon_pid: int | None = None) -> dict:
    """Pre-check for verification scripts. If the running daemon's PID start
    time predates the most recent commit, refuse to call the result "live".

    `daemon_pid` defaults to whatever's recorded in the startup state file
    (so this helper works from a separate verifier process that didn't itself
    record a startup snapshot).

    Returns a dict with::
      should_block   : bool
      message        : str  — ready to print or raise
      pid            : int | None
      pid_start_iso  : str | None
      head           : str | None
      head_time_iso  : str | None
    """
    state = _read_state()
    pid = daemon_pid if daemon_pid is not None else (state or {}).get("pid")
    head = _current_head()
    head_time = _commit_iso_time(head) if head else None

    if pid is None:
        return {
            "should_block":   True,
            "message":        "no daemon PID recorded — run record_startup_commit() "
                              "from the daemon first, then re-verify.",
            "pid":            None,
            "pid_start_iso":  None,
            "head":           head,
            "head_time_iso":  head_time,
        }
    pid_start = _pid_start_epoch(pid)
    if pid_start is None:
        return {
            "should_block":   True,
            "message":        f"PID {pid} not running (or `ps` failed) — "
                              "daemon is not live; nothing to verify.",
            "pid":            pid,
            "pid_start_iso":  None,
            "head":           head,
            "head_time_iso":  head_time,
        }
    pid_start_iso = datetime.fromtimestamp(pid_start).isoformat(timespec="seconds")

    if head is None or head_time is None:
        return {
            "should_block":   False,
            "message":        "git state unreadable — cannot prove staleness, "
                              "proceeding without preflight assertion.",
            "pid":            pid,
            "pid_start_iso":  pid_start_iso,
            "head":           head,
            "head_time_iso":  head_time,
        }
    try:
        head_epoch = time.mktime(
            time.strptime(head_time[:19], "%Y-%m-%dT%H:%M:%S")
        )
    except Exception:
        head_epoch = None

    if head_epoch is not None and pid_start < head_epoch:
        return {
            "should_block":  True,
            "message":       (
                f"PID {pid} started {pid_start_iso} — BEFORE the most recent commit "
                f"({head[:12]} at {head_time}). Whatever you're about to verify "
                "was written after the daemon started; Python does not hot-reload. "
                "Restart the daemon, then re-verify."
            ),
            "pid":            pid,
            "pid_start_iso":  pid_start_iso,
            "head":           head,
            "head_time_iso":  head_time,
        }
    return {
        "should_block":   False,
        "message":        (
            f"PID {pid} (started {pid_start_iso}) postdates HEAD "
            f"{head[:12]} ({head_time}); verification can proceed."
        ),
        "pid":            pid,
        "pid_start_iso":  pid_start_iso,
        "head":           head,
        "head_time_iso":  head_time,
    }


def version_info() -> str:
    """Render the full deploy-guard state for /version. Multi-line, formatted
    for a Telegram reply (under 4096 chars)."""
    state = _read_state() or {}
    check = check_for_stale_code()
    pid = state.get("pid") or os.getpid()
    pid_start_epoch = _pid_start_epoch(pid)
    pid_start_iso = (
        datetime.fromtimestamp(pid_start_epoch).isoformat(timespec="seconds")
        if pid_start_epoch else "(unknown)"
    )
    loaded = check.get("loaded_commit") or "(unknown)"
    head = check.get("current_head") or "(unknown)"
    behind = check.get("commits_behind", 0)
    status = "STALE" if check.get("stale") else "fresh"
    head_time = check.get("head_commit_time") or "(unknown)"
    return (
        "Deploy guard:\n"
        f"  status: {status}\n"
        f"  loaded commit:  {loaded[:12] if loaded != '(unknown)' else loaded}\n"
        f"  current HEAD:   {head[:12] if head != '(unknown)' else head}\n"
        f"  commits behind: {behind}\n"
        f"  HEAD time:      {head_time}\n"
        f"  PID:            {pid}\n"
        f"  PID start:      {pid_start_iso}\n"
        f"  recorded at:    {state.get('loaded_at') or '(unknown)'}\n"
        f"  reason:         {check.get('reason')}"
    )


if __name__ == "__main__":
 # Smoke test — read state if any, then print check + version_info.
    import pprint
    print("=== _read_state ===")
    pprint.pp(_read_state())
    print("\n=== check_for_stale_code ===")
    pprint.pp(check_for_stale_code())
    print("\n=== version_info ===")
    print(version_info())
    print("\n=== verification_preflight ===")
    pprint.pp(verification_preflight())
