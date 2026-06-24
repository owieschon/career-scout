#!/usr/bin/env bash
# Spec section 4, primary trigger: a change to the governor does not take
# force until the eval re-proves it. Mechanized as the fingerprint gate.
source "$(dirname "$0")/../../lib.sh"
_assert "proven governor -> verify PASS" 0 "$(run_verify)"
echo "# human edited a check" >> "$SANDBOX/rails/verifier/verify.sh"
_assert "changed governor -> verify FAILs until re-proven" 1 "$(run_verify)"
_assert "governor_proven is the check that fired" false "$(check_state governor_proven)"
# re-stamp (standing in for a full eval re-run inside the sandbox)
python3 - "$SANDBOX" <<'PY'
import json, subprocess, sys
sb = sys.argv[1]
fp = subprocess.run(["python3", sb + "/rails/verifier/fingerprint.py", sb],
                    capture_output=True, text=True).stdout.strip()
p = sb + "/rails/adversarial/registry.json"
r = json.load(open(p)); r["last_proven_fingerprint"] = fp
json.dump(r, open(p, "w"), indent=2)
PY
_assert "re-proven governor -> verify PASS again" 0 "$(run_verify)"

# Env-fingerprint drift (added 2026-06-09, retrofit #1): the trust layer IS
# bash, so the environment fingerprint records the bash version and
# governor_proven refuses to certify when the recorded version differs from
# the running one -- a proof produced under a different shell does not gate.
python3 - "$SANDBOX" <<'PY'
import json, sys
p = sys.argv[1] + "/rails/adversarial/registry.json"
r = json.load(open(p)); r.setdefault("environment", {})["bash"] = "2.0"
json.dump(r, open(p, "w"), indent=2)
PY
_assert "recorded bash != running -> verify FAILs"        1 "$(run_verify)"
_assert "governor_proven is the check that fired"     false "$(check_state governor_proven)"
python3 - "$SANDBOX" <<'PY'
import json, subprocess, sys
bv = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                    capture_output=True, text=True).stdout.strip()
p = sys.argv[1] + "/rails/adversarial/registry.json"
r = json.load(open(p)); r["environment"]["bash"] = bv
json.dump(r, open(p, "w"), indent=2)
PY
_assert "recorded bash == running -> verify PASS (no false-positive)" 0 "$(run_verify)"
finish
