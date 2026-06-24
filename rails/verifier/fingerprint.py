#!/usr/bin/env python3
"""
Prints a fingerprint of the governor: every file whose change could alter
what the checks catch. verify.sh refuses to gate work if this fingerprint
does not match the one stamped by the last full adversarial-eval pass
(spec section 4: a framework change runs the eval BEFORE it takes force).

Covered: rails/verifier/**, .claude/hooks/**, .claude/settings.json,
rails/adversarial/** (excluding registry.json, which is the stamp itself).
Deliberately NOT covered: rails/config.json (per-repo adapter; the eval
proves mechanisms in its own sandbox, so adapter changes do not invalidate
the mechanism proof).

Lives in the trust layer; not agent-editable.
"""
import hashlib
import os
import sys

root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
if len(sys.argv) > 1:
    root = sys.argv[1]

targets = ["rails/verifier", ".claude/hooks", "rails/adversarial"]
single_files = [".claude/settings.json"]
EXCLUDE_NAMES = {"registry.json", "__pycache__"}

entries = []
for t in targets:
    base = os.path.join(root, t)
    if not os.path.isdir(base):
        continue
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_NAMES]
        for fn in sorted(filenames):
            if fn in EXCLUDE_NAMES or fn.endswith(".pyc"):
                continue
            p = os.path.join(dirpath, fn)
            entries.append(os.path.relpath(p, root))
for f in single_files:
    if os.path.isfile(os.path.join(root, f)):
        entries.append(f)

h = hashlib.sha256()
for rel in sorted(set(entries)):
    h.update(rel.encode())
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            h.update(fh.read())
    except Exception:
        h.update(b"?")

sys.stdout.write(h.hexdigest())
