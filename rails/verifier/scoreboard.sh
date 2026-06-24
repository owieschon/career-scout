#!/usr/bin/env bash
#
# scoreboard.sh -- human-run program-level scoreboard.
#
# Aggregates rails/evidence/ (verdicts + stats.jsonl), rails/incidents/, and
# rails/dispatches/archive/ into a plain-text summary: dispatches completed,
# first-pass verify rate, mean iterations to green, incident count and
# accretion status. Read-only.
#
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import glob, json, os, sys

root = sys.argv[1]


def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


# verdicts: latest per dispatch
verdicts = {}
for p in glob.glob(os.path.join(root, "rails", "evidence", "*", "verdict.json")):
    v = load(p)
    if v and v.get("dispatch"):
        verdicts[v["dispatch"]] = v
passed = {d for d, v in verdicts.items() if v.get("status") == "PASS"}

# stats: verify rejections per dispatch
stats = []
try:
    with open(os.path.join(root, "rails", "evidence", "stats.jsonl")) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    stats.append(json.loads(ln))
                except Exception:
                    pass
except Exception:
    pass

verify_iters = {}  # dispatch -> max verify iteration seen (count of failing runs)
for r in stats:
    if r.get("source") == "verify":
        d = r.get("dispatch")
        verify_iters[d] = max(verify_iters.get(d, 0), int(r.get("iteration", 0)))

archived = [d for d in glob.glob(os.path.join(root, "rails", "dispatches", "archive", "*"))
            if os.path.isdir(d)]

# incidents
incidents = [load(p) for p in glob.glob(os.path.join(root, "rails", "incidents", "*.json"))]
incidents = [i for i in incidents if i]
unlinked = [i for i in incidents if not str(i.get("linked_case") or "").strip()]

# metrics
completed = sorted(passed)
n_done = len(completed)
first_pass = [d for d in completed if verify_iters.get(d, 0) == 0]
iters_to_green = [verify_iters.get(d, 0) + 1 for d in completed]  # failing runs + the green
mean_iters = (sum(iters_to_green) / len(iters_to_green)) if iters_to_green else 0.0

print("==================== agent-rails scoreboard ====================\n")
print(f"  dispatches completed (PASS verdict):  {n_done}")
print(f"  dispatches archived:                  {len(archived)}")
if n_done:
    print(f"  first-pass verify rate:               {len(first_pass)}/{n_done} "
          f"({100*len(first_pass)//n_done}%)")
    print(f"  mean iterations to green:             {mean_iters:.2f}")
else:
    print("  first-pass verify rate:               n/a (no completed dispatches)")
    print("  mean iterations to green:             n/a")
print()
print(f"  incidents recorded:                   {len(incidents)}")
if not incidents:
    print("  accretion status:                     clear (no incidents)")
elif unlinked:
    print(f"  accretion status:                     {len(unlinked)} UNLINKED "
          "(governor will not re-stamp until linked)")
    for i in unlinked:
        print(f"      - {i.get('id','?')} [{i.get('trigger','?')}] dispatch={i.get('dispatch','?')}")
else:
    print("  accretion status:                     clear (all incidents linked to cases)")
print()
PY
