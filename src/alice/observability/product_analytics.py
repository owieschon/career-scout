"""Privacy-safe product analytics for Alice.

PostHog answers product/workflow questions ("which Alice flows get used and
complete?"). It is deliberately separate from Sentry/Phoenix/LangSmith, which
answer error, trace, and LLM-quality questions.

No raw chat text, job descriptions, resumes, names, emails, tokens, or document
content should ever be sent through this module. Callers pass event names and
small structured properties only; this wrapper redacts defensively and drops
oversized/free-text-looking values before they leave the process.
"""
from __future__ import annotations

import os
import json
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from alice.jobcfg import load as _load_cfg


_CLIENT = None
_INITIALIZED = False
_ENABLED = False
_INIT_DETAIL = "not initialized"
_PROJECT_API_KEY = ""
_HOST = ""

_ALLOW_KEYS = {
    "chat_id_hash",
    "chunks",
    "claim_type",
    "component",
    "cost_usd",
    "delivery_failed",
    "event_count",
    "had_keyboard",
    "has_key",
    "is_paste",
    "limit",
    "missing",
    "model",
    "n_chunks",
    "ok",
    "project",
    "reason",
    "role",
    "rounds",
    "since_hours",
    "since_minutes",
    "status",
    "surface",
    "task",
    "tool_count",
    "tool_error",
    "tool_name",
    "tool_names",
    "trace_enabled",
    "workflow",
}
_DENY_KEY_PARTS = (
    "body",
    "content",
    "description",
    "email",
    "key",
    "message",
    "name",
    "note",
    "prompt",
    "raw",
    "resume",
    "secret",
    "text",
    "token",
    "url",
)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def configured() -> dict[str, Any]:
    cfg = _load_cfg()
    return {
        "enabled_flag": _truthy(cfg.get("ALICE_POSTHOG") or os.environ.get("ALICE_POSTHOG")),
        "api_key_configured": bool(cfg.get("POSTHOG_API_KEY") or os.environ.get("POSTHOG_API_KEY")),
        "personal_api_key_configured": bool(cfg.get("POSTHOG_PERSONAL_API_KEY") or os.environ.get("POSTHOG_PERSONAL_API_KEY")),
        "project_id_configured": bool(cfg.get("POSTHOG_PROJECT_ID") or os.environ.get("POSTHOG_PROJECT_ID")),
        "host": cfg.get("POSTHOG_HOST") or os.environ.get("POSTHOG_HOST") or "sdk-default",
        "project": cfg.get("POSTHOG_PROJECT") or os.environ.get("POSTHOG_PROJECT") or "alice",
    }


def enabled() -> bool:
    info = configured()
    return bool(info["enabled_flag"] and info["api_key_configured"])


def init(component: str = "alice") -> bool:
    """Initialize PostHog if explicitly enabled and keyed. Idempotent."""
    global _CLIENT, _INITIALIZED, _ENABLED, _INIT_DETAIL, _PROJECT_API_KEY, _HOST
    if _INITIALIZED:
        return _ENABLED
    _INITIALIZED = True

    cfg = _load_cfg()
    if not _truthy(cfg.get("ALICE_POSTHOG") or os.environ.get("ALICE_POSTHOG")):
        _INIT_DETAIL = "ALICE_POSTHOG not enabled"
        return False
    key = cfg.get("POSTHOG_API_KEY") or os.environ.get("POSTHOG_API_KEY")
    if not key:
        _INIT_DETAIL = "POSTHOG_API_KEY missing"
        return False
    host = cfg.get("POSTHOG_HOST") or os.environ.get("POSTHOG_HOST") or "https://us.i.posthog.com"
    _PROJECT_API_KEY = key
    _HOST = host.rstrip("/")
    try:
        from posthog import Posthog  # type: ignore
    except Exception as e:
        _INIT_DETAIL = f"posthog SDK unavailable: {type(e).__name__}"
        return False

    try:
        _CLIENT = Posthog(
            project_api_key=key,
            host=host,
            sync_mode=True,
            privacy_mode=True,
            disable_geoip=True,
            is_server=True,
        )
        _ENABLED = True
        _INIT_DETAIL = "initialized"
        capture("alice_analytics_initialized", {"component": component})
        return True
    except Exception as e:
        _INIT_DETAIL = f"posthog init failed: {type(e).__name__}"
        _CLIENT = None
        _ENABLED = False
        return False


def status() -> dict[str, Any]:
    info = configured()
    return {
        **info,
        "sdk_importable": _sdk_importable(),
        "initialized": _INITIALIZED,
        "live": bool(_ENABLED and _CLIENT is not None),
        "detail": _INIT_DETAIL,
    }


def _sdk_importable() -> bool:
    try:
        import posthog  # noqa: F401
        return True
    except Exception:
        return False


import re as _re
# Value-level scrub: even allow-listed keys (reason/role/tool_error) can carry
# freeform values with emails/secrets/tokens. Scrub VALUES, not just key names,
# so an allow-key can never ship a credential or address to PostHog.
_VAL_EMAIL = _re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_VAL_SECRET = _re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{12,}|phc_[A-Za-z0-9]{16,}|lsv2_[A-Za-z0-9_]{12,}|"
    r"Bearer\s+[A-Za-z0-9._\-]+|[0-9]{6,}:[A-Za-z0-9_\-]{20,})")
_VAL_TOKEN = _re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")  # long opaque tokens/hashes


def _scrub_value(s: str) -> str:
    s = _VAL_EMAIL.sub("[EMAIL]", s)
    s = _VAL_SECRET.sub("[SECRET]", s)
    s = _VAL_TOKEN.sub("[TOKEN]", s)
    return s


def _safe_scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        if len(value) > 80:
            return "[REDACTED_LONG_TEXT]"
        return _scrub_value(value)
    if isinstance(value, list):
        out = []
        for item in value[:20]:
            if isinstance(item, str) and len(item) <= 80:
                out.append(_scrub_value(item))
            elif isinstance(item, bool | int | float) or item is None:
                out.append(item)
            else:
                out.append("[REDACTED]")
        return out
    return str(type(value).__name__)


def _sanitize_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (properties or {}).items():
        key_s = str(key)
        key_l = key_s.lower()
        if key_s not in _ALLOW_KEYS and any(part in key_l for part in _DENY_KEY_PARTS):
            safe[key_s] = "[REDACTED]"
            continue
        if key_s not in _ALLOW_KEYS and isinstance(value, str) and len(value) > 80:
            safe[key_s] = "[REDACTED_LONG_TEXT]"
            continue
        safe[key_s] = _safe_scalar(value)
    safe["captured_at"] = datetime.now().isoformat(timespec="seconds")
    return safe


def capture(event: str, properties: dict[str, Any] | None = None,
            distinct_id: str = "javery_local") -> bool:
    """Capture a product analytics event. Safe no-op when disabled."""
    if not _ENABLED:
        return False
    try:
        return _direct_capture(event, _sanitize_properties(properties), distinct_id)
    except Exception:
        return False


def _direct_capture(event: str, properties: dict[str, Any], distinct_id: str) -> bool:
    data = json.dumps({
        "api_key": _PROJECT_API_KEY,
        "event": event,
        "distinct_id": distinct_id,
        "properties": properties,
    }).encode("utf-8")
    req = Request(
        f"{_HOST.rstrip('/')}/capture/",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        import certifi
        import ssl

        ctx = ssl.create_default_context(cafile=certifi.where())
        with urlopen(req, timeout=15, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return resp.status < 400 and payload.get("status") == "Ok"
    except Exception:
        return False


def flush() -> bool:
    if not _ENABLED or _CLIENT is None:
        return False
    try:
        _CLIENT.flush()
        return True
    except Exception:
        return False


def query_events(event: str, *, limit: int = 10) -> dict[str, Any]:
    """Read back recent PostHog events via the REST API.

    Requires POSTHOG_PERSONAL_API_KEY (phx_...) and POSTHOG_PROJECT_ID. The
    project API key used for capture cannot query events.
    """
    cfg = _load_cfg()
    personal_key = cfg.get("POSTHOG_PERSONAL_API_KEY") or os.environ.get("POSTHOG_PERSONAL_API_KEY")
    project_id = cfg.get("POSTHOG_PROJECT_ID") or os.environ.get("POSTHOG_PROJECT_ID")
    host = (cfg.get("POSTHOG_HOST") or os.environ.get("POSTHOG_HOST") or "https://us.i.posthog.com").rstrip("/")
    missing = []
    if not personal_key:
        missing.append("POSTHOG_PERSONAL_API_KEY")
    if not project_id:
        missing.append("POSTHOG_PROJECT_ID")
    if missing:
        return {"ok": False, "missing": missing, "events": []}

    safe_limit = max(1, min(int(limit), 50))
    event_lit = event.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        "SELECT event, timestamp "
        "FROM events "
        f"WHERE event = '{event_lit}' "
        "ORDER BY timestamp DESC "
        f"LIMIT {safe_limit}"
    )
    data = json.dumps({"query": {"kind": "HogQLQuery", "query": query}}).encode("utf-8")
    req = Request(
        f"{host.replace('us.i.posthog.com', 'us.posthog.com')}/api/projects/{quote(str(project_id))}/query/",
        data=data,
        headers={"Authorization": f"Bearer {personal_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error = ""
    for attempt in range(3):
        import certifi
        import ssl

        ctx = ssl.create_default_context(cafile=certifi.where())
        try:
            with urlopen(req, timeout=25, context=ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return {"ok": False, "error": last_error, "events": [], "attempts": attempt + 1}

    if isinstance(payload, dict) and payload.get("error"):
        return {"ok": False, "error": str(payload.get("error"))[:200], "events": []}
    results = payload.get("results") if isinstance(payload, dict) else []
    events = []
    for rec in (results or [])[:safe_limit]:
        if isinstance(rec, list):
            events.append({
                "event": rec[0] if len(rec) > 0 else None,
                "timestamp": rec[1] if len(rec) > 1 else None,
            })
            continue
        if isinstance(rec, dict):
            events.append({
                "id": rec.get("id") or rec.get("uuid"),
                "event": rec.get("event"),
                "timestamp": rec.get("timestamp"),
                "distinct_id_present": bool(rec.get("distinct_id")),
            })
    return {"ok": True, "events": events, "attempts": attempt + 1}
