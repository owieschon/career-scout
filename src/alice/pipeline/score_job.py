"""
Score a job listing against kill criteria and fit dimensions.

Usage:
    python3 scripts/score_job.py              # interactive
    python3 scripts/score_job.py <json_file>  # score from JSON

Output: score 0-100, tier (tier1/tier2/tier3/killed), reasoning.

The scorecard is intentionally deterministic and transparent: it serves as a
structured input when evaluating listings, and its output is stored in the
`opportunities` table via `fit_score` and `fit_reasoning`.
"""
import json
import re
import sys
from pathlib import Path

KILL_COMPETITORS = {
    "gong", "clari", "people.ai", "setsail", "outreach", "salesloft",
    "groove", "aviso", "insightsquared", "troops", "madkudu",
    "chorus.ai", "drift"
}

TRACK_KEYWORDS = {
    "ae": ["account executive", "senior ae", "enterprise ae", "founding ae", "sales executive"],
    "revops": ["revenue operations", "sales operations", "revops", "sales ops", "revenue architect", "gtm ops"],
    "tam": ["technical account manager", "customer success", "implementation engineer", "solutions consultant"],
    "fde": ["forward deployed", "solutions engineer", "applied ai", "ai engineer", "implementation engineer"],
    "bridge": ["contract", "consultant", "fractional", "advisor"]
}

AI_NATIVE_COMPANIES = {
 # Foundation / frontier labs and established AI-native tools
    "anthropic", "openai", "quill code", "cascade ai", "octave ai", "lexicon ai", "hebbia",
    "lumen search", "perplexity", "cohere", "mistral", "contextual", "together",
    "scale", "deepseek", "inflection", "character.ai", "runway",
 # AI-native adjacent (added from pipeline review)
    "claro health", "quanta", "norden", "welby.ai", "forge parts", "open", "splittest", "datawell",
 # Pre-added for future sourcing — GTM/RevOps AI-native tool landscape
    "clay", "attio", "11x", "rox", "arena magic", "sybill", "cadenza",
    "apollo", "apollo.io", "unify", "default", "regal", "regal.ai",
}

# Adjacent industry bonuses (keyword-driven, independent of company name).
HEALTHCARE_KEYWORDS = [
    "healthcare", "clinical", "medical billing", "revenue cycle management",
    "rcm", "ehr ", "emr ", "patient data", "hospital operations",
]
FINTECH_BILLING_KEYWORDS = [
    "billing platform", "cpq", "quote-to-cash", "quote-to-revenue",
    "revenue infrastructure", "payments infrastructure", "invoicing platform",
    "usage metering", "dunning",
]
AGENTIC_AI_KEYWORDS = [
    "ai agents", "agentic", "autonomous ai", "ai safety layer",
    "ai infrastructure", "llm ops", "multi-agent", "ai agent",
    "agent framework",
]
B2B_DATA_INFRA_KEYWORDS = [
    "data integration", "etl pipeline", "data pipeline", "reverse etl",
    "webhook ingestion", "event store",
]
DEVELOPER_TOOLS_KEYWORDS = [
    "feature flagging", "feature flags", "a/b testing", "experimentation platform",
    "experimentation", "developer tools", "devtools", "developer platform",
    "developer-first", "product-led growth",
]

# ---------------------------------------------------------------------------
# Role archetype canonicalization. Different title variants (territory, vertical,
# seniority) at the same company collapse to the same archetype for dedupe.
# Order matters: more specific patterns first so "Head of RevOps" doesn't
# match bare "RevOps".

ARCHETYPE_RULES = [
    ("Sales Leadership", [
        "head of sales", "vp sales", "vp of sales", "vice president of sales",
        "chief revenue officer", " cro ", "founding ae", "founding account executive",
        "founding sales", "head of revenue", "head of gtm", "sales lead",
    ]),
    ("RevOps Leadership", [
        "head of revops", "head of revenue operations", "vp revops",
        "vp of revenue operations", "director of revops",
        "director of revenue operations",
    ]),
    ("RevOps", [
        "revenue operations", "revops", "sales operations", "sales ops",
        "revenue architect", "gtm engineer", "gtm ops", "growth engineer",
    ]),
    ("FDE", [
        "forward deployed", "applied ai", "implementation engineer",
    ]),
    ("Solutions Engineer", [
        "solutions engineer", "sales engineer", "strategic solutions engineer",
        "technical solutions", "solutions consultant", "pre-sales",
        "technical sales engineer", "technical sales engineering",
    ]),
    ("TAM / CS", [
        "technical account manager", "customer success", " csm ",
        "implementation specialist",
    ]),
    ("AE", [
        "account executive", "strategic account executive",
        "enterprise account executive", "mid market account executive",
        "commercial account executive", "senior ae", "enterprise ae",
        "smb account executive", "enterprise account manager",
    ]),
    ("Account Manager", [
        "account manager",  # fallback for roles not caught above
    ]),
]

LEADERSHIP_TITLE_MARKERS = ["head of", "vp ", "vp of", "vice president",
                            "director of", "chief ", "founding"]


def identify_archetype(role_title):
    """Canonicalize a messy role title to an archetype. Returns (archetype, is_leadership)."""
    t = " " + normalize(role_title) + " "
    for archetype, patterns in ARCHETYPE_RULES:
        for p in patterns:
            if p in t:
                is_leadership = any(m in t for m in LEADERSHIP_TITLE_MARKERS) or archetype.endswith("Leadership")
                return archetype, is_leadership
    return "Other", False


# ---------------------------------------------------------------------------
# Company-size preference: favor smaller companies.
# Size is a proxy for culture — encoded, not overridden.

COMPANY_SIZE_ESTIMATES = {
 # Hardcoded estimates for companies in the pipeline. Updated as sourced.
    "anthropic": 800,
    "openai": 2500,
    "quill code": 80,
    "cascade ai": 250,
    "octave ai": 60,
    "lexicon ai": 350,
    "perplexity": 200,
    "hebbia": 100,
    "lumen search": 300,
    "claro health": 150,
    "quanta": 10,
    "norden": 5,
    "welby.ai": 49,
    "forge parts": 25,
    "splittest": 25,
    "datawell": 12,
    "open": 30,
    "popl": 60,
}

# Frontier AI labs get a partial pass on the 2001+ hard-kill rule. Their culture
# remains startup-like despite headcount; the target profile treats these as a special case.
FRONTIER_AI_COMPANIES = {"anthropic", "openai", "google deepmind", "xai"}


def _parse_size_range(size_str):
    """Extract a rough employee count from a company_size string like '10-30', '150+', '25'."""
    s = (size_str or "").strip().lower()
    if not s:
        return None
 # Handle ranges like "10-30" take the midpoint
    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", s)
    if m:
        return (int(m.group(1)) + int(m.group(2))) // 2
 # "150+" take 150
    m = re.match(r"(\d+)\s*\+", s)
    if m:
        return int(m.group(1))
 # Pure number
    m = re.match(r"(\d+)", s)
    if m:
        return int(m.group(1))
 # Size descriptors
    if "small team" in s:
        return 15
    if "startup" in s:
        return 30
    return None


def estimate_company_size(listing):
    """Return a numeric employee estimate or None if unknown."""
    company = normalize(listing.get("company"))
    if company in COMPANY_SIZE_ESTIMATES:
        return COMPANY_SIZE_ESTIMATES[company]
    parsed = _parse_size_range(listing.get("company_size") or "")
    return parsed


def score_company_size(listing):
    """Return (score_delta, kill_flag, reason, size_estimate).
    kill_flag=True means this should force a hard kill unless frontier AI.
    """
    est = estimate_company_size(listing)
    company = normalize(listing.get("company"))
    is_frontier = company in FRONTIER_AI_COMPANIES

    if est is None:
        return 0, False, None, None

    if est <= 10:
        return -5, False, f"Very early (<10 employees)", est
    if est <= 150:
        return 10, False, f"Ideal company size ({est} employees, Series A to early B)", est
    if est <= 300:
        return 0, False, None, est  # acceptable, no penalty or bonus
    if est <= 750:
        return -10, False, f"Large company ({est} employees) — soft penalty", est
    if est <= 2000:
        return -20, False, f"Very large company ({est} employees) — manual review warranted", est
 # 2001+
    if is_frontier:
        return -10, False, f"Frontier AI lab ({est} employees) — exception applies", est
    return 0, True, f"Company too large ({est} employees, not frontier AI)", est


# ---------------------------------------------------------------------------
# Culture-fit scoring. Detects language patterns that signal cultural alignment
# with the target preferences: async, high-trust, anti-corporate, work-life boundaries.
# Counters hustle-culture / corporate / process-heavy language.

CULTURE_POSITIVE_PATTERNS = [
    ("Async / distributed", ["async", "async-first", "distributed team", "fully remote",
                             "remote-first", "work from anywhere"], 10),
    ("Trust-based leadership", ["high trust", "low bullshit", "no process theater",
                                 "don't count hours", "don't peek over your shoulder",
                                 "no bullshit", "low bullshit"], 15),
    ("Ownership language", ["real ownership", "own your work", "high autonomy",
                             "first principles", "full ownership"], 10),
    ("Anti-corporate", ["no kool-aid", "question everything", "no busy work",
                         "bias toward action", "don't drink the kool-aid"], 10),
    ("Ship culture", ["ship code and talk to users", "ship it then improve",
                       "shipping over slides", "shipping is the goal"], 5),
]

CULTURE_NEGATIVE_PATTERNS = [
    ("Hustle culture", ["4 months equals 1 year", "4 months = 1 year",
                         "not a 9-5", "hunter mentality", "relentless",
                         "grind culture", "high-performance culture",
                         "always on"], [(2, -15), (3, -20)]),
    ("Corporate language", ["cross-functional alignment", "strategic initiatives",
                             "stakeholder management", "mature organization",
                             "operational excellence"], [(2, -10)]),
    ("Process-heavy", ["heavy documentation", "multiple approvals",
                        "structured process"], [(1, -5)]),
    ("Always-on expectation", ["24/7 availability", "work hard, play hard",
                                "always available"], [(1, -15)]),
]


def score_culture_fit(listing):
    """Return (culture_score, detected_signals list) where detected_signals is
    a list of (signal_name, direction, contribution) tuples for transparency."""
    desc = normalize(listing.get("description", ""))
    title = normalize(listing.get("role_title", ""))
    text = desc + " " + title
    signals = []
    total = 0

    for label, keywords, bonus in CULTURE_POSITIVE_PATTERNS:
        hits = sum(1 for kw in keywords if kw in text)
        if hits >= 2 or (hits >= 1 and label in ("Async / distributed", "Ship culture")):
 # Most need 2+ hits; async/ship accept single strong signal
            amount = min(bonus, bonus * hits // 2) if hits < 2 else bonus
 # Actually just award full bonus once threshold met
            total += bonus
            signals.append((label, "+", bonus, hits))

    for label, keywords, thresholds in CULTURE_NEGATIVE_PATTERNS:
        hits = sum(1 for kw in keywords if kw in text)
 # thresholds is list of (min_hits, penalty); pick the highest triggered
        applied = 0
        for min_h, pen in thresholds:
            if hits >= min_h:
                applied = pen
        if applied:
            total += applied
            signals.append((label, "-", applied, hits))

    return total, signals
TECHNICAL_B2B_SAAS_KEYWORDS = [
    "b2b saas", "technical buyer", "technical decision-maker", "technical closer",
    "sell to engineers", "sell to cto", "sell to data teams",
    "mid-market b2b", "enterprise saas",
]
REMOTE_FIRST_CULTURE_KEYWORDS = [
    "remote-first", "async-first", "fully distributed", "distributed team",
    "work from anywhere", "no office", "async work",
]
OPERATOR_BUILDER_KEYWORDS = [
    "technical closer", "builder and a closer", "ai as leverage",
    "ship code solo", "full-cycle", "operator mindset",
    "builder mindset", "founder mindset",
]

# Explicit work-life-boundary language in the JD. Meaningful fit signal for
# the target work-life-boundary context — most AI-native JDs skew toward hustle-culture
# language. Companies that write about sustainable pace, boundaries, and whole
# humans are signaling a different operating posture.
WORK_LIFE_BOUNDARY_KEYWORDS = [
    "respect each other's boundaries", "respect boundaries", "whole human beings",
    "sustainable pace", "work-life balance", "work life balance",
    "not a hustle culture", "protect evenings", "purposeful momentum",
    "healthy pace", "without burning out", "no burnout culture",
]

# Named enterprise brands that appear in customer-traction statements. Two or
# more named brands = real enterprise traction signal (+5). Distinguishes
# "AI company with 2 logos in a deck" from "AI company with real customers."
ENTERPRISE_BRAND_NAMES = [
    "moneygram", "booking.com", "spacex", "lockheed martin", "apple", "tesla",
    "nasa", "microsoft", "google", "amazon", "stripe", "shopify", "databricks",
    "snowflake", "salesforce", "hubspot", "mollie", "viva.com", "fareharbor",
    "zoom", "atlassian", "slack", "adobe", "oracle", "airbnb", "uber", "lyft",
    "netflix", "spotify", "pinterest", "square", "plaid", "nvidia", "medtronic",
    "abbott", "meta", "siemens", "relativity space", "sandia",
]

# Content-based BDR detection. Two or more of these patterns present in the
# description mean the role is BDR-level regardless of title. Catches the
# "Business Development Manager / Founding BDR / Sales Lead" traps where the
# title signals seniority but the day-to-day work is pure prospecting.
BDR_CONTENT_PATTERNS = [
    "inbound lead management", "first point of contact", "coordinate demos",
    "book qualified meetings", "meetings booked", "high-volume outbound",
    "outbound cadences", "hand off qualified leads", "prospect into target accounts",
    "qualify and convert",
]
BDR_REQUIREMENT_PATTERNS = [
    "sdr/bdr", "sdr or bdr", "bdr/sdr", "1-2 years of bdr",
    "1 to 2 years of bdr", "1+ years of sdr", "sdr experience preferred",
    "bdr experience preferred", "inbound sales, sdr/bdr",
]

# Travel intensity rubric (1 = no travel, 5 = heavy / field sales).
# 4-5 are hard kills (travel-heavy roles are hard-excluded under the
# remote-first preference).
# 3 is a soft penalty and triggers the travel-disclosure flag.
TRAVEL_STRONG = [
 # Intensity 5: >50% travel, territory/field sales, road-warrior framing.
    "travel up to 60", "travel up to 70", "travel up to 80",
    "travel up to 75", "travel up to 100", "heavy travel", "frequent travel",
    "field sales", "regional sales manager", "territory account executive",
    "road warrior",
]
TRAVEL_MODERATE = [
 # Intensity 4: 20-50% explicit travel. Hard travel disclosure — not a kill.
    "travel up to 20", "travel up to 25", "travel up to 30",
    "travel up to 40", "travel up to 50",
    "must be willing to travel", "travel required",
    "willingness to travel", "willing to travel",
    "customer site visits required", "walk the factory floor",
    "on-site customer visits", "regular customer visits",
    "regular on-site visits", "% of the time in the field",
    "in-person presence", "sell in the field", "meet in person when needed",
]
TRAVEL_CULTURAL = [
    "quarterly offsite", "customer visits", "some travel",
    "occasional travel", "travel as needed",
    "meet with customers", "factory tours", "in-person collaboration",
    "periodic travel", "light travel",
]
TRAVEL_MINIMAL = [
    "annual offsite", "company retreat", "team offsite", "yearly offsite",
]

HARDWARE_OR_MFG_TERMS = [
    "manufacturing", "cnc", "machining", "injection molding", "sheet metal",
    "industrial hardware", "hardware startup", "robotics", "physical product",
    "factory", "supply chain hardware", "additive manufacturing", "3d printing",
]


def normalize(s):
    return (s or "").lower().strip()


def _is_small_hardware_or_mfg(text, size):
    size_small = ("1-10" in size or "11-50" in size or "10-30" in size or
                  "20-50" in size or "under 30" in size or "under 50" in size or
                  "small team" in text)
 # Also handle raw numeric sizes (e.g. "25" or "49 employees").
    if not size_small:
        try:
            digits = "".join(c for c in size if c.isdigit())
            if digits and int(digits[:3]) <= 50:
                size_small = True
        except ValueError:
            pass
    is_hw = any(term in text for term in HARDWARE_OR_MFG_TERMS)
    return size_small and is_hw


def score_travel_intensity(listing):
    """
    Return (intensity, signals) where intensity is 1-5 and signals is a list of
    the keyword triggers that drove the score. 4-5 = hard kill tier.
    """
    title = normalize(listing.get("role_title"))
    desc = normalize(listing.get("description"))
    size = normalize(listing.get("company_size"))
    text = f"{title} {desc}"
    signals = []

    for kw in TRAVEL_STRONG:
        if kw in text:
            signals.append(f"strong travel signal: '{kw}'")
            return 5, signals

    for kw in TRAVEL_MODERATE:
        if kw in text:
            signals.append(f"moderate travel signal: '{kw}'")
            return 4, signals

    for kw in TRAVEL_CULTURAL:
        if kw in text:
            signals.append(f"cultural travel signal: '{kw}'")
            if _is_small_hardware_or_mfg(text, size):
                signals.append("small hardware/mfg team amplifies travel likelihood (stays Path 2, not killed)")
            return 3, signals

    for kw in TRAVEL_MINIMAL:
        if kw in text:
            signals.append(f"minimal travel signal: '{kw}'")
            if _is_small_hardware_or_mfg(text, size):
                signals.append("small hardware/mfg team — offsite more likely to be hands-on")
                return 3, signals
            return 2, signals

    if _is_small_hardware_or_mfg(text, size):
        signals.append("small hardware/mfg team — travel likelihood elevated even without explicit signal")
        return 3, signals

    return 1, ["no travel signals"]


def identify_track(role_title, description=""):
    text = normalize(role_title + " " + description)
    for track, keywords in TRACK_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return track
    return "other"


def score_listing(listing):
    """
    Expects a dict with keys:
        company, role_title, description, location, remote_policy,
        base_salary_low, base_salary_high, interview_rounds (optional),
        company_size, company_stage
    Returns dict with: score, tier, track, kills (list), penalties (list), bonuses (list), reasoning
    """
    kills = []
    penalties = []
    bonuses = []
    score = 50  # baseline

    company = normalize(listing.get("company"))
    title = normalize(listing.get("role_title"))
    desc = normalize(listing.get("description", ""))
    remote = normalize(listing.get("remote_policy"))
    location = normalize(listing.get("location"))

 # HARD KILLS
    if company in KILL_COMPETITORS:
        kills.append(f"Direct competitor to Cadence Analytics: {company}")

    if remote and remote not in ("remote", "fully remote", "100% remote", "remote-first", "remote only"):
        if "hybrid" in remote or "in-office" in remote or "on-site" in remote or "onsite" in remote:
            kills.append(f"Not fully remote: {listing.get('remote_policy')}")

    if any(term in title for term in ["sdr", "bdr", "sales development", "business development representative"]):
        kills.append("SDR/BDR role — step backward")

 # Content-based BDR detection. If the title looks senior but the description
 # reads like pure BDR work OR the requirements list SDR/BDR as acceptable
 # prior experience, treat the role as BDR-level regardless of title.
    bdr_content_hits = sum(1 for kw in BDR_CONTENT_PATTERNS if kw in desc)
    bdr_requirement_hits = sum(1 for kw in BDR_REQUIREMENT_PATTERNS if kw in desc)
    if bdr_content_hits >= 2 or bdr_requirement_hits >= 1:
        markers = []
        if bdr_content_hits >= 2:
            markers.append(f"{bdr_content_hits} BDR workflow patterns")
        if bdr_requirement_hits >= 1:
            markers.append(f"SDR/BDR listed as acceptable prior role")
        kills.append(f"Content-based BDR detection: {'; '.join(markers)}")

    if any(term in desc for term in ["must have valid driver", "driving required", "driver's license required"]):
        kills.append("Driving required")

 # Travel intensity rubric:
 # 5 = hard kill (>50% travel, field sales, territory role)
 # 4 = hard travel disclosure — explicit 20-50% travel in JD; penalty -15
 # 3 = cultural travel disclosure — occasional visits, offsite at small hw team; penalty -10
 # 2 = annual offsite only; penalty -5 (no disclosure required)
 # 1 = no travel expectation; no penalty
    travel_intensity, travel_signals = score_travel_intensity(listing)
    travel_disclosure_needed = False
    travel_disclosure_type = None
    if travel_intensity >= 5:
        kills.append(f"Travel intensity 5/5 ({'; '.join(travel_signals)})")
    elif travel_intensity == 4:
        penalties.append((
            f"Travel intensity 4/5 — explicit travel in JD, hard travel disclosure ({'; '.join(travel_signals)})",
            -15,
        ))
        travel_disclosure_needed = True
        travel_disclosure_type = "hard"
    elif travel_intensity == 3:
        penalties.append((
            f"Travel intensity 3/5 — cultural travel, travel disclosure ({'; '.join(travel_signals)})",
            -10,
        ))
        travel_disclosure_needed = True
        travel_disclosure_type = "cultural"
    elif travel_intensity == 2:
        penalties.append(("Travel intensity 2/5 — annual offsite only", -5))

 # Comp — NO HARD FLOOR. Preferred TOTAL comp band $100K-$250K. Comp is a
 # weighted PREFERENCE, not a gate — nothing is killed for being below the
 # band. Reachable comp (the band's upper bound) below $100K accrues a GRADED
 # penalty (~1 pt per $1K below $100K, capped at -50) that trades off against
 # fit, so a strong-fit low-comp role still surfaces (ranked by the tradeoff)
 # while a weak-fit low-comp role sinks via ranking rather than a wall. The
 # only comp-adjacent kill is the $250K ceiling — a SENIORITY signal (role too
 # senior for the senior-IC -> first-line-manager target), not a comp floor.
 # base_salary_* hold whatever band the posting disclosed (base or OTE).
 # base_low > $250K -> kill (above-ceiling seniority signal)
 # reachable < $100K -> graded penalty (~$1K below = 1 pt, max -50)
 # base_low in [$130K, $250K] -> +5 bonus (squarely in preferred band)
 # base_low < $100K <= reachable -> -3 (band straddles the preference point)
 # base_low unknown -> no adjustment (can't evaluate)
    base_low = listing.get("base_salary_low")
    base_high = listing.get("base_salary_high")
    negotiation_required = False
    if base_low is not None:
        reachable = base_high if base_high is not None else base_low
        if base_low > 250000:
            kills.append(f"Comp band starts ${base_low:,} — above $250K ceiling (too senior per calibration)")
        elif reachable < 100000:
            gap = 100000 - reachable
            pen = min(50, max(1, round(gap / 1000)))
            penalties.append((f"Comp tops at ${reachable:,} — ${gap:,} below the preferred $100K band", -pen))
            if base_high is not None and base_high >= 130000:
                negotiation_required = True
        elif base_low >= 130000 and reachable <= 250000:
            bonuses.append(("Comp squarely in $100-250K band", 5))
        elif base_low < 100000:
            penalties.append((f"Comp starts ${base_low:,} (bottom of band; top ${reachable:,} clears $100K)", -3))
            if base_high is not None and base_high >= 130000:
                negotiation_required = True

    if "commission only" in desc or "100% commission" in desc:
        kills.append("Commission-only compensation")

    rounds = listing.get("interview_rounds")
    if rounds is not None and rounds >= 6:
        kills.append(f"{rounds} interview rounds — too costly")

 # SOFT PENALTIES
    size = normalize(listing.get("company_size", ""))
    if "1-10" in size or "under 10" in size:
        penalties.append(("Company under 10 employees", -10))
    elif "5000+" in size or "10000+" in size:
        penalties.append(("Company over 5000 employees", -5))

 # BONUSES
    if company in AI_NATIVE_COMPANIES:
        bonuses.append(("AI-native company", 25))

    if any(term in desc for term in ["manufacturing", "industrial", "supply chain", "b2b hardware"]):
        bonuses.append(("Manufacturing/industrial vertical fit", 15))

    if any(term in desc for term in TECHNICAL_B2B_SAAS_KEYWORDS):
        bonuses.append(("Technical B2B SaaS fit", 15))

    if any(term in desc for term in REMOTE_FIRST_CULTURE_KEYWORDS):
        bonuses.append(("Remote-first team culture", 10))

    if any(term in desc for term in OPERATOR_BUILDER_KEYWORDS):
        bonuses.append(("Operator-builder-friendly language", 10))

    if any(term in desc for term in WORK_LIFE_BOUNDARY_KEYWORDS):
        bonuses.append(("Explicit work-life-boundary values", 10))

 # Enterprise customer traction: two or more named enterprise brands in the
 # description signals real traction, not pitch-deck fluff.
    brand_hits = sum(1 for brand in ENTERPRISE_BRAND_NAMES if brand in desc)
    if brand_hits >= 2:
        bonuses.append((f"Enterprise customer traction ({brand_hits} named brands)", 5))

 # Adjacent-industry fits where operator-builder + technical fluency + buyer
 # knowledge transfer well, even without direct domain experience.
    if any(term in desc for term in HEALTHCARE_KEYWORDS):
        bonuses.append(("Healthcare SaaS / RCM vertical fit", 10))

    if any(term in desc for term in FINTECH_BILLING_KEYWORDS):
        bonuses.append(("Fintech / billing infrastructure fit", 10))

    if any(term in desc for term in AGENTIC_AI_KEYWORDS):
        bonuses.append(("Agentic AI / AI infrastructure fit", 15))

    if any(term in desc for term in B2B_DATA_INFRA_KEYWORDS):
        bonuses.append(("B2B data infrastructure fit", 10))

    if any(term in desc for term in DEVELOPER_TOOLS_KEYWORDS):
        bonuses.append(("Developer tools / experimentation platform fit", 10))

    stage = normalize(listing.get("company_stage", ""))
    if "seed" in stage or "series a" in stage:
        if any(term in title for term in ["head of", "founding", "first"]):
            bonuses.append(("Founder-adjacent role at early-stage company", 15))

 # Seniority ceiling: target is senior IC -> first-line manager. VP/Head-of/
 # Sr-Director titles usually exceed the target title trajectory and the
 # $250K band. Penalize unless it's a founding / first commercial hire at a
 # seed/Series-A company (the one acceptable exception).
    _title_pad = " " + title + " "
    _too_senior_title = any(m in _title_pad for m in [
        " vp ", " vp,", " svp ", " evp ", "vice president", "head of",
        " chief ", "chief revenue officer",
    ]) or (
        ("senior director" in title or "director of" in title or "managing director" in title)
        and "analyst" not in title  # analyst-firm "Director Analyst" is an IC role, not management
    )
    _early_founding = ("seed" in stage or "series a" in stage) and any(
        m in title for m in ["founding", "first commercial", "head of revenue", "head of sales"])
    if _too_senior_title and not _early_founding:
        penalties.append(("Title above target seniority (VP/Head-of/Sr-Director) vs senior-IC-to-manager target", -20))

 # Culture-fit scoring (language-based signal detection).
    culture_score, culture_signals = score_culture_fit(listing)

 # Company-size scoring. 2001+ triggers kill unless frontier AI.
    size_delta, size_kill, size_reason, size_estimate = score_company_size(listing)
    if size_kill:
        kills.append(f"Company-size kill: {size_reason}")

 # Size/culture interaction: strong culture signal (+15 or more) reduces the
 # soft penalty for 151-750-employee companies by 50%. Strong negative
 # culture stacks with size penalty.
    if size_delta is not None and size_delta < 0 and size_estimate and 150 < size_estimate <= 750:
        if culture_score >= 15:
            size_delta = int(size_delta * 0.5)
            size_reason = (size_reason or "") + " (halved — strong culture signal offsets)"
    if size_delta and size_reason:
        if size_delta >= 0:
            bonuses.append((size_reason, size_delta))
        else:
            penalties.append((size_reason, size_delta))

 # Apply culture score last, wrapped as a single bonus/penalty line.
    if culture_score:
        label = f"Culture-fit signal ({'; '.join(f'{s[0]} {s[1]}' for s in culture_signals[:3])})"
        if culture_score > 0:
            bonuses.append((label, culture_score))
        else:
            penalties.append((label, culture_score))

 # Apply. Cap at 120 (not 100) so exceptional fits can rank above merely
 # strong ones — tier thresholds stay the same (≥80 = tier-1).
    score += sum(b[1] for b in bonuses)
    score += sum(p[1] for p in penalties)
    score = max(0, min(120, score))

 # If any hard kill, force score to 0
    if kills:
        score = 0
        tier = "killed"
    elif score >= 80:
        tier = "tier1"
    elif score >= 60:
        tier = "tier2"
    elif score >= 40:
        tier = "tier3"
    else:
        tier = "parked"

    track = identify_track(listing.get("role_title", ""), listing.get("description", ""))
    archetype, leadership = identify_archetype(listing.get("role_title", ""))

    reasoning_parts = []
    if kills:
        reasoning_parts.append("KILLS: " + "; ".join(kills))
    if bonuses:
        reasoning_parts.append("BONUSES: " + "; ".join(f"{b[0]} (+{b[1]})" for b in bonuses))
    if penalties:
        reasoning_parts.append("PENALTIES: " + "; ".join(f"{p[0]} ({p[1]})" for p in penalties))

    return {
        "score": score,
        "tier": tier,
        "track": track,
        "archetype": archetype,
        "leadership_track": leadership,
        "kills": kills,
        "bonuses": [b[0] for b in bonuses],
        "penalties": [p[0] for p in penalties],
        "travel_intensity": travel_intensity,
        "travel_signals": travel_signals,
        "travel_disclosure_needed": travel_disclosure_needed,
        "travel_disclosure_type": travel_disclosure_type,
        "negotiation_required": negotiation_required,
        "culture_fit_score": culture_score,
        "culture_signals": [f"{s[0]} ({s[1]}{abs(s[2])})" for s in culture_signals],
        "company_size_estimate": size_estimate,
        "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Neutral baseline."
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            listing = json.load(f)
    else:
        print("Interactive mode. Enter listing details (blank to skip numeric fields):")
        listing = {
            "company": input("Company: ").strip(),
            "role_title": input("Role title: ").strip(),
            "description": input("Paste description (single line, or blank): ").strip(),
            "location": input("Location: ").strip(),
            "remote_policy": input("Remote policy (remote/hybrid/onsite): ").strip(),
        }
        try:
            b = input("Base salary low ($): ").strip()
            listing["base_salary_low"] = int(b) if b else None
        except ValueError:
            listing["base_salary_low"] = None
        listing["company_size"] = input("Company size (e.g., 50-200, 1000+): ").strip()
        listing["company_stage"] = input("Company stage (seed, series a, public, etc.): ").strip()

    result = score_listing(listing)
    print("\n" + "=" * 60)
    print(f"COMPANY: {listing.get('company')}")
    print(f"ROLE: {listing.get('role_title')}")
    print(f"SCORE: {result['score']}/100 → {result['tier'].upper()}")
    print(f"TRACK: {result['track']}")
    print(f"TRAVEL: intensity {result['travel_intensity']}/5 — disclosure_needed={result['travel_disclosure_needed']}")
    print(f"REASONING: {result['reasoning']}")
    print("=" * 60)
