#!/usr/bin/env python3
"""
predeploy_check.py — one-shot launchd job: verify `main` is coherent before the 11:45
daily-sourcing cron auto-deploys operator-v2 + the promoted fit_judge from committed main.

Fires ~12 min before the cron (independent of any Claude session). On RED, alerts the operator
via Telegram (notify_telegram). Self-removes its own launchd job after running (one-shot).
Run with --dry to test the checks without alerting or self-cleaning.
"""
import subprocess, os, sys, datetime
from pathlib import Path

from alice import repo_paths

REPO = str(repo_paths.ROOT)
SCRIPTS = f"{REPO}/scripts"
LOG = str(repo_paths.DAILY / "predeploy-check.log")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.operator.jobsearch.predeploycheck.plist")
DRY = "--dry" in sys.argv


def run(cmd):
    return subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True)


def main():
    problems = []
    if run(r"grep -rn '^<<<<<<<\|^=======\|^>>>>>>>' scripts/daily_delta.py scripts/fit_judge.py "
           r"config/fit_model.toml scripts/lead_cuts.py").stdout.strip():
        problems.append("conflict markers present in a core file")
    try:
        if 'version              = "operator-v2"' not in open(f"{REPO}/config/fit_model.toml").read() \
           and 'version = "operator-v2"' not in open(f"{REPO}/config/fit_model.toml").read():
            problems.append("operator-v2 missing from fit_model.toml")
        dd = open(f"{REPO}/scripts/daily_delta.py").read()
        if "ALICE_FIT_JUDGE" not in dd or "fit_judge_cut" not in dd:
            problems.append("G0 judge-promote wiring missing from daily_delta.py")
    except FileNotFoundError as e:
        problems.append(f"core file missing: {e}")

    env = dict(os.environ, PYTHONPATH=SCRIPTS)
    t = subprocess.run([sys.executable, "-m", "pytest", "scripts/", "tests/", "-q"],
                       cwd=REPO, env=env, capture_output=True, text=True)
    tail = (t.stdout.strip().splitlines() or ["(no output)"])[-1]
    if t.returncode != 0:
        problems.append(f"pytest FAILED ({tail})")

    head = run("git rev-parse --short HEAD").stdout.strip()
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if problems:
        status = "RED"
        msg = (f"ALERT: Alice pre-deploy check FAILED ({ts}). main={head} is RED before the "
               f"11:45 auto-deploy.\nProblems:\n- " + "\n- ".join(problems) +
               f"\npytest tail: {tail}\nACTION: revert main to a green commit (last good = 402fd19, "
               f"branch alice-tuning-arch) BEFORE 11:45, or the cron ships a broken state.")
    else:
        status = "GREEN"
        msg = (f"Alice pre-deploy check PASSED ({ts}). main={head} green (suite passed, no conflict "
               f"markers, operator-v2 + G0 intact). The 11:45 auto-deploy is safe.")

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"[{ts}] {status} head={head} problems={problems}\n")
    print(status, "|", msg)

    if DRY:
        return
    try:
        from alice.notify import notify_telegram
        if notify_telegram.available():
            notify_telegram.send(msg)
    except Exception as e:
        with open(LOG, "a") as f:
            f.write(f"[{ts}] telegram send error: {type(e).__name__}: {e}\n")
 # one-shot self-clean: detached so unload doesn't kill us mid-cleanup
    subprocess.Popen(f"sleep 3; launchctl unload {PLIST} 2>/dev/null; rm -f {PLIST}",
                     shell=True, start_new_session=True)


if __name__ == "__main__":
    main()
