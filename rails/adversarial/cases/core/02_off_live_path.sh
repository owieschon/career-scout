#!/usr/bin/env bash
# Work that is green but the tested code is not on the path that ships.
source "$(dirname "$0")/../../lib.sh"
_assert "clean tree verifies PASS"        0 "$(run_verify)"
sed_i 's/return add(2, 3)/return 5/' "$SANDBOX/src/main.py"   # call site genuinely dead
_assert "dead call site -> verify FAILs"  1 "$(run_verify)"
_assert "live_path is the check that fired" false "$(check_state live_path)"
( cd "$SANDBOX" && git checkout -q src/main.py )
_assert "restored tree verifies PASS again" 0 "$(run_verify)"
finish
