#!/usr/bin/env bash
# A named check whose condition never occurred is not a pass: a
# load-bearing test that was never collected/ran must fail the gate.
source "$(dirname "$0")/../../lib.sh"
_assert "clean tree verifies PASS" 0 "$(run_verify)"
python3 - "$SANDBOX" <<'PY'
import json, sys
p = sys.argv[1] + "/rails/dispatches/active/D-test/manifest.json"
m = json.load(open(p))
m["load_bearing_tests"].append("test_totally_fictional_behavior")
json.dump(m, open(p, "w"), indent=2)
PY
_assert "never-ran test name -> verify FAILs" 1 "$(run_verify)"
_assert "load_bearing is the check that fired" false "$(check_state load_bearing)"
finish
