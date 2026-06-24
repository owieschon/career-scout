#!/usr/bin/env bash
#
# fixture.sh [sandbox-dir]
#
# Builds a disposable sandbox repo and installs THIS repo's actual
# trust-layer files into it (not a vendored copy), so every case exercises
# the governor that is really in force. Prints the sandbox path.
#
# Departure note (spec 0, open choice): violations run in a sandbox, never
# in the live repo. Grounds: known-bad inputs must not corrupt real work;
# the mechanism under test is byte-identical because the files are copied
# from the host at run time.
set -euo pipefail

HOST="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SB="${1:-$(mktemp -d /tmp/rails-eval-XXXXXX)}"

mkdir -p "$SB"/{src,tests} "$SB/.claude/hooks" "$SB/rails/verifier" \
         "$SB/rails/dispatches/active/D-test/breaks" "$SB/rails/evidence" \
         "$SB/rails/handoff" "$SB/rails/adversarial" "$SB/rails/incidents"

# --- the governor, copied from the host ---------------------------------
cp "$HOST"/.claude/hooks/*.py "$SB/.claude/hooks/"
cp "$HOST"/.claude/settings.json "$SB/.claude/settings.json"
cp "$HOST"/rails/verifier/verify.sh "$HOST"/rails/verifier/remote_ref.sh \
   "$HOST"/rails/verifier/treehash.py "$HOST"/rails/verifier/fingerprint.py \
   "$HOST"/rails/verifier/demonstrated_red.py "$HOST"/rails/verifier/incident.py \
   "$HOST"/rails/verifier/stats.py \
   "$SB/rails/verifier/"
chmod +x "$SB"/rails/verifier/*.sh "$SB"/rails/verifier/*.py "$SB"/.claude/hooks/*.py

# --- toy project ----------------------------------------------------------
cat > "$SB/src/mod.py" <<'EOF'
def add(a, b):
    return a + b
EOF
cat > "$SB/src/main.py" <<'EOF'
from src.mod import add

def entry():
    return add(2, 3)
EOF
cat > "$SB/tests/test_mod.py" <<'EOF'
import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.mod import add

class TestAdd(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)
    def test_add_negative(self):
        self.assertEqual(add(-2, -3), -5)
EOF
touch "$SB/src/__init__.py" "$SB/tests/__init__.py"

cat > "$SB/rails/config.json" <<'EOF'
{
  "main_branch": "main",
  "remote": "origin",
  "allow_local_commits": false,
  "test_cmd": "python3 -m unittest discover -s tests -v 2>&1",
  "count_regex": "Ran ([0-9]+) tests?",
  "collect_cmd": "",
  "migration_patterns": ["alembic upgrade", "prisma migrate", "supabase db push"]
}
EOF

# --- a complete, clean dispatch ------------------------------------------
cat > "$SB/rails/dispatches/active/D-test/manifest.json" <<'EOF'
{
  "id": "D-test",
  "decisions_required": true,
  "live_path_greps": [ { "pattern": "add\\(", "path": "src/main.py" } ],
  "load_bearing_tests": [ "test_add_positive" ],
  "break_plan": [
    {
      "desc": "flip add to subtract",
      "files": ["src/mod.py"],
      "apply": "sed 's/a + b/a - b/' src/mod.py > src/mod.py.rails && mv src/mod.py.rails src/mod.py",
      "expect_fail_cmd": "python3 -m unittest tests.test_mod 2>&1"
    }
  ]
}
EOF
cat > "$SB/rails/dispatches/active/D-test/DECISIONS.md" <<'EOF'
# Decisions: D-test
## D1: implementation of add
- Context: toy fixture for the adversarial eval.
- Options considered: A) builtin operator B) numpy.
- Trade-offs: B adds a dependency for nothing.
- Choice & grounds: A; smallest thing that meets the DoD.
- Reversibility: trivial, single pure function.
EOF

# --- git, load-bearing list, baseline (all BEFORE the governor stamp) ----
printf '__pycache__/\n*.pyc\n' > "$SB/.gitignore"
cd "$SB"
git init -q
git config user.email eval@rails.local
git config user.name rails-eval
git branch -M main 2>/dev/null || true
echo "tests/test_mod.py" >> rails/verifier/load_bearing.txt 2>/dev/null \
  || echo "tests/test_mod.py" > rails/verifier/load_bearing.txt
git add -A
git commit -qm "fixture"
bash rails/verifier/verify.sh BOOTSTRAP --update-baseline >/dev/null 2>&1
rm -rf rails/evidence/BOOTSTRAP
git add -A && git commit -qm "baseline" >/dev/null 2>&1 || true

# --- stamp the sandbox governor as proven (the eval IS the prover; the
#     harness bootstrapping its own instrument is the one legitimate stamp)
python3 - "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}" <<'PYEOF'
import json, platform, subprocess, sys
fp = subprocess.run(
    ["python3", "rails/verifier/fingerprint.py", "."],
    capture_output=True, text=True).stdout.strip()
json.dump({
    "last_proven_fingerprint": fp,
    "stamped_by": "fixture (sandbox bootstrap)",
    "environment": {
        "python": ".".join(platform.python_version_tuple()[:2]),
        "bash": sys.argv[1],
    },
}, open("rails/adversarial/registry.json", "w"), indent=2)
PYEOF

echo "$SB"
