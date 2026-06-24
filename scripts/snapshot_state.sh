#!/usr/bin/env bash
# State-repo nightly snapshot (Layer 4 / C3 — local rollback for write-bug-class incidents).
# Local-only. Never adds a remote. Never pushes.
#
# Invocation:
#   scripts/snapshot_state.sh                 -> daily snapshot
#   scripts/snapshot_state.sh "tag message"   -> snapshot with extra message
#
# Run from cron or run_daily.sh. Idempotent — no-op if nothing changed.
set -euo pipefail

STATE_DIR="$(cd "$(dirname "$0")/.." && pwd)/feedback"
cd "$STATE_DIR"

if [ ! -d ".git" ]; then
    echo "[snapshot_state: $STATE_DIR is not a git repo — run 'git init' first]" >&2
    exit 1
fi

# Hard refusal: never let this repo grow a remote.
if git remote | grep -q .; then
    echo "[snapshot_state: ABORT — state-repo has a remote (must stay local-only)]" >&2
    git remote -v >&2
    exit 2
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
EXTRA="${1:-}"
MSG="state-snapshot $STAMP"
if [ -n "$EXTRA" ]; then
    MSG="$MSG — $EXTRA"
fi

git add -A
if git diff --cached --quiet; then
    echo "[snapshot_state: no changes since last snapshot]"
    exit 0
fi

git \
    -c user.email="alice-state@local" \
    -c user.name="Alice (state)" \
    commit -m "$MSG"
echo "[snapshot_state: $MSG]"
