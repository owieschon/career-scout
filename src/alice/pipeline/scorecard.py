"""Friday weekly scorecard — Alice publishes her honest self-review by the numbers.

Runs from a separate Friday 4pm cron. Pulls funnel data + quality metrics + behavior +
time/cost from the sheet, applications/, feedback/time-cost-log.jsonl, etc.

Outputs:
  - Sends scorecard as a fresh email (not the daily digest)
  - Saves to feedback/scorecards/YYYY-WW.md for history
"""
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger
from alice.persistence import focus
from alice.notify import notify_email

SCORECARDS = Path(str(repo_paths.FEEDBACK / "scorecards"))
COST_LOG = Path(str(repo_paths.FEEDBACK / "time-cost-log.jsonl"))
HYPOTHESES = Path(str(repo_paths.FEEDBACK / "hypotheses.md"))
APPS_DIR = Path(str(repo_paths.APPLICATIONS))
OUTREACH_LOG = Path(str(repo_paths.FEEDBACK / "outreach-responses.jsonl"))


def _funnel_metrics(days=7):
    """Compute funnel counts for the past N days from sheet status_changed_date."""
    ws = ledger._ws()
    rows = ws.get_all_records()
    cutoff = date.today() - timedelta(days=days)
    counts = defaultdict(int)
    surface_dates = []
    submit_dates_with_surface = []
    for r in rows:
        surf_raw = (r.get("surfaced_date") or "").strip()
        try:
            surf_d = date.fromisoformat(surf_raw.split("T")[0]) if surf_raw else None
        except Exception:
            surf_d = None
        if surf_d and surf_d >= cutoff:
            counts["surfaced"] += 1
            surface_dates.append(surf_d)
        status = (r.get("status") or "").strip().lower()
        changed_raw = (r.get("status_changed_date") or "").strip()
        try:
            changed_d = date.fromisoformat(changed_raw.split("T")[0]) if changed_raw else None
        except Exception:
            changed_d = None
        if changed_d and changed_d >= cutoff:
            counts[status] += 1
            if status == "submitted" and surf_d:
                submit_dates_with_surface.append((changed_d - surf_d).days)
    median_surface_to_submit = None
    if submit_dates_with_surface:
        sorted_d = sorted(submit_dates_with_surface)
        median_surface_to_submit = sorted_d[len(sorted_d) // 2]
    return dict(counts), median_surface_to_submit


def _outreach_metrics(days=7):
    """Tally outreach responses logged via 'response from X at Y: classification' directive."""
    if not OUTREACH_LOG.exists():
        return {"sent": 0, "engaged": 0, "no_response": 0, "negative": 0}
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    tally = defaultdict(int)
    with OUTREACH_LOG.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("logged_at", "") >= cutoff:
                    tally[r.get("classification", "unknown")] += 1
            except Exception:
                pass
    return dict(tally)


def _quality_metrics():
    """Edit-distance from Alice's draft to Jordan Avery's final, per artifact type."""
    out = {"resume_edits": [], "cover_edits": []}
    for app_dir in APPS_DIR.glob("*/"):
        for pair in (("resume-draft.md", "resume-final.md"), ("cover-letter-draft.md", "cover-letter-final.md")):
            draft = app_dir / pair[0]
            final = app_dir / pair[1]
            if draft.exists() and final.exists():
                d = draft.read_text()
                f = final.read_text()
 # crude edit-distance proxy: % of changed chars
                if d:
                    diff_pct = abs(len(f) - len(d)) / len(d) * 100
                else:
                    diff_pct = 0
                key = "resume_edits" if "resume" in pair[0] else "cover_edits"
                out[key].append({"app": app_dir.name, "diff_pct": round(diff_pct, 1)})
    return out


def _cost_metrics(days=7):
    """Aggregate cost from time-cost-log.jsonl."""
    if not COST_LOG.exists():
        return {"this_week": 0, "cumulative": 0, "calls": 0, "compute_s": 0, "by_task": {}, "max_op": None}
    week_cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    week_records = []
    all_records = []
    with COST_LOG.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                all_records.append(r)
                if r.get("ts", "") >= week_cutoff:
                    week_records.append(r)
            except Exception:
                pass
    by_task = defaultdict(lambda: {"count": 0, "cost": 0.0})
    for r in week_records:
        t = r.get("task", "unknown")
        by_task[t]["count"] += 1
        by_task[t]["cost"] += r.get("cost_usd", 0)
    max_op = max(week_records, key=lambda r: r.get("cost_usd", 0), default=None)
    return {
        "this_week":  round(sum(r.get("cost_usd", 0) for r in week_records), 4),
        "cumulative": round(sum(r.get("cost_usd", 0) for r in all_records), 4),
        "calls":      len(week_records),
        "compute_s":  round(sum(r.get("latency_s", 0) for r in week_records), 1),
        "by_task":    {k: {"count": v["count"], "cost": round(v["cost"], 4)} for k, v in by_task.items()},
        "max_op":     {"task": max_op["task"], "cost": max_op.get("cost_usd", 0)} if max_op else None,
        "days_active_total": len({r["ts"][:10] for r in all_records if "ts" in r}),
    }


def _focus_discipline_metrics(days=7):
    """Focus list churn + outcomes."""
    state = focus._load()
    history = state.get("version_history", [])
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    week_actions = [h for h in history if h.get("ts", "") >= cutoff]
    return {
        "current_focus":         len(state.get("roles", [])),
        "actions_this_week":     len(week_actions),
        "sets_this_week":        sum(1 for h in week_actions if h.get("action") == "set"),
        "adds_this_week":        sum(1 for h in week_actions if h.get("action") == "add"),
        "drops_this_week":       sum(1 for h in week_actions if h.get("action") == "drop"),
        "auto_drops_this_week":  sum(1 for h in week_actions if h.get("action") == "auto_drop"),
    }


def _hypotheses_summary():
    if not HYPOTHESES.exists():
        return "(no hypotheses tracked yet)"
    return HYPOTHESES.read_text()[:1500]


def _correction_patterns(days=7):
    """Structured pull from the decision-feedback store, scoped to the
    trailing window. The text returned is the human-readable aggregate
    decision_feedback.render_pattern_summary builds; empty if no
    confirmed corrections landed in the window.

    DESIGN BOUNDARY: this is DESCRIPTIVE. The scorecard prompt is what
    decides how to talk about it. Do not turn it into a forecast.
    """
    try:
        from alice.persistence import decision_feedback as df
        return df.render_pattern_summary(days) or "(no confirmed corrections in window)"
    except Exception as e:
        return f"(correction-patterns block unavailable: {e})"


def generate():
    funnel, median_s2s = _funnel_metrics(7)
    outreach = _outreach_metrics(7)
    quality = _quality_metrics()
    cost = _cost_metrics(7)
    focus_disc = _focus_discipline_metrics(7)
    hypotheses = _hypotheses_summary()
    corrections = _correction_patterns(7)

    metrics_summary = f"""
FUNNEL (last 7 days):
  Surfaced: {funnel.get('surfaced', 0)}
  Good fit: {funnel.get('good fit', 0)}
  Materials pending: {funnel.get('materials pending', 0)}
  Submitted: {funnel.get('submitted', 0)}
  First screen scheduled: {funnel.get('first screen scheduled', 0)}
  Interviewing: {funnel.get('interviewing', 0)}
  Offer: {funnel.get('offer', 0)}
  Median surface->submit: {median_s2s} days (if applicable)

OUTREACH (last 7 days):
  By response classification: {outreach}

QUALITY (edit-distance from my drafts to Jordan Avery's finals):
  Resume drafts: {quality['resume_edits']}
  Cover letter drafts: {quality['cover_edits']}

FOCUS DISCIPLINE:
  Current focus list size: {focus_disc['current_focus']}
  Focus actions this week: {focus_disc['actions_this_week']} (sets: {focus_disc['sets_this_week']}, adds: {focus_disc['adds_this_week']}, drops: {focus_disc['drops_this_week']}, auto-drops: {focus_disc['auto_drops_this_week']})

TIME + COST (last 7 days):
  LLM calls: {cost['calls']}
  Compute time (sum of latencies): {cost['compute_s']:.1f}s
  Cost this week: ${cost['this_week']:.4f}
  Cumulative cost: ${cost['cumulative']:.4f} over {cost['days_active_total']} days
  By task type: {cost['by_task']}
  Most expensive single op: {cost['max_op']}

HYPOTHESIS REGISTRY (snippet):
{hypotheses}

CORRECTION PATTERNS (where Jordan Avery's read diverged from mine in the last 7 days
— from feedback/decision-feedback.jsonl, captured via explicit and ambient
triggers; descriptive aggregate, not a prediction):
{corrections}
"""

    brief = llm.load_alice_brief()
    prompt = f"""Produce this week's Friday scorecard for Jordan Avery per your brief.

RAW METRICS
{metrics_summary}

YOUR JOB
Write the scorecard per the format in your brief (Friday scorecard format section). Sections:
  FUNNEL, OUTREACH, INTERVIEWS, QUALITY, CALIBRATION, OBSERVATIONS, PROPOSALS,
  PATTERNS I'M WATCHING, BEHAVIOR PATTERNS, TIME + COST, LAST WEEK'S WRONG CALL,
  WHAT I'D CHANGE NEXT WEEK.

CRITICAL CONSTRAINTS
- Honest by the numbers, no narrative spin
- If sample is tiny (n<10), say so explicitly; do not make claims
- Name your wrong calls — if you don't have a "wrong call" section because no data
  contradicts your prior calls yet, say so plainly
- Estimated human-equivalent recruiter time: rough estimate, flag as estimate
- Jordan Avery's voice: direct, no em dashes, no consulting-speak, no "passionate"
- End with "NO ACTION NEEDED unless you want to push back on any of this."
"""
    print(f"  generating Friday scorecard (model: {llm.MODEL_FOR_TASK['weekly_scorecard']})...")
    res = llm.call("weekly_scorecard", prompt, system=brief, max_tokens=3000)

 # Save to history
    SCORECARDS.mkdir(parents=True, exist_ok=True)
    today = date.today()
    yr, wk, _ = today.isocalendar()
    history_path = SCORECARDS / f"{yr}-W{wk:02d}.md"
    history_path.write_text(f"# Friday Scorecard — week ending {today.isoformat()}\n\n{res['text']}\n\n---\n\n## Raw metrics\n{metrics_summary}\n")
    print(f"  saved to {history_path}")

 # Send via email — C2 verifier: IMAP Sent-folder probe.
    subject = f"Friday scorecard — week ending {today.isoformat()}"
    ok = notify_email.send(subject, res["text"], digest=True)
    if ok:
        from alice.ops import verify
        vr = verify.verify_email_send(subject_substr=subject[:60])
        if not vr.ok:
            print(f"  VERIFY ERROR email_send (scorecard): {vr.claim}")
    print(f"  sent scorecard email")

    return {"cost": res["cost_usd"], "history_path": str(history_path)}


if __name__ == "__main__":
    generate()
