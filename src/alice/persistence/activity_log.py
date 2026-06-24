"""Daily activity log.

Each step in run_daily.sh calls record() with a summary of what it did. The
daily digest reads today's records and renders an Activity Today section so the
digest is a full picture of Alice's work, not just role sourcing.

File: daily/activity-YYYY-MM-DD.jsonl (one JSON record per line, append-only).
"""
import json
from datetime import datetime
from pathlib import Path
from alice import repo_paths

ROOT = Path(repo_paths.DAILY)


def _path(date=None):
    d = date or datetime.now().strftime("%Y-%m-%d")
    return ROOT / f"activity-{d}.jsonl"


def record(step, summary="", count=0, status="ok", cost=0.0, details=None):
    """Append one activity record for today.

    step:    short identifier (e.g. 'imap_reply', 'draft_outreach').
    summary: one-line human-readable description ('0 replies, 0 updates').
    count:   primary count for this step (replies seen, packages drafted, etc).
    status:  'ok' | 'noop' | 'error'. 'noop' = nothing to do this run.
    cost:    LLM spend in USD attributable to this step.
    details: optional dict for richer rendering.
    """
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts":      datetime.now().isoformat(timespec="seconds"),
        "step":    step,
        "status":  status,
        "count":   int(count or 0),
        "summary": summary,
        "cost":    round(float(cost or 0.0), 6),
    }
    if details:
        rec["details"] = details
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def read_today():
    p = _path()
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception as _e:
                try:
                    import obs; obs.capture(_e, where="activity_log:read_today:json", payload={"line": line[:200]})
                except Exception:
                    pass
    return out


def render_activity_section():
    """Render the Activity Today section for the digest.

    Groups steps in orchestration order, shows what produced work vs what was
    a noop, totals LLM spend at the bottom.
    """
    records = read_today()
    if not records:
        return ""

 # Orchestration order — defines render order in the digest.
    ORDER = [
        "imap_reply",
        "focus_auto_drop",
        "triage_observations",
        "prep_materials",
        "interview_prep",
        "debrief",
        "draft_outreach",
        "negotiation_prep",
        "morning_reminder",
        "daily_delta",
    ]
    LABEL = {
        "imap_reply":          "Email replies processed",
        "focus_auto_drop":     "Focus auto-drops",
        "triage_observations": "Observations triaged",
        "prep_materials":      "Application packages drafted",
        "interview_prep":      "Interview prep generated",
        "debrief":             "Debrief answers integrated",
        "draft_outreach":      "Outreach drafts produced",
        "negotiation_prep":    "Negotiation prep produced",
        "morning_reminder":    "Morning reminders sent",
        "daily_delta":         "Role sourcing",
    }

 # Take the latest record per step for today (in case a step ran twice).
    latest = {}
    for r in records:
        latest[r["step"]] = r

    lines = ["ACTIVITY TODAY"]
    lines.append("-" * 14)
    total_cost = 0.0
    productive = 0
    for step in ORDER:
        if step not in latest:
            continue
        r = latest[step]
        label = LABEL.get(step, step)
        if r["status"] == "error":
            lines.append(f"  ! {label}: ERROR — {r['summary']}")
        elif r["status"] == "noop" or r["count"] == 0:
            lines.append(f"  · {label}: {r['summary'] or 'nothing to do'}")
        else:
            lines.append(f"  ✓ {label}: {r['summary']}")
            productive += 1
        total_cost += r.get("cost", 0.0)

 # Canonical 24-hour spend from time-cost-log.jsonl (captures every LLM call,
 # not just per-step activity sums — more reliable when scripts are run ad-hoc).
    canonical_today = None
    canonical_week = None
    try:
        from alice.llm import llm
        canonical_today = llm.cost_today()
        canonical_week = llm.cost_last_n_days(7)
    except Exception as _e:
        try:
            import obs; obs.capture(_e, where="activity_log:render:llm_cost")
        except Exception:
            pass

    lines.append("")
    if canonical_today is not None:
        lines.append(f"  LLM spend (last 24h):  ${canonical_today:.4f}")
        lines.append(f"  LLM spend (last 7 days): ${canonical_week:.4f}")
    elif total_cost > 0:
        lines.append(f"  LLM spend today: ${total_cost:.4f}")

    if productive == 0:
        lines.append("")
        lines.append("  (Quiet day — Alice had no work to produce. Most likely cause:")
        lines.append("   no new replies from you, no submitted apps awaiting outreach,")
        lines.append("   no interviews scheduled. This is normal early in a search week.)")

    return "\n".join(lines)


def main():
    """CLI: print today's activity log for inspection."""
    import sys
    out = render_activity_section()
    if out:
        print(out)
    else:
        print("[no activity records today]")
    sys.exit(0)


if __name__ == "__main__":
    main()
