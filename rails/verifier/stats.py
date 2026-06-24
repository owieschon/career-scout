#!/usr/bin/env python3
"""
stats.py -- rejection statistics (rails/evidence/stats.jsonl).

Every time a check rejects work, one JSON line is appended: which check
fired, on which dispatch, at which loop iteration, when, and from which
gate (verify | stop). The file is append-only and lives under
rails/evidence/, which is already outside the tree hash (treehash.py
excludes it) and outside the governor fingerprint -- so recording a
rejection never invalidates a verdict or the governor.

"iteration" is the count of PRIOR rejecting runs of the same gate for the
same dispatch, plus one: it answers "how many times around the loop did
this dispatch keep failing this gate." All checks rejected in one verify
run share an iteration (they are the same trip around the loop).

CLI:
  python3 stats.py from_verdict <proj> <dispatch> <verdict.json>  # verify gate
  python3 stats.py stop <proj> <dispatch> <check>                 # stop gate
  python3 stats.py summary <proj>                                 # human report

Lives in the trust layer; not agent-editable.
"""
import datetime
import json
import os
import sys


def _path(proj):
    return os.path.join(proj, "rails", "evidence", "stats.jsonl")


def _read(proj):
    rows = []
    try:
        with open(_path(proj)) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def _iteration(proj, source, dispatch):
    """1 + number of prior distinct rejecting runs of this gate for dispatch."""
    stamps = {
        r.get("timestamp")
        for r in _read(proj)
        if r.get("source") == source and r.get("dispatch") == dispatch
    }
    return len(stamps) + 1


def _append(proj, rows):
    os.makedirs(os.path.dirname(_path(proj)), exist_ok=True)
    with open(_path(proj), "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def from_verdict(proj, dispatch, verdict_path):
    try:
        v = json.load(open(verdict_path))
    except Exception:
        return 0
    failed = [k for k, c in (v.get("checks") or {}).items() if not c.get("pass")]
    if not failed:
        return 0
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    it = _iteration(proj, "verify", dispatch)
    _append(proj, [
        {"source": "verify", "check": k, "dispatch": dispatch,
         "iteration": it, "timestamp": ts}
        for k in failed
    ])
    return len(failed)


def stop(proj, dispatch, check):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    it = _iteration(proj, "stop", dispatch)
    _append(proj, [{"source": "stop", "check": check, "dispatch": dispatch,
                    "iteration": it, "timestamp": ts}])
    return 1


def summary(proj):
    rows = _read(proj)
    if not rows:
        print("no rejections recorded (rails/evidence/stats.jsonl is empty/absent)")
        return 0
    by_check, by_dispatch = {}, {}
    for r in rows:
        by_check[r.get("check", "?")] = by_check.get(r.get("check", "?"), 0) + 1
        by_dispatch[r.get("dispatch", "?")] = by_dispatch.get(r.get("dispatch", "?"), 0) + 1
    print(f"rejection stats  ({len(rows)} firings)\n")
    print("by check:")
    for k, n in sorted(by_check.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {k}")
    print("\nby dispatch:")
    for k, n in sorted(by_dispatch.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {k}")
    return 0


def _main(argv):
    if len(argv) == 5 and argv[1] == "from_verdict":
        return 0 if from_verdict(argv[2], argv[3], argv[4]) >= 0 else 1
    if len(argv) == 5 and argv[1] == "stop":
        stop(argv[2], argv[3], argv[4])
        return 0
    if len(argv) == 3 and argv[1] == "summary":
        return summary(argv[2])
    print("usage: stats.py from_verdict|stop|summary ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
