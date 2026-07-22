# Contributing / working in this repo

<!-- sourcebound:purpose -->
A short orientation for a human or agent making changes here.
<!-- sourcebound:end purpose -->


## Run the tests

```bash
pip install -e ".[dev]"
pytest -q       # hermetic: no network, secrets, or database
```

`pip install -e .` installs the `career-scout` distribution and its `alice`
runtime package (src layout); tests and callers
import it by path, e.g. `from alice.pipeline import fit_judge`.

Two testing tiers, kept separate on purpose:
- **`tests/` (pytest)** — the hermetic suite that runs in CI. No network, secrets, or DB.
- **`src/alice/harness/` (`check_*.py`) + `scripts/harness/run_harness.sh`** — a
  manual behavioral harness that makes live LLM calls. Run it by hand
  (`PYTHON=python3 scripts/harness/run_harness.sh`), never in CI. The files are
  named `check_*` (not `test_*`) so pytest never collects them.

## Conventions

- **The engine reads config; it does not embed candidate data.** Fit logic lives
  in code under `src/alice/`; the persona/rubric lives in `config/fit_model.toml`.
  Change behavior by editing the rubric, not by hardcoding specifics.
- **Fail closed.** New tool registrations must carry a guard (enforced at import
  in `src/alice/tools.py`); deterministic gates run before the LLM; parse failures
  fall back to `NOT-FIT`. Keep it that way.
- **Keep the suite hermetic.** Tests must not require network, secrets, or a live
  database. Inject the LLM call and any I/O.
- Record consequential design decisions in `docs/DECISION_LOG.md`.

See [`docs/README.md`](docs/README.md) for current architecture and operating
references. `AUDIT.md` is a preserved assessment, not a current runbook.
