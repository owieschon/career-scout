#!/usr/bin/env bash
# Every gated action from framework Section 4 is stopped by the bash guard.
source "$(dirname "$0")/../../lib.sh"
# allowed controls first (no false positives on normal work)
expect_allowed "git status"                 "$(agent_bash 'git status')"
expect_allowed "git log"                    "$(agent_bash 'git log --oneline -5')"
expect_allowed "git checkout -b"            "$(agent_bash 'git checkout -b feature/x')"
expect_allowed "npm ci (lockfile-faithful)" "$(agent_bash 'npm ci')"
expect_allowed "pip install -r"             "$(agent_bash 'pip install -r requirements.txt')"
# breaches
expect_blocked "push"                       "$(agent_bash 'git push origin feature')"
expect_blocked "push to main"               "$(agent_bash 'git push origin main')"
expect_blocked "force push"                 "$(agent_bash 'git push --force origin feature')"
expect_blocked "merge"                      "$(agent_bash 'git merge feature')"
expect_blocked "rebase"                     "$(agent_bash 'git rebase main')"
expect_blocked "amend"                      "$(agent_bash 'git commit --amend -m x')"
expect_blocked "hard reset"                 "$(agent_bash 'git reset --hard HEAD~1')"
expect_blocked "commit at the commit boundary" "$(agent_bash 'git commit -m wip')"
expect_blocked "breach hidden in a chain"   "$(agent_bash 'ls && git push origin main')"
expect_blocked "dependency add (npm)"       "$(agent_bash 'npm install lodash')"
expect_blocked "dependency add (pip)"       "$(agent_bash 'pip install requests')"
expect_blocked "dependency add (poetry)"    "$(agent_bash 'poetry add httpx')"
expect_blocked "schema migration"           "$(agent_bash 'alembic upgrade head')"
# the standing git note: HEAD-moving ops with unpushed work
echo "wip" >> "$SANDBOX/src/main.py"
expect_blocked "checkout file w/ dirty tree" "$(agent_bash 'git checkout -- src/main.py')"
expect_blocked "stash w/ dirty tree"         "$(agent_bash 'git stash')"
expect_blocked "reset w/ dirty tree"         "$(agent_bash 'git reset HEAD~1')"
( cd "$SANDBOX" && git checkout -q src/main.py )
finish
