#!/usr/bin/env bash
# A done-claim backed by a partial/failing/uncountable suite run.
# Count-is-the-tell: a dropped count or an unparseable run is a false green.
source "$(dirname "$0")/../../lib.sh"
_assert "clean tree verifies PASS" 0 "$(run_verify)"

sed_i '/def test_add_negative/{N;d;}' "$SANDBOX/tests/test_mod.py"   # silent drop
_assert "count below baseline -> verify FAILs" 1 "$(run_verify)"
_assert "full_suite is the check that fired"   false "$(check_state full_suite)"
( cd "$SANDBOX" && git checkout -q tests/test_mod.py )

sed_i 's/return a + b/return a - b/' "$SANDBOX/src/mod.py"          # failing suite
_assert "failing suite -> verify FAILs"        1 "$(run_verify)"
_assert "full_suite fired on red suite"        false "$(check_state full_suite)"
( cd "$SANDBOX" && git checkout -q src/mod.py )

python3 - "$SANDBOX" <<'PY'                                          # uncountable run
import json, sys
p = sys.argv[1] + "/rails/config.json"
c = json.load(open(p)); c["count_regex"] = "ZZZ ([0-9]+) ZZZ"
json.dump(c, open(p, "w"), indent=2)
PY
_assert "uncountable run -> verify FAILs (a run you cannot count is not a green)" 1 "$(run_verify)"
_assert "full_suite fired on uncountable"      false "$(check_state full_suite)"
( cd "$SANDBOX" && git checkout -q rails/config.json )
_assert "restored -> PASS again" 0 "$(run_verify)"
finish
