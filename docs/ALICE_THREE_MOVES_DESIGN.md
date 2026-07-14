# Alice — Three-Move Architecture (design)

<!-- clean-docs:purpose -->
**Status: design surfaced for the operator's review. Nothing built. No behavior changed.** Grounded in a code-read state audit, not self-report. Read this page before changing or relying on Alice — Three-Move Architecture (design) so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow doc-length reason="The Alice — Three-Move Architecture (design) reader path stays in one file because splitting it would separate its operating context from its verification material" -->


This document designs three moves coherently, because all three descend from one
root finding: **Alice is most uncertain, and fails, when a decision depends on
the operator's internal state/intent that she cannot observe** (is a stalled role
deliberating-vs-forgotten; is silence burnout-vs-busy; is this message a question
or a job-search task). The response is not more autonomy (that is more confident
guessing). It is three consistent disciplines:

1. **Move 1** — close the intent/state information gap cheaply (explicit signal, not inference).
2. **Move 2** — make proactivity selective and fail-safe (surface less, gated by cost, not flooding).
3. **Move 3** — enforce answering structurally (remove the pull-to-redirect at prompt-construction, not by instruction).

The research convergence (metacognition + proactive-agent literatures) says the
reliable path is **cheap explicit signals + configured boundaries + structural
enforcement**, NOT the model self-monitoring or modeling the user (both
empirically unreliable). Every move below favors a structural mechanism over an
instructed one, defines an explicit fail-safe direction, and exposes its knobs as
config the operator sets.

A cross-cutting note up front: Moves 1 and 3 both "read what the operator wants," and the
codebase **already has** the seed of a shared reader — `_INTENT_TOPIC_RE` +
`_select_relevant_observations` (`telegram_bot.py:1080-1120`), a topic classifier
with a fail-safe default-to-exclusion. The coherence requirement is satisfied by
**extending that one primitive** into a shared input-reader both moves consume,
not by writing two new classifiers. See §4 (Cross-cutting).

---

## MOVE 1 — Intent-semantics layer (EXTEND existing carriers)

### The current state this targets
- Live pipeline state is the **Google Sheet** (`ledger.py`, `ledger.HEADERS:29-30`), not `pipeline.db` (vestigial). Design targets the Sheet.
- Carriers exist (Sheet `status` dropdown col G, ~24 reply directives, `focus.json`) but **every one encodes pipeline-position, none encodes the operator-intent.**
- `focus_enforce.py` **infers** "deliberating vs forgot vs done" from `status_changed_date` (`compute_disengagement_flag:109`, `compute_distraction_flag:62`, thresholds `DISENGAGEMENT_THRESHOLD_DAYS:25`). Alice guesses at exactly the state the operator could declare.

The gap is **semantic, not infrastructural.** The design adds an intent vocabulary
to carriers that already exist and makes declared intent **override** the heuristic.

### 1A. Prerequisite — close the decision-feedback loop (Move 1, step one)

The audit (and my spot-check) confirmed `decision_feedback.render_digest_block`
(`decision_feedback.py:842`) has **zero callers**. Capture works
(`flag_correction_candidate:267`, `parse_and_apply_reply:900` wired into the reply
paths), and the scorecard reads its pattern summary, but **captured corrections
never surface for confirmation** and auto-expire unseen. The store has never held a
record; the live "correction rate" comes from a hand-kept 2-row CSV.

**Design:** mirror the *already-working* `experience_store` block in
`daily_delta.py:718-747`. That block is the template:

```
get_pending_candidates() -> render_digest_block() -> mark_digest_surfaced()
```

The decision-feedback store exposes the **identical surface**
(`get_pending_candidates:546`, `render_digest_block:842`, `mark_digest_surfaced:572`,
`DIGEST_EXPIRY_THRESHOLD:125`). Closure = add a sibling section (call it 5d) in
`_ledger_push_and_email` that does the same three calls for `decision_feedback`.
No new mechanism; wire the existing one.

**End-to-end verification gate (design it as part of closure):** flag a candidate ->
it appears in the next digest with a `corr-cand-xxx` id -> the operator replies
`confirm corr-cand-xxx` -> `confirm_correction:588` persists to the durable store ->
`render_pattern_summary` surfaces it in the weekly scorecard. The closure is not
"done" until that full path is demonstrated with one real record. (This is the
"close the loop before building new" discipline; the store is a loaded gun that has
never fired.)

**Open choice — decision-feedback surfacing order:** when this closes, both calibration stores surface candidates
in the same digest. That competes for the operator's attention — which is precisely what
Move 2's arbitration layer governs. Decision: does decision-feedback surfacing ship
*before* Move 2 (and risk adding to the flood it is meant to be gated by), or does
it ship *through* the Move 2 gate? Recommend: close the loop now (it is currently
broken), but route its digest surface through Move 2's arbiter once that exists, so
it is gated from day one. Flag for the operator.

### 1B. Intent-state on the Sheet

**Design:** add an explicit **intent** field distinct from `status`.

- **Carrier (primary):** a new column `intent` appended to `ledger.HEADERS` (col M,
  index 12, after `status_changed_date`), with its own one-of-list dropdown applied
  the same way the status dropdown is (`_apply_status_dropdown:103`, which sets data
  validation on a column range). the operator sets it one-tap on the Sheets phone app, in
  the live store, next to status. This reuses an existing mechanism exactly.
- **Carrier (secondary):** a reply directive (`holding: <role>`, `deliberating: <role>`,
  `active: <role>`, `done: <role>`) so the operator can set intent from Telegram/email
  without opening the Sheet. It writes to the same Sheet column via an
  `update_intent(ws, row_idx, intent)` helper modeled on `update_status:113` (note:
  intent is NOT terminal-gated — it is the operator's own declaration, so no
  authorization gate; it journals to a `feedback/intent-write-log.jsonl` for parity).

**Proposed vocabulary (open):** a small set orthogonal to pipeline status:

| intent | meaning | effect on inference |
|---|---|---|
| *(blank)* | undeclared | fall back to existing heuristic (un-annotated roles behave exactly as today) |
| `active` | the operator is working this now | suppress "stalled/forgot" inference; a sit is not disengagement |
| `deliberating` | the operator is deciding, the sit is intentional | suppress the disengagement nudge, optionally a long soft time-box only |
| `holding` | intentionally parked | mute all nudges until the operator changes it |
| `done` | the operator has decided, not advancing | resolve: treat as a soft no without forcing a terminal status write |

This is a starting vocabulary, not a decision. the operator prunes/renames.

### How it replaces the heuristic (graceful, not a rip-out)

`compute_disengagement_flag` and `compute_distraction_flag` gain a **declared-intent
check before the heuristic fires**:

```
intent = role.intent (read from the Sheet column)
if intent in {holding, deliberating, done}:  -> declared-intentional, suppress/soften
elif intent == active:                        -> declared-active, suppress "forgot" framing
else (blank):                                 -> existing heuristic, unchanged
```

Declared intent **overrides** inference; absence **falls back** to today's behavior.
No un-annotated role changes behavior, so this is additive and safe to ship behind
nothing.

### Fail-safe direction (Move 1: do not guess intent)

When intent is **undeclared and the heuristic is at its threshold edge** (the
ambiguous zone where Alice would otherwise guess), the design biases **away from a
confident nag** and **toward eliciting the signal**: the disengagement line becomes
an *invitation to declare* rather than an assertion about the operator's state. E.g. instead
of "you have stalled on X," it becomes "X has been quiet 7d — `holding: X` to mute,
or `help with X` if blocked." This converts the nag into cheap signal-elicitation,
and it is the same fail-safe spirit as the rest of the system: when unsure about
the operator's internal state, ask cheaply rather than assert wrongly. The asymmetry:
guessing-intent-wrong costs accuracy and trust; the invitation costs one line of
digest space.

### Tunability
Intent vocabulary, and the per-intent effect on each nudge, live in config
(`feedback/digest-prefs.json`, already referenced by `focus_enforce.py:6`), not
hardcoded.

---

## MOVE 2 — Proactivity arbitration / cost-gate (BUILD on existing seeds)

### The current state this targets
- **No interruption-calculus anywhere.** `_ledger_push_and_email:612` is a **flat,
  hardcoded-order append pipeline**: each section does `if block: lines.append(block)`.
- Prioritization exists **only for roles** (fit-score sort). Most sections are
  **uncapped** (disengagement, threads, questions, experience candidates,
  behavior-pattern count). The only score-gate anywhere is the experience ambient
  detector's `min_score=0.4`, and it runs out-of-band.
- Result: Alice **floods** (seven patterns fire, the operator acts on none) because there is
  no arbiter to flood through.

### Design target: a collect -> score -> select -> render arbiter

Refactor the flat append pipeline into a four-stage layer. Each section, instead of
appending text directly, **emits one or more `SurfaceCandidate` objects**:

```
SurfaceCandidate {
  type            # focus_nudge | new_roles | thread | question | exp_candidate |
                  # corr_candidate | behavior_pattern | distraction | ...
  render()        # the text it would produce (lazy)
  info_gain       # how much this tells the operator he does not already know  [0..1]
  operator_effort     # how much action/attention it demands               [0..1]
  disruption      # interruption cost of surfacing it                  [0..1]
  confidence      # sample-size / signal-strength behind it            [0..1]
}
```

A new `arbitrate(candidates, prefs)` function:

1. **Scores** each candidate against the research's three-metric bar:
   `value = w_ig*info_gain*confidence - w_eff*operator_effort - w_dis*disruption`.
   Note `info_gain` is multiplied by `confidence` so a low-n "pattern" (statistical
   noise) cannot clear the bar on info-gain alone (see noise handling below).
2. **Filters** to candidates whose `value >= threshold + margin` (the margin is the
   fail-safe; see asymmetry).
3. **Sorts** by value and applies a **budget** (top N per digest, operator-set).
4. **Renders** only the survivors, in value order (this also replaces the current
   hardcoded section order with salience order — except the focus block, which the operator
   may choose to pin; see the open decisions).

### Fail-safe direction (Move 2: surface less when borderline)

From PACT's verified reward asymmetry, the two errors are not symmetric:
**over-surfacing (flooding) is the failure being fixed; under-surfacing (staying
quiet on a mildly-worth-raising item) is mildly suboptimal and recoverable.** So:

- a candidate must clear the threshold **by a margin** (`value >= threshold + margin`);
  ties and near-ties are **held**, not surfaced.
- the budget is a **hard cap**: cleared candidates beyond N are held to a later digest
  (or dropped if ephemeral), never crammed in.
- weights default so over-surface is penalized harder than under-surface.

This is the same fail-safe logic as the A/B disambiguation detector, applied to
proactivity: when unsure, do the recoverable thing (stay quiet).

### Noise / sample-size handling (the companion failure)

The diagnosed companion-failure is surfacing n=4 "patterns" that are noise.
`confidence` (driven by sample size and effect strength) gates this structurally:
a pattern with n below a configured floor gets `confidence` near 0, which zeroes its
`info_gain` contribution, so it cannot clear the bar regardless of how "interesting"
it looks. This connects to Alice's own honestly-identified hard spot (small-sample
signal-vs-noise) and is a structural answer to it, not an instruction to "be careful."

### Configured boundary (the operator sets it, not Alice)

All knobs live in `feedback/digest-prefs.json`:
- `budget` (default N items/digest — open, recommend 3-5),
- `threshold` and `margin`,
- the three weights `w_ig, w_eff, w_dis`,
- the per-type default metric estimates (a disengagement flag on a focus role =
  high info-gain / low effort / moderate disruption; a low-n behavior pattern =
  low info-gain; etc. — open, the seed table),
- the `confidence` sample-size floor.

The **existing threshold constants are the seeds**: `DISENGAGEMENT_THRESHOLD_DAYS`
(`focus_enforce.py:25`), the behavior_patterns `>=3/>=8/>=5` constants, the
`min_score=0.4` experience gate (generalized into the cross-section `confidence`
gate). The layer is net-new but assembled from these.

### Folds in: the weekly scorecard (Move-2-adjacent)

The weekly scorecard **has never run** (broken bash->Desktop path,
`launchctl` exit 126, zero runs), AND its CALIBRATION / OBSERVATIONS / PROPOSALS /
INTERVIEWS sections are **prompted but have no feeding data** -> a confabulation risk
if it ran. Combined fix, in order:

- **(a) Fix the path** — give the weekly the `run_daily.py` treatment. Simplest:
  retire the separate broken weekly plist and add a **Friday-gated scorecard step to
  `run_daily.py`** (which already runs under launchd-python3 with FDA). One
  orchestrator, one working path, no second TCC surface to maintain.
- **(b) Ground the un-fed sections BEFORE it runs** — for each section, either feed it
  real data (calibration = the operator-label vs Alice-fit-call agreement from the Sheet;
  observations = thread-close rate from `threads/`; proposals = accept rate from the
  now-closed decision-feedback store; interviews = from Sheet statuses) **or remove
  the section.** The scorecard must **render only sections it has data for** — the
  same "don't surface confabulation" principle as the arbiter. Making it run while it
  would confabulate is the wrong order (the un-fed sections are exactly the
  fabrication surface the grounding work fights).

**Open:** which un-fed sections get grounded now vs removed until data accrues.

---

## MOVE 3 — Answer/surface separation via context-suppression (NET-NEW separation, structural)

### The current state this targets
- Telegram path is **one LLM call, full tool surface, no code-level intent
  classification** (`_route_message_freeform:1123`). The model decides
  answer-vs-act-vs-surface in one generation.
- Answer-path and surface-path are **entangled at the prompt level**:
  `_build_alice_context` injects FOCUS LIST (`telegram_bot.py:334`), FOCUS ROLE
  DETAILS (`:380`), PIPELINE (`:372`); the router concatenates that context + history
  + "HOW TO ACT" directive (`:1343`) + the user message into one prompt (`:1403`).
- The dropped-question bug is a **redirect reflex driven by focus-context being the
  dominant signal.** And the grounding detectors are **observe-only** (flag to
  Sentry, never block/rewrite) — the failure is **upstream of every safeguard**.

### The chosen fix (decided with the operator): Option 2 — context-suppression on direct questions

When the incoming message classifies as a **direct question**, **suppress the FOCUS
LIST / FOCUS ROLE DETAILS / focus-context injection for that turn**, removing the
dominant pull-to-redirect so the question becomes the strongest remaining signal.
This is structural (the pull is a function of what is in the prompt; remove it at
construction time and the pull is gone), NOT an Alice.md instruction (a counter-
instruction would be two instructions fighting, and the stronger pull wins
stochastically — the focus-discipline instruction is *part of what causes* the
over-redirect). **Do not add an "answer first" line to Alice.md.**

### The direct-question detector (critical; mirror `_is_ab_question`, invert the fail-safe)

Build it on the **proven two-layer structure** of `_is_ab_question`
(`telegram_bot.py:1051`): a deterministic **regex floor** + a cheap **Haiku semantic
backstop** (`_is_ab_question_semantic:1010`). Same shape, **opposite fail-safe**.

`_is_ab_question` biases toward AMBIGUOUS because there a false-positive is harmless.
**Here the asymmetry is inverted:**

| detector outcome | consequence | verdict |
|---|---|---|
| **miss** (does not fire; focus-context stays) | occasional drop = **today's behavior** | no-worse-than-today |
| **false-positive** (fires; focus-context suppressed when the question NEEDED it) | Alice answers in-character but **cannot reference the pipeline data the question implied** | **NEW failure the current system cannot produce** |

So the detector biases toward **under-suppression**: when unsure whether it is a pure
direct question, **leave the focus-context in** (fail toward the harmless miss). The
regex layer fires only on clear off-domain / question-shaped inputs; the semantic
backstop, on uncertainty, returns "not a pure direct question" (the opposite default
from `_is_ab_question_semantic`'s AMBIGUOUS default). And, like the known
`_is_ab_question` coverage gap, this detector **will** miss shapes — by design it
misses **safe**.

### Structural attachment point

The suppression is a **construction-time** change, and that *is* the structural
mechanism — it does not need a write-choke-point:

- the router computes `is_direct_question = detect(user_text)` **before** building
  context;
- `_build_alice_context(suppress_focus_context=is_direct_question)` **omits** the
  FOCUS LIST / FOCUS ROLE DETAILS / PIPELINE-focus sections for that turn.

The pull is removed by not constructing it. No instruction, no second pass, no extra
call beyond the (already-bounded) semantic-backstop classification.

**Open — post-hoc enforcement, recommend deferring.** There are two
existing hard-block hooks: the `register_tool`/`assert_write_allowed` choke point
(`guards.py`) and the **unwired** `verification_preflight` (`deploy_guard.py`,
DEFINED+TESTED+UNWIRED). Neither naturally gates "answer the question" — they gate
writes/claims. A belt-and-suspenders option is to **promote the grounding detector
from observe-to-block**: when `grounding.py` flags `category_mismatch` (response
unrelated to the question) on a turn the detector marked direct-question,
re-prompt once. This adds a real preventive gate where today there is only logging.
Recommend **deferring** it to keep Option 2 simple (the chosen reason over Option 1),
but surface it as available if suppression-alone proves leaky.

**Open — suppression granularity.** All-or-nothing (drop all three focus
sections) vs partial (drop FOCUS ROLE DETAILS but keep a one-line PIPELINE summary).
Recommend all-or-nothing for a clean structural story; partial risks re-introducing
a weaker pull. Flag for the operator.

### Off-domain calibration (the deeper question, coherent with Move 1)

"What is off-domain for Alice" is the same classification problem as Move 1's
"is this an intent declaration" — both read the operator's input. See §4: design **one shared
reader**, not two. Off-domain = the direct-question class that gets focus-context-
suppressed; in-domain pipeline-action = the class that keeps it. The narrow scope
matters: **a direct question must be answered; redirect-to-job-search may follow or
accompany, never replace.** This does NOT weaken focus-discipline for ambient/drift
cases (the digest, the nudges) — those are unchanged. Only the per-turn chat context
for a detected direct question changes.

---

## 4. Cross-cutting coherence

### 4.1 One shared input-reader for Moves 1 and 3

Both moves classify the operator's incoming message. The codebase already has the seed:
`_INTENT_TOPIC_RE` (`telegram_bot.py:1080`) + `_select_relevant_observations:1090`,
a topic classifier with a **fail-safe default-to-exclusion** ("no identifiable intent
-> return nothing"). **Design: extend this one primitive into a shared
`read_operator_intent(message)` reader** that returns:

```
{ is_direct_question: bool,        # Move 3: drives focus-context suppression
  intent_declaration: str | None,  # Move 1: holding/deliberating/active/done, if present
  topics: set[str] }               # shared topic tags (already computed today)
```

Both moves consume this one reader. The off-domain detector (Move 3) and the
intent-directive parser (Move 1) are **two readers of the same classification**, not
two independent classifiers. This is the coherence requirement met with a real
existing primitive, and it keeps the fail-safe defaults consistent (default-to-
exclusion = leave-context-in = do-not-assume-intent).

### 4.2 Consistent fail-safe directions

| Move | "when unsure" direction | why it is the recoverable error |
|---|---|---|
| 1 | do not guess intent; invite the operator to declare (or use conservative heuristic) | wrong-guess costs trust; an invitation costs one line |
| 2 | surface less; hold borderline candidates | over-surface = the flooding being fixed; under-surface is recoverable |
| 3 | leave focus-context IN (under-suppress) | a miss = today's behavior; a false-positive suppression = a new failure |

Same spirit, direction set per-move by which error is recoverable. Note Move 3's is
the **inverted** one and is called out so the detector biases correctly.

### 4.3 Everything tunable (parameterized-engine principle, as in the matcher)

One config surface, `feedback/digest-prefs.json`: Move 1's intent vocabulary +
per-intent nudge effects; Move 2's budget/threshold/weights/metric-table/confidence-
floor; Move 3's off-domain regex scope + suppression granularity + the
defer/enable flag for post-hoc enforcement. No hardcoded thresholds; the operator adjusts.

---

## 5. Recommended build sequence (after the operator approves this design)

Build **move-by-move**, each verified before the next, never three-on-an-unreviewed-design.

1. **Move 1A — decision-feedback loop closure.** Lowest risk, half-built, unambiguous
   (mirror an already-working block). Demonstrate the end-to-end capture->surface->
   confirm->persist path with one real record. This also de-risks Move 1B and feeds
   Move 2's proposals-section grounding.
2. **Move 1B — intent field + directive + heuristic-override.** Additive (un-annotated
   roles unchanged); safe to ship incrementally.
3. **Move 3 — direct-question detector + context-suppression.** Self-contained;
   benefits from the §4.1 shared reader, which 1B starts.
4. **Move 2 — arbiter + weekly-scorecard combined fix.** Largest refactor; lands last
   so the decision-feedback surface (1A) and any new nudges (1B) are gated from the
   start rather than retrofitted.

---

## 6. Open choices for the operator (consolidated — decide before build)

- Decision-feedback surface: ship before Move 2, or route through the Move 2 gate from day one? (Recommend: close now, gate once arbiter exists.)
- Intent vocabulary: the proposed `active/deliberating/holding/done` set — prune, rename, or extend?
- Intent carrier: new Sheet column + directive (recommended both), or directive-only, or column-only?
- Undeclared-ambiguous nudge: invitation-to-declare (recommended) vs conservative-heuristic-silent.
- Digest budget N (recommend 3-5).
- The per-type metric seed table (info_gain/effort/disruption defaults).
- Weekly scorecard: which un-fed sections to ground now vs remove-until-data.
- Section order: pure salience order, or pin the focus block at top regardless?
- Post-hoc enforcement (promote grounding detector to block): defer (recommended) or include?
- Suppression granularity: all-or-nothing (recommended) vs partial.
- Config home: `feedback/digest-prefs.json` (recommended) — confirm.

---

*Design only. Build nothing until the operator reviews and authorizes move-by-move.*
