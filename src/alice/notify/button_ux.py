"""Inline-button confirmation UX — bot-deterministic, text-never-blocked.

Confirmations route through a recognizable code path that attaches the standard
keyboard, NOT model judgment or prose pattern-matching. The mechanism: Alice
calls the `ask_confirmation` tool (registered in tools.py). The tool writes a
pending-confirmation record here; the chat handler detects the call in
tool_calls and attaches the InlineKeyboardMarkup to her reply.

Two non-negotiable structural properties this module exists to enforce:

  1. ESCAPE HATCH — buttons never block text. The `try_resolve_by_text`
     function lets the message handler check "is there a pending
     confirmation, and does this typed reply match an option?" If yes, the
     text resolves the confirmation the same as a button tap. If no, the
     text falls through to normal route handling. There is no path where
     a pending confirmation suppresses text-mode interaction.

  2. STRUCTURED CALLBACK — button taps return a short structured payload
     (`conf:<conf_id>:<option_code>`, fits in 64-byte callback_data
     limit), NOT chat text Alice must interpret. The button press is an
     unambiguous structured signal; option resolution is server-side
     lookup by conf_id, not LLM disambiguation.

State persistence: feedback/button-confirmations.json, single-slot, via
safe_state file-locking primitives. Concurrent writes from the chat handler
and the callback handler are safe.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path
from alice import repo_paths

from alice import safe_state

_STATE = Path(repo_paths.FEEDBACK / "button-confirmations.json")
_DEFAULT_STATE = {"confirmations": {}}

# Callback-data format: 64-byte hard limit on Telegram's side. Our envelope:
# conf:<8-char-id>:<option_code>
# Leaves ~50 bytes for option_code. We enforce 30 chars max on option codes
# at registration time (validation in ask_confirmation tool); long codes
# raise rather than silently truncate.
_CALLBACK_PREFIX = "conf:"
_MAX_OPTION_CODE_LEN = 30
_CALLBACK_DATA_LIMIT = 64


def _new_conf_id() -> str:
    """Short, URL-safe random id. Doesn't need to be cryptographic; just
    needs to be collision-resistant within the active confirmation set."""
    return secrets.token_urlsafe(6)[:8]


def register(*, question: str, options: list[dict],
             chat_id: int | None = None) -> dict:
    """Register a pending confirmation. Called by the ask_confirmation tool.

    Args:
        question: the prompt text Alice wants to ask.
        options: list of {"code": str, "label": str}. code <= 30 chars,
                 label is what the button displays. 2-6 options recommended;
                 hard cap at 8.
        chat_id: the Telegram chat to send to (set later by handler if
                 None at register time — the tool doesn't know chat_id).

    Returns:
        {"conf_id": str, "callback_data_for_option": {code: payload}}.
        The handler uses these to build the InlineKeyboardMarkup.

    Raises:
        ValueError on malformed options or oversized codes — fail loud so
        the bot deterministically refuses bad button configs rather than
        silently rendering broken buttons.
    """
    if not question or not question.strip():
        raise ValueError("register: question must be non-empty")
    if not options or len(options) < 2:
        raise ValueError("register: need at least 2 options (otherwise it's not a confirmation)")
    if len(options) > 8:
        raise ValueError(f"register: too many options ({len(options)}); Telegram inline keyboard renders badly above 8")

    seen_codes = set()
    for opt in options:
        if not isinstance(opt, dict) or "code" not in opt or "label" not in opt:
            raise ValueError(f"register: each option must be {{'code', 'label'}} dict; got {opt!r}")
        code = opt["code"]
        if not code or not isinstance(code, str):
            raise ValueError(f"register: option code must be non-empty string; got {code!r}")
        if len(code) > _MAX_OPTION_CODE_LEN:
            raise ValueError(
                f"register: option code {code!r} exceeds {_MAX_OPTION_CODE_LEN} chars; "
                f"Telegram callback_data is 64 bytes total"
            )
 # Codes must be ascii-printable and not contain ':' (callback delimiter)
        if ":" in code or not all(32 <= ord(c) < 127 for c in code):
            raise ValueError(f"register: option code {code!r} must be ascii without ':' (callback delimiter)")
        if code in seen_codes:
            raise ValueError(f"register: duplicate option code {code!r}")
        seen_codes.add(code)

    conf_id = _new_conf_id()
    now = datetime.now().isoformat(timespec="seconds")

    record = {
        "conf_id":         conf_id,
        "created_at":      now,
        "question":        question.strip(),
        "options":         options,
        "status":          "pending",
        "resolved_choice": None,
        "resolved_at":     None,
        "chat_id":         chat_id,
        "message_id":      None,
    }

    def mutator(state):
        state = state or dict(_DEFAULT_STATE)
        state.setdefault("confirmations", {})
        state["confirmations"][conf_id] = record
        return state, None

    safe_state.atomic_update(_STATE, mutator, default=dict(_DEFAULT_STATE))

 # Build the callback_data per option. The handler uses these to build the
 # inline keyboard.
    callback_for = {}
    for opt in options:
        payload = f"{_CALLBACK_PREFIX}{conf_id}:{opt['code']}"
 # Sanity — should never trip given our validation, but fail loud
 # rather than ship a payload Telegram will silently truncate.
        if len(payload.encode("utf-8")) > _CALLBACK_DATA_LIMIT:
            raise RuntimeError(
                f"register: built callback payload {payload!r} exceeds "
                f"{_CALLBACK_DATA_LIMIT} bytes (Telegram limit). This should "
                f"have been caught by the option-code length check above."
            )
        callback_for[opt["code"]] = payload

    return {
        "conf_id":                   conf_id,
        "question":                  record["question"],
        "options":                   options,
        "callback_data_for_option":  callback_for,
    }


def attach_message(conf_id: str, chat_id: int, message_id: int) -> None:
    """Record which message the keyboard was attached to. Used by the
    callback handler to edit the original message after the button is
    tapped (so the resolved state is visible in chat history)."""
    def mutator(state):
        state = state or dict(_DEFAULT_STATE)
        confs = state.setdefault("confirmations", {})
        if conf_id in confs:
            confs[conf_id]["chat_id"] = chat_id
            confs[conf_id]["message_id"] = message_id
        return state, None
    safe_state.atomic_update(_STATE, mutator, default=dict(_DEFAULT_STATE))


def get_pending() -> list[dict]:
    """All pending (unresolved) confirmations. Used by the message handler
    to check whether a typed reply might be resolving a button question."""
    state = safe_state.atomic_read(_STATE, default=dict(_DEFAULT_STATE))
    confs = state.get("confirmations", {})
    return [c for c in confs.values() if c.get("status") == "pending"]


def get(conf_id: str) -> dict | None:
    state = safe_state.atomic_read(_STATE, default=dict(_DEFAULT_STATE))
    return state.get("confirmations", {}).get(conf_id)


def resolve_by_callback(callback_data: str) -> dict | None:
    """Resolve a confirmation from a button tap's callback_data.

    Returns the resolved confirmation record, OR None if the callback_data
    is malformed, the conf_id is unknown, or the confirmation was already
    resolved (idempotent — double-taps don't double-resolve).
    """
    if not callback_data or not callback_data.startswith(_CALLBACK_PREFIX):
        return None
    parts = callback_data.split(":", 2)
    if len(parts) != 3:
        return None
    _, conf_id, option_code = parts
    return _do_resolve(conf_id, option_code, source="button_tap")


def try_resolve_by_text(user_text: str) -> dict | None:
    """ESCAPE HATCH — the non-negotiable property. If there's a pending
    confirmation AND the user's typed reply matches an option (by code or
    by label, case-insensitive substring), resolve via that match. If no
    match, return None and the caller treats it as a regular message
    (buttons did NOT block text input).

    This function is intentionally PERMISSIVE on matching: 'yes' matches
    a Yes button even if the official option code is 'confirm'. 'a' matches
    the first option if options are unlabeled. The principle: any plausible
    text match resolves; otherwise pass through. Typed text NEVER falls
    into a black hole because of a pending confirmation.
    """
    if not user_text or not user_text.strip():
        return None
    pending = get_pending()
    if not pending:
        return None

    text = user_text.strip().lower()

 # Most-recent pending first — Jordan's most likely intent
    pending_sorted = sorted(pending, key=lambda c: c.get("created_at", ""), reverse=True)

    for conf in pending_sorted:
        options = conf.get("options") or []
 # Pass 1: exact-token match (option code or label, case-insensitive)
        for opt in options:
            code = opt.get("code", "").lower()
            label = opt.get("label", "").lower()
            if text == code or text == label:
                resolved = _do_resolve(conf["conf_id"], opt["code"], source="text_match_exact")
                if resolved:
                    return resolved
 # Pass 2: yes/no convenience aliases
        if text in ("yes", "y", "yeah", "yep", "ok", "okay", "sure", "confirm", "go"):
            for opt in options:
                if any(k in opt.get("label", "").lower() for k in ("yes", "confirm", "ok", "proceed", "go ahead")):
                    return _do_resolve(conf["conf_id"], opt["code"], source="text_match_yes_alias")
        if text in ("no", "n", "nope", "cancel", "stop", "hold off"):
            for opt in options:
                if any(k in opt.get("label", "").lower() for k in ("no", "cancel", "stop", "hold")):
                    return _do_resolve(conf["conf_id"], opt["code"], source="text_match_no_alias")
 # Pass 3: numeric position match (1/2/3 for first/second/third option)
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(options):
                return _do_resolve(conf["conf_id"], options[idx]["code"], source="text_match_positional")
 # Pass 4: substring match on option label
        for opt in options:
            label = opt.get("label", "").lower()
            if label and len(text) >= 3 and text in label:
                return _do_resolve(conf["conf_id"], opt["code"], source="text_match_substring")

    return None


def _do_resolve(conf_id: str, option_code: str, source: str) -> dict | None:
    """Mark the confirmation resolved. Idempotent — re-resolving returns
    None to signal "no new resolution to act on" (prevents double-execution
    on rapid double-tap)."""
    result_holder = {}

    def mutator(state):
        state = state or dict(_DEFAULT_STATE)
        confs = state.setdefault("confirmations", {})
        conf = confs.get(conf_id)
        if conf is None:
            return state, None
        if conf.get("status") != "pending":
 # Already resolved (idempotent on double-tap)
            return state, None
 # Validate the option code is actually one of the registered options
        valid_codes = {o["code"] for o in conf.get("options", [])}
        if option_code not in valid_codes:
            return state, None
        conf["status"] = "resolved"
        conf["resolved_choice"] = option_code
        conf["resolved_at"] = datetime.now().isoformat(timespec="seconds")
        conf["resolved_via"] = source
        result_holder["conf"] = dict(conf)
        return state, None

    safe_state.atomic_update(_STATE, mutator, default=dict(_DEFAULT_STATE))
    return result_holder.get("conf")


def cancel_stale(max_age_minutes: int = 60) -> int:
    """Expire pending confirmations older than max_age_minutes. Called
    opportunistically (e.g., at start of each message_handler invocation)
    so stale confirmations don't accumulate. Returns count cancelled.

    Confirmations are persistent by default; only expire where a stale tap
    would do something wrong. This cleans up records, not the buttons
    themselves — the buttons remain visible (resolution just returns None
    and the handler treats it as a no-op)."""
    cutoff = datetime.now().timestamp() - (max_age_minutes * 60)
    cancelled_count = [0]

    def mutator(state):
        state = state or dict(_DEFAULT_STATE)
        confs = state.setdefault("confirmations", {})
        for conf_id, conf in list(confs.items()):
            if conf.get("status") != "pending":
                continue
            try:
                created = datetime.fromisoformat(conf.get("created_at", "")).timestamp()
            except (ValueError, TypeError):
                continue
            if created < cutoff:
                conf["status"] = "expired"
                conf["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                cancelled_count[0] += 1
        return state, None

    safe_state.atomic_update(_STATE, mutator, default=dict(_DEFAULT_STATE))
    return cancelled_count[0]


# ─── option-set conveniences (Alice can pass standard option-sets through tool) ─

YES_NO = [
    {"code": "yes", "label": "Yes"},
    {"code": "no",  "label": "No"},
]

PROCEED_HOLD = [
    {"code": "proceed", "label": "Proceed"},
    {"code": "hold",    "label": "Hold off"},
]


def build_inline_keyboard(callback_data_for_option: dict, option_labels: list[dict]):
    """Construct an InlineKeyboardMarkup. Lazy-imports python-telegram-bot
    so this module is importable without the lib (the tool can register
    state even in environments where the bot lib isn't installed)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
 # Layout: 2 per row if 4+ options, 1 per row if 2-3 options (more readable)
    n = len(option_labels)
    cols = 2 if n >= 4 else 1
    buttons = []
    row = []
    for opt in option_labels:
        row.append(InlineKeyboardButton(
            text=opt["label"],
            callback_data=callback_data_for_option[opt["code"]],
        ))
        if len(row) == cols:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)
