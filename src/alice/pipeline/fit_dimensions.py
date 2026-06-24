"""Deterministic dimensional fit layer (docs/FIT_STRATEGY_SPINE.md).

Wraps the UNCHANGED LLM judge. The judge stays a pure domain/function semantic
call (GUARD-1 stable). This layer computes a STRUCTURED label from the role / JD /
company + the judge's verdict + the deterministic gate, then derives the final
surfacing band via caps. The dims are the labeling-data richness; the caps correct
the judge's measured over-generosity (pure-build / too-senior / off-thesis → REACH).

Two axes:
  desirability:  domain, role_archetype, seniority_fit, blockers[]
  attainability: company_archetype            (employer_bar = v2)
  band (derived): FIT | REACH | NOT-FIT

Generic patterns (role_archetype, seniority, company_archetype) are engine
defaults — they are not persona-specific. Persona-specific bits (domain on/off, adjacent,
competitors, nonrole, archetype signals) load from fit_model.toml.
Pure (no network, no LLM) — fully unit-testable.
"""
from __future__ import annotations

import os
import re
import tomllib
from alice import repo_paths

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(str(repo_paths.ROOT), "config", "fit_model.toml")

# ── generic (non-persona-specific) title patterns ────────────────────────────────
# pure-build = the exposed/substitute lane (caps to REACH even on-domain).
_PURE_BUILD = re.compile(
    r"\b(software|ml|machine learning|data|backend|frontend|full[- ]?stack|platform|"
    r"infrastructure|computational|research|founding|systems|firmware|embedded) engineer(s)?\b|"
    r"\bSRE\b|site reliability|\bdevops\b|developer\b(?!\s*(advocate|relations))", re.I)
# bridge = the intersection-adjacent roles (FDE / SE / SA / TAM / GTM-eng / impl).
_BRIDGE = re.compile(
    r"forward deployed|solutions (engineer|architect|consultant)|sales engineer|"
    r"customer engineer|field engineer|technical account|gtm engineer|gtm systems|"
    r"implementation (engineer|consultant|manager)|deployment (engineer|manager)|"
    r"success architect|developer (advocate|relations)|applied (ai|ml)|technical commercializ", re.I)
# commercial = revenue/customer-owning roles.
_COMMERCIAL = re.compile(
    r"account executive|\bAE\b|customer success|\bCSM\b|revenue (strategy|operations|ops|architect)|"
    r"sales (strategy|operations|ops|manager)|client partner|growth manager|account manager|"
    r"\brevops\b|go[- ]to[- ]market|\bGTM\b(?! engineer)", re.I)
_LEADERSHIP = re.compile(
    r"\b(vp|vice president|head of|chief|\bcto\b|\bcro\b|director|sr\.? director|senior director|"
    r"managing director)\b", re.I)
_ANALYST = re.compile(r"\banalyst\b|\bresearch(er)?\b(?!.*engineer)", re.I)
_PM = re.compile(r"product manager|product owner|\bPM\b|product lead", re.I)
_PM_COMMERCIAL = re.compile(
    r"commercial|commerciali[sz]ation|go[- ]?to[- ]?market|\bGTM\b|revenue|monetiz|"
    r"\bsales\b|customer|solutions?|partnership|growth", re.I)

# seniority
_TOO_SENIOR = re.compile(
    r"\b(vp|vice president|head of|chief|\bcto\b|\bcro\b|svp|evp|director|sr\.? director|"
    r"senior director|managing director)\b", re.I)
_FOUNDER_ADJ = re.compile(r"founding|first (commercial|sales|gtm) hire|founder", re.I)
_TOO_JUNIOR = re.compile(r"\b(sdr|bdr|intern|new ?grad|entry[- ]level|associate(?! director)|junior|apprentice)\b", re.I)

# blockers (travel/location handled by the gate; comp by numbers)
def _load():
    with open(_CONFIG, "rb") as f:
        return tomllib.load(f)

_CFG = _load()
_fd = _CFG.get("fit_dimensions", {})
_src = _CFG.get("sourcing", {})

def _rx(s):
    return re.compile(s, re.I) if s else re.compile(r"(?!x)x")  # never-match if absent

_DOMAIN_ON = _rx("|".join(re.escape(k) for k in _src.get("domain_kw", [])) or None) if _src.get("domain_kw") else re.compile(r"(?!x)x")
_DOMAIN_OFF = _rx("|".join(re.escape(k) for k in _src.get("domain_neg", [])) or None) if _src.get("domain_neg") else re.compile(r"(?!x)x")
_ADJACENT = _rx(_fd.get("adjacent_domain_patterns", ""))
_OFF_SCREENED = _rx(_fd.get("off_thesis_screened_patterns", ""))
# Competitor match is word-boundaried AND checked against the EMPLOYER name only —
# never the JD body (else "clarify"/"groove"/"troops" in prose false-fire, wrongly
# cutting good roles). The question is "is the employer a Cadence Analytics
# competitor," not "does the JD mention a tool."
_COMPETITOR = re.compile(r"\b(?:" + _fd.get("competitors", "(?!x)x") + r")\b", re.I)
_NONROLE = _rx(_fd.get("nonrole_patterns", ""))
_EARLY = _rx(_fd.get("early_founder_signals", ""))
_ENTERPRISE = _rx(_fd.get("enterprise_signals", ""))


def role_archetype(title: str) -> str:
    t = title or ""
    if _BRIDGE.search(t):
        return "bridge"
    if _PM.search(t):
        return "commercial" if _PM_COMMERCIAL.search(t) else "leadership" if _LEADERSHIP.search(t) else "pm_noncommercial"
    if _COMMERCIAL.search(t):
        return "commercial"
    if _ANALYST.search(t):
        return "analyst"
    if _LEADERSHIP.search(t):
        return "leadership"
    if _PURE_BUILD.search(t):
        return "pure_build"
    return "other"


def seniority_fit(title: str) -> str:
    t = title or ""
    if _FOUNDER_ADJ.search(t):
        return "founder_adjacent"
    if _TOO_SENIOR.search(t):
        return "too_senior"
    if _TOO_JUNIOR.search(t):
        return "too_junior"
    return "target"


def domain_class(title: str, body: str, company: str) -> str:
    blob = f"{title} {body} {company}"
    if _OFF_SCREENED.search(blob):
        return "off_thesis"
    if _DOMAIN_ON.search(blob):
        return "on_thesis"
    if _ADJACENT.search(blob):
        return "adjacent"
    if _DOMAIN_OFF.search(blob):
        return "off_thesis"
    return "adjacent"  # unknown -> treat as adjacent (surface as REACH, don't claim FIT)


def company_archetype(company: str, body: str) -> str:
    blob = f"{company} {body}"
    if _EARLY.search(blob):
        return "early_founder_led"
    if _ENTERPRISE.search(blob):
        return "enterprise"
    return "growth"


def blockers(title: str, body: str, company: str, *, gate_status: str | None,
             gate_constraint: str | None, comp_low, comp_high) -> list[str]:
    out = []
    if gate_status == "kill":
        if gate_constraint == "travel_gate":
            out.append("travel")
        elif gate_constraint == "location_gate":
            out.append("location")
        else:
            out.append(gate_constraint or "location")
    if _COMPETITOR.search(company or ""):   # employer is a competitor (not "JD mentions a tool")
        out.append("competitor")
    if _NONROLE.search(f"{title} {body}"):
        out.append("nonrole")
    try:
        if comp_low is not None and int(comp_low) > 250000:
            out.append("comp_high")
    except (TypeError, ValueError):
        pass
    return out


def compute(*, title: str, company: str, body: str, judge_verdict: str,
            gate_status: str | None = None, gate_constraint: str | None = None,
            comp_low=None, comp_high=None) -> dict:
    """Return the structured fit label + derived band. judge_verdict is the LLM's
    raw verdict (FIT/REACH/NOT-FIT); band is the corrected surfacing decision."""
    dims = {
        "domain": domain_class(title, body, company),
        "role_archetype": role_archetype(title),
        "seniority_fit": seniority_fit(title),
        "company_archetype": company_archetype(company, body),
        "blockers": blockers(title, body, company, gate_status=gate_status,
                             gate_constraint=gate_constraint, comp_low=comp_low,
                             comp_high=comp_high),
    }
    dims["band"] = derive_band(judge_verdict, dims)
    dims["channel"] = ("cold_email_decision_maker"
                       if dims["company_archetype"] == "early_founder_led"
                       else "warm_intro_preferred" if dims["company_archetype"] == "enterprise"
                       else "standard")
    return dims


def derive_band(judge_verdict: str, dims: dict) -> str:
    """Corrected band = judge verdict adjusted by deterministic caps.
    inform-not-gate: caps only ever DOWNGRADE FIT->REACH or enforce hard NOT-FIT on
    real blockers; they never upgrade, and attainability never silently cuts."""
    v = (judge_verdict or "").upper()
    b = dims.get("blockers", [])
 # hard NOT-FIT only on genuine blockers (gates / competitor / nonrole).
 # comp_high is NOT here: comp above the example target band = REACH ("the persona
 # reaches for pay"), reconciling with the judge prompt, not a hard kill.
    if any(x in b for x in ("travel", "location", "competitor", "nonrole")):
        return "NOT-FIT"
    if v == "NOT-FIT":
        return "NOT-FIT"
 # caps: FIT -> REACH for exposed-lane / too-senior / off-thesis
    if v == "FIT":
        if dims["role_archetype"] == "pure_build":
            return "REACH"
        if dims["role_archetype"] == "pm_noncommercial":
            return "REACH"
        if dims["seniority_fit"] == "too_senior":
            return "REACH"
        if dims["domain"] == "off_thesis":
            return "REACH"
        if "comp_high" in b:        # comp above example band: surface as REACH (persona reaches for pay)
            return "REACH"
    return v if v in ("FIT", "REACH") else "REACH"
