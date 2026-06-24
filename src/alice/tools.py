"""Alice's tool layer.

Defines tools as JSON-schema specs + executors, wires them through guards.py
for write-side safety, and exposes them to llm.call() as a dispatcher.

Design choices and the discipline behind them:

  1. Tools are registered via @register_tool, which produces a single
     TOOLS_REGISTRY list. tool_specs() returns the JSON-schema list that
     llm.call() passes to the Messages API. dispatch(name, input_obj) is
     the tool_executor callable that llm.call() invokes per tool_use block.

  2. Mutating tools MUST be guard-wired. register_tool raises at IMPORT
     time if a mutating tool has no guard — turning "the gate is wired
     into the tool path" from a runtime check into a structural invariant.
     Gates can exist in guards.py and still not be wired into the actual
     write path; making the guard mandatory at registration closes that gap.

  3. describe_capabilities queries this registry + the filesystem AT CALL
     TIME. There is no static capabilities blurb anywhere in this file.
     If a tool is added or removed, the next describe_capabilities() call
     reflects it. Structural self-awareness is a capability feature, not a
     trust feature — Alice should be able to enumerate what she has. The live
     query is what makes that property real instead of a description that
     drifts.

  4. Read-only tools have no guard (they can't violate write-side safety),
     but read_file / list_dir are bounded to the repo root so a malicious
     instruction can't make Alice exfiltrate /etc/passwd or ~/.ssh by
     asking nicely.

  5. Errors raise. llm.call() catches and forwards them as tool_result
     with is_error=True per the Messages API contract, so the model can
     recover. Silent failure would defeat the whole point.

  6. repo_status is the authoritative tool for "what changed recently" /
     "what files were modified". It returns real git commits + real file
     modification times — NEVER fabricated. The grounding rule requires
     calling this tool before asserting any commit SHA, filename, or
     modification timestamp from the repo. Read-only, repo-scoped, no
     arbitrary shell.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from alice import repo_paths
from typing import Any, Callable

from alice import guards
from alice import safe_state

REPO_ROOT = Path(repo_paths.ROOT)


# ─── registry ────────────────────────────────────────────────────────────────

TOOLS_REGISTRY: list[dict] = []


def register_tool(*, name: str, description: str, input_schema: dict,
                  mutating: bool = False,
                  guard: Callable | None = None) -> Callable:
    """Decorator: register a tool. If mutating, a guard is mandatory.

    The guard is called with the input_obj BEFORE the executor; if it raises
    ForbiddenAction, the executor is skipped and the exception propagates
    to llm.call() (which surfaces it as is_error=True tool_result).
    """
    def decorator(fn: Callable) -> Callable:
        if mutating and guard is None:
            raise RuntimeError(
                f"register_tool: mutating tool {name!r} has no guard. Mutating "
                f"tools MUST be guard-wrapped — refusing to register."
            )
 # Detect duplicate names early.
        for existing in TOOLS_REGISTRY:
            if existing["name"] == name:
                raise RuntimeError(
                    f"register_tool: duplicate tool name {name!r} "
                    f"(already registered)"
                )
        TOOLS_REGISTRY.append({
            "name":         name,
            "description":  description,
            "input_schema": input_schema,
            "executor":     fn,
            "guard":        guard,
            "mutating":     mutating,
        })
        return fn
    return decorator


def tool_specs() -> list[dict]:
    """Return JSON-schema specs ready to pass as `tools=` to llm.call()."""
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": t["input_schema"]}
        for t in TOOLS_REGISTRY
    ]


def _find_tool(name: str) -> dict | None:
    for t in TOOLS_REGISTRY:
        if t["name"] == name:
            return t
    return None


def dispatch(name: str, input_obj: dict) -> Any:
    """The tool_executor passed to llm.call(). Looks up the tool, runs any
    guard, then the executor. Exceptions propagate (llm.call surfaces them
    as is_error tool_result blocks)."""
    tool = _find_tool(name)
    if tool is None:
 # Fail loud — an unregistered tool name means the spec sent to the
 # model didn't match the executor, which is a build-time mistake.
        raise RuntimeError(
            f"tools.dispatch: tool {name!r} is not registered. "
            f"Available: {[t['name'] for t in TOOLS_REGISTRY]}"
        )
    try:
        from alice.observability import product_analytics
        product_analytics.capture(
            "alice_tool_called",
            {"tool_name": name, "tool_count": 1, "tool_error": False},
        )
    except Exception:
        pass
    if tool["guard"] is not None:
        tool["guard"](input_obj)  # may raise ForbiddenAction
    try:
        return tool["executor"](input_obj)
    except Exception:
        try:
            from alice.observability import product_analytics
            product_analytics.capture(
                "alice_tool_failed",
                {"tool_name": name, "tool_count": 1, "tool_error": True},
            )
        except Exception:
            pass
        raise


# ─── helpers ─────────────────────────────────────────────────────────────────

def _bounded_path(rel_path: str) -> Path:
    """Resolve a user-supplied path against REPO_ROOT and refuse traversal.

    `rel_path` may begin with the repo root absolute path; for ergonomics
    we accept both. Anything pointing outside the repo raises ForbiddenAction.
    """
    if not rel_path:
        raise ValueError("path must be non-empty")
    raw = Path(rel_path)
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = (REPO_ROOT / raw).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        raise guards.ForbiddenAction(
            f"tools._bounded_path refused path outside repo root: {raw}"
        )
    return resolved


def _file_freshness(path: Path) -> dict:
    """Compact freshness descriptor: exists, mtime, size."""
    if not path.exists():
        return {"exists": False}
    st = path.stat()
    return {
        "exists":   True,
        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "size":     st.st_size,
    }


def _load_jsonl(path: Path, *, max_lines: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()
    if max_lines is not None:
        lines = lines[-max_lines:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _redact_observability_text(value: Any) -> Any:
    try:
        from alice import ai_guardrails
        if isinstance(value, str):
            return ai_guardrails.redact_secrets(value)[0]
        if isinstance(value, list):
            return [_redact_observability_text(v) for v in value]
        if isinstance(value, dict):
            return {k: _redact_observability_text(v) for k, v in value.items()}
    except Exception:
        pass
    return value


def _contains_ci(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack.lower()


# ─── READ tools (always allowed; no guard) ───────────────────────────────────

# read_sheet projection. The full grid re-rides payload["messages"] on every
# tool roundtrip (up to 8x) — roughly 148K tokens/turn, dominated by the
# `notes` free-text column (61%; median ~924 chars/row, uniformly large). The
# default returns a SLIM view: drop internal/verbose columns and cap notes
# per-row (~59% smaller, ~61K/turn). Full content (notes/url/rationale) stays
# available on demand via verbose=true; the cap marker tells the model when to
# fetch it. The notes cap is tunable (400 = the gist; verbose+filter = the
# full note).
_SHEET_DROP_DEFAULT = ("rationale", "url", "job_key", "source", "surfaced_date")
_SHEET_NOTES_CAP = 400


def _project_sheet_row(r: dict, row_idx: int, verbose: bool) -> dict:
    if verbose:
        return {"row_idx": row_idx, **r}
    o = {"row_idx": row_idx}
    for k, v in r.items():
        if k in _SHEET_DROP_DEFAULT:
            continue
        if k == "notes":
            s = str(v or "")
            if len(s) > _SHEET_NOTES_CAP:
                v = (s[:_SHEET_NOTES_CAP]
                     + f" …[+{len(s) - _SHEET_NOTES_CAP} chars truncated — call "
                       "read_sheet(verbose=true, company_substring=...) for the full note]")
        o[k] = v
    return o


@register_tool(
    name="read_sheet",
    description=(
        "Read rows from Jordan Avery's job-search pipeline spreadsheet. Returns row dicts. "
        "By DEFAULT a SLIM view (company, role, status, comp, score, intent, "
        "status_changed_date, and notes capped at ~400 chars) to keep the result "
        "small — the full grid is large and re-rides the conversation. A capped "
        "note ends with '…[+N chars truncated — call read_sheet(verbose=true...)]'; "
        "when you need a role's FULL note, or its url/rationale (e.g. to draft "
        "outreach or explain a score), call AGAIN with verbose=true AND a "
        "company_substring filter to get just that role's full row. Optionally "
        "filter by status or company substring (case-insensitive)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "filter_status":     {"type": "string",
                                  "description": "If provided, return only rows whose status matches (case-insensitive)."},
            "company_substring": {"type": "string",
                                  "description": "If provided, return only rows whose company contains this substring (case-insensitive)."},
            "limit":             {"type": "integer", "minimum": 1, "maximum": 200,
                                  "description": "Max rows to return. Default 200."},
            "verbose":           {"type": "boolean",
                                  "description": "If true, return ALL columns uncapped (full notes, url, rationale, job_key). Default false = slim view. Use verbose=true WITH a company_substring filter to fetch one role's full note/url cheaply — do NOT request verbose for the whole sheet."},
        },
        "required": [],
    },
)
def _read_sheet(input_obj: dict) -> dict:
    from alice.persistence import ledger
    if not ledger.available():
        raise RuntimeError("read_sheet: ledger (Google Sheets) is not available")
    ws = ledger._ws()
    rows = ws.get_all_records()
    out = []
    fs = (input_obj.get("filter_status") or "").strip().lower() or None
    cs = (input_obj.get("company_substring") or "").strip().lower() or None
    verbose = bool(input_obj.get("verbose"))
    limit = int(input_obj.get("limit") or 200)
    for i, r in enumerate(rows, start=2):  # rows start at row 2 in the sheet
        if fs and (r.get("status", "") or "").strip().lower() != fs:
            continue
        if cs and cs not in (r.get("company", "") or "").lower():
            continue
        out.append(_project_sheet_row(r, i, verbose))
        if len(out) >= limit:
            break
    return {"rows": out, "total_returned": len(out)}


@register_tool(
    name="read_focus_state",
    description=(
        "Return Jordan Avery's current focus list (the roles he's prioritized for the "
        "week) plus the max focus size. Returns {roles, max_focus}. Each role "
        "has row_idx, company, role, added_at."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _read_focus_state(_input: dict) -> dict:
    from alice.persistence import focus
    return {"roles": focus.current(), "max_focus": focus.MAX_FOCUS}


@register_tool(
    name="read_pending_state",
    description=(
        "Return the current pending-confirmation state (any directive Alice "
        "is holding before execution, or null if nothing pending)."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _read_pending_state(_input: dict) -> dict:
    p = REPO_ROOT / "feedback" / "pending-confirmation.json"
    data = safe_state.atomic_read(p, default=None)
    return {"pending": data}


@register_tool(
    name="query_sentry_events",
    description=(
        "Read recent remote Sentry events for Alice. Use this when Jordan Avery asks "
        "about Alice errors, security events, grounding warnings, observability "
        "canaries, cost anomalies, or production reliability. Returns redacted "
        "event summaries only; never returns raw payloads or secrets. Requires "
        "SENTRY_AUTH_TOKEN/SENTRY_ORG/SENTRY_PROJECT in config."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Sentry search query. Default returns Alice security/observability/routing/grounding messages.",
            },
            "since_minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1440,
                "description": "How far back to search. Default 360 minutes.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": "Maximum event summaries to return. Default 10.",
            },
        },
        "required": [],
    },
)
def _query_sentry_events(input_obj: dict) -> dict:
    query = (input_obj.get("query") or "").strip()
    if not query:
        query = (
            'message:"alice.security" OR message:"alice.observability" OR '
            'message:"llm.cost" OR message:"llm.routing" OR message:"Alice claims without tools"'
        )
    since_minutes = min(max(int(input_obj.get("since_minutes") or 360), 1), 1440)
    limit = min(max(int(input_obj.get("limit") or 10), 1), 30)
    try:
        from alice.observability import sentry_query
        from alice import jobcfg
        cfg = jobcfg.load()
        token = cfg.get("SENTRY_AUTH_TOKEN") or cfg.get("SENTRY_API_TOKEN")
        if not token:
            return {
                "ok": False,
                "missing": ["SENTRY_AUTH_TOKEN"],
                "note": "Sentry emits are live via DSN, but remote readback needs an auth token.",
            }
        org, project, _project_rec = sentry_query.discover_project(token, cfg)
        events = sentry_query.query_events(
            token, org, project,
            query=query,
            since_minutes=since_minutes,
            limit=limit,
        )
        return {
            "ok": True,
            "org": org,
            "project": project,
            "query": query,
            "since_minutes": since_minutes,
            "events": _redact_observability_text(events),
            "note": "Remote Sentry event summaries only; raw event payloads are intentionally omitted.",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "query": query}


@register_tool(
    name="search_transcript",
    description=(
        "Search Alice's Telegram transcript in feedback/telegram-history.jsonl by "
        "role, phrase, and recency. Use this before claiming what Jordan Avery or Alice "
        "said in a recent chat. Returns capped, redacted snippets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "phrase": {"type": "string", "description": "Case-insensitive phrase to search for. Optional."},
            "role": {"type": "string", "enum": ["user", "alice", "any"], "description": "Role filter. Default any."},
            "since_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "Default 24."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Default 10."},
        },
        "required": [],
    },
)
def _search_transcript(input_obj: dict) -> dict:
    phrase = (input_obj.get("phrase") or "").strip()
    role = (input_obj.get("role") or "any").strip().lower()
    since_hours = min(max(int(input_obj.get("since_hours") or 24), 1), 168)
    limit = min(max(int(input_obj.get("limit") or 10), 1), 30)
    cutoff = datetime.now() - timedelta(hours=since_hours)
    records = _load_jsonl(REPO_ROOT / "feedback" / "telegram-history.jsonl")
    matches = []
    for rec in reversed(records):
        ts = _parse_dt(rec.get("ts"))
        if ts and ts < cutoff:
            continue
        if role != "any" and rec.get("role") != role:
            continue
        text = str(rec.get("text") or "")
        if phrase and not _contains_ci(text, phrase):
            continue
        snippet = text.replace("\n", " ")
        if len(snippet) > 700:
            snippet = snippet[:700] + " …[truncated]"
        matches.append({
            "ts": rec.get("ts"),
            "role": rec.get("role"),
            "snippet": _redact_observability_text(snippet),
        })
        if len(matches) >= limit:
            break
    return {
        "path": "feedback/telegram-history.jsonl",
        "phrase": phrase,
        "role": role,
        "since_hours": since_hours,
        "matches": list(reversed(matches)),
    }


@register_tool(
    name="query_cost_log",
    description=(
        "Summarize recent LLM cost/time log entries. Use this to diagnose token "
        "spikes, failed calls, fallback attempts, expensive turns, tool rounds, "
        "and model routing. Reads feedback/time-cost-log.jsonl."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Optional task filter, e.g. telegram_chat."},
            "since_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "Default 24."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Recent entries returned. Default 20."},
        },
        "required": [],
    },
)
def _query_cost_log(input_obj: dict) -> dict:
    task_filter = (input_obj.get("task") or "").strip()
    since_hours = min(max(int(input_obj.get("since_hours") or 24), 1), 168)
    limit = min(max(int(input_obj.get("limit") or 20), 1), 50)
    cutoff = datetime.now() - timedelta(hours=since_hours)
    records = []
    for rec in _load_jsonl(REPO_ROOT / "feedback" / "time-cost-log.jsonl"):
        if rec.get("kind") == "turn_enrichment":
            continue
        ts = _parse_dt(rec.get("ts"))
        if ts and ts < cutoff:
            continue
        if task_filter and rec.get("task") != task_filter:
            continue
        records.append(rec)

    by_task: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "failures": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    failures = []
    high_token_calls = []
    for rec in records:
        task = rec.get("task") or "unknown"
        model = rec.get("model") or "unknown"
        cost = float(rec.get("cost_usd") or 0.0)
        by_task[task]["calls"] += 1
        by_task[task]["cost_usd"] += cost
        by_model[model]["calls"] += 1
        by_model[model]["cost_usd"] += cost
        if not rec.get("ok", True):
            by_task[task]["failures"] += 1
            failures.append({
                "ts": rec.get("ts"),
                "task": task,
                "model": model,
                "error": str(rec.get("error") or "")[:220],
            })
        tokens = int(rec.get("in_tokens") or 0) + int(rec.get("out_tokens") or 0)
        if tokens >= 50_000 or cost >= 0.05:
            high_token_calls.append({
                "ts": rec.get("ts"),
                "task": task,
                "model": model,
                "in_tokens": rec.get("in_tokens"),
                "out_tokens": rec.get("out_tokens"),
                "cost_usd": round(cost, 6),
                "rounds": rec.get("rounds"),
            })

    recent = []
    for rec in records[-limit:]:
        recent.append({
            "ts": rec.get("ts"),
            "task": rec.get("task"),
            "model": rec.get("model"),
            "ok": rec.get("ok"),
            "cost_usd": rec.get("cost_usd"),
            "in_tokens": rec.get("in_tokens"),
            "out_tokens": rec.get("out_tokens"),
            "rounds": rec.get("rounds"),
            "tool_names": [t.get("name") for t in rec.get("tool_calls", []) if isinstance(t, dict)],
            "error": str(rec.get("error") or "")[:180] or None,
        })
    return {
        "path": "feedback/time-cost-log.jsonl",
        "since_hours": since_hours,
        "task_filter": task_filter or None,
        "total_calls": len(records),
        "total_cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in records), 6),
        "by_task": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_task.items()},
        "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_model.items()},
        "failures": failures[-10:],
        "high_token_or_cost_calls": high_token_calls[-10:],
        "recent": recent,
    }


@register_tool(
    name="query_recent_traces",
    description=(
        "Read local prediction span records from feedback/prediction-spans.jsonl. "
        "Use this to find span IDs/job keys for Phoenix and LangSmith correlation "
        "when diagnosing a specific Alice run, healthcheck, eval, or job task."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Optional task filter."},
            "job_key": {"type": "string", "description": "Optional exact job_key filter."},
            "since_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "Default 24."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
        },
        "required": [],
    },
)
def _query_recent_traces(input_obj: dict) -> dict:
    task_filter = (input_obj.get("task") or "").strip()
    job_key_filter = (input_obj.get("job_key") or "").strip()
    since_hours = min(max(int(input_obj.get("since_hours") or 24), 1), 168)
    limit = min(max(int(input_obj.get("limit") or 20), 1), 50)
    cutoff = datetime.now() - timedelta(hours=since_hours)
    records = []
    for rec in _load_jsonl(REPO_ROOT / "feedback" / "prediction-spans.jsonl"):
        ts = _parse_dt(rec.get("ts"))
        if ts and ts < cutoff:
            continue
        if task_filter and rec.get("task") != task_filter:
            continue
        if job_key_filter and rec.get("job_key") != job_key_filter:
            continue
        span_id = str(rec.get("span_id") or "")
        langsmith_run_id = None
        if span_id:
            try:
                from alice.observability import observability_healthcheck
                langsmith_run_id = observability_healthcheck.langsmith_run_id_for_span(span_id)
            except Exception:
                pass
        records.append({
            "ts": rec.get("ts"),
            "task": rec.get("task"),
            "job_key": rec.get("job_key"),
            "span_id": span_id,
            "phoenix_span_id": span_id[-16:] if span_id else None,
            "langsmith_run_id": langsmith_run_id,
        })
    return {
        "path": "feedback/prediction-spans.jsonl",
        "since_hours": since_hours,
        "task_filter": task_filter or None,
        "job_key_filter": job_key_filter or None,
        "traces": records[-limit:],
    }


@register_tool(
    name="query_paste_buffer_log",
    description=(
        "Read paste-buffer diagnostics from feedback/paste-buffer.log. Use this "
        "when a Telegram message was received but not answered, delayed, combined, "
        "or suspected of being misclassified as a paste chunk."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "since_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "Default 24."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."},
        },
        "required": [],
    },
)
def _query_paste_buffer_log(input_obj: dict) -> dict:
    since_hours = min(max(int(input_obj.get("since_hours") or 24), 1), 168)
    limit = min(max(int(input_obj.get("limit") or 20), 1), 50)
    cutoff = datetime.now() - timedelta(hours=since_hours)
    events = []
    for rec in _load_jsonl(REPO_ROOT / "feedback" / "paste-buffer.log"):
        ts = _parse_dt(rec.get("ts"))
        if ts and ts < cutoff:
            continue
        events.append(rec)
    counts = Counter(rec.get("event") for rec in events)
    return {
        "path": "feedback/paste-buffer.log",
        "exists": (REPO_ROOT / "feedback" / "paste-buffer.log").exists(),
        "since_hours": since_hours,
        "event_counts": dict(counts),
        "events": events[-limit:],
    }


@register_tool(
    name="validate_state_claim",
    description=(
        "Validate a concrete state claim before Alice asserts it. Supports "
        "claim_type=file_exists, role_status, commit_exists, or config_key_present. "
        "Use this for claims like 'file X exists', 'role Y is status Z', "
        "'commit SHA exists', or 'config key K is present'. Read-only and redacted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "claim_type": {
                "type": "string",
                "enum": ["file_exists", "role_status", "commit_exists", "config_key_present"],
            },
            "path": {"type": "string", "description": "For file_exists: repo-relative path."},
            "company_substring": {"type": "string", "description": "For role_status: company/role substring."},
            "expected_status": {"type": "string", "description": "For role_status: expected status."},
            "commit": {"type": "string", "description": "For commit_exists: SHA/ref to verify."},
            "config_key": {"type": "string", "description": "For config_key_present: key name only; value is never returned."},
        },
        "required": ["claim_type"],
    },
)
def _validate_state_claim(input_obj: dict) -> dict:
    claim_type = input_obj.get("claim_type")
    if claim_type == "file_exists":
        rel = input_obj.get("path") or ""
        path = _bounded_path(rel)
        return {
            "claim_type": claim_type,
            "path": str(path.relative_to(REPO_ROOT)),
            **_file_freshness(path),
            "valid": path.exists(),
        }

    if claim_type == "commit_exists":
        commit = (input_obj.get("commit") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._/\-]{4,80}", commit):
            return {"claim_type": claim_type, "commit": commit[:20], "valid": False, "error": "invalid commit/ref syntax"}
        try:
            result = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", "-s", "--format=%H\x1f%ai\x1f%s", commit],
                capture_output=True, text=True, timeout=10, shell=False,
            )
            if result.returncode != 0:
                return {"claim_type": claim_type, "commit": commit, "valid": False}
            sha, date_raw, subject = (result.stdout.strip().split("\x1f", 2) + ["", "", ""])[:3]
            return {
                "claim_type": claim_type,
                "commit": commit,
                "valid": True,
                "sha": sha[:12],
                "date": date_raw,
                "subject": subject,
            }
        except Exception as e:
            return {"claim_type": claim_type, "commit": commit, "valid": False, "error": f"{type(e).__name__}: {e}"[:200]}

    if claim_type == "config_key_present":
        key = (input_obj.get("config_key") or "").strip()
        if not re.fullmatch(r"[A-Z0-9_]{2,80}", key):
            return {"claim_type": claim_type, "config_key": key[:80], "valid": False, "error": "invalid config key syntax"}
        from alice import jobcfg
        present = bool(jobcfg.load().get(key))
        return {"claim_type": claim_type, "config_key": key, "present": present, "valid": present}

    if claim_type == "role_status":
        company_substring = (input_obj.get("company_substring") or "").strip().lower()
        expected_status = (input_obj.get("expected_status") or "").strip().lower()
        if not company_substring:
            return {"claim_type": claim_type, "valid": False, "error": "company_substring required"}
        try:
            from alice.persistence import ledger
            if not ledger.available():
                return {"claim_type": claim_type, "valid": False, "error": "ledger unavailable"}
            rows = ledger._ws().get_all_records()
            matches = []
            for i, row in enumerate(rows, start=2):
                hay = f"{row.get('company', '')} {row.get('role', '')}".lower()
                if company_substring in hay:
                    matches.append((i, row))
            if len(matches) != 1:
                return {
                    "claim_type": claim_type,
                    "valid": False,
                    "match_count": len(matches),
                    "matches": [
                        {"row_idx": i, "company": r.get("company"), "role": r.get("role"), "status": r.get("status")}
                        for i, r in matches[:10]
                    ],
                }
            row_idx, row = matches[0]
            actual = (row.get("status") or "").strip().lower()
            return {
                "claim_type": claim_type,
                "valid": bool(expected_status and actual == expected_status),
                "row_idx": row_idx,
                "company": row.get("company"),
                "role": row.get("role"),
                "actual_status": row.get("status"),
                "expected_status": input_obj.get("expected_status"),
            }
        except Exception as e:
            return {"claim_type": claim_type, "valid": False, "error": f"{type(e).__name__}: {e}"[:200]}

    return {"claim_type": claim_type, "valid": False, "error": "unsupported claim_type"}


@register_tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file under the repo root. Returns its content as a "
        "string. Refuses paths outside the repo root. For large files, only "
        "the first 100KB is returned."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Repo-relative or absolute path under the repository root."},
        },
        "required": ["path"],
    },
)
def _read_file(input_obj: dict) -> dict:
    path = _bounded_path(input_obj["path"])
    if not path.exists():
        return {"path": str(path), "exists": False, "content": None}
    if not path.is_file():
        raise RuntimeError(f"read_file: {path} is not a regular file")
    raw = path.read_bytes()
    truncated = False
    if len(raw) > 100_000:
        raw = raw[:100_000]
        truncated = True
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"read_file: decode failed: {e}")
    return {
        "path":      str(path),
        "exists":    True,
        "content":   content,
        "truncated": truncated,
        "size":      path.stat().st_size,
    }


@register_tool(
    name="list_dir",
    description=(
        "List entries in a directory under the repo root. Returns a list of "
        "{name, is_file, is_dir, size, modified}. Refuses paths outside the "
        "repo root. Hidden files (starting with '.') are skipped."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Repo-relative or absolute directory path."},
        },
        "required": ["path"],
    },
)
def _list_dir(input_obj: dict) -> dict:
    path = _bounded_path(input_obj["path"])
    if not path.exists():
        return {"path": str(path), "exists": False, "entries": []}
    if not path.is_dir():
        raise RuntimeError(f"list_dir: {path} is not a directory")
    entries = []
    for child in sorted(path.iterdir()):
        if child.name.startswith("."):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({
            "name":     child.name,
            "is_file":  child.is_file(),
            "is_dir":   child.is_dir(),
            "size":     st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return {"path": str(path), "exists": True, "entries": entries}


@register_tool(
    name="list_knowledge_files",
    description=(
        "List the recruiting/hiring knowledge-base documents Alice can read "
        "(insider context on ATS systems, AI screening, comp negotiation, "
        "interview methodology, market intelligence, etc.). Returns "
        "[{path, summary}] one per doc."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _list_knowledge_files(_input: dict) -> dict:
    from alice.llm import llm
    kb = (REPO_ROOT / "knowledge")
    if not kb.is_dir():
        return {"files": []}
    files = []
    for md in sorted(kb.rglob("*.md")):
        if md.parent == kb and md.name in {"README.md", "sources.md"}:
            continue
        rel = md.relative_to(REPO_ROOT)
        summary = llm._first_summary_line(md)
        files.append({"path": str(rel), "summary": summary})
    return {"files": files}


@register_tool(
    name="read_knowledge_file",
    description=(
        "Read one document from the knowledge base by relative path "
        "(as returned by list_knowledge_files). The knowledge base contains "
        "insider context on how hiring works from the employer side."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Repo-relative path, e.g. 'knowledge/ats-systems/parser-quirks.md'."},
        },
        "required": ["path"],
    },
)
def _read_knowledge_file(input_obj: dict) -> dict:
    rel = input_obj["path"]
 # Constrain to knowledge/ subtree only.
    p = _bounded_path(rel)
    try:
        p.relative_to(REPO_ROOT / "knowledge")
    except ValueError:
        raise guards.ForbiddenAction(
            f"read_knowledge_file: path {p} is not under knowledge/"
        )
    return _read_file({"path": str(p)})


@register_tool(
    name="read_alice_brief",
    description=(
        "Re-read Alice's own assembled system prompt (ALICE_SOUL.md + Alice.md "
        "+ knowledge index). Useful when she wants to ground a self-referential "
        "answer about what she is, what she can do, or what her boundaries are. "
        "Returns the full assembled brief as a string."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _read_alice_brief(_input: dict) -> dict:
    from alice.llm import llm
    return {"brief": llm.load_alice_brief()}


@register_tool(
    name="describe_capabilities",
    description=(
        "Return Alice's actual tools, data sources, and boundaries — queried "
        "LIVE from the running process and filesystem, not from a static "
        "blurb. Use this when the user asks something off-pattern, to "
        "enumerate what's actually available and compose a path from "
        "primitives. The returned shape is "
        "{tools, data_sources, boundaries}."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _describe_capabilities(_input: dict) -> dict:
 # Tools: live registry inventory.
    tools = [
        {"name": t["name"], "description": t["description"],
         "mutating": t["mutating"]}
        for t in TOOLS_REGISTRY
    ]

 # Data sources: live filesystem query for the state files Alice can read.
    state_sources = [
        ("feedback/focus.json",                 "focus list (Jordan Avery's priority roles)"),
        ("feedback/pending-confirmation.json",  "pending directive awaiting execution"),
        ("feedback/prep-queue.json",            "application prep queue"),
        ("feedback/triage-state.json",          "observation-triage cursor"),
        ("feedback/digest-prefs.json",          "digest cadence prefs"),
        ("feedback/observations.md",            "Jordan Avery's free-form observations"),
        ("feedback/hypotheses.md",              "active hypotheses Alice is tracking"),
        ("feedback/time-cost-log.jsonl",        "every LLM call (cost + selection audit)"),
        ("feedback/telegram-history.jsonl",     "Telegram transcript for recent chat failures"),
        ("feedback/prediction-spans.jsonl",     "local span ids for Phoenix/LangSmith correlation"),
        ("feedback/paste-buffer.log",           "paste-buffer append/flush/combine diagnostics"),
        ("state/grounding_flags_fallback.jsonl", "local fallback log when Sentry grounding dispatch fails"),
        ("state/observability/latest-healthcheck.json", "latest end-to-end observability healthcheck report"),
        ("state/observability/slo-summary.json", "7-day SLO summary generated from healthcheck reports"),
        ("state/observability/metrics.json", "runtime/SLO/cost metrics export"),
        ("state/observability/metrics.prom", "Prometheus-format local metrics export"),
        ("state/observability/eval-summary.json", "Alice regression/eval dataset summary"),
        ("state/observability/judged-eval.json", "recurring paid judged eval/drift run summary"),
        ("state/observability/behavior-regression.json", "real-path Alice behavior regression results"),
        ("state/observability/enterprise-readiness.json", "enterprise readiness controls, scores, and external gaps"),
        ("state/observability/escalation-policy.json", "incident severity and alert-route escalation policy"),
        ("state/observability/audit-evidence.json", "audit evidence bundle for observability/interview review"),
        ("state/observability/retention-report.json", "local retention enforcement report"),
        ("state/observability/dashboard.html", "local interview/demo observability dashboard"),
        ("monitoring/prometheus.yml", "Prometheus scrape config for Alice metrics exporter"),
        ("monitoring/alert_rules.yml", "Prometheus alert rules for Alice SLO/runtime failures"),
    ]
    data_sources = []
    for rel, label in state_sources:
        p = REPO_ROOT / rel
        data_sources.append({"path": rel, "label": label, **_file_freshness(p)})

 # Sheet — schema (column names) without the row content
    try:
        from alice.persistence import ledger
        if ledger.available():
            ws = ledger._ws()
            rows = ws.get_all_records()
            schema = list(rows[0].keys()) if rows else []
            data_sources.append({
                "path":     "(google sheets pipeline)",
                "label":    "Jordan Avery's job-search pipeline (one row per role)",
                "schema":   schema,
                "row_count": len(rows),
                "exists":   True,
            })
    except Exception as e:
        data_sources.append({"path": "(google sheets pipeline)",
                             "label": "pipeline", "exists": False,
                             "error": str(e)[:80]})

 # Runtime / self-identity. Sourced LIVE from llm.MODEL_FOR_TASK so the
 # model identity reported here is always the one the call config will
 # actually pick for that task — not a hardcoded string that can drift
 # away from the routing layer.
    runtime = {}
    try:
        from alice.llm import llm as _llm
        runtime["conversational_model"] = _llm.MODEL_FOR_TASK.get(
            "telegram_chat", _llm.TIER_CHEAP["model"]
        )
        runtime["models_by_task"] = dict(_llm.MODEL_FOR_TASK)
        runtime["default_model"]  = _llm.TIER_CHEAP["model"]
    except Exception as e:
        runtime["error"] = f"could not resolve runtime model: {e}"[:120]
    try:
        from alice import jobcfg
        cfg = jobcfg.load()
        runtime["observability"] = {
            "sentry_dsn_configured": bool(cfg.get("SENTRY_DSN")),
            "sentry_remote_readback_configured": bool(
                (cfg.get("SENTRY_AUTH_TOKEN") or cfg.get("SENTRY_API_TOKEN"))
                and (cfg.get("SENTRY_ORG") or cfg.get("SENTRY_ORG_SLUG"))
                and (cfg.get("SENTRY_PROJECT") or cfg.get("SENTRY_PROJECT_SLUG"))
            ),
            "sentry_org": cfg.get("SENTRY_ORG") or cfg.get("SENTRY_ORG_SLUG") or None,
            "sentry_project": cfg.get("SENTRY_PROJECT") or cfg.get("SENTRY_PROJECT_SLUG") or None,
            "phoenix_endpoint": cfg.get("ALICE_PHOENIX_ENDPOINT") or "http://localhost:6006",
            "langsmith_project": cfg.get("LANGSMITH_PROJECT", "alice"),
            "alice_trace_project": cfg.get("ALICE_TRACE_PROJECT", "alice"),
        }
        try:
            from alice.observability import product_analytics
            runtime["observability"]["posthog"] = product_analytics.status()
        except Exception as e:
            runtime["observability"]["posthog"] = {"error": str(e)[:120]}
        try:
            from alice.observability import runtime_metrics
            runtime["infrastructure"] = runtime_metrics.summary()
        except Exception as e:
            runtime["infrastructure"] = {"error": str(e)[:120]}
        try:
            from alice import ai_guardrails
            runtime["security_guardrails"] = {
                "module": "src/alice/ai_guardrails.py",
                "prompt_injection_detection": True,
                "secret_redaction": True,
                "pii_detection": True,
                "user_input_hook": "telegram_bot._security_anchor_for_user_text",
                "tool_result_hook": "llm._guard_tool_result_text",
                "outbound_hook": "telegram_bot._screen_outbound_response",
                "sentry_events": [
                    "alice.security.prompt_injection_user_text",
                    "alice.security.prompt_injection_tool_result",
                    "alice.security.secret_leak",
                    "alice.security.pii_leak",
                ],
                "pattern_counts": {
                    "prompt_injection": len(getattr(ai_guardrails, "_PROMPT_INJECTION_PATTERNS", [])),
                    "secret": len(getattr(ai_guardrails, "_SECRET_PATTERNS", [])),
                    "pii": len(getattr(ai_guardrails, "_PII_PATTERNS", [])),
                },
            }
        except Exception as e:
            runtime["security_guardrails"] = {"error": str(e)[:120]}
    except Exception as e:
        runtime["observability"] = {"error": f"could not resolve observability config: {e}"[:120]}

 # Boundaries: query guards.py for the actual lists (not a hardcoded copy).
    boundaries = {
        "write_allowed_subtrees": [
            str(p.relative_to(REPO_ROOT))
            for p in guards._WRITE_ALLOWED_TREES
        ],
        "self_edit_forbidden_files": [
            str(p.relative_to(REPO_ROOT)) for p in guards._SELF_EDIT_FORBIDDEN
        ],
        "self_edit_forbidden_trees": [
            str(p.relative_to(REPO_ROOT)) for p in guards._SELF_EDIT_FORBIDDEN_TREE
        ],
        "delete_protected_subtrees": [
            str(p.relative_to(REPO_ROOT))
            for p in guards._PROTECTED_DELETE_SUBTREES
        ],
        "code_gates": [
            "no_git_push",
            "no_autonomous_irreversible_delete",
            "no_arbitrary_shell_beyond_self_inspection",
            "no_self_edit_of_brief_or_soul_or_scripts",
            "no_third_party_send (hardcoded send destinations in notify_email/notify_telegram)",
        ],
    }
    return {"tools": tools, "data_sources": data_sources,
            "boundaries": boundaries, "runtime": runtime}


@register_tool(
    name="query_runtime_metrics",
    description=(
        "Return Alice's local infrastructure/runtime metrics: launchd service "
        "state, PID, restart/run count, CPU/RSS memory sample, disk headroom, "
        "and production threshold checks. Read-only and repo-local. Use this "
        "before answering questions about uptime, restarts, CPU, memory, disk, "
        "or Layer 1 infrastructure status."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _query_runtime_metrics(_input: dict) -> dict:
    from alice.observability import runtime_metrics
    return runtime_metrics.summary()


@register_tool(
    name="query_observability_artifacts",
    description=(
        "Return paths and freshness for Alice's generated production "
        "observability artifacts: dashboard, SLO summary, metrics exports, "
        "eval summary, retention report, incident reports, and trace examples. "
        "Use this before answering questions about observability artifacts or "
        "interview-demo collateral."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _query_observability_artifacts(_input: dict) -> dict:
    from alice.observability import observability_artifacts
    paths = [
        observability_artifacts.LATEST_HEALTHCHECK,
        observability_artifacts.METRICS_JSON,
        observability_artifacts.METRICS_PROM,
        observability_artifacts.SLO_JSON,
        observability_artifacts.SLO_MD,
        observability_artifacts.EVAL_JSON,
        observability_artifacts.EVAL_MD,
        observability_artifacts.JUDGED_EVAL_JSON,
        observability_artifacts.JUDGED_EVAL_MD,
        observability_artifacts.BEHAVIOR_REGRESSION_JSON,
        observability_artifacts.BEHAVIOR_REGRESSION_MD,
        observability_artifacts.ENTERPRISE_READINESS_JSON,
        observability_artifacts.ENTERPRISE_READINESS_MD,
        observability_artifacts.ESCALATION_POLICY_JSON,
        observability_artifacts.ESCALATION_POLICY_MD,
        observability_artifacts.AUDIT_EVIDENCE_JSON,
        observability_artifacts.AUDIT_EVIDENCE_MD,
        observability_artifacts.TRACE_MD,
        observability_artifacts.RETENTION_JSON,
        observability_artifacts.RETENTION_MD,
        observability_artifacts.DASHBOARD_HTML,
        observability_artifacts.PROMETHEUS_CONFIG,
        observability_artifacts.PROMETHEUS_RULES,
    ]
    incidents = sorted(observability_artifacts.INCIDENT_DIR.glob("*.md")) if observability_artifacts.INCIDENT_DIR.exists() else []
    return {
        "artifacts": [
            {"path": str(p.relative_to(REPO_ROOT)), **_file_freshness(p)}
            for p in paths
        ],
        "incidents": [
            {"path": str(p.relative_to(REPO_ROOT)), **_file_freshness(p)}
            for p in incidents[-10:]
        ],
    }


# ─── repo_status: grounded, real git log + file mtimes (read-only) ───────────

# This is the tool Alice calls when Jordan Avery asks "what changed recently?" /
# "what was improved?" / "show me recent commits" / "what's the latest?".
# Without it, the model can confabulate filenames (e.g. assert a plausible
# script name with zero tool calls). With it, she cites real output. The
# grounding rule requires calling this before any commit / filename /
# timestamp assertion.

# Constraints (read-only, bounded, no arbitrary shell):
# - git log only: --oneline --format= with bounded N (max 50).
# - file mtime stat only: glob within REPO_ROOT, no traversal outside.
# - subprocess.run with shell=False; no shell metachars accepted.
# - NO writes, NO git mutating commands, NO shell=True.

def _run_git_log(n: int) -> list[dict]:
    """Run `git log --oneline` in REPO_ROOT and return structured commits.

    Returns list of {sha, date, subject} dicts. Never raises into caller —
    on any error returns a single entry with error text.
    """
    try:
        result = subprocess.run(
            [
                "git", "-C", str(REPO_ROOT),
                "log", f"--max-count={n}",
                "--format=%H\x1f%ai\x1f%s",
            ],
            capture_output=True, text=True, timeout=10, shell=False,
        )
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) == 3:
                sha, date_raw, subject = parts
 # Normalise date to ISO seconds (drop timezone suffix).
                try:
                    from datetime import datetime as _dt
                    date_iso = _dt.fromisoformat(date_raw.strip()).isoformat(timespec="seconds")
                except Exception:
                    date_iso = date_raw.strip()
                commits.append({
                    "sha":     sha[:12],
                    "date":    date_iso,
                    "subject": subject.strip(),
                })
        return commits
    except Exception as e:
        return [{"error": f"git log failed: {type(e).__name__}: {e}"}]


def _file_mtimes_for_glob(glob_pattern: str, limit: int = 20) -> list[dict]:
    """Stat files matching a glob pattern inside REPO_ROOT.

    Refuses patterns that would escape the repo root. Returns list of
    {path, modified, size} sorted by mtime descending (newest first).
    """
    import fnmatch
    try:
 # Resolve the glob against REPO_ROOT. Accept both relative and
 # absolute patterns — normalise to relative first.
        raw = Path(glob_pattern)
        if raw.is_absolute():
            try:
                raw = raw.relative_to(REPO_ROOT)
            except ValueError:
                return [{"error": f"path outside repo root: {glob_pattern}"}]
 # Refuse traversal patterns (../ or absolute escapes after join).
        resolved_dir = (REPO_ROOT / raw.parent).resolve()
        try:
            resolved_dir.relative_to(REPO_ROOT)
        except ValueError:
            return [{"error": f"path outside repo root: {glob_pattern}"}]

        matches = sorted(
            REPO_ROOT.glob(str(raw)),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )[:limit]
        results = []
        for p in matches:
            try:
                st = p.stat()
                results.append({
                    "path":     str(p.relative_to(REPO_ROOT)),
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "size":     st.st_size,
                })
            except OSError:
                continue
        return results
    except Exception as e:
        return [{"error": f"glob failed: {type(e).__name__}: {e}"}]


@register_tool(
    name="repo_status",
    description=(
        "Return REAL, GROUNDED data about the job-search repo: recent git commits "
        "(with SHA, date, subject) and/or file modification times for a path glob. "
        "CALL THIS before asserting any commit, filename, modification timestamp, "
        "or 'what changed recently' claim — those claims MUST be grounded in this "
        "tool's output, never fabricated from training data. "
        "Returns {commits: [...], files: [...], fetched_at}. "
        "commits: last N git log entries (max 50, default 10), each with sha/date/subject. "
        "files: entries matching path_glob (repo-relative glob, e.g. 'src/alice/*.py' or "
        "'*.md'), sorted newest-modified first (max 20 per call). "
        "Read-only; no writes; bounded to repo root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "n_commits": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Number of recent git commits to return. Default 10.",
            },
            "path_glob": {
                "type": "string",
                "description": (
                    "Repo-relative glob to stat (e.g. 'scripts/*.py', '*.md', "
                    "'src/alice/observability/obs.py'). Optional. If omitted, only commits are returned."
                ),
            },
        },
        "required": [],
    },
)
def _repo_status(input_obj: dict) -> dict:
    n = min(max(int(input_obj.get("n_commits") or 10), 1), 50)
    glob_pattern = (input_obj.get("path_glob") or "").strip()

    commits = _run_git_log(n)
    files: list[dict] = []
    if glob_pattern:
        files = _file_mtimes_for_glob(glob_pattern)

    return {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root":  str(REPO_ROOT),
        "commits":    commits,
        "files":      files,
        "note": (
            "This is authoritative repo state fetched at the timestamp above. "
            "Cite sha/date/subject from commits[] and path/modified from files[] "
            "directly — do not paraphrase or invent detail beyond what's here."
        ),
    }


# ─── WRITE tools (mutating; guard-wrapped) ───────────────────────────────────

def _write_file_guard(input_obj: dict) -> None:
    """Route every write_file call through guards.assert_write_allowed.
    guards.py is correct in isolation; this dispatch hook is what makes it
    correct in use (the check is wired into the actual write path)."""
    path = input_obj.get("path", "")
    guards.assert_write_allowed(path)


@register_tool(
    name="write_file",
    description=(
        "Write content to a file under one of Alice's permitted subtrees "
        "(applications/, feedback/, targets/, output/, knowledge/). Refuses "
        "paths outside the allowlist, refuses templates/, refuses self-edit "
        "of Alice.md / ALICE_SOUL.md / scripts/. Returns {path, bytes_written}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path":    {"type": "string",
                        "description": "Repo-relative or absolute path inside an allowed subtree."},
            "content": {"type": "string",
                        "description": "Full content to write. Existing file is overwritten."},
        },
        "required": ["path", "content"],
    },
    mutating=True,
    guard=_write_file_guard,
)
def _write_file(input_obj: dict) -> dict:
    path = _bounded_path(input_obj["path"])
 # The guard has already vetted the path. Re-check here so a code path that
 # bypasses the guard (e.g. testing the executor directly) is still safe.
    guards.assert_write_allowed(path)
    content = input_obj["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return {"path": str(path), "bytes_written": len(content.encode("utf-8"))}


def _focus_guard(_input: dict) -> None:
    """Focus mutations are reversible and bounded to focus.json (a permitted
    write-allowlist target). The chat-handler gates them upstream; here we
    just confirm the underlying state file is in the allowed set."""
 # If guards.assert_write_allowed accepts feedback/focus.json, focus
 # tools are safe to call. (They route through src/alice/persistence/focus.py and
 # safe_state — they don't take arbitrary paths.)
    guards.assert_write_allowed(REPO_ROOT / "feedback" / "focus.json")


@register_tool(
    name="set_focus",
    description=(
        "Replace Jordan Avery's current focus list with the given role substrings. "
        "Max 5 roles. Each substring is matched against company+role on the "
        "sheet; unique matches are added, ambiguous/missing are reported. "
        "Returns {set, not_found, ambiguous}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "substrings": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1, "maxItems": 5,
                "description": "List of company/role substrings, up to 5."},
        },
        "required": ["substrings"],
    },
    mutating=True,
    guard=_focus_guard,
)
def _set_focus(input_obj: dict) -> dict:
    from alice.persistence import focus
    return focus.set_focus(input_obj["substrings"], actor="alice")


@register_tool(
    name="add_focus",
    description="Add one role to the focus list. Substring matched against company+role.",
    input_schema={
        "type": "object",
        "properties": {
            "substring": {"type": "string"},
        },
        "required": ["substring"],
    },
    mutating=True,
    guard=_focus_guard,
)
def _add_focus(input_obj: dict) -> dict:
    from alice.persistence import focus
    return focus.add(input_obj["substring"], actor="alice")


@register_tool(
    name="drop_focus",
    description="Remove the first focus role whose company+role matches the substring.",
    input_schema={
        "type": "object",
        "properties": {
            "substring": {"type": "string"},
        },
        "required": ["substring"],
    },
    mutating=True,
    guard=_focus_guard,
)
def _drop_focus(input_obj: dict) -> dict:
    from alice.persistence import focus
    return focus.drop(input_obj["substring"], actor="alice")


def _observation_guard(_input: dict) -> None:
    guards.assert_write_allowed(REPO_ROOT / "feedback" / "observations.md")


@register_tool(
    name="append_observation",
    description=(
        "Append a free-form note to feedback/observations.md (the file Jordan Avery "
        "reads for context Alice has captured between digests)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
    mutating=True,
    guard=_observation_guard,
)
def _append_observation(input_obj: dict) -> dict:
    p = REPO_ROOT / "feedback" / "observations.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    text = input_obj["text"]
    with p.open("a") as f:
        f.write(f"\n---\n## {ts}\n\n{text}\n")
    return {"appended_to": str(p), "ts": ts, "chars": len(text)}


# ─── Directive-shaped tools (replace the regex parse layer for chat) ─────────

# Tools are the structure. When Jordan Avery says "northwind enterprise: good
# fit", Alice calls mark_role_status; "prep: meridian labs" calls enqueue_prep;
# "prep stop: northwind" calls dequeue_prep. No regex layer in the chat path.
# Each goes through ledger / imap_reply helpers shared with the cron path.

_CANONICAL_STATUSES = {
    "new", "good fit", "not a fit", "materials pending", "submitted",
    "first screen scheduled", "interviewing", "offer", "negotiating",
    "closed",
}


def _sheet_write_guard(_input: dict) -> None:
    """Sheet writes are reversible (Jordan Avery can re-edit on the sheet) but
    they are external state. The guard is a marker call — the action ledger
    captures Alice's writes vs Jordan Avery's via the
    `authorized=True, source=...` audit."""
 # No path-based check here (sheet isn't a file). The audit is in the
 # ledger.update_status call's source=... and authorized=True kwargs.
    return None


@register_tool(
    name="mark_role_status",
    description=(
        "Set the status of a role on Jordan Avery's pipeline sheet. company_substring "
        "matches one row by company+role (case-insensitive). "
        "NON-TERMINAL statuses write directly: 'new', 'good fit', 'not a fit', "
        "'materials pending', 'first screen scheduled'. "
        "TERMINAL statuses ('submitted', 'interviewing', 'interviewed', 'offer', "
        "'negotiating', 'closed', 'rejected', 'withdrawn') are REFUSED by this tool "
        "— they are effectively irreversible and require Jordan Avery's explicit "
        "confirmation. Do NOT promise to set a terminal status directly (you will "
        "be refused). Instead, route it through confirmation (call ask_confirmation) "
        "or tell Jordan Avery he can set it himself. "
        "Refuses on 0 or 2+ row matches (ambiguity)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "company_substring": {
                "type": "string",
                "description": "Substring matching company+role (case-insensitive). E.g. 'northwind enterprise' or 'meridian labs revenue'.",
            },
            "status": {
                "type": "string",
                "description": "Canonical status (exact). Non-terminal ('good fit', "
                               "'materials pending', 'first screen scheduled', etc.) "
                               "write directly; terminal ('submitted', 'interviewing', "
                               "'offer', 'negotiating', 'closed', 'rejected', "
                               "'withdrawn') are refused and need Jordan Avery's confirmation.",
            },
        },
        "required": ["company_substring", "status"],
    },
    mutating=True,
    guard=_sheet_write_guard,
)
def _mark_role_status(input_obj: dict) -> dict:
    from alice.persistence import ledger
    substr = (input_obj.get("company_substring") or "").strip()
    status = (input_obj.get("status") or "").strip().lower()
    if not substr:
        raise ValueError("mark_role_status: company_substring required")
    if status not in _CANONICAL_STATUSES:
        raise ValueError(
            f"mark_role_status: status {status!r} not canonical. "
            f"Must be one of: {sorted(_CANONICAL_STATUSES)}"
        )
 # A terminal/irreversible status must NOT be set directly from chat: a bare
 # conversational "sent" can be misparsed as 'submitted' on an inferred role,
 # written with authorized=True, and auto-drop the role from focus. The
 # ledger gate (ledger.update_status authorized=) is the structural backstop;
 # this refuses BEFORE any sheet access so a terminal status is never written
 # on an inferred parse — parse-independent and fail-safe. Non-terminal
 # statuses still write directly. Chat-initiated terminal writes via a
 # confirm-then-execute flow (the button_ux side-effecting wire) are deferred.
    if status in ledger.TERMINAL_GATED:
        return {
            "ok":     False,
            "error":  "terminal_status_needs_confirmation",
            "detail": (
                f"'{status}' is a terminal status — I can't set it directly from "
                "chat (safety: it's hard to reverse, and a misparse here once "
                "wrote a wrong 'submitted' and dropped a role from focus). To set "
                "it, reply to the daily email with the status directive (the "
                "confirmed path), or tell me to ask you to confirm it. I can still "
                "set non-terminal statuses (good fit, materials pending, first "
                "screen scheduled, ...) here directly."
            ),
        }
    if not ledger.available():
        raise RuntimeError("mark_role_status: ledger (sheet) is not available")
    ws = ledger._ws()
    rows = ws.get_all_records()
 # Use the same token-AND matching pattern focus.py uses (via _match_sheet_row)
    from alice.notify.imap_reply import _match_sheet_row
    hits = _match_sheet_row(substr, rows)
    if not hits:
        raise RuntimeError(f"mark_role_status: no row matches {substr!r}")
    if len(hits) > 1:
        candidates = [(i, r.get("company", ""), r.get("role", "")) for i, r in hits[:5]]
        raise RuntimeError(
            f"mark_role_status: ambiguous ({len(hits)} matches) for {substr!r}. "
            f"Refine the substring. Candidates: {candidates}"
        )
    row_idx, row = hits[0]
    ledger.update_status(
        ws, row_idx, status,
        authorized=True,
        source="alice_tool_mark_role_status",
    )
    return {
        "row_idx":     row_idx,
        "company":     row.get("company", ""),
        "role":        row.get("role", ""),
        "new_status":  status,
    }


def _prep_queue_guard(_input: dict) -> None:
    guards.assert_write_allowed(REPO_ROOT / "feedback" / "prep-queue.json")


@register_tool(
    name="enqueue_prep",
    description=(
        "Queue one or more roles for application-package prep. The next "
        "prep_materials cron run drafts resume + cover letter + targeted "
        "questions + strategy for each. Use rush=true for prep_now shorthand "
        "(rushes the queue order). On Jordan Avery's 'prep: northwind' or 'prep order: "
        "northwind, meridian' or 'prep now: meridian', use this tool."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "substrings": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Company+role substrings to queue.",
            },
            "rush": {
                "type": "boolean",
                "description": "If true, marks entries as rushed (prep_now shorthand).",
            },
        },
        "required": ["substrings"],
    },
    mutating=True,
    guard=_prep_queue_guard,
)
def _enqueue_prep(input_obj: dict) -> dict:
    from alice.notify import imap_reply
    substrings = input_obj.get("substrings") or []
    rush = bool(input_obj.get("rush", False))
    if not substrings:
        raise ValueError("enqueue_prep: substrings list required")
    imap_reply._enqueue_prep(substrings, rush=rush)
    return {"queued": substrings, "rush": rush, "count": len(substrings)}


@register_tool(
    name="dequeue_prep",
    description=(
        "Remove the first matching pending entry from the prep queue. "
        "On Jordan Avery's 'prep stop: northwind enterprise', use this tool. Leaves any "
        "partial drafts in place; only halts further work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "substring": {
                "type": "string",
                "description": "Substring matching the queued entry.",
            },
        },
        "required": ["substring"],
    },
    mutating=True,
    guard=_prep_queue_guard,
)
def _dequeue_prep(input_obj: dict) -> dict:
    from alice.notify import imap_reply
    substr = (input_obj.get("substring") or "").strip()
    if not substr:
        raise ValueError("dequeue_prep: substring required")
    imap_reply._dequeue_prep(substr)
    return {"dequeued": substr}


# ─── Gated prep pipeline (Stage 1/2/3/4 — see prep_pipeline.py) ──────────────

def _generate_package_guard(input_obj: dict) -> None:
    """Pipeline writes the four artifact files + .pipeline-metadata.json
    under applications/<slug>/. Guard the directory subtree at the seam."""
    substr = (input_obj.get("substring") or "").strip()
    if not substr:
        raise ValueError("generate_application_package: substring required")
 # The actual file paths get guard-checked again inside write_file's path;
 # here we just confirm applications/ subtree is writable, which it is by
 # the guards.py allowlist.
    guards.assert_write_allowed(REPO_ROOT / "applications" / "_guard_probe")


@register_tool(
    name="generate_application_package",
    description=(
        "Generate a full tailored application package for ONE role from the "
        "sheet via the four-stage gated pipeline (GROUND → WRITE → VERIFY → "
        "assembled artifacts on disk). This is the structurally-correct way "
        "to draft materials: Stage 1 fetches the JD body and loads Jordan Avery's "
        "history, halting if anything required is missing; Stage 2 generates "
        "resume → cover → questions → strategy in dependency order (one LLM "
        "call per artifact, not a tool-loop); Stage 3 checks generated claims "
        "against retrieved ground material; the package lands in "
        "applications/<slug>/. Use this tool instead of calling write_file "
        "directly when Jordan Avery asks you to prep / draft / generate a v2.0 / "
        "rebuild a package. Returns a structured result with the halt stage "
        "(if any), file paths, verification verdict, and cost."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "substring": {
                "type": "string",
                "description": (
                    "Company+role substring that uniquely identifies ONE sheet "
                    "row. E.g. 'northwind enterprise', 'boreal flowcad strategic'. "
                    "Fails if no match or multiple matches."
                ),
            },
            "verify_only": {
                "type": "boolean",
                "description": (
                    "If true, runs Stage 1/2/3 but does NOT write artifacts "
                    "to disk. Use to dry-run the pipeline before committing."
                ),
            },
        },
        "required": ["substring"],
    },
    mutating=True,
    guard=_generate_package_guard,
)
def _generate_application_package(input_obj: dict) -> dict:
    """Run the four-stage prep pipeline for one sheet row. Halt-on-missing-input,
    sequence Stage 2, verify Stage 3. Result is structured so the chat path
    can report honestly (including the halt reason if Stage 1 fails)."""
    from alice.pipeline import prep_pipeline
    from alice.persistence import ledger
    from alice.notify.imap_reply import _match_sheet_row

    substr = (input_obj.get("substring") or "").strip()
    if not substr:
        raise ValueError("generate_application_package: substring required")
    verify_only = bool(input_obj.get("verify_only", False))

    if not ledger.available():
        raise RuntimeError("ledger (Google Sheets) is not available")
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(substr, rows)
    if not hits:
        raise ValueError(
            f"generate_application_package: no sheet row matches {substr!r}"
        )
    if len(hits) > 1:
        candidates = [
            f"row {idx}: {r.get('company')} | {r.get('role')[:60]}"
            for idx, r in hits[:5]
        ]
        raise ValueError(
            f"generate_application_package: ambiguous match for {substr!r} "
            f"({len(hits)} hits). Refine. Candidates: {candidates}"
        )
    row_idx, row = hits[0]
    company   = row.get("company", "")
    role      = row.get("role", "")
    url       = row.get("url", "")
    rationale = row.get("rationale", "")
    import re as _re
    am = _re.match(r"\[([^\]]+)\]", rationale)
    archetype = am.group(1) if am else "Other"

    result = prep_pipeline.run_pipeline(
        company=company, role=role, url=url, archetype=archetype,
        rationale=rationale, row_idx=row_idx, row=row,
        write_to_disk=not verify_only,
        verify_only=verify_only,
    )

 # Project to a JSON-clean dict for the tool_result content. The full
 # PipelineResult dataclass nests other dataclasses; convert via asdict
 # then strip non-essential fields the model doesn't need to see.
    import dataclasses as _dc
    out = {
        "slug":           result.slug,
        "company":        result.company,
        "role":           result.role,
        "pkg_dir":        result.pkg_dir,
        "halted_at_stage": result.halted_at_stage,
        "halt_reason":    result.halt_reason,
        "files_written":  result.files_written,
        "total_cost":     round(result.total_cost, 4),
        "started_at":     result.started_at,
        "finished_at":    result.finished_at,
        "ground": {
            "jd_chars":              result.ground.jd_chars,
            "jd_source":             result.ground.jd_source,
            "operator_variant":          result.ground.operator_variant,
            "company_research_source": result.ground.company_research_source,
            "company_research_incomplete": result.ground.company_research_incomplete,
            "experience_entries_used": [
                e.get("entry_id") for e in result.ground.experience_extras_entries
            ],
        },
    }
    if result.write:
        out["write"] = {
            "artifacts_generated":   result.write.artifacts_generated,
            "artifact_costs":        result.write.artifact_costs,
            "artifact_models":       result.write.artifact_models,
        }
    if result.verify:
        out["verify"] = {
            "overall_grounded_pct":  result.verify.overall_grounded_pct,
            "overall_flagged_count": result.verify.overall_flagged_count,
            "verdicts_summary": [
                {
                    "artifact":      v.artifact,
                    "total_claims":  v.total_claims,
                    "grounded":      v.grounded_claims,
                    "flagged":       len(v.flagged_claims),
                    "flagged_examples": [c["token"] for c in v.flagged_claims[:6]],
                    "attribution":   v.attribution,
                }
                for v in result.verify.verdicts
            ],
        }
    return out


# ─── Experience-capture store (flag_experience_candidate + reply parsing) ────

def _experience_candidates_guard(_input: dict) -> None:
    """Stages a candidate into feedback/experience-candidates.jsonl. Allowed
    subtree per guards.py — the guard is here to ensure the file path
    matches the staging file, not the durable store. Durable store writes
    are reachable only through confirm_candidate, which is itself fenced
    in by reply-parser logic, not by a raw chat tool."""
    guards.assert_write_allowed(
        REPO_ROOT / "feedback" / "experience-candidates.jsonl"
    )


@register_tool(
    name="flag_experience_candidate",
    description=(
        "Stage a candidate experience-detail for Jordan Avery's morning confirmation. "
        "Use this when Jordan Avery says 'remember this', 'save this', 'log this' or "
        "similar EXPLICIT trigger in chat, AND the factual detail to capture "
        "is visible in the recent history (his prior or current turn). "
        "The verbatim MUST be a literal substring of a real Jordan Avery user turn "
        "from feedback/telegram-history.jsonl — paraphrase or summary is "
        "REJECTED at the API level (VerbatimMismatchError). The model_summary "
        "field is for Jordan Avery's eyes only in the morning digest; it NEVER flows "
        "to writers or the verifier. The candidate lands in staging — Jordan Avery "
        "confirms in the morning digest before it enters the durable store."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "verbatim": {
                "type": "string",
                "description": (
                    "EXACT substring (literally present in the source turn) of "
                    "the factual detail to capture. Will be rejected if it is "
                    "not a substring of the cited turn's text."
                ),
            },
            "source_turn_ts": {
                "type": "string",
                "description": (
                    "ISO timestamp of the Jordan Avery user turn this verbatim came "
                    "from (matches the 'ts' field in telegram-history.jsonl). "
                    "Usually the most recent user turn, or one turn back if "
                    "the explicit trigger was a standalone message."
                ),
            },
            "model_summary": {
                "type": "string",
                "description": (
                    "Your one-sentence interpretation for Jordan Avery's review in the "
                    "morning digest. NEVER flows to writers or verifier."
                ),
            },
            "suggested_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "2-5 tags Jordan Avery will see (account names, metrics, outcomes). "
                    "He can edit these at confirmation."
                ),
            },
        },
        "required": ["verbatim", "source_turn_ts"],
    },
    mutating=True,
    guard=_experience_candidates_guard,
)
def _flag_experience_candidate(input_obj: dict) -> dict:
    from alice.persistence import experience_store
    try:
        cid = experience_store.flag_experience_candidate(
            verbatim       = (input_obj.get("verbatim") or "").strip(),
            source_turn_ts = (input_obj.get("source_turn_ts") or "").strip(),
            trigger        = "explicit",
            model_summary  = (input_obj.get("model_summary") or "").strip(),
            suggested_tags = input_obj.get("suggested_tags") or [],
        )
    except experience_store.VerbatimMismatchError as e:
 # Surface the API-level rejection as a structured error the model
 # can recover from (e.g. retry with the actual substring).
        return {
            "ok":     False,
            "error":  "verbatim_mismatch",
            "detail": str(e),
        }
    except ValueError as e:
        return {
            "ok":     False,
            "error":  "invalid_input",
            "detail": str(e),
        }
    return {
        "ok":           True,
        "candidate_id": cid,
        "note":         (
            "Candidate staged. Jordan Avery confirms in the morning digest before "
            "the entry enters the durable store."
        ),
    }


@register_tool(
    name="list_pending_experience_candidates",
    description=(
        "Return all experience candidates with status='pending' — entries "
        "Jordan Avery hasn't confirmed or rejected yet. Use to show Jordan Avery what's "
        "waiting if he asks 'what's in my experience queue?' or to verify "
        "a flag_experience_candidate call actually staged."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _list_pending_experience_candidates(_input: dict) -> dict:
    from alice.persistence import experience_store
    return {"pending": experience_store.get_pending_candidates()}


# ─── Decision-feedback / correction store ────────────────────────────────────

# Mirrors the experience_store wiring shape. The substring-check seam at
# decision_feedback.flag_correction_candidate is the critical safety —
# Alice cannot paraphrase Jordan Avery's correction into the store. See module
# docstring for the descriptive-not-predictive boundary.

def _decision_feedback_candidates_guard(_input: dict) -> None:
    """Stages a candidate into feedback/decision-feedback-candidates.jsonl.
    Promotion to the durable feedback/decision-feedback.jsonl is reachable
    only via confirm_correction, which is fenced behind reply-parser logic
    and the digest gate, not by a raw chat tool."""
    guards.assert_write_allowed(
        REPO_ROOT / "feedback" / "decision-feedback-candidates.jsonl"
    )


@register_tool(
    name="flag_correction_candidate",
    description=(
        "Stage a correction Jordan Avery just delivered for his morning confirmation. "
        "Use this when Jordan Avery says 'log this correction', 'you were wrong about X', "
        "'that's not what I said', 'correction:', or similar EXPLICIT trigger, AND "
        "you can name (a) the prior assistant claim he's pushing back on and "
        "(b) Jordan Avery's exact correction verbatim. "
        "operator_correction MUST be a literal substring of a real Jordan Avery user turn "
        "from feedback/telegram-history.jsonl — paraphrase is REJECTED at the "
        "API level (VerbatimMismatchError). If alice_turn_ts is supplied, "
        "alice_claim MUST be a literal substring of that assistant turn. "
        "Category is one of: factual, judgment, framing, scope, tone, other. "
        "Jordan Avery confirms / edits / rejects in the morning digest before the "
        "correction enters the durable store. "
        "This store is DESCRIPTIVE — it records Alice's error patterns, not "
        "a predictive model of Jordan Avery. Do not use it to forecast his replies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "operator_correction": {
                "type": "string",
                "description": (
                    "EXACT substring of Jordan Avery's correction turn — the words "
                    "he used to push back. Substring check is enforced."
                ),
            },
            "operator_turn_ts": {
                "type": "string",
                "description": (
                    "ts of the Jordan Avery user turn containing the correction "
                    "(matches the 'ts' field in telegram-history.jsonl)."
                ),
            },
            "alice_claim": {
                "type": "string",
                "description": (
                    "EXACT substring of the prior assistant turn — the "
                    "specific claim Jordan Avery is correcting. Optional but "
                    "strongly preferred; without it the correction has no "
                    "named target. Substring-checked against the assistant "
                    "turn cited by alice_turn_ts."
                ),
            },
            "alice_turn_ts": {
                "type": "string",
                "description": (
                    "ts of the prior assistant turn (the claim being "
                    "corrected). Optional. If supplied, alice_claim is "
                    "substring-checked against that turn's text."
                ),
            },
            "category": {
                "type": "string",
                "enum": ["factual", "judgment", "framing", "scope", "tone", "other"],
                "description": (
                    "Category bin for pattern aggregation. Pick the best "
                    "fit; Jordan Avery can edit at confirmation time."
                ),
            },
            "model_summary": {
                "type": "string",
                "description": (
                    "Your one-sentence gloss for Jordan Avery's review in the "
                    "morning digest. NEVER aggregated into patterns."
                ),
            },
        },
        "required": ["operator_correction", "operator_turn_ts", "category"],
    },
    mutating=True,
    guard=_decision_feedback_candidates_guard,
)
def _flag_correction_candidate(input_obj: dict) -> dict:
    from alice.persistence import decision_feedback as df
    try:
        cid = df.flag_correction_candidate(
            operator_correction = (input_obj.get("operator_correction") or "").strip(),
            operator_turn_ts    = (input_obj.get("operator_turn_ts") or "").strip(),
            alice_claim     = (input_obj.get("alice_claim") or "").strip(),
            alice_turn_ts   = (input_obj.get("alice_turn_ts") or None) or None,
            category        = (input_obj.get("category") or "other").strip().lower(),
            trigger         = "explicit",
            model_summary   = (input_obj.get("model_summary") or "").strip(),
        )
    except df.VerbatimMismatchError as e:
        return {
            "ok":     False,
            "error":  "verbatim_mismatch",
            "detail": str(e),
        }
    except ValueError as e:
        return {
            "ok":     False,
            "error":  "invalid_input",
            "detail": str(e),
        }
    return {
        "ok":           True,
        "candidate_id": cid,
        "note":         (
            "Correction candidate staged. Jordan Avery confirms / edits / rejects "
            "in the morning digest before it enters the durable store."
        ),
    }


@register_tool(
    name="list_pending_correction_candidates",
    description=(
        "Return all correction candidates with status='pending' — corrections "
        "Jordan Avery hasn't confirmed or rejected yet. Use to verify a "
        "flag_correction_candidate call actually staged, or to show the "
        "pending correction queue."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def _list_pending_correction_candidates(_input: dict) -> dict:
    from alice.persistence import decision_feedback as df
    return {"pending": df.get_pending_candidates()}


# ─── ask_confirmation: bot-deterministic button confirmations ────────────────

# Per the Telegram-UX dispatch (build Part 1): confirmations route
# through a recognizable code path that attaches the standard keyboard, NOT
# model judgment or prose pattern-matching. Alice calls this tool when she
# wants to ask a yes/no or A/B question; the chat handler detects this call
# in tool_calls and attaches the InlineKeyboardMarkup to her reply message.

# Jordan Avery's escape hatch (NON-NEGOTIABLE): buttons NEVER block text. If Jordan Avery
# types his answer instead of tapping, button_ux.try_resolve_by_text() in
# the message handler resolves the typed reply against pending options.
# Text-typed-while-buttons-shown ALWAYS works. See button_ux.py for the
# permissive matching algorithm and the verification check (check_telegram_ux).

def _ask_confirmation_guard(_input: dict) -> None:
    guards.assert_write_allowed(REPO_ROOT / "feedback" / "button-confirmations.json")


@register_tool(
    name="ask_confirmation",
    description=(
        "Ask Jordan Avery a confirmation question with tappable button choices. Use "
        "this WHENEVER you need a yes/no or A/B decision from him before "
        "proceeding — instead of asking in free prose and waiting for him "
        "to type. The buttons render as an inline keyboard under your "
        "response message. Jordan Avery can either tap a button OR type his answer "
        "(both work; buttons never block text). Use 2-6 options; keep "
        "option codes short (<=30 chars, ascii, no ':'). Option labels are "
        "what Jordan Avery sees on the buttons; codes are the short identifier the "
        "handler resolves. Returns the registered confirmation id and the "
        "rendered button payloads — the chat handler attaches the keyboard "
        "after your turn completes."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type":        "string",
                "description": "The question text Alice wants Jordan Avery to answer.",
            },
            "options": {
                "type":  "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code":  {"type": "string", "description": "Short identifier (<=30 chars, no ':'). E.g. 'yes', 'northwind_only', 'hold'."},
                        "label": {"type": "string", "description": "Button display text Jordan Avery sees. Keep short for clean rendering."},
                    },
                    "required": ["code", "label"],
                },
                "minItems":    2,
                "maxItems":    8,
                "description": "List of 2-8 options. For yes/no: [{code:'yes',label:'Yes'},{code:'no',label:'No'}].",
            },
        },
        "required": ["question", "options"],
    },
    mutating=True,
    guard=_ask_confirmation_guard,
)
def _ask_confirmation(input_obj: dict) -> dict:
    from alice.notify import button_ux
    try:
        registered = button_ux.register(
            question = input_obj.get("question", ""),
            options  = input_obj.get("options")  or [],
        )
    except ValueError as e:
        return {"ok": False, "error": "invalid_confirmation", "detail": str(e)}
    return {
        "ok":      True,
        "conf_id": registered["conf_id"],
        "note": (
            "Confirmation registered. The chat handler will attach the "
            "inline keyboard to your reply when this turn returns. Jordan Avery can "
            "tap a button OR type his answer; both resolve."
        ),
    }
