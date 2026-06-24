"""The constraint-driven fit-judge (model-judge, cost-layered).

WHAT THIS IS:
  An LLM-judge that reads the candidate's fit rubric (config/fit_model.toml) plus
  the stored JD body and emits a structured fit verdict (FIT / NOT-FIT / REACH), a
  reason, and which constraint drove the verdict. It runs ONLY on gate-survivors
  (the handful per run that survive daily_delta's cheap lexical gates), reading
  the JD body persisted to seen_jobs. That is the cost-layering contract.

WHERE IT SITS:
    Universe -> cheap lexical gates (daily_delta) -> SURVIVORS -> [THIS] -> digest
  The survivor count is tiny, so the expensive model-judge is affordable.

DESIGN PRINCIPLES:
  - Engine vs config: this module is the engine; fit_model.toml is the config.
    The module READS the config, never embeds the candidate's specifics. The
    constraint-loader (`load_constraints`) is pure + testable.
  - Constraint-driven, NOT a hard-coded gate sequence: we feed the model the
    constraints + the JD body and let it judge. We do NOT traffic-control it
    through fixed sub-checks. The deterministic part is only: parse the VERDICT
    line, fail-safe on parse error.
  - _judge reuse: this reuses evals._judge / evals._verdict's pattern — a single
    cheap llm.call with a VERDICT line we parse — extending the verdict vocabulary
    to the three-way FIT/NOT-FIT/REACH the fit-judge needs.
  - Traced: the judge call is wrapped in a span (`fit_judge.role`) carrying role
    id/company, verdict, driving constraint, model. evals._judge -> llm.call opens
    an inner `llm.call.*` span; this span is the parent that makes calibration
    queryable.
  - Location/travel viability (mechanical, JD-resolvable) and domain/functional
    fit are enforced here. Travel/onsite is in the JD and feeds the remote-first
    preference, so it is in scope.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any
from alice import repo_paths

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = str(repo_paths.ROOT)
DEFAULT_CONFIG_PATH = os.path.join(_REPO, "config", "fit_model.toml")

# Fit-judge model: gemini-2.5-flash via OpenRouter (tighter on off-domain noise,
# ~5x cheaper than haiku). Scoped to the fit-judge only (evals._judge model
# override); the other judges stay on haiku (evals._JUDGE_MODEL).
_JUDGE_MODEL = "google/gemini-2.5-flash"

# The three-way verdict vocabulary. Distinct from evals' PASS/FAIL because a
# fit-judge needs the REACH band (great-fit-but-stretch) the doc calls out
# (e.g. the Supabase SA role: on-domain + remote, but the 6yr-SA bar is the reach).
VERDICTS = ("FIT", "NOT-FIT", "REACH")
PASSING_VERDICTS = ("FIT", "REACH")  # surface these; NOT-FIT is cut


# ─────────────────────────────────────────────────────────────────────────────
# 1. TOML reader constraints object (pure, testable).
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DomainWorld:
    """One portfolio world. The label is an INTERNAL handle only — it is NEVER
    rendered into a prompt (keyword-substring guard). The model judges against
    `definition` + `anti_examples`."""
    label: str
    definition: str
    anti_examples: tuple[str, ...]


@dataclass(frozen=True)
class Constraints:
    """The loaded, typed fit model: binary gates, multi-selects, weighted dims,
    fill-ins, composites. Pure data — no engine behavior, no candidate-specific
    logic baked in."""
    version: str
 # Binary gates
    remote_us_eligible: bool
    non_remote_locations: tuple[str, ...]
    non_remote_radius_mi: int
    travel_allowed: bool
    travel_relaxes_on: str
    anti_fit_buckets: tuple[str, ...]
 # Multi-select
    seniority_selected: tuple[str, ...]
 # Weighted
    functional_buckets: dict[str, float]
    combinatoric_emphasis: float
    adjacency_coverage: float
    fit_weight: float
    value_weight: float
 # Fill-in
    location_center: dict[str, Any]
 # Composite
    comp_threshold_usd: int
    comp_soft_below_threshold: bool
    comp_hard_floor_usd: int
 # Domain worlds
    domain_worlds: tuple[DomainWorld, ...]
 # Identity (loaded from config, not hard-coded). Defaults keep the dataclass
 # backward-compatible; a different user supplies their own via config.
    candidate_name: str = ""
    portfolio_summary: str = ""
 # Raw, for any field not promoted above (audit / future use)
    raw: dict = field(default_factory=dict, repr=False)


def load_constraints(path: str = DEFAULT_CONFIG_PATH) -> Constraints:
    """Load config/fit_model.toml into a typed Constraints object.

    Pure: no network, no LLM, no side effects. Raises FileNotFoundError if the
    config is missing and KeyError if a required section is absent (fail loud at
    load — a malformed rubric must not silently degrade the judge)."""
    with open(path, "rb") as f:
        d = tomllib.load(f)

    gloc = d["gates"]["location"]
    gaf = d["gates"]["anti_fit"]
    fg = d["weights"]["functional_gradient"]
    comp = d["composite"]["comp_floor"]
    fvv = d["weights"]["fit_vs_value_tradeoff"]
    gid = d.get("identity", {})

    worlds = tuple(
        DomainWorld(
            label=w["label"],
            definition=w["definition"],
            anti_examples=tuple(w.get("anti_examples", [])),
        )
        for w in d.get("domain_worlds", [])
    )

    return Constraints(
        version=d.get("version", "unknown"),
        remote_us_eligible=bool(gloc.get("remote_us_eligible", False)),
        non_remote_locations=tuple(gloc.get("non_remote_locations", [])),
        non_remote_radius_mi=int(gloc.get("non_remote_radius_mi", 0)),
        travel_allowed=bool(gloc.get("travel_allowed", False)),
        travel_relaxes_on=str(gloc.get("travel_relaxes_on", "")),
        anti_fit_buckets=tuple(gaf.get("buckets", [])),
        seniority_selected=tuple(d["selects"]["seniority"].get("selected", [])),
        functional_buckets=dict(fg.get("buckets", {})),
        combinatoric_emphasis=float(d["weights"]["combinatoric_emphasis"]["value"]),
        adjacency_coverage=float(d["weights"]["adjacency_coverage"]["value"]),
        fit_weight=float(fvv.get("fit_weight", 0.5)),
        value_weight=float(fvv.get("value_weight", 0.5)),
        location_center=dict(d.get("values", {}).get("location_center", {})),
        comp_threshold_usd=int(comp.get("threshold_usd", 0)),
        comp_soft_below_threshold=bool(comp.get("soft_below_threshold", True)),
        comp_hard_floor_usd=int(comp.get("hard_floor_usd", 0)),
        domain_worlds=worlds,
        candidate_name=str(gid.get("name", "")),
        portfolio_summary=str(gid.get("portfolio_summary", "")),
        raw=d,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Prompt builder — injects the constraints as the rubric + the candidate's profile.

# CONSTRAINT-DRIVEN: we render the full rubric (gates, seniority, functional
# buckets, comp composite, domain worlds AS DEFINITIONS) and the JD body, and ask
# for ONE holistic verdict. We do NOT pre-decide sub-checks in code and feed the
# model only leftovers. The keyword-substring guard is honored: world DEFINITIONS
# + anti_examples are rendered; world LABELS are never in the prompt.
# ─────────────────────────────────────────────────────────────────────────────
def _render_worlds(constraints: Constraints) -> str:
    """Render domain worlds as numbered DEFINITION blocks. Labels are NOT
    included — the model reasons against definitions, not tokens."""
    blocks = []
    for i, w in enumerate(constraints.domain_worlds, start=1):
        anti = "\n".join(f"      - NOT this: {ex}" for ex in w.anti_examples)
        blocks.append(
            f"  World {i}:\n"
            f"    {w.definition}\n"
            f"{anti}"
        )
    return "\n\n".join(blocks)


def build_judge_system(constraints: Constraints) -> str:
    """Construct the fit-judge SYSTEM prompt from the loaded constraints.

    This is the rubric + the candidate's profile, derived ENTIRELY from the config so the
    human-tuning loop (Layer 3) changes behavior by editing the TOML, not code."""
    worlds = _render_worlds(constraints)
    fb = constraints.functional_buckets
    return (
        "You are a fit-judge for the candidate's job search. Given the candidate's fit "
        "rubric and a job posting (title, company, location, comp, and the full "
        "JD body), judge whether the role is a fit. Reason holistically over the "
        "rubric and the JD body together. Do not invent facts not in the JD.\n\n"

        f"{constraints.candidate_name.upper()}'S PROFILE (the portfolio you are "
        f"matching against):\n  {constraints.portfolio_summary}\n\n"

        "LOCATION & TRAVEL — ALREADY HANDLED; do NOT re-judge. A deterministic "
        "pre-gate has already verified location + travel viability (remote-US / Ohio "
        "eligibility, relocation/residence, on-site/hybrid, and travel) before this "
        "point. Every role you see has PASSED that gate. Do NOT evaluate location or "
        "travel, and do NOT emit location_gate or travel_gate — that is the gate's "
        "job. Judge ONLY domain, function, seniority, comp, and anti-fit.\n\n"


        "ANTI-FIT FUNCTIONAL BUCKETS (gated out — these are NOT-FIT regardless "
        f"of other strengths): {list(constraints.anti_fit_buckets)}. Use ONLY "
        "these exact buckets; do NOT invent new anti-fit categories. Teaching / "
        "curriculum / technical-education with a customer-enablement or technical "
        "angle is ON-domain (it composes with the education-pedagogy world), NOT "
        "an anti-fit. Revenue-operations / sales-operations / GTM-operations is a "
        "CORE on-target function (the candidate's RevOps track), never an anti-fit. A role "
        "whose CORE FUNCTION is marketing (Marketer, Marketing/Brand/Content/"
        "Demand-Gen/Growth-Marketing/Product-Marketing manager or lead) IS the "
        "pure_marketing anti-fit -> NOT-FIT, even with a 'Forward Deployed', "
        "'Founding', or 'technical' modifier (a 'Forward Deployed Marketer' is "
        "still marketing). This does NOT include GTM Engineer, Growth Engineer, "
        "RevOps, or sales/CS roles, which are commercial/technical, not marketing. "
        "The pure_ic_sales_no_tech_depth anti-fit is ONLY for transactional / SDR / "
        "BDR / no-technical-angle selling; an Enterprise or Strategic Account "
        "Executive selling a TECHNICAL product (AI, dev-tools, industrial, technical "
        "SaaS) to technical / engineering / ops buyers is commercial-led-and-"
        "technically-fluent (functional bucket a), NOT this anti-fit — do not gate it.\n\n"

        "FUNCTIONAL FIT GRADIENT (higher is better; rank, do not gate except the "
        "anti-fits above):\n"
        f"  (a) commercial-led, technically fluent — owns customer/revenue, "
        f"technical depth is the edge. weight {fb.get('a')}.\n"
        f"  (c) genuine bridge — hired BECAUSE he does both commercial + "
        f"technical. weight {fb.get('c')}.\n"
        f"  (b) technical-led, hands-on building with some customer exposure. "
        f"weight {fb.get('b')}.\n\n"

        "SENIORITY (multi-select — any of these is valid): "
        f"{list(constraints.seniority_selected)}. Too-JUNIOR (SDR/BDR/entry/new-grad) "
        "is a true mismatch (trends NOT-FIT). Too-SENIOR (VP/Director/very-senior) "
        "is NOT a viability fail — the candidate reaches for seniority; surface as REACH, "
        "never NOT-FIT on seniority alone.\n\n"

        "COMPENSATION (composite — NOT a hard gate at the high end): soft "
        f"preference floor ${constraints.comp_threshold_usd:,}; great fit buys down "
        f"below it (soft_below_threshold = {constraints.comp_soft_below_threshold}). "
        f"Hard floor ${constraints.comp_hard_floor_usd:,} (below this, kill unless "
        f"strong negotiability). HIGH comp / bands well above the target band are NOT a fail — "
        f"the candidate reaches for pay; never NOT-FIT on comp-too-high (surface as REACH at "
        f"most). fit weight {constraints.fit_weight}, value weight {constraints.value_weight}.\n\n"

        "DOMAIN WORLDS — the candidate's portfolio coverage. Judge whether the COMPANY's "
        "actual business overlaps or ADJOINS any of these worlds (adjacency "
        f"counts; combinatoric coverage counts; emphasis "
        f"{constraints.combinatoric_emphasis}, adjacency tolerance "
        f"{constraints.adjacency_coverage}). WEIGH DOMAIN BY ROLE TYPE — critical:\n"
        "  - SELL-INTO roles (Account Executive, or a Sales Engineer / CSM selling "
        "into a specific industry): domain credibility is LOAD-BEARING. The candidate needs an "
        "industry story for the buyer, so a company on NONE of these worlds is a "
        "domain miss and trends NOT-FIT.\n"
        "  - BUILD / FUNCTION-PORTABLE roles — the function IS the value and travels "
        "across verticals: Applied AI / AI / Agent / ML / Founding / GTM / Growth "
        "Engineer, LLMOps / eval engineer, Solutions / Forward-deployed ENGINEERING, "
        "AND the function-portable CUSTOMER roles (Customer Success, Onboarding, "
        "Implementation, Technical Account Manager, Solutions Architect, Customer "
        "Engineer), AND Revenue/Sales Operations and Product Management. For ALL of "
        "these, domain is a SOFT signal, NOT a gate — the candidate's functional + builder "
        "credibility (production ML, agent orchestration, MCP, full-stack, RevOps) "
        "travels. Do NOT NOT-FIT these on domain alone — an off-world build/function "
        "role is at worst REACH; an on-world one is FIT. Even at an off-everything "
        "company (fintech, data-infra, etc.) a build/function role is REACH, not a "
        "kill. A Product Manager title is REACH (surface), never anti-fit. Reserve "
        "domain-driven NOT-FIT for SELL-INTO roles (AE / industry-specific seller) "
        "where the buyer-story IS the job, and reserve anti-fit ONLY for a CORE "
        "marketing function (the pure_marketing bucket) — not for PM, CS, or "
        "technical-curriculum/enablement (those are on-domain or REACH).\n"
        "NOTE: AI/LLM observability + eval (Phoenix / Arize / LangSmith / Braintrust), "
        "AI-AGENT / orchestration / MCP platforms, agentic B2B-ops automation, and "
        "voice-AI are all on-domain for the candidate — hands-on builder in each. Do NOT treat "
        "them as off-domain:\n\n"
        f"{worlds}\n\n"

        "VERDICT VOCABULARY:\n"
        "  FIT     — passes viability gates, on-domain, functional bucket "
        "matches (a/c/b), seniority and comp are workable.\n"
        "  REACH   — on-domain / combinatorically interesting and passed the "
        "pre-gate, but stretches on one axis: a years-of-experience bar above the candidate's, "
        "a too-SENIOR title, or a HIGH comp band (the candidate reaches for seniority/pay). "
        "Worth surfacing; the candidate decides.\n"
        "  NOT-FIT — clearly off-domain (no story for the company's business), an "
        "anti-fit functional bucket, or too-junior (SDR/BDR/entry). Seniority-too-"
        "senior and comp-too-high are NEVER NOT-FIT (they are REACH). Do NOT emit "
        "NOT-FIT for location or travel — the deterministic pre-gate owns those.\n"

        "OUTPUT FORMAT (exact):\n"
        "  Line 1: 'VERDICT: FIT' or 'VERDICT: REACH' or 'VERDICT: NOT-FIT'\n"
        "  Line 2: 'CONSTRAINT: <the single constraint that most drove the "
        "verdict>' (e.g. travel_gate, location_gate, domain_fit, "
        "functional_fit, seniority, comp, anti_fit).\n"
        "  Line 3+: one or two sentences of reason, citing JD phrases.\n"
    )


def build_judge_prompt(*, title, company, body, location=None,
                       comp_low=None, comp_high=None, remote_flag=None) -> str:
    """Construct the per-listing USER prompt. The JD `body` is the critical
    input (Build A persisted it to seen_jobs)."""
    comp = "unspecified"
    if comp_low or comp_high:
        lo = f"${int(comp_low):,}" if comp_low else "?"
        hi = f"${int(comp_high):,}" if comp_high else "?"
        comp = f"{lo} - {hi}"
    remote = "unspecified" if remote_flag is None else ("yes" if remote_flag else "no")
    body = body or "(no JD body available)"
    return (
        f"TITLE: {title}\n"
        f"COMPANY: {company}\n"
        f"LOCATION: {location or 'unspecified'}\n"
        f"COMP: {comp}\n"
        f"REMOTE_FLAG: {remote}\n\n"
        f"JD BODY:\n{body}\n\n"
        "Judge per the rubric. Output the VERDICT / CONSTRAINT / reason format."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Verdict parsing — deterministic; fail-safe to NOT-FIT on parse error.

# Mirrors evals._verdict (doc §13 / evals.py) but for the three-way vocabulary.
# Fail-safe direction: an unparseable judge response yields a NON-PASSING
# verdict (NOT-FIT), never a false FIT — same discipline as evals' "error, not a
# false pass". A role only surfaces on an explicit FIT/REACH.
# ─────────────────────────────────────────────────────────────────────────────
def parse_verdict(judge_text: str) -> dict:
    """Parse the judge response into {verdict, driving_constraint, reason}.
    Fail-safe: unparseable -> verdict NOT-FIT with driving_constraint
    'parse_error' (never a false FIT)."""
    text = (judge_text or "").strip()
    up = text.upper()

    verdict = None
    seg = up.split("VERDICT:")
    if len(seg) >= 2:
        head = re.sub(r"[*_`#>]+", "", seg[1]).strip()  # tolerate markdown emphasis
 # Order matters: 'NOT-FIT' before 'FIT' so the substring doesn't mis-hit.
        if head.startswith("NOT-FIT") or head.startswith("NOT FIT"):
            verdict = "NOT-FIT"
        elif head.startswith("REACH"):
            verdict = "REACH"
        elif head.startswith("FIT"):
            verdict = "FIT"

    if verdict is None:
        return {"verdict": "NOT-FIT", "driving_constraint": "parse_error",
                "reason": "Unparseable judge response; fail-safe to NOT-FIT.",
                "raw": text}

 # Driving constraint (best-effort; absence is not fatal).
    driving = "unspecified"
    cseg = up.split("CONSTRAINT:")
    if len(cseg) >= 2:
 # take the rest of that line from the original-case text
        idx = up.find("CONSTRAINT:") + len("CONSTRAINT:")
        line = text[idx:].splitlines()[0].strip()
        if line:
            driving = line

 # Reason = everything after the CONSTRAINT line (or after VERDICT if no
 # constraint line), trimmed. Best-effort, never raises.
    reason = ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    body_lines = []
    for ln in lines:
        u = ln.strip().upper()
        if u.startswith("VERDICT:") or u.startswith("CONSTRAINT:"):
            continue
        body_lines.append(ln.strip())
    reason = " ".join(body_lines).strip()

    return {"verdict": verdict, "driving_constraint": driving,
            "reason": reason, "raw": text}


# ─────────────────────────────────────────────────────────────────────────────
# 4. The judge — reuses evals._judge (one cheap llm.call), Phoenix-traced.
# ─────────────────────────────────────────────────────────────────────────────
# ── PM-title REACH cap (calibration: "D2 PM-title = REACH") ──────────────
# A product-manager title surfaces at REACH, not FIT — the candidate is not a product PM
# by title. EXCEPTION: commercial / GTM / technical-commercialization PMs are
# explicit CLAUDE.md seniority targets ("commercial PM") and stay FIT-eligible.
# This is a DETERMINISTIC cap (applied post-judge, like the location reach_flag
# cap): the prompt instruction alone does not hold — the model reasons past it
# on strong on-domain logic (observed: Trailhead Robotics "PM, Technical
# Commercialization" judged FIT live validation).
_PM_TITLE_RE = re.compile(r"\bproduct\s+(?:manager|owner|lead)\b", re.I)
_PM_COMMERCIAL_RE = re.compile(
    r"commercial|commerciali[sz]ation|go[\s\-]?to[\s\-]?market|\bGTM\b|revenue|"
    r"monetiz|\bsales\b|customer|solutions?|partnership|growth", re.I)


def _is_noncommercial_pm_title(title: str) -> bool:
    """True for a Product Manager/Owner/Lead title with NO commercial/GTM signal.
    Commercial PMs (the CLAUDE.md target) return False and stay FIT-eligible."""
    t = title or ""
    return bool(_PM_TITLE_RE.search(t)) and not _PM_COMMERCIAL_RE.search(t)


def judge_listing(*, title, company, body, location=None, comp_low=None,
                  comp_high=None, remote_flag=None, listing_id=None,
                  constraints: Constraints | None = None,
                  model: str = _JUDGE_MODEL) -> dict:
    """Run the constraint-driven fit-judge on ONE listing. Returns
    {listing_id, company, title, verdict, driving_constraint, reason, model,
     fit_model_version, judge_raw}.

    Reuses evals._judge (doc §13) — a single cheap llm.call with a parsed VERDICT
    line. Wrapped in a Phoenix span (`fit_judge.role`) carrying the calibration
    attributes (role id/company, verdict, driving-constraint, model). The span is
    a no-op unless ALICE_TRACING=1 (telemetry.py contract), so this is safe and
    zero-overhead off-path. evals._judge -> llm.call opens an inner `llm.call.*`
    span; this span is its parent.

    On any LLM error (including missing API key -> llm raises RuntimeError) the
    judge fail-safes to NOT-FIT with driving_constraint 'judge_error' — never a
    false FIT.
    """
    if constraints is None:
        constraints = load_constraints()

 # ── Deterministic location/travel gate (decision; see
 # scripts/location_gate.py + docs/DECISION_LOG.md). Runs BEFORE the LLM so a
 # kill skips the model entirely (cheaper) and a domain/prompt change can never
 # move a location verdict. status: kill | reach_flag | ok.
    from alice.pipeline.location_gate import location_travel_gate
    _gate = location_travel_gate(title=title, body=body, location=location,
                                 remote_flag=remote_flag)
    if _gate["status"] == "kill":
        gres = {
            "listing_id": listing_id, "company": company, "title": title,
            "verdict": "NOT-FIT", "driving_constraint": _gate["constraint"],
            "reason": _gate["reason"], "model": "deterministic_gate",
            "fit_model_version": constraints.version, "judge_raw": "",
        }
 # the gate-kill path skips the model, so attach the dimensional label here
 # too — every result carries band/dimensions/channel (no None bands).
        try:
            from alice.pipeline import fit_dimensions
            gdims = fit_dimensions.compute(
                title=title or "", company=company or "", body=body or "",
                judge_verdict="NOT-FIT", gate_status="kill",
                gate_constraint=_gate["constraint"], comp_low=comp_low, comp_high=comp_high)
            gres["dimensions"] = gdims
            gres["band"] = gdims["band"]
            gres["channel"] = gdims["channel"]
        except Exception:
            gres["band"] = "NOT-FIT"
        return gres

    system = build_judge_system(constraints)
    prompt = build_judge_prompt(title=title, company=company, body=body,
                                location=location, comp_low=comp_low,
                                comp_high=comp_high, remote_flag=remote_flag)

 # ---- Phoenix span (no-op unless ALICE_TRACING=1) -----------------------
    try:
        from alice.observability.telemetry import init_tracing, tracer, set_attr
        init_tracing()
 # re-fetch tracer post-init (init may have replaced the no-op)
        from alice.observability.telemetry import tracer as _tr
        span_cm = _tr.start_as_current_span("fit_judge.role")
    except Exception:
        class _N:
            def __enter__(self): return None
            def __exit__(self, *a): return False
        span_cm = _N()
        set_attr = None  # type: ignore

    with span_cm as span:
 # set the pre-call attributes we already know
        if set_attr and span is not None:
            set_attr(span, "alice.task", "fit_judge")
            set_attr(span, "tool.name", str(listing_id or ""))      # role id handle
            set_attr(span, "input.value", f"{title} @ {company}")
            set_attr(span, "llm.model_name", model)

        try:
            from alice.pipeline import evals
            judge_text = evals._judge(
                task="fit_judge",
                system=system,
                prompt=prompt,
                max_tokens=400,
                model=model,  # fit-judge model (gemini-2.5-flash); other F2 judges stay haiku
                job_key=listing_id,  # record_prediction_span keys on this -> B3 outcome can annotate back
            )
            parsed = parse_verdict(judge_text)
        except Exception as e:
            parsed = {"verdict": "NOT-FIT", "driving_constraint": "judge_error",
                      "reason": f"{type(e).__name__}: {e}", "raw": ""}

 # reach_flag: location is ambiguous (remote-flagged metros / metro with no
 # remote stated / unknown residence area). Surface as REACH for the candidate — never
 # let the LLM's domain/function judgment silently promote it to FIT.
        verdict = parsed["verdict"]
        constraint = parsed["driving_constraint"]
        reason = parsed["reason"]
        if _gate["status"] == "reach_flag" and verdict == "FIT":
            verdict = "REACH"
            reason = f"{reason} [location flag: {_gate['note']}]"
 # PM-title cap: a non-commercial product-manager title is REACH, never FIT.
        if verdict == "FIT" and _is_noncommercial_pm_title(title):
            verdict = "REACH"
            constraint = "seniority"
            reason = f"{reason} [PM-title: non-commercial product role surfaces as REACH]"

        result = {
            "listing_id": listing_id,
            "company": company,
            "title": title,
            "verdict": verdict,
            "driving_constraint": constraint,
            "reason": reason,
            "model": model,
            "fit_model_version": constraints.version,
            "judge_raw": parsed.get("raw", ""),
        }

 # Dimensional layer (docs/FIT_STRATEGY_SPINE.md): structured label + the
 # corrected surfacing band. Additive — `verdict` is the judge's semantic
 # call (unchanged, GUARD-1-stable); `band` is what daily_delta should cut on.
        try:
            from alice.pipeline import fit_dimensions
            dims = fit_dimensions.compute(
                title=title or "", company=company or "", body=body or "",
                judge_verdict=verdict, gate_status=_gate.get("status"),
                gate_constraint=_gate.get("constraint"),
                comp_low=comp_low, comp_high=comp_high)
            result["dimensions"] = dims
            result["band"] = dims["band"]
            result["channel"] = dims["channel"]
        except Exception as _e:
            result["band"] = verdict  # fail-open to the judge verdict

 # post-call calibration attributes — stamp the structured dims onto the
 # span so Phoenix/B3 reality-feedback keys on the dimensional label (band,
 # domain, archetype), not just the raw verdict.
        if set_attr and span is not None:
            set_attr(span, "output.value",
                     f"{result['verdict']} / {result['driving_constraint']}")
            _d = result.get("dimensions") or {}
            set_attr(span, "fit.band", result.get("band", ""))
            set_attr(span, "fit.domain", _d.get("domain", ""))
            set_attr(span, "fit.role_archetype", _d.get("role_archetype", ""))
            set_attr(span, "fit.seniority_fit", _d.get("seniority_fit", ""))
            set_attr(span, "fit.company_archetype", _d.get("company_archetype", ""))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# 5. The orchestrator-facing callable — cost layering plug-in (doc §; held wire).

# This is what daily_delta.run() calls AFTER survivors are computed and their
# bodies persisted. The single call site (held until the B module + agent-1's
# tools.py settle) is:

# survivors = fit_judge.judge_survivors(survivors)

# Each survivor rec (per daily_delta._fetch_new_listings) carries:
# id, title, company, url, location, body, comp_low, comp_high, remote_flag
# We attach: fit_verdict, fit_reason, driving_constraint, fit_judge_model.
# ─────────────────────────────────────────────────────────────────────────────
def judge_survivors(survivors: list[dict],
                    constraints: Constraints | None = None,
                    drop_not_fit: bool = False,
                    model: str = _JUDGE_MODEL) -> list[dict]:
    """Run the fit-judge over gate-survivors. Mutates each rec in place adding
    fit_verdict / fit_reason / driving_constraint / fit_judge_model, and returns
    the list. With drop_not_fit=True, NOT-FIT survivors are filtered out (so the
    digest only sees FIT/REACH); default False keeps them annotated for audit.

    Load the constraints ONCE for the whole batch (one config read per run)."""
    if constraints is None:
        constraints = load_constraints()
    out = []
    for rec in survivors:
        res = judge_listing(
            title=rec.get("title"), company=rec.get("company"),
            body=rec.get("body"), location=rec.get("location"),
            comp_low=rec.get("comp_low"), comp_high=rec.get("comp_high"),
            remote_flag=rec.get("remote_flag"),
            listing_id=rec.get("id"), constraints=constraints, model=model,
        )
        rec["fit_verdict"] = res["verdict"]
        rec["fit_reason"] = res["reason"]
        rec["driving_constraint"] = res["driving_constraint"]
        rec["fit_judge_model"] = res["model"]
 # Dimensional layer: corrected surfacing band + structured label + channel.
        rec["fit_band"] = res.get("band", res["verdict"])
        rec["fit_dimensions"] = res.get("dimensions", {})
        rec["fit_channel"] = res.get("channel", "standard")
 # Cut on the corrected BAND, not the raw verdict (catches competitor/nonrole
 # the judge FIT'd; keeps pure-build/too-senior as surfaced REACH).
        if drop_not_fit and rec["fit_band"] not in PASSING_VERDICTS:
            continue
        out.append(rec)
    return out
