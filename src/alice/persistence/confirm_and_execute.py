"""Confirmation gate — step 0 in run_daily.sh.

Reads feedback/pending-confirmation.json and either:
  1. Finds a correction reply from Jordan (subject: "Alice: confirming") ->
       re-parse, update pending, send new echo, reset 5-min timer.
  2. Pending is >= 5 min old with no correction ->
       execute all pending actions (status updates + directives), mark executed.
  3. Pending is fresh (< 5 min) ->
       report "waiting N more minutes" and exit. imap_reply will run after us
       but skip re-writing a new pending if one is already live.

If no pending-confirmation.json exists, exits immediately (nothing to do).
"""
import email
import imaplib
import json
import re
import ssl
import sys
from datetime import datetime, timedelta
from pathlib import Path

from alice import repo_paths

from alice import safe_state

_PENDING_CONF = Path(str(repo_paths.FEEDBACK / "pending-confirmation.json"))
_CORRECTION_SUBJECT_MARKER = "Alice: confirming"
_CONFIRM_WINDOW_MINUTES = 5

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


# ─── pending state helpers ────────────────────────────────────────────────────

def load_pending():
    try:
        return safe_state.atomic_read(_PENDING_CONF, default=None)
    except Exception:
        return None


def save_pending(data):
    safe_state.atomic_write(_PENDING_CONF, data)


def mark_executing(data):
    """Persist the 'executing' state BEFORE running handlers. This is the
    transition that protects against double-execution: if anything else
    tries to write to pending while status==executing, the writer refuses
    (see imap_reply._write_pending_confirmation invariant 3).
    """
    data["status"] = "executing"
    data["executing_at"] = datetime.now().isoformat(timespec="seconds")
    save_pending(data)


def mark_executed(data):
    data["status"] = "executed"
    data["executed_at"] = datetime.now().isoformat(timespec="seconds")
    save_pending(data)


# ─── IMAP helpers ─────────────────────────────────────────────────────────────

def _imap_open():
    from alice.jobcfg import load
    cfg = load()
    user = cfg["GMAIL_USER"]
    pw = cfg["GMAIL_APP_PASSWORD"].replace(" ", "")
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=_SSL_CTX)
    M.login(user, pw)
    return M, user


def _decode_subject(raw):
    from email.header import decode_header
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            out.append(txt.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(txt)
    return "".join(out)


def _body_text(msg):
    if msg.is_multipart():
        plain = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                txt = payload.decode(charset, errors="replace")
            except Exception:
                txt = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain.append(txt)
        return "\n".join(plain)
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def _strip_quoted(text):
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^On .{0,80}wrote:\s*$", stripped):
            break
        if re.match(r"^Sent from my (iPhone|iPad|Android)", stripped, re.I):
            continue
        if re.match(r"^-+\s*Original Message\s*-+$", stripped, re.I):
            break
        if re.match(r"^From:\s+", stripped):
            break
        out.append(line)
    return "\n".join(out)


def search_correction_replies():
    """Search INBOX for Jordan's unflagged replies to Alice's confirmation echo emails.

    Returns at most ONE correction reply per run — the oldest unflagged one.
    A backlog of unflagged corrections (e.g. accumulated while cron was down)
    drains one-per-cycle, not all-at-once. Without this throttle, a 9-message
    backlog batch-fires 9 echo emails + 9 telegrams in 2 minutes, which is
    indistinguishable from a live loop from Jordan's side.

    Deferred entries remain UNFLAGGED so the next run sees them. The deferred
    count is reported in the journal entry below.
    """
    try:
        from alice.jobcfg import load
        cfg = load()
        if not (cfg.get("GMAIL_USER") and cfg.get("GMAIL_APP_PASSWORD")):
            return []
        user = cfg["GMAIL_USER"]
        M, _ = _imap_open()
        try:
            M.select("INBOX")
            typ, data = M.search(
                None,
                '(FROM "%s" SUBJECT "%s" UNFLAGGED)' % (user, _CORRECTION_SUBJECT_MARKER)
            )
            if typ != "OK" or not data[0].split():
                return []
            ids = data[0].split()
            backlog_count = len(ids)
 # Process at most the oldest one. IMAP returns ids in ascending
 # order (oldest first) so ids[0] is the oldest. Defer the rest.
            results = []
            for mid in ids[:1]:
                typ2, mdata = M.fetch(mid, "(RFC822)")
                if typ2 != "OK" or not mdata or not mdata[0]:
                    continue
                msg = email.message_from_bytes(mdata[0][1])
                subj = _decode_subject(msg.get("Subject", ""))
                body = _body_text(msg)
                clean = _strip_quoted(body).strip()
                if clean:
                    results.append((mid, subj, clean))
                    M.store(mid, "+FLAGS", "\\Flagged")
            if backlog_count > 1:
                deferred = backlog_count - 1
                print(f"  [correction backlog throttle: processing 1, deferring {deferred} to next run]")
            return results
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception as e:
        print(f"[confirm_and_execute: IMAP search for corrections failed: {e}]")
        return []


# ─── echo generation (mirrors imap_reply._generate_echo) ─────────────────────

def _generate_echo_from_correction(correction_text, original_pending):
    """Call LLM to re-interpret the correction + produce updated understanding + agenda."""
    from alice.llm import llm

    original_understanding = original_pending.get("understanding", "")
    original_agenda = original_pending.get("agenda", [])
    pending_updates = original_pending.get("pending_status_updates", [])
    pending_dirs = original_pending.get("pending_directives", [])
    nl_dirs = original_pending.get("nl_directives", [])

    prompt = (
        "You are Alice, Jordan's job search agent. Jordan replied to your confirmation echo "
        "to make a correction.\n\n"
        f"ORIGINAL UNDERSTANDING:\n{original_understanding}\n\n"
        f"ORIGINAL AGENDA:\n" + "\n".join(original_agenda) + "\n\n"
        f"JORDAN'S CORRECTION:\n{correction_text[:800]}\n\n"
        "Revise your understanding and agenda to incorporate Jordan's correction. "
        "Also identify any changes to the pending actions.\n\n"
        "Respond with JSON only:\n"
        '{"understanding": "...", "agenda": ["1. ...", "2. ..."], '
        '"nl_directives": [], "status_update_overrides": [], "decisions_made": []}\n\n'
        "decisions_made: ONLY populate when you made a real choice between defensible "
        "alternatives interpreting the correction. Three trigger classes only:\n"
        "  (1) Scope expansion: revised plan touches >1 role / >1 artifact set / >1 workstream.\n"
        "  (2) Sequencing: revised plan defers Jordan's input until after LLM cost is committed.\n"
        "  (3) Defaulting on ambiguity: underspecified part of the correction resolved by "
        "choosing one plausible read.\n"
        "Each entry: {\"fork\": \"short description\", \"chose\": \"what you picked\", "
        "\"alternative\": \"the other option\", \"reason\": \"why\"}. "
        "If there was no real fork, leave []. Padding with fake choices is worse than empty."
    )

    try:
        res = llm.call("confirm_understanding", prompt, max_tokens=512)
        data = json.loads(res["text"].strip())
        return data, res["cost_usd"]
    except Exception as e:
        print(f"[confirm_and_execute: LLM echo generation failed: {e}]")
        return {
            "understanding": f"(Correction received: {correction_text[:200]})",
            "agenda": original_agenda,
            "nl_directives": nl_dirs,
            "decisions_made": [],
        }, 0.0


def _send_echo_email(understanding, agenda, created_at_str, is_correction=False,
                     decisions_made=None):
    import notify_email, notify_telegram, verify
    from alice.notify import imap_reply
    ts = created_at_str[:16].replace("T", " ")
    subject = f"Alice: confirming — {ts}"
    prefix = "Correction received. Updated understanding:\n\n" if is_correction else ""
    agenda_text = "\n".join(agenda)
    decisions_block = imap_reply._format_decisions_block(decisions_made)
    body = (
        f"Jordan,\n\n"
        f"{prefix}"
        f"{understanding}\n\n"
        f"Agenda:\n{agenda_text}\n\n"
        f"{decisions_block}"
        f"If this is right, no action needed. I'll proceed unless you correct.\n"
        f"If I got something wrong, reply to this email with corrections.\n\n"
        f"Alice"
    )
    ok = notify_email.send(subject, body)
 # Independent verification of email send: IMAP Sent-folder probe.
    if ok:
        vr = verify.verify_email_send(subject_substr=ts)
        if not vr.ok:
            print(f"  VERIFY ERROR email_send: {vr.claim}")
    try:
        tg_prefix = "Correction received. Updated understanding:\n\n" if is_correction else ""
        tg_res = notify_telegram.send_with_id(
            f"Alice: confirming\n\n{tg_prefix}{understanding}\n\nAgenda:\n{agenda_text}\n\n"
            f"{decisions_block}"
            f"No action needed if correct. I'll proceed unless you correct.\n"
            f"Reply here or via email to correct."
        )
 # Independent verification of telegram send.
        if tg_res.get("ok"):
            vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
            if not vr.ok:
                print(f"  VERIFY ERROR telegram_send: {vr.claim}")
    except Exception as _e:
        print(f"[telegram echo failed: {_e}]")
    return subject, ok


# ─── execute pending ──────────────────────────────────────────────────────────

def _release_held_observations(held_observations):
    """Write observations held during confirmation back to observations.md."""
    if not held_observations:
        return
    obs_log = Path(str(repo_paths.FEEDBACK / "observations.md"))
    obs_log.parent.mkdir(parents=True, exist_ok=True)
    for entry in held_observations:
        subj = entry.get("subject", "")
        text = entry.get("text", "").strip()
        count = entry.get("structured_count", 0)
        if not text:
            continue
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = f"\n---\n## {ts}  ·  reply to: {subj!r}"
        if count:
            header += f"  ·  ({count} status update{'s' if count != 1 else ''} also applied)"
        block = header + "\n\n" + text + "\n"
        with obs_log.open("a", encoding="utf-8") as f:
            f.write(block)
        print(f"  Released held observation ({len(text)} chars) -> {obs_log}")


def execute_pending(pending):
    """Apply all held actions from pending-confirmation.json.

    After each action group, run an INDEPENDENT verifier (item 5 / C2):
    a separate-protocol or fresh-handle check that the action actually landed.
    Verifier results journal to feedback/verify-log.jsonl. Failures append to
    the local errors list — caller decides what to do with them.
    """
    from alice.persistence import ledger
    from alice.notify import imap_reply
    from alice.ops import verify

    errors = []
    verifications = []

 # Status updates
    status_updates = pending.get("pending_status_updates", [])
    if status_updates:
        try:
            ws = ledger._ws()
            ledger.update_status_batch(
                ws,
                [(idx, canon) for idx, canon in status_updates],
                authorized=True,
                source="confirm_and_execute:jordan_confirmed_reply",
            )
            print(f"  Applied {len(status_updates)} status update(s)")
 # Independent verification: fresh-auth read-back per row
            for idx, canon in status_updates:
                vr = verify.verify_sheet_status_write(idx, canon)
                verifications.append(("sheet_write", idx, canon, vr.ok, vr.verified, vr.claim))
                if not vr.ok:
                    errors.append(f"sheet_write_verify row {idx}: {vr.claim}")
                    print(f"  VERIFY ERROR row {idx}={canon!r}: {vr.claim}")
        except Exception as e:
            errors.append(f"status_updates: {e}")
            print(f"  ERROR applying status updates: {e}")

 # Structured directives
    structured_dirs = pending.get("pending_directives", [])
    if structured_dirs:
        try:
            directive_tuples = [(d_type, payload, raw) for d_type, payload, raw in structured_dirs]
            imap_reply._apply_directives(directive_tuples)
            print(f"  Applied {len(structured_dirs)} directive(s)")
        except Exception as e:
            errors.append(f"directives: {e}")
            print(f"  ERROR applying directives: {e}")

 # Natural-language directives extracted by LLM
    nl_dirs = pending.get("nl_directives", [])
    focus_set_expected = []
    if nl_dirs:
        synthesized = []
        for d in nl_dirs:
            d_type = d.get("type")
            if not d_type:
                continue
            payload = {k: v for k, v in d.items() if k != "type"}
            raw_line = f"[nl: {d_type} {payload}]"
            synthesized.append((d_type, payload, raw_line))
            if d_type == "focus_set":
                focus_set_expected = list(payload.get("substrings", []))
        if synthesized:
            try:
                imap_reply._apply_directives(synthesized)
                print(f"  Applied {len(synthesized)} NL directive(s)")
            except Exception as e:
                errors.append(f"nl_directives: {e}")
                print(f"  ERROR applying NL directives: {e}")

 # Release held observations to observations.md so triage can pick them up
    try:
        _release_held_observations(pending.get("held_observations", []))
    except Exception as e:
        errors.append(f"held_observations: {e}")
        print(f"  ERROR releasing held observations: {e}")

 # Independent verification: if focus_set was applied, re-read focus.json
    if focus_set_expected:
        vr = verify.verify_focus_state(focus_set_expected)
        verifications.append(("focus_apply", None, focus_set_expected, vr.ok, vr.verified, vr.claim))
        if not vr.ok:
            errors.append(f"focus_apply_verify: {vr.claim}")
            print(f"  VERIFY ERROR focus_apply: {vr.claim}")

 # Independent verification: log verifications summary
    if verifications:
        ok_count = sum(1 for v in verifications if v[3])
        print(f"  Verified {ok_count}/{len(verifications)} actions via independent surface")

    return errors


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    from alice.observability import obs
    obs.init("confirm_and_execute")

    pending = load_pending()
    if not pending:
        print("[confirm_and_execute: no pending confirmation — nothing to do]")
        return

 # Fix 2a idempotency guard: never re-execute a terminal pending. The
 # state machine transitions pending -> executing -> executed exactly once.
 # If we see status='executed', another run already did the work — no-op.
 # If we see status='executing', either it crashed mid-flight (>60s stale
 # we surface loudly) or another run is in-flight (we exit silently).
    status = pending.get("status", "")
    directive_id = pending.get("directive_id", "?")
    if status == "executed":
        executed_at = pending.get("executed_at", "?")
        print(f"[confirm_and_execute: directive {directive_id[:8]} already executed at "
              f"{executed_at} — idempotent no-op]")
        return
    if status == "executing":
        executing_at = pending.get("executing_at")
        try:
            t_exec = datetime.fromisoformat(executing_at)
            age_s = (datetime.now() - t_exec).total_seconds()
        except Exception:
            age_s = 9999.0
        if age_s > 60:
 # Stuck — log loudly and refuse to retry (manual intervention).
            print(f"[confirm_and_execute: directive {directive_id[:8]} STUCK in executing "
                  f"for {age_s:.0f}s. Refusing automatic retry. Manual recovery required.]")
            try:
                from alice.observability import obs
                obs.capture_message(
                    "stuck_executing",
                    level="error",
                    where="confirm_and_execute:main",
                    extras={"directive_id": directive_id, "age_s": age_s},
                )
            except Exception:
                pass
        else:
            print(f"[confirm_and_execute: directive {directive_id[:8]} is executing "
                  f"({age_s:.0f}s) — another run in flight, no-op]")
        return
    if status != "pending":
        print(f"[confirm_and_execute: pending status={status!r} — skipping]")
        return

    created_at = datetime.fromisoformat(pending["created_at"])
    expires_at = datetime.fromisoformat(pending["expires_at"])
    now = datetime.now()
    minutes_elapsed = (now - created_at).total_seconds() / 60
    minutes_remaining = max(0, (expires_at - now).total_seconds() / 60)

    print(f"[confirm_and_execute: pending from {pending['created_at']} "
          f"({minutes_elapsed:.0f} min ago, {minutes_remaining:.0f} min remaining)]")

 # Check for correction replies
    corrections = search_correction_replies()
    if corrections:
 # Correction-logging telemetry — every correction Jordan issues is
 # measurement material for the readiness check (item 8). When
 # Sentry is wired (DSN present) this also pages.
        from alice.observability import obs
        for mid, subj, correction_text in corrections:
            obs.capture_message(
                "correction",
                level="warning",
                where="confirm_and_execute:search_correction_replies",
                extras={
                    "subject":          str(subj)[:200],
                    "correction_text":  str(correction_text)[:1000],
                    "original_understanding": pending.get("understanding", "")[:300],
                },
            )
        for mid, subj, correction_text in corrections:
            print(f"  [correction reply found: {subj[:60]}]")
            echo_data, cost = _generate_echo_from_correction(correction_text, pending)
 # Update pending with corrected understanding, reset timer.
 # Keep the same directive_id — a correction is a refinement, not a
 # new directive. The throttle in search_correction_replies() caps
 # this to 1/run so the timer cannot reset 9x in one run.
            pending["understanding"] = echo_data.get("understanding", pending["understanding"])
            pending["agenda"] = echo_data.get("agenda", pending["agenda"])
            pending["nl_directives"] = echo_data.get("nl_directives", pending.get("nl_directives", []))
            decisions = echo_data.get("decisions_made", []) or []
            pending["decisions_made"] = decisions
            from alice.notify import imap_reply as _ir
            _ir._log_decisions(decisions, source="confirm_and_execute:correction")
            pending["created_at"] = now.isoformat(timespec="seconds")
            pending["expires_at"] = (now + timedelta(minutes=_CONFIRM_WINDOW_MINUTES)).isoformat(timespec="seconds")
            pending["status"] = "pending"  # ensure correction doesn't shift state
            save_pending(pending)
            echo_subj, echo_ok = _send_echo_email(
                pending["understanding"], pending["agenda"],
                pending["created_at"], is_correction=True,
                decisions_made=decisions,
            )
            print(f"  [correction echo sent: {echo_subj}, cost ${cost:.4f}]")
            print(f"  [timer reset to {_CONFIRM_WINDOW_MINUTES} min from now]")
        return  # Don't execute yet — wait for fresh 5-minute window

 # No correction — check if window has expired
    if now >= expires_at:
        print(f"  [{_CONFIRM_WINDOW_MINUTES}-minute window elapsed, no correction — executing pending actions]")
 # Fix 2a state machine: persist 'executing' BEFORE running handlers.
 # While status==executing, _write_pending_confirmation refuses to
 # clobber this directive (60s grace window). On crash, the directive
 # is left in 'executing' state with executing_at timestamp; the next
 # run sees it and surfaces a stuck-executing signal rather than
 # silently retrying or losing it.
        mark_executing(pending)
        errors = execute_pending(pending)
        mark_executed(pending)
 # Independent verification: re-read pending-confirmation.json from a fresh
 # file handle and confirm we did actually flip status to executed.
        from alice.ops import verify
        vr = verify.verify_pending_executed()
        if not vr.ok:
            print(f"  VERIFY ERROR pending_executed: {vr.claim}")
            errors.append(f"pending_executed_verify: {vr.claim}")
        if errors:
            print(f"  [execute completed with {len(errors)} error(s): {errors}]")
        else:
            print(f"  [execute complete — all actions applied + verified]")
    else:
        print(f"  [window still open ({minutes_remaining:.0f} min remaining) — waiting for correction or timeout]")


if __name__ == "__main__":
    main()
