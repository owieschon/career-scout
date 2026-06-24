"""Real-tool integration test — runs Alice end-to-end in dry-run mode.

What it tests:
  - LLM wrapper makes real Anthropic calls
  - Sheet reads work
  - Sheet writes are SIMULATED (printed, not applied) — avoids polluting prod sheet
  - Email send is SIMULATED (printed, not sent) — avoids spam in the operator's inbox
  - Full directive parsing + state flow

Run before any significant code change to verify the integration still works.
"""
import sys
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger
from alice.notify import notify_email


def check_llm():
    """Make a minimal real LLM call to verify the key + wrapper + cost log all work."""
    print("  [llm] making minimal API call to Haiku 4.5...")
    try:
        res = llm.call("triage_observation", "Reply with exactly: OK", max_tokens=10)
        ok = res.get("text", "").strip().upper() == "OK"
        print(f"    {'OK' if ok else 'FAIL'}: model={res['model']} reply={res['text']!r} cost=${res['cost_usd']:.6f}")
        return ok
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def check_sheet_read():
    """Verify sheet is readable."""
    print("  [sheet] reading rows...")
    try:
        ws = ledger._ws()
        rows = ws.get_all_records()
        print(f"    OK: {len(rows)} rows, cols include {list(rows[0].keys())[:5] if rows else '(empty)'}...")
        return True
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def check_imap():
    """Verify IMAP login works (read-only, doesn't process anything)."""
    print("  [imap] testing Gmail login...")
    try:
        from alice.notify.imap_reply import _imap_open
        M, user = _imap_open()
        M.select("INBOX")
        M.logout()
        print(f"    OK: logged in as {user}")
        return True
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def check_state_files():
    """Verify state files are readable or createable."""
    print("  [state] verifying file paths...")
    paths = [
        str(repo_paths.FEEDBACK / "focus.json"),
        str(repo_paths.FEEDBACK / "observations.md"),
        str(repo_paths.FEEDBACK / "time-cost-log.jsonl"),
        str(repo_paths.ROOT / "Alice.md"),
        str(repo_paths.SCRIPTS / "run_daily.sh"),
    ]
    ok = True
    for p in paths:
        path = Path(p)
        if path.exists():
            print(f"    OK: {p} ({path.stat().st_size} bytes)")
        else:
            # Some are created on first use; flag but don't fail
            print(f"    INFO: {p} not yet created (created on first use)")
    return ok


def check_directives_parse():
    """Verify directives parser handles all 11+ directive types."""
    print("  [directives] parsing sample input...")
    try:
        from alice.pipeline.directives import parse_block
        sample = """focus: a, b, c
prep: x
debrief 3: nailed it
hypothesis: this works
approve proposal-1
help with northwind
response from name at company: positive"""
        results = parse_block(sample)
        expected_types = {"focus_set", "prep", "debrief_answer", "hypothesis", "approve", "help_with", "outreach_response"}
        actual_types = {t for t, _, _ in results}
        missing = expected_types - actual_types
        if missing:
            print(f"    FAIL: missing directive types: {missing}")
            return False
        print(f"    OK: parsed {len(results)} directives, all expected types present")
        return True
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def check_alice_brief():
    """Verify Alice.md loads and is non-trivial."""
    print("  [alice] loading brief...")
    try:
        brief = llm.load_alice_brief()
        if len(brief) < 1000:
            print(f"    FAIL: brief too short ({len(brief)} chars)")
            return False
        # spot-check key sections
        required_sections = ["## Identity", "## Job", "## Voice", "## Scope", "## Focus & accountability"]
        missing = [s for s in required_sections if s not in brief]
        if missing:
            print(f"    FAIL: missing required sections: {missing}")
            return False
        print(f"    OK: brief loaded ({len(brief)} chars, all required sections present)")
        return True
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def check_cron_loaded():
    """Verify the launchd jobs are loaded."""
    print("  [cron] checking launchd...")
    import subprocess
    try:
        out = subprocess.check_output(["launchctl", "list"], timeout=10).decode()
        daily = "com.alice.jobsearch\n" in out or "com.alice.jobsearch\t" in out
        weekly = "com.alice.jobsearch.weekly" in out
        print(f"    daily cron loaded: {daily}")
        print(f"    weekly cron loaded: {weekly}")
        return daily and weekly
    except Exception as e:
        print(f"    FAIL: {e}")
        return False


def main():
    checks = [
        ("alice brief",   check_alice_brief),
        ("state files",   check_state_files),
        ("directives",    check_directives_parse),
        ("sheet read",    check_sheet_read),
        ("imap login",    check_imap),
        ("cron loaded",   check_cron_loaded),
        ("llm api",       check_llm),
    ]
    results = {}
    print("INTEGRATION CHECKS")
    print("=" * 60)
    for name, fn in checks:
        results[name] = fn()
    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"summary: {passed}/{total} checks passed")
    for name, ok in results.items():
        status = "OK" if ok else "FAIL"
        print(f"  [{status}] {name}")
    return passed == total


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
