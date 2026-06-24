# Phoenix Observability Audit — Alice

**Scope:** Read-only audit of Alice (the job-search agent at `~/Desktop/job-search`) mapping where Arize Phoenix (open-source, OpenTelemetry-native AI observability + evaluation) would add real value. Cadence Analytics is explicitly out of scope. No Alice code, prompts, or config were modified; the only artifact produced is this document.

**Method:** Grounded in direct reads of the real codebase (Python in `scripts/`; an early assumption of a JS `agent/` layout was wrong and corrected against the tree). Every architectural claim cites a real file:line or live DB/log state. Phoenix capabilities were verified against `arize.com/docs/phoenix` where it matters; unverified claims are flagged.

**Date:** 2026-05-30

---

## TL;DR

- Alice has **cost telemetry, not execution tracing.** Every model call's cost/latency/tokens is logged to `feedback/time-cost-log.jsonl` (`llm._log_call`, `llm.py:266`), but **not** the prompt/response content, and **not** which job or run the call belonged to. The only per-turn execution spans that exist are Sentry spans on the *single* telegram freeform path (`telegram_bot.py:1439`), and they are a no-op unless `SENTRY_DSN` is set (`obs.py:40`).
- **Critical correction for instrumentation:** Alice does **not** use the Anthropic SDK. Every call is a hand-rolled `urllib` POST to `/v1/messages` (`llm.py:454-489`). Phoenix's usual one-liner — `openinference-instrumentation-anthropic` auto-patching the SDK — **captures nothing here.** The good news: there is still exactly **one chokepoint** (`llm.call`, `llm.py:499`; HTTP at `_http_call_once`, `llm.py:454`), so ~15 lines of *manual* span wrapping at that one function instruments all ~20 call sites at once. Proposed diff in §3.
- **Part B (the real prize):** Alice computes the ground truth that makes a career agent's observability valuable — fit-score, status transitions with dates, outreach response class, interview fit-signal — but **never joins the prediction to the outcome.** The DB table built for it (`opportunities`, with a 10-value `stage` enum and `date_applied`/`date_last_touch`) is **frozen mid-April: 16 rows, all stuck at `scored`/`parked`, those date columns NULL for all 16, and no code path advances them.** The live outcome record migrated to a Google Sheet whose `score` (col F) and `status` (col G) sit in the same row but are **never read together by any code.** Closing that loop — predictions graded by reality — is the highest-leverage work, and Phoenix span annotations are built for it.

---

## Phase 1 — Alice Inventory (ground truth)

### 1. Runtime & invocation model

- **Runtime:** Python, pinned to CPython 3.14 at an absolute interpreter path (`run_daily.py:19`, `run_daily.sh:7`). Deps: `python-telegram-bot` (`telegram_bot.py:45`), Google Sheets via `gspread`/service account (`ledger.py:92-111`), optional `sentry_sdk` (`obs.py:26`). Anthropic via raw HTTPS, **no SDK**.
- **Three launchd entry points** (in `~/Library/LaunchAgents/`, **outside the repo / not version-controlled**):
  | Plist | Program | Schedule | Type |
  |---|---|---|---|
  | `com.jordan.jobsearch.plist` | `python3 scripts/run_daily.py` | daily 11:45 | **one-shot batch** |
  | `com.jordan.jobsearch.telegram.plist` | `python3 scripts/telegram_bot.py` | `KeepAlive` | **long-running interactive daemon** |
  | `com.jordan.jobsearch.weekly.plist` | `/bin/bash scripts/run_weekly.sh` | Fri 16:00 | **one-shot batch** |
- **Daily run = one-shot, 14 sequential steps** via `run_step()` spawning each as an isolated `subprocess.run(..., timeout=600)` (`run_daily.py:29-48`, sequence `51-119`): snapshot → (Sat: discover) → imap_reply → focus auto-drop → triage_observations → prep_materials → interview_prep → debrief → draft_outreach → negotiation_prep → morning_reminder → confirm_and_execute → daily_delta (sources + sends digest) → readiness_check → snapshot. Each subprocess is independent; **nothing identifies "this is run N"** to the 14 children.
- **Interactive = multi-turn** telegram bot (`telegram_bot.py`, `app.run_polling`). The only conversational surface; history reconstructed by reading the last N lines of `telegram-history.jsonl` (`telegram_bot.py:157`), the operator-turns-only after the contamination fix.
- **Operational notes worth flagging (out of Phoenix scope, found in passing):** the weekly scorecard is currently **broken at launchd** — `/bin/bash` hits a macOS TCC "Operation not permitted" reading `~/Desktop` (`daily/weekly-stderr.log`); only the daily runner was ported to `python3` (which has Full Disk Access). The telegram daemon is also running **5 commits stale** right now (`deploy_guard` logs in `daily/telegram-bot.err`), and `telegram_chat` blew its $5 tripwire at ~$6.79/day (see §9).

### 2. LLM call sites

- **Provider/SDK:** **none — raw `urllib`.** `_API_URL = ".../v1/messages"` (`llm.py:30`); the only function touching the wire is `_http_call_once(payload, key, ...)` (`llm.py:454-489`), `urlopen` at `llm.py:473`, retry on 429/503/529/5xx. There is **no `anthropic` import, no client object** anywhere (grep `import anthropic` → 0). `opentelemetry-*` packages ARE installed, but only transitively via `sentry-sdk`; nothing in `scripts/` imports them.
- **Single public entry point:** `call(task, prompt, system=None, max_tokens=..., tools=None, tool_executor=None, effort=None, tier=None, ...)` (`llm.py:499-701`). Tool use is **folded into the same function**, not a separate `call_with_tools`: when `stop_reason=="tool_use"` it dispatches every `tool_use` block (parallel tool calls all run), appends `tool_result`, loops, capped at 8 roundtrips (`llm.py:576-659`, `_MAX_TOOL_ROUNDTRIPS=36? → 8`, `llm.py:36`). The names `call_llm`/`call_with_tools`/`_get_client`/`_resolve_model` do **not** exist.
- **Model resolution:** `select_call_config(task, ...)` (`llm.py:172`) maps task→tier→model: cheap `claude-haiku-4-5-20251001`, medium `claude-sonnet-4-6`, expensive `claude-opus-4-8` (`llm.py:77-88`, `TIER_FOR_TASK` `116-157`); `resume_draft`/`cover_letter_draft` pinned to `claude-opus-4-7` (`llm.py:168-169`). Unknown task → silently cheap (`source:"default"`).
- **Streaming: none** (full body read in one shot, `llm.py:473`).
- **~20 production call sites**, all routing through `llm.call(...)`: e.g. `telegram_bot.py:717,1450`, `prep_materials.py:157,190,217,246,307,429`, `prep_pipeline.py:306,951,972,991,1009,1027`, `interview_prep.py:113`, `triage_observations.py:144`, `draft_outreach.py:166`, `scorecard.py:242`, `negotiation_prep.py:74`, `morning_reminder.py:102`, `debrief.py:156`, `imap_reply.py:908`, `critic.py:131`, `confirm_and_execute.py:233` (this one passes **no system prompt**). Because all funnel through `call()`, there is exactly **one seam** to instrument.

### 3. Tool surface

- **24 tools**, registered via a `@register_tool` decorator into one module-global `TOOLS_REGISTRY` (`tools.py:52-87`); `tool_specs()` projects the Messages-API schema list; `dispatch(name, input)` is the executor `llm.call` invokes per `tool_use` block (`tools.py:90-120`). **Structural safety invariant:** a `mutating=True` tool with no guard raises at **import time** (`tools.py:65-70`).
  - **Read-only (9):** `read_sheet`, `read_focus_state`, `read_pending_state`, `read_file`, `list_dir`, `list_knowledge_files`, `read_knowledge_file`, `read_alice_brief`, `describe_capabilities`.
  - **Mutating, guard-wired (15):** `write_file`, `set_focus`, `add_focus`, `drop_focus`, `append_observation`, `mark_role_status`, `enqueue_prep`, `dequeue_prep`, `generate_application_package`, `flag_experience_candidate`, `list_pending_experience_candidates`, `flag_correction_candidate`, `list_pending_correction_candidates`, `ask_confirmation`.
- Note: `_sheet_write_guard` (`tools.py:641`) is a deliberate **no-op marker** — sheet writes are protected only by the `authorized=True, source=...` audit trail (`ledger`), not a path/value gate. The strongest consequence tools are the write-side ones, which is exactly what tool-selection/parameter-extraction evals would watch.

### 4. Prompt surface

- **Prompts live in markdown + inline strings, not a registry:**
  - `load_alice_brief()` (`llm.py:704-752`) assembles the system prompt on **every call**: `ALICE_SOUL.md` (~17 KB) + `Alice.md` (~53 KB) + a **freshly `rglob`-walked knowledge-base index** (`_build_knowledge_index`, `llm.py:755`, uncached).
  - User prompts are hand-built f-strings per caller; the richest is `telegram_bot.py` (context block `247+`, anchors + history assembled `1393-1408`). The big behavioral directives are **literal inline Python constants** (`telegram_bot.py:1343-1391`).
  - `self/capabilities.md` is **not** in the standing prompt — it's only reachable via the `describe_capabilities`/`read_alice_brief` tools.
- **Versioning:** none beyond git — no prompt id, tag, or per-call prompt hash. The only snapshot is `feedback/full-prompt-last.txt` (the *last* turn's complete prompt, overwritten each turn — the `feedback_full_prompt_capture_pattern` debug aid) + `feedback/debug-context-last.txt`.

### 5. State & persistence

- **`pipeline.db` (SQLite, gitignored) — built for the funnel, largely inert:**
  | Table | Rows | Holds | Live? |
  |---|---|---|---|
  | `opportunities` | **16** | the scored-job record: `fit_score`, `fit_reasoning`, `stage` (enum incl. `applied/screened/interview/offer/closed_won/closed_lost`), `date_applied`, `date_last_touch` | **lifecycle dead** — all 16 at `scored`/`parked`; `date_applied`/`date_last_touch` NULL for all 16; last write 2026-04-16. Only `source_listings.py` writes it, and only `role_title/fit_score/role_variants` — **no code writes `stage`/`date_applied`/`date_last_touch`.** |
  | `conversations` | 0 | designed contact log w/ FK to opportunities | never written (dead) |
  | `daily_stats` | 0 | funnel counters | never written (dead) |
  | `sourcing_log` | 11 | per-run sourcing decisions; has `run_id` | live but small; last 2026-04-17 |
  | `seen_jobs` | 6525 | dedup cache | live (also a *separate* `state/seen_jobs.json`) |
- **Google Sheet ledger (`ledger.py`) = the real system of record.** 13 cols incl. `score` (F), `status` (G, the 10-value live outcome dropdown), `job_key` (J), `status_changed_date` (L, **overwritten** each transition). Terminal statuses are write-gated (`TERMINAL_GATED`, `ledger.py:43`; `_check_authorization`).
- **File/JSONL state** under `feedback/` (its own git repo) and `state/`: `time-cost-log.jsonl`, `verify-log.jsonl`, `sheet-write-log.jsonl` (the only full status-transition audit), `decision-forks.jsonl`, `intent-write-log.jsonl`, `focus.json`, per-app `.metadata.json`/`.pipeline-metadata.json`, `self/correction-log.csv`.
- **Recoverability:** cost/gate/transition logs recoverable from JSONL; **prompts/responses of past runs are not** (only the single most-recent in `full-prompt-last.txt`). The Sheet has no transition *history* (col L overwrite); the append-only `sheet-write-log.jsonl` is the only transition trail, and it is **not** linked to `score`.

### 6. Session / multi-turn model

- **The only correlation id in the whole system is `sourcing_log.run_id`** (`init_db.py:76`, set in `daily_delta.py:844` as `YYYYMMDD-HHMMSS`), and it groups **sourcing only.**
- Everywhere else: **none.** `grep -E 'session_id|conversation_id|trace_id' scripts/` → 0 real hits. No id ties the 14 daily steps into one run, the multiple LLM calls of one chat turn together, or a telegram conversation's turns. The cost record (`{ts, task, model, in_tokens, out_tokens, cost_usd, latency_s, ok, ...}`) carries **no job/run/conversation id.** The `_log_call` record and its `turn_enrichment` partner are joined by `task`+timestamp-within-1s heuristic (`llm.py:303-305`), explicitly not by id. Cross-cutting questions ("everything Alice did for Northwind Systems", "last Tuesday's whole run") require manual timestamp correlation across files.

### 7. Outcome capture (the crux)

- **Status transitions WITH dates are captured — in the Sheet only:** `materials pending` auto-set after package build (`prep_materials.py:354`), `first screen scheduled` on directive (`imap_reply.py:649`), `submitted/interviewing/offer/closed` only via authorized directives. Outreach response class via `response from <name>: positive|...` → `outreach-responses.jsonl` (writer `imap_reply.py:801`; **file does not yet exist** — never exercised). Interview fit-signal via debrief prompt 3 → `applications/<slug>/debrief-r<N>.md` (matched to role by a time-window heuristic, **no stable key**).
- **The fatal gap — prediction is never linked to outcome:**
  1. `opportunities.fit_score` co-locates with `stage`, but `stage` never advances (0/16 applied), so there's no outcome to correlate in the DB.
  2. Sheet `score` (F) sits next to `status` (G) in the same row — **the one place a join is physically possible — but no code reads them together.** `scorecard.py`'s funnel (`:30-61`) and outreach (`:64-78`) metrics never read `score`; the "CALIBRATION" line (`scorecard.py:228`) is a **prompt heading with no underlying score-vs-outcome data.**
  3. Outreach/debrief outcome records carry a `company` string but no `job_key`/`opportunity_id` — can't be joined except by fuzzy name match, which nothing attempts.
  4. `.metadata.json` is **build-time only** (`draft_costs`, `models_used`, `answers_received:[]`, `final_generated:false`) — it records what Alice *produced*, never the job's *outcome*.
- **Direct answer to "is Alice's fit score ever connected to what actually happened": No.**

### 8. Existing observability

- **Cost/usage:** `_log_call` (`llm.py:266`) → `feedback/time-cost-log.jsonl`, shape `{ts, task, model, in_tokens, out_tokens, cost_usd, latency_s, ok, error?}` + extras `{tier, selection_source, effort, thinking_tokens, rounds, stop_reason, tool_calls[]}`; cost via `_calc_cost` + `PRICING` (`llm.py:40,328`). A second `turn_enrichment` record carries `grounding_flags` (`log_turn_enrichment`, `llm.py:293`).
- **Activity/gates:** `activity_log.py` → `daily/activity-*.jsonl` (`{ts, step, status, count, summary, cost, details}`); `verify-log.jsonl` (post-action verification, `{ts, kind, ok, verified, claim, detail}` — 106 records `ok:false`); grounding detectors per chat turn (`telegram_bot.py:1512-1627`).
- **Sentry (dormant):** `obs.py` is an all-no-op shim unless `SENTRY_DSN` set. Per-turn spans (`start_turn_span`, op `alice.turn`) exist **only** on the telegram freeform path. This telegram-only asymmetry is the strongest argument for instrumenting universally at `llm.call`.
- **No PII redaction exists** (no `_redact` anywhere). The cost log stores **full tool results verbatim — including entire sheet contents** (company names, URLs, statuses); grounding events ship raw `user_text[:200]`/`raw[:300]` to Sentry. If Phoenix spans carry message bodies, the same exposure applies and must be handled at the wrap point (see §3 Part A caveat).
- **Debug workflow today:** read `full-prompt-last.txt` + `debug-context-last.txt` + the `.err`/`.log` files — works for the *most recent* failure only, shows the prompt but not a step-by-step execution timeline.

### 9. Context management

- **No token budgeting.** Only guardrails are dollar caps (post-hoc accounting, never gates) and ad-hoc char-slicing for Telegram size limits. There is no `_truncate` helper and no `max_input_tokens`.
- **Measured bloat:** a single `telegram_chat` turn logged **`in_tokens: 121560`** (`time-cost-log.jsonl`, 2026-05-30T11:53) — `read_sheet` returns the whole sheet as a tool result, which then **rides along in `payload["messages"]` on every subsequent roundtrip** (`llm.py:657-658`), uncapped. Compounded by `load_alice_brief` inlining ~70 KB of markdown on every call. This is *the* cause of the `telegram_chat` cost overrun ($6.79/day vs $5 tripwire) — and it is currently invisible per-component. Tracing surfaces it immediately.

---

## Phase 2 — Phoenix capability reference (verification status)

| Capability | Package | Verified? |
|---|---|---|
| Manual OpenInference LLM spans (model, messages, tokens, tool calls) | `arize-phoenix-otel` + OTEL SDK | ✅ Standard OTEL; **required path here** since there's no SDK to auto-instrument. |
| Auto-instrument the Anthropic SDK | `openinference-instrumentation-anthropic` | ✅ exists — **but N/A to Alice** (raw `urllib`, no SDK). |
| Sessions (group spans by session id) | `SpanAttributes.SESSION_ID` + `using_session(...)` ctx mgr | ✅ Verified against docs. |
| Datasets + Experiments from Python | `arize-phoenix-client` | ✅ Verified: `client.datasets.create_dataset(name, dataframe, input_keys, output_keys)`; `client.experiments.run_experiment(dataset, task, evaluators)`; `evaluate_experiment`. |
| LLM-judge / code / agent evals (tool selection, param extraction, trajectory) | `arize-phoenix-evals` | ✅ (eval taxonomy as documented). |
| Human annotations on existing spans by `span_id`, after the fact | `arize-phoenix-client` / REST `POST /v1/span_annotations` | ✅ REST endpoint + `annotator_kind="HUMAN"`, `label/score/explanation` verified. ⚠️ exact client method name (`client.annotations.add_span_annotation`) to confirm against installed version. |
| Prompt management / versioning / prompts-in-code | Phoenix prompts | ◻️ product knowledge; not re-verified. |
| Prompt playground + span replay | Phoenix UI | ◻️ product knowledge. |
| Cost & token tracking per span | built into tracing | ✅. |
| CLI / agent skills for Claude Code | `arize-skills`, repo `.agents/skills/` | ◻️ exists; verify skill list before relying. |
| Deployment: local / Docker / Phoenix Cloud (free); self-host keeps data local | `arize-phoenix` | ✅. |

**Right-sizing:** Alice is single-user. RBAC, multi-tenant, and scale tooling are **overkill** and excluded below.

---

## Phase 3 — Part A: Immediate opportunities (Alice as-is)

### The single smallest first step

**Wrap the one chokepoint with a manual span.** Because every call routes through `llm.call` (`llm.py:499`), one `with tracer.start_as_current_span(...)` around its body produces a complete end-to-end trace of an entire Alice run — prompt, system, model, per-roundtrip tool calls, tokens, latency, cost — with **zero changes to the ~20 call sites.** (The auto-instrument one-liner does **not** apply: no SDK to patch.)

What it lets you see that you can't today: the full prompt+response of *every* step (not just the last), the tool loop inside `call()`, and — critically — the per-call token breakdown that exposes the 121 K-token sheet-in-context blowup (§9).

#### Proposed instrumentation diff (PROPOSED ONLY — not applied)

```diff
*** /dev/null
--- a/scripts/telemetry.py
@@
+"""Phoenix/OpenTelemetry tracing bootstrap for Alice. Import-once, side-effecting.
+No-op if ALICE_TRACING=0 or Phoenix isn't installed, so runs without a collector
+are unaffected. Alice calls the REST API directly (no anthropic SDK), so we emit
+MANUAL OpenInference spans from llm.call rather than relying on auto-instrumentation."""
+import os
+_STARTED = False
+
+def init_tracing(project_name: str = "alice") -> None:
+    global _STARTED
+    if _STARTED or os.environ.get("ALICE_TRACING", "1") == "0":
+        return
+    try:
+        from phoenix.otel import register
+        # register() sets the global OTEL tracer provider + OTLP exporter.
+        # auto_instrument is irrelevant here (no SDK); our manual spans still flow.
+        register(project_name=project_name)
+        _STARTED = True
+    except Exception as e:   # never let telemetry break a run
+        print(f"[telemetry] tracing disabled: {e}")
```

```diff
--- a/scripts/llm.py
+++ b/scripts/llm.py
@@ def call(task, prompt, system=None, max_tokens=1024, model=None, temperature=1.0,
-    cfg = load()
-    key = cfg.get("ANTHROPIC_API_KEY")
+    from opentelemetry import trace
+    _span_cm = trace.get_tracer("alice.llm").start_as_current_span(f"llm.{task}")
+    _span = _span_cm.__enter__()
+    cfg = load()
+    key = cfg.get("ANTHROPIC_API_KEY")
@@ (after chosen_model / chosen_effort are resolved)
+    # OpenInference LLM-span semantic attributes (manual; no SDK to auto-capture)
+    _span.set_attribute("openinference.span.kind", "LLM")
+    _span.set_attribute("llm.model_name", chosen_model)
+    _span.set_attribute("llm.invocation_parameters", json.dumps({"max_tokens": max_tokens, "temperature": temperature, "effort": chosen_effort}))
+    _span.set_attribute("input.value", prompt[:20000])
+    if system:
+        _span.set_attribute("llm.system", system[:20000])
@@ (in the terminal branch, right before `return {...}`)
+            _span.set_attribute("llm.token_count.prompt", total_in)
+            _span.set_attribute("llm.token_count.completion", total_out)
+            _span.set_attribute("output.value", final_text[:20000])
+            _span.set_attribute("llm.cost.total", total_cost)
+            _span_cm.__exit__(None, None, None)
             return { ... }
```
*(The tool roundtrips already loop inside `call()`; record each `tool_calls_log` entry as a span event or child span for tool-selection visibility. Clean form: a `try/finally` around the `while` loop so the span closes on the exception path at `llm.py:693` too.)*

```diff
--- a/scripts/run_daily.py        # and the same two lines in telegram_bot.py main()
+++ b/scripts/run_daily.py
@@ def main():
+    from telemetry import init_tracing
+    init_tracing()
```

Dependencies (proposed, install separately): `pip install arize-phoenix-otel` (+ `arize-phoenix` for a local instance, or a free Phoenix Cloud endpoint). `opentelemetry` is already present transitively. The `try/except` + `ALICE_TRACING=0` keep this safe: no collector → Alice behaves exactly as today.

**PII caveat (must address before turning on):** `input.value`/`output.value`/tool results contain sheet contents and the operator-PII. Either run Phoenix **self-hosted/local only** (data never leaves the machine) or add a redaction hook at the span-set boundary. There is no existing `_redact` to lean on.

*This patch is proposed for copy-paste readiness; per the read-only lane fence it has not been applied. Verify the exact `telegram_bot.py` startup anchor and the `return`/exception sites in `call()` before applying.*

**Alternative worth considering:** migrate `_http_call_once` to the `anthropic` SDK. Larger change, but then `openinference-instrumentation-anthropic` auto-captures everything and you delete the manual span code. Not required; flagged for the roadmap.

### Remaining as-is opportunities (ranked by value-to-effort)

1. **Per-step token/cost breakdown (free with step 1).** Immediately localizes the 121 K-token `telegram_chat` blowup to `read_sheet` results resent every roundtrip (§9) — the direct cause of the $5→$6.79 tripwire. **Effort: zero.**
2. **Tool selection / parameter visibility (free).** Records the 24-tool dispatch loop — e.g. did `mark_role_status` get the right row/status. **Effort: zero.**
3. **Historical, any-run prompt/replay debug.** Replaces "read `full-prompt-last.txt` for the *last* failure" with "open the trace for *any* past run." Generalizes the `feedback_full_prompt_capture_pattern` to all calls and all history. **Effort: zero beyond step 1.**
4. **Manual spans around GROUND→WRITE→VERIFY→ASSEMBLE** (`prep_pipeline`, mirrored in `verify-log.jsonl`) — a readable span tree with gate verdicts as attributes. **Effort: low.**

---

## Phase 4 — Part B: Latent / missed opportunities (need new infra)

Alice's brief already *describes* a measurement flywheel; the code computes pieces ephemerally and **discards them**, and the DB schema built for it is unwired. Ordered by dependency.

### B0. Session/run/job ids threaded through runs *(enabler for everything below)*

- **Value:** "show me last Tuesday's whole run"; "everything Alice did for the Northwind Systems role across days"; join an LLM call to the sheet write and verification it produced.
- **Gap:** only `sourcing_log.run_id` exists, sourcing-scoped (§6). No id on `opportunities`, no id in any feedback JSONL, none in the LLM path.
- **Build:** generate a `run_id` at the top of `run_daily.py:main()` and a `session_id` per telegram conversation (the telegram chat id already exists — thread it into `llm.call`); set as `SpanAttributes.SESSION_ID` / propagate via `using_session(...)`; add a stable `job_key` (the sheet's existing col J) as a span attribute wherever a step works a specific role; stamp the same ids into the JSONL records for cross-reference.
- **Effort:** low–medium. **Depends on:** Part A. **Unlocks:** Sessions; per-run/per-role grouping; prerequisite for B3.

### B1. Labeled fit-eval dataset → Experiments

- **Value:** the fit model (`score_job.py` / `source_listings.py`) is tuned by feel. A golden set of jobs with the operator's known-correct labels lets you run **experiments**: change the scoring prompt/weights, re-run against the golden set, measure calibration *before* shipping — instead of waiting for the Friday scorecard.
- **Gap:** no persisted labeled fit dataset. The ground truth (the operator's `good fit`/`not a fit`) lives only as Sheet col G and is consumed ephemerally as company-level *suppression counters* (`ledger.load_statuses`), never as `(job, score, label)` triples. `opportunities.fit_score` is frozen.
- **Build:** assemble `golden_fits` = `{jd_text, jd_features, operator_label, alice_score, archetype, comp_band}` from the Sheet's historical col F + col G (the operator has labeled for weeks); `client.datasets.create_dataset(...)`. Wire `score_job` as the experiment task; the fit-call-vs-label comparison that *should* exist in `scorecard.py` becomes the evaluator. Phoenix benchmarks scoring-prompt versions side by side.
- **Effort:** medium. **Depends on:** Part A + actually persisting score+label together. **Unlocks:** Experiments, calibration regression.

### B2. Externalized prompt registry → Prompt management & A/B

- **Value:** the brief plans A/B tests of cover-letter openings / resume framings. You can't cleanly A/B prompts that live as 70 KB of markdown + inline f-strings + inline directive constants (`telegram_bot.py:1343-1391`).
- **Gap:** prompts in `ALICE_SOUL.md`/`Alice.md`/inline; no id/version/tag (§4).
- **Build:** lift task prompts (scoring, cover letter, outreach, triage) into Phoenix-managed prompts (or a local prompt module with ids+versions); record prompt id/version as a span attribute. "cover-letter-opening = cadence-pitch vs jd-hook" becomes a filterable, experimentable variable.
- **Effort:** medium (touches many call sites). **Depends on:** Part A. **Unlocks:** prompt management, playground, span replay, clean A/B.

### B3. Outcome-feedback annotations — **highest value for a career agent**

- **Value:** the flywheel. Alice scores a job and drafts the application; today the signal **ends there.** If the real outcome (applied → response → first screen → offer/rejection; outreach → positive/no-response) is **annotated back onto the span that produced the prediction**, the operator's actual job search becomes ground-truth eval data — fit score graded by what actually happened. This is the predictions-graded-by-reality loop.
- **Gap (two parts):** (1) outcomes aren't captured in linkable form — `opportunities` lifecycle is dead, outcome records carry only a `company` string, no stable key; (2) even when known, the prediction wasn't traced with a stable id to attach to (needs B0).
- **Build:**
  1. On every scoring/drafting call, persist `span_id` keyed by `job_key` (finally a real use for the inert `opportunities` table, or a small `prediction_spans` table).
  2. When `ledger.update_status` fires (status change) or `imap_reply` parses a response tag, write `POST /v1/span_annotations` (or `client.annotations.add_span_annotation`) onto that job's prediction span: `name="outcome", label=<submitted|first_screen|offer|rejected|no_response>, score=<1.0 advanced / 0.0 rejected>, annotator_kind="HUMAN"`.
- **Effort:** medium (wiring is small; the discipline of stamping `span_id` per job is the work). **Depends on:** B0 + Part A. **Unlocks:** the whole flywheel — labeled data accrues from real activity, feeding B1 and B4.
- **Honesty caveat:** sample size. n is tiny for one job search (16 scored, 0 advanced in DB, no interviews yet). Per `feedback_reference_class_discipline` and Alice's own phased-analysis doctrine, treat annotated data as a **corpus for debugging and qualitative pattern-spotting first** ("the 3 mis-scored roles were all AI-native-but-hub-bound"), statistical tuning much later.

### B4. Online evals + regression suite

- **Value:** continuous LLM-judge evals on outputs (voice rules — no em-dash/no-consulting-speak; every fit claim grounded in a JD+the operator-evidence pair) running each run; plus a regression set of known failures so a prompt change can't silently reintroduce a past mistake.
- **Gap:** needs a persistent collector (B0/Part A) + a defined rubric; curated failure cases don't exist (they fall out of B3).
- **Build:** port the rules already encoded in `critic.py`/`verify.py`/`grounding.py` into `arize-phoenix-evals` evaluators; attach as dataset evaluators; promote annotated failures (B3) into the regression dataset.
- **Effort:** medium–high. **Depends on:** B0, B1, B3. **Unlocks:** online evals, regression suite, dataset evaluators.

### The flywheel, stated plainly

Predictions (fit scores, drafts) → traced with a stable per-job span id (B0) → real outcomes annotated back onto those spans (B3) → accumulated into a labeled dataset (B1) → experiments + regression evals (B4) → a measurably better Alice. **The smallest investment that starts the loop is B0 + B3 on top of Part A:** trace with ids, then write outcomes back. Everything else compounds on that.

---

## Phase 5 — Roadmap as a checkpoint chain (capture-now / consume-later)

This roadmap is a **chain of checkpoints**, not a single deliverable. Each checkpoint (CP) is a clean *pickup seam*: it ends in a known-good, committed state with this doc and `PART_A_IMPLEMENTATION_NOTE.md` updated, so whoever goes next — the same agent with warm context, a fresh session, or a human — starts from fact, not from re-deriving scope. Stop-and-flag at every transition (the project's checkpoint discipline).

**The line that governs the whole chain is not "tracing vs. more." It is "build capture vs. consume data you don't have."** Almost everything is worth building now; the *only* hard hold is optimizing against outcome data that does not exist yet (n=16 scored, 0 advanced, 0 interviews — experiments/tuning there would fit noise and call it signal). This supersedes any earlier, more cautious "(a)-only, pencil (b)" framing: judge/rubric evals and the outcome-*capture* flywheel are near-term; only experiment/tuning *consumption* is deferred.

**Two kinds of gate move the chain forward — distinct from a CP being "done":**
- a **DoD** closes a checkpoint (work verified + committed);
- a **gate** makes the *next* checkpoint crossable. Gates are either **the operator-greenlight** (anything that spends Anthropic budget or runs the live job-search system) or **data** (B1/B4 need accumulated outcomes). A finished DoD never auto-advances past a gate.

### The chain

**CP1 — Part A: execution tracing** *(in flight; see `PART_A_IMPLEMENTATION_NOTE.md`)*
- **Scope:** manual OpenInference span at the single chokepoint `llm.call` (no SDK to auto-instrument), redaction from line one, local self-hosted Phoenix only.
- **Entry:** readiness gate green (`docs/diagnostics/READINESS_AUDIT_2026-05-30.md`). ✅
- **DoD:** patch verified against the live tree; fail-safes confirmed (OFF ⇒ byte-identical; ON-no-collector ⇒ graceful no-op); results written into the implementation note; **uncommitted pending the operator.**
- **Exit gate → CP2:** (1) the operator OKs the Part A commit; (2) enabling tracing + capturing the live 121K baseline is **the operator-greenlight** (paid Anthropic calls on the live daemon; requires `ALICE_TRACING=1` + local `phoenix serve` + launchd restart discipline). The DoD deliberately stops *before* this line.

**CP2 — read_sheet cost fix** *(spec: `~/Downloads/files (6)/alice-read_sheet-fix-spec.md`)*
- **Why before evals:** a judge-eval runs an LLM on every turn; turning that on *before* fixing the leak multiplies a turn already over its $5/day tripwire (the 121K-token `read_sheet` payload re-riding every roundtrip). Cost ordering is a real dependency, not a preference.
- **Entry:** CP1 committed **and** baseline trace captured (so the fix has a before-number).
- **DoD:** projection/slim fix proposed → reviewed → applied; after-trace shows the prompt-token drop recorded next to the 121K baseline; chat answers unchanged in substance on a couple of representative turns.
- **Exit gate → CP3:** the operator OKs; tracing confirmed live and cheap.

**CP3 — judge-evals + capture flywheel (B0 ids + B3 outcome annotations)** — *the bulk of the "more," and all of it buildable now*
- **Judge/rubric evals (the live form of Move 3):** an LLM-judge "did Alice answer the direct question" eval is the automated version of the Move-3 verification we did by hand — and it needs **traces + a rubric, not a labeled dataset.** The voice rules (no em-dash / no consulting-speak) and grounding checks already exist as code in `critic.py` / `verify.py`; porting them to Phoenix evaluators is translation, not new infra. This is where Part A converges with the Move 3 lane.
- **B0 — thread run/session/job ids:** reuse the sheet's `job_key` and the telegram chat id; set OTel `SESSION_ID` / propagate. Low-risk, additive. Prereq for attaching evals/outcomes to the right span.
- **B3 — outcome-feedback annotations (build the perishable pipe now):** persist each prediction's `span_id` keyed by job, then on `ledger.update_status` / `imap_reply` response-parse, write the real outcome back as a Phoenix span annotation. **The reason to do this now despite no payoff yet: the data is perishable** — every week the prediction→outcome link isn't persisted is labeled training data lost forever. Deferring this is leakage, not prudence.
- **Entry:** CP2 done (tracing live + cheap); ids threaded (B0 first within this CP).
- **DoD (per piece):** evals visible in Phoenix on real turns; ids present on spans; outcomes annotating the originating prediction spans.
- **Exit gate → CP4:** **data**, not calendar — see below.

**CP4 — experiments (B1) + tuning/online-eval-at-scale (B4)** *(the one hard hold)*
- **Entry gate is data:** picked up only once B3 has accumulated real outcomes (advanced/rejected/responded) in enough volume to mean something. Until then this is *correctly not started* — not "blocked," just premature. Also includes B2 (externalized prompts) for clean A/B once there's something to A/B.
- **DoD:** golden fit-dataset built from accumulated labels; scoring-prompt versions benchmarked as experiments; regression suite from confirmed failures.

### Gate table

| CP | Buildable now? | Entry depends on | Closed by (DoD) | Crossing gate to next |
|---|---|---|---|---|
| CP1 tracing | yes (applied) | readiness green ✅ | verified + fail-safes + uncommitted | the operator OKs commit; live baseline = the operator-greenlight (paid/live) |
| CP2 read_sheet fix | yes | CP1 committed + baseline captured | after-trace shows token drop | the operator OK |
| CP3 judge-evals + B0 + B3 | **yes** (no labeled data needed) | CP2 (live + cheap) | evals live, ids on spans, outcomes annotated | **data** accrues |
| CP4 experiments + B1/B4/B2 | **no — deferred** | accumulated outcomes | dataset + experiments + regression | n/a (terminal) |

### Single highest-ROI next step

Land **CP1** (it pays for itself on the next debug *and* localizes the cost overrun already breaching the tripwire), then **CP2** (kill the read_sheet leak) *before* turning on any continuous eval. Then **CP3** is the real prize: the live Move-3 eval plus the perishable-data capture pipe. Hold **CP4** until reality has filled the pipe. The only thing this roadmap refuses to do early is optimize against data that doesn't exist yet.

---

## Appendix — audit integrity notes

- An early assumption of a JS `agent/orchestrator.js` was **wrong and corrected against the real tree** (Alice is Python in `scripts/`). The first draft of this report inherited two further memory-shaped errors that grounded reads then corrected: (a) it assumed the Anthropic **SDK** with an auto-instrument one-liner — reality is raw `urllib`, so instrumentation must be manual; (b) it mis-described `pipeline.db` tables — the real schema is `opportunities`/`conversations`/`daily_stats`/`sourcing_log`/`seen_jobs`. All findings above are from direct reads.
- Critical absences proven, not inferred: no `anthropic` import (0 hits); session/trace ids = 0 (only `sourcing_log.run_id` exists); `opportunities.stage`/`date_applied` writers = 0; `opportunities` rows all `scored`/`parked` with NULL dates (live query); no `_redact` anywhere.
- Phoenix claims marked ◻️/⚠️ in Phase 2 should be re-verified against the installed Phoenix version before implementation; ✅ claims were checked against `arize.com/docs/phoenix` during this audit.
- Out-of-scope operational issues surfaced in passing (not Phoenix, but worth the operator's attention): weekly scorecard broken at launchd (bash TCC denial); daemon 5 commits stale; `telegram_chat` over its $5/day tripwire; `readiness_check` self-reports NOT READY (26 silent failures, 1 unauthorized-write attempt in 30 days); launchd plists live outside version control.
