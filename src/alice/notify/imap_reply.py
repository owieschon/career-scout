"""IMAP reply parser — read user replies to digest emails and update sheet statuses.

Trigger: runs BEFORE the daily delta in run_daily.sh so today's run uses today's feedback.
Auth: same Gmail App Password as notify_email.py (config.env: GMAIL_USER, GMAIL_APP_PASSWORD).
Idempotency: uses IMAP `\\Flagged` to mark processed messages. Re-runs skip flagged messages.

Reply syntax (permissive, mobile-friendly):
    northwind enterprise: good fit
    boreal flowcad submitted
    not a fit: openai growth cross channel
    materials pending: watershed

Matching rules:
  - Status aliases recognized for: good fit | not a fit | materials pending | submitted | closed
  - Company+role substring matched case-insensitive against "company role" string per sheet row
  - 0 matches  -> logged as no-match (won't apply)
  - 1 match    -> status applied
  - 2+ matches -> logged as ambiguous (won't apply, prevents accidental wrong updates)
"""
import email
import imaplib
import json
import re
import ssl
from datetime import datetime
from email.header import decode_header
from email.utils import parseaddr

from alice.jobcfg import load
from alice import safe_state
from alice import repo_paths

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# Status aliases — left side is canonical (matches ledger TERMINAL/dropdown values).
# Aliases that are common English words ("no", "yes", "sent", "done") are
# only recognized when they sit at the START or END of the line, or
# immediately adjacent to a ':' / '=' separator. The intent-style aliases
# ("good fit", "not a fit", "materials pending") can match anywhere because
# they're unambiguous as phrases.
STATUS_ALIASES = {
    "good fit": ["good fit", "great fit", "strong fit", "good", "yes", "fit", "interested", "like", "love"],
    "not a fit": ["not a fit", "not fit", "no", "pass", "skip", "reject", "nope", "drop", "delete", "bad"],
    "materials pending": ["materials pending", "pending", "drafting", "materials", "wip", "working", "draft", "tailoring"],
    "submitted": ["submitted", "applied", "sent", "done", "app sent"],
    "closed": ["closed", "withdrew", "rejected", "ghosted", "dead"],
}

# Aliases that are ONLY recognized in disciplined positions (start/end of line or
# adjacent to a delimiter). Prevents false positives on plain prose.
_RESTRICTED_ALIASES = {"good", "yes", "fit", "no", "pass", "skip", "reject", "bad", "nope",
                       "drop", "delete", "pending", "draft", "wip", "working", "sent",
                       "done", "dead", "like", "love", "materials"}

_ALIAS_TO_CANONICAL = []
for canon, aliases in STATUS_ALIASES.items():
    for a in aliases:
        _ALIAS_TO_CANONICAL.append((a, canon))
_ALIAS_TO_CANONICAL.sort(key=lambda x: -len(x[0]))

# Common role-name abbreviations the user might type in replies. Mapped to the
# fuller phrases that appear in sheet rows so token-matching can expand them.
_ROLE_ABBREVS = {
    "csm": ["customer success manager"],
    "ae": ["account executive"],
    "se": ["solutions engineer", "sales engineer"],
    "sa": ["solutions architect"],
    "tam": ["technical account manager"],
    "fde": ["forward deployed engineer", "forward deployed"],
    "pm": ["product manager"],
    "revops": ["revenue operations", "rev ops"],
    "rev": ["revenue"],
}


def available():
    cfg = load()
    return bool(cfg.get("GMAIL_USER") and cfg.get("GMAIL_APP_PASSWORD"))


def _strip_quoted(text):
    """Drop quoted reply lines (prefixed with '>') and everything after 'On ... wrote:' boilerplate."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
 # iOS / Gmail boilerplate markers
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


def _parse_line(line):
    """Return (canonical_status, company_role_substring) or (None, None) if line has no actionable update.

    Strategy:
      1. Try aliases longest-first.
      2. For RESTRICTED aliases (single common-English words), require disciplined
         position: anchored to start or end of line, OR adjacent to ':' or '='.
         This prevents 'no' matching in 'with no status' or 'sent' in 'Sent from my iPhone'.
      3. Phrase-form aliases ('good fit', 'not a fit') match anywhere.
      4. Remainder of the line after stripping the alias = company-role substring.
      5. Require substring length >= 3 to count as actionable.
    """
    raw = line.strip()
    if not raw:
        return None, None
    low = raw.lower()
    for alias, canon in _ALIAS_TO_CANONICAL:
        if alias in _RESTRICTED_ALIASES:
 # disciplined positions only:
 # "alias: rest" "alias = rest" "alias rest" (start)
 # "rest: alias" "rest = alias" "rest alias" (end)
            patterns = [
                r"^\s*" + re.escape(alias) + r"\s*[:=]\s*(.+)$",      # alias:rest
                r"^(.+?)\s*[:=]\s*" + re.escape(alias) + r"\s*$",     # rest:alias
                r"^\s*" + re.escape(alias) + r"\s+(.+)$",             # alias rest
                r"^(.+?)\s+" + re.escape(alias) + r"\s*$",            # rest alias
            ]
            for pat in patterns:
                m = re.match(pat, low, re.I)
                if m:
                    substr = m.group(1).strip()
                    substr = re.sub(r"^[:\-=\s]+|[:\-=\s]+$", "", substr).strip()
                    substr = re.sub(r"\s+", " ", substr)
                    if len(substr) < 3:
                        return None, None
 # remap substring to original case (best effort)
                    orig_start = low.find(substr.lower())
                    if orig_start >= 0:
                        substr = raw[orig_start:orig_start + len(substr)]
                    return canon, substr
            continue
 # phrase-form alias: match anywhere (word-boundary-ish)
        pat = re.compile(r"(?:^|[^a-z])" + re.escape(alias) + r"(?:$|[^a-z])", re.I)
        m = pat.search(low)
        if m:
            substr = (raw[:m.start()] + " " + raw[m.end():]).strip()
            substr = re.sub(r"^[:\-=\s]+|[:\-=\s]+$", "", substr).strip()
            substr = re.sub(r"\s+", " ", substr)
            if len(substr) < 3:
                return None, None
            return canon, substr
    return None, None


def _match_sheet_row(substr, rows):
    """Token-based AND-match: every token in substr must be present (literal OR via
    role-name abbreviation expansion) in the row's company+role haystack.
    Handles 'boreal flowcad' against 'Boreal CAD Strategic Account Executive, FlowCAD' (non-contiguous)
    and 'redis csm' against 'Redis (VC) Customer Success Manager' (csm -> customer success manager)."""
    tokens = [t for t in re.split(r"[\s,]+", substr.lower()) if t]
    if not tokens:
        return []
    hits = []
    for i, r in enumerate(rows, start=2):  # data starts at row 2
        hay = ((r.get("company", "") or "") + " " + (r.get("role", "") or "")).lower()
 # strip "(YC)" / "(VC)" / "(auto)" suffixes from haystack so user doesn't
 # have to type them
        hay = re.sub(r"\((?:yc|vc|auto)\)", "", hay)
        all_present = True
        for t in tokens:
            if t in hay:
                continue
 # try abbreviation expansions: any expansion present satisfies the token
            if t in _ROLE_ABBREVS and any(exp in hay for exp in _ROLE_ABBREVS[t]):
                continue
            all_present = False
            break
        if all_present:
            hits.append((i, r))
    return hits


def _apply_set_intent(payload):
    """Apply a `set_intent` directive: write the Sheet intent column for the
    matched role. Un-gated (the operator's own state declaration — nothing to authorize).
    0/1/many resolution: surface candidates on ambiguity, never auto-pick."""
    from alice.persistence import ledger
    intent = (payload.get("intent") or "").strip().lower()
    substr = payload.get("substr") or ""
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(substr, rows)
    if not hits:
        return {"error": f"no match for {substr!r}", "intent": intent}
    if len(hits) > 1:
        return {"error": f"ambiguous ({len(hits)} matches): {substr!r}", "intent": intent,
                "candidates": [(i, r.get("company", ""), r.get("role", "")) for i, r in hits[:5]]}
    i, r = hits[0]
    ledger.update_intent(ws, i, intent, source="reply.set_intent")
    return {"set": {"row_idx": i, "company": r.get("company", ""),
                    "role": r.get("role", ""), "intent": intent}}


def _imap_open():
    cfg = load()
    user = cfg["GMAIL_USER"]; pw = cfg["GMAIL_APP_PASSWORD"].replace(" ", "")
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=_SSL_CTX)
    M.login(user, pw)
    return M, user


def _decode_subject(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(txt.decode("utf-8", errors="replace"))
        else:
            out.append(txt)
    return "".join(out)


def _body_text(msg):
    """Extract text/plain body (prefer plain over html)."""
    if msg.is_multipart():
 # collect text/plain first; fall back to text/html-stripped
        plain = []
        html_fallback = []
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
            elif ctype == "text/html":
                html_fallback.append(re.sub(r"<[^>]+>", " ", txt))
        return "\n".join(plain) if plain else "\n".join(html_fallback)
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


_FEEDBACK_LOG = str(repo_paths.FEEDBACK / "observations.md")
_PREP_QUEUE = str(repo_paths.FEEDBACK / "prep-queue.json")
_HYPOTHESES = str(repo_paths.FEEDBACK / "hypotheses.md")
_DIGEST_PREFS = str(repo_paths.FEEDBACK / "digest-prefs.json")
_PROPOSALS_DIR = str(repo_paths.FEEDBACK / "proposed")
_APPLIED_DIR = str(repo_paths.FEEDBACK / "applied")
_PENDING_CONF = str(repo_paths.FEEDBACK / "pending-confirmation.json")


_UNKNOWN_DIRECTIVES_LOG = str(repo_paths.FEEDBACK / "unknown-directives.jsonl")

# Canonical handler registry — used both for dispatch and for the
# "unrecognized directive" fail-loud check. Fix 3: anything not in this
# set is fail-CLOSED (visible failure + the operator notification + journal entry),
# not fail-OPEN (silent no-op as it was before).
_KNOWN_DIRECTIVE_TYPES = {
    "focus_set", "focus_add", "focus_drop", "focus_clear", "focus_show",
    "prep", "prep_now", "prep_order", "prep_stop",
    "screen_scheduled",
    "debrief_request", "debrief_now", "debrief_answer", "question_answer",
    "hypothesis", "approve", "reject", "revert",
    "digest_pause", "digest_lighter", "digest_resume",
    "help_with", "revise", "drop_role", "outreach_response",
    "stop_all_pending",
    "set_intent",  # Move 1B: the operator declares their state about a role (un-gated)
}


def _journal_unknown_directive(d_type, payload, raw_line):
    """Log an unrecognized directive type for the audit trail. The 2026-05-28
    incident — stop_all_pending silently no-op'd because no handler existed —
    would now surface here AND notify the operator.
    """
    import os
    try:
        os.makedirs(os.path.dirname(_UNKNOWN_DIRECTIVES_LOG), exist_ok=True)
        record = {
            "ts":       datetime.now().isoformat(timespec="seconds"),
            "d_type":   str(d_type)[:80],
            "payload":  payload,
            "raw_line": str(raw_line)[:400],
            "known_types": sorted(_KNOWN_DIRECTIVE_TYPES),
        }
        with open(_UNKNOWN_DIRECTIVES_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[unknown-directives log failed: {e}]")


def _notify_unknown_directive(d_type, payload):
    """Fire a Telegram + email notification to the operator that an unknown directive
    type was attempted. Best-effort — if either channel fails, the journal
    entry above is still made.
    """
    try:
        import notify_telegram, notify_email, verify
        msg = (
            f"Alice: UNKNOWN DIRECTIVE TYPE\n\n"
            f"Type: {d_type!r}\n"
            f"Payload: {payload!r}\n\n"
            f"No handler in _apply_directives. Logged to "
            f"feedback/unknown-directives.jsonl. NO ACTION TAKEN. "
            f"Add a handler in imap_reply._apply_directives if this directive "
            f"is real, or fix the source so it stops emitting unknown types."
        )
        try:
            tg_res = notify_telegram.send_with_id(msg)
            if tg_res.get("ok"):
                vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
                if not vr.ok:
                    print(f"  VERIFY ERROR telegram_send (unknown_directive): {vr.claim}")
        except Exception as e:
            print(f"  [unknown-directive telegram failed: {e}]")
        try:
            subj = f"Alice: UNKNOWN DIRECTIVE TYPE — {d_type!r}"
            ok = notify_email.send(subj, msg)
            if ok:
                vr = verify.verify_email_send(subject_substr=subj[:60])
                if not vr.ok:
                    print(f"  VERIFY ERROR email_send (unknown_directive): {vr.claim}")
        except Exception as e:
            print(f"  [unknown-directive email failed: {e}]")
    except Exception as e:
        print(f"  [unknown-directive notify failed entirely: {e}]")


def _handle_stop_all_pending():
    """Clear the active pending and drain prep-queue of non-completed entries.
    The directive is named stop_all_pending; the meaning is: cancel any
    in-flight work that hasn't started executing yet.
    """
    cancelled = {"pending_cleared": False, "prep_entries_cancelled": 0}

 # 1. Mark current pending as 'cancelled' (don't delete — keep audit trail).
    def pending_mutator(cur):
        if not cur:
            return cur, False
        cur_status = (cur.get("status") or "").lower()
        if cur_status != "pending":
            return cur, False
        _archive_superseded(
            cur,
            new_directive_id="(stop_all_pending)",
            reason="cancelled_by_stop_all_pending",
        )
        cur["status"] = "cancelled"
        cur["cancelled_at"] = datetime.now().isoformat(timespec="seconds")
        return cur, True

    try:
        cancelled["pending_cleared"] = safe_state.atomic_update(
            _PENDING_CONF, pending_mutator, default=None,
            skip_write_if_unchanged=True,
        )
    except Exception as e:
        print(f"  [stop_all_pending: error clearing pending: {e}]")

 # 2. Drain prep-queue.json of non-completed entries (status='pending').
    def queue_mutator(q):
        if q is None:
            return q, 0
        new_q = []
        cancelled_n = 0
        for entry in q:
            if entry.get("status") == "pending":
                entry["status"] = "cancelled_by_stop_all_pending"
                entry["cancelled_at"] = datetime.now().isoformat(timespec="seconds")
                cancelled_n += 1
            new_q.append(entry)
        return new_q, cancelled_n

    try:
        cancelled["prep_entries_cancelled"] = safe_state.atomic_update(
            _PREP_QUEUE, queue_mutator, default=None,
            skip_write_if_unchanged=True,
        )
    except Exception as e:
        print(f"  [stop_all_pending: error draining prep-queue: {e}]")

    return cancelled


def _apply_directives(directives):
    """Dispatch each directive to its handler. Returns dict of {directive_type: count_or_result}.

    Most directives are stateful (modify focus.json, prep-queue.json, hypotheses.md, etc.).
    LLM-using directives (like outreach_response which feeds pattern tracking) just log;
    the LLM-using behaviors handle them in their own scripts.

    Fix 3: directives whose type is not in _KNOWN_DIRECTIVE_TYPES are
    treated as failures. They are journaled to feedback/unknown-directives.jsonl
    AND the operator is notified via telegram + email. No silent pass-through.
    """
    import json, os
    from datetime import datetime
    from alice.persistence import focus as focus_mod

    results = {
        "focus_changes": [],
        "prep_queued": [],
        "screen_scheduled": [],
        "debrief_requests": [],
        "debrief_answers": [],
        "question_answers": [],
        "hypotheses_added": [],
        "approvals": [],
        "rejections": [],
        "reverts": [],
        "digest_prefs": [],
        "outreach_responses": [],
        "stops": [],
        "unknown": [],
        "errors": [],
    }

    for d_type, payload, raw_line in directives:
        try:
            if d_type == "focus_set":
                r = focus_mod.set_focus(payload["substrings"], actor="operator")
                results["focus_changes"].append({"action": "set", "result": r})
                print(f"  applied focus_set: {len(r.get('set',[]))} roles set, {len(r.get('not_found',[]))} not found, {len(r.get('ambiguous',[]))} ambiguous")

            elif d_type == "focus_add":
                r = focus_mod.add(payload["substr"], actor="operator")
                results["focus_changes"].append({"action": "add", "result": r})
                print(f"  applied focus_add: {r}")

            elif d_type == "focus_drop":
                r = focus_mod.drop(payload["substr"], actor="operator")
                results["focus_changes"].append({"action": "drop", "result": r})
                print(f"  applied focus_drop: {r}")

            elif d_type == "focus_clear":
                focus_mod.clear(actor="operator")
                results["focus_changes"].append({"action": "clear"})
                print(f"  applied focus_clear")

            elif d_type == "set_intent":
                r = _apply_set_intent(payload)
                results.setdefault("intent_changes", []).append(r)
                print(f"  applied set_intent: {r}")

            elif d_type == "focus_show":
 # Just sets a flag for next digest to expand focus block
                _set_digest_pref("expand_focus_next", True)
                results["digest_prefs"].append({"key": "expand_focus_next", "value": True})

            elif d_type in ("prep", "prep_now"):
                _enqueue_prep([payload["substr"]], rush=(d_type == "prep_now"))
                results["prep_queued"].append({"substr": payload["substr"], "rush": d_type == "prep_now"})

            elif d_type == "prep_order":
                _enqueue_prep(payload["substrings"], rush=False)
                results["prep_queued"].extend([{"substr": s, "rush": False} for s in payload["substrings"]])

            elif d_type == "prep_stop":
                _dequeue_prep(payload["substr"])
                results["prep_queued"].append({"substr": payload["substr"], "action": "stop"})

            elif d_type == "screen_scheduled":
 # Store the schedule + update sheet status. Alice's interview_prep
 # cron picks this up and produces the prep package.
                _record_screen_scheduled(payload)
                results["screen_scheduled"].append(payload)
                print(f"  recorded screen scheduled: {payload}")

            elif d_type in ("debrief_request", "debrief_now"):
                _enqueue_debrief(payload)
                results["debrief_requests"].append(payload)

            elif d_type == "debrief_answer":
                _record_debrief_answer(payload)
                results["debrief_answers"].append(payload)

            elif d_type == "question_answer":
                _record_question_answer(payload)
                results["question_answers"].append(payload)

            elif d_type == "hypothesis":
                _append_hypothesis(payload["text"])
                results["hypotheses_added"].append(payload["text"])

            elif d_type == "approve":
                r = _approve_proposal(payload["id"])
                results["approvals"].append(r)

            elif d_type == "reject":
                r = _reject_proposal(payload["id"], payload.get("reason"))
                results["rejections"].append(r)

            elif d_type == "revert":
                r = _revert_apply(payload["id"])
                results["reverts"].append(r)

            elif d_type == "digest_pause":
                _set_digest_pref("paused", True)
                results["digest_prefs"].append({"key": "paused", "value": True})

            elif d_type == "digest_lighter":
                _set_digest_pref("lighter", True)
                results["digest_prefs"].append({"key": "lighter", "value": True})

            elif d_type == "digest_resume":
                _set_digest_pref("paused", False)
                _set_digest_pref("lighter", False)
                results["digest_prefs"].append({"key": "paused", "value": False})

            elif d_type == "help_with":
 # Queue an Alice-response request for next digest; Alice produces a thread
                _enqueue_help_request(payload["substr"])
                results["digest_prefs"].append({"key": "help_request", "value": payload["substr"]})

            elif d_type == "revise":
                _enqueue_revise(payload["substr"])
                results["digest_prefs"].append({"key": "revise_request", "value": payload["substr"]})

            elif d_type == "drop_role":
 # Drop a role from focus AND mark sheet status as "not a fit"
                r = focus_mod.drop(payload["substr"], actor="operator")
                results["focus_changes"].append({"action": "drop_role", "focus_result": r})
 # Also update sheet status
                _drop_role_status(payload["substr"])

            elif d_type == "outreach_response":
                _record_outreach_response(payload)
                results["outreach_responses"].append(payload)

            elif d_type == "stop_all_pending":
                r = _handle_stop_all_pending()
                results["stops"].append(r)
                print(f"  applied stop_all_pending: pending_cleared={r['pending_cleared']}, "
                      f"prep_entries_cancelled={r['prep_entries_cancelled']}")

            else:
 # Fix 3: fail-loud on unrecognized directive type.
 # Silent no-op (the prior behavior) is fail-OPEN: it looks like
 # success from outside but no work was done. Fail-CLOSED here:
 # log + notify the operator + record the miss in results.
                _journal_unknown_directive(d_type, payload, raw_line)
                _notify_unknown_directive(d_type, payload)
                results["unknown"].append({
                    "d_type":  d_type,
                    "payload": payload,
                    "raw":     raw_line,
                })
                print(f"  UNKNOWN DIRECTIVE TYPE: {d_type!r} — no handler, NO ACTION TAKEN. "
                      f"Logged + the operator notified.")

        except Exception as e:
            results["errors"].append({"directive": d_type, "error": str(e), "raw": raw_line})
            print(f"  ERROR applying {d_type}: {e}")

    return results


# --- directive handlers ---

def _enqueue_prep(substrings, rush=False):
    """Append substrings to prep queue (FIFO). Atomic under prep-queue lock."""
    from datetime import datetime
    ts = datetime.now().isoformat(timespec="seconds")
    new_entries = [
        {"substr": s, "queued_at": ts, "rush": rush, "status": "pending"}
        for s in substrings
    ]

    def mutator(queue):
        queue = queue or []
        queue.extend(new_entries)
        return queue, None

    safe_state.atomic_update(_PREP_QUEUE, mutator, default=[])


def _dequeue_prep(substr):
    """Remove first matching pending entry. Atomic under prep-queue lock."""
    needle = substr.lower()

    def mutator(queue):
        if not queue:
            return queue, False
        out = []
        removed = False
        for entry in queue:
            if (not removed
                and entry.get("status") == "pending"
                and needle in entry.get("substr", "").lower()):
                removed = True
                continue
            out.append(entry)
        return out, removed

    safe_state.atomic_update(_PREP_QUEUE, mutator, default=None,
                             skip_write_if_unchanged=True)


def _record_screen_scheduled(payload):
    """Set sheet status to 'first screen scheduled' + store schedule details."""
    import json, os
    from datetime import datetime
    sched_path = str(repo_paths.FEEDBACK / "scheduled-screens.json")
    os.makedirs(os.path.dirname(sched_path), exist_ok=True)
    schedules = []
    if os.path.exists(sched_path):
        with open(sched_path) as f:
            schedules = json.load(f)
    entry = {
        "substr":          payload.get("substr", ""),
        "when":            payload.get("when", ""),
        "interviewers":    payload.get("interviewers", ""),
        "scheduled_at":    datetime.now().isoformat(timespec="seconds"),
        "prep_generated":  False,
    }
    schedules.append(entry)
    with open(sched_path, "w") as f:
        json.dump(schedules, f, indent=2)
 # Update sheet status
    from alice.persistence import ledger
    from alice.ops import verify
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(payload.get("substr", ""), rows)
    if len(hits) == 1:
        i, r = hits[0]
        ledger.update_status(
            ws, i, "first screen scheduled",
            authorized=True,
            source="imap_reply:operator_screen_scheduled_directive",
        )
 # C2 verifier: fresh-auth read-back of column G.
        vr = verify.verify_sheet_status_write(i, "first screen scheduled")
        if not vr.ok:
            print(f"  VERIFY ERROR sheet_write row {i}: {vr.claim}")


def _enqueue_debrief(payload):
    """Queue a debrief request — Alice's debrief script picks it up next cycle."""
    import json, os
    from datetime import datetime
    q_path = str(repo_paths.FEEDBACK / "debrief-queue.json")
    os.makedirs(os.path.dirname(q_path), exist_ok=True)
    queue = []
    if os.path.exists(q_path):
        with open(q_path) as f:
            queue = json.load(f)
    queue.append({**payload, "queued_at": datetime.now().isoformat(timespec="seconds"), "status": "pending"})
    with open(q_path, "w") as f:
        json.dump(queue, f, indent=2)


def _record_debrief_answer(payload):
    """Store the operator's debrief answer — Alice's debrief script integrates."""
    import json, os
    from datetime import datetime
    a_path = str(repo_paths.FEEDBACK / "debrief-answers.jsonl")
    os.makedirs(os.path.dirname(a_path), exist_ok=True)
    with open(a_path, "a") as f:
        f.write(json.dumps({**payload, "received_at": datetime.now().isoformat(timespec="seconds")}) + "\n")


def _record_question_answer(payload):
    """Store the operator's answer to a targeted-question prompt."""
    import json, os
    from datetime import datetime
    a_path = str(repo_paths.FEEDBACK / "question-answers.jsonl")
    os.makedirs(os.path.dirname(a_path), exist_ok=True)
    with open(a_path, "a") as f:
        f.write(json.dumps({**payload, "received_at": datetime.now().isoformat(timespec="seconds")}) + "\n")


def _append_hypothesis(text):
    """Append a user-raised hypothesis to the registry."""
    import os
    from datetime import datetime
    os.makedirs(os.path.dirname(_HYPOTHESES), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d")
    block = f"\n### OPERATOR-RAISED ({ts})\n{text}\n  status: pending Alice classification\n"
    with open(_HYPOTHESES, "a") as f:
        f.write(block)


def _approve_proposal(prop_id):
    """Move proposal from proposed/ → applied/ and execute its patch."""
    import os, shutil
    from datetime import datetime
    proposed = os.path.join(_PROPOSALS_DIR, f"{prop_id}.md")
    if not os.path.exists(proposed):
        return {"id": prop_id, "error": "proposal not found"}
    os.makedirs(_APPLIED_DIR, exist_ok=True)
    applied = os.path.join(_APPLIED_DIR, f"{prop_id}-applied-{datetime.now().strftime('%Y-%m-%d')}.md")
    shutil.move(proposed, applied)
    return {"id": prop_id, "moved_to": applied, "note": "patch execution handled by Alice's apply script"}


def _reject_proposal(prop_id, reason=None):
    """Move proposal to rejected/ with reason annotation."""
    import os, shutil
    from datetime import datetime
    proposed = os.path.join(_PROPOSALS_DIR, f"{prop_id}.md")
    if not os.path.exists(proposed):
        return {"id": prop_id, "error": "proposal not found"}
    rej_dir = str(repo_paths.FEEDBACK / "rejected")
    os.makedirs(rej_dir, exist_ok=True)
    rejected = os.path.join(rej_dir, f"{prop_id}-rejected-{datetime.now().strftime('%Y-%m-%d')}.md")
    shutil.move(proposed, rejected)
    if reason:
        with open(rejected, "a") as f:
            f.write(f"\n\n## Rejection reason\n{reason}\n")
    return {"id": prop_id, "moved_to": rejected, "reason": reason}


def _revert_apply(apply_id):
    """Mark an applied patch for revert — Alice's revert script handles."""
    import json, os
    from datetime import datetime
    revert_q = str(repo_paths.FEEDBACK / "revert-queue.json")
    os.makedirs(os.path.dirname(revert_q), exist_ok=True)
    queue = []
    if os.path.exists(revert_q):
        with open(revert_q) as f:
            queue = json.load(f)
    queue.append({"apply_id": apply_id, "requested_at": datetime.now().isoformat(timespec="seconds")})
    with open(revert_q, "w") as f:
        json.dump(queue, f, indent=2)
    return {"id": apply_id, "queued_for_revert": True}


def _set_digest_pref(key, value):
    def mutator(prefs):
        prefs = prefs or {}
        prefs[key] = value
        return prefs, None
    safe_state.atomic_update(_DIGEST_PREFS, mutator, default={})


def _enqueue_help_request(substr):
    """Queue a help-with request — Alice responds in next digest thread."""
    import json, os
    from datetime import datetime
    q_path = str(repo_paths.FEEDBACK / "help-requests.jsonl")
    os.makedirs(os.path.dirname(q_path), exist_ok=True)
    with open(q_path, "a") as f:
        f.write(json.dumps({"substr": substr, "requested_at": datetime.now().isoformat(timespec="seconds"),
                            "status": "pending"}) + "\n")


def _enqueue_revise(substr):
    """Queue a revise request — Alice's prep_materials script re-runs."""
    import json, os
    from datetime import datetime
    q_path = str(repo_paths.FEEDBACK / "revise-queue.jsonl")
    os.makedirs(os.path.dirname(q_path), exist_ok=True)
    with open(q_path, "a") as f:
        f.write(json.dumps({"substr": substr, "requested_at": datetime.now().isoformat(timespec="seconds")}) + "\n")


def _drop_role_status(substr):
    """Mark a role as 'not a fit' in the sheet (used by 'drop <substring>' shortcut)."""
    from alice.persistence import ledger
    from alice.ops import verify
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(substr, rows)
    if len(hits) == 1:
        i, r = hits[0]
        ledger.update_status(
            ws, i, "not a fit",
            authorized=True,
            source="imap_reply:operator_drop_directive",
        )
 # C2 verifier: fresh-auth read-back of column G.
        vr = verify.verify_sheet_status_write(i, "not a fit")
        if not vr.ok:
            print(f"  VERIFY ERROR sheet_write row {i}: {vr.claim}")


def _record_outreach_response(payload):
    """Log outreach response for pattern tracking."""
    import json, os
    from datetime import datetime
    r_path = str(repo_paths.FEEDBACK / "outreach-responses.jsonl")
    os.makedirs(os.path.dirname(r_path), exist_ok=True)
    with open(r_path, "a") as f:
        f.write(json.dumps({**payload, "logged_at": datetime.now().isoformat(timespec="seconds")}) + "\n")


def _append_observation(subject, residual_text, structured_count):
    """Append unstructured observational feedback (anything that wasn't a status command)
    to feedback/observations.md. Daily digest surfaces unread observations the next morning."""
    import os
    from datetime import datetime
    os.makedirs(os.path.dirname(_FEEDBACK_LOG), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"\n---\n## {ts}  ·  reply to: {subject!r}"
    if structured_count:
        header += f"  ·  ({structured_count} status update{'s' if structured_count != 1 else ''} also applied)"
    block = header + "\n\n" + residual_text.strip() + "\n"
    with open(_FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(block)


def _generate_echo(reply_text, updates, directives, focus_roles):
    """Call LLM to produce: understanding summary, ordered agenda, and any NL directives
    not captured by the structured parser (e.g. 'prioritize X, begin prep for Y').

    Uses the same soul + grounding invariants + agency directive as the chat
    path, so it's one Alice across surfaces. Wires tools=tools.tool_specs()
    + tool_executor=tools.dispatch so the email path can also reach for the
    sheet / focus state / files when grounding her understanding.
    """
    from alice.llm import llm
    from alice import tools as alice_tools

    updates_str = "\n".join(
        f"  - {company} | {role[:40]} -> {canon}"
        for (_, canon, _, company, role) in updates
    ) or "  (none)"

    dirs_str = "\n".join(
        f"  [{d_type}] {payload}"
        for (d_type, payload, _) in directives
    ) or "  (none)"

    focus_str = ", ".join(
        f"{r['company']} {r['role']}"
        for r in focus_roles
    ) or "(empty)"

 # Same agency directive as the chat path.
    agency_directive = (
        "\nHOW TO ACT:\n"
        "When you need to ground your understanding in something concrete, "
        "use your tools — read_sheet, read_focus_state, read_pending_state, "
        "read_file. Use multiple tools if one isn't enough. Act on your best "
        "grounded judgment for reversible work. Ask only when genuinely "
        "blocked, when the action is irreversible or external, or when the "
        "alternatives are materially different and only the operator can choose."
    )

 # Same strengthened HARD INVARIANT as the chat path. Tool actions count
 # as receipts; prior turns and intuition do not.
    state_grounding_invariant = (
        "\nHARD INVARIANT — STATE & ACTION GROUNDING:\n"
        "Every claim you make about the operator's pipeline (focus list, role statuses, "
        "counts, what's queued, what's submitted) MUST be backed by something "
        "you can point to in this turn — either the context provided in this "
        "prompt or a tool result returned in this turn.\n"
        "Every action you claim to have taken ('I read', 'I wrote', 'I set focus', "
        "'I queued prep', 'I sent') MUST be backed by an actual tool call you "
        "performed in this turn that returned success. Do not narrate a read you "
        "did not perform. Do not claim an action succeeded when the tool returned "
        "an error or you didn't call the tool at all."
    )

    prompt = (
        "The operator has replied to your digest email. Process their reply and produce "
        "an echo (understanding + agenda) so they can confirm or correct.\n\n"
        f"Their reply (quoted sections stripped):\n---\n{reply_text[:1200]}\n---\n\n"
        f"Structured updates I parsed:\n{updates_str}\n\n"
        f"Directives I parsed:\n{dirs_str}\n\n"
        f"Current focus list: {focus_str}\n"
        f"{agency_directive}"
        f"{state_grounding_invariant}"
        "\n\n"
        "YOUR TASK — respond with JSON only, no markdown fences:\n"
        '{"understanding": "2-4 sentence plain English summary of what the operator is asking",'
        ' "agenda": ["1. First action", "2. Second action"],'
        ' "nl_directives": []}\n\n'
        "Rules:\n"
        "- understanding: what the operator wants, stated plainly. No em dashes.\n"
        "- agenda: ordered list of what you will do. Be specific. Use company/role names.\n"
        "- nl_directives: include explicit actionable commands not already captured by "
        "structured parsing (e.g. 'prioritize X and Y' -> focus_set, 'begin prep for both' "
        "-> prep_order).\n"
        "  Allowed types: focus_set(substrings=[...]), focus_add(substr=''), "
        "focus_drop(substr=''), prep(substr=''), prep_order(substrings=[...]).\n"
    )

 # Load Alice's full brief (soul + operational + knowledge index) — same
 # as the chat path uses. F1 unification: one Alice across surfaces.
    system = llm.load_alice_brief()

    try:
        res = llm.call(
            "confirm_understanding",
            prompt,
            system=system,
            max_tokens=1500,
            tools=alice_tools.tool_specs(),
            tool_executor=alice_tools.dispatch,
        )
        text = res["text"].strip()
 # Robust JSON extraction (same discipline as the chat parser):
 # locate the first '{', use JSONDecoder().raw_decode() which parses
 # the JSON object and ignores anything after — tolerates leading
 # fences, trailing fences, and trailing prose.
        start = text.find("{")
        if start < 0:
            raise ValueError(f"no JSON object found in echo response (len={len(text)})")
        try:
            data, _end = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
 # Fallback: strip fences the old way and try again
            text2 = text
            if text2.startswith("```"):
                text2 = re.sub(r"^```[a-z]*\n?", "", text2)
                text2 = re.sub(r"\n?```\s*.*$", "", text2, flags=re.S)
            data = json.loads(text2)
        return data, res["cost_usd"]
    except Exception as e:
 # Fallback: template-based echo, no NL extraction
        agenda = []
        if updates:
            agenda.append(f"Apply {len(updates)} status update(s) to the sheet")
        if directives:
            agenda.append(f"Execute {len(directives)} directive(s) (focus, prep, etc.)")
        if not agenda:
            agenda = ["Log your reply and triage in next digest"]
        return {
            "understanding": "I received your reply and will apply the following updates.",
            "agenda": agenda,
            "nl_directives": [],
            "decisions_made": [],
        }, 0.0


_DECISION_FORKS_LOG = str(repo_paths.FEEDBACK / "decision-forks.jsonl")


def _log_decisions(decisions, source):
    """Append every non-empty decisions_made list to feedback/decision-forks.jsonl
    for calibration tracking. Each fork becomes its own line so it can be counted
    per source / per class. Silent on failure — logging must not break echo flow.
    """
    if not decisions:
        return
    try:
        import os as _os
        _os.makedirs(_os.path.dirname(_DECISION_FORKS_LOG), exist_ok=True)
        with open(_DECISION_FORKS_LOG, "a") as f:
            ts = datetime.now().isoformat(timespec="seconds")
            for d in decisions:
                if not isinstance(d, dict):
                    continue
                rec = {
                    "ts":          ts,
                    "source":      source,
                    "fork":        str(d.get("fork", ""))[:300],
                    "chose":       str(d.get("chose", ""))[:300],
                    "alternative": str(d.get("alternative", ""))[:300],
                    "reason":      str(d.get("reason", ""))[:500],
                }
                f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[decisions log failed: {e}]")


def _format_decisions_block(decisions):
    """P2 (2026-05-28): retired. Returns empty string unconditionally.

    The block was LLM-narrated fiction at response time — not a record of real
    code-level decisions (the LLM produced contradictory "Parallel" vs
    "Serialize" for the same directive, proving fabrication). Until real
    code-level decision tracking exists (the code that makes an ambiguous
    choice records what it chose and why), this surface is presenting fiction
    for HITL approval, which is worse than presenting nothing.

    Signature kept so call sites can be retired separately without breaking.
    """
    return ""


_SUPERSEDED_LOG = str(repo_paths.FEEDBACK / "superseded-directives.jsonl")
_EXECUTED_GRACE_SECONDS = 60


def _archive_superseded(prior, new_directive_id, reason):
    """Log a directive being overwritten before it terminated. The full content
    of the prior directive is preserved so a human (or audit) can reconstruct
    what was clobbered and why.

    Called from _write_pending_confirmation when a non-terminal pending exists.
    """
    import os
    try:
        os.makedirs(os.path.dirname(_SUPERSEDED_LOG), exist_ok=True)
        record = {
            "ts":                  datetime.now().isoformat(timespec="seconds"),
            "superseded_directive_id": prior.get("directive_id"),
            "superseded_status":   prior.get("status"),
            "superseded_created_at": prior.get("created_at"),
            "superseded_expires_at": prior.get("expires_at"),
            "superseded_understanding": prior.get("understanding", "")[:500],
            "superseded_agenda":   prior.get("agenda", []),
            "superseded_nl_directives": prior.get("nl_directives", []),
            "superseded_pending_status_updates": prior.get("pending_status_updates", []),
            "superseded_pending_directives": prior.get("pending_directives", []),
            "superseded_by_directive_id": new_directive_id,
            "reason":              reason,
        }
        with open(_SUPERSEDED_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[superseded-log failed: {e}]")


def _write_pending_confirmation(updates, directives, nl_directives, understanding, agenda,
                                source_subject, reply_text, held_observations=None,
                                decisions_made=None):
    """Write pending-confirmation.json.

    State-machine invariants enforced here (Fix 2a):
      1. Every pending has a stable `directive_id` (UUID4).
      2. If a prior pending exists in a NON-TERMINAL state ({pending,executing}),
         it is archived to superseded-directives.jsonl BEFORE being overwritten.
         The full content is preserved with a superseded_by pointer + reason.
      3. If a prior pending shows status=executing AND it was set within the
         last _EXECUTED_GRACE_SECONDS, refuse the new write entirely. The
         in-flight directive is allowed to finish; the new directive is logged
         to superseded-directives.jsonl as "refused_during_executing_grace"
         and the function returns the prior pending unchanged.

    held_observations: list of {subject, text, structured_count} dicts — observations
    captured from this reply that will be released to observations.md after execution.
    decisions_made: list of {fork, chose, alternative, reason} dicts surfaced in the echo.
    """
    import uuid
    from datetime import timedelta
    now = datetime.now()

 # ─── invariant 1: every pending has a directive_id ───────────────────────
    new_directive_id = str(uuid.uuid4())

    data = {
        "directive_id":           new_directive_id,
        "created_at":             now.isoformat(timespec="seconds"),
        "expires_at":             (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "source_subject":         source_subject,
        "reply_text":             reply_text[:2000],
        "understanding":          understanding,
        "agenda":                 agenda,
        "pending_status_updates": [[idx, canon] for idx, canon, _, _, _ in updates],
        "pending_directives":     [[d_type, payload, raw] for d_type, payload, raw in directives],
        "nl_directives":          nl_directives,
        "decisions_made":         decisions_made or [],
        "held_observations":      held_observations or [],
        "echo_sent_at":           None,
        "executing_at":           None,
        "executed_at":            None,
        "status":                 "pending",
    }

 # ─── invariants 2 + 3 enforced atomically under the state lock ───────────
    def mutator(prior):
        if prior:
            prior_status = (prior.get("status") or "").strip().lower()

 # Invariant 3 — executing grace: refuse to clobber an actively-executing
 # directive (between execute_pending and mark_executed).
            if prior_status == "executing":
                executing_at = prior.get("executing_at")
                try:
                    t_exec = datetime.fromisoformat(executing_at)
                    age_s = (now - t_exec).total_seconds()
                except Exception:
                    age_s = 9999.0
                if age_s < _EXECUTED_GRACE_SECONDS:
                    _archive_superseded(
                        {
                            "directive_id": new_directive_id,
                            "status": "refused_during_executing_grace",
                            "created_at": now.isoformat(timespec="seconds"),
                            "understanding": understanding,
                            "agenda": agenda,
                            "nl_directives": nl_directives,
                            "pending_status_updates": [[idx, canon] for idx, canon, _, _, _ in updates],
                            "pending_directives": [[d_type, payload, raw] for d_type, payload, raw in directives],
                        },
                        new_directive_id=prior.get("directive_id"),
                        reason=f"refused_during_executing_grace (in-flight directive {age_s:.1f}s old)",
                    )
                    print(f"  [pending refused: prior directive {prior.get('directive_id','?')[:8]} is executing "
                          f"({age_s:.1f}s old, grace={_EXECUTED_GRACE_SECONDS}s)]")
                    return prior, prior  # no write (skip_write_if_unchanged)

 # Invariant 2 — archive non-terminal prior before overwriting.
            if prior_status in ("pending", "executing"):
                _archive_superseded(
                    prior,
                    new_directive_id=new_directive_id,
                    reason=f"overwritten_by_new_directive (prior was status={prior_status!r})",
                )
                print(f"  [pending superseded: prior directive {prior.get('directive_id','?')[:8]} "
                      f"status={prior_status!r} archived to superseded-directives.jsonl]")

        return data, data

    return safe_state.atomic_update(
        _PENDING_CONF, mutator,
        default=None,
        skip_write_if_unchanged=True,
    )


def _send_echo_email(understanding, agenda, created_at_str, decisions_made=None):
    """Send the confirmation echo email (and Telegram ping). Returns (subject, success_bool).

    C2 verifiers run on both surfaces:
      - email: IMAP Sent-folder probe (independent protocol from SMTP send)
      - telegram: getChat probe against the server-assigned message_id
    """
    import notify_email, notify_telegram, verify
    ts = created_at_str[:16].replace("T", " ")
    subject = f"Alice: confirming — {ts}"
    agenda_text = "\n".join(agenda)
    decisions_block = _format_decisions_block(decisions_made)
    body = (
        f"Hi Jordan,\n\n"
        f"Here's my understanding of your last reply:\n\n"
        f"{understanding}\n\n"
        f"Agenda:\n{agenda_text}\n\n"
        f"{decisions_block}"
        f"If this is right, no action needed. I'll proceed unless you correct.\n"
        f"If I got something wrong, reply to this email with corrections.\n\n"
        f"Alice"
    )
    ok = notify_email.send(subject, body)
 # C2: IMAP Sent-folder probe.
    if ok:
        vr = verify.verify_email_send(subject_substr=ts)
        if not vr.ok:
            print(f"  VERIFY ERROR email_send: {vr.claim}")
    try:
        tg_res = notify_telegram.send_with_id(
            f"Alice: confirming\n\n{understanding}\n\nAgenda:\n{agenda_text}\n\n"
            f"{decisions_block}"
            f"No action needed if correct. I'll proceed unless you correct.\n"
            f"Reply here or via email to correct."
        )
 # C2: server-state check via getChat using the message_id.
        if tg_res.get("ok"):
            vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
            if not vr.ok:
                print(f"  VERIFY ERROR telegram_send: {vr.claim}")
    except Exception as _e:
        print(f"[telegram echo failed: {_e}]")
    return subject, ok


def process_replies():
    """Read unflagged replies to digest emails; apply status updates; capture observations; flag processed."""
    if not available():
        print("[imap_reply: no Gmail creds in config.env — skipping]")
        return {"replies_seen": 0, "updates_applied": 0, "no_match": 0, "ambiguous": 0}

 # load sheet once
    try:
        from alice.persistence import ledger
        if not ledger.available():
            print("[imap_reply: sheet ledger unavailable — skipping]")
            return {"replies_seen": 0, "updates_applied": 0, "no_match": 0, "ambiguous": 0}
        ws = ledger._ws()
        rows = ws.get_all_records()
    except Exception as e:
        print(f"[imap_reply: ledger read failed: {e} — skipping]")
        return {"replies_seen": 0, "updates_applied": 0, "no_match": 0, "ambiguous": 0}

    M, user = _imap_open()
    try:
        M.select("INBOX")
 # search: subject contains "Job digest" AND not flagged (our processed marker)
 # also: sent BY the user (their replies to the digest)
        typ, data = M.search(None, '(FROM "%s" SUBJECT "Job digest" UNFLAGGED)' % user)
        if typ != "OK":
            print(f"[imap_reply: IMAP search failed: {typ}]")
            return {"replies_seen": 0, "updates_applied": 0, "no_match": 0, "ambiguous": 0}
        ids = data[0].split()
        if not ids:
            print("[imap_reply: no unprocessed replies found]")
            return {"replies_seen": 0, "updates_applied": 0, "no_match": 0, "ambiguous": 0}

        updates = []         # (row_index, canonical_status, substr_matched)
        no_match = []        # (substr, canonical)
        ambiguous = []       # (substr, canonical, candidates)
        directives = []      # (type, payload, raw_line)
        observations = 0     # count of replies containing unstructured feedback
        replies_seen = 0
        all_clean_texts = []           # accumulated for echo generation
        last_subject = ""              # subject of last processed message
        held_observation_entries = []  # (subj, text, structured_count) — written later

        from alice.pipeline import directives as directives_mod

        for mid in ids:
            typ, mdata = M.fetch(mid, "(RFC822)")
            if typ != "OK" or not mdata or not mdata[0]:
                continue
            replies_seen += 1
            raw = mdata[0][1]
            msg = email.message_from_bytes(raw)
            subj = _decode_subject(msg.get("Subject", ""))
            body = _body_text(msg)
            clean = _strip_quoted(body)
            print(f"[reply seen: {subj[:60]}]")
            applied_in_this_msg = 0
            actioned_lines = set()  # text of lines that produced status updates / commands / directives
            for line in clean.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
 # First try directives (focus:, prep:, debrief:, hypothesis:, etc.) — these
 # take precedence over status parsing because they're explicit syntax
                d_type, d_payload = directives_mod.parse_line(stripped)
                if d_type:
                    directives.append((d_type, d_payload, stripped))
                    actioned_lines.add(stripped)
                    print(f"  directive [{d_type}]: {d_payload}")
                    continue
 # Then try status updates
                canon, substr = _parse_line(line)
                if not canon or not substr:
                    continue
                hits = _match_sheet_row(substr, rows)
                if not hits:
                    no_match.append((substr, canon))
                    print(f"  no-match: {substr!r} -> {canon}")
                    actioned_lines.add(stripped)
                elif len(hits) > 1:
                    ambiguous.append((substr, canon, [(i, r.get('company',''), r.get('role','')) for i, r in hits]))
                    print(f"  ambiguous ({len(hits)} matches): {substr!r} -> {canon}")
                    for i, r in hits[:5]:
                        print(f"      row {i}: {r.get('company','')} {r.get('role','')}")
                    actioned_lines.add(stripped)
                else:
                    row_idx, r = hits[0]
                    updates.append((row_idx, canon, substr, r.get('company',''), r.get('role','')))
                    applied_in_this_msg += 1
                    actioned_lines.add(stripped)
                    print(f"  update: row {row_idx} ({r.get('company','')} {r.get('role','')[:35]}) -> {canon}")

 # Capture any non-actioned, non-empty lines as unstructured observational feedback.
 # Preserves qualitative input ("noticed OpenAI hub-bound roles keep slipping through";
 # "the Loopwork hypothesis had a false positive on CAD") that would otherwise be
 # silently dropped when the message gets flagged as processed.
            residual_lines = [ln for ln in clean.splitlines()
                              if ln.strip() and ln.strip() not in actioned_lines]
            residual_text = "\n".join(residual_lines).strip()
            if residual_text:
 # Collect but don't write yet — may be held in pending-confirmation.json
                held_observation_entries.append((subj, residual_text, applied_in_this_msg))
                observations += 1
                print(f"  + observation captured ({len(residual_text)} chars)")

            all_clean_texts.append(clean)
            last_subject = subj

 # Flag the message as processed regardless of whether anything matched —
 # otherwise no-match replies get re-scanned forever
            M.store(mid, "+FLAGS", "\\Flagged")

 # ── Confirmation loop: hold ALL substantive content until the operator confirms ──
 # Triggers on structured updates, explicit directives, OR any unstructured text
 # (natural-language commands like "Prioritize X, begin prep for Y").
        has_substantive = bool(updates or directives or observations > 0)
        echo_cost = 0.0
        directive_results = {}
        pending_written = False

        if has_substantive:
 # Load current focus for context in the echo
            try:
                from alice.persistence import focus as _focus_mod
                focus_roles = _focus_mod.current()
            except Exception:
                focus_roles = []

            all_clean_text = "\n---\n".join(all_clean_texts)

            echo_data, echo_cost = _generate_echo(
                all_clean_text, updates, directives, focus_roles
            )

 # Store held observations in pending so confirm_and_execute can release them
            held_obs_payload = [
                {"subject": s, "text": t, "structured_count": c}
                for s, t, c in held_observation_entries
            ]

            decisions = echo_data.get("decisions_made", []) or []
            _log_decisions(decisions, source="imap_reply:process_replies")

            pending = _write_pending_confirmation(
                updates, directives,
                echo_data.get("nl_directives", []),
                echo_data.get("understanding", ""),
                echo_data.get("agenda", []),
                last_subject,
                all_clean_text,
                held_observations=held_obs_payload,
                decisions_made=decisions,
            )

            echo_subject, echo_ok = _send_echo_email(
                echo_data.get("understanding", ""),
                echo_data.get("agenda", []),
                pending["created_at"],
                decisions_made=decisions,
            )

            if echo_ok:
                pending["echo_sent_at"] = datetime.now().isoformat(timespec="seconds")
                safe_state.atomic_write(_PENDING_CONF, pending)

            pending_written = True
            print(f"  [confirmation echo sent: {echo_subject!r} (${echo_cost:.4f})]")
            print(f"  [actions held — execute after 1 hr (confirm_and_execute.py)]")
        else:
 # Nothing substantive — write any observations directly (no confirmation needed)
            for subj, text, count in held_observation_entries:
                _append_observation(subj, text, count)
                print(f"  + observation written -> {_FEEDBACK_LOG}")

        return {
            "replies_seen":     replies_seen,
            "updates_applied":  0 if has_substantive else len(updates),
            "updates_pending":  len(updates) if has_substantive else 0,
            "no_match":         len(no_match),
            "ambiguous":        len(ambiguous),
            "observations":     observations,
            "directives":       len(directives),
            "directive_results": directive_results,
            "pending_confirmation": pending_written,
            "echo_cost":        echo_cost,
            "updates":          updates,
            "no_match_detail":  no_match,
            "ambiguous_detail": ambiguous,
        }
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _parse_lines(text, rows):
    """Parse a text body for status updates and directives. No LLM, no side effects.

    Returns (updates, no_match, ambiguous, directives_list, residual_text).
    Used by process_text_reply() and by telegram_bot.py for directive handling.
    """
    from alice.pipeline import directives as directives_mod

    clean = text.strip()
    updates = []
    no_match = []
    ambiguous = []
    directives_list = []
    actioned_lines = set()

    for line in clean.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        d_type, d_payload = directives_mod.parse_line(stripped)
        if d_type:
            directives_list.append((d_type, d_payload, stripped))
            actioned_lines.add(stripped)
            continue
        canon, substr = _parse_line(line)
        if not canon or not substr:
            continue
        hits = _match_sheet_row(substr, rows)
        if not hits:
            no_match.append((substr, canon))
            actioned_lines.add(stripped)
        elif len(hits) > 1:
            ambiguous.append((substr, canon,
                              [(i, r.get("company", ""), r.get("role", "")) for i, r in hits]))
            actioned_lines.add(stripped)
        else:
            row_idx, r = hits[0]
            updates.append((row_idx, canon, substr, r.get("company", ""), r.get("role", "")))
            actioned_lines.add(stripped)

    residual_lines = [ln for ln in clean.splitlines()
                      if ln.strip() and ln.strip() not in actioned_lines]
    residual_text = "\n".join(residual_lines).strip()

    return updates, no_match, ambiguous, directives_list, residual_text


def process_text_reply(text, source_subject="telegram"):
    """Process a reply text body from a non-email source (e.g. Telegram).

    Parses directives and status updates, generates an LLM echo, writes
    pending-confirmation.json. Does NOT send any notification — caller sends
    on the appropriate channel (Telegram, email, etc.).

    Returns dict with keys: updates, no_match, ambiguous, directives,
    residual_text, has_substantive, echo_data, pending, echo_cost, error.
    """
    try:
        from alice.persistence import ledger
        if not ledger.available():
            return {"error": "ledger unavailable", "has_substantive": False,
                    "echo_data": None, "pending": None}
        ws = ledger._ws()
        rows = ws.get_all_records()
    except Exception as e:
        return {"error": str(e), "has_substantive": False, "echo_data": None, "pending": None}

 # Experience-capture confirmations are applied IN-LINE before line
 # parsing so the residual_text the LLM sees doesn't try to "decide
 # what to do" about a 'confirm exp-cand-abc' line that's already
 # been executed. Idempotent: re-applying confirms is safe (silent
 # no-op).
    try:
        from alice.persistence import experience_store
        exp_applied = experience_store.parse_and_apply_reply(text)
    except Exception as _e:
        exp_applied = {"confirmed": [], "rejected": [], "edited": [], "errors": []}

    updates, no_match, ambiguous, directives_list, residual_text = _parse_lines(text, rows)

    has_substantive = (
        bool(updates or directives_list or residual_text)
        or any(exp_applied[k] for k in ("confirmed", "rejected", "edited"))
    )
    echo_data = None
    pending_data = None
    echo_cost = 0.0

    if has_substantive:
        try:
            from alice.persistence import focus as _focus_mod
            focus_roles = _focus_mod.current()
        except Exception:
            focus_roles = []

        echo_data, echo_cost = _generate_echo(text, updates, directives_list, focus_roles)

        held_obs_payload = []
        if residual_text:
            held_obs_payload = [{"subject": source_subject, "text": residual_text,
                                 "structured_count": len(updates)}]

        decisions = echo_data.get("decisions_made", []) or []
        _log_decisions(decisions, source=f"imap_reply:process_text_reply:{source_subject}")

        pending_data = _write_pending_confirmation(
            updates, directives_list,
            echo_data.get("nl_directives", []),
            echo_data.get("understanding", ""),
            echo_data.get("agenda", []),
            source_subject,
            text,
            held_observations=held_obs_payload,
            decisions_made=decisions,
        )

    return {
        "updates": updates,
        "no_match": no_match,
        "ambiguous": ambiguous,
        "directives": directives_list,
        "residual_text": residual_text,
        "has_substantive": has_substantive,
        "echo_data": echo_data,
        "pending": pending_data,
        "echo_cost": echo_cost,
        "error": None,
    }


def main():
    res = process_replies()
    print()
    if res.get("pending_confirmation"):
        print(f"summary: {res['replies_seen']} replies seen, "
              f"{res['updates_pending']} update(s) pending confirmation, "
              f"{res['directives']} directive(s) pending, "
              f"{res.get('observations', 0)} observation(s) captured "
              f"[echo sent — execute after 1 hr]")
    else:
        print(f"summary: {res['replies_seen']} replies seen, "
              f"{res['updates_applied']} updates applied, "
              f"{res['no_match']} no-match, "
              f"{res['ambiguous']} ambiguous, "
              f"{res.get('observations', 0)} observation(s) captured")
    try:
        from alice.persistence import activity_log
        replies = res.get("replies_seen", 0)
        updates_applied = res.get("updates_applied", 0)
        updates_pending = res.get("updates_pending", 0)
        obs = res.get("observations", 0)
        directives = res.get("directives", 0)
        pending = res.get("pending_confirmation", False)
        if pending:
            summary = (f"{replies} repl{'y' if replies == 1 else 'ies'}, "
                       f"{updates_pending} update(s) pending confirmation, "
                       f"{directives} directive(s) pending, "
                       f"{obs} observation{'' if obs == 1 else 's'}")
        else:
            summary = (f"{replies} repl{'y' if replies == 1 else 'ies'}, "
                       f"{updates_applied} status update{'' if updates_applied == 1 else 's'}, "
                       f"{directives} directive{'' if directives == 1 else 's'}, "
                       f"{obs} observation{'' if obs == 1 else 's'}")
        activity_log.record(
            step="imap_reply",
            summary=summary,
            count=(replies + directives + obs),
            status="noop" if (replies + directives + obs) == 0 else "ok",
            details={"replies": replies, "updates_applied": updates_applied,
                     "updates_pending": updates_pending,
                     "directives": directives, "observations": obs,
                     "pending_confirmation": pending,
                     "no_match": res.get("no_match", 0),
                     "ambiguous": res.get("ambiguous", 0)},
        )
    except Exception as e:
        print(f"[activity_log: {e}]")


if __name__ == "__main__":
    main()
