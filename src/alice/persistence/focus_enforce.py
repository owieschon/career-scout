"""Focus enforcement — computes focus block, distraction flag, disengagement flag for daily digest.

Reads:
  - feedback/focus.json (current focus list)
  - sheet (current statuses + dates)
  - feedback/digest-prefs.json (cadence prefs)

Writes:
  - Returns a dict of digest sections that daily_delta.py renders.
  - No LLM calls — pure observation + structured rendering.

The actual nudge LANGUAGE (tone, framing) is Alice's brief responsibility;
Alice's voice gets applied when triage_observations or the digest assembler
writes the final text. This module just identifies WHAT to surface.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from alice import repo_paths
from alice.persistence import focus
from alice.persistence import ledger

DISENGAGEMENT_THRESHOLD_DAYS = {
    "materials pending": 5,   # alert if package has been pending Jordan action 5+ days
    "submitted":         10,  # alert at 10 days for follow-up
    "first screen scheduled": 14,  # alert if scheduled date passed without status change
    "good fit":          7,   # in focus, in good fit > 7d = analysis paralysis
}

# Move 1B: per-intent nudge effects for the DECLARED-intent states. The two
# longer windows below are the only NEW thresholds; the blank-intent path keeps
# using DISENGAGEMENT_THRESHOLD_DAYS unchanged. Tunable via
# feedback/digest-prefs.json under key "intent_effects" (merged over defaults),
# so Jordan can adjust without code changes ( / tunability requirement).
_INTENT_EFFECT_DEFAULTS = {
    "deliberating_soft_days": 14,  # a `deliberating` role: soft eventual re-surface only
    "waiting_cold_days":      21,  # a `waiting` role: flag only if it goes genuinely cold
}


def _intent_config():
    cfg = dict(_INTENT_EFFECT_DEFAULTS)
    try:
        p = Path(repo_paths.FEEDBACK / "digest-prefs.json")
        if p.exists():
            data = json.loads(p.read_text() or "{}")
            if isinstance(data.get("intent_effects"), dict):
                cfg.update(data["intent_effects"])
    except Exception:
        pass
    return cfg


def compute_focus_block():
    """Return dict with focus_roles list + meta (set_at, days_since_set, prompt_for_new)."""
    statuses = focus.status_for_focus()
    state = focus._load()
    set_at = state.get("set_at")
    days_since_set = None
    if set_at:
        try:
            d = datetime.fromisoformat(set_at).date()
            days_since_set = (date.today() - d).days
        except Exception:
            pass

 # Detect need to prompt for new focus
    needs_prompt = (
        len(statuses) == 0 or
        all(s["status"] in {"submitted", "first screen scheduled", "interviewing",
                            "offer", "negotiating", "closed", "not a fit"}
            for s in statuses)
    )

    return {
        "roles":          statuses,
        "set_at":         set_at,
        "days_since_set": days_since_set,
        "needs_prompt":   needs_prompt,
    }


def compute_distraction_flag():
    """Detect: Jordan labeled/engaged non-focus roles in last 24h while focus roles sit.
    Returns dict with counts + role names if distraction detected, or None."""
    focus_idxs = {r.get("row_idx") for r in focus.current()}
    if not focus_idxs:
        return None  # no focus set; no distraction to detect

    ws = ledger._ws()
    rows = ws.get_all_records()
    today = date.today()
    cutoff = today - timedelta(days=1)

    non_focus_labels = []
    for i, r in enumerate(rows, start=2):
        if i in focus_idxs:
            continue
        status = (r.get("status") or "").strip().lower()
        changed_raw = (r.get("status_changed_date") or "").strip()
        if not changed_raw or status in ("", "new"):
            continue
        try:
            changed = date.fromisoformat(changed_raw.split("T")[0])
        except Exception:
            continue
        if changed >= cutoff:
            non_focus_labels.append({
                "row_idx": i,
                "company": r.get("company", ""),
                "role":    r.get("role", ""),
                "status":  status,
                "changed": changed_raw,
            })

 # Are any focus roles sitting without movement?
    focus_sitting = []
    for s in focus.status_for_focus():
 # Move 1B: a DECLARED intent means the sit is accounted-for, not an
 # unexplained distraction. Blank intent -> unchanged behavior.
        if s.get("intent"):
            continue
        if s["status"] in ("good fit", "materials pending") and (s["days_at_status"] or 0) >= 2:
            focus_sitting.append(s)

    if non_focus_labels and focus_sitting:
        return {
            "non_focus_labeled": non_focus_labels,
            "focus_sitting":     focus_sitting,
        }
    return None


def compute_disengagement_flag():
    """Focus roles that haven't moved in their threshold-days. Returns list of
    stalled roles.

    Move 1B: a DECLARED intent overrides the heuristic, each state with its own
    distinct effect; a BLANK intent falls through to the existing heuristic
    UNCHANGED (the safety property — un-annotated roles behave exactly as today)."""
    cfg = _intent_config()
    stalled = []
    for s in focus.status_for_focus():
        status = s["status"]
        days = s["days_at_status"]
        intent = s.get("intent", "")
        if days is None:
            continue

 # ── declared-intent override (fires ONLY when Jordan has declared) ──
        if intent in ("holding", "done"):
 # holding = total mute; done = resolved. No nudge, ever.
            continue
        if intent == "active":
 # Jordan is actively working it; a sit is not disengagement.
            continue
        if intent == "deliberating":
 # Intentional sit: suppress the normal threshold, soft eventual
 # re-surface only, at a longer window.
            soft = cfg["deliberating_soft_days"]
            if days >= soft:
                stalled.append({**s, "threshold_days": soft,
                                "overdue_by": days - soft, "frame": "deliberating"})
            continue
        if intent == "waiting":
 # Expected sit (ball in their court): not disengagement, but flag if
 # it has gone genuinely cold past a longer window.
            cold = cfg["waiting_cold_days"]
            if days >= cold:
                stalled.append({**s, "threshold_days": cold,
                                "overdue_by": days - cold, "frame": "waiting_cold"})
            continue

 # ── blank intent: existing heuristic, UNCHANGED ──
        threshold = DISENGAGEMENT_THRESHOLD_DAYS.get(status)
        if threshold is None:
            continue
        if days >= threshold:
            stalled.append({**s, "threshold_days": threshold, "overdue_by": days - threshold})
    return stalled


def render_focus_block(block):
    """Render the focus_block dict as digest text (Alice voice — no em dashes)."""
    out = []
    if not block["roles"]:
        if block["needs_prompt"]:
            out.append("YOUR FOCUS: (empty — what's the focus this week?)")
            out.append("  Reply 'focus: <role>, <role>, <role>' to set up to 5 priority roles.")
            out.append("  I'll suggest candidates from this week's surfaced list if you want: reply 'focus suggest'.")
        return "\n".join(out)

    age = f", set {block['days_since_set']}d ago" if block["days_since_set"] is not None else ""
    out.append(f"YOUR FOCUS ({len(block['roles'])} roles{age}):")
    out.append("")
    for r in block["roles"]:
        days = f" ({r['days_at_status']} days)" if r["days_at_status"] is not None else ""
        out.append(f"  • {r['company']} — {r['role']}")
        out.append(f"    status: {r['status']}{days}")
        out.append(f"    next move: {r['next_move']}")
        out.append("")
    return "\n".join(out).rstrip()


def render_distraction_flag(flag):
    if not flag:
        return ""
    n = len(flag["non_focus_labeled"])
    sample = flag["non_focus_labeled"][:3]
    sample_str = ", ".join(f"{r['company']} {r['role'][:25]}" for r in sample)
    if n > 3:
        sample_str += f" + {n - 3} more"
    sitting = flag["focus_sitting"]
    sitting_str = ", ".join(f"{s['company']} ({s['status']}, {s['days_at_status']}d)" for s in sitting[:3])
    return (
        f"DISTRACTION FLAG (non-focus engagement in last 24h):\n"
        f"  You labeled / moved {n} non-focus role(s): {sample_str}.\n"
        f"  They're added to the running list. Meanwhile, focus roles sitting: {sitting_str}.\n"
        f"  If you want to reprioritize: 'focus add: <substring>' or 'focus: <new list>'.\n"
        f"  If you're sourcing in parallel and focus is right, no action needed."
    )


def render_disengagement_flag(stalled):
    if not stalled:
        return ""
    out = ["DISENGAGEMENT FLAG (focus roles past their move-threshold):"]
    for s in stalled:
        frame = s.get("frame")
        if frame == "deliberating":
            out.append(f"  • {s['company']} ({s['role'][:35]}): still deliberating, "
                       f"{s['days_at_status']}d (you said deliberating; soft check at {s['threshold_days']}d).")
        elif frame == "waiting_cold":
            out.append(f"  • {s['company']} ({s['role'][:35]}): waiting on them {s['days_at_status']}d "
                       f"(you said waiting; may have gone cold past {s['threshold_days']}d).")
        else:
 # Blank-intent / normal stalled role — byte-for-byte the today's line.
            out.append(f"  • {s['company']} ({s['role'][:35]}): {s['status']}, "
                       f"{s['days_at_status']}d (threshold {s['threshold_days']}d, +{s['overdue_by']}d over).")
    out.append("  Each needs a move from you. Reply 'help with <role>' if blocked,")
    out.append("  'revise <role>' if materials need another pass, or 'drop <role>' if changed mind.")
 # Invitation-to-declare (): teach the intent vocabulary so the nudge is
 # signal-elicitation, not a bare assertion. Additive — the lines above are
 # unchanged; this only ADDS a way for Jordan to tell me the state.
    out.append("  Or tell me the state so I stop guessing: 'holding: <role>' (mute), "
               "'deliberating: <role>', 'waiting: <role>', 'done: <role>'.")
    return "\n".join(out)


def main():
    """Standalone run: print all 3 sections for inspection."""
    block = compute_focus_block()
    distraction = compute_distraction_flag()
    disengagement = compute_disengagement_flag()

    print(render_focus_block(block))
    print()
    if distraction:
        print(render_distraction_flag(distraction))
        print()
    if disengagement:
        print(render_disengagement_flag(disengagement))
        print()
    if not distraction and not disengagement:
        print("(no flags this cycle)")


if __name__ == "__main__":
    main()
