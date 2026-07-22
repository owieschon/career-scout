# Architecture

<!-- sourcebound:purpose -->
Use this page to locate career-scout's current execution path, deterministic safety boundaries, configuration, and external effects.
<!-- sourcebound:end purpose -->

## Runtime path

The scheduled operator workflow starts at [`scripts/run_daily.sh`](../scripts/run_daily.sh).
It runs state, reply-processing, preparation, reminder, sourcing, readiness, and
snapshot steps in a fixed sequence. The final sourcing step invokes
`python3 -m alice.pipeline.daily_delta --ledger`.

That shell script is an operator command, not a local demonstration. It can call
external APIs, mutate the configured ledger and local state, and send email or
Telegram notifications. The hermetic `pytest` suite is the safe local proof
path.

## Screening funnel

1. [`daily_delta.py`](../src/alice/pipeline/daily_delta.py) collects and
   de-duplicates listings, then applies role, domain, remote, scoring, and
   travel gates. It attempts to record a reason for each deterministic drop;
   a failed state write increments `skip_reason_write_failures` and prints an error.
2. Main-funnel survivors with a JD body enter
   [`fit_judge.py`](../src/alice/pipeline/fit_judge.py), which calls
   `location_travel_gate(...)` before any model request. A `kill` returns
   immediately; only listings that clear this second gate reach the model.
3. The fit judge reads [`config/fit_model.toml`](../config/fit_model.toml) and
   emits `FIT`, `REACH`, or `NOT-FIT`. Parse and request failures resolve to
   `NOT-FIT` with explicit error constraints.
4. A separate recall lane copies at most `ALICE_DROPPED_SAMPLE_MAX`
   deterministic drops into the same guarded fit-judge path. The default cap is
   `20`, and the hard maximum is `100`; `0` disables only the recall lane. A
   non-integer, negative, or above-limit value stops before source fetches.
   Each run records the configured value, effective value, and configuration source. A
   rescue-lane `FIT` or `REACH` is printed for operator review and counted in run statistics; it is
   not inserted into the main shortlist or ledger. Attempted, completed, and
   failed judge counts remain distinct, with failure classes retained. Body-less sample entries
   remain explicitly unjudged. The fit judge and recall lane are enabled by
   default; `ALICE_FIT_JUDGE=0` disables both.
5. Persistence routes the main shortlist through the Supabase, Google Sheets, or dual-write
   backend selected by configuration. Reporting queries live in
   [`src/alice/persistence/sql/`](../src/alice/persistence/sql/).

The main application path routes model requests through
[`src/alice/llm/llm.py`](../src/alice/llm/llm.py). That module selects the
Anthropic or OpenRouter transport, records cost, and applies task-level budget
tripwires. The isolated
[`validate_messages_api.py`](../src/alice/harness/validate_messages_api.py)
conformance harness bypasses the wrapper for up to three explicit raw API
probes; those calls do not enter the cost log.

## Downstream preparation

[`prep_pipeline.py`](../src/alice/pipeline/prep_pipeline.py) uses a staged
`GROUND -> WRITE -> VERIFY -> ASSEMBLE` flow. Missing required Stage 1
grounding stops the pipeline. Stage 3 records ordinary claim-pattern,
value-led, voice, and residue findings for operator review, but those findings
do not withhold the generated drafts in v1. A banned-framing or anonymization
breach is the hard verification rule: it sets `halted_at_stage` to `VERIFY`,
withholds the drafts, and writes a blocked report.

## Configuration boundary

- `config/fit_model.toml` owns versioned profile values: domain definitions,
  selected seniority, weights, and thresholds. `fit_judge.py` owns their
  interpretation, fixed exceptions, and verdict contract.
- `config/profile_archetypes.toml` owns profile archetype data.
- `config/observability_production_policy.json` owns observability SLO and
  alert policy.
- Credentials and live operator state are external to this public repository.

Historical design records are listed separately in the
[documentation index](README.md). They explain why the current implementation
exists but do not define the live path.
