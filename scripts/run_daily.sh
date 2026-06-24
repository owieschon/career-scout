#!/bin/bash
# Local job-search daily runner. Orchestrates Alice's full daily cycle.
# Saturdays: refresh YC + VC boards first.
# Daily: process replies/directives, run Alice's behaviors, send digest.

cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
PY="${PYTHON:-python3}"
LOG="daily/cron.log"

echo "" >> "$LOG"
echo "=== run $(date) ===" >> "$LOG"

# Saturday: refresh discovery targets
if [ "$(date +%u)" = "6" ]; then
  echo "[$(date +%T)] discover_yc + discover_vc (saturday)" >> "$LOG"
  "$PY" -m alice.pipeline.discover_yc --max 150 >> "$LOG" 2>&1
  "$PY" -m alice.pipeline.discover_vc >> "$LOG" 2>&1
fi

# 0a. Snapshot state-repo (local-only rollback point for the day's run)
echo "[$(date +%T)] snapshot_state (pre-run rollback point)" >> "$LOG"
scripts/snapshot_state.sh "pre-run" >> "$LOG" 2>&1

# 0. Execute any confirmed pending actions (or process correction replies)
echo "[$(date +%T)] confirm_and_execute (apply confirmed pending / process corrections)" >> "$LOG"
"$PY" -m alice.persistence.confirm_and_execute >> "$LOG" 2>&1

# 1. Process Operator's replies first (status updates, directives, observations)
echo "[$(date +%T)] imap_reply (status updates + directives + observation capture)" >> "$LOG"
"$PY" -m alice.notify.imap_reply >> "$LOG" 2>&1

# 2. Auto-drop focus roles that moved past 'submitted'
echo "[$(date +%T)] focus.auto_drop_submitted" >> "$LOG"
"$PY" -m alice.ops.run_focus_drop_step >> "$LOG" 2>&1

# 3. Triage observations (Alice's conversational response to unstructured feedback)
echo "[$(date +%T)] triage_observations" >> "$LOG"
"$PY" -m alice.persistence.triage_observations >> "$LOG" 2>&1

# 4. Process prep queue (generate application materials for any 'prep:' directives)
echo "[$(date +%T)] prep_materials" >> "$LOG"
"$PY" -m alice.pipeline.prep_materials >> "$LOG" 2>&1

# 5. Generate interview prep for any newly-scheduled screens
echo "[$(date +%T)] interview_prep" >> "$LOG"
"$PY" -m alice.pipeline.interview_prep >> "$LOG" 2>&1

# 6. Integrate any new debrief answers
echo "[$(date +%T)] debrief.integrate_debrief_answers" >> "$LOG"
"$PY" -m alice.ops.run_debrief_step >> "$LOG" 2>&1

# 7. Draft outreach for any newly-submitted apps
echo "[$(date +%T)] draft_outreach" >> "$LOG"
"$PY" -m alice.pipeline.draft_outreach >> "$LOG" 2>&1

# 8. Negotiation prep for any newly-offered apps
echo "[$(date +%T)] negotiation_prep" >> "$LOG"
"$PY" -m alice.pipeline.negotiation_prep >> "$LOG" 2>&1

# 9. Morning reminder for any interview today (no-op if none)
echo "[$(date +%T)] morning_reminder" >> "$LOG"
"$PY" -m alice.notify.morning_reminder >> "$LOG" 2>&1

# 10. Final: source new roles + send the daily digest
echo "[$(date +%T)] daily_delta (sources new roles + sends digest)" >> "$LOG"
"$PY" -m alice.pipeline.daily_delta --ledger >> "$LOG" 2>&1

echo "[$(date +%T)] === run complete ===" >> "$LOG"

# Readiness check (C5(c)): coverage gate then conjunction members; notify
# only on state change. Currently will correctly report NOT READY.
echo "[$(date +%T)] readiness_check" >> "$LOG"
"$PY" -m alice.observability.readiness_check >> "$LOG" 2>&1

# Final: snapshot state-repo with the day's drift recorded
echo "[$(date +%T)] snapshot_state (post-run)" >> "$LOG"
scripts/snapshot_state.sh "post-run" >> "$LOG" 2>&1
