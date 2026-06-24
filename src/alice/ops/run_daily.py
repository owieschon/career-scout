#!/usr/bin/env python3
"""Daily orchestrator — Python equivalent of run_daily.sh.

Invoked directly by launchd to bypass a TCC denial on /bin/bash: launchd's
bash subprocess gets "Operation not permitted" reading the scripts, but
launchd's python3 has Full Disk Access. python3 spawns each step as a
subprocess, which inherits that access.

Each step's stdout/stderr is appended to daily/cron.log.
"""
import datetime
import subprocess
import sys
from pathlib import Path

from alice import repo_paths

REPO = Path(repo_paths.ROOT)
PY = sys.executable  # same interpreter running this orchestrator
LOG = REPO / "daily" / "cron.log"


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(msg + "\n")


def run_step(label, args, fatal=False, alert_on_fail=None):
    """Run one orchestration step via subprocess. stdout/stderr → cron.log.

    alert_on_fail: if set (a short label) and the step exits non-zero, push a
    loud Telegram alert to Jordan Avery distinguishing a CRASH from a zero-result day,
    so a failed sourcing run can never masquerade as "no new roles" (the
    silent-failure class). Best-effort; never raises."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log(f"[{ts}] {label}")
    try:
        result = subprocess.run(
            args, cwd=str(REPO), capture_output=True, text=True, timeout=600,
        )
        if result.stdout:
            log(result.stdout.rstrip())
        if result.stderr:
            log(result.stderr.rstrip())
        if result.returncode != 0:
            log(f"  [step exited code={result.returncode}]")
            if alert_on_fail:
                msg = (f"🚨 {alert_on_fail} FAILED (exit {result.returncode}). "
                       "This is a CRASH, not a zero-result day — the step did not "
                       "complete. Check daily/cron.log.")
                log(f"  [ALERT] {msg}")
                try:
                    from alice.notify import notify_telegram
                    if notify_telegram.available():
                        notify_telegram.send(msg)
                except Exception as _ae:
                    log(f"  [alert send failed: {_ae}]")
            if fatal:
                sys.exit(result.returncode)
    except subprocess.TimeoutExpired:
        log(f"  [step TIMEOUT after 600s]")
    except Exception as e:
        log(f"  [step ERROR: {type(e).__name__}: {e}]")


def main():
    import os
    import uuid
    from alice.observability.telemetry import init_tracing
    init_tracing()  # no-op unless ALICE_TRACING=1
 # One run_id per daily run, exported to the env so every step subprocess
 # (each inherits the parent env) — and every llm.call inside it — stamps
 # the same id. "Show me everything run N did" becomes joinable across
 # spans and the cost log, even while tracing is off.
    run_id = datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    os.environ["ALICE_RUN_ID"] = run_id
    log("")
    log(f"=== run {datetime.datetime.now()} (run_id={run_id}) ===")

 # 0. Pre-run state-repo snapshot (rollback floor for write-bug-class incidents).
 # Local-only; snapshot_state.sh hard-refuses any remote and never pushes.
    run_step("snapshot_state (pre-run rollback point)",
             ["bash", "scripts/snapshot_state.sh", "pre-run"])

 # Saturday: refresh discovery targets
    if datetime.datetime.now().isoweekday() == 6:
        run_step("discover_yc + discover_vc (saturday)",
                 [PY, "scripts/discover_yc.py", "--max", "150"])
        run_step("discover_vc",
                 [PY, "scripts/discover_vc.py"])

 # 1. Process Jordan Avery's replies (status updates + directives + observation capture)
    run_step("imap_reply", [PY, "scripts/imap_reply.py"])

 # 2. Auto-drop focus roles past submitted
    run_step("focus.auto_drop_submitted",
             [PY, "scripts/run_focus_drop_step.py"])

 # 3. Triage observations (Alice's conversational response to feedback)
    run_step("triage_observations",
             [PY, "scripts/triage_observations.py"])

 # 4. Process prep queue — THIS is what generates the queued Northwind Systems + Meridian materials
    run_step("prep_materials",
             [PY, "scripts/prep_materials.py"])

 # 5. Interview prep for newly-scheduled screens
    run_step("interview_prep",
             [PY, "scripts/interview_prep.py"])

 # 6. Debrief integration
    run_step("debrief.integrate_debrief_answers",
             [PY, "scripts/run_debrief_step.py"])

 # 7. Draft outreach for newly-submitted apps
    run_step("draft_outreach",
             [PY, "scripts/draft_outreach.py"])

 # 8. Negotiation prep for newly-offered apps
    run_step("negotiation_prep",
             [PY, "scripts/negotiation_prep.py"])

 # 9. Morning reminder for today's interviews
    run_step("morning_reminder",
             [PY, "scripts/morning_reminder.py"])

 # 10. Confirm + execute any pending directives whose window elapsed
 # (this also handles directives queued by other paths, not just the override)
    run_step("confirm_and_execute",
             [PY, "scripts/confirm_and_execute.py"])

 # 11. Final: source new roles + send daily digest
    run_step("daily_delta (sources new roles + sends digest)",
             [PY, "scripts/daily_delta.py", "--ledger"],
             alert_on_fail="sourcing run")

 # 12. Self-assessment (writes readiness-last.json; pings Jordan Avery only on change).
    run_step("readiness_check",
             [PY, "scripts/readiness_check.py"])

 # 13. Post-run snapshot: capture the day's state drift as a rollback point.
    run_step("snapshot_state (post-run)",
             ["bash", "scripts/snapshot_state.sh", "post-run"])

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log(f"[{ts}] === run complete ===")


if __name__ == "__main__":
    main()
