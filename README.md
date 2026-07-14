# career-scout

<!-- clean-docs:purpose -->
You're job-hunting under a hard constraint — *I can't relocate, I can't take a travel-heavy role right now* — and the most expensive mistake an agent can make isn't missing a good job. It's a confident **"this one fits"** on a role that was never viable: you read it, tailor a résumé, apply, and the cost lands on the one thing a job search can't get back — your time.
<!-- clean-docs:end purpose -->


**career-scout sources listings, screens them under that constraint, and hands back a short, explained list plus drafted application materials.** Alice is the bundled operator persona, not a separate product or package. The location/travel kill happens in deterministic code *before* any model runs; the LLM only gets a vote on roles that already cleared viability.

> Public, sanitized copy of a tool actually run on a daily schedule. The persona it screens for — "Jordan Avery" — and the experience corpus it matches against are **synthetic**; the example data describes no real person. Licensed under Apache-2.0; see [`LICENSE`](LICENSE).

## How it screens

Listings come in from public job APIs and ATS boards. Alice spends compute in increasing order of cost: cheap deterministic gates reject the bulk of every run with no model call, and the LLM fit-judge only ever scores what survives.

```
 SOURCES                  DETERMINISTIC GATES                MODEL                 OUTPUT
 (public, real)           (reject most — no LLM)             (survivors only)      ──────
 ──────────────           ───────────────────────           ──────────────
 Remotive                 role / archetype                   fit-judge reads       ranked,
 RemoteOK         ──────► domain (mfg / AI / SaaS)   ──────► config/fit_model      explained
 Jobicy                   travel (negation-aware)            .toml → one           shortlist
 Himalayas                location / residence / RTO         holistic verdict      + drafted
 HN who's-hiring          ───────────► location_gate         FIT / NOT-FIT /       materials
 Greenhouse / Lever /     RUNS BEFORE THE MODEL              REACH
 Ashby ATS boards              │                             (unparseable
 (curated + auto-grown)        ▼                              → NOT-FIT)
                          gate survivors only
```

**The kill decision is deterministic, and it runs first.** `location_gate.py`'s `location_travel_gate(...)` is called inside `fit_judge.judge_listing()` *before* the model — a `kill` short-circuits the LLM entirely. The gate reads the JD **body** for an explicit requirement (relocation, days-in-office/RTO, residence area, non-US-only, travel ≥10%), never a bare city label, and is deliberately conservative: anything ambiguous returns `reach_flag` or `ok`, never a silent kill. This is a reversal of an earlier design that lived inside the LLM prompt — pulled out because an entangled prompt drifted on location every time the surrounding prose changed (three destabilization incidents in one session; see [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md)).

**The model votes; it never authors the kill.** The fit-judge reads the rubric and the JD body and emits one of `FIT` / `NOT-FIT` / `REACH`. Its system prompt is built entirely from the TOML — the persona's domain worlds, functional-fit gradient, seniority targets, and comp composite are *data*, never engine code. Crucially, the parser **fail-closes**: an unparseable judge reply resolves to `NOT-FIT` with constraint `parse_error`, and any LLM error (including a missing API key) resolves to `NOT-FIT` with `judge_error`. A malformed response can cost you a missed role; it can never cost you a wasted application.

**The rubric is versioned data, not engine.** `fit_judge.py` is pure engine; `config/fit_model.toml` (`version = "operator-v3"`) holds the worlds, gates, weights, and comp band. Tuning the search means editing the TOML — the judge logs the config version with every verdict for audit and reproducibility.

**One LLM chokepoint.** Every model call goes through `llm.py` — stdlib `urllib`, no vendor SDK. It pins a model per task across three tiers (Haiku for cheap conversational paths, Sonnet for synthesis, Opus for résumé/cover drafts and the adversarial critic; the fit-judge itself runs on `gemini-2.5-flash` via OpenRouter), appends every call's token cost to an append-only JSONL log, and fires a soft tripwire past a **$2/day / $14/week** budget. Tool-result text from the model loop is run through a prompt-injection annotator, and the roundtrip cap fails loud rather than looping.

**A mutating tool can't register without a guard** — `register_tool(..., mutating=True, guard=None)` raises at *import*, not at runtime, and `tests/test_tool_guard_invariant.py` keeps that invariant in CI.

**Reporting is analytical SQL.** Pipeline funnel, company-suppression, judge-drift, and status-transition reporting are expressed as CTEs, conditional aggregation (`count(*) filter (...)`), and a `lead() over (partition by ...)` window function in `reporting.py`, unit-tested against SQLite, and shipped as Postgres views declared `WITH (security_invoker = true)` so a tenant's RLS policies still apply — an aggregate over *only* its own rows, never across tenants.

Downstream, `prep_pipeline.py` drafts application materials through an explicit `GROUND → WRITE → VERIFY → ASSEMBLE` pass that won't emit a claim it can't ground in the source. Persistence is a thin router over three ledger backends — Supabase (canonical), Google Sheets (legacy bridge), or dual-write — so where results land is a config flag, not a rewrite.

## Run it

The test suite is hermetic — no network, no secrets, no database — so a fresh clone shows the pipeline working:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest        # 412 passing — fail-closed paths, the location gate, the ledger router, and the SQL are all covered
```

Running it live (real sourcing, a real ledger, a real digest) needs an LLM key and ledger/notifier credentials from the environment. Those are not in the repo.

## Eval discipline

The judge is checked against an **adversarial harness** (`src/alice/harness/adversarial.py`) that deliberately tries to make Alice break her brief — fabricate a comp datum she doesn't have, auto-apply a subtractive filter without approval, or follow a prompt-injection input — with each case asserting on what *failure* looks like. The location gate is exercised by `tests/test_location_gate.py`, travel negation by `tests/test_travel_negation.py`, and the fail-closed verdict path by `tests/test_fit_judge.py`.

## Where to look first

- [`docs/SOURCING_MATCHER_REDESIGN.md`](docs/SOURCING_MATCHER_REDESIGN.md) — the funnel: cost-layering, the engine/config split, the keyword-not-in-prompt guard.
- [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md) — why location moved out of the LLM prompt into a deterministic pre-gate, and the alternatives rejected.
- `src/alice/pipeline/location_gate.py` + `src/alice/pipeline/fit_judge.py` — the gate, the chokepoint order, and the fail-closed parser.
- [`AUDIT.md`](AUDIT.md) — an honest current-state assessment, including the one persistence seam left deliberately for a reviewed pass.
