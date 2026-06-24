"""Dimensional fit layer (docs/FIT_STRATEGY_SPINE.md): structured label + derived
band via deterministic caps, wrapping the unchanged judge. Pure (no LLM)."""
from pathlib import Path

from alice.pipeline import fit_dimensions as fd


def _band(title, company, body, jv, **kw):
    return fd.compute(title=title, company=company, body=body, judge_verdict=jv, **kw)


# ── caps: judge FIT downgraded to REACH ───────────────────────────────────────
def test_pure_build_caps_to_reach():
    d = _band("Software Engineer - AI Productivity", "Hightouch", "remote us, data activation", "FIT")
    assert d["role_archetype"] == "pure_build" and d["band"] == "REACH"


def test_too_senior_caps_to_reach():
    d = _band("Head of AI Enablement", "J.D. Power", "lead ai strategy, 12+ years", "FIT")
    assert d["seniority_fit"] == "too_senior" and d["band"] == "REACH"


def test_off_thesis_caps_to_reach():
    d = _band("Senior Associate, Revenue Strategy", "Mercury", "fintech banking platform", "FIT")
    assert d["domain"] == "off_thesis" and d["band"] == "REACH"


def test_noncommercial_pm_caps_to_reach():
    d = _band("Product Manager", "Linear", "own the product roadmap", "FIT")
    assert d["role_archetype"] == "pm_noncommercial" and d["band"] == "REACH"


# ── intersection / bridge / commercial stay FIT ───────────────────────────────
def test_bridge_stays_fit():
    d = _band("GTM Systems Engineer", "Syncari", "revenue operations systems, gtm stack", "FIT")
    assert d["role_archetype"] == "bridge" and d["band"] == "FIT"


def test_commercial_pm_not_capped():
    # a technical-commercialization PM reads as bridge (intersection-y), NOT the
    # capped pm_noncommercial — the point is it stays FIT, not which label wins.
    d = _band("Product Manager, Technical Commercialization", "Trailhead Robotics",
              "robotics manufacturing, go-to-market", "FIT")
    assert d["role_archetype"] in ("commercial", "bridge")
    assert d["role_archetype"] != "pm_noncommercial"
    assert d["band"] == "FIT"


def test_applied_ai_founder_adjacent_stays_fit():
    d = _band("Founding Full Stack / Applied AI Engineer", "Cora AI", "seed stage, yc, applied ai", "FIT")
    assert d["role_archetype"] == "bridge"
    assert d["seniority_fit"] == "founder_adjacent"
    assert d["company_archetype"] == "early_founder_led"
    assert d["band"] == "FIT"


# ── Forge Parts regression: reality says FIT, judge said anti_fit ────────────────────
def test_forge_parts_regression_not_anti_fit():
    """Ground truth: cold email -> VP -> interview in 48h, qualified.
    The dim layer must surface it FIT (commercial, on-thesis, no blocker) and route
    to cold-email — never anti_fit."""
    d = _band("Account Executive", "Forge Parts",
              "manufacturing procurement marketplace, yc startup, remote us", "FIT")
    assert d["domain"] == "on_thesis"
    assert d["role_archetype"] == "commercial"
    assert d["company_archetype"] == "early_founder_led"
    assert d["band"] == "FIT"
    assert d["channel"] == "cold_email_decision_maker"
    assert "anti_fit" not in d["blockers"]


# ── blockers force NOT-FIT ─────────────────────────────────────────────────────
def test_nonrole_blocker():
    d = _band("Account Executive Talent Pool US", "Loopwork", "talent pool general application", "FIT")
    assert "nonrole" in d["blockers"] and d["band"] == "NOT-FIT"


def test_competitor_blocker():
    d = _band("Account Executive", "Gong", "revenue intelligence platform", "FIT")
    assert "competitor" in d["blockers"] and d["band"] == "NOT-FIT"


def test_competitor_no_substring_false_positive():
    """Regression: competitor must match the EMPLOYER name word-boundaried, never a
    substring in the JD body ('clarify'/'groove'/'troops'). Caught false-cutting
    Cobalt Automation/Webflow/Canals in the 83 re-grade."""
    for co, body in [("Cobalt Automation", "we clarify manufacturing ops; clarity matters"),
                     ("Webflow", "get into the groove of building"),
                     ("Canals", "deploy troops of agents")]:
        d = _band("Customer Project Manager", co, body, "FIT")
        assert "competitor" not in d["blockers"], f"{co} false-fired competitor"


def test_travel_gate_blocker():
    d = _band("Field Sales Engineer", "Acme", "travel 50%", "FIT",
              gate_status="kill", gate_constraint="travel_gate")
    assert "travel" in d["blockers"] and d["band"] == "NOT-FIT"


# ── caps never UPGRADE; never silently cut on attainability ───────────────────
def test_caps_never_upgrade():
    # judge REACH stays REACH even for a clean intersection role (caps only downgrade)
    d = _band("Solutions Engineer", "Haulix", "logistics delivery platform", "REACH")
    assert d["band"] == "REACH"


def test_enterprise_archetype_routes_warm_not_cut():
    d = _band("Enterprise Account Executive", "Oracle",
              "fortune 500 global leader, nasdaq", "FIT")
    # enterprise is low channel-accessibility but NOT auto-cut (inform-not-gate)
    assert d["company_archetype"] == "enterprise"
    assert d["band"] in ("FIT", "REACH")  # not NOT-FIT from archetype alone
    assert d["channel"] == "warm_intro_preferred"


# comp_high (>$250K) = REACH (the operator reaches for pay), not NOT-FIT
def test_comp_high_is_reach_not_kill():
    d = _band("Senior Solutions Engineer", "Acme", "industrial B2B, remote us", "FIT",
              comp_low=300000, comp_high=350000)
    assert "comp_high" in d["blockers"]
    assert d["band"] == "REACH"        # surfaced, not cut
