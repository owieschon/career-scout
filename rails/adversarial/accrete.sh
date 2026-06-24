#!/usr/bin/env bash
#
# accrete.sh <slug> [core|project]   (default: project)
#
# Spec section 5: a failure slipping through once is a gap; twice is
# negligence. This is the one-step path from "a failure escaped" to "a
# permanent test exists for it." Human-run by design (the eval is part of
# the governor; the agent proposes new cases in its handoff, the human
# accretes them).
set -euo pipefail
SLUG="${1:?usage: accrete.sh <slug> [core|project]}"
SCOPE="${2:-project}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$HERE/cases/$SCOPE"
mkdir -p "$DIR"
N="$(printf '%02d' $(( $(ls "$DIR"/*.sh 2>/dev/null | wc -l) + 1 )))"
DEST="$DIR/${N}_${SLUG}.sh"
cp "$HERE/case_template.sh" "$DEST"
chmod +x "$DEST"
echo "created $DEST"
echo "It exits 1 until you write its assertions, so the eval stays red"
echo "(and verify.sh stays closed) until the new catch is real. Write the"
echo "known-bad input, assert the check fires, assert clean work passes,"
echo "then run: bash rails/adversarial/run_eval.sh"
