"""Regression suite for the deterministic location/travel gate (see
docs/DECISION_LOG.md). Covers the policy cases the gate enforces. Pure (no LLM,
no I/O).

The gate's contract: location_travel_gate(...) -> {"status": "kill"|"reach_flag"|"ok"}.
  - kill       : a clear-in-JD disqualifier (conflicts with the remote-first, no-relocation preference)
  - reach_flag : ambiguous (metro listed, unknown residence) -> surface as REACH
  - ok         : location/travel clearly fine -> the LLM judges freely
Conservative by design: ambiguity NEVER yields a silent kill.
"""
from pathlib import Path

import pytest
from alice.pipeline.location_gate import location_travel_gate as gate


# (name, expected_status, kwargs)
CASES = [
    # Ohio-inclusive / broad-eastern residence -> ELIGIBLE (ok)
    ("ohio_valley_lists_ohio", "ok",
     dict(body="This role is remote (must be located in Ohio Valley: Ohio, Michigan, Tennessee).",
          location="Remote - US", remote_flag="remote")),
    ("us_east_coast_operator_eligible", "ok",
     dict(body="This role is remote (must be located in US East Coast).",
          location="Remote - US", remote_flag="remote")),
    ("midwest_region", "ok",
     dict(body="Remote, must be based in the Midwest.", location="Remote - US", remote_flag="remote")),
    ("soft_region_pref", "ok",
     dict(body="Remote, US East Coast preferred, work from anywhere in the US.",
          location="Remote - US", remote_flag="remote")),
    ("fully_remote_no_location", "ok",
     dict(body="Fully remote, work from anywhere in the US.", location="Remote", remote_flag="remote")),
    ("team_offsite_only_not_travel", "ok",
     dict(body="Fully remote. We gather for a quarterly team offsite.",
          location="Remote - US", remote_flag="remote")),

    # Residence requirement that EXCLUDES Ohio -> kill
    ("mid_atlantic_enumerated", "kill",
     dict(body="This role is remote (must be based in the Mid-Atlantic: DC/DE/MD/PA/VA/WV).",
          location="Remote - US", remote_flag="remote")),
    ("must_be_based_seattle", "kill",
     dict(body="This role is remote (must be based in Seattle).", location="Remote", remote_flag="remote")),
    ("southeast_territory", "kill",
     dict(body="based in territory within the Southeast", location="Charlotte, NC", remote_flag="")),

    # Non-US -> kill
    ("london_non_us", "kill",
     dict(body="Join our London team.", location="London", remote_flag="remote")),

    # Travel -> kill
    ("twenty_pct_travel", "kill",
     dict(body="The role might include up to 20% travel to customers.", location="Remote", remote_flag="remote")),

    # Explicit onsite (kills regardless of a stray remote mention)
    ("hybrid_3_days_office", "kill",
     dict(body="This is a hybrid role, 3 days a week in our Mountain View office.",
          location="Mountain View, CA", remote_flag="")),
    ("hybrid_work_model_days_office_relocation", "kill",
     dict(body="We use a hybrid work model of 3 days in the office per week and offer relocation assistance.",
          location="San Francisco", remote_flag="")),
    ("office_n_days_word_order", "kill",   # "work from an office 4 days per week" (Watershed shape)
     dict(body="Must be willing to work from an office 4 days per week (except for remote roles).",
          location="San Francisco", remote_flag="")),

    # Remote-flagged + metros-as-hubs / metro-no-remote -> surface (reach_flag), never kill
    ("remote_with_sf_nyc_hubs", "reach_flag",
     dict(body="We are remote-friendly with hubs.", location="San Francisco / New York City",
          remote_flag="remote")),
    ("metro_listed_no_remote_stated", "reach_flag",
     dict(body="We build great software.", location="Austin, TX", remote_flag="")),

    # False-kill guards: 'hybrid' as a ROLE blend / boilerplate must NOT kill a remote role
    ("role_hybrid_not_worktype", "ok",
     dict(body="Remote in these states incl OH. This role is a hybrid of project manager + builder.",
          location="Remote - United States", remote_flag="remote")),
    ("remote_with_boilerplate_hybrid", "ok",
     dict(body="Remote-US role. #LI-REMOTE. Benefits include a hybrid-friendly culture.",
          location="Remote - US", remote_flag="remote")),
]


@pytest.mark.parametrize("name,expected,kw", CASES, ids=[c[0] for c in CASES])
def test_gate_policy(name, expected, kw):
    got = gate(**kw)["status"]
    assert got == expected, f"{name}: gate returned {got!r}, expected {expected!r}"


def test_kill_results_carry_constraint_and_reason():
    r = gate(body="must be based in Seattle.", location="Remote", remote_flag="remote")
    assert r["status"] == "kill"
    assert r["constraint"] in ("location_gate", "travel_gate")
    assert r.get("reason")


def test_ambiguous_never_silently_kills():
    # The core conservative invariant: a bare metro with no explicit disqualifier
    # must surface (reach_flag), not kill.
    for loc in ["San Francisco", "New York", "Austin, TX", "Seattle / Remote"]:
        assert gate(body="Great role.", location=loc, remote_flag="remote")["status"] != "kill"


def test_full_time_travel_is_killed():
    """100%-travel roles must be killed; the percent regex previously capped at
    two digits and silently passed '100%'."""
    for body in ("Requires travel 100% of the time.",
                 "Up to 100% travel.",
                 "This role requires 100% travel across the territory."):
        r = gate(body=body, location="Remote", remote_flag="remote")
        assert r["status"] == "kill" and r["constraint"] == "travel_gate", \
            f"100% travel must kill: {body!r} -> {r}"
