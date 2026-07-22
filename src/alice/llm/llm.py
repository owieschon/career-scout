"""LLM wrapper for Alice — single chokepoint for every Anthropic API call.

Handles: model selection, API call, retry, token + cost logging, budget guardrail.

Cost log (`feedback/time-cost-log.jsonl`) is append-only; every call writes one line.
Daily budget cap: $2.00 (soft — Alice surfaces if approaching).
Weekly budget cap: $14.00.

Model defaults are pinned per task in MODEL_FOR_TASK below. Override at call site
with `model=` if needed. Task names should match keys in MODEL_FOR_TASK so cost
analytics roll up cleanly.
"""
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from alice.jobcfg import load

# Tracing is optional and self-disabling. The tracer is a no-op unless an entry
# point called telemetry.init_tracing() with ALICE_TRACING=1, so importing this
# is free and never changes behavior. set_attr() routes every attribute through
# redaction and never raises; redact() is used directly for span events.

# Reference telemetry.tracer DYNAMICALLY (not `from telemetry import tracer`):
# init_tracing() REASSIGNS telemetry.tracer, so a name bound at import time
# stays the no-op tracer even after init runs — making live capture silently
# empty (the worst failure for an instrument). `import telemetry` +
# telemetry.tracer.start_as_current_span(...) at call time is import-order
# independent. set_attr/redact are functions (never reassigned) — safe to bind.
from alice.observability import telemetry
from alice.observability.telemetry import set_attr, redact

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

import os
from alice import repo_paths

_REPO = repo_paths.ROOT   # portable repo root (not hardcoded)
_LOG = _REPO / "feedback" / "time-cost-log.jsonl"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Failsafe cap on multi-turn tool roundtrips. 8 covers any realistic Alice
# workflow (read sheet read file compose response done is 2 rounds);
# hitting the cap is a fail-loud signal that something is looping wrong.
_MAX_TOOL_ROUNDTRIPS = 8

# Per-million-token pricing for cost estimation. Cached locally; auditable
# against Anthropic billing. Update when prices change.
PRICING = {
    "claude-haiku-4-5-20251001":  {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6":          {"in": 3.00, "out": 15.00},
    "claude-opus-4-7":            {"in": 5.00, "out": 25.00},
    "claude-opus-4-8":            {"in": 5.00, "out": 25.00},
 # ── OpenRouter-routed non-Anthropic candidates ──────────────────────────
 # Pricing per million tokens via OpenRouter's public rate card.
 # "in" = prompt, "out" = completion. Subject to OpenRouter markup.
    "openai/gpt-4o":                         {"in": 2.50,   "out": 10.00},
    "openai/gpt-4o-mini":                    {"in": 0.15,   "out": 0.60},
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": {"in": 0.30, "out": 0.60},
    "google/gemini-2.5-flash":               {"in": 0.15,   "out": 0.60},
    "google/gemini-2.5-pro":                 {"in": 1.25,   "out": 10.00},
    "meta-llama/llama-3.3-70b-instruct":     {"in": 0.12,   "out": 0.30},
}

# ─── OpenRouter provider path ─────────────────────────────────────────────────

# OpenRouter exposes an OpenAI-compatible REST API at /api/v1/chat/completions.
# The Anthropic-native path (_http_call_once + /v1/messages) is independent of
# this — every Anthropic call flows through it. This is a second path for
# non-Anthropic models routed via OpenRouter.

# Provider routing preferences (data policy + US hosting):
# - data_collection: "deny" no-retain/no-train policy; essential for
# resume PII, JD bodies, and anything flowing through fit_judge.
# - providers: US-hosted backends only. The preference list below names the
# OpenRouter-supported US providers for each model class. OpenRouter falls
# back within the preference order; if no US provider is available the call
# fails-loud rather than silently routing to a non-US endpoint.

# The live routing table in TIER_FOR_TASK / MODEL_FOR_TASK does not route here.
# This path is used by model_sweep.py to benchmark candidates; routing the live
# tasks here is a separate, gated change the operator makes after reviewing the
# benchmark results.

_OR_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# US-hosting provider preferences per model family (OpenRouter provider labels).
# Listed in preference order. Empty list = no constraint beyond data_collection.
_OR_PROVIDER_PREFS: dict[str, list[str]] = {
    "openai/gpt-4o":                         ["OpenAI"],
    "openai/gpt-4o-mini":                    ["OpenAI"],
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": ["DeepInfra", "Together"],
    "google/gemini-2.5-flash":               ["Google"],
    "google/gemini-2.5-pro":                 ["Google"],
    "meta-llama/llama-3.3-70b-instruct":     ["Together", "DeepInfra", "Fireworks"],
}

# Model IDs that belong to the OpenRouter provider path. Keyed for O(1) lookup.
_OR_MODELS: frozenset[str] = frozenset(_OR_PROVIDER_PREFS.keys())


def _infer_provider(model: str) -> str:
    """Return 'openrouter' if model id belongs to the OR set, else 'anthropic'.

    This is the single routing gate. Call it ONCE at the start of llm.call so
    the branch is deterministic and loggable. Unknown models default to 'anthropic'
    (fail-open toward the known-working path, not toward an unknown provider).
    """
    return "openrouter" if model in _OR_MODELS else "anthropic"


def _build_or_payload(model: str, messages: list, system: str | None,
                      max_tokens: int, temperature: float) -> dict:
    """Build an OpenAI-shaped payload for OpenRouter.

    OpenRouter speaks the OpenAI chat/completions API. System prompt is passed
    as the first message with role='system'. Provider preferences (US hosting +
    no-retain data policy) are injected into the payload's 'provider' key.

    Extended thinking is NOT supported via OpenRouter for non-Anthropic models —
    callers must pass effort=None for OR-routed models. _http_call_or raises if
    effort is non-None and the model is OR-routed (enforce at call site).
    """
    chat_messages = []
    if system:
        chat_messages.append({"role": "system", "content": system})
    chat_messages.extend(messages)

    prefs = _OR_PROVIDER_PREFS.get(model, [])
    provider_block: dict = {"data_collection": "deny"}
    if prefs:
        provider_block["order"] = prefs
 # require_parameters: only route to providers that honor our preferences.
 # This makes the call fail rather than silently route to a non-US endpoint.
        provider_block["allow_fallbacks"] = True  # still falls back within prefs list

    payload: dict = {
        "model":       model,
        "messages":    chat_messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "provider":    provider_block,
    }
    return payload


def _http_call_or(payload: dict, key: str, retries: int = 2,
                  retry_delay: float = 2.0) -> tuple[dict, float]:
    """One HTTPS round-trip to OpenRouter /v1/chat/completions.

    Returns (response_body_dict, latency_seconds). Raises RuntimeError on
    exhaustion or non-retriable error. Response is translated into the same
    shape as _http_call_once's return so the caller doesn't branch post-call:
      {usage: {input_tokens, output_tokens}, content: [{type, text}], stop_reason}
    """
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                _OR_API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://cadence-analytics.example.com",   # OpenRouter attribution
                    "X-Title":      "Alice (job-search agent)",
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, context=_SSL, timeout=120) as r:
                raw = json.loads(r.read())
                latency = time.time() - t0
 # Translate OpenAI-shaped response internal shape used by llm.call.
 # OpenAI: {choices: [{message: {content}, finish_reason}], usage: {prompt_tokens, completion_tokens}}
 # Internal: {content: [{type, text}], stop_reason, usage: {input_tokens, output_tokens}}
                choices = raw.get("choices") or []
                text_content = ""
                stop_reason = None
                if choices:
                    msg = choices[0].get("message") or {}
                    text_content = msg.get("content") or ""
                    stop_reason = choices[0].get("finish_reason")
                usage_raw = raw.get("usage") or {}
                translated = {
                    "content": [{"type": "text", "text": text_content}],
                    "stop_reason": stop_reason,
                    "usage": {
                        "input_tokens":  usage_raw.get("prompt_tokens", 0),
                        "output_tokens": usage_raw.get("completion_tokens", 0),
                    },
                }
                return translated, latency
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:300]
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code in (429, 503, 529) or e.code >= 500:
                if attempt < retries:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            raise RuntimeError(f"OpenRouter API failed: {last_err}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise RuntimeError(f"OpenRouter API failed: {last_err}")
    raise RuntimeError(f"OpenRouter API exhausted retries: {last_err}")


def _guard_tool_result_text(tool_name: str, result) -> str:
    text = str(result) if result is not None else ""
    try:
        from alice import ai_guardrails
        guarded, finding = ai_guardrails.annotate_untrusted_text(text, source=f"tool:{tool_name}")
        if finding.flagged:
            try:
                from alice.observability import obs
                obs.capture_message(
                    "alice.security.prompt_injection_tool_result",
                    level="warning",
                    where="llm:tool_result",
                    extras=ai_guardrails.sentry_payload(finding, surface=f"tool:{tool_name}"),
                )
            except Exception:
                pass
        return guarded
    except Exception:
        return text

# Per-task daily cost tripwires. When today's logged cost for a task crosses
# its threshold, fires a Sentry warning (via obs.capture_message) ONCE per
# task per day. Does NOT hard-block — Alice keeps running.
TASK_DAILY_TRIPWIRES = {
    "telegram_chat": 5.00,   # ~$150/mo if sustained — the "scary" projection
}
_ALERT_STATE = _REPO / "feedback" / "cost-alert-state.json"
CALL_ANOMALY_THRESHOLDS = {
    "telegram_chat": {
        "in_tokens": 80_000,
        "out_tokens": 20_000,
        "cost_usd": 1.00,
    },
}
OPENROUTER_FALLBACK_MODEL = "openai/gpt-4o-mini"


# ─── Dynamic model / effort / thinking selection ─────────────────────────────

# Three-tier control surface. Each tier picks a model AND a thinking budget.
# Selection is driven by the task key (a code-set string, not LLM-decided)
# mapped to a tier. The heuristic is deterministic and checkable; we don't ask
# the LLM "is this complex?" because meta-reasoning over its own model choice is
# a high-confabulation-risk surface.

# Tier rationale:
# - conversational / simple / frequent paths run on Haiku; a bigger model
# gives no measurable lift there.
# - resume/cover_letter run on Opus and produce visibly sharper output.
# - the adversarial-critic role needs the most thoughtful model with thinking
# on, so it falsifies against ground truth rather than co-signing.

# Every call's tier + selection + outcome lands in the cost log (extras field)
# so the policy is auditable: are cheap-tier outputs being corrected? does the
# expensive tier earn its cost?

TIER_CHEAP = {
    "model": "claude-haiku-4-5-20251001",
    "effort": None,                  # no extended thinking
}
TIER_MEDIUM = {
    "model": "claude-sonnet-4-6",
    "effort": None,                  # off by default; explicit effort only when caller asks
}
TIER_EXPENSIVE = {
    "model": "claude-opus-4-8",
    "effort": "high",                # extended thinking on for complex reasoning + critic
}

# Per-model thinking-API shape:
# classic payload key is `thinking: {type: "enabled", budget_tokens: N}`,
# with N < max_tokens. Supported on Haiku 4.5 and Sonnet 4.6.
# adaptive payload key is `thinking: {type: "adaptive"}` plus
# `output_config: {effort: "high"|"medium"|"low"}`. Supported
# on Sonnet 4.6, Opus 4.7, and Opus 4.8.
# none thinking not supported at all.

# Sonnet 4.6 accepts both shapes; we prefer adaptive for consistency with
# the Opus models, so callers can think in tier-level "effort" without
# caring which shape the wire uses.
THINKING_API = {
    "claude-haiku-4-5-20251001": "classic",
    "claude-sonnet-4-6":         "adaptive",
    "claude-opus-4-7":           "adaptive",
    "claude-opus-4-8":           "adaptive",
}

# Classic-API budget mapping per effort level. Caller asks for "high"; the
# wire layer translates to a concrete budget_tokens count for classic-API
# models. Adaptive-API models pass the effort string through unchanged.
_CLASSIC_BUDGET_FOR_EFFORT = {"low": 1024, "medium": 2048, "high": 4096}

# Task tier. State-read / simple-frequent cheap. Multi-source synthesis
# medium. Strategic creative output OR adversarial critic expensive.
TIER_FOR_TASK = {
 # Cheap: conversational, daily-volume, short-output. No measurable lift
 # from a bigger model on these paths.
    "confirm_understanding":   "cheap",
    "triage_observation":      "cheap",
    "focus_distraction_flag":  "cheap",
    "focus_disengagement":     "cheap",
    "behavior_pattern":        "cheap",
    "thank_you_note":          "cheap",
    "morning_reminder":        "cheap",
    "targeted_questions":      "cheap",
    "hypothesis_classify":     "cheap",
    "debrief_capture":         "cheap",
    "fit_hypothesis":          "cheap",
    "telegram_chat":           "cheap",
    "observability_judged_eval": "cheap",
 # Ambient experience-capture detector (Haiku review of recent operator
 # turns). Tuned LIBERAL — false-negatives at capture cost more than
 # false-positives (the operator prunes in the morning digest).
    "experience_ambient_review": "cheap",
 # A/B disambiguation semantic backstop (Haiku classifier for prior-
 # question shapes the regex layer didn't catch). Biased toward
 # AMBIGUOUS — false-positive = mild annoyance, false-negative = the
 # default-to-first bug this gate exists to prevent.
    "ab_disambiguation_check": "cheap",

 # Medium: multi-source research synthesis, structured prep. Visible quality
 # lift over Haiku.
    "outreach_draft":          "medium",
    "interview_prep":          "medium",
    "negotiation_prep":        "medium",
    "application_strategy":    "medium",
    "company_deep_dive":       "medium",
    "weekly_scorecard":        "medium",
 # Structured profile extraction from resume/chat/voice. Grounded JSON
 # extraction — quality matters (a wrong field corrupts the whole search),
 # but it's bounded structured work, not creative output.
    "profile_extraction":      "medium",

 # Expensive: strategic / creative output that survives the operator sending it,
 # or the adversarial critic role (falsify against ground truth).
    "resume_draft":            "expensive",
 # Derive a track-tailored resume variant from the uploaded resume.
 # Resume-quality output the user sends — same tier as resume_draft.
    "resume_variant_derive":   "expensive",
    "cover_letter_draft":      "expensive",
    "adversarial_critic":      "expensive",
    "complex_reasoning":       "expensive",   # explicit-override task key
}

# Back-compat — some callers still reference MODEL_FOR_TASK directly. Derive
# from TIER_FOR_TASK so the two stay consistent automatically.
_TIER_TO_CONFIG = {"cheap": TIER_CHEAP, "medium": TIER_MEDIUM, "expensive": TIER_EXPENSIVE}
MODEL_FOR_TASK = {
    task: _TIER_TO_CONFIG[tier]["model"]
    for task, tier in TIER_FOR_TASK.items()
}
# Pin resume + cover drafts to opus-4-7 until opus-4-8 is validated against the
# resume test suite.
MODEL_FOR_TASK["resume_draft"]       = "claude-opus-4-7"
MODEL_FOR_TASK["cover_letter_draft"] = "claude-opus-4-7"


def select_call_config(task: str, *, override_model: str | None = None,
                       override_effort: str | None = None,
                       override_tier: str | None = None) -> dict:
    """Pick model + thinking effort for a task. Returns:
        {"model": str, "effort": str | None, "tier": str, "source": str}

    `effort` is None (no extended thinking), "low", "medium", or "high".
    The wire layer (_build_thinking_payload) translates effort to the per-
    model API shape (classic budget_tokens vs adaptive output_config).

    `source` is 'override_model', 'override_tier', 'task_map', or 'default'.
    Logged with each call so the selection is auditable.

    Cannot fail — unknown tasks default to cheap tier, surfaced via 'source'.
    """
    if override_model:
 # Caller knows what it wants; honor the model directly.
        return {
            "model": override_model,
            "effort": override_effort,  # may be None
            "tier": "override",
            "source": "override_model",
        }
    if override_tier:
        if override_tier not in _TIER_TO_CONFIG:
            raise ValueError(f"unknown tier {override_tier!r}; expected one of {list(_TIER_TO_CONFIG)}")
        tier = _TIER_TO_CONFIG[override_tier]
        return {
            "model": tier["model"],
            "effort": override_effort if override_effort is not None else tier["effort"],
            "tier": override_tier,
            "source": "override_tier",
        }
    tier_key = TIER_FOR_TASK.get(task)
    if tier_key:
        tier = _TIER_TO_CONFIG[tier_key]
 # Honor resume_draft/cover_letter_draft's opus-4-7 pin
        chosen_model = MODEL_FOR_TASK.get(task, tier["model"])
        return {
            "model": chosen_model,
            "effort": override_effort if override_effort is not None else tier["effort"],
            "tier": tier_key,
            "source": "task_map",
        }
 # Unknown task — default to cheap tier and label clearly so logs surface it.
    return {
        "model": TIER_CHEAP["model"],
        "effort": override_effort,
        "tier": "cheap",
        "source": "default",
    }


def _build_thinking_payload(model: str, effort: str | None,
                            current_max_tokens: int) -> tuple[dict, int]:
    """Return (payload_fragments, possibly-raised max_tokens) for thinking
    on the given model + effort. Empty fragments dict means no thinking.

    For classic-API models, ensures max_tokens > budget_tokens by raising
    max_tokens if needed — the API rejects with HTTP 400 otherwise.

    The fragments dict has zero or two keys: 'thinking' (always), and
    'output_config' (adaptive only). Caller merges into payload.
    """
    if not effort:
        return {}, current_max_tokens
    api = THINKING_API.get(model, "none")
    if api == "none":
 # Caller asked for thinking on a non-thinking model. Fail loud rather
 # than silently swallowing the request.
        raise RuntimeError(
            f"_build_thinking_payload: model {model!r} does not support "
            f"extended thinking; cannot honor effort={effort!r}. "
            f"Pick a different model or pass effort=None."
        )
    if api == "classic":
        budget = _CLASSIC_BUDGET_FOR_EFFORT.get(effort)
        if budget is None:
            raise ValueError(f"unknown effort {effort!r}; expected one of {list(_CLASSIC_BUDGET_FOR_EFFORT)}")
 # API requires max_tokens > budget_tokens. Raise max_tokens if needed.
        adjusted_max = max(current_max_tokens, budget + 512)
        return ({
            "thinking": {"type": "enabled", "budget_tokens": budget},
        }, adjusted_max)
    if api == "adaptive":
        if effort not in ("low", "medium", "high"):
            raise ValueError(f"unknown effort {effort!r}; expected 'low'|'medium'|'high' for adaptive thinking")
        return ({
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }, current_max_tokens)
    raise RuntimeError(f"_build_thinking_payload: unknown thinking API {api!r} for model {model!r}")


def _log_call(task, model, in_tokens, out_tokens, cost, latency_s, ok,
              error=None, extras=None):
    """Append one line to time-cost-log.jsonl.

    extras is an optional dict of additional fields — tier, source, rounds,
    thinking_tokens, tool_calls — that surface dynamic-selection decisions
    for audit.
    """
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "model": model,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": round(cost, 6),
        "latency_s": round(latency_s, 3),
        "ok": ok,
    }
    if error:
        record["error"] = str(error)[:200]
    if extras:
        record.update(extras)
    with _LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    _check_call_anomaly(record)


def _call_anomaly_flags(record: dict) -> dict:
    thresholds = CALL_ANOMALY_THRESHOLDS.get(record.get("task") or "")
    if not thresholds:
        return {}
    flags = {}
    for key, threshold in thresholds.items():
        value = record.get(key, 0) or 0
        if value >= threshold:
            flags[key] = {"value": value, "threshold": threshold}
    return flags


def _check_call_anomaly(record: dict) -> None:
    flags = _call_anomaly_flags(record)
    if not flags:
        return
    task = record.get("task") or "unknown"
    today = date.today().isoformat()
    alert_key = f"{task}:call_anomaly:{today}"

    def mutator(state):
        state = state or {}
        if state.get(alert_key):
            return state, True
        state[alert_key] = True
        return state, False

    try:
        from alice import safe_state
        already_alerted = safe_state.atomic_update(
            _ALERT_STATE, mutator, default={},
            skip_write_if_unchanged=True,
        )
        if already_alerted:
            return
    except Exception:
        pass
    try:
        from alice.observability import obs
        obs.capture_message(
            f"llm.cost.call_anomaly: task={task}",
            level="warning",
            where="llm:call_anomaly",
            extras={
                "task": task,
                "model": record.get("model"),
                "flags": flags,
                "cost_usd": record.get("cost_usd"),
                "in_tokens": record.get("in_tokens"),
                "out_tokens": record.get("out_tokens"),
                "date": today,
            },
        )
    except Exception:
        pass


def _fallback_model_for_error(task: str, chosen_model: str, error: str, cfg: dict) -> str | None:
    if chosen_model == OPENROUTER_FALLBACK_MODEL:
        return None
    if task != "telegram_chat":
        return None
    if not cfg.get("OPENROUTER_API_KEY"):
        return None
    lowered = (error or "").lower()
    if any(sig in lowered for sig in ("429", "503", "529", "rate", "timeout", "urlerror", "api failed")):
        return OPENROUTER_FALLBACK_MODEL
    return None


def log_turn_enrichment(*, task: str, model: str | None,
                          stop_reason: str | None,
                          tool_names: list[str],
                          grounding_flags: dict | None,
                          rounds: int | None = None,
                          ref_ts: str | None = None) -> None:
    """Append a post-call enrichment record to time-cost-log.jsonl.

    Detectors run AFTER llm.call has logged its primary record, so this is
    the only place to persist grounding verdicts and the flat tool_names
    projection alongside the original call. The two records are joinable on
    ts (within ~1s) and task; pass `ref_ts` if the caller already captured
    the primary record's timestamp.

    grounding_flags is the per-detector dict — None values mean "did not
    fire"; non-None values are the structured verdicts from grounding.py.
    """
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts":              datetime.now().isoformat(timespec="seconds"),
        "kind":            "turn_enrichment",
        "task":            task,
        "model":           model or "?",
        "stop_reason":     stop_reason or "",
        "tool_names":      list(tool_names or []),
        "grounding_flags": grounding_flags or {},
    }
    if rounds is not None:
        record["rounds"] = rounds
    if ref_ts:
        record["ref_ts"] = ref_ts
    with _LOG.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _calc_cost(model, in_tokens, out_tokens):
    p = PRICING.get(model)
    if not p:
        # Fail loud rather than silently book $0: an unpriced model means the
        # budget circuit is blind to this call's spend. Surface it; don't crash.
        print(f"[llm] WARNING: no pricing entry for model {model!r}; "
              "recording $0 cost (budget circuit cannot see this spend)",
              file=sys.stderr)
        return 0.0
    return (in_tokens * p["in"] + out_tokens * p["out"]) / 1_000_000


def _read_log_records():
    if not _LOG.exists():
        return []
    out = []
    with _LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception as _e:
                try:
                    import obs; obs.capture(_e, where="llm:read_log:json", payload={"line": line[:200]})
                except Exception:
                    pass
    return out


def cost_today():
    today = date.today().isoformat()
    return sum(r.get("cost_usd", 0) for r in _read_log_records()
               if r.get("ts", "").startswith(today))


def cost_today_by_task(task):
    today = date.today().isoformat()
    return sum(r.get("cost_usd", 0) for r in _read_log_records()
               if r.get("ts", "").startswith(today) and r.get("task") == task)


def _check_tripwire(task, today_spend):
    """Fire a Sentry warning once per task per day when threshold crossed.
    Does not block; logs + alerts and returns."""
    threshold = TASK_DAILY_TRIPWIRES.get(task)
    if threshold is None or today_spend < threshold:
        return
 # Check + update alert state (idempotent: one alert per task per day)
    today = date.today().isoformat()

    def mutator(state):
        state = state or {}
        if state.get(task) == today:
            return state, True  # already alerted; signal caller to skip
        state[task] = today
        return state, False

    try:
        from alice import safe_state
        already_alerted = safe_state.atomic_update(
            _ALERT_STATE, mutator, default={},
            skip_write_if_unchanged=True,
        )
        if already_alerted:
            return
    except Exception:
        pass
    msg = f"llm.cost.tripwire: task={task} today=${today_spend:.2f} threshold=${threshold:.2f}"
    print(f"[{msg}]", file=__import__("sys").stderr)
    try:
        from alice.observability import obs
        obs.capture_message(msg, level="warning",
                            extras={"task": task, "today_spend_usd": round(today_spend, 4),
                                    "threshold_usd": threshold, "date": today})
    except Exception:
        pass


def cost_last_n_days(n=7):
    cutoff = (datetime.now() - timedelta(days=n)).isoformat()
    return sum(r.get("cost_usd", 0) for r in _read_log_records()
               if r.get("ts", "") >= cutoff)


def cost_summary():
    records = _read_log_records()
    if not records:
        return {"total": 0.0, "today": 0.0, "week": 0.0, "calls": 0, "days_active": 0}
    days = {r["ts"][:10] for r in records if "ts" in r}
    return {
        "total":       round(sum(r.get("cost_usd", 0) for r in records), 4),
        "today":       round(cost_today(), 4),
        "week":        round(cost_last_n_days(7), 4),
        "calls":       len(records),
        "days_active": len(days),
        "per_task":    _per_task_cost(records),
    }


def _per_task_cost(records):
    out = {}
    for r in records:
        t = r.get("task", "unknown")
        out.setdefault(t, {"count": 0, "cost": 0.0})
        out[t]["count"] += 1
        out[t]["cost"] += r.get("cost_usd", 0)
    for v in out.values():
        v["cost"] = round(v["cost"], 4)
    return out


def _post_process(text):
    """Hard-strip voice violations from every Alice output before it reaches the operator.
    The em-dash rule was repeatedly violated by the model even when present in the
    brief; soft prompts aren't sufficient. This is a guarantee.

    Replaces:
      - Em dash (—) and en dash (–) → ", " (preserves sentence flow)
      - Double-hyphen-as-dash ( -- ) → ", "
    Does NOT modify legitimate code/CLI patterns like '--flag'.
    """
    import re
 # Em + en dash comma-space (works in most prose contexts)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
 # Double-hyphen as inter-word dash comma-space; preserve CLI flags
    text = re.sub(r"(?<=\w)\s+--\s+(?=\w)", ", ", text)
    return text


def _http_call_once(payload: dict, key: str, retries: int = 2,
                    retry_delay: float = 2.0) -> tuple[dict, float]:
    """One HTTPS round-trip to /v1/messages with retry on 429/503/529/5xx.
    Returns (response_body_dict, latency_seconds). Raises RuntimeError on
    exhaustion or non-retriable error.
    """
    last_err = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                _API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": key,
                    "anthropic-version": _API_VERSION,
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, context=_SSL, timeout=120) as r:
                return json.loads(r.read()), time.time() - t0
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:300]
            last_err = f"HTTP {e.code}: {err_body}"
            if e.code in (429, 503, 529) or e.code >= 500:
                if attempt < retries:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
            raise RuntimeError(f"Anthropic API failed: {last_err}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise RuntimeError(f"Anthropic API failed: {last_err}")
    raise RuntimeError(f"Anthropic API exhausted retries: {last_err}")


def _extract_text(content: list) -> str:
    """Concatenate text from every text-typed block. Non-text blocks
    (thinking, tool_use) are intentionally skipped — extended thinking is
    a reasoning signal, not output; tool_use is a structured call, not text."""
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def _trace_ids(session_id=None, job_key=None):
    """The run/session/job ids stamped on each call's span and cost-log record,
    so an LLM call joins to its run, its conversation, and the role it served.
    run_id is read from the env — run_daily exports ALICE_RUN_ID and its step
    subprocesses inherit it. Empty ids are omitted (no blank keys)."""
    ids = {
        "run_id":     os.environ.get("ALICE_RUN_ID", ""),
        "session_id": session_id or "",
        "job_key":    job_key or "",
    }
    return {k: v for k, v in ids.items() if v}


# Span-attribute name per id key (Phoenix convention is session.id; ours namespaced).
_TRACE_ID_ATTR = {"run_id": "alice.run_id", "session_id": "session.id", "job_key": "alice.job_key"}


def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0,
         retries=2, retry_delay=2.0,
         tools: list | None = None,
         tool_executor=None,
         effort: str | None = None,
         tier: str | None = None,
         session_id: str | None = None,
         job_key: str | None = None,
         max_tool_roundtrips: int = _MAX_TOOL_ROUNDTRIPS,
         _fallback_attempt: bool = False):
    """One Anthropic API call (or multi-turn tool loop).

    Backwards-compatible for text-only callers: pass task + prompt and get
    back {'text', 'in_tokens', 'out_tokens', 'cost_usd', 'latency_s', 'model'}.

    Extended modes (D1):
      - effort="low"|"medium"|"high" → enables extended thinking. The wire
        layer (_build_thinking_payload) translates effort to the per-model
        API shape (classic budget_tokens for Haiku; adaptive output_config
        for Sonnet+Opus). max_tokens is auto-raised if it would conflict
        with a classic budget. Forces temperature=1.0.
      - tools=[{name, description, input_schema}, ...] + tool_executor(name,
        input) → runs the multi-turn tool loop. When stop_reason='tool_use',
        each tool_use block is dispatched to tool_executor; the result is
        sent back as a tool_result message; loop continues until stop_reason
        is 'end_turn' or the cap. Cap of 8 roundtrips by default; hitting it
        raises (fail loud) rather than silently truncating.

    Selection: model + effort come from select_call_config(task) unless
    overridden by explicit kwargs. The chosen tier + source land in the
    log's extras for audit.
    """
    cfg = load()
    key = cfg.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in config.env")
    or_key = cfg.get("OPENROUTER_API_KEY", "")

    selection = select_call_config(
        task,
        override_model=model,
        override_effort=effort,
        override_tier=tier,
    )
    chosen_model = selection["model"]
    chosen_effort = selection["effort"]
    _provider = _infer_provider(chosen_model)

 # OpenRouter models do not support extended thinking. Fail loud rather than
 # silently ignoring the effort flag — a caller asking for thinking on an OR
 # model has a misconfiguration that should surface, not be swallowed.
    if _provider == "openrouter" and chosen_effort:
        raise RuntimeError(
            f"call: effort={chosen_effort!r} is not supported for OpenRouter-routed "
            f"model {chosen_model!r}. Pass effort=None for non-Anthropic models."
        )
    if _provider == "openrouter" and not or_key:
        raise RuntimeError("OPENROUTER_API_KEY not set in config.env (required for OR-routed models)")

 # Extended thinking requires temperature=1.0.
    if chosen_effort:
        temperature = 1.0

    messages = [{"role": "user", "content": prompt}]

    if _provider == "openrouter":
 # OpenAI-compatible payload path — Anthropic payload is NOT built.
        payload = _build_or_payload(chosen_model, messages, system, max_tokens, temperature)
        thinking_fragments = {}  # no thinking for OR models
    else:
 # Anthropic-native path — byte-identical to pre-Phase-M code.
 # Build thinking payload (handles per-model API shape + max_tokens
 # auto-raise for classic budget).
        thinking_fragments, max_tokens = _build_thinking_payload(
            chosen_model, chosen_effort, max_tokens,
        )
 # Some newer Anthropic models (e.g. claude-opus-4-8) have deprecated
 # the temperature field entirely — the API returns HTTP 400 if it is
 # present. Omit it for those models; all other Anthropic models still
 # accept it.
        _TEMP_DEPRECATED = frozenset({"claude-opus-4-8"})
        payload = {
            "model": chosen_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if chosen_model not in _TEMP_DEPRECATED:
            payload["temperature"] = temperature
        if system:
            payload["system"] = system
        payload.update(thinking_fragments)
        if tools:
            payload["tools"] = tools

 # Multi-round accounting — every roundtrip's usage rolls up to one log line.
    rounds = 0
    total_in = 0
    total_out = 0
    total_thinking = 0
    total_cost = 0.0
    total_latency = 0.0
    tool_calls_log: list[dict] = []
    last_err = None

 # Open ONE span around the whole multi-round body. Opened manually (not via
 # `with`) so the finally can close it with __exit__(None, None, None) —
 # guaranteeing it can never suppress an in-flight Alice exception. A no-op
 # tracer makes this zero-cost when tracing is off.
    _span_cm = telemetry.tracer.start_as_current_span(f"llm.call:{task}")
    span = _span_cm.__enter__()
    _span_closed = False
    try:
 # Entry attributes: structured first (no redaction), then content
 # (redacted + capped; skipped entirely when ALICE_TRACE_CONTENT=0).
        set_attr(span, "llm.provider", _provider)
        set_attr(span, "llm.model_name", chosen_model)
        set_attr(span, "alice.task", task)
        set_attr(span, "alice.tier", selection["tier"])
        set_attr(span, "alice.selection_source", selection["source"])
        set_attr(span, "alice.effort", chosen_effort)
        _trace_id_vals = _trace_ids(session_id, job_key)
        for _idk, _idv in _trace_id_vals.items():
            set_attr(span, _TRACE_ID_ATTR[_idk], _idv)
        if job_key:  # record this prediction's span so its outcome can be annotated back
            _pred_sid = telemetry.span_id_of(span)
            if _pred_sid:
                telemetry.record_prediction_span(job_key, _pred_sid, task)
        set_attr(span, "input.value", prompt)
        if system:
            set_attr(span, "llm.system", system)
        while True:
            rounds += 1
            if rounds > max_tool_roundtrips:
                raise RuntimeError(
                    f"call: max_tool_roundtrips ({max_tool_roundtrips}) exceeded "
                    f"for task={task!r}. Fail-loud: an infinite tool loop is more "
                    f"dangerous than a refused turn."
                )

            if _provider == "openrouter":
                body, latency = _http_call_or(payload, or_key, retries=retries,
                                              retry_delay=retry_delay)
            else:
                body, latency = _http_call_once(payload, key, retries=retries,
                                                retry_delay=retry_delay)
            total_latency += latency

            usage = body.get("usage", {}) or {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            thinking_tok = ((usage.get("output_tokens_details") or {})
                            .get("thinking_tokens", 0))
            cost = _calc_cost(chosen_model, in_tok, out_tok)
            total_in += in_tok
            total_out += out_tok
            total_thinking += thinking_tok
            total_cost += cost

            stop_reason = body.get("stop_reason")
            content = body.get("content", []) or []

            if stop_reason == "tool_use":
                if tool_executor is None:
                    raise RuntimeError(
                        f"call: response stopped at tool_use but no tool_executor "
                        f"was provided. When tools=[...] is passed, tool_executor "
                        f"is required. (task={task!r})"
                    )
                tool_results = []
                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    t_id = block.get("id")
                    t_name = block.get("name", "")
                    t_input = block.get("input", {}) or {}
                    log_entry = {
                        "round": rounds,
                        "name": t_name,
                        "input": t_input,
                    }
                    try:
                        result = tool_executor(t_name, t_input)
 # Capture the result so grounding detectors can compare
 # the response text against what the tool actually
 # returned (category_mismatch detector needs this).
                        log_entry["result"] = result
                        tool_calls_log.append(log_entry)
 # Record this tool roundtrip as a span event so the
 # dispatch loop is visible. Content is redacted; size is
 # structured. Best-effort; never gates the model loop.
                        try:
                            _res_str = str(result) if result is not None else ""
                            _ev = {
                                "tool.name": redact("tool.name", t_name),
                                "tool.round": redact("tool.round", rounds),
                                "tool.result_size": redact("tool.result_size", len(_res_str)),
                            }
                            _prev = redact("tool.result_preview", _res_str)
                            if _prev is not None:
                                _ev["tool.result_preview"] = _prev
                            _args = redact("tool.args_summary", t_input)
                            if _args is not None:
                                _ev["tool.args_summary"] = _args
                            span.add_event("tool.result", _ev)
                        except Exception:
                            pass
 # Inform the whimsical progress UI that this tool
 # just ran — its next edit will preferentially
 # pull from this tool's phrase pool. Best-effort;
 # whimsy is a UX nicety and must never gate the
 # model loop.
                        try:
                            from alice.llm import whimsy
                            whimsy.record_tool(t_name)
                        except Exception:
                            pass
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": t_id,
                            "content": _guard_tool_result_text(t_name, result),
                        })
                    except Exception as e:
                        log_entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                        tool_calls_log.append(log_entry)
                        try:
                            span.add_event("tool.error", {
                                "tool.name": redact("tool.name", t_name),
                                "tool.round": redact("tool.round", rounds),
                                "error.type": type(e).__name__,
                            })
                        except Exception:
                            pass
 # Surface the failure to the model so it can recover —
 # but mark it is_error per the API contract.
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": t_id,
                            "content": f"ERROR: {type(e).__name__}: {e}",
                            "is_error": True,
                        })
 # Continue the conversation: append the assistant's tool_use
 # message, then the user-side tool_result message, then loop.
                payload["messages"].append({"role": "assistant", "content": content})
                payload["messages"].append({"role": "user", "content": tool_results})
                continue

 # Terminal: end_turn, max_tokens, stop_sequence, etc.
            final_text = _post_process(_extract_text(content))
            log_extras = {
                "tier":             selection["tier"],
                "selection_source": selection["source"],
                "effort":           chosen_effort,
                "thinking_tokens":  total_thinking,
                "rounds":           rounds,
                "stop_reason":      stop_reason,
            }
            if tool_calls_log:
                log_extras["tool_calls"] = tool_calls_log
            log_extras.update(_trace_id_vals)  # run/session/job ids into the cost log
            _log_call(task, chosen_model, total_in, total_out, total_cost,
                      total_latency, True, extras=log_extras)
 # Completion attributes (clean path).
            set_attr(span, "llm.token_count.prompt", total_in)
            set_attr(span, "llm.token_count.completion", total_out)
            set_attr(span, "llm.token_count.thinking", total_thinking)
            set_attr(span, "llm.cost.total", round(total_cost, 6))
            set_attr(span, "llm.latency_s", round(total_latency, 3))
            set_attr(span, "alice.rounds", rounds)
            set_attr(span, "llm.stop_reason", stop_reason or "")
            set_attr(span, "output.value", final_text)
            if task in TASK_DAILY_TRIPWIRES:
                try:
                    _check_tripwire(task, cost_today_by_task(task))
                except Exception:
                    pass
            return {
                "text":            final_text,
                "in_tokens":       total_in,
                "out_tokens":      total_out,
                "cost_usd":        total_cost,
                "latency_s":       total_latency,
                "model":           chosen_model,
                "tier":            selection["tier"],
                "rounds":          rounds,
                "thinking_tokens": total_thinking,
                "tool_calls":      tool_calls_log,
                "stop_reason":     stop_reason,
            }
    except Exception as e:
        last_err = str(e)
        _log_call(task, chosen_model, total_in, total_out, total_cost,
                  total_latency, False, error=last_err, extras={
                      "tier":             selection["tier"],
                      "selection_source": selection["source"],
                      "rounds":           rounds,
                      **_trace_ids(session_id, job_key),  # ids on the error record too
                  })
 # Mark the span errored, then re-raise the ORIGINAL exception unchanged.
        try:
            span.record_exception(e)
            from opentelemetry.trace import Status, StatusCode  # lazy; only if installed
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
        except Exception:
            pass
        fallback_model = None if _fallback_attempt else _fallback_model_for_error(
            task, chosen_model, last_err, cfg
        )
        if fallback_model:
            try:
                from alice.observability import obs
                obs.capture_message(
                    "llm.routing.fallback",
                    level="warning",
                    where="llm:call:fallback",
                    extras={
                        "task": task,
                        "from_model": chosen_model,
                        "to_model": fallback_model,
                        "error": last_err[:200],
                    },
                )
            except Exception:
                pass
            try:
                _span_cm.__exit__(None, None, None)
                _span_closed = True
            except Exception:
                pass
            return call(
                task,
                prompt,
                system=system,
                max_tokens=max_tokens,
                model=fallback_model,
                temperature=temperature,
                retries=retries,
                retry_delay=retry_delay,
                tools=tools,
                tool_executor=tool_executor,
                effort=None,
                tier=None,
                session_id=session_id,
                job_key=job_key,
                max_tool_roundtrips=max_tool_roundtrips,
                _fallback_attempt=True,
            )
        raise
    finally:
 # Guarantees span end on every path. __exit__(None, None, None) cannot
 # suppress an in-flight exception (no exc info passed), so the original
 # raise above still propagates. Keep this manual shape — do NOT convert
 # to `with tracer.start_as_current_span(...) as span:` or a buggy span
 # __exit__ could swallow a real Alice exception.
        if not _span_closed:
            try:
                _span_cm.__exit__(None, None, None)
            except Exception:
                pass


def load_alice_brief():
    """Load Alice's full system prompt: soul + operational brief + knowledge index.

    Synthesis order:
      1. ALICE_SOUL.md  — identity layer (loads first; grounds everything below)
      2. Alice.md       — operational rules and scope
      3. Knowledge base index — dynamic listing of knowledge/*.md so she
                          knows what insider context she has on tap.

    The soul leads the identity; the operational rules follow. A missing soul or
    missing brief is fail-loud in caller diagnostics (a returned-empty brief
    surfaces as an empty system prompt, which the full-prompt capture makes
    visible).

    Returns the assembled system-prompt string. Empty string only if BOTH
    soul and brief are missing — caller treats that as a bad install.
    """
    repo = _REPO
    soul_path = repo / "ALICE_SOUL.md"
    brief_path = repo / "Alice.md"

    parts = []
    if soul_path.exists():
        parts.append(soul_path.read_text())
    if brief_path.exists():
        if parts:
            parts.append("\n\n---\n\n# Operational brief (Alice.md)\n\n")
        parts.append(brief_path.read_text())

    if not parts:
        return ""

    assembled = "".join(parts)

    kb_index = _build_knowledge_index()
    if kb_index:
        assembled = (
            assembled
            + "\n\n---\n\n"
            + "## Knowledge notebook (mixed evidence status)\n\n"
            + "You have a recruiting and hiring notebook with synthetic examples and "
            + "uncited hypothesis pages. Treat uncited, time-sensitive claims as unverified; "
            + "do not present them as facts. Draw on the notebook when the operator asks "
            + "about recruiting strategy, "
            + "interview prep, ATS behavior, comp negotiation, referrals, AI "
            + "screening, or market dynamics. When useful, state the evidence status and "
            + "cite the specific file "
            + "path so the operator can read deeper.\n\n"
            + kb_index
        )
    return assembled


def _build_knowledge_index() -> str:
    """Walk knowledge/ and emit a one-page index of every document.

    Each entry: relative path, then the first non-header text line as a one-line
    summary (most docs open with an italic summary). Index is short enough to
    sit comfortably in Alice's system prompt on every call.
    """
    root = _REPO / "knowledge"
    if not root.is_dir():
        return ""

 # Skip meta files at the top of knowledge/ — they describe the system,
 # not insider hiring knowledge. Subdirectory README.md files (if any) stay.
    SKIP_NAMES = {"README.md", "sources.md"}

    lines = []
    for md in sorted(root.rglob("*.md")):
        if md.parent == root and md.name in SKIP_NAMES:
            continue
        rel = md.relative_to(root.parent)  # keep "knowledge/" prefix for clarity
        summary = _first_summary_line(md)
        if summary:
            lines.append(f"- `{rel}` — {summary}")
        else:
            lines.append(f"- `{rel}`")
    return "\n".join(lines)


def _first_summary_line(path: Path) -> str:
    """Return the first prose line of a markdown doc (skipping H1 and blanks).

    Strips italic markers and trims to ~180 chars so the index stays compact.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as _e:
        try:
            import obs; obs.capture(_e, where="llm:_first_summary_line", payload={"path": str(path)})
        except Exception:
            pass
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("---"):
            continue
        if line.startswith("<!--"):
            continue
        # Strip leading/trailing italic / bold markers.
        cleaned = line.strip("*_").strip()
        if not cleaned:
            continue
        if len(cleaned) > 180:
            cleaned = cleaned[:177] + "..."
        return cleaned
    return ""


if __name__ == "__main__":
 # Quick self-test
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        print(json.dumps(cost_summary(), indent=2))
    else:
        res = call("triage_observation", "Reply with exactly: OK", max_tokens=10)
        print(f"OK: model={res['model']} tokens={res['in_tokens']}+{res['out_tokens']} cost=${res['cost_usd']:.6f} reply={res['text']!r}")
