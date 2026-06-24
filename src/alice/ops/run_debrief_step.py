"""Wrapper for daily-cron debrief step — calls integrate_debrief_answers and
records activity. Kept as a separate file so run_daily.sh stays a thin shell.
"""
import sys
from pathlib import Path

from alice.ops import debrief
from alice.persistence import activity_log

result = debrief.integrate_debrief_answers()
print(result)

integrated = result.get("integrated", 0)
if integrated > 0:
    co = result.get("company", "")
    role = result.get("role", "")
    summary = f"{integrated} debrief answer{'' if integrated == 1 else 's'} integrated for {co} {role}".strip()
    activity_log.record(step="debrief", summary=summary,
                        count=integrated, cost=result.get("cost", 0.0))
else:
    info = result.get("info") or "no debrief answers awaiting integration"
    activity_log.record(step="debrief", summary=info, count=0, status="noop")
