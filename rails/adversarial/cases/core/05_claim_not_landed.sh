#!/usr/bin/env bash
# "Pushed" is a claim until the remote shows it.
# Departure note (spec 0, open choice): proven against a local bare remote;
# ground truth is identical to origin semantics, no network dependency.
source "$(dirname "$0")/../../lib.sh"
cd "$SANDBOX"
git init -q --bare .remote.git
git remote add origin "$SANDBOX/.remote.git"
git push -qu origin main 2>/dev/null
_assert "pushed commit -> LANDED (exit 0)" 0 "$( bash rails/verifier/remote_ref.sh main >/dev/null 2>&1; echo $? )"
echo "# local only" >> src/mod.py && git add -A && git commit -qm "local only"
_assert "unpushed commit -> NOT LANDED (exit 1)" 1 "$( bash rails/verifier/remote_ref.sh main >/dev/null 2>&1; echo $? )"
_assert "explicit sha of unpushed commit -> NOT LANDED" 1 "$( bash rails/verifier/remote_ref.sh main "$(git rev-parse HEAD)" >/dev/null 2>&1; echo $? )"
finish
