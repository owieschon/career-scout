"""Wrapper for daily-cron focus auto-drop step — runs focus.auto_drop_submitted
and records activity for the digest.

C4 gate (item 6a): auto_drop is no longer silent. Every drop announces to
Jordan Avery via Telegram and email, naming the role and the triggering status.
Roles whose triggering status was an unauthorized Alice write are NOT
dropped (the guard now lives in focus.auto_drop_submitted) but are surfaced
to Jordan Avery with the source of the unauthorized write so he can investigate.
"""
import sys
from pathlib import Path

from alice.observability import obs
obs.init("focus_auto_drop")

from alice.persistence import focus
from alice.persistence import activity_log

result = focus.auto_drop_submitted()
print(result)

dropped = result.get("dropped", [])
skipped = result.get("skipped_unauthorized", [])

# C4 announce gate: notify Jordan Avery via BOTH channels for every drop.
# C2 verifiers run on each send so verify-log.jsonl captures every announce.
if dropped:
    try:
        import notify_telegram, notify_email, verify
        for d in dropped:
            role_label = f"{d.get('company','')} | {d.get('role','')}".strip(" |")
            trigger = d.get("auto_dropped_at_status", "?")
            line = f"Dropped {role_label} from focus, trigger: status={trigger}"
            print(f"  ANNOUNCE: {line}")
            try:
                tg_res = notify_telegram.send_with_id(f"Alice: focus auto-drop\n\n{line}")
                if tg_res.get("ok"):
                    vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
                    if not vr.ok:
                        print(f"  VERIFY ERROR telegram_send (auto_drop): {vr.claim}")
            except Exception as e:
                print(f"  [telegram announce failed: {e}]")
            try:
                subj = f"Alice: focus auto-drop — {role_label}"
                ok = notify_email.send(subj, f"Jordan Avery,\n\n{line}\n\nAlice")
                if ok:
                    vr = verify.verify_email_send(subject_substr=subj[:60])
                    if not vr.ok:
                        print(f"  VERIFY ERROR email_send (auto_drop): {vr.claim}")
            except Exception as e:
                print(f"  [email announce failed: {e}]")
    except Exception as e:
        print(f"  [announce step failed: {e}]")

# Surface the unauthorized-write skips loudly — these are Boreal CAD-class signals.
if skipped:
    try:
        import notify_telegram, notify_email, verify
        for s in skipped:
            role_label = f"{s.get('company','')} | {s.get('role','')}".strip(" |")
            trigger = s.get("trigger_status", "?")
            src = s.get("last_write_source", "?")
            line = (
                f"Skipped auto-drop of {role_label}: triggering status='{trigger}' "
                f"was written by Alice WITHOUT authorization (source={src}). "
                f"Boreal CAD-class. Investigate before this status is trusted."
            )
            print(f"  SKIP-UNAUTH: {line}")
            try:
                tg_res = notify_telegram.send_with_id(
                    f"Alice: Boreal CAD-class write detected\n\n{line}"
                )
                if tg_res.get("ok"):
                    vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
                    if not vr.ok:
                        print(f"  VERIFY ERROR telegram_send (ptc_skip): {vr.claim}")
            except Exception as e:
                print(f"  [telegram skip-announce failed: {e}]")
            try:
                subj = f"Alice: Boreal CAD-class write detected — {role_label}"
                ok = notify_email.send(subj, f"Jordan Avery,\n\n{line}\n\nAlice")
                if ok:
                    vr = verify.verify_email_send(subject_substr=subj[:60])
                    if not vr.ok:
                        print(f"  VERIFY ERROR email_send (ptc_skip): {vr.claim}")
            except Exception as e:
                print(f"  [email skip-announce failed: {e}]")
    except Exception as e:
        print(f"  [skip-announce step failed: {e}]")

if dropped:
    names = [f"{d.get('company','')} {d.get('role','')}".strip() for d in dropped]
    summary = f"auto-dropped {len(dropped)} focus role(s) past submitted: {', '.join(names)}"
    activity_log.record(step="focus_auto_drop", summary=summary,
                        count=len(dropped), details={"dropped": names,
                                                     "skipped_unauthorized": len(skipped)})
elif skipped:
    activity_log.record(step="focus_auto_drop",
                        summary=f"skipped {len(skipped)} unauthorized-write drops (Boreal CAD-class)",
                        count=0, status="skipped",
                        details={"skipped_unauthorized": len(skipped)})
else:
    activity_log.record(step="focus_auto_drop",
                        summary="no focus roles past submitted",
                        count=0, status="noop")
