# career-scout

<!-- sourcebound:purpose -->
career-scout is a single-operator sourcing and screening agent for turning noisy job feeds into a short, explained review queue. A false positive can consume a full listing review and resume-tailoring cycle before a location or travel conflict surfaces; an overbroad rule can hide a viable role entirely. The primary lane applies deterministic constraints before an LLM judges fit, while a bounded recall lane checks a sample of rejects without admitting them automatically.
<!-- sourcebound:end purpose -->


It sources listings, returns a short explained list, and drafts grounded materials for roles that survive screening. Alice is the bundled operator persona, not a separate product or package. Both sourcing paths into the fit model first pass the deterministic location and travel gate. The primary path judges gate survivors. The recall path routes at most `ALICE_DROPPED_SAMPLE_MAX` rejects through the same guarded judge path, then reports likely false negatives for operator review instead of adding them to the shortlist.

> This public copy uses a synthetic persona and experience corpus; the example data describes no real person. Licensed under Apache-2.0; see [`LICENSE`](LICENSE).

## How it screens

Listings come in from public job APIs and ATS boards. Alice spends compute in increasing order of cost: cheap deterministic gates handle the main funnel before model judgment. The fit judge and recall lane are enabled by default; set `ALICE_FIT_JUDGE=0` to disable both. `ALICE_DROPPED_SAMPLE_MAX` caps the first sample of rejected listings sent down the recall path. Its default is `20`, and the hard maximum is `100`; set it to `0` to disable only the recall lane. A non-integer, negative, or above-limit value stops the run before source fetches. Each run logs the configured value, effective value, and whether the value came from the default or explicit configuration.

```text
 SOURCES              CHEAP DETERMINISTIC GATES        GUARDED FIT JUDGE       OUTCOME
 public APIs + ATS ─► role / domain / remote / travel ─► location_travel_gate ─► shortlist
                                      │                  then model verdict
                                      │ rejects
                                      ▼
                             first bounded sample ──────► same guarded judge ───► review-only
                                                                                rescue candidates
```

The main and recall lanes have different authority. Main-lane `FIT` and `REACH`
results can reach the review queue. Recall-lane `FIT` and `REACH` results are
printed as rescue candidates and counted in run statistics, but
`daily_delta.py` does not insert them into the shortlist or ledger.

**The kill decision is deterministic, and it runs first.** `location_gate.py`'s `location_travel_gate(...)` is called inside `fit_judge.judge_listing()` *before* the model — a `kill` short-circuits the LLM entirely. The gate reads the JD **body** for an explicit requirement (relocation, days-in-office/RTO, residence area, non-US-only, travel ≥10%), never a bare city label, and is deliberately conservative: anything ambiguous returns `reach_flag` or `ok`, never a silent kill. This is a reversal of an earlier design that lived inside the LLM prompt — pulled out because an entangled prompt drifted on location every time the surrounding prose changed (three destabilization incidents in one session; see [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md)).

**The model votes; it never authors the kill.** The fit-judge reads the JD body and emits one of `FIT` / `NOT-FIT` / `REACH`. Its system prompt combines versioned profile values from `config/fit_model.toml` with stable judgment rules in `fit_judge.py`, including category meanings, exceptions, and verdict semantics. The parser **fail-closes**: an unparseable judge reply resolves to `NOT-FIT` with constraint `parse_error`, and any LLM error (including a missing API key) resolves to `NOT-FIT` with `judge_error`. A malformed response can cost you a missed role; it can never cost you a wasted application.

**The rescue lane checks recall; it does not override policy.** `daily_delta.py`
attempts to record the reason for every deterministic drop and reports any
failed state write in the run statistics and error output. When the fit judge is active,
it copies no more than the configured sample cap into a second judge batch. The
same deterministic location and travel gate still runs before any model call.
A `FIT` or `REACH` result identifies a review candidate, not an automatic
admission. Every run separates attempted, completed, and failed judge work, retains
failure classes, and initializes all recall counts even on zero-count paths.

**Profile values are versioned data; judgment semantics are engine code.** `config/fit_model.toml` (`version = "operator-v3"`) holds domain definitions, selected seniority, weights, and thresholds. `fit_judge.py` owns their interpretation, the fixed exceptions, and the output contract. The judge logs the config version with every verdict so a review can identify the profile values in force.

**Application LLM chokepoint.** The main application path routes model calls through `llm.py` — stdlib `urllib`, no vendor SDK. It pins a model per task across three tiers (Haiku for cheap conversational paths, Sonnet for synthesis, Opus for résumé/cover drafts and the adversarial critic; the fit-judge itself runs on `gemini-2.5-flash` via OpenRouter), appends those calls' token cost to an append-only JSONL log, and fires a soft tripwire past a **$2/day / $14/week** budget. Tool-result text from the model loop is run through a prompt-injection annotator, and the roundtrip cap fails loud rather than looping. The isolated `validate_messages_api.py` conformance harness deliberately bypasses the wrapper for up to three raw Anthropic wire-shape probes; those explicit live probes do not enter the cost log.

**A mutating tool can't register without a guard** — `register_tool(..., mutating=True, guard=None)` raises at *import*, not at runtime, and `tests/test_tool_guard_invariant.py` keeps that invariant in CI.

**Reporting is analytical SQL.** Pipeline funnel, company-suppression, judge-drift, and status-transition reporting are expressed as CTEs, conditional aggregation (`count(*) filter (...)`), and a `lead() over (partition by ...)` window function in `reporting.py`, unit-tested against SQLite, and shipped as Postgres views declared `WITH (security_invoker = true)` so a tenant's RLS policies still apply — an aggregate over *only* its own rows, never across tenants.

Downstream, `prep_pipeline.py` drafts application materials through an explicit `GROUND → WRITE → VERIFY → ASSEMBLE` pass. Missing required Stage 1 grounding stops the pipeline. The v1 Stage 3 verifier records pattern-based grounding and voice findings for operator review; those ordinary findings do not withhold drafts. Only the named banned-framing and anonymization breaches halt at verification and replace the drafts with a blocked report. Persistence is a thin router over three ledger backends — Supabase (canonical), Google Sheets (legacy bridge), or dual-write — so where results land is a config flag, not a rewrite.

## Verify it locally

The test suite is hermetic — no network, no secrets, no database — so a fresh clone shows the pipeline working:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The live operator entry point is [`scripts/run_daily.sh`](scripts/run_daily.sh).
It performs network calls, reads and writes operator state, and can send
notifications; it is not a demo command. A live run needs an LLM key plus
ledger and notifier credentials from the environment. Those are not in the
repository.

## Eval discipline

The judge is checked against an **adversarial harness** (`src/alice/harness/adversarial.py`) that deliberately tries to make Alice break her brief — fabricate a comp datum she doesn't have, auto-apply a subtractive filter without approval, or follow a prompt-injection input — with each case asserting on what *failure* looks like. The location gate is exercised by `tests/test_location_gate.py`, travel negation by `tests/test_travel_negation.py`, and the fail-closed verdict path by `tests/test_fit_judge.py`.

## Where to look first

- [`docs/README.md`](docs/README.md) — current documentation and preserved design records, separated by lifecycle.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the current funnel, entry points, configuration, and external-effect boundary.
- [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md) — why location moved out of the LLM prompt into a deterministic pre-gate, and the alternatives rejected.
- [`src/alice/pipeline/location_gate.py`](src/alice/pipeline/location_gate.py) and [`src/alice/pipeline/fit_judge.py`](src/alice/pipeline/fit_judge.py) — the gate, the chokepoint order, and the fail-closed parser.
