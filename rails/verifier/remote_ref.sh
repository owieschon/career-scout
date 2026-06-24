#!/usr/bin/env bash
#
# remote_ref.sh <branch> [<sha>]
#
# Claim != landed (framework Section 3 / discipline 7). "Pushed" is a claim
# until the remote shows it. Run this AFTER you (the human) push, or whenever
# an agent transcript claims something was committed/pushed.
#
# Verifies against origin's actual refs, never against a success message.
set -u
BRANCH="${1:?usage: remote_ref.sh <branch> [<sha>]}"
SHA="${2:-}"
REMOTE="$(python3 -c "
import json
try: print(json.load(open('$(dirname "${BASH_SOURCE[0]}")/../config.json')).get('remote','origin'))
except Exception: print('origin')
")"

git fetch "$REMOTE" "$BRANCH" --quiet || { echo "FAIL: cannot fetch $REMOTE/$BRANCH"; exit 1; }

if [ -z "$SHA" ]; then
  SHA="$(git rev-parse HEAD)"
fi

if git merge-base --is-ancestor "$SHA" "$REMOTE/$BRANCH" 2>/dev/null; then
  echo "LANDED: $SHA is on $REMOTE/$BRANCH"
  git log --oneline -1 "$SHA"
  exit 0
else
  echo "NOT LANDED: $SHA is NOT on $REMOTE/$BRANCH. The push claim is false or incomplete."
  exit 1
fi
