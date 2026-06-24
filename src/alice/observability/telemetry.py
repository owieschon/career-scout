"""Alice tracing bootstrap + PII redaction.

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

Dual-export: Phoenix (local self-hosted) + LangSmith (cloud, portfolio-visible).
When LANGSMITH_API_KEY is present in jobcfg, a second BatchSpanProcessor is added
to the SAME TracerProvider, exporting via OTLP/HTTP to LangSmith's ingest endpoint.
The two exporters share one provider: spans flow to both backends with zero changes
to any llm.call instrumentation. A LangSmith bootstrap failure is fully swallowed —
Phoenix continues unaffected and llm.call is never disrupted.

Env surface:
  ALICE_TRACING            "1" to enable; unset/"0" => total no-op.
  ALICE_PHOENIX_ENDPOINT   collector base URL (default http://localhost:6006).
  ALICE_TRACE_CONTENT      "0" => never attach content attrs (input/output/tool
                           results); metadata-only. Default "1" (content allowed,
                           but always capped + redacted).
  ALICE_TRACE_MAX_CHARS    per-content-attr truncation cap (default 800).
  ALICE_TRACE_PROJECT      Phoenix project name (default "alice").
  LANGSMITH_API_KEY        LangSmith API key (loaded via jobcfg.load(); if absent,
                           LangSmith export is silently skipped).
  LANGSMITH_PROJECT        LangSmith project name (default "alice").
  LANGSMITH_OTEL_ONLY      "1" => LangSmith-only mode (skip Phoenix). Default off
                           (dual-export). Documented for completeness; not used in
                           production (dual is the preferred default).
"""
import os
import re
from alice import repo_paths

# ─────────────────────────────────────────────────────────────────────────────
# Module-level state. `tracer` is what other modules import. When tracing is
# off (or bootstrap fails), it stays a no-op tracer that produces no-op spans,
# so callers never branch on "is tracing on?" — they just open spans.
# ─────────────────────────────────────────────────────────────────────────────
_INITIALIZED = False
tracer = None  # set by init_tracing(); _NoopTracer until/unless real init succeeds
_ls_span_processor = None  # set when LangSmith BatchSpanProcessor is live


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


# ─── No-op tracer / span: used whenever tracing is off or bootstrap fails ────
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


# Start as no-op so importing this module alone (without init_tracing) is safe
# and any accidental `from telemetry import tracer; tracer.start_span(...)`
# degrades gracefully.
tracer = _NoopTracer()


# ─────────────────────────────────────────────────────────────────────────────
# Redaction — the single chokepoint every span-attribute set passes through.
# ─────────────────────────────────────────────────────────────────────────────

# Allow-list of STRUCTURED attribute names that carry no free-text PII: model
# names, task/tier labels, token counts, cost, latency, tool names, stop_reason,
# roundtrip counts. These pass through untouched (still bounded — they're scalars
# or short identifiers we set ourselves).
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
 # Structured ids we own (run / session / job), set by us, safe to pass
 # through untouched (no free-text PII).
    "session.id",
    "alice.run_id",
    "alice.job_key",
})

# CONTENT attribute names — free text that may contain PII. Always capped +
# scrubbed, and dropped entirely when ALICE_TRACE_CONTENT=0.
_CONTENT_ATTRS = frozenset({
    "input.value",
    "output.value",
    "llm.system",
    "tool.args_summary",
    "tool.result_preview",
})

# Conservative, high-precision PII patterns. Goal is to catch the obvious leaks
# (email, phone, SSN-shaped, API keys) without trying to be a full DLP engine —
# the cap + the ALICE_TRACE_CONTENT=0 kill switch are the real safety nets.
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


# ─────────────────────────────────────────────────────────────────────────────
# Outcome-feedback flywheel. All no-ops when tracing is off: predictions are
# recorded only when a real span exists; outcomes annotate only then. The
# wiring stays in place so capture starts the moment tracing is enabled.
# ─────────────────────────────────────────────────────────────────────────────
import os as _os
_PRED_SPANS_PATH = _os.path.join(
    str(repo_paths.ROOT),
    "feedback", "prediction-spans.jsonl")

# Status -> (outcome label, score): predictions graded by reality. advanced/offer
# = the fit score was borne out (1.0); rejected = not (0.0); applied = in-flight.
_OUTCOME_MAP = {
    "submitted":              ("applied", 0.5),
    "first screen scheduled": ("advanced", 0.75),
    "interviewing":           ("advanced", 1.0),
    "offer":                  ("offer", 1.0),
    "negotiating":            ("offer", 1.0),
    "closed":                 ("rejected", 0.0),
    "not a fit":              ("rejected", 0.0),
    "rejected":               ("rejected", 0.0),
}


def is_on():
    """True only when real tracing is active (init_tracing succeeded, not no-op)."""
    return _INITIALIZED and not isinstance(tracer, _NoopTracer)


def flush_langsmith(timeout_ms: int = 5000):
    """Force-flush the LangSmith BatchSpanProcessor so spans drain before process
    exit. No-op if the processor is not live. Never raises.

    Call this at end of short-lived scripts (recall_benchmark, test harnesses) where
    the process exits before the background flush thread has had time to drain its
    queue. The normal daemon never needs this — it runs continuously.
    """
    global _ls_span_processor
    if _ls_span_processor is None:
        return
    try:
        _ls_span_processor.force_flush(timeout_millis=timeout_ms)
    except Exception:
        pass


def shutdown_tracing(timeout_ms: int = 10_000):
    """Flush and shut down all span processors before process exit.

    Ensures the BatchSpanProcessor background thread drains its queue so no
    spans are silently dropped on daemon shutdown / SIGTERM. Idempotent: safe
    to call multiple times. Never raises (tracing hiccup must never break the
    bot shutdown path).

    The orchestrator should call this at bot shutdown (after the polling loop
    exits). Short-lived scripts call flush_langsmith() instead (cheaper —
    they don't need full shutdown, just a flush before sys.exit).

    Flushes in order: LangSmith processor first (remote, slower), then the
    global TracerProvider (which flushes Phoenix's processor). Both get the
    full timeout budget independently.
    """
 # 1. Flush LangSmith BatchSpanProcessor.
    flush_langsmith(timeout_ms=timeout_ms)

 # 2. Flush + shutdown the global OTel TracerProvider (Phoenix + any other
 # processors registered on it). The SDK's force_flush drains queued spans;
 # shutdown() then stops the background thread cleanly.
    try:
        from opentelemetry import trace as _otel_trace
        provider = _otel_trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=timeout_ms)
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass


def span_id_of(span):
    """Hex span id of a LIVE span, else None (no-op span / invalid context)."""
    try:
        sid = span.get_span_context().span_id
        return format(sid, "032x") if sid else None
    except Exception:
        return None


def record_prediction_span(job_key, span_id, task=""):
    """Prediction side: persist {job_key -> span_id} so a later outcome can be
    annotated back onto the span that produced the prediction. No-op if either id
    is missing (tracing off => no span_id => nothing recorded). Never raises."""
    if not (job_key and span_id):
        return
    try:
        from datetime import datetime
        from pathlib import Path
        import json as _json
        p = Path(_PRED_SPANS_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(_json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                 "job_key": job_key, "span_id": span_id,
                                 "task": task}) + "\n")
    except Exception as _e:
 # Loud, not silent: a dropped prediction record means the flywheel can
 # never annotate this job's outcome — surface it instead of swallowing.
        import sys as _sys
        print(f"[telemetry.record_prediction_span] write failed: "
              f"{type(_e).__name__}: {_e}", file=_sys.stderr)


def _latest_prediction_span(job_key):
    """Most recent span_id recorded for job_key, else None."""
    try:
        from pathlib import Path
        import json as _json
        p = Path(_PRED_SPANS_PATH)
        if not p.exists():
            return None
        found = None
        for line in p.read_text().splitlines():
            try:
                rec = _json.loads(line)
            except Exception:
                continue
            if rec.get("job_key") == job_key and rec.get("span_id"):
                found = rec["span_id"]
        return found
    except Exception:
        return None


def annotate_outcome(job_key, status):
    """Outcome side: write the real outcome back onto the prediction span for
    this job. No-op when tracing is off, the status isn't an outcome, or no
    prediction span was recorded. NEVER raises — observability must not break a
    ledger write.

    Writes to Phoenix (by span_id) and, when LangSmith is live, to LangSmith
    (by run_id looked up via REST query). The LangSmith path is best-effort and
    swallowed independently — a LangSmith failure never blocks the Phoenix write.
    """
    if not is_on():
        return
    mapped = _OUTCOME_MAP.get((status or "").strip().lower())
    if not (mapped and job_key):
        return
    span_id = _latest_prediction_span(job_key)
    if not span_id:
        return
    label, score = mapped

 # ── Phoenix annotation ───────────────────────────────────────────────────
    try:
        import phoenix.client as _px
 # Phoenix 16.x: annotation API is under .spans.add_span_annotation,
 # not .annotations.add_span_annotation (which does not exist).
        _px.Client().spans.add_span_annotation(
            span_id=span_id, annotation_name="outcome",
            label=label, score=score, annotator_kind="HUMAN",
            metadata={"status": status, "job_key": job_key},
        )
    except Exception:
 # The span_id + outcome stay joinable via prediction-spans.jsonl + the
 # status-write journal regardless, so no data is lost if this no-ops.
        pass

 # ── LangSmith feedback annotation (best-effort) ──────────────────────────
 # LangSmith OTLP ingest assigns run_ids deterministically from the OTel span_id:
 # run_id = UUID(bytes = b'\x00'*8 + bytes.fromhex(span_id))
 # i.e. 00000000-0000-0000-{span_id[0:4]}-{span_id[4:16]}
 # This means we can compute the LangSmith run_id directly from the span_id
 # already stored in prediction-spans.jsonl — no REST lookup needed.
 # Example: Phoenix span_id 42f1400b690e482e ->
 # LangSmith run_id 00000000-0000-0000-42f1-400b690e482e.
    global _ls_span_processor
    if _ls_span_processor is None:
        return  # LangSmith not live; skip
    try:
        import uuid as _uuid
        try:
            from alice import jobcfg as _jcfg
        except ImportError:
            from scripts import jobcfg as _jcfg  # fallback when not on sys.path
        _cfg = _jcfg.load()
        _ls_key = _cfg.get("LANGSMITH_API_KEY", "")
        if not _ls_key:
            return
 # Derive the LangSmith run_id from the OTel span_id (16-hex = 8 bytes).
 # LangSmith zero-pads to 16 bytes (128-bit UUID).
        _span_bytes = bytes.fromhex(span_id.zfill(16))  # ensure 16 hex chars
        _run_uuid = str(_uuid.UUID(bytes=b"\x00" * (16 - len(_span_bytes)) + _span_bytes))
        from langsmith import Client as _LSClient
        _ls_client = _LSClient(api_key=_ls_key)
        _ls_project = _cfg.get("LANGSMITH_PROJECT", "alice")
        _ls_client.create_feedback(
            run_id=_run_uuid,
            key="outcome",
            score=score,
            comment=label,
            value=label,
            extra={"status": status, "job_key": job_key},
        )
    except Exception:
 # Swallow entirely — LangSmith feedback failure never blocks Phoenix or ledger.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap.
# ─────────────────────────────────────────────────────────────────────────────
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
        return  # Phoenix failed; skip LangSmith wiring below

 # ── LangSmith dual-export (additive; Phoenix path above must have succeeded) ──
 # When LANGSMITH_API_KEY is present, add a second BatchSpanProcessor to the
 # SAME TracerProvider. Spans then flow to BOTH backends. A failure here is
 # swallowed completely — Phoenix and llm.call are NEVER affected.

 # Spec (verified against LangSmith OTLP docs):
 # endpoint : https://api.smith.langchain.com/otel/v1/traces
 # headers : x-api-key + Langsmith-Project (header names case-sensitive)

 # LANGSMITH_OTEL_ONLY=1 enables LangSmith-only mode (Phoenix exporter skipped),
 # but dual is the production default and is what ships here.
    try:
        import sys as _sys
 # jobcfg lives alongside telemetry.py in scripts/. Support both direct
 # invocation (PYTHONPATH=scripts) and package import (from scripts import).
        try:
            from scripts import jobcfg as _jobcfg
        except ImportError:
            from alice import jobcfg as _jobcfg  # noqa: F401 (fallback when scripts/ is on sys.path)
        _cfg = _jobcfg.load()
        _ls_key = _cfg.get("LANGSMITH_API_KEY", "")
        if _ls_key:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            _ls_project = _cfg.get("LANGSMITH_PROJECT", "alice")
            _ls_exporter = OTLPSpanExporter(
                endpoint="https://api.smith.langchain.com/otel/v1/traces",
                headers={
                    "x-api-key": _ls_key,
                    "Langsmith-Project": _ls_project,
                },
            )
 # export_timeout_millis is raised to 60s so the background flush
 # thread doesn't time out under a brief network hiccup or a large
 # batch. The daemon is long-lived, so the longer timeout costs
 # nothing on the hot path (spans are queued and flushed off it by
 # the background thread). max_export_batch_size stays at the default
 # (512); schedule_delay stays at the default (5s) so spans export
 # promptly. Fail-open: if this constructor gains new required params
 # in a future SDK version the outer except swallows the failure and
 # Phoenix continues unaffected.
            _ls_processor = BatchSpanProcessor(
                _ls_exporter,
                export_timeout_millis=60000,
            )
 # phoenix.otel's TracerProvider.add_span_processor() has a custom
 # signature: add_span_processor(..., replace_default_processor=True).
 # By default it REMOVES Phoenix's own processor (the "default") before
 # adding the new one — exactly the wrong behaviour for dual-export.
 # Passing replace_default_processor=False preserves Phoenix's processor
 # and APPENDS the LangSmith processor to the same provider. If the
 # provider is a plain SDK TracerProvider (no phoenix override), the
 # kwarg is silently ignored — both paths are safe.
            try:
                provider.add_span_processor(
                    _ls_processor, replace_default_processor=False
                )
            except TypeError:
 # Fallback: standard SDK TracerProvider.add_span_processor() doesn't
 # accept the Phoenix kwarg — call without it (no-replace semantics
 # are the default for standard SDK).
                provider.add_span_processor(_ls_processor)
 # Store the processor for force-flush in verify() helper below.
            global _ls_span_processor
            _ls_span_processor = _ls_processor
    except Exception as _ls_err:
 # Swallow entirely. A bad key, network error, or package missing must
 # NEVER surface here — Phoenix is already running above.
        try:
            import sys as _sys2
            print(f"[telemetry] LangSmith exporter bootstrap failed (Phoenix unaffected): "
                  f"{type(_ls_err).__name__}: {_ls_err}", file=_sys2.stderr)
        except Exception:
            pass
