# Decision Log

Non-trivial design decisions with the reasoning + the alternatives rejected.
Newest first.

---

## 2026-06-01 — Location moved from the LLM judge prompt to a deterministic pre-gate

**Decision.** Pull the LOCATION viability check out of `fit_judge`'s
LLM prompt and into a deterministic pre-gate (`src/alice/pipeline/location_gate.py`,
`location_gate(...) -> kill | reach_flag | ok`). The gate runs *before* the
LLM; a `kill` short-circuits the model entirely; `reach_flag` caps the LLM verdict
at REACH (surface); `ok` lets the LLM judge freely. The LLM prompt was slimmed
(~15.3K → ~11.5K chars) to judge ONLY domain / function / seniority / comp.

**Why.** This reverses the redesign's original choice (`docs/SOURCING_MATCHER_
REDESIGN.md` §4.2: "constraint-driven, NOT a hard-coded gate sequence — feed the
model the constraints and let it judge"). That choice made location
**unstable**: it was entangled in one large prompt, so every domain/prompt change
perturbed it. Observed three times in one session — (1) the metro-thrash,
(2) the regional fix regressing when domain prose was added, (3) the jordan-v3
domain expansion dropping GUARD-1 ~37→33 by shifting *location* verdicts. A
deterministic rule encodes the remote-first preference exactly; an entangled
prompt drifts on it every time the surrounding prose changes.

**Mitigation of §4.2's original concern** ("don't silently kill on a city label the
JD never qualifies"): the gate (1) reads the JD *body* for an explicit requirement,
never the bare city label; (2) is conservative — anything ambiguous returns
`reach_flag` or `ok`, never a silent `kill`. A listed metro with no residence
requirement is *surfaced*, not cut.

**Alternatives rejected.**
- *Keep tuning the in-prompt location logic.* Rejected: three destabilization
  incidents showed the entanglement is structural, not a wording bug.
- *Switch the judge to a stronger model.* Rejected: multiple models agreed on
  the location reads — not a model-strength problem.
- *Pure deterministic gate as sole authority, no reach_flag.* Rejected: too brittle
  (false-killed FIT roles on bare "hybrid"); the three-way kill/reach_flag/ok with
  conservative defaults is the balance.

**Outcome.** Gate policy 13/13 (incl. the regional reads the LLM couldn't).
GUARD-1 **37/40 lenient, 0 wobble across runs** vs a re-labeled benchmark (parity +
stable). Tests: `tests/test_location_gate.py` (gate policy) + `tests/test_fit_judge.py`
(prompt-location assertions migrated to gate-behavior). Benchmark re-labeled per
the adjudicated policy (bare-metro → REACH; build/function roles incl.
CS/Onboarding/TAM → domain-soft → REACH).

**Status.** On branch `alice-location-gate`; merge to main + deploy pending review.
Follow-ups: 3 residuals (one travel gate-miss; two on-domain sell-into LLM
over-cuts); an agent-role-labeled set to measure the agent-domain (jordan-v3) benefit.
