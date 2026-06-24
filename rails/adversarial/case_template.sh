#!/usr/bin/env bash
# <one line: the violation class this proves the governor catches>
# Accreted: <date> from <the real failure that escaped>.
source "$(dirname "$0")/../../lib.sh"

# 1. clean control: the check must NOT false-positive on good work
#_assert "clean tree verifies PASS" 0 "$(run_verify)"

# 2. the known-bad input: inject the violation, assert the check FIRES
#_assert "<violation> -> verify FAILs" 1 "$(run_verify)"
#_assert "<check> is the check that fired" false "$(check_state <check>)"

echo "    FAIL template not yet written (this is deliberate: an unwritten"
echo "         catch-test counts as a gap, never as a pass)"
exit 1
