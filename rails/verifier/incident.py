#!/usr/bin/env python3
"""
incident.py -- the incident ledger (rails/incidents/).

An incident is a durable, tamper-evident record of a governance event that
must not be silently forgotten:
  - a dispatch ended BLOCKED, or
  - verify.sh flipped PASS -> FAIL on an UNCHANGED tree hash (a check that
    once certified the exact same tree now rejects it: either the old PASS
    was wrong or the new FAIL is -- both demand a human look).

Records are append-only. The trust layer (verify.sh, gate_stop.py) WRITES
them; the agent may not edit or delete an existing one (guard_files.py /
guard_bash.py enforce that). A human links each incident to the eval case
that now covers its failure shape by setting "linked_case"; run_eval.sh
refuses to re-stamp the governor while any incident is still unlinked --
the accretion rule, mechanized: a class that bit us once does not get
forgotten until a test guarantees it cannot bite twice.

CLI:
  python3 incident.py record <proj> <dispatch> <trigger> <check> \\
                             <claimed> <observed> <tree_hash>
  python3 incident.py check  <proj>        # exit 1 if any incident unlinked

Lives in the trust layer; not agent-editable.
"""
import datetime
import glob
import json
import os
import sys

VALID_TRIGGERS = ("blocked", "pass_to_fail_unchanged_tree")


def _incidents_dir(proj):
    return os.path.join(proj, "rails", "incidents")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def record(proj, dispatch, trigger, check, claimed, observed, tree_hash):
    """Append one incident, idempotent on (dispatch, trigger, tree_hash).

    Returns the record path, or None if a matching open record already exists
    (so repeated verify/stop runs on the same state do not spam the ledger).
    """
    d = _incidents_dir(proj)
    os.makedirs(d, exist_ok=True)
    for existing in glob.glob(os.path.join(d, "*.json")):
        r = _load(existing)
        if r and (r.get("dispatch"), r.get("trigger"), r.get("tree_hash")) == (
            dispatch, trigger, tree_hash
        ):
            return None  # already recorded this exact event
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stamp = ts.replace(":", "").replace("-", "").split(".")[0] + "Z"
    safe_dispatch = "".join(c if c.isalnum() or c in "-_" else "_" for c in dispatch)
    rec = {
        "id": f"INC-{stamp}-{safe_dispatch}",
        "timestamp": ts,
        "dispatch": dispatch,
        "trigger": trigger,
        "check": check,
        "claimed": claimed,
        "observed": observed,
        "tree_hash": tree_hash,
        "linked_case": None,
    }
    path = os.path.join(d, rec["id"] + ".json")
    # never clobber: if the id collides, suffix it
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"{rec['id']}-{n}.json")
        n += 1
    with open(path, "w") as f:
        json.dump(rec, f, indent=2)
    return path


def unlinked(proj):
    """Return a list of (id, path) for incidents lacking a linked_case."""
    out = []
    for p in sorted(glob.glob(os.path.join(_incidents_dir(proj), "*.json"))):
        r = _load(p)
        if r is None:
            out.append((os.path.basename(p), p))  # unreadable == unaccounted
            continue
        lc = r.get("linked_case")
        if not lc or not str(lc).strip():
            out.append((r.get("id", os.path.basename(p)), p))
    return out


def _main(argv):
    if len(argv) >= 2 and argv[1] == "record":
        if len(argv) != 9:
            print("usage: incident.py record <proj> <dispatch> <trigger> "
                  "<check> <claimed> <observed> <tree_hash>", file=sys.stderr)
            return 2
        _, _, proj, dispatch, trigger, check, claimed, observed, tree_hash = argv
        if trigger not in VALID_TRIGGERS:
            print(f"unknown trigger '{trigger}'", file=sys.stderr)
            return 2
        path = record(proj, dispatch, trigger, check, claimed, observed, tree_hash)
        print(path or "(duplicate; not re-recorded)")
        return 0
    if len(argv) == 3 and argv[1] == "check":
        proj = argv[2]
        miss = unlinked(proj)
        if miss:
            print("UNLINKED INCIDENTS (set linked_case to the eval case that "
                  "now covers each, then re-run the eval):", file=sys.stderr)
            for iid, p in miss:
                print(f"  {iid}  ({os.path.relpath(p, proj)})", file=sys.stderr)
            return 1
        return 0
    print(__doc__.strip().splitlines()[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
