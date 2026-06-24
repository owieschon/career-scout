"""Deterministic location + travel gate (see docs/DECISION_LOG.md).

WHY THIS EXISTS
---------------
Location and travel are the mechanical, JD-resolvable viability checks. Keeping
them in deterministic rules (rather than inside the fit_judge LLM prompt) makes
them:
  - stable (a domain or prompt change can't move a location verdict),
  - exactly the operator's policy (encoded, not argued with a model),
  - testable without LLM cost,
and lets the LLM judge reason ONLY about domain + function fit. Inside the LLM
prompt these checks were unstable: prompt changes perturbed them, and the model
would not reliably honor the operator's calibration (e.g. "East Coast" must read
as Ohio-eligible).

The gate is conservative about not killing on a bare city label the JD never
qualifies, in two ways:
  1. it reads the JD BODY for an EXPLICIT requirement, never the bare city label;
  2. it is CONSERVATIVE — anything ambiguous returns 'reach_flag' or 'ok', never
     a silent 'kill'. A listed metro with no residence requirement is surfaced,
     not cut.

CONTRACT
--------
location_travel_gate(...) -> dict:
  {"status": "kill",       "constraint": "location_gate"|"travel_gate", "reason": str}
  {"status": "reach_flag", "note": str}   # passes, but cap the verdict at REACH (surface)
  {"status": "ok"}                        # location/travel clearly fine; judge freely

Operator calibration (example): remote-US, based in Columbus, OH (Franklin
County). Ohio-eligible regions include the broad eastern + midwest designations
("East Coast", "Eastern US", "ET", "Midwest", "Great Lakes", "Ohio Valley",
"anywhere in the US"). Remote-first preference: does not relocate and does not
take travel-heavy roles.
"""
from __future__ import annotations

import re
from alice import repo_paths

# ── text prep ────────────────────────────────────────────────────────────────
def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


# ── User geography (config [gates.location]) ───────────────────────────────────
# The user's home tokens + the broad regions they treat as including them. Loaded
# from fit_model.toml, never baked into engine code — a different user (e.g.
# West-Coast-based) supplies their own base + eligible regions.
def _loc_calibration():
    """Load (base_patterns, eligible_region_patterns) from fit_model.toml
    [gates.location]. Fail loud if absent: the gate's geography is the USER's
    calibration, not an engine default."""
    import os, tomllib
    p = os.path.join(str(repo_paths.ROOT),
                     "config", "fit_model.toml")
    with open(p, "rb") as f:
        g = tomllib.load(f).get("gates", {}).get("location", {})
    base, elig = g.get("base_patterns"), g.get("eligible_region_patterns")
    if not base or not elig:
        raise ValueError("location_gate: [gates.location].base_patterns + "
                         "eligible_region_patterns are required (Phase 0 de-hardcode).")
    return base, elig

_BASE_GEO_SRC, _ELIGIBLE_SRC = _loc_calibration()
_BASE_GEO = re.compile(_BASE_GEO_SRC, re.I)                            # the user's home tokens
_ELIGIBLE_REGIONS = re.compile(r"\b(" + _ELIGIBLE_SRC + r")\b", re.I)  # broad regions that include them

# Specific areas that plainly EXCLUDE Ohio
_NON_OHIO_AREA = re.compile(
    r"\b(west coast|pacific (?:time|northwest|nw)|\bPST\b|\bPT\b|mountain time|\bMST\b|"
    r"mid-?atlantic|southeast|south[- ]?central|new england|northeast corridor|"
    r"bay area|silicon valley|seattle|san francisco|\bSF\b|new york|\bNYC\b|los angeles|"
    r"\bLA\b|austin|denver|boston|chicago|atlanta|miami|dallas|houston|phoenix|portland|"
    r"san diego|nashville|charlotte|washington,? d\.?c\.?|\bDC\b)\b", re.I)

_NON_US = re.compile(
    r"\b(london|united kingdom|\bUK\b|england|singapore|tokyo|japan|bengaluru|bangalore|"
    r"india|sydney|australia|\bANZ\b|\bAPAC\b|\bAPJ\b|\bEMEA\b|\bLATAM\b|\bCEE\b|toronto|"
    r"vancouver|canada|dublin|ireland|berlin|munich|germany|paris|france|amsterdam|"
    r"netherlands|tel aviv|israel|s[aã]o paulo|brazil|mexico city|mexico|seoul|korea|"
    r"madrid|spain|stockholm|sweden|zurich|switzerland|hong kong|shanghai|china)\b", re.I)

# ── requirement phrasings ──────────────────────────────────────────────────────
_RESIDENCE_REQ = re.compile(
    r"(?:must be (?:based|located)|must (?:live|reside)|required to (?:live|reside|be based)|"
    r"based in territory within|must be (?:a )?resident of|relocat\w+ to)\s+(?:the\s+)?([^.;,\n)]{2,60})",
    re.I)

_HYBRID_ONSITE = re.compile(
    r"hybrid (?:work|role|schedule|model|position|setup|environment|policy|approach)|"
    r"(?:work|office) is hybrid|hybrid,?\s*(?:remote|in[- ]?office|\d)|"
    r"\d+\s*days?\s*(?:a|per)?\s*week\s*(?:in|at)\s*(?:the\s*)?office|"
    r"(?:in|at)\s*(?:the\s*)?office\s*\d+\s*days?|"
    r"\d+\s*days?\s*(?:in|at)\s*(?:the\s*)?office|"
    r"on-?site (?:requirement|expectation|presence|\d+\s*days)|"
    r"in-?office (?:requirement|expectation|presence|\d+\s*days)|"
    r"return to office|\bRTO\b|"
    r"based (?:out of|in) (?:our|the) [A-Za-z ]{2,30}? (?:office|hub)", re.I)

# STRONG onsite: explicit, requirement-grade — kills regardless of a stray "remote"
# mention (these are not boilerplate). Distinct from the weaker _HYBRID_ONSITE,
# which a genuine remote signal can suppress.
_STRONG_ONSITE = re.compile(
    r"\d+\s*days?\s*(?:a|per)?\s*week\s*(?:in|at)\s*(?:the\s*)?office|"
    r"(?:in|at)\s*(?:the\s*)?office\s*\d+\s*days?\s*(?:a|per)?\s*week|"
    r"\d+\s*days?\s*(?:in|at)\s*(?:the\s*)?office|"
    r"\boffice\b[^.]{0,20}?\d+\s*days?\s*(?:a|per)?\s*week|"
    r"\d+\s*days?\s*(?:a|per)?\s*week[^.]{0,20}?\boffice\b|"
    r"relocation (?:assistance|required|provided|package|support|offered|benefits)|"
    r"required to relocate|must relocate|relocate to (?:our|the|[A-Z])|"
    r"return to office|\bRTO\b|must be (?:in|at) (?:the )?office", re.I)

_FULLY_REMOTE = re.compile(
    r"fully remote|100% remote|remote-first|work from anywhere|remote \((?:us|usa)\)|"
    r"this role is remote(?!\s*\((?:must|based))|us[- ]remote|remote[- ]us|distributed team|"
    r"can be (?:based )?remote(?:ly)? anywhere", re.I)

_TRAVEL_PCT = re.compile(r"travel\s*(?:up to|of|approximately|~)?\s*(\d{1,3})\s*%|(\d{1,3})\s*%\s*(?:of\s*the\s*time\s*)?travel", re.I)
_TRAVEL_FIELD = re.compile(
    r"customer site|client site|on-?site (?:delivery|training|deployment|visit)|field-?based|"
    r"field engineer|deskside|plant floor|shop floor|on-?site at (?:the )?(?:customer|client)|"
    r"in-?person (?:training|delivery|workshop|implementation)|trade ?show|conference booth|"
    r"\bbooth\b|customer/client site|valid (?:driver'?s? license|passport)|willing to travel|"
    r"ability to travel|travel required|frequent travel|extensive travel", re.I)
_TRAVEL_TEAM_EXEMPT = re.compile(
    r"team offsite|company (?:retreat|offsite|gathering)|annual (?:gathering|retreat|kickoff|offsite)|"
    r"quarterly (?:onsite|gathering|offsite)|occasional team", re.I)

_TRAVEL_THRESHOLD = 10  # percent; >= this fails (remote-first preference: no travel-heavy roles)


def _remote_flagged(remote_flag) -> bool:
    return str(remote_flag or "").strip().lower() in ("remote", "yes", "true", "us-remote", "remote-us", "1")


def _classify_area(area: str) -> str:
    """ohio_ok | non_ohio | unknown for a required-residence area string."""
    a = area or ""
    if _BASE_GEO.search(a) or _ELIGIBLE_REGIONS.search(a):
        return "ohio_ok"
    if _NON_US.search(a) or _NON_OHIO_AREA.search(a):
        return "non_ohio"
    return "unknown"


def location_travel_gate(*, title: str = "", body: str = "", location: str = "",
                         remote_flag=None) -> dict:
    """Deterministic mechanical gate. See module docstring for the contract."""
    body_c = _clean(body)
    loc_c = _clean(location)
    blob = f"{title}\n{loc_c}\n{body_c}"
    remote = (_remote_flagged(remote_flag) or bool(_FULLY_REMOTE.search(blob))
              or bool(re.search(r"\bremote\b", loc_c, re.I)))

 # ── TRAVEL (checked first; a travel kill is unambiguous) ────────────────────
    if not _TRAVEL_TEAM_EXEMPT.search(blob):
        m = _TRAVEL_PCT.search(blob)
        if m:
            pct = int(m.group(1) or m.group(2) or 0)
            if pct >= _TRAVEL_THRESHOLD:
                return {"status": "kill", "constraint": "travel_gate",
                        "reason": f"JD states ~{pct}% travel; exceeds remote-first preference (no travel-heavy roles)."}
        if _TRAVEL_FIELD.search(blob):
            hit = _TRAVEL_FIELD.search(blob).group(0)
            return {"status": "kill", "constraint": "travel_gate",
                    "reason": f"JD indicates customer-site/field/required travel ('{hit}'); conflicts with remote-first preference."}

 # ── LOCATION ────────────────────────────────────────────────────────────────
 # 1) Residence requirement — check Ohio-inclusion first.
    res = _RESIDENCE_REQ.search(blob)
    if res:
        area = res.group(1).strip()
        cls = _classify_area(area)
        if cls == "non_ohio":
            return {"status": "kill", "constraint": "location_gate",
                    "reason": f"Required residence excludes Ohio ('must be based in {area[:50]}'); operator does not relocate."}
        if cls == "ohio_ok":
            return {"status": "ok"}
 # unknown area + a hard "must be based in" conservative REACH (surface), not a kill
        return {"status": "reach_flag",
                "note": f"residence requirement names '{area[:50]}' — confirm Ohio eligibility with the operator."}

 # 2) Non-US-only location (no US/remote signal anywhere).
    us_signal = remote or re.search(r"\b(?:US|U\.S\.|USA|United States|remote)\b", blob, re.I)
    if _NON_US.search(f"{title}\n{loc_c}") and not us_signal:
        hit = _NON_US.search(f"{title}\n{loc_c}").group(0)
        return {"status": "kill", "constraint": "location_gate",
                "reason": f"Non-US-only location ('{hit}'); operator needs remote-US."}
 # non-US in location even WITH a weak remote flag but no fully-remote-US statement still kill
 # if the location is *primarily* a non-US city (e.g. 'London' with isRemote=true but UK-based)
    if _NON_US.search(loc_c) and not _FULLY_REMOTE.search(blob) and not re.search(r"\b(US|U\.S\.|USA|United States)\b", blob):
        hit = _NON_US.search(loc_c).group(0)
        return {"status": "kill", "constraint": "location_gate",
                "reason": f"Location is non-US ('{hit}') with no explicit US-remote eligibility."}

 # 3a) STRONG onsite (days-in-office, relocation, RTO) — kills regardless of a
 # stray 'remote' mention; these are explicit requirements, not boilerplate.
    if _STRONG_ONSITE.search(body_c):
        hit = _STRONG_ONSITE.search(body_c).group(0)
        return {"status": "kill", "constraint": "location_gate",
                "reason": f"Explicit on-site/relocation requirement ('{hit}'); not commutable from Ohio."}

 # 3b) Weaker hybrid mention — suppressed by a genuine remote signal (boilerplate).
    if _HYBRID_ONSITE.search(body_c) and not remote:
        hit = _HYBRID_ONSITE.search(body_c).group(0)
        return {"status": "kill", "constraint": "location_gate",
                "reason": f"Explicit on-site/hybrid requirement ('{hit}'); not within commuting range of Ohio."}

 # 4) Remote-flagged + US metros listed as hubs (no residence requirement) surface as REACH.
    if remote and _NON_OHIO_AREA.search(loc_c):
        return {"status": "reach_flag",
                "note": f"remote-flagged but lists US metro(s) ('{loc_c[:50]}') with no residence requirement — confirm full-remote with the operator."}

 # 5) Geography-ambiguous: a metro in the location, no remote/onsite stated surface, don't kill.
    if _NON_OHIO_AREA.search(loc_c) and not remote:
        return {"status": "reach_flag",
                "note": f"location lists '{loc_c[:50]}' with no remote/on-site statement — may be non-commutable; surface for the operator."}

 # 6) Clear pass.
    return {"status": "ok"}
