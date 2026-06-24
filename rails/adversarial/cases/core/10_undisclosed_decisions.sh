#!/usr/bin/env bash
# Judgment quality is human-graded (spec section 8); judgment DISCLOSURE is
# machine-required. Missing/structurally-empty DECISIONS.md fails the gate;
# a human-approved exemption in the manifest passes it.
source "$(dirname "$0")/../../lib.sh"
_assert "decisions present -> verify PASS" 0 "$(run_verify)"
mv "$SANDBOX/rails/dispatches/active/D-test/DECISIONS.md" "$SANDBOX/.dec.bak"
_assert "decisions missing -> verify FAILs" 1 "$(run_verify)"
_assert "decisions is the check that fired" false "$(check_state decisions)"
printf '# Decisions\nstuff happened\n' > "$SANDBOX/rails/dispatches/active/D-test/DECISIONS.md"
_assert "structurally empty decisions -> verify FAILs" 1 "$(run_verify)"
rm "$SANDBOX/rails/dispatches/active/D-test/DECISIONS.md"
python3 - "$SANDBOX" <<'PY'
import json, sys
p = sys.argv[1] + "/rails/dispatches/active/D-test/manifest.json"
m = json.load(open(p)); m["decisions_required"] = False
json.dump(m, open(p, "w"), indent=2)
PY
_assert "human-approved exemption -> verify PASS" 0 "$(run_verify)"
finish
