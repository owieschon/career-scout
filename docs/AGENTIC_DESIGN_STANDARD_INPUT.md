# Alice Agentic-Design Framework

This is the design framework Alice's architecture follows. The thesis: you cannot code away the probability of a nondeterministic agent; you manage it through evals and self-correction. Every principle is a probability-management strategy, not a correctness guarantee.

**The framework (five source principles plus extensions):**

Source principles (after Philipp Schmid, philschmid.de/why-engineers-struggle-building-agents): (1) Stop Fighting the Model — dispatcher not traffic-controller; constraints not procedures. (2) Preserve Meaning — text over booleans; carry rich state to judgment, project to discrete at action, never discard the source. (3) Design for Recovery — errors are inputs in work-loops (feed back, self-correct), fail-fast at guards. (4) Evaluate Don't Assert — measure the distribution (pass^k, LLM-as-judge), not binary assertion; include negative cases; grade outcomes not paths. (5) Build to Delete — harness and scaffolding are modular and deletable because they compensate for the current model and will be rewritten; agent-ready interfaces (semantic naming, verbose docstrings, unambiguous parameters — especially critical because Alice's chat runs a literalist cheap-tier model).

**Extensions beyond the source, central to this project:**

- **The regime classification (the meta-rule):** classify each boundary first as one of three regimes — JUDGMENT/WORK (soft: carry rich state, hand over control, errors-as-inputs), IRREVERSIBLE-ACTION/GUARD (hard: discrete state, retain control via confirmation, fail-fast), or DETERMINISTIC-PLUMBING (fixed-is-correct; the principles largely don't apply — e.g. `run_daily`'s fixed step sequence; mis-classifying plumbing as judgment would "fix" correct deterministic code). The classification determines which way each principle points, and is what makes correct-inversion discrimination possible.

- **Loud-not-silent corollary:** errors must become surfaced inputs, never silently swallowed — the swallowed error (which looks like success) is the worst case. This applies to unrecovered errors; transient-then-recovered errors are fine to handle quietly. This is the highest-yield principle in practice.

- **Preserve-Meaning override sub-clause:** the live signal must retain authority to override the stale discrete projection; downstream actions must consult the live signal, not only the discrete state. This is the structural root of the trust-failure class — for example, `focus.auto_drop_submitted` (`focus.py:206-228`) acts on the status enum without consulting the live intent column.

- **Sixth principle — BUILD TO AUDIT:** distinct from loud-not-silent (errors) and Evaluate-Don't-Assert (pre-deploy verification). Covers ongoing auditability of non-error decisions in production: a gate dropping 42 roles isn't an error, but you must be able to inspect which 42 and why. Sourcing reports aggregate counts with no itemized trail, so "0 qualified" is indistinguishable from "silently starving." This is the structural answer to the recurring calibration question.

**Conflicts the regime classification resolves:** Preserve-Meaning vs Build-to-Delete (keep the source only if a downstream judgment consumes it). Stop-Fighting vs Design-for-Recovery on transient errors (transient → self-recover; persistent → surface loud).

**Prioritized opportunity list (leverage = blast-radius × cheapness):**

1. **Ambiguous-parameter tool cluster** (Build-to-Delete): `flag_experience_candidate::source_turn_ts` (`tools.py:~1013-1020`), `flag_correction_candidate::operator_turn_ts/alice_turn_ts/alice_claim` (`~1137-1159`), `generate_application_package::substring` (`~856`), and `mark_role_status`. A literalist cheap-tier model fills these by inference, writing to durable stores. Fix: structured references, enum constraints, copy-don't-infer-with-validation.
2. **Silent-swallow cluster** (loud-not-silent): `obs.flag_grounding_event` swallows Sentry-dispatch exceptions and returns False, and callers (`telegram_bot.py:1556-1616`) don't check — a grounding flag (a safety surface) can silently fail to surface. Sourcing aggregator fetch failures are swallowed (`daily_delta.py:362-432`).
3. **Build-to-Audit instrumentation for sourcing gates:** an itemized drop log (which role, which gate, why) plus anomaly surfacing. The durable answer to "is the filter starving you."
4. **Negative-case coverage for the eval spine:** `detect_category_mismatch` and `detect_specific_claims_without_tools` have no unit tests; the F2 evals are single-trial.
5. **Intent-consults-on-action** (Preserve-Meaning sub-clause applied): auto-drop, scoring, and digest must consult live intent before acting on status — structural mitigation for the trust-failure class.

**Correct-inversions to document as intentional (do not "fix"):** the `ledger.py:72` auth gate; `focus.py:217-228` auth-skip-on-unauthorized; `check_push_consistency` fail-fast; `deploy_guard` STALE; stale-code fail-loud; the confirmation window; grounding detectors in-process every turn; the `run_daily` deterministic sequence (the plumbing regime).

**Framework-vs-work cross-check:** the framework explains the shipped work. Sourcing body store maps to Preserve Meaning; misparse hardening to Build-to-Delete plus Stop-Fighting in the hard regime; silent-empty to loud-not-silent (the class is broader, spanning `obs.py` and the aggregators); the pre-push gate to a correct-inversion fail-fast guard; `describe_capabilities` to Preserve Meaning; the grounding detectors to Evaluate-Don't-Assert (with the caveat that two of four detectors still lack negative-case tests).
