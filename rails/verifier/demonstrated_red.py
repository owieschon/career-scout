#!/usr/bin/env python3
"""
demonstrated_red.py <root> <manifest> <evid>

The demonstrated-red check, extracted from verify.sh. Lives as its own file
(not an inline heredoc) because bash 3.2 mis-parses a here-doc inside a $(...)
command substitution when the body contains an apostrophe -- the verifier
must run on stock macOS. Part of the trust layer (fingerprint-covered); not
agent-editable.

For each break in the dispatch's break_plan it proves green -> red -> green:
the targeted test passes on the real code, goes RED on the applied break, and
passes again after the files are restored. A test never seen red is unproven.
Prints "OK" or "FAIL: ...".
"""
import json, os, shutil, subprocess, sys, tempfile

root, manifest_path, evid = sys.argv[1], sys.argv[2], sys.argv[3]
log = open(os.path.join(evid, "demonstrated_red.log"), "w")

# Stale-bytecode hazard (found by the adversarial eval): a same-second,
# same-size source rewrite passes Python's pyc validation, so a test can
# run cached GOOD bytecode against BROKEN source and stay green. Every
# test invocation in this phase clears the break files' __pycache__ and
# suppresses repopulation. Other ecosystems' caches are caught by the
# green/red/green structure itself; bust them in expect_fail_cmd if hit.
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

def clear_caches(files):
    for fpath in files:
        d = os.path.join(root, os.path.dirname(fpath), "__pycache__")
        shutil.rmtree(d, ignore_errors=True)

def sh(cmd, bust=None):
    if bust:
        clear_caches(bust)
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                       cwd=root, env=ENV)
    log.write(f"$ {cmd}\n(exit {r.returncode})\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}\n")
    return r.returncode

try:
    plan = json.load(open(manifest_path)).get("break_plan", [])
except Exception:
    print("FAIL: no readable break_plan"); sys.exit(0)
if not plan:
    print("FAIL: empty break_plan"); sys.exit(0)

problems = []
for i, item in enumerate(plan):
    desc = item.get("desc", f"break#{i}")
    files = item.get("files", [])
    apply_cmd = item.get("apply", "")
    test_cmd = item.get("expect_fail_cmd", "")
    if not (files and apply_cmd and test_cmd):
        problems.append(f"{desc}: incomplete (needs files, apply, expect_fail_cmd)")
        continue

    # 1. green before red: the targeted test must pass on the real code
    if sh(test_cmd, bust=files) != 0:
        problems.append(f"{desc}: targeted test is not green BEFORE the break; red is meaningless")
        continue

    # 2. back up the exact files, apply the break under a restore guarantee
    tmp = tempfile.mkdtemp(prefix="rails-red-")
    backups = []
    try:
        for fpath in files:
            srcp = os.path.join(root, fpath)
            dstp = os.path.join(tmp, fpath.replace(os.sep, "__"))
            shutil.copy2(srcp, dstp)
            backups.append((srcp, dstp))
        if sh(apply_cmd) != 0:
            problems.append(f"{desc}: break could not be applied")
            continue
        # 3. the broken code must make the test go RED
        if sh(test_cmd, bust=files) == 0:
            problems.append(f"{desc}: test stayed GREEN on broken code -> the test does not prove the behavior")
            continue
    finally:
        for srcp, dstp in backups:
            try:
                shutil.copy2(dstp, srcp)
            except Exception as e:
                problems.append(f"{desc}: RESTORE FAILED for {srcp}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)

    # 4. green again after restore: prove we put it back
    if sh(test_cmd, bust=files) != 0:
        problems.append(f"{desc}: test not green after restore; working tree may be damaged")

log.close()
print("OK" if not problems else "FAIL: " + " | ".join(problems))
