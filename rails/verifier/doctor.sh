#!/usr/bin/env bash
#
# doctor.sh -- preflight health check for an agent-rails install.
#
# Read-only. Prints PASS/FAIL/WARN per item and exits non-zero if any item
# FAILs, so /go can stop a dispatch before building on a broken install.
# All logic lives in one standalone python block (not inside $(), so it is
# safe on bash 3.2 even with apostrophes in the body).
#
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import json, os, platform, re, shutil, subprocess, sys

root = sys.argv[1]
fails = 0


def ok(m):   print(f"  [PASS] {m}")
def warn(m): print(f"  [WARN] {m}")
def bad(m):
    global fails
    print(f"  [FAIL] {m}")
    fails += 1


def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


print(f"agent-rails doctor: {root}\n")

# ---- 1. governor proven + fingerprint matches the stamp -------------------
reg = load(os.path.join(root, "rails", "adversarial", "registry.json"))
if not reg:
    bad("no registry.json -- governor never proven (run rails/adversarial/run_eval.sh)")
else:
    try:
        cur = subprocess.run(
            ["python3", os.path.join(root, "rails", "verifier", "fingerprint.py"), root],
            capture_output=True, text=True).stdout.strip()
        if reg.get("last_proven_fingerprint") == cur:
            ok(f"governor fingerprint matches stamp ({cur[:16]}...)")
        else:
            bad("governor fingerprint != stamp -- trust layer changed since the "
                "last eval; re-run rails/adversarial/run_eval.sh")
    except Exception as e:
        bad(f"could not compute fingerprint: {e}")

# ---- 2. environment vs recorded (python, bash, claude_code) ---------------
env = (reg or {}).get("environment", {})
pyv = ".".join(platform.python_version_tuple()[:2])
bv = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
                    capture_output=True, text=True).stdout.strip()
for name, cur in (("python", pyv), ("bash", bv)):
    rec = env.get(name)
    if rec in (None, "unknown", ""):
        warn(f"{name} not recorded in registry")
    elif rec == cur:
        ok(f"{name} {cur} matches registry")
    else:
        bad(f"{name} mismatch: registry {rec} vs running {cur} -- re-run the eval")
cc_rec = env.get("claude_code")
cc_now = "unknown"
if shutil.which("claude"):
    try:
        cc_now = subprocess.run(["claude", "--version"], capture_output=True,
                                text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        cc_now = "unknown"
if cc_rec in (None, "unknown", "") or cc_now in ("unknown", ""):
    warn(f"claude_code version not comparable (registry={cc_rec}, now={cc_now})")
elif cc_rec == cc_now:
    ok(f"claude_code {cc_now} matches registry")
else:
    bad(f"claude_code mismatch: registry {cc_rec} vs running {cc_now} -- re-run the eval")

# ---- 3. config parses; collect runs and is countable ----------------------
cfgp = os.path.join(root, "rails", "config.json")
cfg = load(cfgp)
if cfg is None:
    bad("rails/config.json missing or unparseable")
    cfg = {}
else:
    ok("rails/config.json parses")
    if not str(cfg.get("scope", "")).strip():
        warn("config.json has no 'scope' line (wrong-repo tripwire is disabled)")

count_regex = cfg.get("count_regex", "")
try:
    re.compile(count_regex)
    ok(f"count_regex compiles: {count_regex!r}")
except Exception as e:
    bad(f"count_regex does not compile: {e}")

collect_cmd = cfg.get("collect_cmd", "")
collected = None
if not collect_cmd:
    warn("no collect_cmd configured -- load-bearing-by-name uses the suite log instead")
else:
    try:
        r = subprocess.run(["bash", "-c", collect_cmd], capture_output=True,
                            text=True, cwd=root, timeout=180)
        if r.returncode != 0:
            bad(f"collect_cmd exited {r.returncode} (a collect that errors cannot gate)")
        else:
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            collected = sum(1 for ln in lines if "::" in ln) or len(lines)
            ok(f"collect_cmd runs; ~{collected} collectible item(s)")
    except Exception as e:
        bad(f"collect_cmd failed to run: {e}")

# ---- 4. baseline exists and is not above the current suite size -----------
base = load(os.path.join(root, "rails", "verifier", "baseline.json"))
if base is None:
    bad("baseline.json missing -- seed it from a known-good run "
        "(verify.sh BOOTSTRAP --update-baseline)")
else:
    bc = base.get("test_count")
    if not isinstance(bc, int):
        bad(f"baseline.json has no integer test_count ({bc!r})")
    elif collected is not None and collected < bc:
        bad(f"suite appears smaller ({collected}) than baseline ({bc}) -- tests "
            "may have been dropped, or the baseline is stale")
    else:
        ok(f"baseline test_count={bc}"
           + (f" (<= ~{collected} collected)" if collected is not None else ""))

# ---- 5. hooks registered in settings --------------------------------------
sp = os.path.join(root, ".claude", "settings.json")
try:
    settings_txt = open(sp).read()
except Exception:
    settings_txt = ""
if not settings_txt:
    bad(".claude/settings.json missing -- hooks are not registered (the guards "
        "and stop gate are inert). If you merged into an existing settings file, "
        "confirm the rails hooks block is present.")
else:
    missing = [h for h in ("guard_bash.py", "guard_files.py", "gate_stop.py")
               if h not in settings_txt]
    if missing:
        bad("settings.json does not register: " + ", ".join(missing))
    else:
        ok("all three hooks registered in settings.json")

print()
if fails:
    print(f"DOCTOR: {fails} FAIL(s). Fix these before dispatching; an install "
          "this far out of true cannot certify work.")
    sys.exit(1)
print("DOCTOR: all checks passed.")
sys.exit(0)
PY
