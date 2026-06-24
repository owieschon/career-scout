#!/bin/bash
# Friday weekly scorecard runner. Triggered by separate launchd plist at 4pm Fri.
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
PY="${PYTHON:-python3}"
LOG="daily/cron.log"

echo "" >> "$LOG"
echo "=== weekly scorecard $(date) ===" >> "$LOG"
"$PY" -m alice.pipeline.scorecard >> "$LOG" 2>&1
echo "[$(date +%T)] === weekly run complete ===" >> "$LOG"
