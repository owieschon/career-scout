# Alice — a job-search sourcing & screening engine

Alice sources job listings from public APIs and applicant-tracking boards, runs
each one through a **cost-layered screening funnel** (cheap deterministic gates
first, an LLM fit-judge only for survivors), and produces a ranked, explained
shortlist plus drafted application materials.

It is a **personal project** built to practice LLM-engineering on a real,
messy problem: treating an unreliable language model as one stage in an
otherwise deterministic, testable pipeline. It is a single-operator tool, not a
multi-tenant product. The persona it screens for in this public copy is
**synthetic** (see [Synthetic data](#synthetic-data)).

## What it demonstrates

- **Cost-layered funnel** — deterministic lexical/keyword/location gates reject
  the large majority of listings before any model call; the LLM only ever sees
  the few survivors. The funnel is documented in
  [`docs/SOURCING_MATCHER_REDESIGN.md`](docs/SOURCING_MATCHER_REDESIGN.md) and the
  code matches the doc.
- **Engine / config separation** — the fit logic is pure code that *reads* a
  versioned rubric (`config/fit_model.toml`); candidate-specific data never lives
  in the engine. The fit-judge scores from the rubric, so the persona's worlds,
  comp band, and seniority are data, not code; its display name still appears in
  a few prompt strings.
- **Safety invariants that fail closed** — a mutating tool cannot register
  without a guard (enforced at import in `src/alice/tools.py`); the deterministic
  location gate runs *before* the LLM so a location verdict can't drift when a
  prompt changes; an unparseable model reply fails safe to `NOT-FIT`, never a
  false `FIT`.
- **A single LLM chokepoint** — every model call goes through `src/alice/llm/llm.py`,
  which pins a model per task, logs token cost, and warns on a daily-budget
  overage (a soft alert, not a hard cutoff).
- **Eval discipline** — a hermetic test suite plus an adversarial harness that
  checks the judge against fabricated-evidence cases, not just happy paths.
- **SQL where it belongs** — reporting and aggregation are expressed as
  analytical SQL (CTEs, conditional aggregation, a window function for status
  transitions), not Python row loops. The queries are unit-tested against SQLite
  (`src/alice/persistence/reporting.py`, `tests/test_reporting.py`) and ship as RLS-aware
  Postgres views (`migrations/supabase/`).

## Architecture

```
 sources                 screening funnel                    sinks
 ───────                  ────────────────                    ─────
 public job APIs   ┐
 (Remotive,        │      ┌─ keyword / role / remote-US gates  (score_job.py)
  RemoteOK,        │      │        │  (cheap, deterministic — reject most)
  Jobicy,          ├─► new│        ▼
  Himalayas,       │  jobs│      location / travel gate        (location_gate.py)
  HN "who's        │  only│        │  (runs BEFORE the model)
  hiring")         │   ▲  │        ▼
 curated ATS       │   │  │      LLM fit-judge  ── reads ──►   config/fit_model.toml
 boards (Greenhouse│   │  └─►       │  FIT / NOT-FIT / REACH + dimensional band
  Lever, Ashby)    ┘   │           ▼
                       │      ranked shortlist ──► dated Markdown brief
              dedup ───┘                      ├──► ledger (Sheets │ Postgres │ dual)
            (seen_jobs,                        ├──► Telegram / email digest
             SQLite or JSON)                   └──► prep-package + outreach drafts
```

- **Sourcing** (`src/alice/pipeline/daily_delta.py`, `source_*.py`) pulls listings, keys on
  stable job IDs against a `seen_jobs` store (SQLite, or JSON for cloud runs), and
  passes only genuinely-new postings downstream.
- **Screening** (`src/alice/pipeline/score_job.py` → `location_gate.py` → `fit_judge.py`)
  is the cost-layered funnel above. The fit-judge reads the JD body plus the TOML
  rubric and returns a verdict with a structured `fit_dimensions` band.
- **Serving** — survivors are written to a dated Markdown brief, recorded in the
  ledger, and pushed as a digest. The **ledger** (`src/alice/persistence/ledger.py`) is a thin
  router over pluggable backends — Google Sheets, Supabase Postgres, or a
  dual-write bridge — so the storage choice is a config flag, not a rewrite.
- **Downstream generation** — `prep_pipeline.py` produces application materials
  through an explicit `GROUND → WRITE → VERIFY → ASSEMBLE` sequence that refuses
  to emit claims it can't ground in the source; `draft_outreach.py` drafts
  outreach. A Telegram bot (`telegram_bot.py`) is the interactive front end.

## Run it

The repository is the **engine**. The test suite is hermetic — no network,
secrets, or database — and is the fastest way to see the pipeline work:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 412 passing, 2 skipped
```

`pip install -e ".[dev]"` installs the `alice` package (src layout) and its
runtime dependencies plus `pytest`. The engine is a real package, so tests and
callers import it by path — `from alice.pipeline import fit_judge`.

Running the pipeline live (sourcing real listings, writing a ledger, sending a
digest) requires API access and configuration — an LLM key for the fit-judge, and
ledger/notifier credentials read from the environment. Those are deliberately not
in the repository.

## Repository layout

| Path | What's there |
|------|--------------|
| `src/alice/` | The engine package: `pipeline/` (sourcing, scoring, fit-judge, prep), `persistence/` (ledger, reporting), `llm/`, `notify/`, `observability/`, `ops/` |
| `src/alice/harness/` | The manual behavioral harness (`check_*.py`, driven by `scripts/harness/run_harness.sh`) — live LLM calls, run separately from `pytest` |
| `config/` | The fit-model rubric and profile archetypes (TOML) — the engine reads these |
| `knowledge/` | Reference material the engine retrieves over (screening methodology, a synthetic experience corpus) |
| `tests/` | The hermetic `pytest` suite + fixtures |
| `evals/` | Scope-regression eval cases |
| `migrations/` | Supabase Postgres schema (`0001`) + reporting views (`0002`) |
| `docs/` | Design docs — start with `SOURCING_MATCHER_REDESIGN.md`, `FIT_STRATEGY_SPINE.md`, `DECISION_LOG.md` |

## Synthetic data

The example data is synthetic: a **fictional persona** ("Jordan Avery") and
fictional employers. The `config/fit_model.toml` rubric, the
`knowledge/experience/` corpus, and the test fixtures are example data — they
demonstrate how the engine consumes a profile and describe no real person.

## Status

A working personal project. The test suite is green, the engine is a real
`src/alice/` package with an acyclic import graph (enforced by a test), and the
load-bearing paths — fit-judge, ledger router/dual-write, message routing — are
covered. One honest seam remains: more than one persistence path behind the
ledger, kept as a deliberate adapter. See [`AUDIT.md`](AUDIT.md) for the full
current-state assessment.
