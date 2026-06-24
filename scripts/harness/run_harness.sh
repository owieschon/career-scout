#!/bin/bash
# Run the full harness: constraints (programmatic), integration (real-tool),
# regression (canonical), adversarial (edge cases), judge (quality review).
# Cost: ~$0.10 per full run.

cd "$(cd "$(dirname "$0")/../.." && pwd)" || exit 1
PY="${PYTHON:-python3}"

echo "=== HARNESS RUN $(date) ==="
echo

echo "--- 1. Integration checks (no LLM) ---"
"$PY" -m alice.harness.integration
echo

echo "--- 2. Regression suite (canonical inputs) ---"
"$PY" -m alice.harness.regression
echo

echo "--- 3. Adversarial test cases ---"
"$PY" -m alice.harness.adversarial
echo

echo "--- 4. LLM judge (quality review of recent outputs) ---"
"$PY" -m alice.harness.judge
echo

echo "--- 5. Programmatic constraints on recent outputs ---"
"$PY" -c "
import sys
from pathlib import Path

from alice.harness.constraints import check, format_violations

apps = Path('applications')
threads = Path('feedback/threads')
all_checks = []
for d in [apps, threads]:
    if not d.exists():
        continue
    for f in d.rglob('*.md'):
        violations = check(f.read_text(), severity_threshold='warn')
        if violations:
            print(f'  {f}: {len(violations)} violation(s)')
            print(format_violations(violations))
            all_checks.extend(violations)
print()
print(f'total violations across recent outputs: {len(all_checks)}')
"
echo

echo "=== HARNESS COMPLETE ==="
