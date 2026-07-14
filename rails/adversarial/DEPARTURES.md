# Departures from SPEC_adversarial_eval_framework.md

<!-- clean-docs:purpose -->
Per spec section 0: every departure stated, with grounds. All are open choices; none touches the section 10 non-divergable core. Read this page before changing or relying on Departures from SPEC_adversarial_eval_framework.md so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->


1. **Sandbox fixture instead of live-repo violations.** Cases run against
   a disposable toy repo into which the host's ACTUAL trust-layer files
   are copied at run time. Grounds: known-bad inputs must not corrupt
   real work; the mechanism under test is byte-identical.

2. **Hook cases drive the guard scripts via the documented stdin
   contract, not a live Claude Code session.** Grounds: determinism and
   zero token cost. Residual assumption (Claude Code delivers hook events
   per contract) is owned by the environment-change trigger: the registry
   records the CC version, and verify.sh fails the governor check on a
   recorded environment change, forcing a re-proof.

3. **"Inflation" is implemented as its mechanically assertable forms:**
   stale-green presented as current (case 08), forged/tampered verdicts
   (case 09), and fabricated test names (case 06). Prose-level scope
   inflation in a handoff has no mechanical oracle and remains at the
   human gate, consistent with spec section 8.

4. **Claim-not-landed proven against a local bare remote.** Grounds:
   identical git semantics, no network dependency, deterministic.

5. **Two classes added beyond the section 3 starting set** (the set is
   explicitly a floor): `undisclosed-judgment` (case 10, the senior-
   engineer disclosure gate) and `unproven-governor-change` (case 11,
   the section 4 primary trigger, mechanized as a fingerprint gate).

Framework changes made alongside this eval (human-released via review of
this delivery, and themselves proven by the eval before taking force,
because verify.sh now refuses an unstamped governor):
- rails/evidence/, rails/config.json, rails/adversarial/ added to the
  protected set (closes forge-a-verdict and soften-the-test-command,
  both found while designing case 01).
- decisions check (disclosure of judgment, not grading of it).
- governor_proven check (spec section 4, framework-change trigger).

---

## Repo-specific departures: job-search (Alice_Agent), retrofit 2026-06-09

First retrofit of the kit onto an existing live repo. Stated here so none
of it is a silent drop.

R1. **Gating suite scoped to `tests/`, not the whole repo.** `tests/` is
    hermetic: 374 collected, 372 passed + 2 skipped, exit 0, ~5s. The
    `scripts/harness/` tree (107 tests) is EXCLUDED from `test_cmd`/
    `collect_cmd`. Grounds: those are live-integration tests that make real
    Anthropic API calls over the network and fail deterministically offline
    (4 fail in a clean sandbox). This is NOT a partial-run dodge (cases
    04/06): the declared suite IS `tests/`, and collect-vs-run counts
    reconcile (374 = 372 + 2). The excluded set is a distinct live category,
    declared here, not hidden.

R2. **Open item — a safety test that cannot yet gate.**
    `scripts/harness/test_critic.py::test_catches_planted_flaws` is exactly
    the kind of check worth promoting to critical (it asserts the critic
    catches planted flaws). It needs a live model, so it cannot gate offline
    deterministic work. To make it gating it needs a recorded-cassette mode
    (e.g. respx/vcr) or a separate live-credentialed verify pass. The
    operator's call; left out of `load_bearing.txt` until then.

R3. **Two governance regimes, deliberately not merged.** This repo already
    has an autonomous-agent protocol (Alice-the-daemon: pushes to remote at
    session end, tracks work in `bd`/beads, has its own pre-push
    self-consistency gate). agent-rails' dispatch model is the OPPOSITE for
    the actor it governs: stop at the commit boundary, human commits, no
    push. These are not in conflict because they govern DIFFERENT actors:
    the rails apply to dispatch-driven DEV sessions building Alice's code
    (active via `.claude/settings.rails.json` once merged); Alice's own
    autonomous runtime stays under `.claude/settings.json` and its existing
    protocol. They must not be conflated. The appended rails block in
    CLAUDE.md is scoped to dispatch work, not to Alice's daemon.

Found during retrofit, NOT a gating departure (flagged to the operator
separately): an API key could print in plaintext in the pytest tracebacks of
the excluded `scripts/harness/` tests — rotate + stop binding the raw key to a
traceback-visible local.
