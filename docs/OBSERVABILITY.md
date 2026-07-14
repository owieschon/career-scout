# Alice LLM Observability — Arize Phoenix / OpenTelemetry

<!-- clean-docs:purpose -->
**Status:** Instrumented and live-demonstrated. Production enablement requires the operator's launchd greenlight. Read this page before changing or relying on Alice LLM Observability — Arize Phoenix / OpenTelemetry so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->
<!-- clean-docs:allow doc-length reason="The Alice LLM Observability — Arize Phoenix / OpenTelemetry reader path stays in one file because splitting it would separate its operating context from its verification material" -->


---

## What this is

Alice makes every LLM call via a raw `urllib`-based HTTP client (no Anthropic SDK, no LangChain).
Standard auto-instrumentation hooks do not apply. Every span is created manually using the
OpenTelemetry Python API, exported via `arize-phoenix-otel` to a local self-hosted Phoenix
instance (`http://localhost:6006`). This document describes the instrumentation architecture,
the attribute schema, PII redaction, and the outcome-annotation eval loop.

**Resume line this backs:**  
*Built manual OTel tracing on a raw-HTTP (non-SDK) Anthropic client; wired outcome-feedback
annotation loop via Arize Phoenix (self-hosted); spans carry provider/model/task/tier/effort,
PII-scrubbed content, and real outcomes annotated back onto prediction spans.*

---

## Files

| File | Role |
|------|------|
| `src/alice/observability/telemetry.py` | Tracing bootstrap, `redact()`, `set_attr()`, B3 outcome flywheel |
| `src/alice/llm/llm.py:614+` | Manual `llm.call:{task}` span wrapping the raw HTTP loop |
| `src/alice/pipeline/fit_judge.py:401+` | `fit_judge.role` parent span wrapping each judge invocation |
| `src/alice/observability/obs.py` | Sentry turn-level spans (separate concern, not OTel/Phoenix) |

---

## Architecture

### Enable flag

Tracing is **OFF by default**. No imports execute, no provider is initialized:

```
ALICE_TRACING=1        # "1"|"true"|"yes"|"on" => real tracing; anything else => total no-op
ALICE_PHOENIX_ENDPOINT # OTLP base URL (default http://localhost:6006)
ALICE_TRACE_PROJECT    # Phoenix project name (default "alice")
ALICE_TRACE_CONTENT    # "0" => metadata-only (drop input/output attrs entirely)
ALICE_TRACE_MAX_CHARS  # per-content-attr truncation cap (default 800 chars)
```

`init_tracing()` (`telemetry.py:309`) is idempotent. When disabled it sets `_INITIALIZED=True`
and returns, leaving `tracer` as `_NoopTracer`. When enabled it calls `phoenix.otel.register()`
with `batch=True` (background `BatchSpanProcessor`) so a dead collector drops spans silently
rather than blocking the model call path.

### Span creation — manual, not auto-instrumented

`llm.call()` (`llm.py:614`) opens a span with `tracer.start_as_current_span(f"llm.call:{task}")`,
enters it manually (not via `with`) so the `finally` clause can close it without any risk of
suppressing an in-flight Alice exception:

```python
_span_cm = telemetry.tracer.start_as_current_span(f"llm.call:{task}")
span = _span_cm.__enter__()
try:
    ...  # multi-round HTTP loop
finally:
    _span_cm.__exit__(None, None, None)
```

`fit_judge.py` wraps each judge call in a `fit_judge.role` parent span (`fit_judge.py:407`),
which becomes the parent of the `llm.call:fit_judge` child span in Phoenix traces.

---

## Attribute schema

### Structured attributes (allow-listed — pass through redact() untouched)

| Attribute | Example value | Description |
|-----------|---------------|-------------|
| `llm.provider` | `"anthropic"` | Always "anthropic" for Alice |
| `llm.model_name` | `"claude-haiku-4-5-20251001"` | Chosen model |
| `alice.task` | `"fit_judge"` | Task label from call site |
| `alice.tier` | `"cheap"` | Model tier (cheap/medium/expensive/override) |
| `alice.selection_source` | `"task_map"` | How model was chosen |
| `alice.effort` | `""` | Extended thinking effort level (empty = off) |
| `alice.rounds` | `1` | Multi-round tool-call count |
| `llm.token_count.prompt` | `3115.0` | Input tokens |
| `llm.token_count.completion` | `322.0` | Output tokens |
| `llm.token_count.thinking` | `0` | Thinking tokens (extended thinking) |
| `llm.cost.total` | `0.004725` | USD cost of the call |
| `llm.latency_s` | `5.154` | Wall-clock latency |
| `llm.stop_reason` | `"end_turn"` | API stop reason |
| `alice.run_id` | `"2026-05-31T..."` | Daily run id (from `ALICE_RUN_ID` env) |
| `session.id` | `"8971729835"` | Telegram chat_id — per-conversation bucketing |
| `alice.job_key` | `"recall-001"` | Job key for outcome linking |

### Content attributes (PII-scrubbed + truncated)

| Attribute | Content |
|-----------|---------|
| `input.value` | Prompt text (scrubbed, capped at `ALICE_TRACE_MAX_CHARS`) |
| `output.value` | Model response text (scrubbed, capped) |
| `llm.system` | System prompt (scrubbed, capped) |
| `tool.args_summary` | Tool call input dict (scrubbed, capped) |
| `tool.result_preview` | Tool result text (scrubbed, capped) |

Content attributes are dropped entirely when `ALICE_TRACE_CONTENT=0`.

---

## PII redaction

`redact()` (`telemetry.py:161`) is the single chokepoint every span attribute passes through.

**Structured attrs** (allow-list at `telemetry.py:108`) pass through untouched — they are
scalars or short identifiers we set ourselves (model names, task labels, token counts, ids).

**Content attrs** are: stringified, run through high-precision PII patterns (email, phone,
SSN-shaped, API keys, 32+ char tokens), then truncated with a `"...[+N chars]"` marker.

**Fail-closed**: unknown attribute names are treated as content and scrubbed, so a future
attribute added without updating the allow-list still gets redacted, never leaked raw.

`set_attr()` (`telemetry.py:190`) wraps `redact()` + `span.set_attribute()` and skips the
`set_attribute` call entirely when `redact()` returns `None` (content-disabled mode).

### PII redaction verified

Confirmed on live spans from the 2026-05-31 recall_benchmark run:
- `attributes.llm.system` — no email or phone pattern matched
- `attributes.input.value` — no email matched
- System prompt contains the operator persona name (name only, no contact info): passes through

---

## Outcome-feedback flywheel (prediction -> real outcome annotation)

### How it works

1. **Prediction side** (`llm.call`, `llm.py:628`): when `job_key` is set, `span_id` is extracted
   from the live OTel span (`telemetry.span_id_of(span)`) and persisted to `feedback/prediction-spans.jsonl`:
   ```json
   {"ts": "2026-05-31T18:39:46", "job_key": "recall-001-demo", "span_id": "36d3dae6224ee625", "task": "fit_judge"}
   ```

2. **Outcome side** (`annotate_outcome`, `telemetry.py:278`): when a ledger status update arrives
   (submitted / interviewing / offer / rejected), the most recent span for the job is looked up
   and annotated via `Phoenix.spans.add_span_annotation(...)`:
   ```python
   annotate_outcome("recall-001-demo", "interviewing")
   # writes: label="advanced", score=1.0, annotator_kind="HUMAN"
   ```

### Outcome label map

| Status | Label | Score |
|--------|-------|-------|
| submitted | applied | 0.5 |
| first screen scheduled | advanced | 0.75 |
| interviewing | advanced | 1.0 |
| offer / negotiating | offer | 1.0 |
| closed / not a fit / rejected | rejected | 0.0 |

### Demonstrated end-to-end

```
span_id=36d3dae6224ee625  (llm.call:fit_judge, Supabase SA, FIT)
  annotate_outcome(job_key='recall-001-demo-v2', status='interviewing')
  -> Phoenix annotation confirmed:
     name='outcome'  label='advanced'  score=1.0
     annotator_kind='HUMAN'
     metadata={'status': 'interviewing', 'job_key': 'recall-001-demo-v2'}
```

### Annotation client path

The annotation call uses `_px.Client().spans.add_span_annotation(...)`. Phoenix 16.x
has no `.annotations` attribute on the client, so the `.spans` path is required;
`telemetry.py:294`.

### What remains held

- **Outcome write-back from ledger** (`ledger.py`): the `annotate_outcome()` call site in
  status update paths. Held until the active sourcing lane clears (collision risk). The
  mechanism is fully functional; only the call site wiring to `ledger.py` is missing.
- **LIVE daemon enablement**: `ALICE_TRACING=1` in the launchd plist + bringing up `phoenix_capture.sh`
  for a capture window. Requires the operator's greenlight (paid Anthropic calls with tracing overhead).

---

## Session-id threading

`telegram_bot.py:_route_message_freeform()` accepts `session_id: str | None = None`
and threads it to `llm.call(session_id=session_id)`. Both call sites (main message handler
line 2067, button-callback handler line 2298) pass `session_id=str(chat_id)`.

Each Telegram conversation produces spans bucketed by `session.id = chat_id`,
enabling per-conversation trace views in Phoenix.

---

## Production deployment (collector on-demand model)

Per `scripts/phoenix_capture.sh`:

- The daemon runs with `ALICE_TRACING=1` permanently in its launchd plist. Spans are emitted
  every turn but drop harmlessly if no collector is up (`BatchSpanProcessor` never blocks).
- `phoenix_capture.sh start` brings up the collector for a measurement window.
- `phoenix_capture.sh stop` takes it down (resting state).
- `phoenix_capture.sh start` runs `src/alice/observability/phoenix_local_server.py`, which disables Phoenix's
  unused OTLP/gRPC bind in this local macOS runtime. Alice exports over OTLP/HTTP
  (`http://localhost:6006/v1/traces`), so the HTTP UI/collector remains the source of truth.

This means **no daemon restart is needed** to begin or end a capture window.

### Live gate

Run:

```
PYTHONPATH=scripts python3 src/alice/observability/observability_healthcheck.py
```

The gate fails nonzero unless the full chain is live: deploy guard, launchd `ALICE_TRACING=1`,
Sentry canary, Phoenix HTTP collector, one short `llm.call`, cost log, prediction-span record,
Phoenix span query, and LangSmith run lookup. It prints only presence/status metadata, never
secret values.

Latest live pass: 2026-06-03 08:08 ET. Canary span
`0000000000000000cdb3ee1bbfd0d571` was found in Phoenix as `cdb3ee1bbfd0d571` and in LangSmith
as run `00000000-0000-0000-cdb3-ee1bbfd0d571`.

### Phoenix LaunchAgent

Installed local service:

```
~/Library/LaunchAgents/com.jordan.jobsearch.phoenix.plist
```

It runs `src/alice/observability/phoenix_local_server.py serve` with `PHOENIX_HOST=127.0.0.1`,
`RunAtLoad=true`, and `KeepAlive=true`. Logs land in `state/phoenix.launchd.log` and
`state/phoenix.launchd.err`.

### Healthcheck Watchdog

Installed local service:

```
~/Library/LaunchAgents/com.jordan.jobsearch.observability-healthcheck.plist
```

It runs `PYTHONPATH=scripts python3 src/alice/observability/observability_healthcheck.py` every 6 hours
(`StartInterval=21600`) and at load. A failing run exits nonzero and sends
`alice.observability.healthcheck_failed` to Sentry when Sentry is reachable. Logs land in
`state/observability_healthcheck.launchd.log` and `state/observability_healthcheck.launchd.err`.

### Eval Dataset

Versioned cases:

```
evals/alice_scope_regression_cases.jsonl
```

Loader:

```
PYTHONPATH=scripts python3 src/alice/pipeline/alice_eval_dataset.py --load-phoenix
```

Phoenix dataset: `alice-scope-regression`. The seed cases cover the failure class from the
2026-06-03 transcript: paste chunk buffering, search-relevant technical/codebase audit scope,
observability introspection, and guardrail implementation planning.

### Security Guardrails

`src/alice/ai_guardrails.py` adds deterministic Layer 7 checks:

- prompt-injection markers in pasted/user content add a security note to the model context and
  emit `alice.security.prompt_injection_user_text` to Sentry;
- prompt-injection markers in tool results are wrapped as untrusted data before being returned
  to the model and emit `alice.security.prompt_injection_tool_result`;
- outbound Telegram text is screened before delivery; API keys/bearer-style secrets are redacted
  and `alice.security.secret_leak`/`alice.security.pii_leak` events are emitted.

### Cost And Routing Guardrails

`llm.py` now emits per-call cost anomaly warnings in addition to daily tripwires. For
`telegram_chat`, anomalous single calls are currently flagged at `in_tokens >= 80000`,
`out_tokens >= 20000`, or `cost_usd >= 1.00`.

When Anthropic fails for `telegram_chat` and `OPENROUTER_API_KEY` is configured, `llm.call`
falls back once to `openai/gpt-4o-mini` through the existing OpenRouter path. The original
failed call still lands in `feedback/time-cost-log.jsonl`; the fallback emits
`llm.routing.fallback` to Sentry.

---

## LangSmith dual-export

**Status:** Live. Spans flow to both Phoenix and LangSmith simultaneously, with feedback parity.

### Architecture

When `LANGSMITH_API_KEY` is present in `~/.config/job-search/config.env` (read via `jobcfg.load()`),
`init_tracing()` adds a second `BatchSpanProcessor(OTLPSpanExporter(...))` to the SAME
`TracerProvider` that Phoenix's `register()` returns. Zero changes to any call site.

The critical detail: `phoenix.otel`'s `TracerProvider.add_span_processor()` has a custom signature
with `replace_default_processor=True` by default, which would remove Phoenix's processor. We pass
`replace_default_processor=False` to append instead of replace, giving 2 processors on one provider:

```
BatchSpanProcessor(HTTPSpanExporter)    -> http://localhost:6006/v1/traces   (Phoenix)
BatchSpanProcessor(OTLPSpanExporter)    -> https://api.smith.langchain.com/otel/v1/traces  (LangSmith)
```

### Env surface (additions)

```
LANGSMITH_API_KEY      API key (loaded via jobcfg.load(); absent = LangSmith silently skipped)
LANGSMITH_PROJECT      LangSmith project name (default "alice")
LANGSMITH_OTEL_ONLY    "1" = LangSmith-only mode (documented, not default)
```

### Fail-open contract

A LangSmith bootstrap failure (bad key, network, package missing) is fully swallowed inside a
`try/except` AFTER the Phoenix provider is already live. Phoenix and `llm.call` are never affected.
Same discipline applies to `annotate_outcome`'s LangSmith feedback path.

### Flush discipline for short-lived scripts

`telemetry.flush_langsmith(timeout_ms=5000)` force-flushes the LangSmith `BatchSpanProcessor`
before process exit. Called at the end of `recall_benchmark.py main()` so spans drain in scripts
that exit before the background flush thread runs. Daemon never needs this.

---

## LangSmith — verified traces
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

Run: `ALICE_TRACING=1 PYTHONPATH=scripts python3 src/alice/pipeline/recall_benchmark.py --judge`

**Phoenix: 6 spans received** (regression confirmed — dual-export did NOT break Phoenix):

```
span_id=7e10097da33257ea  fit_judge.role        23:09:24   [recall-001, FIT]
span_id=f445d039ffbfc2f6  llm.call:fit_judge    23:09:24   [Supabase SA, FIT]
span_id=8b1a3cc746e08bc8  fit_judge.role        23:09:30   [recall-002, REACH]
span_id=a33f73ab709e31ba  llm.call:fit_judge    23:09:30   [Trailhead Robotics PM, REACH]
span_id=86251295ae1617e9  fit_judge.role        23:09:34   [recall-003, NOT-FIT]
span_id=42f1400b690e482e  llm.call:fit_judge    23:09:34   [Cobalt Automation CPM, NOT-FIT]
```

**LangSmith: same 6 spans received** (Alice project, id `0300c257-...`):

```
name=llm.call:fit_judge  status=success  run_id=00000000-0000-0000-42f1-400b690e482e
name=fit_judge.role      status=success  run_id=...
(+ 4 more)
```

**Attributes round-trip survived:**

```json
{
  "metadata.OTEL_SPAN_ID":    "42f1400b690e482e",
  "metadata.OTEL_TRACE_ID":   "6212e8ab160e59081c57eeca9c153410",
  "metadata.ls_model_name":   "claude-haiku-4-5-20251001",
  "usage_metadata.input_tokens":  3142,
  "usage_metadata.output_tokens": 102
}
```

**PII check on LangSmith span (`llm.call:fit_judge`, run_id=`00000000-0000-0000-42f1-...`):**
- `input.value` (815 chars): zero email/phone matches. Ends with `...[+199 chars]` truncation marker.
- `llm.system` (817 chars): same truncation. Zero PII patterns matched.
- Redaction is upstream of export — both backends receive scrubbed content identically.

---

## LangSmith — feedback parity

### run_id derivation (no REST lookup needed)

LangSmith derives its internal run UUID from the OTel span_id deterministically:

```
run_id = UUID(bytes = b'\x00'*8 + bytes.fromhex(span_id))
```

i.e. `00000000-0000-0000-{span_id[0:4]}-{span_id[4:16]}`.

Verified: Phoenix span_id `42f1400b690e482e` -> LangSmith run_id `00000000-0000-0000-42f1-400b690e482e`.

Since `prediction-spans.jsonl` already stores the OTel span_id, `annotate_outcome()` computes the
LangSmith run_id locally without any REST call, then calls:

```python
langsmith.Client(api_key=key).create_feedback(
    run_id=run_uuid, key="outcome", score=score, comment=label, value=label,
    extra={"status": status, "job_key": job_key},
)
```

### Demonstrated end-to-end

```
span_id=236c10cdca7d77d7  (llm.call:fit_judge, fresh recall run)
  record_prediction_span(job_key='demo-e2e-feedback-v1', span_id='236c10cdca7d77d7')
  annotate_outcome('demo-e2e-feedback-v1', 'interviewing')
  -> Phoenix:    add_span_annotation(span_id=..., outcome=advanced, score=1.0)  [OK]
  -> LangSmith:  create_feedback(run_id=00000000-0000-0000-236c-10cdca7d77d7,
                   key=outcome, score=1.0, comment=advanced)  [OK, confirmed via API]
```

LangSmith feedback API response:
```
key=outcome  score=1.0  comment=advanced  value=advanced
```

Both backends receive outcome annotations from a single `annotate_outcome()` call.
