# Sourcing-Matcher Redesign — Architecture

<!-- clean-docs:purpose -->
**Status:** DESIGN ONLY. Building zero lines of code until the operator has reviewed and decided every flagged open decision below. The learning layer especially is poisonous to build wrong — wrong-shape training data accumulates silently and is expensive to unwind. Read this page before changing or relying on Sourcing-Matcher Redesign — Architecture so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->
<!-- clean-docs:allow doc-length reason="The Sourcing-Matcher Redesign — Architecture reader path stays in one file because splitting it would separate its operating context from its verification material" -->


**Scope:** the sourcing/fit-scoring core (`source_*.py` + `score_job.py`). Replaces the current keyword/regex matcher with a parameterized, learnable, model-judged fit engine. Composes — does not duplicate — `experience_store.py`, `targets/companies/<slug>.md` (company research), and `prep_pipeline.py` (the existing 4-stage gated GROUND→WRITE→VERIFY→ASSEMBLE pattern).

**Compass:** match on **what the role actually is and what the company actually does**, against **what the operator's portfolio actually covers (combinatorically, including the tails)**, scored with **decomposed, model-judged, interpretable** per-dimension reasoning that persists for both human tuning and any future learned re-ranker. Not blob-embedding similarity (uninterpretable; measures textual closeness, not fit). Not title-keyword matching (the root bug).

---

## 1. Diagnosis recap (what this fixes)

Three root causes of "few/poor-fit leads," from the conversation:

1. **Universe is too small and can't expand.** `source_multi.py:REGISTRY` is a hardcoded list of ATS slugs (Greenhouse/Ashby/Lever). Wrong slugs yield nothing. No web reach. the operator's universe is bounded by what was hardcoded months ago.
2. **Independent-substring scoring with no role-domain coherence.** `score_job.py:score_listing` matches `TRACK_KEYWORDS` and `AI_NATIVE_COMPANIES` as independent regex hits. Two unrelated keyword matches sum to a score. Nothing checks whether the role's *actual function* coheres with the *company's actual domain*. A "Senior Account Executive" at a fintech and at a manufacturing-IoT company both pass the AE track filter; their fit-to-the operator is wildly different.
3. **No real fit signal — only a 33-name list.** `AI_NATIVE_COMPANIES`/`FRONTIER_AI_COMPANIES` are membership lookups. No model of *why* a company is a fit for the operator's portfolio. The combinatoric story (industrial + hardware + commercial + ML across the operator's prior roles) is invisible to the scorer.

The dispatch's reframe of all three: **title/keyword matching is the wrong unit entirely.** Match on what the role and company actually are, scored against a rich fit model, decomposed and interpretable. Already-built infrastructure (company research, experience store) is the substrate this composes.

---

## 2. The architectural principle: engine vs config

**The matcher is a GENERAL engine. the operator's fit model is the first CONFIG it's instantiated with. They are separated, structurally, from day one.**

Why this matters in two directions:

- **For the operator, today:** "tunable" means the fit model lives in a config the operator can edit (parameters, their types, their values, their tradeoffs) without touching engine code. If the fit model is hardcoded into the engine, it isn't tunable in any meaningful sense — every adjustment becomes a code change, every code change risks regression, and the human-tuning loop (Layer 3 of the learning architecture below) is structurally undermined.
- **For the product/open-source option, later:** the engine is general so other users (whose fit models differ from the operator's) can load their own config. The cost of preserving that today is zero — keep them separate from the start. The cost of separating them later is high.

**The split, concretely:**

- **Engine** (`src/alice/pipeline/matcher_engine.py`, when authorized): a parameterized scorer that takes a fit-model config + a listing (role + company-research + the operator's experience store) and returns a per-dimension score + per-dimension reasoning. Knows about parameter types, tradeoffs, the decomposition discipline. Knows nothing about the operator specifically.
- **Config** (`config/fit_model.yaml` or `.toml`, when authorized): the dimensions, parameter-types, weights, gates, selects that describe the operator's fit model. Edited by hand for the human-tuning loop. Versioned in git.

**Open decision 1 [operator to decide]:** YAML vs TOML vs JSON for the config format. YAML wins on readability but is parser-fragile; TOML is type-safer but verbose; JSON is what `feedback/*.json` already uses. Default to TOML for type-safety on a config that gates real spend; defer.

---

## 3. the operator's fit model — the first config

Codified from the dispatch's "the operator's fit model" section. **The engine must accept this as a config, not embed it.**

### 3.1 Fit dimensions

- **Combinatoric + conventional fit (BOTH)** — surface strong conventional fits AND the unusual intersection/easter-egg roles. The robotics market-development $168K role would have been killed by a precision-first conventional-fit filter; the matcher must NOT miss those.
- **Portfolio-of-worlds matching by adjacency-and-combination, NOT overlap** — the operator's portfolio is non-enumerable (education/teaching, design-prototyping-manufacturing pedagogy, industrial/manufacturing commercial, additive/CNC/3D-spatial hardware, makerspace ops + hardware procurement, technical account management / sales engineering, RevOps/systems/data, self-taught full-stack + ML, buyer-side empathy across verticals, sold technical hardware/software into aerospace/defense/automotive/medical/electronics and more, global supply-chain navigation, ERP-CRM-CLI fluency, owned a startup marketing function, multi-language). The matcher judges whether the portfolio *covers* the role's required intersection — allowing **adjacency** (close-enough world counts; e.g., robotics-via-CNC/additive) and **combination** (portfolio as a whole covers a gap even when no single world is the "main" one). NOT "has the operator done this exact thing" — that's overlap-matching, and undersells combinatoric candidates.
- **Functional fit, gradient not gate:** **(a)** commercial-led, technically fluent (owns customer/revenue relationship; technical depth is the edge) is top fit; **(c)** genuine bridge, hired *because* he does both (and often pays more) is co-equal top priority; **(b)** technical-led, hands-on building/systems is workable but lower. Anti-fits (pure marketing, pure-IC-sales-no-technical-depth, pure-engineering-no-customer) score low. Rank by the gradient; exclude only the anti-fits.
- **Value axis, traded against fit:** comp/seniority/upside matter and trade against fit (a c-role ties (a) partly *because* it pays more). Comp floor is an example target band (e.g. $150k–$190k base), **soft** — the operator will go lower for great fit. Comp is a value that modulates a fit-vs-comp tradeoff, NOT a hard gate.

### 3.2 Parameter-type framework — THE architectural principle

**"Tunable" does NOT mean "everything is a slidable weight." Each parameter is its true type — or composition of types — that matches its actual behavior. None gets forced into the wrong type for uniformity.**

The five types, mapped:

| Type | Behavior | Examples in the operator's fit model | Wrong-type-cost |
|---|---|---|---|
| **Binary gate** | Hard on/off, non-overridable, applied FIRST before any scoring. A perfect-fit role that fails the gate is unviable; no score "rescues" it. | **Location viability** — remote-first preference (remote, or the operator's home metro). | Treating it as a weight = surfacing roles the operator cannot take. |
| **Multi-select** | Choose several, non-exclusive. Multiple are valid simultaneously. | **Seniority** — mid-senior IC AND low-level-manager AND "founding ___" (high-performing IC who defines a function/dept as a startup scales — its own distinct box, not a sub-case of either). | Forcing into single-select = missing the founding-role band. Forcing into weight = no semantic concept of "this is a senior-IC role" vs "this is a manager role." |
| **Weighted/continuous** | Genuine matters-of-degree that trade against each other. The model accumulates scores and tradeoffs happen on the gradient. | **Fit dimensions** — functional gradient (a≈c>b), combinatoric-vs-conventional balance, domain/portfolio adjacency-coverage, the master **fit-vs-value** tradeoff. | Forcing into gates = killing combinatoric tails (which the dispatch explicitly forbids). |
| **Fill-in-the-blank** | Literal value used as-is. Not scored, not weighted — just consumed by an engine step that needs the value. | Location radius / suburb list. The time-box date for when the travel gate relaxes. | Forcing into weights = arbitrary numerification. |
| **Composite** | A value that modulates a weight. The value sets a threshold; the weight handles the gradient around it. | **Comp floor (example band $150k–$190k base, soft)** — the value sets the threshold; the fit-vs-comp tradeoff is the weight that lets great fit "buy down" below the floor. | Forcing into a single type either makes it a hard kill (loses great fits below) or a pure weight (loses the threshold semantics). |

**Composites are not a fallback — they are first-class.** Real fit decisions routinely involve "a value that modulates a weight." Designing the framework to accommodate them honestly avoids the common ML-rubric trap of pretending everything is a weight when it isn't.

**Open decision 2 [operator to decide]:** are there parameters in the operator's fit model that I've under-typed above? E.g., is `domain-adjacency` really a weighted/continuous dimension, or is it a multi-select over a small ontology of "world labels" (industrial, hardware, AI-native, etc.) that the model scores against? My current placement is weighted/continuous; the multi-select-of-worlds shape would be sharper if the world ontology is enumerable and small.

### 3.3 The fit-model config (concrete shape, for reference)
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

Sketch only — exact format pending decision 1:

```toml
## Binary gates — applied first
[gates.location]
type = "binary_gate"
remote_ok = true
remote_first = true
non_remote_locations = ["Columbus, OH", "Franklin County, OH"]

## Multi-select — choose several, all valid
[selects.seniority]
type = "multi_select"
selected = ["mid_senior_ic", "first_line_manager", "founding_role"]

## the operator's portfolio worlds — each world is a TRIPLE, not a bare string.
## Adjacency is judged against the `definition`, never the `label`.
## Structural enforcement: the engine's prompt-builder passes the definition
## (not the label) to the model. The label is for human readability and
## logging only. See §4.1 below for the prompt-template mechanism that makes
## label-keyword-matching impossible.
[[selects.domain_worlds]]
label       = "industrial_manufacturing"
definition  = "Companies whose product or operations center on producing physical goods at scale in factories — durable goods, components, materials, industrial equipment, manufactured consumables. Sells to or operates within F500-scale manufacturing operations."
anti_examples = [
  "Pure SaaS with no industrial vertical",
  "Consumer marketplaces",
  "Fintech / payments",
]

[[selects.domain_worlds]]
label       = "additive_cnc_hardware"
definition  = "Companies whose product or expertise center on 3D printing / additive manufacturing, CNC machining, or other digitally-controlled spatial-hardware fabrication. Includes machine OEMs, materials, software for these processes, and adjacent precision-fabrication tooling."
anti_examples = [
  "General industrial software with no AM/CNC tie",
  "Consumer 3D-printed novelty goods",
]

[[selects.domain_worlds]]
label       = "technical_account_mgmt"
definition  = "Roles or company functions where the work is owning a technical customer relationship — running QBRs, leading technical deployment, translating between customer engineering and vendor product. Sales-engineering, solutions-architect, and customer-success-engineer roles fit; pure-IC-sales without technical depth does not."
anti_examples = [
  "Pure transactional sales (SDR/BDR)",
  "Customer-support tier-1 ticket handling",
]

[[selects.domain_worlds]]
label       = "revops_systems_data"
definition  = "Roles or company functions building the revenue-operations infrastructure: CRM administration, sales-data pipelines, forecasting models, GTM analytics, revenue attribution. The work is structuring how revenue gets predicted, measured, and operated on."
anti_examples = [
  "Pure finance / accounting roles",
  "Marketing analytics with no revenue-attribution scope",
]

## ... non-exhaustive; this is the seed. Add the remaining 4-10 critical
## worlds in the same triple shape (label / definition / anti_examples).

## Weighted — gradient dimensions
[weights.functional_gradient]
type = "weighted"
## (a) commercial-led-tech-fluent, (c) bridge, (b) tech-led-customer-facing
buckets = { a = 1.0, c = 1.0, b = 0.6 }
anti_fits = ["pure_marketing", "pure_ic_sales_no_tech", "pure_eng_no_customer"]

[weights.combinatoric_emphasis]
type = "weighted"
## 0.0 = pure conventional; 1.0 = chase only easter eggs. Default high enough
## to protect tails per the dispatch.
value = 0.7

[weights.adjacency_coverage]
type = "weighted"
## How generously the matcher accepts world-adjacency. Higher = more tolerant.
value = 0.6

[weights.fit_vs_value_tradeoff]
type = "weighted"
## The MASTER tradeoff. How much fit "buys" against comp/seniority/upside.
fit_weight = 0.65
value_weight = 0.35

## Fill-in-the-blank — literal values
[values.location]
type = "fill_in"
radius_miles = 50
center = "Columbus, OH"

## Composite — value modulates a weight
## NOTE: example illustrative values, not anyone's real preferences.
[composite.comp_floor]
type = "composite"
threshold_usd = 150000        # example target band: $150k–$190k base
## fit_vs_value_tradeoff above is the weight; this composite is the value
## that parameterizes "at what comp does the tradeoff start kicking in"
soft_below_threshold = true   # great fit can buy down below
```

**Open decision 3 [operator to decide]:** the domain_worlds ontology. Is the seed list above the right shape and granularity? Should worlds be hierarchical (e.g., `additive_cnc_hardware > metal_am`)? Should they have weights per world or stay equal? The dispatch's "non-enumerable" framing suggests don't try to fully enumerate — but the matcher needs SOMETHING to score against. Pragmatic answer probably: enumerate the 8-15 most most important worlds, allow free-text "other" tags the model can match against more loosely.

---

## 4. The decomposed model-judged matcher

The engine evaluates a listing in **three decomposed dimensions**, judged by an LLM grounded in real evidence, with per-dimension persisted scores + reasoning. This is the structural answer to root cause 2 (independent substring matching) and root cause 3 (no real fit signal). It composes the existing infrastructure rather than duplicating it.

### 4.1 Three dimensions
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

For each listing, the engine produces three judged sub-scores, each with reasoning:

**Dimension A — Functional fit.**
Input: JD body. Question to the model: *What is this role's actual function (commercial-led / bridge / tech-led / anti-fit)? Cite the JD phrases.* Output: bucket assignment + reasoning + confidence. Scored against the functional_gradient weight.

**Dimension B — Company-domain fit.**
Input: `targets/companies/<slug>.md` (existing company-research format with CONFIRMED/UNCONFIRMED citations). Question to the model: *What is this company actually selling, to whom, in what domain? Which of the operator's portfolio worlds (from config) does that overlap or adjoin? Cite specific company-research lines for each world match.* Output: list of worlds matched (multi-select) with strength per match + reasoning. Scored against the adjacency_coverage weight.

**Structural enforcement that adjacency is graded model-judgment, NOT keyword overlap (this is the whole-ballgame fix):**

The model never receives the bare world `label`. It receives the `definition` (and `anti_examples`). Label-keyword-matching is impossible because the matching token (the label) is never in the prompt the model sees.

```python
## src/alice/pipeline/matcher_engine.py — Dimension B prompt builder
def _build_dimension_B_prompt(company_research: str,
                               domain_worlds: list[dict]) -> str:
    """Construct the Dimension B prompt. Each world is rendered using its
    DEFINITION and ANTI_EXAMPLES — the label is NEVER included in the
    prompt. The model judges semantic adjacency against the definition,
    not surface-token presence against the label."""
    world_blocks = []
    for w in domain_worlds:
        # NOTE: w['label'] is NOT included in the rendered block.
        # The label is used only as an internal handle for the
        # returned strength score; the model judges against the
        # definition + anti_examples it sees here.
        anti = "\n".join(f"    - NOT this world: {ex}" for ex in w["anti_examples"])
        world_blocks.append(
            f"  World {w['_idx']}:\n"
            f"    Definition: {w['definition']}\n"
            f"{anti}"
        )
    worlds_text = "\n\n".join(world_blocks)
    return (
        "Read the company research below. For each numbered world, judge "
        "how strongly this company's actual business overlaps or adjoins "
        "the world as DEFINED. Cite specific lines from the company "
        "research to support each strength score. Adjacency counts — a "
        "company doesn't have to BE the world to score; it can be a "
        "close-enough adjacent.\n\n"
        f"COMPANY RESEARCH:\n{company_research}\n\n"
        f"WORLDS (numbered):\n{worlds_text}\n\n"
        "For each world, return: strength (0.0-1.0), reasoning, cited "
        "company-research line(s). Return strength=0.0 if the world is "
        "clearly absent or matches an anti-example."
    )

## The model's response is parsed by index ("World 1: strength=0.85, ...").
## The dimension B output mapper then attaches the internal label back:
def _parse_dimension_B_response(response: str,
                                  domain_worlds: list[dict]) -> list[dict]:
    """Parse the model's per-world judgments. Reattaches the label for
    downstream code use; the label was never in the prompt."""
    # ... parse response by index, attach domain_worlds[idx]['label'] ...
```

**Verification (build-time tests):**
1. Inspect the rendered Dimension B prompt for any test config. Assert that no world's `label` string appears anywhere in the prompt body.
2. Construct a world with `label="rocket_telemetry"` and `definition="<actual semantic description of telemetry-from-rockets>"`. Render the prompt. Assert that `"rocket_telemetry"` (the label) does not appear; the definition does.
3. Round-trip: synthesize company research that semantically describes industrial manufacturing without using the words "industrial" or "manufacturing"; pass through Dimension B; assert the `industrial_manufacturing` world scores high (the model judged from semantics, not surface tokens).

**Precedent in codebase:** `experience_store.flag_experience_candidate` makes paraphrase impossible by requiring verbatim spans — the failure mode (paraphrased / fabricated claims) is structurally blocked at the API. Same shape here: label-keyword-matching (the failure mode that would reintroduce the substring bug) is structurally blocked by not putting the label in the prompt.

**Dimension C — Portfolio-combination fit.**
Input: the operator's experience store (existing `experience_store.py:get_all_entries()`) + the worlds matched in Dimension B. Question to the model: *Given the world-coverage from B, does the operator's portfolio combinatorially cover the intersection this role requires? Identify the strongest single-world overlap AND the strongest multi-world combination. Cite specific experience-store entries.* Output: overlap-strength + combination-strength + reasoning + identified easter-egg signal if any. Scored against the combinatoric_emphasis weight.

The three sub-scores compose into a final fit score via the fit_vs_value_tradeoff and the binary gates. **The composition is deterministic code, not LLM-decided.** Meta-reasoning over its own scoring is exactly the failure mode tonight's dispatch warns about (and the codebase has Phase 1 Task 1.4 precedent — model selection is heuristic, not LLM-chosen, for the same reason).

### 4.2 Why model-judged, not regex

The current `score_job.py` uses regex over keywords. That fails because:
- Same role appears under many titles (a TAM, a Solutions Engineer, a Field CTO, a Customer Success Architect can all be the same job).
- Same title means different roles at different companies (a "Senior Account Executive" at an industrial distributor is operationally a completely different job than at an AI lab).
- Independent substring matches don't compose into a fit judgment; two unrelated keyword hits just sum.

A model judging each dimension separately against grounded evidence can read past title noise and produce a coherent per-dimension verdict, because the question being asked of it is small and bounded ("what function is this," "what domain is this") — exactly the prep_pipeline.py decomposition pattern that already works.

### 4.3 Why decomposed, not blob-similarity

Blob-embedding similarity (embed the JD, embed the operator's resume, take cosine) was discussed and rejected for two reasons:
- **Uninterpretable.** A single number with no reasoning. the operator can't tune it; the human-tuning loop has nothing to look at.
- **Wrong objective.** Cosine measures *textual* similarity, not *fit*. A JD that reads like the operator's resume isn't necessarily a good role for him; a JD that reads nothing like it might be a perfect combinatoric easter egg.

Decomposition into A/B/C with reasoning is the alternative. Each piece is small enough for an LLM to handle without confabulation, and each piece persists its evidence for both human review and future learning.

### 4.4 Per-dimension persistence (the core primitive, made concrete)
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

Every surfaced lead persists, alongside the listing, this structure (sketch — exact schema TBD):

```json
{
  "listing_id": "...",
  "scored_at": "2026-05-29T...",
  "fit_model_config_version": "jordan-v1",
  "engine_version": "matcher-v1.0",

  "gates": {
    "location": {"passed": true, "reasoning": "Listing is remote-US."},
    "anti_fit": {"passed": true, "reasoning": "Functional bucket=a, not anti-fit."}
  },

  "dimension_A_functional": {
    "bucket": "a",
    "confidence": 0.82,
    "score": 0.85,
    "reasoning": "JD opens 'You'll own customer success for our F500 manufacturing accounts...' — commercial-led with technical depth as the edge. Cited phrases: [...]"
  },

  "dimension_B_company_domain": {
    "worlds_matched": [
      {"world": "industrial_manufacturing", "strength": 0.9, "evidence": "targets/companies/northwind.md:PRODUCT — 'Industrial AI platform for F500 manufacturers'"},
      {"world": "additive_cnc_hardware",    "strength": 0.3, "evidence": "JD mentions IoT sensors — adjacent to spatial-hardware experience."}
    ],
    "score": 0.78,
    "reasoning": "Strong match to industrial_manufacturing; weak-adjacent to additive_cnc_hardware via IoT-sensor surface."
  },

  "dimension_C_portfolio_combination": {
    "single_world_strongest": {"world": "industrial_manufacturing", "experience_entries": ["entry-id-1", "entry-id-2"], "strength": 0.95},
    "multi_world_combination": {"worlds": ["industrial_manufacturing", "technical_account_mgmt", "global_supply_chain"], "strength": 0.88, "reasoning": "Combination covers F500-named-account IoT-platform sales motion."},
    "easter_egg_signal": false,
    "score": 0.91,
    "reasoning": "Strong direct overlap from prior additive-manufacturing aerospace-OEM work + global supply-chain experience. Combination covers the role's intersection."
  },

  "composition": {
    "fit_score": 0.85,
    "value_score": 0.72,
    "final_score": 0.81,
    "tradeoff_applied": "fit_weight=0.65, value_weight=0.35"
  },

  "surfaced_to_digest": true,
  "digest_date": "2026-05-29"
}
```

This shape is the substrate for the human-tuning loop (the operator reads the per-dimension reasoning and tunes config parameters), the cuts data (a cut + this score-record = a complete labeled-data row), and any future learned re-ranker (per-dimension features are the training input).

**Storage location TBD:** `feedback/fit-scoring-log.jsonl` is the natural place (append-only, mirrors `time-cost-log.jsonl` shape).

---

## 5. Recall-first, start-wide-then-peel

**Tune the initial pass toward recall, not precision.** The dispatch's rationale: the operator's combinatoric easter-eggs look low-fit on conventional axes; a precision-first filter would kill the robotics role. The universe is already too small; err toward surfacing more.

Concretely:
- **Gates start permissive** within their hard-rule bounds. Location gate stays strict (it's structural). Anti-fit gate excludes only the clear anti-fits.
- **Selects start broad.** Seniority multi-select keeps the founding-role box checked even when the listing doesn't say "founding" — the matcher infers from team-size + responsibility scope.
- **Weights start generous toward easter eggs.** combinatoric_emphasis starts higher than feels comfortable. adjacency_coverage starts higher.
- **The surfacing threshold is low.** Tier 1 / Tier 2 / Tier 3 cuts (current `score_job.py` returns these) start with low cutoffs.

Then the peeling-back (the labeled-cuts loop in §6) teaches where to tighten — driven by the reasons, not by total-cut volume.

**Anti-pattern explicitly forbidden:** don't pre-narrow the matcher based on intuition before any reason-labeled cut data exists. That bakes in the exact undersampling-of-tails problem the dispatch warns against.

---

## 6. The core primitive: reason-labeled cuts + per-dimension persisted scores

**Every cut MUST be logged with a reason from a fixed small set, AND every surfaced lead persists its per-dimension sub-scores + reasoning (§4.4).** Without both halves, the human-tuning loop (Layer 3 below) can't see what to tune, and any future learned model (Layer 4) trains on garbage.

### 6.1 The fixed reason set
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

The minimal set that makes the human-tuning loop work AND keeps Trap 1 (rejection ≠ clean negative) defanged:

| Reason code | Meaning | Feeds fit-learning? |
|---|---|---|
| `bad_functional_fit` | Wrong functional bucket (e.g., role is pure IC sales with no technical depth, even though scored as bridge) | **YES** |
| `wrong_domain` | Company domain doesn't actually overlap the operator's portfolio (e.g., scored as fit on a regex but it's actually fintech/payments) | **YES** |
| `comp_too_low` | Below soft floor with insufficient fit to buy down | **YES** (for tuning the composite) |
| `viability_or_circumstance` | Location or other role-eligibility constraint — anything that's not about fit per se | **NO — never teaches fit model** |
| `not_interested_other` | The operator just isn't interested for an idiosyncratic reason. Free-text field for what. | **NO — kept as audit; never teaches fit model** |

**This is the defense against Trap 1.** A reason-less rejection is a corrupt label. The fit-learning pipelines (Layer 3 + Layer 4) consume ONLY cuts with reasons in the YES rows. Viability cuts are kept for audit but never inform fit.

**Structural enforcement (mechanism, not policy — closed at BOTH ends):**

```python
## src/alice/pipeline/lead_cuts.py — the writer-side gate
FROZEN_REASON_SET = frozenset({
    "bad_functional_fit",
    "wrong_domain",
    "wrong_seniority",          # per decision 4
    "comp_too_low",
    "viability_or_circumstance",
    "prefers_other_lead",        # per decision 4 — circumstance, not fit
    "not_interested_other",
})
FIT_LEARNING_REASONS = frozenset({          # subset that trains fit
    "bad_functional_fit",
    "wrong_domain",
    "wrong_seniority",
    "comp_too_low",
})
## FIT_LEARNING_REASONS is a module-level frozenset, not a config value.
## Adding/removing entries requires a code change + review, which is the
## structural guarantee against "someone configures viability cuts to feed
## fit-learning by accident."
# <!-- clean-docs:allow section-length reason="This code excerpt keeps the frozen reason registry beside its only writer so reviewers can verify the training boundary in one block" -->

def write_cut(listing_id: str, reason: str, free_text: str | None = None,
              score_record_id: str | None = None) -> None:
    """Append one cut record to feedback/lead-cuts.jsonl. Raises ValueError
    if reason is missing or not in FROZEN_REASON_SET. There is no path that
    bypasses this — all cut surfaces (chat handler, digest, sheet trigger)
    funnel through this single writer. Anchor: same shape as
    experience_store.flag_experience_candidate, which raises ValueError on
    non-verbatim spans — paraphrase is impossible at the API level."""
    if not reason or reason not in FROZEN_REASON_SET:
        raise ValueError(
            f"write_cut: reason must be one of {sorted(FROZEN_REASON_SET)}; "
            f"got {reason!r}. Reason-less cuts are forbidden by the Trap-1 "
            f"guard — every cut is a labeled training datum, not optional."
        )
    # ... append to lead-cuts.jsonl ...


def read_fit_cuts() -> list[dict]:
    """Reader used by Layer 3 aggregation and Layer 4 training. Returns
    ONLY cuts whose reason is in FIT_LEARNING_REASONS. Viability and
    circumstance cuts are structurally invisible to fit-learning — even
    if mislabeled at write time, they cannot reach the fit-learner because
    this reader is the only path in and it filters at the source.
    """
    cuts = _read_jsonl(_LEAD_CUTS_PATH)
    return [c for c in cuts if c.get("reason") in FIT_LEARNING_REASONS]
```

**Verification that proves the guarantee structural (build-time tests):**
1. Attempt `write_cut(listing_id="x", reason=None)` → asserts `ValueError`.
2. Attempt `write_cut(listing_id="x", reason="not_a_real_code")` → asserts `ValueError`.
3. Write 3 cuts with `viability_or_circumstance` + 2 cuts with `bad_functional_fit`. `read_fit_cuts()` returns exactly the 2 fit cuts. The viability cuts cannot reach the fit learner because the reader is the only path.

**Precedent in codebase:** `experience_store.flag_experience_candidate` raises on non-verbatim spans; `tools.register_tool` raises at import if a mutating tool has no guard. Same pattern: failure mode is impossible at the mechanism level, not policed at runtime.

**Open decision 4 [operator to decide]:** is this reason set complete? Two candidates I'm uncertain about:
- `prefers_other_lead` — the operator cut this because he picked a better one to focus on. Not a fit problem; an attention-allocation problem. Should it teach fit-learning? My read: NO, but flag.
- `wrong_seniority` — separate from `bad_functional_fit`? E.g., role is too junior or too senior even though functional bucket matches. Probably its own code; the seniority multi-select is a distinct parameter.

### 6.2 Where this lives

- **Cut events:** `feedback/lead-cuts.jsonl` (new, append-only). Schema: `{ts, listing_id, reason, free_text?, score_record_id, cut_by: "jordan"}`.
- **Surfaced leads + their per-dim scores:** `feedback/fit-scoring-log.jsonl` (new, append-only). Schema: §4.4 above.
- **Confirmed-interest events (the inverse of cuts):** `feedback/lead-keeps.jsonl` (new). Same shape as cuts but with a positive label. Or — alternative — kept-leads are the ones that get a `prep` directive, which already lives in `feedback/prep-queue.json` and the sheet status flow. Don't duplicate; reuse.

**Open decision 5 [operator to decide]:** keep `lead-keeps.jsonl` separate or treat existing `prep` directives + sheet status changes as the implicit keep-signal? My read: reuse what exists. A `prep` directive is the strongest possible keep-signal the operator can give, and the sheet status changes (good fit / submitted / etc.) are graduated keep-signals. No new file needed.

### 6.3 The two-side audit pattern

This mirrors patterns already in the codebase:
- `feedback/sheet-write-log.jsonl` + `feedback/sheet-write-blocked.jsonl` (authorized vs unauthorized writes — already pattern of "labeled action + persistence")
- `experience_store.py:reject_candidate(candidate_id, reason)` — labeled rejection with free-text reason
- `feedback/decision-forks.jsonl` — pre-action choices with alternatives and reasoning

The reason-labeled cuts + per-dim persisted scores extends this established pattern to fit-scoring. No new infrastructure shape; just a new surface.

---

## 7. The four-layer learning architecture

The dispatch's design — and the section most likely to get built wrong if under-specified. Each layer's purpose, what trains it, what it does NOT do, and how the three traps are guarded.

### 7.1 Layer 1 — Wide recall pass

**What it is:** the rule-based parameterized matcher of §4. Reads the fit model config, scores every listing in the universe on the three decomposed dimensions, applies gates, surfaces the recall-first cut.

**What trains it:** nothing — the operator's edits to the config are the only knob. (The next layer feeds those edits, but Layer 1 itself is pure rules-from-config.)

**Why this is the foundation:** every later layer rides on it. A bad Layer 1 produces garbage at every other layer.

### 7.2 Layer 2 — Reason-labeled peel-back (the data substrate)

**What it is:** the reason-labeled cuts logged into `lead-cuts.jsonl` per §6.1, paired with the per-dim score records from §4.4. The substrate for everything downstream.

**What trains it:** the operator, by labeling each cut.

**What it does NOT do:** it does not, by itself, change anything. It accumulates labeled cut/keep data. The change happens in Layer 3.

**Open decision 6 [operator to decide]:** how does the labeling happen? Three UX options:
- **(a) Inline in chat:** the operator says `cut: <substring>` and Alice asks "Reason? (b)ad_fit / (d)omain / (c)omp / (v)iability / (o)ther" — single keypress to label.
- **(b) Digest-end batch:** the digest shows surfaced leads; the operator marks cuts at digest read with reasons.
- **(c) Sheet-column-driven:** add a `cut_reason` column to the pipeline sheet; the operator labels there when changing status to `not a fit`.

My read: (c) is the most natural extension of the existing sheet-driven status flow. (a) is the most frictionless inline. (b) might collect the least signal. **You decide.** All three would feed the same `lead-cuts.jsonl` substrate.

### 7.3 Layer 3 — Human-tuning loop FIRST (the near-term "learning")

**What it is:** the operator periodically reads the patterns in the labeled cuts + per-dim score records, and adjusts the fit model config (the typed parameters from §3). NO ML. Just a human reading reasoned data and making informed config edits.

**What trains it:** Layer 2's accumulated reason-labeled cuts. Specifically, only the fit-reason cuts (rows where reason ∈ {`bad_functional_fit`, `wrong_domain`, `comp_too_low`} — viability cuts never teach this layer either).

**Concrete workflow:**
- Weekly (Friday scorecard cycle that already exists in `scorecard.py`), Alice surfaces aggregates: "21 cuts this week; 14 were `viability_or_circumstance` (not fit-relevant); of the remaining 7, 5 were `wrong_domain` on companies scored ≥0.6 on Dimension B. Pattern: B is overconfident on adjacency for fintech-IoT. Suggest tightening `adjacency_coverage` from 0.6 to 0.5 OR adding `fintech_payments` to anti-domain list."
- the operator reads, decides, edits the config.
- Engine re-runs with new config. Next week's data shows whether the adjustment helped.

**Why this is the right "learning" near-term, per the dispatch's Trap 3:** real ML on tiny single-annotator drifting data is overfit-prone. operator-reads-patterns-and-tunes uses the same data but routes the judgment through the operator's interpretation, which is the high-signal labeler this data has. The system *learns* in the sense that the matcher's behavior changes with new data — but the model is the operator's brain, and the persistence is the config edits.

**This is the only "learning" that should ship for a while.** Months, not days. Until there's enough fit-reason-labeled data that a learned re-ranker (Layer 4) wouldn't be noisy.

### 7.4 Layer 4 — Deferred, guarded learned re-ranker (much later)
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

**What it is:** ONLY when enough fit-reason-labeled cut data has accumulated (estimate: hundreds-low-thousands of cuts, months of usage), a small learned model may *re-rank* leads within what Layer 1 surfaces. Examples of "small": logistic regression on the per-dim features; a gradient-boosted tree on the score-record fields. NOT a neural net, NOT an embedding model — the data scale doesn't support either.

**What trains it:** ONLY fit-reason cuts (Trap 1 guard). NEVER viability cuts. NEVER `not_interested_other` cuts. The training data filter is hard-coded into the training pipeline so a future maintainer can't accidentally include viability cuts.

**What it does NOT do:**
- It does NOT replace Layer 1. It re-ranks within what Layer 1 surfaces. Layer 1 still decides what's eligible.
- It does NOT erode easter-egg tails (Trap 2 guard). Any listing flagged by Dimension C as `easter_egg_signal=true` is **excluded from re-ranking** — it appears in its Layer-1-surface position untouched. The learned model never gets to smooth away the tails.
- It does NOT override the rule engine. It augments — re-orders within the surface — and never removes what the rules protect.

**Structural enforcement of the Trap 2 guard (filtered AT CONSTRUCTION, not policed downstream):**

The Layer-4 model code must not be able to SEE easter-egg rows in the first place. The mechanism: the data-loader is the only path into the training/inference functions, and the loader filters easter-egg rows at row-fetch time, before constructing the model's input. The model code's input domain is structurally restricted; there is no in-model "filter first thing" step because the rows aren't present to filter.

```python
## scripts/matcher_layer4_data.py — the load-time filter
def load_training_rows() -> "pd.DataFrame":
    """Return training rows for the learned re-ranker. Easter-egg-flagged
    rows are EXCLUDED at load time, before the DataFrame exists. The
    model never sees them — not because the model code is told to skip,
    but because the data the model receives does not include them.

    A future maintainer cannot accidentally include easter-egg rows in
    training by editing the model code — the rows aren't in the loader's
    output. To re-include them, you would have to delete this filter,
    which is a code change + review event.
    """
    all_rows = _read_jsonl(_FIT_SCORING_LOG_PATH)
    return _to_df([
        r for r in all_rows
        if not r.get("dimension_C_portfolio_combination", {}).get("easter_egg_signal", False)
    ])

def load_inference_rows(listings: list[dict]) -> "pd.DataFrame":
    """Same exclusion at inference time. Easter-egg listings bypass the
    re-ranker — they appear in their Layer-1-surface position untouched.
    The re-ranker never receives them, so cannot re-order them, so cannot
    smooth them away."""
    return _to_df([l for l in listings if not l.get("easter_egg_signal", False)])

## scripts/matcher_layer4_train.py — the model code
def train_reranker(rows: "pd.DataFrame") -> "Model":
    """Trains the re-ranker. Takes the DataFrame from load_training_rows().
    Does NOT call load_training_rows() itself — receives the already-
    filtered data as its input. The function literally cannot see easter-
    egg rows; they aren't in the input."""
    # ... train on whatever rows arrived ...
```

**Verification (build-time tests):**
1. Synthesize 10 score records: 7 conventional, 3 with `easter_egg_signal=true`. Call `load_training_rows()`. Assert returned DataFrame has exactly 7 rows.
2. Pass that DataFrame to `train_reranker`. Inspect the model's input shape — it received 7 rows, never 10.
3. Synthesize 10 inference listings: 8 conventional, 2 easter-egg. Call `load_inference_rows()`. Assert returned DataFrame has exactly 8 rows; the 2 easter-egg listings reach the digest assembler through a separate (unfiltered) path with their Layer-1 surface position intact.

**Precedent in codebase:** `tools.dispatch` runs the `guard()` BEFORE the executor — the executor never sees a forbidden input because the guard raised first. Same pattern here: the loader filters before the model code's input domain exists.

**When this gets built:** the operator explicitly authorizes, with a written threshold for "enough data." E.g., "Layer 4 builds when we have ≥200 fit-reason-labeled cuts AND ≥30 keeps AND four weeks of data drawn from the post-config-stabilization period."

**Open decision 7 [operator to decide]:** the data-thresholds. The above is a sketch; the actual threshold is your call when the time comes.

### 7.5 The three traps, summary

| Trap | Mechanism | Guard |
|---|---|---|
| **1. Rejection ≠ clean negative** | A viability cut (a role-eligibility or location constraint, not fit) trained as "bad fit" would teach the model the OPPOSITE of the operator's preferences in those domains. | **Mandatory reason field on every cut. Fit-learning pipelines filter to fit-reason rows only.** Hard-coded filter at every training step. |
| **2. Conservatism bias collapses tails** | Naive preference-learning regresses toward the center of expressed preference (conventional fit) and erodes the high-value combinatoric tails. | **Dimension C's `easter_egg_signal` field excludes flagged listings from learned re-ranking, both directions.** Layer 4 never gets to smooth away the tails. |
| **3. Tiny, single-annotator, drifting data** | A trained ML model on one operator's labels is noisy and overfit for a long time. Treating "learning" as "build the ML now" is over-engineering. | **Layer 3 (human-tuning) ships first and only. Layer 4 is deferred until data threshold met (decision 7).** Months. |

---

## 8. Universe expansion (root cause 1)

The current universe is `source_multi.py:REGISTRY` — a hardcoded list of ATS slugs. To expand: needs broader reach + a governor that prevents runaway scrapes.

Three approaches, presented for your decision:

### Option A — Broad-net web sourcing

**What:** scrape job aggregator boards (LinkedIn, Indeed, Wellfound, etc.) for queries that match the operator's seniority + functional buckets + location gate. Pull raw listings; run them through the full Layer 1 matcher (which is the discipline that makes broad-net tolerable — the matcher reads past noise).

**Pros:** maximum universe expansion. Catches roles at companies not in the ATS slug list. The recall-first matcher discipline handles the noise.

**Cons:** scraper fragility (boards block, change layouts). Higher inference cost (every fetched listing runs through the model-judged matcher). Requires the governor (bounded loop with dedup, no-progress detection, graceful partial) to prevent runaway scrapes.

### Option B — Company-first, then check openings

**What:** maintain a list of *companies* (not slugs) that match the operator's domain worlds. For each company, lookup their careers page / ATS / known job-board profile. Pull openings from there. The list of companies grows via web research (find companies matching world X), not via slug enumeration.

**Pros:** higher signal-per-fetch (every company on the list already matches Dimension B). Less scraper-fragility (one ATS per company; failures are local). Composable with existing `targets/companies/<slug>.md` research workflow.

**Cons:** universe still bounded by what's in the company list. Cold-start is slow (company list grows incrementally). Misses listings at small/unknown companies that would be great combinatoric fits.

### Option C — Hybrid

**What:** Option B as the *trusted/curated* pull; Option A as the *exploratory* pull running at lower frequency. Curated daily; exploratory weekly. Different surfacing thresholds (curated leads get more conservative scoring; exploratory leads are explicitly marked as exploratory and the digest shows them in a separate section).

**Pros:** quality + reach without forcing one mode. the operator can see exploratory leads marked as such (filters his attention appropriately).

**Cons:** two pipelines to maintain. Two governors (one per pull strategy).

**Open decision 8 [operator to decide]:** Option A / B / C. My read: **C is the architecturally right answer** — the curated path runs continuously, the exploratory path expands the universe gradually, and the surfacing UX naturally segregates them. But the cost is two pipelines. If you'd rather start simpler, B alone is honest and the universe expansion is graduated rather than explosive. Defer to your judgment on operational complexity vs reach.

### 8.1 The governor (regardless of A/B/C)

All three approaches need the bounded-loop discipline `prep_pipeline.py:stage_ground` already uses:
- **Dedup** by `(action, args)` — don't re-fetch the same URL/slug.
- **No-progress detection** — if N consecutive fetches yield nothing new, stop attempting in this run.
- **Graceful partial** — return what was found, log what failed, never blame "incomplete run" on missing data the matcher should still try to use.

Reuse `_log` / `seen_attempts` / `no_progress_streak` pattern from `stage_ground`. Build the governor once, share across the sourcing modes.

---

## 9. Viability / human-judgment boundary
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

**The matcher enforces the LOCATION GATE (reliable, mechanical). It does NOT attempt other role-eligibility judgments that aren't reliably JD-resolvable.**

The reasoning:
- Some eligibility requirements are sometimes in the JD, sometimes not, sometimes flexible. A regex hard-rule kills a role where the requirement is actually negotiable. Pre-filtering them turns an uncertain situation into a certain ceiling.
- Some eligibility concerns are NEVER JD-resolvable — they live in the recruiter/HM conversation. Pre-filtering would require the matcher to guess at conversations it can't see.

**Both are a human pass the operator owns AFTER the matcher narrows.** The matcher's job is to find roles strong on fit, viable on location. The operator adjudicates the rest at the conversation stage. The funnel:

```
Universe → location gate → fit scoring → digest →
  operator reads + flags any eligibility concerns →
  operator sends `prep` for the keepers
```

**What the matcher DOES surface, even on potentially-eligibility-sensitive roles:** the role with the matcher's reasoning. The matcher might note "potential eligibility-sensitivity: federal contractor" as context — but does not down-score for it. The operator sees the note, makes the call.

**Open decision 9 [operator to decide]:** does the matcher annotate potentially-eligibility-sensitive listings with a flag (for the operator's attention), even though it doesn't filter on it? My read: yes. A simple bool field `eligibility_sensitivity_flag: true | false` in the per-dim record, surfaced in the digest. Helps the operator prioritize their human pass without filtering.

**Structural enforcement that the sensitivity flag does not touch the score (no filtering-by-the-back-door):**

The scoring function's input domain explicitly excludes `eligibility_sensitivity_flag`. The flag is set in a pre-scoring enrichment step and appended to the surfaced record by the digest renderer — a sibling channel the scorer cannot read.

```python
## src/alice/pipeline/matcher_engine.py — the scoring function signature is the gate
def score(*,
          gates:      dict,
          dim_A:      dict,   # functional fit judgment + reasoning
          dim_B:      dict,   # company-domain world matches + reasoning
          dim_C:      dict,   # portfolio-combination judgment + reasoning
          tradeoffs:  dict,   # fit_vs_value weights from config
          ) -> dict:
    """Compose the per-dimension judgments into a final fit score.

    NOTE the signature: only gates + the three judged dimensions + tradeoff
    weights. The `eligibility_sensitivity_flag` is NOT a parameter. The
    scoring function CANNOT read it because it never receives it. A future
    maintainer who wanted the flag to affect the score would have to change
    this signature, which is a code-review event.
    """
    # ... compose dim_A + dim_B + dim_C against gates and tradeoffs ...
    # No reference to eligibility_sensitivity_flag exists anywhere in this
    # function's body, because it isn't in the input.

## src/alice/pipeline/matcher_pipeline.py — the surfacing assembly
def assemble_surfaced_record(listing: dict, score_record: dict,
                              enrichments: dict) -> dict:
    """Combine the score with sibling enrichments (eligibility flag,
    universe-source-mode, etc.) for the digest. The enrichments are
    appended AFTER scoring and never feed back into it."""
    return {
        **score_record,
        "eligibility_sensitivity_flag": enrichments.get("eligibility_sensitivity_flag", False),
        "eligibility_sensitivity_reason": enrichments.get("eligibility_sensitivity_reason"),
        # ... other digest-only annotations ...
    }
```

**Verification (build-time tests):**
1. Inspect `score`'s function signature. Assert `eligibility_sensitivity_flag` is not a parameter.
2. Grep the body of `score` (and any function it calls into) for `eligibility_sensitivity`. Assert zero matches.
3. Score the same listing twice — once with `eligibility_sensitivity_flag=True` in the enrichments, once with `False`. Assert the two score records are byte-identical; only the surfaced record (assembled outside the scorer) differs.

**Precedent in codebase:** `notify_email.send` is hard-coded to `cfg.get("EMAIL_TO") or cfg.get("GMAIL_USER")` — there is no input parameter for "send to other recipient." The third-party-send guarantee is structural because the function cannot route elsewhere; same shape here, the score cannot be affected by eligibility-sensitivity because the function does not receive it.

---

## 10. Composition with existing infrastructure

The redesign is NOT a from-scratch rewrite. It composes:

| Existing | Role in the redesign |
|---|---|
| `experience_store.py` (verbatim-anchored, confirmed-by-the operator entries) | Dimension C input. Each entry is one or more "world labels"; Dimension C scores against them. |
| `targets/companies/<slug>.md` (PRODUCT/CUSTOMERS/POSITION sections with CONFIRMED/UNCONFIRMED citations) | Dimension B input. The matcher reads these directly — does not re-research what's already grounded. |
| `prep_pipeline.py:stage_ground` (4-stage gated decompose+ground pattern with retrieval-log governor) | The pattern Layer 1 follows: gates → decomposed scoring → persisted reasoning. The governor pattern is reused for universe expansion (§8). |
| `score_job.py` (current keyword/regex scorer with kills/penalties/bonuses/reasoning) | DEPRECATED by the redesign but the return-shape (per-dim reasoning, tier classification) is the template the new engine's output mirrors. Migration: run both in parallel for a calibration period, compare per-listing. |
| `source_multi.py` + `source_deep.py` + `source_listings.py` (hardcoded ATS slugs) | Become the curated pull in §8 Option B/C, kept as-is for proven companies. Universe expansion adds modes alongside. |
| `daily_delta.py` (sourcing orchestrator with `seen_jobs` dedup) | Becomes the orchestrator for whichever universe-expansion shape lands (§8 decision). Already has dedup. |
| `feedback/sheet-write-log.jsonl`, `feedback/decision-forks.jsonl`, `experience_store.reject_candidate` | Pattern precedents for the `feedback/lead-cuts.jsonl` + `feedback/fit-scoring-log.jsonl` substrates. Same append-only-with-reasoning shape. |
| `obs.flag_grounding_event` + Sentry instrument (built tonight) | If the matcher's Dimension C disagrees materially with the operator's cut reason (e.g., Dimension C scored 0.9 but the operator cut as `wrong_domain`), emit a grounding event. The instrument watches for matcher-vs-human-judgment divergences. |

**This is the right shape — every piece of new behavior plugs into a pattern that already exists.** The infrastructure cost of the redesign is mostly in the engine + config + cut-labeling UX. The substrate, the governor, the persistence patterns, the company-research format, the experience store — already there.

---

## 11. What this is NOT (anti-scope)

- **Not a from-scratch rewrite.** Compose the existing infrastructure listed in §10. Replacing what works is a tax with no return.
- **Not a black-box embedding similarity matcher.** Decomposed, model-judged, interpretable per-dimension reasoning is the whole point. Blob similarity is uninterpretable and measures the wrong thing.
- **Not an ML model trained on cuts (yet).** Layer 4 is deferred until data threshold + the operator's authorization. Building it now is the most expensive way to be wrong.
- **Not a matcher that filters non-fit eligibility concerns.** Those stay a human pass the operator owns after surfacing.
- **Not a UI-clicks-required-to-tune model.** All tuning happens by editing the config (a versioned file). The human-tuning loop is reading the data + editing the file. No web UI needed; CLI / sheet flow if anything.
- **Not a learning system that smooths away tails.** The Trap 2 guard is structural; the redesign would rather have the operator explicitly mark exploratory leads as such (§8 Option C) than have a learned model decide the tails are uninteresting.

---

## 12. Open decisions, all flagged for the operator

Pulled together for easy review:

| # | Decision | Default if you don't decide |
|---|---|---|
| 1 | Config format: TOML / YAML / JSON | TOML (type-safe) |
| 2 | Any parameters under-typed in §3.2? | None flagged — defer to your read on `domain-adjacency` as weighted vs multi-select |
| 3 | `domain_worlds` ontology — seed list + shape | Pragmatic enumerate 8-15 most important worlds + free-text "other"; non-hierarchical |
| 4 | Reason-set completeness — add `prefers_other_lead` / `wrong_seniority`? | Add `wrong_seniority` as own code; skip `prefers_other_lead` |
| 5 | Separate `lead-keeps.jsonl` vs reuse `prep` directives + sheet status | Reuse existing — `prep` directives are the strongest keep-signal |
| 6 | Cut-labeling UX — inline chat / digest-batch / sheet column | (c) sheet column extension |
| 7 | Layer 4 data threshold — when to build the learned re-ranker | Your call, when the time comes — sketch said ≥200 fit-reason cuts + ≥30 keeps + 4 weeks |
| 8 | Universe expansion — Option A / B / C | C (curated daily + exploratory weekly, separate digest sections) |
| 9 | Background-sensitivity flag on listings (annotate without filtering) | Yes — bool field in per-dim record |

**Plus the dispatch's flagged decisions, restated for completeness:**

- **Universe expansion approach** (covered by #8).
- **Whether the matcher annotates or filters viability concerns** (covered by §9 + #9).

---

## 13. When you authorize, the build sequence

DESIGN ONLY UNTIL YOU SAY GO. When/if you authorize:

1. **Config schema + fit-model-v1 file.** TBD on format per decision 1. the operator's fit model encoded as data, versioned in git.
2. **`feedback/fit-scoring-log.jsonl` + `feedback/lead-cuts.jsonl` schemas.** Append-only writers that mirror the existing `time-cost-log.jsonl` pattern.
3. **Matcher engine v1 (`src/alice/pipeline/matcher_engine.py`).** Reads config + listing + company research + experience store. Returns the §4.4 record shape. NO ML; just the parameterized rules.
4. **One probe-run against the existing pipeline.** Take the past N sourced listings, score them with the new engine alongside `score_job.py`. Surface the per-listing diff. the operator reviews and tunes config v2 from the diff.
5. **Cut-labeling UX (decision 6).** Whichever path lands.
6. **Layer 3 human-tuning loop integration.** Weekly aggregate in `scorecard.py`. operator-reads-patterns workflow.
7. **Universe expansion (decision 8 path).** Whichever path lands, with the §8.1 governor.
8. **Layer 4 — only after data threshold + your authorization.** Months out.

Each step is its own unit. Each ships independently. None happens until the prior is reviewed.

---

**End of architecture.** Building zero until you have signed off on the open decisions above. Read the §7 (learning architecture) section twice — that's where the value and the risk concentrate, and where I'd most expect to be wrong if I'm wrong.
