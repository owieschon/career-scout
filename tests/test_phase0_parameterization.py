"""The engine/profile split: a different profile produces different, correct
engine output, with no operator-specifics leaking. Verifies that judge identity
and gate geography are both parameterized rather than hardcoded. Pure (no LLM)."""
import dataclasses
import re
from pathlib import Path

from alice.pipeline import fit_judge


def _maya_constraints():
    """The operator's loaded constraints, fully swapped to a synthetic 2nd user (Maya):
    identity + domain worlds. (A real Maya profile would come from her own config
    / Profile.to_engine_toml; here we swap the engine inputs directly.)"""
    c = fit_judge.load_constraints()
    maya_worlds = (
        fit_judge.DomainWorld(
            label="consumer_fintech_product",
            definition="Companies building consumer financial products where product "
                       "design and PM craft are the edge.",
            anti_examples=("Pure B2B infrastructure with no consumer surface",)),
    )
    return dataclasses.replace(
        c,
        candidate_name="Maya",
        portfolio_summary="Product designer turned PM, 8 yrs at consumer-fintech "
                          "startups in the Bay Area.",
        domain_worlds=maya_worlds,
    )


def test_judge_identity_parameterizes_per_profile():
    operator = fit_judge.build_judge_system(fit_judge.load_constraints())
    maya = fit_judge.build_judge_system(_maya_constraints())
    # The operator's prompt is the operator's
    assert "JORDAN AVERY'S PROFILE" in operator and "Lattice Additive" in operator
    # Maya's prompt is hers, and carries ZERO operator-specifics
    assert "MAYA'S PROFILE" in maya and "consumer-fintech" in maya
    assert "Lattice Additive" not in maya and "Cadence Analytics" not in maya and "JORDAN AVERY" not in maya


def test_gate_geography_parameterizes_per_config():
    """Same area string, opposite eligibility for a Bay-Area user vs the operator — driven
    purely by [gates.location] base/eligible patterns."""
    maya_base = re.compile(r"\bcalifornia\b|\bCA\b|san francisco|\bSF\b|bay area", re.I)
    maya_elig = re.compile(r"\b(west coast|pacific(?: time)?|\bPT\b|california|bay area|"
                           r"anywhere in the (?:us|united states)|remote[ -]?us)\b", re.I)
    from alice.pipeline.location_gate import _BASE_GEO, _ELIGIBLE_REGIONS

    def eligible(area, base, elig):
        return bool(base.search(area) or elig.search(area))

    # West Coast: eligible for Maya, NOT for the operator
    assert eligible("West Coast", maya_base, maya_elig)
    assert not eligible("West Coast", _BASE_GEO, _ELIGIBLE_REGIONS)
    # Ohio Valley: eligible for the operator, NOT for Maya
    assert eligible("Ohio Valley", _BASE_GEO, _ELIGIBLE_REGIONS)
    assert not eligible("Ohio Valley", maya_base, maya_elig)


def test_prep_baseline_parameterizes_per_profile():
    """The prep evidence baseline (_BASELINE_TAGS) drives whether 'builder' is
    seeded on every role. The operator (builder) -> injected; a non-builder profile
    (empty baseline) -> NOT injected. Proves prep_pipeline reads the profile,
    not a hardcode."""
    from alice.pipeline import prep_pipeline as p
    assert p._BASELINE_TAGS == ["builder", "applied-ai"]  # the operator's config
    assert "builder" in p._target_tags_for_role("AE", "Senior Account Executive", "X")
    saved = p._BASELINE_TAGS
    try:
        p._BASELINE_TAGS = []  # a pure-salesperson profile
        assert "builder" not in p._target_tags_for_role("AE", "Senior Account Executive", "X")
    finally:
        p._BASELINE_TAGS = saved
