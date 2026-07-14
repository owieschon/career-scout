# Fit Strategy Spine — single source of truth

<!-- clean-docs:purpose -->
This is the authoritative strategy the **whole pipeline** (sourcing → matching → prep) must align to. Engine code reads it via `fit_model.toml`; it is NOT re-derived per session. Update this doc when the strategy changes.
<!-- clean-docs:end purpose -->


## 0. The position
Jordan's edge is one scarce thing: **an AI-native operator who turns capability into
revenue inside industrial environments** — senior commercial/CS judgment × real
advanced-manufacturing domain × the ability to evaluate and ship AI, held by one
person. Building is the *unlock* (lets them operate in the new regime), NOT the
*edge*.

## 1. Positioning North Stars (govern framing AND fit)
1. **Position the intersection as ONE capability, never a stack of parts.** Three
   half-credentials lose to specialists on each axis; one fused capability has no
   specialist to lose to.
2. **Legibility is the game; demonstration wins it.** Proof (shipped work) over
   claims. Combination value sounds like hedging unless shown.
3. **Pick the door before the message.** Channel determines more variance than
   wording. Warm/operator/founder channels read the intersection; cold ATS funnels
   screen it out as noise.
4. **Speak in value created, not tools used.** Lead with business outcome; tools
   are the credibility for the outcome, never the headline.
5. **Stay on the complement side.** Deflating = artifact-production the model now
   does. Holding = judgment/trust/domain/translation. Position relentlessly on #2.

## 2. Two axes — desirability × attainability (the counterbalance)
The fatal flaw of a preference-only model: it answers "does the operator *want* this?"
and nothing else — a closed loop with no vote from reality. Every role carries TWO axes:

- **Desirability** (the operator's preference) — the four dims in §3.
- **Attainability** (external reality) — will it actually happen / will the world
  read them. Grounded, in order of how *truly external*:
  1. **Outcome feedback** (truest, ground truth) — real response/screen/interview/
     offer, fed back through the outcome-annotation flywheel. The dimensional
     taxonomy is its prerequisite.
  2. **Employer-bar / mutual-fit** (concrete now, v2) — JD hard must-haves vs the
     operator's profile; their bar, not the operator's taste.
  3. **`company_archetype`** (concrete now, v1) — stage/channel-accessibility/
     legibility. See §4.

**inform-not-gate:** attainability re-ranks, annotates, and routes the channel. It
NEVER silently cuts — a naive odds-gate would kill the illegible-intersection roles
the strategy says to pursue via the right channel (proven by the AE seed below).

## 3. Desirability dimensions (v1 — config vocab)
- `domain`: `on_thesis | adjacent | off_thesis` — industrial/mfg/hardware/CAD-PLM/
  robotics/additive/B2B/AI = on; data-infra/observability/fintech/HR-tech = off
  (per CLAUDE.md). The judge cannot be trusted here (it FIT'd fintech/data-infra).
- `role_archetype`: `intersection | bridge | commercial | pure_build | leadership |
  analyst`. Encodes "building is the unlock not the edge" — `pure_build` is the
  exposed/substitute lane → caps to REACH even on-domain.
- `seniority_fit`: `target | too_senior | too_junior | founder_adjacent`. Senior-IC
  → first-line-mgr is target; VP/Head/Dir-10yr = too_senior → REACH; founding/first-
  commercial-hire at seed/Series-A = founder_adjacent (keep).
- `blockers[]` (multi): `travel | location | comp_high | comp_low | anti_fit |
  nonrole | competitor`. The *why* of every REACH/cut, separable + countable.
  NOTE: `anti_fit` over-fires (see §6) — tighten its definition.

**Derived band** = f(dims) via deterministic caps (post-judge, GUARD-1-safe):
- `pure_build` (no intersection signal) + FIT → REACH
- `too_senior` / non-commercial PM + FIT → REACH
- `off_thesis` domain + FIT → REACH (or cut if they screen)
- hard gate in blockers (travel/location/comp) → NOT-FIT
- `anti_fit` / `nonrole` → NOT-FIT
- else → judge's verdict stands

## 4. company_archetype (v1 attainability dim)
`early_founder_led | growth | enterprise`. Extractable from YC/VC lists, funding
stage, headcount, founder-led signals (data partly exists: `yc_boards.json`,
`vc_boards.json`, `(VC)`/`(YC)` tags).
- The winning quadrant is an **intersection**: `on_thesis × early_founder_led ×
  (commercial|bridge)` — desirability + attainability + right-channel all align.
- `on_thesis × early × pure_build` = still exposed lane (stage doesn't rescue role).
- `on_thesis × enterprise` = high desirability, LOW channel-accessibility (intersection
  illegible to their funnel — the large-frontier-lab pattern).
- **Routing behavior:** archetype picks the door. `early_founder_led` → recommend
  cold-email the VP/founder directly (the move that worked in the AE seed below),
  not the portal.

## 5. Outcome loop — decompose, never binarize
"No offer" ≠ "bad fit." Log the funnel + a terminal reason:
`applied → response → screen → interview → offer`, terminal reason ∈
`{fit_reject, performance, comp, withdrew, ghost}`. An intermediate positive
(interview secured) is a label in its own right. Attribute the terminal "no" to the
right subsystem — interview-performance is NOT a fit/sourcing signal.

## 6. Ground-truth anchors (n is tiny — anchor + refute, do not overfit)
- **Hardware-startup AE seed:** judge said NOT-FIT/anti_fit. Reality:
  cold email → VP of Sales → interview in 48h; qualified; lost on interview
  performance, not fit.
  Metadata: `{domain: on_thesis, company: yc_startup(early_founder_led),
  channel: cold_email_to_VP, reached: interview, terminal: no_offer(performance)}`.
  → Falsifies the anti_fit call; validates the channel North Star + inform-not-gate +
  company_archetype. **Held as a regression: the tightened judge must NOT call this
  AE seed anti_fit.**

## 7. Sourcing alignment
- **Deep-fetch is the good path** (full JD body via ATS APIs → the judge reads real
  text). Bodyless ledger rows came from the role_scan-shortlist (by title, no fetch)
  + aggregators (excerpts), which bypass it.
- **Extend deep-fetch** beyond Greenhouse/Ashby/Lever to **Workday-CXS + BuiltIn**
  (proven fetchable) so on-domain custom-ATS industrial companies get full bodies.
- Add proven on-domain companies to the `source_deep` BOARDS list.

## 8. Build sequence
1. **Matcher (now):** dimensional taxonomy (config vocab + deterministic dim layer +
   derived-band caps) → GUARD-1 re-validate → re-run the 83. + `company_archetype` +
   channel routing. + deep-fetch extension. + record the AE seed outcome-label.
2. **Prep (next phase):** reference materials (resume masters value-led + intersection-
   fused + banned-framing-clean; positioning docs as the spine) AND rules (prep
   generates `company_archetype`-aware materials + channel-routed outreach; verify
   gates enforce strategy, not just grounding). Prep CONSUMES the matcher's dims —
   that's why matching ships first.
3. **Outcome loop:** wire real outcomes back, keyed by the dimensional labels.
