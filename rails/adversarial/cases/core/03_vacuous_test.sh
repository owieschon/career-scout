#!/usr/bin/env bash
# A test that passes but does not go red when the behavior breaks.
source "$(dirname "$0")/../../lib.sh"
_assert "clean tree verifies PASS" 0 "$(run_verify)"
cat > "$SANDBOX/tests/test_vacuous.py" <<'PY'
import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.mod import add
class T(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(add(0, 0), 0)   # subtraction also satisfies this
PY
python3 - "$SANDBOX" <<'PY'
import json, sys
p = sys.argv[1] + "/rails/dispatches/active/D-test/manifest.json"
m = json.load(open(p))
m["break_plan"][0]["expect_fail_cmd"] = "python3 -m unittest tests.test_vacuous 2>&1"
json.dump(m, open(p, "w"), indent=2)
PY
_assert "vacuous proof -> verify FAILs" 1 "$(run_verify)"
_assert "demonstrated_red is the check that fired" false "$(check_state demonstrated_red)"
finish
