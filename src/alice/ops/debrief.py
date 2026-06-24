"""Post-interview debrief — Alice prompts Jordan, integrates the answers, produces
thank-you note draft + updates pattern tracking.

Reads:  feedback/debrief-queue.json (pending debrief requests)
        feedback/debrief-answers.jsonl (Jordan's answers)
        applications/<slug>/.metadata.json (app context)
Writes: applications/<slug>/debrief-r<N>.md
        applications/<slug>/thank-you-r<N>.md
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger

QUEUE = Path(str(repo_paths.FEEDBACK / "debrief-queue.json"))
ANSWERS = Path(str(repo_paths.FEEDBACK / "debrief-answers.jsonl"))
APPS_DIR = Path(str(repo_paths.APPLICATIONS))

DEBRIEF_PROMPTS = [
    "What 2-3 questions caught you flat-footed?",
    "What did you nail?",
    "What signal did you get on fit (positive / negative / mixed)?",
    "What did the interviewer signal about next steps?",
    "Any red flags that surfaced?",
    "Your read: want to proceed if invited?",
    "What would you do differently next time?",
]


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


def get_pending_prompts():
    """Return list of debrief-prompt-package strings to include in next digest."""
    if not QUEUE.exists():
        return []
    queue = json.loads(QUEUE.read_text())
    out = []
    for entry in queue:
        if entry.get("status") != "pending":
            continue
        hit = _row_for_substr(entry.get("substr", ""))
        if not hit:
            continue
        row_idx, row = hit
        company = row.get("company", "")
        role_title = row.get("role", "")
        slug = _slug(company, role_title)
        round_n = entry.get("round", 1)
        prompt_text = f"""Quick capture from your {company} screen (round {round_n}):

"""
        for i, q in enumerate(DEBRIEF_PROMPTS, 1):
            prompt_text += f"  {i}. {q}\n"
        prompt_text += f"""
Reply 'debrief 1: <text>' through 'debrief 7: <text>'.
I'll save to applications/{slug}/debrief-r{round_n}.md and draft a thank-you within 24h.
"""
        out.append({
            "substr":     entry.get("substr"),
            "company":    company,
            "role":       role_title,
            "round":      round_n,
            "prompt":     prompt_text,
            "queue_entry": entry,
        })
    return out


def integrate_debrief_answers():
    """Read debrief-answers.jsonl, group by recency, produce debrief docs + thank-yous."""
    if not ANSWERS.exists():
        print("[debrief: no answers yet]")
        return {"integrated": 0}
 # Group answers by recency clusters — naive: any answer received in past 36h is "current"
 # Better: tag answers with thread/slug in directive payload (Jordan would need to specify
 # which role they're debriefing). For now, group most recent batch and pick the most-recent
 # pending debrief.
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=36)).isoformat()
    answers = []
    with ANSWERS.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("received_at", "") >= cutoff:
                    answers.append(r)
            except Exception:
                pass
    if not answers:
        return {"integrated": 0}

 # Find which debrief these belong to (most recent pending)
    if not QUEUE.exists():
        return {"integrated": 0, "info": "no pending debrief requests"}
    queue = json.loads(QUEUE.read_text())
    pending = [e for e in queue if e.get("status") == "pending"]
    if not pending:
        return {"integrated": 0, "info": "no pending debrief requests"}
 # Most recent first
    entry = sorted(pending, key=lambda e: e.get("queued_at", ""), reverse=True)[0]

    hit = _row_for_substr(entry.get("substr", ""))
    if not hit:
        return {"error": f"no row for {entry.get('substr')!r}"}
    row_idx, row = hit
    company = row.get("company", "")
    role_title = row.get("role", "")
    slug = _slug(company, role_title)
    round_n = entry.get("round", 1)
    pkg_dir = APPS_DIR / slug
    pkg_dir.mkdir(parents=True, exist_ok=True)

 # Build the debrief doc with Jordan's answers mapped to prompts
    answers_by_num = {a.get("q_num"): a.get("text", "") for a in answers}
    debrief_md = f"""# Debrief — {company} · {role_title} · Round {round_n}

**Captured:** {datetime.now().isoformat(timespec='seconds')}

"""
    for i, q in enumerate(DEBRIEF_PROMPTS, 1):
        ans = answers_by_num.get(i, "(not answered)")
        debrief_md += f"## {i}. {q}\n\n{ans}\n\n"
    (pkg_dir / f"debrief-r{round_n}.md").write_text(debrief_md)

 # Draft thank-you note
    brief = llm.load_alice_brief()
    thank_prompt = f"""Jordan just did their round {round_n} screen at {company} for the {role_title} role.
Here's the debrief:

{json.dumps(answers_by_num, indent=2)}

Draft a thank-you note they can send within 24h. Names a SPECIFIC moment from the conversation
(from the debrief), references something concrete from the role. Jordan's voice: direct, no em dashes,
no consulting-speak, no "passionate". 3-5 sentences. Sign off plainly.
"""
    print(f"    drafting thank-you (model: {llm.MODEL_FOR_TASK['thank_you_note']})...")
    res = llm.call("thank_you_note", thank_prompt, system=brief, max_tokens=600)
    (pkg_dir / f"thank-you-r{round_n}.md").write_text(res["text"])

 # Mark the queue entry done
    entry["status"] = "completed"
    entry["completed_at"] = datetime.now().isoformat(timespec="seconds")
    entry["answers_received"] = len(answers)
    QUEUE.write_text(json.dumps(queue, indent=2))

    print(f"  [debrief integrated: {len(answers)} answers, thank-you drafted, ${res['cost_usd']:.4f}]")
    return {"integrated": len(answers), "company": company, "role": role_title, "cost": res["cost_usd"]}


if __name__ == "__main__":
    integrate_debrief_answers()
    pending = get_pending_prompts()
    if pending:
        print(f"{len(pending)} pending debrief prompts to include in next digest")
        for p in pending:
            print(f"  - {p['company']} ({p['role']})")
