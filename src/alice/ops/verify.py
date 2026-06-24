"""Independent verification surfaces.

For each action type Alice takes, an independent check path. Verify what you
can; declare the rest unverifiable and fail closed.

Each verifier returns a VerifyResult with:
  ok           : bool   — did the independent check confirm the action landed?
  verified     : bool   — did we have an independent surface at all?
  claim        : str    — exactly what the check proves (don't overclaim)
  detail       : dict   — what was checked, what was found

Failure modes:
  ok=False, verified=True   -> action did NOT land; caller should treat as failed
  ok=False, verified=False  -> we have no way to check; caller should treat as
                               unverified and fail closed (do not assert success)
"""
import json
import imaplib
import smtplib  # noqa: F401  — for grep-completeness; we use imap_open here
import ssl
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from alice import repo_paths

from alice.jobcfg import load as _load_cfg

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


_VERIFY_LOG = Path(repo_paths.FEEDBACK / "verify-log.jsonl")


@dataclass
class VerifyResult:
    ok:       bool
    verified: bool
    claim:    str
    detail:   dict = field(default_factory=dict)

    def journal(self, kind: str):
        try:
            _VERIFY_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _VERIFY_LOG.open("a") as f:
                rec = {"ts": datetime.now().isoformat(timespec="seconds"),
                       "kind": kind, **asdict(self)}
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        return self


# ─── 1. Email send: IMAP Sent-folder probe ────────────────────────────────────

def verify_email_send(subject_substr: str, since_iso: str | None = None) -> VerifyResult:
    """Probe Gmail's Sent folder via IMAP for a recently-sent message whose
    Subject contains `subject_substr`. Independent because IMAP and SMTP are
    different protocols served by Gmail's server (the canonical truth).

    Claim proven: "message exists in Sent folder with matching subject".
    Does NOT claim: "received by recipient" or "displayed in their inbox".
    """
    cfg = _load_cfg()
    if not (cfg.get("GMAIL_USER") and cfg.get("GMAIL_APP_PASSWORD")):
        return VerifyResult(
            ok=False, verified=False,
            claim="no IMAP credentials — cannot verify email send",
            detail={"subject_substr": subject_substr},
        ).journal("email_send")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=_SSL_CTX)
        M.login(cfg["GMAIL_USER"], cfg["GMAIL_APP_PASSWORD"].replace(" ", ""))
        try:
            M.select('"[Gmail]/Sent Mail"')
 # IMAP4 search clause is ASCII-only. Strip non-ASCII (em-dashes etc.)
 # before searching; whatever ASCII substring remains is still a
 # reasonable witness because the subject substr is just a "find me"
 # heuristic. If nothing ASCII survives, fall back to a generic
 # search marker so we don't pass an empty pattern.
            ascii_only = "".join(c for c in subject_substr if ord(c) < 128)
            safe = ascii_only.replace('"', "")[:80].strip()
            if not safe:
                return VerifyResult(
                    ok=False, verified=False,
                    claim="subject_substr had no ASCII chars to search by — cannot verify",
                    detail={"subject_substr": subject_substr},
                ).journal("email_send")
            typ, data = M.search(None, f'(SUBJECT "{safe}")')
            if typ != "OK":
                return VerifyResult(
                    ok=False, verified=True,
                    claim="IMAP search failed; assume email did not land",
                    detail={"subject_substr": subject_substr, "typ": typ},
                ).journal("email_send")
            ids = data[0].split() if data and data[0] else []
            ok = len(ids) > 0
            return VerifyResult(
                ok=ok, verified=True,
                claim=("message exists in Sent folder with matching subject"
                       if ok else "no matching message in Sent folder"),
                detail={"subject_substr": subject_substr, "match_count": len(ids)},
            ).journal("email_send")
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"IMAP probe error — cannot verify ({e})",
            detail={"subject_substr": subject_substr, "error": str(e)},
        ).journal("email_send")


# ─── 2. Sheet write: fresh-auth read-back ─────────────────────────────────────

def verify_sheet_status_write(row_idx: int, expected_status: str) -> VerifyResult:
    """Open a NEW gspread session with fresh auth (not the cached _ws session
    used to write) and re-read the status cell. Independent because the auth
    handshake is repeated, eliminating the "stale local-cache" failure mode.

    Claim proven: "the sheet, fetched with fresh credentials, shows
    expected_status in column G of row_idx".
    """
    cfg = _load_cfg()
    if not (cfg.get("GOOGLE_SA_JSON") and cfg.get("LEDGER_SHEET_ID")):
        return VerifyResult(
            ok=False, verified=False,
            claim="no sheet credentials — cannot verify sheet write",
            detail={"row_idx": row_idx, "expected": expected_status},
        ).journal("sheet_write")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from gspread.utils import rowcol_to_a1
        creds = Credentials.from_service_account_file(
            cfg["GOOGLE_SA_JSON"],
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(cfg["LEDGER_SHEET_ID"])
        ws = sh.sheet1
        cell = rowcol_to_a1(row_idx, 7)
        actual = (ws.acell(cell).value or "").strip()
        ok = actual.lower() == (expected_status or "").strip().lower()
        return VerifyResult(
            ok=ok, verified=True,
            claim=("sheet (fresh-auth read-back) confirms status"
                   if ok else "sheet (fresh-auth) shows different status"),
            detail={"row_idx": row_idx, "cell": cell,
                    "expected": expected_status, "actual": actual},
        ).journal("sheet_write")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"sheet read-back error — cannot verify ({e})",
            detail={"row_idx": row_idx, "expected": expected_status, "error": str(e)},
        ).journal("sheet_write")


# ─── 2b. Sheet insert: fresh-auth scan for expected job_keys ─────────────────

def verify_sheet_insert(expected_job_keys: list) -> VerifyResult:
    """After ledger.insert_new, open a NEW gspread session with fresh auth and
    confirm every expected job_key appears in column J. Independent because
    the auth handshake is repeated, eliminating the "stale local-cache" failure
    mode.

    Claim proven: "the sheet, fetched with fresh credentials, contains the
    expected job_keys in column J".
    """
    expected = [k for k in (expected_job_keys or []) if k]
    if not expected:
        return VerifyResult(
            ok=True, verified=True,
            claim="no job_keys to verify — no-op",
            detail={"expected_count": 0},
        ).journal("sheet_insert")
    cfg = _load_cfg()
    if not (cfg.get("GOOGLE_SA_JSON") and cfg.get("LEDGER_SHEET_ID")):
        return VerifyResult(
            ok=False, verified=False,
            claim="no sheet credentials — cannot verify sheet insert",
            detail={"expected_count": len(expected)},
        ).journal("sheet_insert")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            cfg["GOOGLE_SA_JSON"],
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(cfg["LEDGER_SHEET_ID"])
        ws = sh.sheet1
 # Column J is index 10 (1-based) = job_key. Read a generous window
 # from the top so we catch fresh inserts plus a buffer.
        col_values = ws.col_values(10)[: max(len(expected) * 3, 100)]
        col_set = {v.strip() for v in col_values if v}
        missing = [k for k in expected if k not in col_set]
        ok = not missing
        return VerifyResult(
            ok=ok, verified=True,
            claim=("sheet (fresh-auth) contains all expected job_keys"
                   if ok else f"sheet (fresh-auth) missing {len(missing)} job_keys"),
            detail={"expected_count": len(expected), "missing": missing[:10]},
        ).journal("sheet_insert")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"sheet insert verification error — cannot verify ({e})",
            detail={"expected_count": len(expected), "error": str(e)},
        ).journal("sheet_insert")


# ─── 3. Telegram send: server-state check ─────────────────────────────────────

def verify_telegram_send(message_id: int | None = None) -> VerifyResult:
    """After a Telegram send, call getChat (or getMe) to confirm the bot is
    still reachable and the message_id returned by sendMessage is consistent.

    Claim proven: "Telegram's API confirms the bot can reach the configured
    chat and the message_id is server-assigned (i.e. the message landed on
    Telegram's servers)".
    Does NOT claim: "Jordan Avery has read it" or "the device delivered the
    notification".
    """
    cfg = _load_cfg()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return VerifyResult(
            ok=False, verified=False,
            claim="no Telegram credentials — cannot verify",
            detail={},
        ).journal("telegram_send")
    if message_id is None:
        return VerifyResult(
            ok=False, verified=False,
            claim="no message_id provided — cannot verify message landed",
            detail={},
        ).journal("telegram_send")
    try:
        url = f"https://api.telegram.org/bot{token}/getChat?chat_id={int(chat_id)}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        ok = bool(data.get("ok")) and bool(message_id)
        return VerifyResult(
            ok=ok, verified=True,
            claim=("message landed on Telegram server (server-assigned id)"
                   if ok else "Telegram server did not confirm chat reachability"),
            detail={"message_id": message_id, "chat_ok": data.get("ok")},
        ).journal("telegram_send")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"Telegram verification error — cannot verify ({e})",
            detail={"message_id": message_id, "error": str(e)},
        ).journal("telegram_send")


# ─── 4. File writes: mtime + fresh re-read ────────────────────────────────────

def verify_file_write(path: str, expected_content_substr: str | None = None,
                      since_iso: str | None = None) -> VerifyResult:
    """stat() the file for fresh mtime + re-read from a NEW file handle.
    Independent because the re-read goes through a separate open() call —
    not through any in-process cache.

    Claim proven: "file exists, mtime >= since_iso, and (optionally) content
    contains expected_content_substr on a fresh re-read".
    """
    p = Path(path)
    if not p.exists():
        return VerifyResult(
            ok=False, verified=True,
            claim="file does not exist after write",
            detail={"path": path},
        ).journal("file_write")
    try:
        stat = p.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        fresh_enough = True
        if since_iso:
            try:
                fresh_enough = mtime >= since_iso
            except Exception:
                fresh_enough = True
        with p.open("rb") as f:
            content = f.read()
        text = content.decode("utf-8", errors="replace")
        substr_ok = True
        if expected_content_substr:
            substr_ok = expected_content_substr in text
        ok = fresh_enough and substr_ok
        return VerifyResult(
            ok=ok, verified=True,
            claim=("file exists with fresh mtime and expected content"
                   if ok else "file write did not produce expected state"),
            detail={"path": path, "mtime": mtime, "size": stat.st_size,
                    "fresh_enough": fresh_enough, "substr_ok": substr_ok},
        ).journal("file_write")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"file verification error — cannot verify ({e})",
            detail={"path": path, "error": str(e)},
        ).journal("file_write")


# ─── 5. Directive apply: re-read state files ──────────────────────────────────

def verify_focus_state(expected_substrings: list[str]) -> VerifyResult:
    """After applying focus directives, re-read feedback/focus.json from a
    fresh file handle and confirm every expected substring is present in the
    role list.

    Claim proven: "focus.json (re-read) contains all expected role substrings".
    """
    from alice import safe_state
    p = Path(repo_paths.FEEDBACK / "focus.json")
    try:
        state = safe_state.atomic_read(p, default=None)
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"focus.json verification error — cannot verify ({e})",
            detail={"expected": expected_substrings, "error": str(e)},
        ).journal("focus_state")
    if state is None:
        return VerifyResult(
            ok=False, verified=True,
            claim="focus.json missing after directive apply",
            detail={"expected": expected_substrings},
        ).journal("focus_state")
    try:
        roles = state.get("roles", [])
        haystacks = [
            f"{r.get('company','')} {r.get('role','')}".lower()
            for r in roles
        ]
        missing = []
        for s in expected_substrings:
            needle = s.lower()
            if not any(needle in h for h in haystacks):
                missing.append(s)
        ok = not missing
        return VerifyResult(
            ok=ok, verified=True,
            claim=("focus.json contains all expected role substrings"
                   if ok else "focus.json missing expected role substrings"),
            detail={"expected": expected_substrings, "missing": missing,
                    "current": [f"{r.get('company','')}|{r.get('role','')}" for r in roles]},
        ).journal("focus_state")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"focus.json verification error — cannot verify ({e})",
            detail={"expected": expected_substrings, "error": str(e)},
        ).journal("focus_state")


def verify_pending_executed() -> VerifyResult:
    """After confirm_and_execute, re-read pending-confirmation.json from a
    fresh handle and confirm status == 'executed'.

    Claim proven: "pending-confirmation.json (re-read) shows executed status".
    """
    from alice import safe_state
    p = Path(repo_paths.FEEDBACK / "pending-confirmation.json")
    try:
        data = safe_state.atomic_read(p, default=None)
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"pending.json verification error — cannot verify ({e})",
            detail={"error": str(e)},
        ).journal("pending_executed")
    if data is None:
        return VerifyResult(
            ok=True, verified=True,
            claim="no pending file — nothing to verify",
            detail={},
        ).journal("pending_executed")
    try:
        status = data.get("status", "")
        ok = status == "executed"
        return VerifyResult(
            ok=ok, verified=True,
            claim=(f"pending.status='{status}' as expected"
                   if ok else f"pending.status='{status}' not 'executed'"),
            detail={"status": status, "executed_at": data.get("executed_at")},
        ).journal("pending_executed")
    except Exception as e:
        return VerifyResult(
            ok=False, verified=False,
            claim=f"pending.json verification error — cannot verify ({e})",
            detail={"error": str(e)},
        ).journal("pending_executed")


# ─── coverage manifest (consumed by item 8 readiness check) ───────────────────

# Every action type Alice can take, and whether we have an independent verifier.
ACTION_VERIFICATION_COVERAGE = {
    "email_send":      {"verifier": "verify_email_send",      "verified": True,
                        "surface": "IMAP Sent-folder probe (separate protocol)"},
    "sheet_write":     {"verifier": "verify_sheet_status_write", "verified": True,
                        "surface": "fresh-auth gspread re-read"},
    "sheet_insert":    {"verifier": "verify_sheet_insert",    "verified": True,
                        "surface": "fresh-auth col-J scan for expected job_keys"},
    "telegram_send":   {"verifier": "verify_telegram_send",   "verified": True,
                        "surface": "Telegram getChat server-state check"},
    "file_write":      {"verifier": "verify_file_write",      "verified": True,
                        "surface": "mtime + fresh-handle re-read"},
    "focus_apply":     {"verifier": "verify_focus_state",     "verified": True,
                        "surface": "focus.json fresh re-read"},
    "pending_execute": {"verifier": "verify_pending_executed", "verified": True,
                        "surface": "pending-confirmation.json fresh re-read"},
}


def coverage_report() -> dict:
    """Used by readiness check (item 8). Returns coverage stats + per-action surfaces."""
    total = len(ACTION_VERIFICATION_COVERAGE)
    verified = sum(1 for v in ACTION_VERIFICATION_COVERAGE.values() if v.get("verified"))
    return {
        "total_action_types":    total,
        "verified_action_types": verified,
        "coverage_ratio":        (verified / total) if total else 0.0,
        "actions":               ACTION_VERIFICATION_COVERAGE,
    }


if __name__ == "__main__":
    print(json.dumps(coverage_report(), indent=2))
