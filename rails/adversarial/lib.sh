#!/usr/bin/env bash
#
# lib.sh: assertion helpers for known-bad cases. Every case must prove BOTH
# directions: the check FIRES on the violation and stays QUIET on clean
# work (spec section 9: red on a real violation, no false-positive).
#
# Cases run as the HARNESS (direct filesystem/git access to the sandbox is
# allowed; the harness plays the human/world). Simulated AGENT actions go
# through agent_bash / agent_edit, which invoke the real hook scripts with
# the documented stdin contract.
set -u
: "${SANDBOX:?lib.sh requires SANDBOX}"

_FAILS=0
_PASSES=0

sed_i() {  # sed_i <sed-script> <file>  -- portable in-place edit (BSD + GNU)
  # macOS/BSD 'sed -i' needs a suffix arg and rejects GNU 'sed -i SCRIPT';
  # edit via temp + mv instead, so cases run on stock macOS. No -i at all.
  local script="$1" file="$2" tmp
  tmp="$(mktemp)"
  sed "$script" "$file" > "$tmp" && mv "$tmp" "$file"
}

_assert() {  # _assert <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then
    _PASSES=$((_PASSES + 1)); printf '    ok   %s\n' "$1"
  else
    _FAILS=$((_FAILS + 1));  printf '    FAIL %s (expected %s, got %s)\n' "$1" "$2" "$3"
  fi
}

agent_bash() {  # agent_bash <shell-command-string>  -> echoes hook exit code
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1" \
    | CLAUDE_PROJECT_DIR="$SANDBOX" python3 "$SANDBOX/.claude/hooks/guard_bash.py" >/dev/null 2>&1
  echo $?
}

agent_edit() {  # agent_edit <file-path>  -> echoes hook exit code
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Edit","tool_input":{"file_path":sys.argv[1]}}))' "$1" \
    | CLAUDE_PROJECT_DIR="$SANDBOX" python3 "$SANDBOX/.claude/hooks/guard_files.py" >/dev/null 2>&1
  echo $?
}

agent_stop() {  # -> echoes stop-gate exit code
  printf '{"hook_event_name":"Stop","stop_hook_active":false}' \
    | CLAUDE_PROJECT_DIR="$SANDBOX" python3 "$SANDBOX/.claude/hooks/gate_stop.py" >/dev/null 2>&1
  echo $?
}

run_verify() {  # -> echoes verify.sh exit code
  ( cd "$SANDBOX" && bash rails/verifier/verify.sh D-test ) >/dev/null 2>&1
  echo $?
}

check_state() {  # check_state <check-name>  -> echoes true/false/missing
  python3 -c '
import json,sys
try:
    v=json.load(open(sys.argv[1]))
    print(str(v["checks"][sys.argv[2]]["pass"]).lower())
except Exception:
    print("missing")
' "$SANDBOX/rails/evidence/D-test/verdict.json" "$1"
}

expect_blocked() { _assert "$1" 2 "$2"; }   # hooks block with exit 2
expect_allowed() { _assert "$1" 0 "$2"; }

finish() {
  printf '  case summary: %d ok, %d failed\n' "$_PASSES" "$_FAILS"
  [ "$_FAILS" -eq 0 ] && exit 0 || exit 1
}
