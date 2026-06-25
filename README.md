# Alice — a fail-closed job-search agent

When you're job-hunting under hard constraints — I can't relocate, I can't take
travel-heavy roles right now — the most expensive mistake an agent can make is a
confident *"this one fits"* on a job that was never geographically or logistically
viable. You read it, you tailor a résumé, you apply. The cost lands on the one
resource a job search can't get back: your time.

So I built Alice around a rule I won't let the model break: **the location and
travel gate runs before the model, and an LLM reply it can't parse resolves to
NOT-FIT, never FIT.** The verdict that wastes my time can't be authored by a
drifting prompt or a malformed response. The model gets a vote on the roles that
are already viable — never the kill decision on the ones that aren't.

That's the whole stance: an unreliable LLM is one tested, fail-closed stage
inside a deterministic pipeline, never the engine that decides which roles are
worth my time.

## How it screens

Alice pulls new listings, then spends compute in increasing order of cost. Cheap
deterministic gates reject the bulk of them; the LLM fit-judge only ever scores
what survives.

```
 listings          deterministic gates          model            output
 ────────          (reject most, no LLM)         ─────            ──────
 Remotive          keyword / role / remote-US    fit-judge        ranked, explained
 RemoteOK     ───► location / travel        ───► reads a TOML ──► shortlist
 Jobicy            (runs BEFORE the model)       rubric →         + drafted materials
 Himalayas              │                        FIT/NOT-FIT/REACH
 HN who's-hiring        ▼                        (unparseable
 Greenhouse/        gate-survivors only           → NOT-FIT)
 Lever/Ashby
```

The fit logic is code that *reads* a versioned rubric (`config/fit_model.toml`):
the persona's target worlds, comp band, and seniority are data, not engine. Every
model call goes through one chokepoint (`src/alice/llm/llm.py`) — stdlib `urllib`,
no LLM SDK — which pins a model per task, appends every call's token cost to a
JSONL log, and warns past a soft `$2/day` / `$14/week` budget. A mutating tool
can't even register without a guard; that's checked at import, not at runtime.

Reporting is analytical SQL (CTEs, conditional aggregation, a window function),
unit-tested against SQLite and shipped as RLS-aware Postgres views (denied by
default, scoped per user). The ledger is a thin router over three backends —
Sheets, Supabase, or dual-write — so where results land is a config flag, not a
rewrite.

## Run it

The repo is the engine. The test suite is hermetic — no network, secrets, or
database — so a fresh clone shows the pipeline working:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest        # over 400 tests; fail-closed paths, ledger router, and SQL are covered
```

Running it live (real sourcing, a real ledger, a real digest) needs an LLM key
and ledger/notifier credentials from the environment. Those are not in the repo.

## What's here

Sources are real: Remotive, RemoteOK, Jobicy, Himalayas, HN who's-hiring, plus
Greenhouse / Lever / Ashby ATS boards. Downstream, `prep_pipeline.py` drafts
application materials through an explicit `GROUND → WRITE → VERIFY → ASSEMBLE`
pass that won't emit a claim it can't ground in the source.

Public, sanitized version of a tool I actually run: the persona it screens for
("Jordan Avery") and its experience corpus are synthetic, so the example data
describes no real person. All rights reserved — published for review, not reuse
(see `LICENSE`).

The design reasoning is in [`docs/DECISION_LOG.md`](docs/DECISION_LOG.md) and
[`docs/FIT_STRATEGY_SPINE.md`](docs/FIT_STRATEGY_SPINE.md); the funnel in
[`docs/SOURCING_MATCHER_REDESIGN.md`](docs/SOURCING_MATCHER_REDESIGN.md). The
honest current-state assessment — including the one persistence seam I kept on
purpose — is in [`AUDIT.md`](AUDIT.md).
