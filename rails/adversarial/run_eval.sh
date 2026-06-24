#!/usr/bin/env bash
#
# run_eval.sh: the demonstrate-the-catch layer for the governor.
#
# Runs every known-bad case (core, then project) in a fresh sandbox each,
# proving each check fires on its violation and stays quiet on clean work.
# On a FULL pass it stamps rails/adversarial/registry.json with the current
# governor fingerprint; verify.sh refuses to certify any dispatch until
# that stamp matches (a changed governor does not take force unproven).
#
# Find, don't fix (spec section 7): this script surfaces and proves. It
# never modifies the trust layer. Gaps it finds become human work.
#
# Isolation (spec section 6): core and project scopes run and report
# separately; a project's gap never greens the core, and vice versa.
set -u
HOST="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$HOST/rails/adversarial"
RUNID="$(date -u +%Y%m%dT%H%M%SZ)"
EVID="$HOST/rails/evidence/ADVERSARIAL/$RUNID"
mkdir -p "$EVID"

# bash 3.2 portability: no associative arrays. Case names are dynamic (and
# contain '/'), so status is held in parallel indexed arrays looked up by a
# linear scan -- the case count is tiny.
NAMES=(); STATES=()
_set_status() { NAMES+=("$1"); STATES+=("$2"); }
_status_of() {  # _status_of <name> -> echoes state or MISSING
  local q="$1" i
  for i in "${!NAMES[@]}"; do
    [ "${NAMES[$i]}" = "$q" ] && { printf '%s' "${STATES[$i]}"; return; }
  done
  printf 'MISSING'
}
TOTAL=0; FAILED=0

run_scope() {  # run_scope <scope>
  local scope="$1"
  local dir="$HERE/cases/$scope"
  [ -d "$dir" ] || return 0
  shopt -s nullglob
  for case_sh in "$dir"/*.sh; do
    local name; name="$scope/$(basename "$case_sh" .sh)"
    TOTAL=$((TOTAL + 1))
    printf '== %s\n' "$name"
    local sb; sb="$(bash "$HERE/fixture.sh" 2>"$EVID/$(basename "$case_sh").fixture.err")"
    if [ -z "$sb" ] || [ ! -d "$sb" ]; then
      _set_status "$name" "FAIL (fixture)"; FAILED=$((FAILED + 1))
      printf '    FAIL fixture did not build\n'; continue
    fi
    if SANDBOX="$sb" bash "$case_sh" >"$EVID/$(basename "$case_sh").log" 2>&1; then
      _set_status "$name" "PASS"
    else
      _set_status "$name" "FAIL"; FAILED=$((FAILED + 1))
    fi
    sed 's/^/  /' "$EVID/$(basename "$case_sh").log"
    [ "${KEEP_SANDBOX:-0}" = "1" ] || rm -rf "$sb"
  done
}

run_scope core
run_scope project

# ---- coverage register: spec section 3 classes -> cases ------------------
classmap() {  # classmap <class> -> echoes the case path(s) proving it
  case "$1" in
    weaken-a-check)           echo "core/01_weaken_a_check" ;;
    off-live-path)            echo "core/02_off_live_path" ;;
    vacuous-test)             echo "core/03_vacuous_test" ;;
    partial-run-green)        echo "core/04_partial_run_green" ;;
    claim-not-landed)         echo "core/05_claim_not_landed" ;;
    not-exercised-as-pass)    echo "core/06_not_exercised" ;;
    boundary-breach)          echo "core/07_boundary_breach" ;;
    inflation)                echo "core/08_stale_green_stop_gate + core/09_forge_a_verdict" ;;
    undisclosed-judgment)     echo "core/10_undisclosed_decisions" ;;
    unproven-governor-change) echo "core/11_governor_drift" ;;
    incident-ledger)          echo "core/12_incident_ledger" ;;
  esac
}

echo
echo "=============== COVERAGE REGISTER (known classes) ==============="
for cls in weaken-a-check off-live-path vacuous-test partial-run-green \
           claim-not-landed not-exercised-as-pass boundary-breach inflation \
           undisclosed-judgment unproven-governor-change incident-ledger; do
  cases="$(classmap "$cls")"
  ok=1
  for c in ${cases//+/ }; do
    c="$(echo "$c" | xargs)"; [ -z "$c" ] && continue
    [ "$(_status_of "$c")" = "PASS" ] || ok=0
  done
  if [ "$ok" -eq 1 ]; then v="PROVEN"; else v="NOT PROVEN"; fi
  printf '  %-26s %-11s (%s)\n' "$cls" "$v" "$cases"
done
echo "  (gaps beyond these classes are unenumerated by definition;"
echo "   the boundaries bound their blast radius -- spec section 5)"

echo
echo "=============== CASE RESULTS (core | project isolated) ==========="
for k in $(printf '%s\n' "${NAMES[@]}" | sort); do
  printf '  %-34s %s\n' "$k" "$(_status_of "$k")"
done

# ---- stamp only on a full pass -------------------------------------------
if [ "$FAILED" -eq 0 ] && [ "$TOTAL" -gt 0 ]; then
  # Accretion gate: an unlinked incident is a failure class that bit us and is
  # not yet guaranteed against. The governor does not re-stamp until every
  # incident points at the eval case that now covers it.
  if ! python3 "$HOST/rails/verifier/incident.py" check "$HOST"; then
    echo
    echo "GOVERNOR NOT PROVEN: unlinked incident(s) above. Set linked_case on"
    echo "each to the eval case that now covers it, then re-run. NOT stamped."
    echo "evidence: rails/evidence/ADVERSARIAL/$RUNID/"
    exit 1
  fi
  # CI proves but does not stamp: stamping the registry is a local, human-
  # released act, never something a CI runner writes. CI just asserts green.
  if [ "${RAILS_NO_STAMP:-0}" = "1" ]; then
    echo
    echo "GOVERNOR PROVEN (CI mode, RAILS_NO_STAMP=1): all $TOTAL cases passed."
    echo "Registry NOT stamped -- stamping stays a local human-released act."
    echo "evidence: rails/evidence/ADVERSARIAL/$RUNID/"
    exit 0
  fi
  python3 - "$HOST" "$RUNID" "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}" <<'PYEOF'
import json, platform, shutil, subprocess, sys
host, runid, bashv = sys.argv[1], sys.argv[2], sys.argv[3]
fp = subprocess.run(["python3", host + "/rails/verifier/fingerprint.py", host],
                    capture_output=True, text=True).stdout.strip()
cc = "unknown"
if shutil.which("claude"):
    try:
        cc = subprocess.run(["claude", "--version"], capture_output=True,
                            text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        pass
json.dump({
    "last_proven_fingerprint": fp,
    "run_id": runid,
    "stamped_by": "run_eval.sh (full pass)",
    "environment": {
        "python": ".".join(platform.python_version_tuple()[:2]),
        "bash": bashv,
        "claude_code": cc,
    },
}, open(host + "/rails/adversarial/registry.json", "w"), indent=2)
print(f"\nGOVERNOR PROVEN: registry stamped (fingerprint {fp[:16]}..., cc={cc})")
PYEOF
  echo "evidence: rails/evidence/ADVERSARIAL/$RUNID/"
  exit 0
else
  echo
  echo "GOVERNOR NOT PROVEN: $FAILED of $TOTAL cases failed. Registry NOT stamped."
  echo "verify.sh will refuse to certify dispatches until this passes."
  echo "evidence: rails/evidence/ADVERSARIAL/$RUNID/"
  exit 1
fi
