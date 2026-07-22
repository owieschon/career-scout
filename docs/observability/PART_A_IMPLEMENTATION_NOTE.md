# Part A — Phoenix Execution Tracing for Alice (Implementation Note)

<!-- sourcebound:purpose -->
Use this historical implementation note to inspect the proposed Phoenix tracing patch and its original verification record; use `../OBSERVABILITY.md` and `PRODUCTION_STANDARD.md` for current paths and commands.
<!-- sourcebound:end purpose -->

> **Original status (preserved):** The tracing code is landed and OFF by default
> (`ALICE_TRACING` unset means a total no-op): `scripts/telemetry.py` on disk,
> manual spans in `llm.py`, on-demand collector `scripts/phoenix_capture.sh`.
> Not yet done is live enablement — `ALICE_TRACING=1` + `phoenix serve` +
> capturing the live 121K baseline — which is an operator-greenlight gate (paid
> Anthropic calls on the live daemon plus restart discipline). The `read_sheet`
> cost fix is also landed. So tracing is built but off; turning it on (and thus
> starting the live capture) awaits the operator's greenlight and a free daemon
> lane. **Scope:** Full execution tracing of every Alice LLM call with PII
> redaction built in from line one, plus a documented before/after plan for the
> `read_sheet` token-bloat bug. Cadence Analytics is out of scope. **Readiness
> gate:** Cleared — the readiness audit returned a green verdict; the RED
> counters were dev/test artifacts, safe to instrument. **Reviewer action:**
> This single doc is the deliverable. Read §(d) fail-safes and §(f) riskiest-part
> before approving. Apply by hand; do not let an agent apply it.
>
> **Current disposition:** Do not execute commands or apply diffs from this
> record. Current code lives under `src/alice/`; the supported architecture is
> [`../OBSERVABILITY.md`](../OBSERVABILITY.md) and the current runbook is
> [`PRODUCTION_STANDARD.md`](PRODUCTION_STANDARD.md).


---

## (a) Grounding re-verification — live tree vs. the audit's line refs

The [archived audit](../archive/observability/PHOENIX_AUDIT.md) is a snapshot. I re-read the live tree (`scripts/llm.py` in full; `run_daily.py` and `telegram_bot.py` via grep line-anchoring) and reconciled every critical line ref. The audit's core architectural finding holds: **no Anthropic SDK; one hand-rolled `urllib` chokepoint; `openinference-instrumentation-anthropic` would capture nothing here; manual span wrapping at `call()` instruments all ~20 call sites at once.**

| Anchor | Audit ref | Live tree (verified) | Status |
|---|---|---|---|
| `llm.call(...)` signature | `llm.py:499` | `def call(...)` at **line 499** | ✅ exact |
| `call()` arg list | (implied) | `task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0, retries=2, retry_delay=2.0, tools=None, tool_executor=None, effort=None, tier=None, max_tool_roundtrips=_MAX_TOOL_ROUNDTRIPS` (lines 499–505) | ✅ |
| HTTP wire | `_http_call_once` `llm.py:454` | **line 454**, `urlopen` at **473** | ✅ exact |
| Outer `try:` that opens the multi-round body | (implied near 575) | **line 575** | ✅ |
| Tool-roundtrip loop `while True:` | within `576-659` | **line 576** | ✅ |
| Tool-result append to `payload["messages"]` | `llm.py:657-658` | **lines 657-658** (`assistant` content then `user` tool_results) | ✅ exact |
| Clean-path log + return | (implied ~657-659) | `_log_call(..., True, ...)` at **673-674**; terminal `return {...}` at **680-692** | ⚠️ **correction** — the success `return` is at **680-692**, not at/near 659. 659 is the `continue` that re-enters the loop after a tool roundtrip. |
| Exception path | `llm.py:693` | `except Exception as e:` at **line 693**; failure `_log_call(..., False, ...)` at **695-700**; `raise` at **701** | ✅ exact |
| `_MAX_TOOL_ROUNDTRIPS` | audit muddled it ("`=36? → 8`") | **`= 8`** at **line 36** | ⚠️ **correction** — it is unambiguously `8`; the audit's "36?" is a typo/artifact. |
| `run_daily.py` `main()` | "run_daily.py main()" | `def main()` at **line 51**; body first line `log("")` at **52**; `__main__` guard **125-126** | ⚠️ **correction** — `main()` is at **51** (not 44); guard at **125-126** (file is 126 lines). |
| `telegram_bot.py` `main()` / polling seam | "`app.run_polling` startup seam" | `def main()` at **line 2520**; body starts **2521** (`import obs; obs.init("telegram_bot")`); builder `app = Application.builder()...` at **2543**; `app.run_polling(drop_pending_updates=True)` at **2559**; `__main__` guard **2562-2563** (file is 2563 lines) | ✅ variable IS `app` (audit's `app.run_polling` was correct); **line-number correction** — `main()` is at **2520**, not the "1456" my first grounding pass mis-grabbed (1456 was an unrelated mid-file `call()` invocation with a `max_tokens=8000` comment block). Insertion point: top of `main()` body, line 2521, before `import obs`. |

**No existing redaction or tracing to lean on.** `grep -rln "init_tracing|ALICE_TRACING|arize|phoenix|openinference"` over `scripts/` → 0 matches. There is no `_redact`, no `tracer`, no OTel imports in `scripts/` (OTel is present only transitively under `sentry-sdk`, unused by Alice code). `telemetry.py` is genuinely net-new; redaction assumes **no upstream scrubbing**.

**`read_sheet` consumers (for the separate fix spec, enumerated here so the projection isn't under-returned):** `read_sheet` is a **model-facing tool**, not a Python API — it is *defined* in `scripts/tools.py` (`@register_tool name="read_sheet"` at line 162; impl `_read_sheet` at line 183) and the model is *told it exists* in the tool-name lists embedded in `scripts/telegram_bot.py:1347` and `scripts/imap_reply.py:857`. It has **no direct Python call sites** — the model invokes it via `tool_executor=alice_tools.dispatch`. (`whimsy.py` only references the name for progress-UI phrasing; the `harness/test_*` files exercise it in tests.) Therefore "who consumes the projection" = **whatever the LLM reasons over in the chat/imap paths**, which is exactly why the fix spec demands enumerating fields the model actually uses (`job_key`, `role_title`, `score`, `status`, …) before narrowing. The narrowing decision lives in `_read_sheet` (tools.py:183). (That fix is NOT part of this patch — see §(e).)

**Grounding caveat (resolved):** my first pass mis-anchored `telegram_bot.py main()` to line 1456 — that line is actually inside an unrelated mid-file `llm.call(...)` invocation (the `max_tokens=8000` multi-file-build comment block at 1453-1465). A direct `grep -n "def main"` + a full Read of lines 2520-2563 corrected it: the real `main()` is at **2520**, builder at **2543**, polling at **2559**. The §B.4 diff below uses the corrected anchors. `run_daily.py` and `llm.py` were read in full and are authoritative (see the `run_daily.py` correction immediately below).

**`run_daily.py` correction:** `def main()` is at **line 51** (not 44); its body's first statement is `log("")` at **line 52**. (The "44" in an earlier draft of the table was the `run_step` function tail, not `main`.) The §B.3 diff below uses line 51/52.

---

## (b) The proposed patch (text — NOT applied)

### B.1 — `scripts/telemetry.py` (NEW FILE, full)

```python
"""Alice tracing bootstrap + PII redaction — Part A of the Phoenix observability plan.

Design goals (in priority order):
  1. OFF by default and byte-identical to today when off. If ALICE_TRACING is
     unset or "0", init_tracing() is a complete no-op: no provider, no exporter,
     no OTel imports executed, zero overhead. Importing this module must also be
     cheap and side-effect-free (no OTel import at module top).
  2. Never break Alice. Tracing is observability, not a dependency. Every public
     surface here (init_tracing, span helpers, redact) must swallow its own
     errors and degrade to no-op. A tracing failure must not raise into llm.call.
  3. Redaction from line one. Every span-attribute value passes through redact().
     Content is capped + PII-scrubbed; ALICE_TRACE_CONTENT=0 drops content attrs
     entirely (metadata-only safe mode). There is NO upstream scrubbing.

Local self-hosted Phoenix ONLY. Default OTLP endpoint http://localhost:6006,
overridable via ALICE_PHOENIX_ENDPOINT. Phoenix Cloud is intentionally NOT wired.

Env surface:
  ALICE_TRACING            "1" to enable; unset/"0" => total no-op.
  ALICE_PHOENIX_ENDPOINT   collector base URL (default http://localhost:6006).
  ALICE_TRACE_CONTENT      "0" => never attach content attrs (input/output/tool
                           results); metadata-only. Default "1" (content allowed,
                           but always capped + redacted).
  ALICE_TRACE_MAX_CHARS    per-content-attr truncation cap (default 800).
  ALICE_TRACE_PROJECT      Phoenix project name (default "alice").
"""
import os
import re

## ─────────────────────────────────────────────────────────────────────────────
## Module-level state. `tracer` is what other modules import. When tracing is
## off (or bootstrap fails), it stays a no-op tracer that produces no-op spans,
## so callers never branch on "is tracing on?" — they just open spans.
## ─────────────────────────────────────────────────────────────────────────────
_INITIALIZED = False
tracer = None  # set by init_tracing(); _NoopTracer until/unless real init succeeds


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _tracing_enabled() -> bool:
    # OFF unless explicitly ALICE_TRACING=1 (or true/yes/on).
    return _truthy("ALICE_TRACING", "0")


def _content_enabled() -> bool:
    # Content attrs allowed unless explicitly disabled. Default ON, but always
    # capped + redacted by redact(). ALICE_TRACE_CONTENT=0 => metadata-only.
    return os.environ.get("ALICE_TRACE_CONTENT", "1").strip().lower() not in ("0", "false", "no", "off")


def _max_chars() -> int:
    try:
        return max(0, int(os.environ.get("ALICE_TRACE_MAX_CHARS", "800")))
    except Exception:
        return 800


## ─── No-op tracer / span: used whenever tracing is off or bootstrap fails ────
class _NoopSpan:
    def set_attribute(self, *a, **k):  # noqa: D401
        return None

    def set_status(self, *a, **k):
        return None

    def record_exception(self, *a, **k):
        return None

    def add_event(self, *a, **k):
        return None

    def end(self, *a, **k):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        # Never suppress the caller's exception (return falsy).
        return False


class _NoopTracer:
    def start_as_current_span(self, *a, **k):
        return _NoopSpan()

    def start_span(self, *a, **k):
        return _NoopSpan()


## Start as no-op so importing this module alone (without init_tracing) is safe
## and any accidental `from telemetry import tracer; tracer.start_span(...)`
## degrades gracefully.
tracer = _NoopTracer()


## ─────────────────────────────────────────────────────────────────────────────
## Redaction — the single chokepoint every span-attribute set passes through.
## ─────────────────────────────────────────────────────────────────────────────
#
## Allow-list of STRUCTURED attribute names that carry no free-text PII: model
## names, task/tier labels, token counts, cost, latency, tool names, stop_reason,
## roundtrip counts. These pass through untouched (still bounded — they're scalars
## or short identifiers we set ourselves).
_STRUCTURED_ATTRS = frozenset({
    "llm.model_name",
    "llm.provider",
    "alice.task",
    "alice.tier",
    "alice.selection_source",
    "alice.effort",
    "alice.rounds",
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.token_count.thinking",
    "llm.cost.total",
    "llm.latency_s",
    "llm.stop_reason",
    "tool.name",
    "tool.result_size",
    "tool.round",
})

## CONTENT attribute names — free text that may contain PII. Always capped +
## scrubbed, and dropped entirely when ALICE_TRACE_CONTENT=0.
_CONTENT_ATTRS = frozenset({
    "input.value",
    "output.value",
    "llm.system",
    "tool.args_summary",
    "tool.result_preview",
})

## Conservative, high-precision PII patterns. Goal is to catch the obvious leaks
## (email, phone, SSN-shaped, API keys) without trying to be a full DLP engine —
## the cap + the ALICE_TRACE_CONTENT=0 kill switch are the real safety nets.
_PII_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Anthropic-style keys and long bearer-ish tokens.
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}"), "[APIKEY]"),
    (re.compile(r"\b[A-Za-z0-9\-_]{32,}\b"), "[TOKEN]"),
]


def _scrub_pii(text: str) -> str:
    for pat, repl in _PII_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact(attr_name: str, value):
    """Single chokepoint for every span attribute value.

    - Structured attrs (allow-list): returned as-is (scalars / short ids we own).
    - Content attrs: stringified, PII-scrubbed, then truncated to ALICE_TRACE_MAX_CHARS
      with a "...[+N chars]" marker. Returns None (i.e. "do not set this attr") when
      ALICE_TRACE_CONTENT=0 — the caller MUST skip setting the attribute on None.
    - Unknown attr names: treated as content (fail-closed: scrub + cap), so a future
      attr added without updating the allow-list still gets redacted, never leaked raw.

    Never raises. On any internal error returns "[REDACTION_ERROR]" rather than
    risking a raw leak or an exception into the span path.
    """
    try:
        if attr_name in _STRUCTURED_ATTRS:
            return value
        # Everything else is treated as content (fail-closed).
        if not _content_enabled():
            return None  # caller skips setting the attr entirely (metadata-only mode)
        s = value if isinstance(value, str) else str(value)
        s = _scrub_pii(s)
        cap = _max_chars()
        if len(s) > cap:
            s = s[:cap] + f"...[+{len(s) - cap} chars]"
        return s
    except Exception:
        return "[REDACTION_ERROR]"


def set_attr(span, attr_name: str, value):
    """Set a span attribute through redact(). Skips the attr when redact() returns
    None (content disabled). Never raises."""
    try:
        red = redact(attr_name, value)
        if red is None:
            return
        span.set_attribute(attr_name, red)
    except Exception:
        pass


## ─────────────────────────────────────────────────────────────────────────────
## Bootstrap.
## ─────────────────────────────────────────────────────────────────────────────
def init_tracing():
    """Idempotent. If ALICE_TRACING is unset/"0": COMPLETE no-op (no imports,
    no provider, no exporter). Otherwise configure an OpenTelemetry tracer
    provider exporting via arize-phoenix-otel to a LOCAL Phoenix endpoint.

    Bootstrap failures (Phoenix not installed, endpoint typo, etc.) are swallowed
    and leave `tracer` as the no-op tracer — tracing-on with a broken collector
    must still never break Alice.
    """
    global _INITIALIZED, tracer
    if _INITIALIZED:
        return
    if not _tracing_enabled():
        # Total no-op: do not import OTel/Phoenix at all. tracer stays _NoopTracer.
        _INITIALIZED = True
        return
    try:
        # Imports are INSIDE the enabled branch so the off-path pays nothing.
        from phoenix.otel import register  # arize-phoenix-otel

        endpoint = os.environ.get("ALICE_PHOENIX_ENDPOINT", "http://localhost:6006")
        project = os.environ.get("ALICE_TRACE_PROJECT", "alice")
        # register() returns a configured TracerProvider wired to Phoenix's OTLP
        # collector. batch=True => background BatchSpanProcessor: exports are
        # queued and flushed off the hot path; a dead collector drops/queues
        # silently rather than blocking or raising into call().
        provider = register(
            project_name=project,
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            batch=True,
            set_global_tracer_provider=True,
        )
        tracer = provider.get_tracer("alice.llm")
        _INITIALIZED = True
    except Exception as e:
        # Tracing-on but bootstrap failed => degrade to no-op, do not raise.
        tracer = _NoopTracer()
        _INITIALIZED = True
        try:
            import sys
            print(f"[telemetry] tracing bootstrap failed, continuing without tracing: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        except Exception:
            pass
```

---

### B.2 — `scripts/llm.py` (unified diff)

The span wraps the existing multi-round body. Two import lines at the top; an
`init_tracing()` is **not** called here (entry points own that — §B.3), but the
module imports `telemetry` lazily and uses its no-op `tracer` if init never ran,
so a stray `python -c "import llm; llm.call(...)"` is still safe.

The span is opened with `start_as_current_span(...)` as a context manager around
the **entire** existing `try/except` block, so:
- entry attrs (`input.value`, `llm.system`, model/tier) are set before the loop;
- per-roundtrip tool detail is recorded as span **events**;
- completion attrs (tokens, cost, output) are set on the clean path;
- the exception path records the exception on the span;
- the `with` block guarantees `span.end()` on BOTH paths — no leaked span. The
  span context manager is OUTSIDE the existing `try`, so even the `raise` at the
  old line 701 exits through `__exit__`, which ends the span and does NOT suppress
  the exception.

```diff
--- a/scripts/llm.py
+++ b/scripts/llm.py
@@ -18,6 +18,12 @@ from datetime import date, datetime, timedelta
 from pathlib import Path

 from jobcfg import load
+
+# Tracing is optional and self-disabling. `tracer` is a no-op unless an entry
+# point called telemetry.init_tracing() with ALICE_TRACING=1, so importing it
+# here is free and never changes behavior. set_attr() routes every attribute
+# through redaction; it never raises.
+from telemetry import tracer, set_attr

 try:
     import certifi
@@ -562,7 +568,21 @@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0
     tool_calls_log: list[dict] = []
     last_err = None

-    try:
+    # Open ONE span around the whole multi-round body. The context manager is
+    # OUTSIDE the existing try/except so the span ends on every exit path
+    # (clean return, raised exception) — no leaked span. A no-op tracer makes
+    # this zero-cost when tracing is off.
+    _span_cm = tracer.start_as_current_span(f"llm.call:{task}")
+    span = _span_cm.__enter__()
+    try:
+        # Entry attributes. Structured first (no redaction), then content
+        # (redacted + capped; skipped entirely if ALICE_TRACE_CONTENT=0).
+        set_attr(span, "llm.provider", "anthropic")
+        set_attr(span, "llm.model_name", chosen_model)
+        set_attr(span, "alice.task", task)
+        set_attr(span, "alice.tier", selection["tier"])
+        set_attr(span, "alice.selection_source", selection["source"])
+        set_attr(span, "alice.effort", chosen_effort)
+        set_attr(span, "input.value", prompt)
+        if system:
+            set_attr(span, "llm.system", system)
         while True:
             rounds += 1
             if rounds > max_tool_roundtrips:
@@ -624,6 +644,16 @@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0
                     try:
                         result = tool_executor(t_name, t_input)
                         log_entry["result"] = result
+                        # Record this tool roundtrip as a span event so the
+                        # dispatch loop is visible. Args + result are summarized
+                        # and routed through redaction; result_size is structured.
+                        try:
+                            _res_str = str(result) if result is not None else ""
+                            span.add_event("tool.result", {
+                                "tool.name": redact("tool.name", t_name),
+                                "tool.round": redact("tool.round", rounds),
+                                "tool.result_size": redact("tool.result_size", len(_res_str)),
+                                **({"tool.result_preview": redact("tool.result_preview", _res_str)}
+                                   if redact("tool.result_preview", _res_str) is not None else {}),
+                                **({"tool.args_summary": redact("tool.args_summary", t_input)}
+                                   if redact("tool.args_summary", t_input) is not None else {}),
+                            })
+                        except Exception:
+                            pass
                         tool_calls_log.append(log_entry)
                         try:
                             import whimsy
@@ -639,6 +669,14 @@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0
                     except Exception as e:
                         log_entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                         tool_calls_log.append(log_entry)
+                        try:
+                            span.add_event("tool.error", {
+                                "tool.name": redact("tool.name", t_name),
+                                "tool.round": redact("tool.round", rounds),
+                                "error.type": type(e).__name__,
+                            })
+                        except Exception:
+                            pass
                         tool_results.append({
                             "type": "tool_result",
                             "tool_use_id": t_id,
@@ -671,6 +709,14 @@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0
             if tool_calls_log:
                 log_extras["tool_calls"] = tool_calls_log
             _log_call(task, chosen_model, total_in, total_out, total_cost,
                       total_latency, True, extras=log_extras)
+            # Completion attributes (clean path).
+            set_attr(span, "llm.token_count.prompt", total_in)
+            set_attr(span, "llm.token_count.completion", total_out)
+            set_attr(span, "llm.token_count.thinking", total_thinking)
+            set_attr(span, "llm.cost.total", round(total_cost, 6))
+            set_attr(span, "llm.latency_s", round(total_latency, 3))
+            set_attr(span, "alice.rounds", rounds)
+            set_attr(span, "llm.stop_reason", stop_reason or "")
+            set_attr(span, "output.value", final_text)
             if task in TASK_DAILY_TRIPWIRES:
                 try:
                     _check_tripwire(task, cost_today_by_task(task))
@@ -697,9 +743,22 @@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0
     except Exception as e:
         last_err = str(e)
         _log_call(task, chosen_model, total_in, total_out, total_cost,
                   total_latency, False, error=last_err, extras={
                       "tier":             selection["tier"],
                       "selection_source": selection["source"],
                       "rounds":           rounds,
                   })
+        # Mark the span as errored. Never let span bookkeeping mask the real
+        # exception — record, then re-raise the original.
+        try:
+            span.record_exception(e)
+            from opentelemetry.trace import Status, StatusCode  # lazy; only if installed
+            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
+        except Exception:
+            pass
         raise
+    finally:
+        # Guarantees span.end() on every path (clean return inside the loop,
+        # any raise). __exit__(None,None,None) is safe here because we already
+        # recorded the exception above; we do NOT pass exc info so __exit__
+        # cannot suppress it, and the original exception continues to propagate.
+        try:
+            _span_cm.__exit__(None, None, None)
+        except Exception:
+            pass
```

> Implementation note on the `try/finally` shape: the original function had a
> single `try/except`. The patch adds a `finally:` to that same block whose only
> job is to close the span context manager. Because `record_exception` /
> `set_status` happen in `except` and the re-`raise` is preserved, and the
> `finally` calls `__exit__(None, None, None)` (which cannot swallow the
> in-flight exception), the span always ends and the caller-visible behavior of
> `call()` is unchanged except for the side effect of emitting a span.

---

### B.3 — `scripts/run_daily.py` (unified diff)

```diff
--- a/scripts/run_daily.py
+++ b/scripts/run_daily.py
@@ -51,6 +51,8 @@ def run_step(label, args, fatal=False):

 def main():
+    from telemetry import init_tracing
+    init_tracing()
     log("")
     log(f"=== run {datetime.datetime.now()} ===")
```

> Caveat for the reviewer (documented, not a blocker): `run_daily.py` orchestrates
> 14 steps as **isolated `subprocess.run` children** (`run_step`, line 19). Calling
> `init_tracing()` in the parent's `main()` does **not** propagate the tracer into
> those subprocesses — each child is a fresh interpreter. With `ALICE_TRACING=0`
> (the default and the state for the manual baseline/after capture, which both go
> through the **telegram** path, not the daily batch), this is irrelevant. If
> full daily-batch tracing is wanted later, the right move is to set
> `ALICE_TRACING=1` in the **environment** (`run_daily.sh` / the launchd plist)
> so every child inherits it and calls its own `init_tracing()` at its own
> `main()`. That is out of scope for Part A and noted for Part B.

---

### B.4 — `scripts/telegram_bot.py` (unified diff)

```diff
--- a/scripts/telegram_bot.py
+++ b/scripts/telegram_bot.py
@@ -2519,6 +2519,8 @@

 def main():
+    from telemetry import init_tracing
+    init_tracing()
     import obs
     obs.init("telegram_bot")
```

> Anchor note: `main()` is at **line 2520**; its body opens with
> `import obs; obs.init("telegram_bot")` at **2521-2522**; the builder is
> `app = Application.builder().token(token).build()` at **2538**; the polling
> seam is `app.run_polling(drop_pending_updates=True)` at **2559** (the audit's
> `app.run_polling` variable name was correct). `init_tracing()` goes at the
> **very top of `main()`'s body (new line 2521, before `import obs`)**, before
> the builder/handlers/polling setup, so the tracer is live for the whole daemon
> lifetime. The telegram daemon is a single long-running process (no subprocess
> fan-out), so one `init_tracing()` here covers every `telegram_chat` turn —
> which is exactly the `read_sheet` before/after path. **Daemon-restart
> discipline applies:** after applying, `launchctl unload`+`load` (not kill),
> and verify new code via a prompt/log marker (per Alice's launchd discipline).

---

## (c) Redaction design

**One chokepoint:** `redact(attr_name, value)` in `telemetry.py`. Every span
attribute is set via `set_attr(span, name, value)`, which calls `redact()` and
skips the attribute when `redact()` returns `None`. No code path sets a raw
attribute directly.

**Two attribute classes:**
- **Structured (allow-list, `_STRUCTURED_ATTRS`):** model, provider, task, tier,
  selection source, effort, token counts (prompt/completion/thinking), cost,
  latency, stop_reason, tool name, tool result *size*, roundtrip count. These are
  scalars or short identifiers Alice itself produces — no PII, no redaction,
  passed through as-is.
- **Content (`_CONTENT_ATTRS` + everything not on the allow-list):**
  `input.value`, `output.value`, `llm.system`, `tool.args_summary`,
  `tool.result_preview`. Treated fail-closed — an attr name we forgot to classify
  is handled as content, never leaked raw.

**Content handling (three layers, defense in depth):**
1. **Kill switch:** `ALICE_TRACE_CONTENT=0` → `redact()` returns `None` for all
   content attrs → `set_attr` skips them → **metadata-only safe mode** (tokens,
   cost, model, tool names, sizes still flow; no prompt/response text ever leaves
   the process).
2. **PII scrub:** conservative high-precision regexes replace email, phone,
   SSN-shaped, Anthropic `sk-ant-…` keys, and long token-shaped strings with
   typed placeholders. Deliberately not a full DLP engine — the cap and the kill
   switch are the real safety nets; the regexes catch the obvious leaks.
3. **Truncation:** every content value is capped to `ALICE_TRACE_MAX_CHARS`
   (default **800**) with a `...[+N chars]` marker. This is what tames the
   `read_sheet` 121K-token payload at the span layer: even if a full sheet rides
   in `input.value`, only the first 800 chars (scrubbed) are attached to the
   span. **This is intentionally consistent with the `read_sheet` fix spec** —
   redaction stops the bloat leaking into spans; the read_sheet fix stops it
   inflating the prompt. Complementary, not redundant.

**`redact()` never raises** — on internal error it returns `"[REDACTION_ERROR]"`
rather than risk either a raw leak or an exception into the span path.

---

## (d) Fail-safe guarantees (reasoned — could not run to verify)

### Fail-safe 1 — Tracing OFF ⇒ byte-identical to today

- `ALICE_TRACING` unset/`0` ⇒ `init_tracing()` hits the `if not _tracing_enabled(): return` branch **before any OTel/Phoenix import**. No provider, no exporter, no background threads.
- `telemetry.py`'s module top imports only `os` and `re` (stdlib, already loaded) and constructs a `_NoopTracer()` — no heavy import as a side effect of `from telemetry import tracer, set_attr` in `llm.py`.
- In `call()`, `tracer.start_as_current_span(...)` returns a `_NoopSpan`; `set_attr` calls `redact()` (pure string ops, cheap) then `span.set_attribute` which is a no-op `return None`. `add_event` is a no-op. `_span_cm.__exit__` is a no-op.
- The wrapping `try/finally` changes control flow only by adding a `finally` that calls a no-op `__exit__`. The clean-path `return {...}` returns the identical dict; the exception path re-`raise`s the identical exception. **No new latency of consequence, no new exceptions, return value unchanged.** "Byte-identical" is exact for the returned dict and the raised exception; the only added work on the off-path is a handful of no-op method calls and cheap string passes through `redact()` for structured attrs (which short-circuit on the allow-list) — negligible and non-observable to callers.
  - *Honesty note:* strictly, `redact()` does run for the entry/completion attrs even when off, because `set_attr` calls it before discovering the span is a no-op. For structured attrs it returns immediately (allow-list hit). For the content attrs (`input.value`, `output.value`), `redact()` will scrub+truncate the strings even though the no-op span discards them — a small amount of wasted CPU. If the reviewer wants *true* zero work when off, add an early `if isinstance(tracer, _NoopTracer): return` guard at the top of `set_attr`. I left it out to keep `set_attr` honest about always redacting, but it's a one-line hardening if the off-path cost matters. **This is the only respect in which "byte-identical" is approximate, and it is non-observable to callers.**

### Fail-safe 2 — Tracing ON but no collector reachable ⇒ graceful no-op

- `register(..., batch=True)` installs a **BatchSpanProcessor**: spans are queued in memory and exported on a background thread. Export I/O never happens on `call()`'s thread, so a dead/unreachable Phoenix collector cannot block or raise into `call()`.
- If `register()` itself fails at bootstrap (Phoenix not installed, bad endpoint, OTLP setup error), the `except Exception` in `init_tracing()` swallows it, leaves `tracer` as `_NoopTracer`, prints one stderr line, and sets `_INITIALIZED=True`. Subsequent spans are no-ops.
- Export failures *after* a healthy bootstrap are handled inside the OTel exporter/processor (they log/drop), not surfaced to the producer. The producer (`call()`) only ever touches `start_as_current_span` / `set_attribute` / `add_event` / `end`, all of which are local, non-blocking operations.

### Fail-safe 3 — Instrumentation cannot change `call()`'s return value or raise a new exception into callers

- The span context manager is opened with `__enter__()` and closed with `__exit__(None, None, None)` in a `finally`. We deliberately do **not** route the live exception through `__exit__` (we pass `None, None, None`), so `__exit__` has no opportunity to suppress the caller's exception — the original `raise` in the `except` block is what propagates.
- Every tracing side effect is wrapped in its own `try/except` that swallows: `set_attr` (and the `redact` it calls) never raise; `span.add_event(...)` calls are wrapped; `span.record_exception` / `set_status` are wrapped; `_span_cm.__exit__` is wrapped.
- The clean-path `return {...}` is **unchanged** — completion attrs are set *before* `return`, via `set_attr` (which can't raise), and the returned dict literal is byte-identical to the original.
- Net: the only behaviors that can flow out of `call()` are still (a) the original return dict, or (b) the original exception. Tracing adds spans as a pure side effect and can, at worst, fail to record a span — never break the call.

**Live-system framing:** Alice runs the operator's real, time-pressured job search on a daemon (`telegram_bot.py`) with budget tripwires already firing. The design treats tracing as strictly additive observability: off by default, self-disabling on any fault, structurally unable to alter the LLM call's contract.

### (d.1) Verification results — EXECUTED 2026-05-30 (steps A + B), by the Move-3 session

The guarantees above were *reasoned, not run* in the original note. They have now
been executed against the live tree (handoff steps A + B; C/D remain gated). The
four edits were re-verified at line refs shifted by the Move-3 telegram_bot.py
work (`llm.py`/`run_daily.py` untouched by Move 3).

**Step A — patch intact, compiles:**
- `telemetry.py` module-top imports are `os`, `re` only; `from phoenix.otel import register` is inside `init_tracing()`'s enabled branch (line 218). So `from telemetry import tracer, set_attr, redact` in `llm.py` cannot fail — the highest-blast-radius assumption (§f.3) holds. ✅
- `llm.py` span shape verified: `_span_cm = tracer.start_as_current_span` (585) → `__enter__` (586) → `try` (587) → `except Exception as e` (752, records + re-raises) → `finally` (768) → `_span_cm.__exit__(None, None, None)` (775). Manual shape preserved (not a `with`). ✅
- `run_daily.py:52-53` and `telegram_bot.py:2544-2545` `init_tracing()` at the top of each `main()` (telegram before `import obs`). ✅
- All four `py_compile` clean. ✅

**Step B — fail-safes (run safely; no API spend, no collector):**
- B.1 OFF (`ALICE_TRACING` unset): `init_tracing()` no-op; `tracer` stays `_NoopTracer`; `import llm` succeeds; `llm.tracer` is `_NoopTracer`. ✅
- B.2 ON-but-no-collector (`ALICE_TRACING=1`, phoenix not installed): `init_tracing()` degrades to no-op — one stderr line (`ModuleNotFoundError: No module named 'phoenix'`), `tracer` stays `_NoopTracer`, span/`set_attr` calls run harmlessly, nothing raises. ✅
- Redaction (invariant #4): structured attrs pass through; email/phone/`sk-ant` key scrubbed; unknown attr fail-closed (scrubbed as content); cap applies (`...[+N chars]`); `ALICE_TRACE_CONTENT=0` drops content to `None`. ✅
  - *Flag:* the `[TOKEN]` pattern `\b[A-Za-z0-9_-]{32,}\b` scrubs ANY 32+ char alnum run — aggressive (could over-redact long IDs/slugs/base64). Safe direction for PII, but worth knowing if a trace looks unexpectedly `[TOKEN]`-heavy.

**Step B addendum — import-binding finding (NEW; not in the §(d) reasoning):**
`llm.py` uses `from telemetry import tracer`, which binds the name at import time,
while `init_tracing()` REASSIGNS the module global `telemetry.tracer`. Verified
empirically: after a simulated init, `llm.tracer is telemetry.tracer` → **False**.
So tracing-ON *capture* depends on `llm` being imported AFTER `init_tracing()`.
- **Holds today:** `import telegram_bot` (module-load, no `main()`) leaves `llm`
  and `telemetry` OUT of `sys.modules` — `import llm` is lazy (handlers, lines
  312/622/1037/1150), so the first bind happens after `main()`'s `init_tracing()`.
  **Step C will capture in the current daemon.** ✅
- **But fragile:** a future top-level `import llm` in `telegram_bot.py` (or any
  module it imports at load) would bind `_NoopTracer` before init and *silently*
  disable capture — no error. **Recommended hardening (optional, before/with
  Step C):** have `llm.py` reference `telemetry.tracer` dynamically (`import
  telemetry`; `telemetry.tracer.start_as_current_span(...)`) instead of binding
  it via `from telemetry import tracer`. Converts an accident-of-import-order
  into a guarantee. Not a blocker — capture works today.

**Steps C (live before/after capture) and D (`read_sheet` fix) NOT executed** —
they need `pip install arize-phoenix-otel arize-phoenix`, a local `phoenix serve`,
a daemon restart, and a real heavy `telegram_chat` turn spending Anthropic budget
on the operator's live search. **Left gated for the operator's explicit go.**

---

## (e) Manual before/after runbook (NOT executed here)

> **I did NOT perform any of these steps.** Trace capture needs a local Phoenix
> instance and a live Alice run (a human step). This is the exact sequence for
> the user to run by hand. The `read_sheet` fix is a **separate** spec
> (`~/Downloads/files (6)/alice-read_sheet-fix-spec.md`) applied *between*
> baseline and after-trace; it is not part of this patch.

1. **Apply this patch by hand.** Create `scripts/telemetry.py` from §B.1; apply the §B.2/B.3/B.4 diffs. Review first; do not auto-apply.
2. **Install the deps** (names only — see §(f); do NOT install as part of review):
   `pip install arize-phoenix-otel arize-phoenix`
3. **Stand up local Phoenix** (self-hosted, local-only): `phoenix serve`
   (default UI/OTLP on `http://localhost:6006`). Confirm the UI loads.
4. **Enable tracing for the daemon:** set `ALICE_TRACING=1` (and optionally
   `ALICE_PHOENIX_ENDPOINT`, `ALICE_TRACE_MAX_CHARS`, `ALICE_TRACE_CONTENT`) in the
   telegram daemon's environment, then restart the daemon per the launchd
   discipline (`launchctl unload` + `load`, not kill). Verify new-code via a
   prompt/log marker before trusting the run.
5. **Capture the BASELINE trace.** Re-drive the exact `telegram_chat`/`read_sheet`
   path the audit measured at `in_tokens: 121560` (audit §9,
   `time-cost-log.jsonl`, 2026-05-30T11:53). In the Phoenix UI, open the
   `llm.call:telegram_chat` span and record `llm.token_count.prompt` (and the
   per-roundtrip `tool.result` events showing the full-sheet payload re-riding).
6. **Apply the SEPARATE `read_sheet` fix** from the fix spec (propose → review →
   apply; modifies `tools.py`, optionally the `llm.py:657-658` append site). Keep
   its size cap consistent with `ALICE_TRACE_MAX_CHARS`. Restart the daemon again.
7. **Capture the AFTER trace.** Re-drive the *same* `telegram_chat` path. Record
   the new `llm.token_count.prompt` on the `llm.call:telegram_chat` span.
8. **Record the delta** in this doc, next to the baseline:
   `baseline prompt tokens: 121560 → after: <captured by hand> (Δ <…>)`.
   Sanity-check (not a full eval — that's Part B) that the chat's answers on a
   couple of representative turns are unchanged in substance.

**Baseline (from audit, not re-measured here):** `in_tokens: 121560` on a single
`telegram_chat` turn (audit §9). **After:** _to be filled in by the manual run._

---

## (f) Named pip dependencies (do NOT install during review)

- **`arize-phoenix-otel`** — the OpenTelemetry bootstrap shim (`phoenix.otel.register`) that wires an OTel TracerProvider to Phoenix's OTLP collector. This is the import `telemetry.py` uses.
- **`arize-phoenix`** — the local self-hosted Phoenix server/UI (`phoenix serve`) that receives and displays the traces on `http://localhost:6006`.

Both are open-source and run entirely locally; **Phoenix Cloud is intentionally not wired.** No `openinference-instrumentation-anthropic` is needed (Alice has no Anthropic SDK to auto-patch — the whole reason for the manual span). OTel core packages arrive transitively with `arize-phoenix-otel`.

---

## Riskiest part for the reviewer to scrutinize

**The `try/finally` + span-context-manager surgery in `llm.py:call()` (§B.2).**
It is the one change that touches live control flow on the path that runs the operator's
real job search. The specific things to verify by eye:
1. The `finally:` calls `_span_cm.__exit__(None, None, None)` — passing `None`s
   (not the live exc info) so `__exit__` **cannot** suppress the in-flight
   exception. If someone "tidies" this to `with tracer.start_as_current_span(...) as span:`,
   the `with` would pass real exc info to `__exit__`, and a buggy/overzealous
   span impl could swallow Alice's exception. The manual `__enter__`/`finally
   __exit__` shape is deliberate — keep it.
2. The original clean-path `return {...}` (old lines 680-692) must remain
   **inside** the `try` and unchanged; completion attrs are set immediately
   before it via no-raise `set_attr`.
3. No-op-when-off rests on `from telemetry import tracer` resolving to
   `_NoopTracer` whenever `init_tracing()` didn't run or ran with
   `ALICE_TRACING=0`. Confirm the import can't fail (telemetry.py top-level
   imports only stdlib) — an ImportError there would break `llm.py` for every
   call site, which would be catastrophic. (It can't, by construction, but it's
   the highest-blast-radius assumption in the patch.)
