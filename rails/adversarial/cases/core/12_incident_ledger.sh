#!/usr/bin/env bash
# Incident ledger: a BLOCKED dispatch or a PASS->FAIL flip on an unchanged tree
# becomes a durable, tamper-evident record. The governor will not re-stamp
# while any record is unlinked (the accretion rule), and the loop can neither
# edit nor delete a record -- only a human links one to the case that covers it.
source "$(dirname "$0")/../../lib.sh"

gate() { ( cd "$SANDBOX" && python3 rails/verifier/incident.py check . >/dev/null 2>&1; echo $? ); }

_assert "empty ledger -> incident gate OK"                 0 "$(gate)"

# the trust layer records an unlinked incident
python3 "$SANDBOX/rails/verifier/incident.py" record "$SANDBOX" D-test \
  pass_to_fail_unchanged_tree full_suite "claimed PASS" "observed FAIL" deadbeef >/dev/null
_assert "unlinked incident -> gate BLOCKS the stamp"       1 "$(gate)"

# writing a NEW record is agent-permitted ...
_assert "new record path -> write allowed"                 0 "$(agent_edit "rails/incidents/INC-new.json")"
# ... editing or deleting an EXISTING one is not
REC="$(ls "$SANDBOX/rails/incidents/"*.json | head -1)"
RELREC="rails/incidents/$(basename "$REC")"
_assert "edit an existing record -> blocked"               2 "$(agent_edit "$RELREC")"
expect_blocked "shell-delete a record"   "$(agent_bash "rm $RELREC")"
expect_blocked "shell-rewrite a record"  "$(agent_bash "echo tampered > $RELREC")"

# idempotent: the same event does not spam the ledger
N1="$(ls "$SANDBOX/rails/incidents/"*.json | wc -l | tr -d ' ')"
python3 "$SANDBOX/rails/verifier/incident.py" record "$SANDBOX" D-test \
  pass_to_fail_unchanged_tree full_suite "claimed PASS" "observed FAIL" deadbeef >/dev/null
N2="$(ls "$SANDBOX/rails/incidents/"*.json | wc -l | tr -d ' ')"
_assert "duplicate event -> not re-recorded"               "$N1" "$N2"

# a human links it (direct edit, no hook) -> gate clears, no false block
python3 - "$REC" <<'PY'
import json, sys
p = sys.argv[1]; r = json.load(open(p))
r["linked_case"] = "core/12_incident_ledger"
json.dump(r, open(p, "w"), indent=2)
PY
_assert "linked incident -> gate OK again (no false-positive)" 0 "$(gate)"
finish
