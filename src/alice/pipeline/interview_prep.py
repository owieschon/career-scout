"""Interview prep package — triggered by 'first screen scheduled' status.

Reads:  feedback/scheduled-screens.json (the operator's `screen scheduled:` directives)
        applications/<slug>/.metadata.json (app context if package exists)
Writes: applications/<slug>/interview-prep-r<N>.md (per round)
        Updates scheduled-screens.json with prep_generated=True
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger

SCHEDULES = Path(str(repo_paths.FEEDBACK / "scheduled-screens.json"))
APPS_DIR = Path(str(repo_paths.APPLICATIONS))


def _slug(company, role):
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def _row_for_substr(substr):
    from alice.notify.imap_reply import _match_sheet_row
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(substr, rows)
    if len(hits) != 1:
        return None
    return hits[0]


def _fetch_jd(url):
    try:
        from alice.pipeline.enrich_hypotheses import fetch_jd
        return fetch_jd(url)
    except Exception:
        return None


def generate_prep(scheduled_entry, round_num=1):
    substr = scheduled_entry.get("substr", "")
    when = scheduled_entry.get("when", "")
    interviewers = scheduled_entry.get("interviewers", "")
    hit = _row_for_substr(substr)
    if not hit:
        return {"error": f"no unique row match for {substr!r}"}
    row_idx, row = hit
    company = row.get("company", "")
    role_title = row.get("role", "")
    url = row.get("url", "")
    rationale = row.get("rationale", "")

    slug = _slug(company, role_title)
    pkg_dir = APPS_DIR / slug
    pkg_dir.mkdir(parents=True, exist_ok=True)

 # Read application-strategy if it exists for context continuity
    strategy_path = pkg_dir / "application-strategy.md"
    strategy_context = strategy_path.read_text() if strategy_path.exists() else ""

 # Read outreach-targets for any contact intel
    targets_path = pkg_dir / "outreach-targets.md"
    targets_context = targets_path.read_text() if targets_path.exists() else ""

    jd_body = _fetch_jd(url) or "(JD not fetchable; reasoning from rationale + role title)"

    brief = llm.load_alice_brief()
    prep_prompt = f"""Produce the interview prep package for the operator's upcoming screen at {company}.

CONTEXT
Company: {company}
Role: {role_title}
URL: {url}
When: {when}
Interviewers: {interviewers}
Round: {round_num}
Fit signal: {rationale}

JD
{jd_body[:5500]}

APPLICATION STRATEGY (your earlier note for the operator's screen prep)
{strategy_context[:2000] if strategy_context else "(no prior strategy doc; this is the first artifact for this role)"}

CONTACT INTEL (if any)
{targets_context[:1500] if targets_context else "(no contact intel surfaced)"}

YOUR JOB — produce ALL sections per your brief (Triggered by `first screen scheduled`):

1. **Company deep dive (last 30/60/90 days):** funding events, leadership changes, public roadmap signals, recent product releases, competitive moves, layoffs/reorgs, press mentions. Reason from public knowledge; flag any gaps as "verify before screen". Do NOT fabricate news that doesn't exist.

2. **Interviewer research:** if interviewer names + LinkedIn URLs are provided in the input, extract relevant context. Without URLs, work from name + role only. If you have no specific intel, say so honestly.

3. **Likely questions for this role + the operator's STAR-format draft answers:** 5-8 questions a hiring manager at {company} in this seat would actually ask. Draft the operator's STAR answer for each, using [FILL: <specific story needed>] where the operator's specifics go.

4. **Questions the operator should ASK:** 5-8 sharp questions tailored to {company} that demonstrate seriousness and probe real concerns. Include the 1-2 uncomfortable questions worth asking (leadership turnover; what happened to the prior person; what's the bar for 12-month review).

5. **Red flags worth probing delicately:** anything from your deep dive that warrants caution. Phrased as questions to ask, not accusations.

6. **Comp positioning if comp comes up:** anchored to what's been disclosed or your benchmark estimate for {company} at this stage.

OUTPUT: clean markdown with the 6 sections above as H2 headers. The operator's voice in any drafted answers: direct, no em dashes, no "passionate", no consulting-speak.
"""
    print(f"    generating interview prep r{round_num} (model: {llm.MODEL_FOR_TASK['interview_prep']})...")
    res = llm.call("interview_prep", prep_prompt, system=brief, max_tokens=3500)
    prep_path = pkg_dir / f"interview-prep-r{round_num}.md"
    header = f"""# Interview Prep — {company} · {role_title} · Round {round_num}

**Scheduled:** {when}
**Interviewers:** {interviewers}
**Generated:** {datetime.now().isoformat(timespec='seconds')}
**LLM:** {res['model']} ({res['in_tokens']}+{res['out_tokens']} tokens, ${res['cost_usd']:.4f})

---

"""
    prep_path.write_text(header + res["text"])
    return {"prep_path": str(prep_path), "cost": res["cost_usd"], "row_idx": row_idx}


def process_pending():
    """Generate prep for any scheduled screens that haven't been prep'd yet."""
    if not SCHEDULES.exists():
        print("[interview_prep: no scheduled screens]")
        try:
            from alice.persistence import activity_log
            activity_log.record(step="interview_prep",
                                summary="no scheduled screens",
                                count=0, status="noop")
        except Exception as e:
            print(f"[activity_log: {e}]")
        return {"processed": 0}
    schedules = json.loads(SCHEDULES.read_text())
    processed = 0
    total_cost = 0.0
    for entry in schedules:
        if entry.get("prep_generated"):
            continue
        print(f"  [generating prep for {entry.get('substr')!r}]")
        try:
            result = generate_prep(entry)
            entry["prep_generated"] = True
            entry["prep_generated_at"] = datetime.now().isoformat(timespec="seconds")
            entry["prep_path"] = result.get("prep_path")
            total_cost += result.get("cost", 0)
            processed += 1
        except Exception as e:
            entry["error"] = str(e)[:200]
            print(f"    ERROR: {e}")
    SCHEDULES.write_text(json.dumps(schedules, indent=2))
    print()
    print(f"summary: {processed} interview prep packages generated, ${total_cost:.4f} spent")
    try:
        from alice.persistence import activity_log
        activity_log.record(
            step="interview_prep",
            summary=(f"{processed} interview prep pack{'' if processed == 1 else 's'} generated"
                     if processed else "no new prep needed"),
            count=processed, cost=total_cost,
            status="ok" if processed else "noop",
        )
    except Exception as e:
        print(f"[activity_log: {e}]")
    return {"processed": processed, "cost": total_cost}


if __name__ == "__main__":
    process_pending()
