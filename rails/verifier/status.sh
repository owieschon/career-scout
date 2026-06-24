#!/usr/bin/env bash
#
# status.sh -- read-only operator dashboard (backs the /status command).
#
# Surfaces, at a glance: active dispatches and their state, BLOCKED handoffs
# with reasons, incidents lacking a linked_case, whether the governor is
# proven (with stamp age), baseline age, and the last verify verdict.
#
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import datetime, glob, json, os, sys

root = sys.argv[1]
now = datetime.datetime.now(datetime.timezone.utc)


def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def age(dt):
    secs = (now - dt).total_seconds()
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def mtime_age(p):
    try:
        return age(datetime.datetime.fromtimestamp(os.path.getmtime(p), datetime.timezone.utc))
    except Exception:
        return "unknown"


def current_tree():
    import subprocess
    try:
        return subprocess.run(["python3", os.path.join(root, "rails", "verifier", "treehash.py")],
                              capture_output=True, text=True, cwd=root, timeout=30).stdout.strip()
    except Exception:
        return "UNKNOWN"


print("==================== agent-rails status ====================\n")

# governor
reg = load(os.path.join(root, "rails", "adversarial", "registry.json"))
if not reg:
    print("  governor:   NOT PROVEN (no registry.json -- run the eval)")
else:
    rid = reg.get("run_id", "")
    when = "unknown"
    try:
        when = age(datetime.datetime.strptime(rid, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc))
    except Exception:
        when = mtime_age(os.path.join(root, "rails", "adversarial", "registry.json"))
    print(f"  governor:   proven, stamped {when} "
          f"(fingerprint {str(reg.get('last_proven_fingerprint',''))[:12]}...)")

# baseline
bp = os.path.join(root, "rails", "verifier", "baseline.json")
b = load(bp)
if b:
    print(f"  baseline:   test_count={b.get('test_count','?')}, updated {mtime_age(bp)}")
else:
    print("  baseline:   MISSING (seed with verify.sh BOOTSTRAP --update-baseline)")

# active dispatches
tree = current_tree()
active = sorted(d for d in glob.glob(os.path.join(root, "rails", "dispatches", "active", "*"))
                if os.path.isdir(d))
print(f"\n  active dispatches: {len(active)}")
for d in active:
    did = os.path.basename(d)
    approved = os.path.exists(os.path.join(d, "APPROVED"))
    v = load(os.path.join(root, "rails", "evidence", did, "verdict.json"))
    if not v:
        state = "no verdict yet"
    elif v.get("status") != "PASS":
        state = f"last verdict {v.get('status','?')}"
    elif v.get("tree_hash") != tree:
        state = "PASS but STALE (tree changed since verify)"
    else:
        state = "PASS (fresh)"
    print(f"      {did}: {'approved' if approved else 'NOT approved'}, {state}")

# blocked handoffs
blocked = sorted(glob.glob(os.path.join(root, "rails", "handoff", "*.BLOCKED.md")))
print(f"\n  blocked handoffs: {len(blocked)}")
for p in blocked:
    first = ""
    try:
        for ln in open(p):
            if ln.strip():
                first = ln.strip()
                break
    except Exception:
        pass
    print(f"      {os.path.basename(p)}: {first[:90]}")

# incidents
incs = [load(p) for p in glob.glob(os.path.join(root, "rails", "incidents", "*.json"))]
incs = [i for i in incs if i]
unlinked = [i for i in incs if not str(i.get("linked_case") or "").strip()]
print(f"\n  incidents: {len(incs)} recorded, {len(unlinked)} UNLINKED")
for i in unlinked:
    print(f"      {i.get('id','?')} [{i.get('trigger','?')}] dispatch={i.get('dispatch','?')} "
          f"-- needs linked_case")

# last verdict overall
verdicts = sorted(glob.glob(os.path.join(root, "rails", "evidence", "*", "verdict.json")),
                  key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
if verdicts:
    lv = load(verdicts[-1])
    if lv:
        print(f"\n  last verify: {lv.get('dispatch','?')} -> {lv.get('status','?')} "
              f"({mtime_age(verdicts[-1])})")
print()
PY
