#!/usr/bin/env python3
"""
Prints a hash of the exact working-tree state: HEAD + staged + unstaged +
untracked file list. The verifier stamps its verdict with this; the stop
gate recomputes it. If they differ, the green is stale and does not count.

Single source of truth for freshness. Lives in the trust layer; not
agent-editable.
"""
import hashlib
import subprocess
import sys


def out(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=30)
        return r.stdout
    except Exception:
        return "?"


h = hashlib.sha256()
# rails/evidence is excluded: it is the verifier's own output and is
# guard-protected from agent writes, so including it would make every
# fresh verdict invalidate itself (the verdict file changes the very
# hash it was stamped with). Found by adversarial eval case 08.
EXC = ":(exclude)rails/evidence"
for args in (
    ["rev-parse", "HEAD"],
    ["diff", "--", ".", EXC],
    ["diff", "--cached", "--", ".", EXC],
    ["ls-files", "--others", "--exclude-standard", "--", ".", EXC],
):
    h.update(out(args).encode("utf-8", "replace"))

sys.stdout.write(h.hexdigest())
