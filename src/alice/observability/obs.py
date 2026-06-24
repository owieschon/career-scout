"""Observability shim around Sentry.

Wraps sentry_sdk so callers can `from obs import capture, capture_message, init`
unconditionally. If SENTRY_DSN is not set in config.env (or sentry_sdk is not
installed), every function is a no-op. Activation is the operator's separate step —
this module never creates a project or generates a DSN.

Usage:
    from alice.observability import obs
    obs.init("telegram_bot")         # idempotent; once per process

    try:
        risky_thing()
    except Exception as e:
        obs.capture(e, where="source_deep:greenhouse", payload={...})

    obs.capture_message("correction", level="warning", extras={...})

Design: code wired NOW, activation later. If DSN appears in config, the wiring
becomes live without any code changes.

Security: before_send + before_breadcrumb scrubbers run on every Sentry
event/breadcrumb before it leaves the process. The primary target is the
Telegram bot token, which httpx captures verbatim in request URLs; the scrubber
also catches common secret patterns (Bearer tokens, lsv2_ tokens, sk- API keys)
defensively. The scrubber is additive-only on the event dict — it never raises
into the Sentry SDK path.
"""
import os
import re
from alice.jobcfg import load as _load_cfg
from alice import repo_paths


# ─── Secret / PII scrubber ────────────────────────────────────────────────────

# Applied in before_send and before_breadcrumb. Walks every string-valued leaf
# we know Sentry captures (request URLs, breadcrumb message/data, span
# descriptions, exception values). Scrubs in place — returns the modified event.

# Patterns ordered from most-specific (telegram token) to least (generic token).
_SCRUB_PATTERNS: list[tuple[re.Pattern, str]] = [
 # Telegram bot token in request URL: /bot<token>/ /bot[REDACTED]/
    (re.compile(r"(api\.telegram\.org/bot)[A-Za-z0-9_\-:]{20,}(/)",
                re.IGNORECASE),
     r"\1[REDACTED]\2"),
 # Telegram token as a bare value (e.g. in headers/data): bot<token>
    (re.compile(r"\bbot([0-9]{8,12}:[A-Za-z0-9_\-]{30,})\b"),
     "bot[REDACTED]"),
 # Bearer tokens in Authorization headers.
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-_.~+/]{16,}(=*)",
                re.IGNORECASE),
     r"\1[REDACTED]"),
 # LangSmith / Vercel / generic lsv2_ tokens.
    (re.compile(r"\blsv2_[A-Za-z0-9_\-]{16,}\b"),
     "lsv2_[REDACTED]"),
 # Anthropic / OpenAI API keys.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}\b"),
     "sk-ant-[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{32,}\b"),
     "sk-[REDACTED]"),
]


def _scrub_string(s: str) -> str:
    """Apply all scrub patterns to a string. Returns the scrubbed value."""
    for pat, repl in _SCRUB_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _scrub_value(v):
    """Scrub a leaf value (str) or recursively walk dict/list. Non-str leaves
    are returned as-is. Never raises."""
    try:
        if isinstance(v, str):
            return _scrub_string(v)
        if isinstance(v, dict):
            return {k: _scrub_value(val) for k, val in v.items()}
        if isinstance(v, list):
            return [_scrub_value(item) for item in v]
    except Exception:
        pass
    return v


def _scrub_event(event: dict, hint=None) -> dict:
    """before_send hook: scrub the entire event dict in-place where needed,
    then return it. Returning None would DROP the event — we never do that here;
    the scrubber only redacts, never suppresses (except for the benign bot-restart
    ConnectError handled separately).

    Also suppresses the benign bot-restart httpx.ConnectError so it
    stops cluttering the Issues feed. A ConnectError from the bot's own teardown
    is structurally noise — the bot reconnects on the next poll cycle.
    """
    try:
 # ── Suppress benign bot-restart httpx.ConnectError ────────────────────
 # Identify by exception type name only — don't match on message content
 # (which could change). If it's ConnectError from httpx and the url looks
 # like the Telegram long-poll endpoint, drop it.
        exceptions = event.get("exception", {}).get("values", [])
        for exc in exceptions:
            if exc.get("type") == "ConnectError":
 # Check if any frame or the exception value references Telegram.
                exc_val = exc.get("value", "") or ""
                if "api.telegram.org" in exc_val or "getUpdates" in exc_val:
                    return None  # suppress: benign bot-restart teardown noise
 # Also suppress if the httpx module is in the stacktrace.
                frames = (exc.get("stacktrace") or {}).get("frames") or []
                for frame in frames:
                    module = frame.get("module") or ""
                    filename = frame.get("filename") or ""
                    if "httpx" in module or "httpx" in filename:
 # Only suppress if telegram is in the exception value
 # (avoid suppressing legitimate httpx errors elsewhere).
                        if "telegram" in exc_val.lower() or "getUpdates" in (event.get("request") or {}).get("url", ""):
                            return None

 # ── Scrub request URL ────────────────────────────────────────────────────
        req = event.get("request")
        if isinstance(req, dict):
            if isinstance(req.get("url"), str):
                req["url"] = _scrub_string(req["url"])
 # Scrub headers dict values too.
            headers = req.get("headers")
            if isinstance(headers, dict):
                req["headers"] = {k: _scrub_value(v) for k, v in headers.items()}
 # Scrub data/body.
            if req.get("data"):
                req["data"] = _scrub_value(req["data"])

 # ── Scrub exception values/messages ─────────────────────────────────────
        for exc in exceptions:
            if isinstance(exc.get("value"), str):
                exc["value"] = _scrub_string(exc["value"])
 # Scrub frame locals and extra context.
            frames = (exc.get("stacktrace") or {}).get("frames") or []
            for frame in frames:
                if isinstance(frame.get("vars"), dict):
                    frame["vars"] = {k: _scrub_value(v) for k, v in frame["vars"].items()}

 # ── Scrub breadcrumbs ────────────────────────────────────────────────────
        breadcrumbs = event.get("breadcrumbs", {})
        if isinstance(breadcrumbs, dict):
            values = breadcrumbs.get("values") or []
            for crumb in values:
                if isinstance(crumb, dict):
                    if isinstance(crumb.get("message"), str):
                        crumb["message"] = _scrub_string(crumb["message"])
                    if isinstance(crumb.get("data"), dict):
                        crumb["data"] = {k: _scrub_value(v) for k, v in crumb["data"].items()}

 # ── Scrub span/transaction data (extra, tags, contexts) ─────────────────
        for top_key in ("extra", "tags", "contexts"):
            section = event.get(top_key)
            if isinstance(section, dict):
                event[top_key] = {k: _scrub_value(v) for k, v in section.items()}

 # ── Scrub logentry (capture_message path) ────────────────────────────────
        logentry = event.get("logentry")
        if isinstance(logentry, dict):
            if isinstance(logentry.get("message"), str):
                logentry["message"] = _scrub_string(logentry["message"])
            if isinstance(logentry.get("formatted"), str):
                logentry["formatted"] = _scrub_string(logentry["formatted"])

    except Exception:
 # NEVER raise from before_send — that would silently drop the event.
        pass
    return event


def _scrub_breadcrumb(crumb: dict, hint=None) -> dict:
    """before_breadcrumb hook: scrub any Telegram token out of breadcrumb
    message and data fields before they're stored in the event buffer.
    Returns the scrubbed crumb. Never raises. Never returns None (that would
    drop the breadcrumb entirely which could hide useful non-secret context)."""
    try:
        if not isinstance(crumb, dict):
            return crumb
        if isinstance(crumb.get("message"), str):
            crumb["message"] = _scrub_string(crumb["message"])
        if isinstance(crumb.get("data"), dict):
            crumb["data"] = {k: _scrub_value(v) for k, v in crumb["data"].items()}
 # category field may carry a URL-like value (e.g. httpx breadcrumbs).
        if isinstance(crumb.get("category"), str):
            crumb["category"] = _scrub_string(crumb["category"])
    except Exception:
        pass
    return crumb

try:
    import sentry_sdk  # type: ignore
    _SENTRY_AVAILABLE = True
except ImportError:
    sentry_sdk = None
    _SENTRY_AVAILABLE = False

_INITIALIZED = False


def _dsn():
    cfg = _load_cfg()
    return cfg.get("SENTRY_DSN") or os.environ.get("SENTRY_DSN")


def available():
    """True iff sentry_sdk is installed AND a DSN is configured."""
    return _SENTRY_AVAILABLE and bool(_dsn())


def init(component: str = "alice"):
    """Initialize sentry_sdk if DSN is present. Idempotent. No-op otherwise."""
    global _INITIALIZED
    if _INITIALIZED:
        return False
    if not available():
        _INITIALIZED = True  # don't re-check every call
        return False
    try:
        sentry_sdk.init(
            dsn=_dsn(),
            send_default_pii=False,
 # Tracing enabled for the span-attributes path: standalone Metrics is
 # sunset on this Sentry instance, so spans + events are the surfaces
 # that actually receive. Sample at 1.0 while validating
 # instrumentation; tune down if volume becomes a cost issue, but
 # verify the alert rules still fire at the lower rate first.
            traces_sample_rate=float(os.environ.get("ALICE_TRACES_RATE", "1.0")),
            environment=os.environ.get("ALICE_ENV", "production"),
            release=os.environ.get("ALICE_RELEASE"),
 # Scrub the Telegram bot token (and other secrets) before any event
 # or breadcrumb leaves the process. Closes the token-leak path where
 # httpx captures POST https://api.telegram.org/bot<TOKEN>/... verbatim
 # into Sentry span/breadcrumb data. Also suppresses the benign
 # bot-restart httpx.ConnectError to reduce noise.
            before_send=_scrub_event,
            before_breadcrumb=_scrub_breadcrumb,
        )
        sentry_sdk.set_tag("component", component)
        _INITIALIZED = True
        return True
    except Exception as e:
        print(f"[obs.init failed: {e}]")
        _INITIALIZED = True
        return False


def capture(exc: BaseException, where: str = "", payload: dict | None = None):
    """Capture an exception. No-op if Sentry not available."""
    if not available():
        return False
    try:
        with sentry_sdk.push_scope() as scope:
            if where:
                scope.set_tag("where", where)
            if payload:
                for k, v in payload.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
        return True
    except Exception:
        return False


def capture_message(msg: str, level: str = "info", extras: dict | None = None,
                    where: str = ""):
    """Capture a message (e.g. a correction log entry). No-op if not available."""
    if not available():
        return False
    try:
        with sentry_sdk.push_scope() as scope:
            if where:
                scope.set_tag("where", where)
            if extras:
                for k, v in extras.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_message(msg, level=level)
        return True
    except Exception:
        return False


def start_turn_span(task: str, attrs: dict | None = None):
    """Start a transaction span for one Alice chat/email turn.

    Use as a context manager. The span carries the per-turn instrumentation
    (tool_calls_count, tool_names, model, tier, rounds, fabrication_flagged)
    set via set_turn_attrs / span.set_data inside the block.

    Returns a context manager. No-op if Sentry not available.

    op="alice.turn" — Sentry surfaces this as a transaction and the attributes
    are queryable in the spans dataset, which this instance receives end-to-end.
    """
    if not available():
        class _Noop:
            def __enter__(self): return None
            def __exit__(self, *args): return False
            def set_data(self, *a, **kw): return None
            def set_tag(self, *a, **kw): return None
        return _Noop()
    try:
        tx = sentry_sdk.start_transaction(op="alice.turn", name=f"alice.turn.{task}")
 # set_tag is best-effort here since the transaction may not be fully
 # entered until __enter__ — but the SDK accepts pre-enter tags.
        if attrs:
            for k, v in attrs.items():
                try:
                    tx.set_data(k, v)
                except Exception:
                    pass
        return tx
    except Exception:
        class _Noop:
            def __enter__(self): return None
            def __exit__(self, *args): return False
            def set_data(self, *a, **kw): return None
            def set_tag(self, *a, **kw): return None
        return _Noop()


def _grounding_fallback(kind, summary, payload, fingerprint_extra, reason):
    """Persist a grounding event that did NOT reach Sentry (stderr + a local JSONL
    log) so the fabrication-detection surface can't fail silently. A safety
    surface that swallows its own dispatch failures blinds the operator to the
    exact fabrication class it exists to catch."""
    import sys
    import json
    import datetime
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": kind, "summary": summary, "fingerprint_extra": fingerprint_extra,
        "undispatched_reason": reason, "payload": payload,
    }
    print(f"[grounding-flag UNDISPATCHED:{reason}] {kind}: {summary}", file=sys.stderr)
    try:
        path = os.path.join(str(repo_paths.ROOT),
                            "state", "grounding_flags_fallback.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass  # stderr already carried it; never raise from the safety path


def flag_grounding_event(kind: str, summary: str, payload: dict,
                          fingerprint_extra: str | None = None,
                          level: str = "warning") -> bool:
    """Emit a grounding-violation event (e.g. category-mismatch fabrication,
    specific-claims-without-tools). Lands in Issues feed with a stable
    fingerprint so it groups across recurrences.

    `kind` is the category ("category_mismatch", "claims_without_tools").
    The fingerprint = ["alice.grounding", kind, fingerprint_extra or "default"]
    so each kind groups as its own issue.

    Returns True if dispatched to Sentry, False otherwise. On False (Sentry
    unavailable OR a dispatch error) the event is written to a local fallback log
    + stderr so a grounding violation is never silently lost.
    """
    if not available():
        _grounding_fallback(kind, summary, payload, fingerprint_extra,
                            reason="sentry_unavailable")
        return False
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("alice.grounding_kind", kind)
            scope.set_tag("alice.surface", "telegram_chat")
            for k, v in (payload or {}).items():
                try:
                    scope.set_extra(k, v)
                except Exception:
                    pass
            scope.fingerprint = ["alice.grounding", kind, fingerprint_extra or "default"]
            sentry_sdk.capture_message(summary, level=level)
        return True
    except Exception as e:
        _grounding_fallback(kind, summary, payload, fingerprint_extra,
                            reason=f"dispatch_error:{type(e).__name__}")
        return False


def monitor_cron(slug: str):
    """Decorator/context-manager for cron-monitor on a step. No-op if not available.
    Use as: with obs.monitor_cron("daily-run"): ..."""
    if not available():
 # Return a dummy context manager
        class _Noop:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        return _Noop()
    try:
        return sentry_sdk.monitor(monitor_slug=slug)
    except Exception:
        class _Noop:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        return _Noop()
