"""Behavior pattern observation — scans sheet + state for Jordan's engagement patterns.

Detects: labeling-without-prep, prep-without-submit, submit-without-prep, inactivity gaps,
hot spots. Returns structured findings for daily digest rendering.

No LLM by default — observation is pure state inspection. Alice's voice is applied when
the digest assembler writes the human text from these findings.
"""
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from alice import repo_paths
from alice.persistence import ledger

LOOKBACK_DAYS = 7


def _get_log_records(days=7):
    """Read time-cost-log.jsonl to assess activity (proxy for engagement)."""
    log = Path(str(repo_paths.FEEDBACK / "time-cost-log.jsonl"))
    if not log.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    out = []
    with log.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("ts", "") >= cutoff:
                    out.append(r)
            except Exception as _e:
                try:
                    import obs; obs.capture(_e, where="behavior_patterns:_get_log_records:json", payload={"line": line[:200]})
                except Exception:
                    pass
    return out


def detect_patterns():
    """Return dict of {pattern_key: {detected: bool, detail: ...}}."""
    ws = ledger._ws()
    rows = ws.get_all_records()
    today = date.today()
    cutoff = today - timedelta(days=LOOKBACK_DAYS)

 # Group rows by status change activity
    by_status_recent = defaultdict(list)
    for i, r in enumerate(rows, start=2):
        status = (r.get("status") or "new").strip().lower()
        date_str = (r.get("status_changed_date") or "").strip()
        if not date_str or status == "new":
            continue
        try:
            d = date.fromisoformat(date_str.split("T")[0])
        except Exception as _e:
            try:
                import obs; obs.capture(_e, where="behavior_patterns:detect_patterns:date", payload={"date_str": date_str})
            except Exception:
                pass
            continue
        if d >= cutoff:
            by_status_recent[status].append({
                "row_idx": i, "company": r.get("company", ""), "role": r.get("role", ""),
                "days_since_change": (today - d).days,
            })

 # Pattern 1: labeling-without-prep
    good_fits = by_status_recent.get("good fit", [])
    materials_pending = by_status_recent.get("materials pending", [])
    labeling_without_prep = {
        "detected": len(good_fits) >= 3 and len(materials_pending) == 0,
        "good_fit_count": len(good_fits),
        "materials_pending_count": len(materials_pending),
        "sample_good_fits": good_fits[:5],
    }

 # Pattern 2: prep-without-submit
    submitted = by_status_recent.get("submitted", [])
    stuck_in_prep = [m for m in materials_pending if m["days_since_change"] >= 5]
    prep_without_submit = {
        "detected": len(stuck_in_prep) > 0,
        "stuck_count": len(stuck_in_prep),
        "stuck_roles": stuck_in_prep,
    }

 # Pattern 3: submit-without-prep (status went submitted but row never showed materials_pending)
 # Hard to detect without history; use heuristic: any submitted row whose prep dir is empty
    submit_without_prep = {"detected": False, "submitted_without_package": []}
    apps_dir = Path(str(repo_paths.APPLICATIONS))
    for s in submitted:
        role_slug = _to_slug(s["company"], s["role"])
        pkg = apps_dir / role_slug
        if not pkg.exists() or not any(pkg.iterdir()):
            submit_without_prep["submitted_without_package"].append(s)
    submit_without_prep["detected"] = len(submit_without_prep["submitted_without_package"]) > 0

 # Pattern 4: inactivity gap — no log records, no status changes in past 2+ days
    log_records = _get_log_records(days=3)
    if log_records:
        last_log_ts = max(r.get("ts", "") for r in log_records)
        try:
            last_log_date = datetime.fromisoformat(last_log_ts).date()
            days_since_log = (today - last_log_date).days
        except Exception as _e:
            try:
                import obs; obs.capture(_e, where="behavior_patterns:detect_patterns:last_log_ts", payload={"last_log_ts": last_log_ts})
            except Exception:
                pass
            days_since_log = 0
    else:
        days_since_log = LOOKBACK_DAYS
    any_recent_status_change = any(
        d.get("days_since_change", LOOKBACK_DAYS) <= 2
        for status_list in by_status_recent.values()
        for d in status_list
    )
    inactivity_gap = {
        "detected": days_since_log >= 2 and not any_recent_status_change,
        "days_since_last_activity": days_since_log,
    }

 # Pattern 5: hot spot — many status changes in past 24h
    last_24h_changes = sum(
        1 for status_list in by_status_recent.values()
        for d in status_list
        if d.get("days_since_change", LOOKBACK_DAYS) <= 1
    )
    hot_spot = {
        "detected": last_24h_changes >= 8,
        "changes_in_24h": last_24h_changes,
    }

    return {
        "labeling_without_prep":  labeling_without_prep,
        "prep_without_submit":    prep_without_submit,
        "submit_without_prep":    submit_without_prep,
        "inactivity_gap":         inactivity_gap,
        "hot_spot":               hot_spot,
    }


def _to_slug(company, role):
    """Mirror the slug-generation used by prep_materials so app directories match."""
    import re
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80]


def render(patterns):
    """Render detected patterns as digest text. Returns empty string if nothing fires."""
    lines = []
    if patterns["labeling_without_prep"]["detected"]:
        n = patterns["labeling_without_prep"]["good_fit_count"]
        lines.append(f"  • You labeled {n} good fits this week and started prep on 0. "
                     f"What's the friction? Reply 'help with <substring>' or 'just busy'.")
    if patterns["prep_without_submit"]["detected"]:
        for r in patterns["prep_without_submit"]["stuck_roles"][:3]:
            lines.append(f"  • {r['company']} package has been ready {r['days_since_change']}d. "
                         f"Submit or 'revise {r['company'].lower()}'?")
    if patterns["submit_without_prep"]["detected"]:
        for r in patterns["submit_without_prep"]["submitted_without_package"][:3]:
            lines.append(f"  • You submitted to {r['company']} without going through prep. "
                         f"Was that intentional? If not, want me to draft a follow-up note?")
    if patterns["inactivity_gap"]["detected"]:
        d = patterns["inactivity_gap"]["days_since_last_activity"]
        lines.append(f"  • Quiet for {d}d. No pressure. Reply 'pause digest' to silence, "
                     f"'lighter digest' to reduce volume, or just thread back when ready.")
    if patterns["hot_spot"]["detected"]:
        n = patterns["hot_spot"]["changes_in_24h"]
        lines.append(f"  • Big session yesterday ({n} status changes). Want me to surface "
                     f"the 3 highest-leverage roles to focus on, or are you in throughput mode?")
    if not lines:
        return ""
    return "BEHAVIOR PATTERNS (observational, no judgment):\n" + "\n".join(lines)


if __name__ == "__main__":
    patterns = detect_patterns()
    text = render(patterns)
    print(text if text else "(no behavior patterns detected this cycle)")
