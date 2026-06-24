"""Email nudge sender. Default transport: Gmail SMTP with an App Password
(simplest; sends from your account to your inbox). Reads from config.env:
    GMAIL_USER=you@gmail.com
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   (Google Account > Security > App passwords)
    EMAIL_TO=you@example.com           (defaults to GMAIL_USER)
Falls back to Resend if RESEND_API_KEY + EMAIL_FROM are set instead.
"""
import json
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from alice.jobcfg import load

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def available():
    cfg = load()
    return bool(cfg.get("GMAIL_USER") and cfg.get("GMAIL_APP_PASSWORD")) or \
        bool(cfg.get("RESEND_API_KEY") and cfg.get("EMAIL_FROM"))


def send(subject, body, digest=False):
    """Send email. Only scheduled daily/weekly digests go via email; everything
    else routes through Telegram (notify_telegram).
    Callers must pass digest=True explicitly to send; all other calls become
    no-ops (logged to stderr) so the system can't accidentally email
    confirmations, status nudges, or interview reminders."""
    if not digest:
        import sys
        print(f"[email gated: non-digest send suppressed (subject={subject[:60]!r}); telegram handles this]",
              file=sys.stderr)
        return False
    cfg = load()
    to = cfg.get("EMAIL_TO") or cfg.get("GMAIL_USER")
 # Gmail SMTP (preferred)
    if cfg.get("GMAIL_USER") and cfg.get("GMAIL_APP_PASSWORD"):
        msg = EmailMessage()
        msg["From"] = cfg["GMAIL_USER"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=_SSL_CTX) as s:
            s.login(cfg["GMAIL_USER"], cfg["GMAIL_APP_PASSWORD"].replace(" ", ""))
            s.send_message(msg)
        return True
 # Resend fallback
    if cfg.get("RESEND_API_KEY") and cfg.get("EMAIL_FROM"):
        data = json.dumps({"from": cfg["EMAIL_FROM"], "to": [to],
                           "subject": subject, "text": body}).encode()
        req = urllib.request.Request("https://api.resend.com/emails", data=data,
                                     headers={"Authorization": f"Bearer {cfg['RESEND_API_KEY']}",
                                              "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
        return True
    print("[email skipped: no GMAIL_APP_PASSWORD or RESEND_API_KEY in config]")
    return False
