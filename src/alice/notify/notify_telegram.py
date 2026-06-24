"""Send a text message to the operator via Alice's Telegram bot.

Reads from config.env:
    TELEGRAM_BOT_TOKEN=<bot token>
    TELEGRAM_CHAT_ID=<the operator's chat ID>
"""
import json
import ssl
import urllib.request
from alice.jobcfg import load

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


def available():
    cfg = load()
    return bool(cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_CHAT_ID"))


def send(text):
    """Send a message. Returns True/False. Kept as the legacy signature so
    existing callers don't change. For verification (item 5 / C2), use
    send_with_id() — it returns the server-assigned message_id which the
    `verify.verify_telegram_send` surface needs."""
    res = send_with_id(text)
    return res.get("ok", False)


def send_with_id(text):
    """Send and return {ok, message_id, error}. message_id is the
    server-assigned id needed by verify.verify_telegram_send."""
    cfg = load()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram: no credentials in config — skipping]")
        return {"ok": False, "message_id": None, "error": "no credentials"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": int(chat_id), "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        mid = (data.get("result") or {}).get("message_id")
        return {"ok": bool(data.get("ok")), "message_id": mid, "error": None}
    except Exception as e:
        print(f"[telegram send failed: {e}]")
        return {"ok": False, "message_id": None, "error": str(e)}
