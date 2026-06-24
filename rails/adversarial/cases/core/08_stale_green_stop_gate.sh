#!/usr/bin/env bash
# Inflation, mechanized form 1: presenting an old PASS as current proof.
# The stop gate honors a PASS only against the exact tree it was stamped on.
source "$(dirname "$0")/../../lib.sh"
_assert "no verdict yet -> stop BLOCKED"      2 "$(agent_stop)"
_assert "verify PASSes clean"                 0 "$(run_verify)"
_assert "fresh PASS -> stop allowed"          0 "$(agent_stop)"
echo "# drift" >> "$SANDBOX/src/main.py"
_assert "tree changed after PASS -> stop BLOCKED (stale green is not green)" 2 "$(agent_stop)"
( cd "$SANDBOX" && git checkout -q src/main.py )
_assert "tree restored to stamped state -> stop allowed" 0 "$(agent_stop)"
sed_i 's/return a + b/return a - b/' "$SANDBOX/src/mod.py"
run_verify >/dev/null
_assert "FAIL verdict -> stop BLOCKED"        2 "$(agent_stop)"
touch "$SANDBOX/rails/handoff/D-test.BLOCKED.md"
_assert "formal BLOCKED declaration -> stop allowed" 0 "$(agent_stop)"
rm "$SANDBOX/rails/handoff/D-test.BLOCKED.md"
( cd "$SANDBOX" && git checkout -q src/mod.py )
finish
