# Codebase audit

An honest assessment of this repository: what it is, what is strong,
and what would change to make a senior engineer respect it. Written so a
reviewer who has never seen the code can calibrate quickly.

## What it is

A single-operator job-search agent. A scheduled run (`src/alice/ops/run_daily.py`)
sources listings from public job APIs and ATS boards, de-duplicates against a
`seen_jobs` store, and funnels each new posting through cheap deterministic gates
and then an LLM fit-judge that reads a TOML rubric and returns FIT / NOT-FIT /
REACH. Survivors are written to a dated brief, recorded in a pluggable ledger,
and pushed as a Telegram/email digest; downstream stages draft application
materials. ~120 modules, organized as an installable `src/alice/` package (subpackages: pipeline, persistence, llm, notify, observability, ops, harness).

## Strengths (worth a reviewer's attention)

- **The cost-layered funnel is real and documented.** Deterministic gates run
  before the model; the code matches `docs/SOURCING_MATCHER_REDESIGN.md`.
- **Engine/config separation.** `fit_judge.py` is pure engine; all
  candidate-specific data lives in `config/fit_model.toml`. The judge reads the
  rubric and never embeds specifics.
- **Safety invariants that fail closed.** Mutating tools can't register without a
  guard (enforced at import); the location gate runs before the LLM so verdicts
  can't drift with prompt changes; parse failures fail safe to NOT-FIT.
- **One LLM chokepoint** (`llm.py`) with per-task model pinning, cost logging, and
  a soft daily-budget alert.
- **Adversarial eval discipline** — the harness checks the judge against
  fabricated-evidence cases, not just happy paths.

The honest one-line framing: *an LLM-engineering project with real eval
discipline and real cost/safety reasoning* — not a deployable product.

## Engineering notes

Notable structural work in this codebase, each verified against the green test
suite (412 passing):

- **Credentials externalized** to environment variables; none are committed.
- **Portability:** two tests that read git-ignored local state were made
  hermetic / removed, so `pytest` passes on a fresh clone.
- **Dependency story made true** — `supabase_ledger.py` imported `supabase`
  without declaring it; added as a `[supabase]` optional-dependency extra.
- **Sourcers unified** — the four duplicate Greenhouse/Ashby/Lever fetch
  implementations (across `daily_delta`, `source_multi`, `source_deep`, and the
  orphaned `source_listings`) collapsed into one `src/alice/pipeline/ats_client.py`; callers
  inject their own HTTP helper so behavior is identical. Deleted the orphaned
  726-LOC `source_listings.py`; added `tests/test_ats_client.py`.
- **Ledger duplication removed + design made deliberate** — the verbatim-duplicated
  helpers (incl. the safety-critical write-authorization gate) extracted to
  `src/alice/persistence/ledger_common.py` with a documented `Backend` protocol; both backends
  re-export them (one class object). Docstrings reframed so Supabase Postgres is
  the canonical backend and Sheets/dual-write are a labeled legacy bridge, not an
  unfinished migration.
- **Promoted to a real package** — the flat, `pythonpath`-hacked `scripts/`
  (90 modules) is now an installable `src/alice/` package with subpackages
  (`pipeline/`, `persistence/`, `llm/`, `notify/`, `observability/`, `ops/`,
  `harness/`). All `sys.path.insert` hacks removed; imports rewritten to absolute
  `alice.*` paths across 134 files; `pyproject` switched to a src layout. Verified
  by the full suite + an import-every-module check (123 modules). The top-level
  dependency graph is acyclic — measured, and enforced by `tests/test_no_import_cycles.py`.
- **Two test systems disentangled** — the 19 orphaned `scripts/harness/test_*.py`
  (collected by neither pytest nor the harness runner) renamed to `check_*.py`
  and documented as the manual behavioral harness; fixed a vacuous adversarial
  assertion (`lambda t: True`) and a hardcoded interpreter path in `run_harness.sh`.
- **SQL reporting layer added** — pipeline/judge/transition reporting expressed
  as analytical SQL (CTEs, conditional aggregation, a window function) in
  `src/alice/persistence/reporting.py`, unit-tested against SQLite, and shipped as RLS-aware
  Postgres views in `migrations/supabase/0002_reporting_views.sql`. Both
  migrations were applied and the views exercised against a live Postgres
  instance; the SQLite path is covered by `tests/test_reporting.py`. This is the
  "aggregation belongs in SQL" answer to the Python-`Counter`-over-all-rows
  pattern the ledger still uses on its Sheets backend (see proposal 1).
- **Ledger router + dual-write now tested** — `tests/test_ledger_router.py` (8
  tests, in-memory fakes) covers dual fan-out, drift journaling on either-backend
  failure, dual reads with Supabase-preference + Sheets fallback, and the
  write-authorization gate across the Supabase dispatch path.
- **Freeform router decomposed** — the 737-line `_route_message_freeform` body
  was reduced to 444 lines by extracting 10 verbatim anchor/directive helpers,
  behind 12 new characterization tests (`tests/test_route_freeform.py`).
- **No import cycles — proven and enforced** — measured the 123-module graph:
  zero top-level cycles, and none of the function-local internal imports are
  cycle-breakers (they are deliberate lazy-loads: optional deps, off-by-default
  observability, avoiding import-time cost). `tests/test_no_import_cycles.py`
  fails CI if a cycle is ever introduced.
## What would still change (prioritized)

These are **proposals**, not done. They change behavior or structure and were
deliberately left for a reviewed pass rather than executed blind.

### High value
1. **Finish the ledger collapse (deferred — needs live verification).** The
   duplication is gone and the design is now a deliberate Supabase-canonical
   adapter, but Sheets + dual-write still exist and the Supabase backend still
   emulates a gspread worksheet (`SupabaseWorksheet`/`_Cell`/`_parse_a1`) because
   the caller-facing API is worksheet/`row_idx`-shaped. Removing Sheets and moving
   to a job-key-keyed repository changes that API across ~8 caller modules, several
   on live paths the hermetic suite cannot exercise. Left as a reviewed step
   rather than a blind one, to honor 'don't ship a change you can't verify'.
2. **Resolve the sourcing-persona divergence.** The shared ATS fetch is now one
   client (`ats_client.py`), but the role-keyword sets still differ
   (`source_multi`/`source_deep` hardcode lists; `daily_delta` reads
   `fit_model.toml`). Converge them on the config-driven persona.
### Lower
3. **Language fit** — `triage_observations.py` reverse-engineers structured events
   out of Markdown headers; append them as JSONL (like `outcomes.py`) and render
   Markdown separately, removing a silent-data-loss path.

## Known limitations / open questions

- **Live integration.** The hermetic suite now covers the ledger router and
  dual-write *logic* via in-memory fakes, but the real daemon (live Supabase /
  Telegram / Google Sheets over the network) is exercised only by the manual
  `src/alice/harness/`, not by CI.
- The behavioral harness (`src/alice/harness/`) makes live API calls and is run
  by hand (`run_harness.sh`), not in CI.
