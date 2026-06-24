#!/usr/bin/env bash
#
# verify.sh <dispatch-id> [--update-baseline]
#
# The keystone (framework Section 3): your distrust, mechanized. A dispatch
# is DONE only if every check below passes against the CURRENT tree:
#
#   manifest_complete   the dispatch actually declares its proof obligations
#   full_suite          suite runs to completion, exit 0, count >= baseline
#                       (count-is-the-tell: a dropped count is a false green)
#   load_bearing        the named load-bearing tests were collected/ran BY NAME
#                       (not-exercised is not pass)
#   live_path           grep proves the tested code is on the path that ships
#   demonstrated_red    each break in the plan makes its test go red, then
#                       green again after restore (a test never seen red is
#                       unproven)
#
# Writes rails/evidence/<id>/verdict.json stamped with the tree hash.
# The Stop gate only honors a PASS whose tree hash matches the current tree.
#
# This file is the trust layer: not agent-editable. --update-baseline is
# human-only (the bash guard blocks the agent from invoking it).
#
set -u

DISPATCH="${1:-}"
MODE="${2:-}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CFG="$ROOT/rails/config.json"
BASELINE="$ROOT/rails/verifier/baseline.json"

if [ -z "$DISPATCH" ]; then
  # If exactly one dispatch is active, use it.
  ACT=()
  while IFS= read -r _ln; do ACT+=("$_ln"); done \
    < <(find "$ROOT/rails/dispatches/active" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  if [ "${#ACT[@]}" -eq 1 ]; then
    DISPATCH="$(basename "${ACT[0]}")"
  else
    echo "usage: verify.sh <dispatch-id>   (found ${#ACT[@]} active dispatches)" >&2
    exit 2
  fi
fi

DDIR="$ROOT/rails/dispatches/active/$DISPATCH"
MANIFEST="$DDIR/manifest.json"
EVID="$ROOT/rails/evidence/$DISPATCH"
mkdir -p "$EVID"

jqpy() { python3 -c "$1" "${@:2}"; }

cfg_get() {
  jqpy "
import json,sys
try:
    cfg=json.load(open('$CFG'))
except Exception:
    cfg={}
v=cfg.get(sys.argv[1], sys.argv[2] if len(sys.argv)>2 else '')
print(v if isinstance(v,str) else json.dumps(v))
" "$@"
}

TEST_CMD="$(cfg_get test_cmd 'pytest -q')"
COUNT_REGEX="$(cfg_get count_regex '([0-9]+) passed')"
COLLECT_CMD="$(cfg_get collect_cmd '')"

PASS=0; FAIL=1
# bash 3.2 portability: bash 3.2 (stock macOS) has no associative arrays. The
# check set is fixed and its keys are valid identifiers, so results live in
# plain vars RESULT_<key>/DETAIL_<key>, read via indirect expansion (${!ref})
# in the verdict loops below. No check semantics change.
CUR_BASH="${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"

note() { printf '%s\n' "$*"; }

# ---------------------------------------------------------- governor proven
# Spec section 4: a change to the governor runs the adversarial eval BEFORE
# it takes force. Mechanized: if the trust-layer fingerprint differs from
# the one stamped at the last full eval pass, this verifier refuses to
# certify work until rails/adversarial/run_eval.sh passes again.
REG="$ROOT/rails/adversarial/registry.json"
GOV="$(jqpy "
import json,subprocess
cur=subprocess.run(['python3','$ROOT/rails/verifier/fingerprint.py','$ROOT'],capture_output=True,text=True).stdout.strip()
try:
    reg=json.load(open('$REG'))
except Exception:
    print('FAIL: no adversarial registry; the checks are unproven. Run: bash rails/adversarial/run_eval.sh'); raise SystemExit
if reg.get('last_proven_fingerprint')!=cur:
    print('FAIL: trust layer changed since last adversarial proof. A changed governor does not gate work until re-proven. Run: bash rails/adversarial/run_eval.sh')
    raise SystemExit
env=reg.get('environment',{})
import sys,platform
pyv='.'.join(platform.python_version_tuple()[:2])
if env.get('python') not in (None,'unknown',pyv):
    print(f\"FAIL: environment changed (python {env.get('python')} -> {pyv}); spec section 4: re-run the eval\"); raise SystemExit
bashv='$CUR_BASH'
if env.get('bash') not in (None,'unknown',bashv):
    print(f\"FAIL: environment changed (bash {env.get('bash')} -> {bashv}); spec section 4: re-run the eval\"); raise SystemExit
import shutil
cc_rec=env.get('claude_code')
cc_now='unknown'
if shutil.which('claude'):
    try:
        cc_now=subprocess.run(['claude','--version'],capture_output=True,text=True,timeout=10).stdout.strip() or 'unknown'
    except Exception:
        cc_now='unknown'
if cc_rec not in (None,'unknown','') and cc_now not in ('unknown','') and cc_rec!=cc_now:
    print(f\"FAIL: environment changed (claude_code {cc_rec} -> {cc_now}); spec section 4: re-run the eval\"); raise SystemExit
print('OK')
")"
if [ "$GOV" = "OK" ]; then
  RESULT_governor_proven=$PASS; DETAIL_governor_proven="fingerprint matches last eval pass"
else
  RESULT_governor_proven=$FAIL; DETAIL_governor_proven="${GOV#FAIL: }"
fi

# ---------------------------------------------------------------- manifest
if [ ! -f "$MANIFEST" ]; then
  RESULT_manifest_complete=$FAIL
  DETAIL_manifest_complete="missing $MANIFEST"
else
  M_OK="$(jqpy "
import json,sys
m=json.load(open('$MANIFEST'))
probs=[]
if not m.get('live_path_greps'): probs.append('no live_path_greps')
if not m.get('load_bearing_tests'): probs.append('no load_bearing_tests')
if not m.get('break_plan'): probs.append('no break_plan (demonstrated-red is mandatory)')
print('OK' if not probs else '; '.join(probs))
")"
  if [ "$M_OK" = "OK" ]; then
    RESULT_manifest_complete=$PASS; DETAIL_manifest_complete="ok"
  else
    RESULT_manifest_complete=$FAIL; DETAIL_manifest_complete="$M_OK"
  fi
fi

# ---------------------------------------------------------------- decisions
# Senior-engineer judgment cannot be machine-graded; its DISCLOSURE can be
# machine-required. DECISIONS.md (options considered, trade-offs, choice
# with grounds, reversibility) must exist before "done", and it travels in
# the handoff for human review. The human may exempt trivial dispatches
# via "decisions_required": false in the manifest they approve.
DEC="$(jqpy "
import json,os
try:
    m=json.load(open('$MANIFEST'))
except Exception:
    m={}
if m.get('decisions_required', True) is False:
    print('OK exempted by approved manifest'); raise SystemExit
p='$DDIR/DECISIONS.md'
if not os.path.isfile(p) or os.path.getsize(p)==0:
    print('FAIL: DECISIONS.md missing/empty -- undisclosed judgment is the violation (write options, trade-offs, choice, reversibility)'); raise SystemExit
txt=open(p,errors='replace').read().lower()
need=['options considered','trade-offs','choice','reversibility']
miss=[n for n in need if n not in txt]
print('OK structurally complete' if not miss else 'FAIL: DECISIONS.md missing sections: '+', '.join(miss))
")"
case "$DEC" in
  OK*) RESULT_decisions=$PASS; DETAIL_decisions="${DEC#OK }";;
  *)   RESULT_decisions=$FAIL; DETAIL_decisions="${DEC#FAIL: }";;
esac

# -------------------------------------------------------------- full suite
note "== full suite: $TEST_CMD"
( cd "$ROOT" && bash -c "$TEST_CMD" ) >"$EVID/full_suite.log" 2>&1
SUITE_EXIT=$?
COUNT="$(jqpy "
import re,sys
txt=open('$EVID/full_suite.log',errors='replace').read()
m=re.search(r'''$COUNT_REGEX''',txt)
print(m.group(1) if m else '-1')
")"

if [ "$MODE" = "--update-baseline" ]; then
  if [ "$SUITE_EXIT" -ne 0 ] || [ "$COUNT" = "-1" ]; then
    note "refusing to baseline a failing or uncountable run (exit=$SUITE_EXIT count=$COUNT)"; exit 2
  fi
  OLD_BASE="$(jqpy "
import json
try: print(json.load(open('$BASELINE')).get('test_count',0))
except Exception: print(0)
")"
  # Downward rebaselining is the test-deletion laundering path: raising the
  # floor is conservative and delegable, lowering it hides removed tests and
  # is human-only (guard_bash.py blocks --allow-shrink for the agent).
  ALLOW_SHRINK=0; [ "${3:-}" = "--allow-shrink" ] && ALLOW_SHRINK=1
  if [ "$COUNT" -lt "$OLD_BASE" ] && [ "$ALLOW_SHRINK" -ne 1 ]; then
    note "refusing to LOWER the baseline ($OLD_BASE -> $COUNT) without --allow-shrink."
    note "downward rebaselining hides deleted tests and is human-only. If the"
    note "shrink is genuinely intended, a human re-runs with --allow-shrink."
    exit 2
  fi
  printf '{"test_count": %s}\n' "$COUNT" > "$BASELINE"
  note "baseline updated: test_count=$COUNT (was $OLD_BASE)"; exit 0
fi

BASE_COUNT="$(jqpy "
import json
try: print(json.load(open('$BASELINE')).get('test_count',0))
except Exception: print(0)
")"

if [ "$SUITE_EXIT" -ne 0 ]; then
  RESULT_full_suite=$FAIL; DETAIL_full_suite="suite exit=$SUITE_EXIT (see full_suite.log)"
elif [ "$COUNT" = "-1" ]; then
  RESULT_full_suite=$FAIL; DETAIL_full_suite="could not parse test count (count_regex='$COUNT_REGEX'); a run you cannot count is not a green"
elif [ "$COUNT" -lt "$BASE_COUNT" ]; then
  RESULT_full_suite=$FAIL; DETAIL_full_suite="count dropped: $COUNT < baseline $BASE_COUNT (silent drop / partial collection)"
else
  RESULT_full_suite=$PASS; DETAIL_full_suite="exit 0, $COUNT tests (baseline $BASE_COUNT)"
fi

# ------------------------------------------------------------ load-bearing
note "== load-bearing tests, by name"
if [ -n "$COLLECT_CMD" ]; then
  ( cd "$ROOT" && bash -c "$COLLECT_CMD" ) >"$EVID/collect.log" 2>&1 || true
  SRC="$EVID/collect.log"
else
  SRC="$EVID/full_suite.log"
fi
LB_MISS="$(jqpy "
import json,sys
names=[]
try:
    m=json.load(open('$MANIFEST'))
    names+= m.get('load_bearing_tests',[])
except Exception: pass
try:
    for ln in open('$ROOT/rails/verifier/load_bearing.txt'):
        ln=ln.strip()
        if ln and not ln.startswith('#'): names.append(ln)
except Exception: pass
hay=open('$SRC',errors='replace').read()
import os
def seen(n):
    base=os.path.basename(n)
    stem=os.path.splitext(base)[0]
    return (n in hay) or (base in hay) or (stem in hay)
missing=[n for n in names if not seen(n)]
print('OK' if not missing else 'NOT COLLECTED: '+', '.join(missing))
")"
if [ "$LB_MISS" = "OK" ]; then
  RESULT_load_bearing=$PASS; DETAIL_load_bearing="all named tests present in $(basename "$SRC")"
else
  RESULT_load_bearing=$FAIL; DETAIL_load_bearing="$LB_MISS"
fi

# --------------------------------------------------------------- live path
note "== live-path greps"
LP="$(jqpy "
import json,subprocess
try:
    m=json.load(open('$MANIFEST')); greps=m.get('live_path_greps',[])
except Exception:
    greps=[]
fails=[]; hits=[]
for g in greps:
    pat=g.get('pattern',''); path=g.get('path','.')
    r=subprocess.run(['grep','-RnE',pat,path],capture_output=True,text=True,cwd='$ROOT')
    if r.returncode!=0 or not r.stdout.strip():
        fails.append(f\"{pat} in {path}\")
    else:
        hits.append(r.stdout.strip().splitlines()[0])
open('$EVID/live_path.log','w').write('\n'.join(hits))
print('OK' if not fails else 'NO MATCH: '+'; '.join(fails))
")"
if [ "$LP" = "OK" ]; then
  RESULT_live_path=$PASS; DETAIL_live_path="all greps matched (live_path.log)"
else
  RESULT_live_path=$FAIL; DETAIL_live_path="$LP -- correct in isolation is not load-bearing live"
fi

# --------------------------------------------------------- demonstrated red
note "== demonstrated-red"
# Extracted to its own file: bash 3.2 mis-parses a here-doc inside $(...) when
# the body has an apostrophe (e.g. "Python's pyc"). A plain script call is the
# bash-3.2-safe form and changes no check semantics.
DR="$(python3 "$ROOT/rails/verifier/demonstrated_red.py" "$ROOT" "$MANIFEST" "$EVID")"
if [ "$DR" = "OK" ]; then
  RESULT_demonstrated_red=$PASS; DETAIL_demonstrated_red="every break went red, restore proven green (demonstrated_red.log)"
else
  RESULT_demonstrated_red=$FAIL; DETAIL_demonstrated_red="$DR"
fi

# ----------------------------------------------------------------- verdict
TREE="$(python3 "$ROOT/rails/verifier/treehash.py")"
HEAD="$(cd "$ROOT" && git rev-parse HEAD 2>/dev/null || echo 'NO-GIT')"

STATUS="PASS"
for k in governor_proven manifest_complete decisions full_suite load_bearing live_path demonstrated_red; do
  _r="RESULT_$k"
  [ "${!_r:-1}" -ne 0 ] && STATUS="FAIL"
done

python3 - "$EVID/verdict.json" "$ROOT" <<PYEOF
import json, sys, datetime, os
verdict_path, root = sys.argv[1], sys.argv[2]
try:
    prior = json.load(open(verdict_path))
except Exception:
    prior = None
checks = {
  "governor_proven":   {"pass": ${RESULT_governor_proven:-1} == 0,   "detail": """${DETAIL_governor_proven:-}"""},
  "manifest_complete": {"pass": ${RESULT_manifest_complete:-1} == 0, "detail": """${DETAIL_manifest_complete:-}"""},
  "decisions":         {"pass": ${RESULT_decisions:-1} == 0,         "detail": """${DETAIL_decisions:-}"""},
  "full_suite":        {"pass": ${RESULT_full_suite:-1} == 0,        "detail": """${DETAIL_full_suite:-}"""},
  "load_bearing":      {"pass": ${RESULT_load_bearing:-1} == 0,      "detail": """${DETAIL_load_bearing:-}"""},
  "live_path":         {"pass": ${RESULT_live_path:-1} == 0,         "detail": """${DETAIL_live_path:-}"""},
  "demonstrated_red":  {"pass": ${RESULT_demonstrated_red:-1} == 0,  "detail": """${DETAIL_demonstrated_red:-}"""},
}
status = "$STATUS"
tree = "$TREE"
# PASS -> FAIL on an UNCHANGED tree hash is a governance event: a check that
# certified this exact tree now rejects it (either the old PASS was wrong or
# the new FAIL is). Record it -- the trust layer writes; record() is idempotent.
if prior and prior.get("status") == "PASS" and status == "FAIL" \
        and prior.get("tree_hash") == tree:
    try:
        sys.path.insert(0, os.path.join(root, "rails", "verifier"))
        import incident
        fired = [k for k, v in checks.items() if not v["pass"]]
        incident.record(root, "$DISPATCH", "pass_to_fail_unchanged_tree",
                        ", ".join(fired) or "unknown",
                        "a prior verify PASS was stamped on this exact tree_hash",
                        "verify now FAILs on the identical tree_hash",
                        tree)
    except Exception:
        pass
json.dump({
  "dispatch": "$DISPATCH",
  "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
  "head": "$HEAD",
  "tree_hash": tree,
  "status": status,
  "checks": checks,
}, open(verdict_path, "w"), indent=2)
PYEOF

# Rejection stat: one append-only line per failing check (rails/evidence,
# outside the tree hash). Non-fatal: a stats hiccup never fails a verdict.
python3 "$ROOT/rails/verifier/stats.py" from_verdict "$ROOT" "$DISPATCH" "$EVID/verdict.json" >/dev/null 2>&1 || true

note ""
note "================ VERDICT: $STATUS ($DISPATCH) ================"
for k in governor_proven manifest_complete decisions full_suite load_bearing live_path demonstrated_red; do
  _r="RESULT_$k"; _d="DETAIL_$k"
  if [ "${!_r:-1}" -eq 0 ]; then ICON="PASS"; else ICON="FAIL"; fi
  note "  [$ICON] $k: ${!_d:-}"
done
note "evidence: rails/evidence/$DISPATCH/  (verdict stamped to tree $TREE)"

[ "$STATUS" = "PASS" ] && exit 0 || exit 1
