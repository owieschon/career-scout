"""Pre-interview morning reminder — sends a fresh email (separate from daily digest)
~3 hours before any scheduled interview today.

Reads:  feedback/scheduled-screens.json (date strings parsed best-effort)
        applications/<slug>/interview-prep-r<N>.md (top talking points + ask-questions)
Sends:  Standalone email via notify_email
Writes: Marks reminder_sent=True on the schedule entry to avoid re-sending.
"""
import json
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.notify import notify_email

SCHEDULES = Path(str(repo_paths.FEEDBACK / "scheduled-screens.json"))
APPS_DIR = Path(str(repo_paths.APPLICATIONS))


def _slug(company, role):
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def _is_today(when_str):
    """Best-effort parse of 'fri 6/14 11am ET' style strings — returns True if appears to be today."""
    today = date.today()
    if not when_str:
        return False
    s = when_str.lower()
 # Look for month/day patterns
    m = re.search(r"(\d{1,2})/(\d{1,2})", s)
    if m:
        try:
            mm = int(m.group(1)); dd = int(m.group(2))
            yr = today.year
            if date(yr, mm, dd) == today:
                return True
        except Exception:
            pass
 # Also accept "today" literal
    if "today" in s:
        return True
 # Day-of-week match (loose)
    weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_dow = weekday_names[today.weekday()]
    if today_dow in s:
 # Only true if no specific date that conflicts
        return True
    return False


def _read_prep_doc(slug):
    """Read the latest interview-prep-r<N>.md for this slug."""
    pkg_dir = APPS_DIR / slug
    if not pkg_dir.exists():
        return None
    prep_files = sorted([p for p in pkg_dir.glob("interview-prep-r*.md")])
    if not prep_files:
        return None
    return prep_files[-1].read_text()


def _generate_morning_brief(entry, prep_doc):
    company = entry.get("substr", "").split()[0].title()  # best-guess display
    when = entry.get("when", "")
    interviewers = entry.get("interviewers", "")
    brief = llm.load_alice_brief()
    prompt = f"""Write the morning-of-interview reminder email the operator will read on their phone before the screen.

CONTEXT
Interview: {entry.get('substr')}
When: {when}
Interviewers: {interviewers}

PREP DOC (full prep package — distill the top items only)
{prep_doc[:6000] if prep_doc else "(no prep doc — fallback to a generic confidence reminder)"}

YOUR JOB
Produce a tight, mobile-readable email body. Per your brief (Triggered by upcoming scheduled
interview section), include:
  - Who they're talking to (1 line)
  - Top 3 talking points
  - One thing about the interviewer (if known)
  - 3 questions to ask
  - Comp position if it comes up
  - Explicit DO NOTs (e.g. don't over-disclose or negotiate at first-screen stage)
  - Link to full prep doc

CONSTRAINTS
- Plain text, mobile-readable
- Brief — fits one phone screen if possible
- The operator's voice: direct, no em dashes, no "passionate"
- End with one line of "good luck" (plain, not theatrical)
- Note: tomorrow morning's digest will include the debrief prompt
"""
    print(f"    generating morning reminder (model: {llm.MODEL_FOR_TASK['morning_reminder']})...")
    res = llm.call("morning_reminder", prompt, system=brief, max_tokens=1000)
    return res


def main():
    if not SCHEDULES.exists():
        print("[morning_reminder: no scheduled screens]")
        try:
            from alice.persistence import activity_log
            activity_log.record(step="morning_reminder",
                                summary="no scheduled screens",
                                count=0, status="noop")
        except Exception as e:
            print(f"[activity_log: {e}]")
        return
    schedules = json.loads(SCHEDULES.read_text())
    sent = 0
    for entry in schedules:
        if entry.get("reminder_sent"):
            continue
        when = entry.get("when", "")
        if not _is_today(when):
            continue
        substr = entry.get("substr", "")
 # Find the matching slug
        from alice.notify.imap_reply import _match_sheet_row
        from alice.persistence import ledger
        rows = ledger._ws().get_all_records()
        hits = _match_sheet_row(substr, rows)
        if len(hits) != 1:
            print(f"  no unique sheet match for {substr!r}; skipping")
            continue
        _, row = hits[0]
        slug = _slug(row.get("company", ""), row.get("role", ""))
        prep_doc = _read_prep_doc(slug)
        try:
            from alice.ops import verify
            res = _generate_morning_brief(entry, prep_doc)
            subject = f"{when} screen at {row.get('company')} — quick brief"
            ok = notify_email.send(subject, res["text"])
 # C2 verifier: IMAP Sent-folder probe.
            if ok:
                vr = verify.verify_email_send(subject_substr=subject[:60])
                if not vr.ok:
                    print(f"  VERIFY ERROR email_send (morning_reminder): {vr.claim}")
            try:
                from alice.notify import notify_telegram
                tg_res = notify_telegram.send_with_id(
                    f"Alice: {subject}\n\nCheck email for full brief."
                )
                if tg_res.get("ok"):
                    vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
                    if not vr.ok:
                        print(f"  VERIFY ERROR telegram_send (morning_reminder): {vr.claim}")
            except Exception as _tge:
                print(f"[telegram reminder ping failed: {_tge}]")
            entry["reminder_sent"] = True
            entry["reminder_sent_at"] = datetime.now().isoformat(timespec="seconds")
            sent += 1
            print(f"  sent morning reminder for {row.get('company')}")
        except Exception as e:
            print(f"  ERROR: {e}")
    if sent:
        SCHEDULES.write_text(json.dumps(schedules, indent=2))
    print()
    print(f"summary: {sent} morning reminders sent")
    try:
        from alice.persistence import activity_log
        activity_log.record(
            step="morning_reminder",
            summary=(f"{sent} morning reminder{'' if sent == 1 else 's'} sent" if sent
                     else "no interviews today"),
            count=sent, status="ok" if sent else "noop",
        )
    except Exception as e:
        print(f"[activity_log: {e}]")


if __name__ == "__main__":
    main()
