"""Offline unit tests for fit_judge.py.

Covers (no LLM, no network):
  1. TOML reader -> Constraints (load_constraints) — shape, types, world triples.
  2. Prompt builder — the keyword-substring guard (doc §4.1): world LABELS must
     NEVER appear in the rendered prompt; definitions must.
  3. Verdict parser — FIT / REACH / NOT-FIT, ordering (NOT-FIT before FIT),
     driving-constraint extraction, and the fail-safe to NOT-FIT on garbage.
  4. judge_listing fail-safe — when the LLM path raises (no API key in this env),
     the judge returns NOT-FIT/judge_error, never a false FIT.

Mirrors tests/test_safety_invariants.py: insert scripts/ on sys.path first.
"""
import sys
from pathlib import Path


from alice.pipeline import fit_judge  # noqa: E402


# ── 1. TOML reader ───────────────────────────────────────────────────────────
def test_load_constraints_shape():
    c = fit_judge.load_constraints()
    assert c.version == "operator-v3"
    assert c.remote_us_eligible is True
    assert c.travel_allowed is True
    assert c.travel_relaxes_on == ""
    assert c.comp_threshold_usd == 190000
    assert c.comp_hard_floor_usd == 150000
    assert c.functional_buckets["a"] == 1.0
    assert c.functional_buckets["b"] == 0.6
    assert 0.0 <= c.adjacency_coverage <= 1.0
    assert "founding_role" in c.seniority_selected
    assert "pure_marketing" in c.anti_fit_buckets


def test_load_constraints_domain_worlds_are_triples():
    c = fit_judge.load_constraints()
    assert len(c.domain_worlds) >= 8  # seed list is 12; guard against truncation
    for w in c.domain_worlds:
        assert w.label and isinstance(w.label, str)
        assert w.definition and len(w.definition) > 20
        assert isinstance(w.anti_examples, tuple)


# ── 2. Prompt builder — keyword-substring guard (doc §4.1) ───────────────────
def test_world_labels_never_in_prompt():
    c = fit_judge.load_constraints()
    system = fit_judge.build_judge_system(c)
    for w in c.domain_worlds:
        assert w.label not in system, (
            f"world label {w.label!r} leaked into the prompt — this reintroduces "
            f"the keyword-substring bug the redesign exists to kill (doc §4.1)")
        # the DEFINITION must be present (the model judges against it)
        assert w.definition in system


def test_prompt_includes_jd_body_and_domain():
    # Location/travel are the deterministic pre-gate's job
    # (scripts/location_gate.py + tests/test_location_gate.py), not the LLM prompt.
    # The prompt judges only domain/function/seniority/comp.
    c = fit_judge.load_constraints()
    system = fit_judge.build_judge_system(c)
    assert "ALREADY HANDLED" in system            # the pre-gate hand-off note
    assert "DOMAIN" in system and "FUNCTION" in system
    assert ("Do NOT evaluate location" in system or "do NOT emit location_gate" in system)
    prompt = fit_judge.build_judge_prompt(
        title="Applied AI Engineer", company="Acme",
        body="UNIQUE_JD_MARKER_42 build production agents",
        location="Remote", comp_low=120000, comp_high=160000, remote_flag=1)
    assert "UNIQUE_JD_MARKER_42" in prompt
    assert "$120,000" in prompt


# ── 3. Verdict parser ────────────────────────────────────────────────────────
def test_parse_fit():
    r = fit_judge.parse_verdict(
        "VERDICT: FIT\nCONSTRAINT: domain_fit\nOn-domain industrial AI, remote.")
    assert r["verdict"] == "FIT"
    assert r["driving_constraint"] == "domain_fit"
    assert "industrial" in r["reason"].lower()


def test_parse_reach():
    r = fit_judge.parse_verdict(
        "VERDICT: REACH\nCONSTRAINT: seniority\nGreat fit but 6yr SA bar is a stretch.")
    assert r["verdict"] == "REACH"
    assert r["driving_constraint"] == "seniority"


def test_parse_not_fit_ordering():
    # 'NOT-FIT' must not be mis-parsed as 'FIT' by substring.
    r = fit_judge.parse_verdict("VERDICT: NOT-FIT\nCONSTRAINT: travel_gate\n40% travel.")
    assert r["verdict"] == "NOT-FIT"
    assert r["driving_constraint"] == "travel_gate"


def test_parse_not_fit_space_variant():
    r = fit_judge.parse_verdict("VERDICT: NOT FIT\nCONSTRAINT: location_gate\nOnsite.")
    assert r["verdict"] == "NOT-FIT"


def test_parse_failsafe_on_garbage():
    # Unparseable -> NOT-FIT, never a false FIT (evals discipline).
    for junk in ["", "hello there", "I think this is a good role", None]:
        r = fit_judge.parse_verdict(junk)
        assert r["verdict"] == "NOT-FIT"
        assert r["driving_constraint"] == "parse_error"


def test_passing_verdicts_set():
    assert "FIT" in fit_judge.PASSING_VERDICTS
    assert "REACH" in fit_judge.PASSING_VERDICTS
    assert "NOT-FIT" not in fit_judge.PASSING_VERDICTS


# ── 4. judge_listing fail-safe (no API key in this env -> RuntimeError path) ──
def test_judge_listing_failsafe_without_keys(monkeypatch):
    # Force the LLM path to raise, simulating missing API key, and assert the
    # judge returns NOT-FIT/judge_error rather than a false FIT.
    from alice.pipeline import evals

    def _boom(*a, **k):
        raise RuntimeError("ANTHROPIC_API_KEY absent")

    monkeypatch.setattr(evals, "_judge", _boom)
    res = fit_judge.judge_listing(
        title="Senior AE", company="Acme", body="Some JD text",
        location="Remote", listing_id="job-1")
    assert res["verdict"] == "NOT-FIT"
    assert res["driving_constraint"] == "judge_error"
    assert res["company"] == "Acme"
    assert res["fit_model_version"] == "operator-v3"


def test_judge_survivors_attaches_fields(monkeypatch):
    from alice.pipeline import evals
    monkeypatch.setattr(
        evals, "_judge",
        lambda **k: "VERDICT: FIT\nCONSTRAINT: domain_fit\nGood.")
    survivors = [
        {"id": "a", "title": "TAM", "company": "Northwind Systems", "body": "industrial AI",
         "location": "Remote", "comp_low": 130000, "comp_high": 170000,
         "remote_flag": 1},
    ]
    out = fit_judge.judge_survivors(survivors)
    assert out[0]["fit_verdict"] == "FIT"
    assert out[0]["driving_constraint"] == "domain_fit"
    assert out[0]["fit_judge_model"]


def test_judge_survivors_drop_not_fit(monkeypatch):
    from alice.pipeline import evals
    monkeypatch.setattr(
        evals, "_judge",
        lambda **k: "VERDICT: NOT-FIT\nCONSTRAINT: travel_gate\nTravel.")
    survivors = [{"id": "a", "title": "FDE", "company": "X", "body": "40% travel"}]
    kept = fit_judge.judge_survivors(survivors, drop_not_fit=True)
    assert kept == []
    # but the rec is still annotated
    assert survivors[0]["fit_verdict"] == "NOT-FIT"


# ── 5. REACH refinement: geography-ambiguous → surface, don't hard-kill ───────
#
# Operator decision: an on-domain role whose JD states NO on-site/travel/relocation
# requirement, but whose listed location is non-commutable only on OUTSIDE
# knowledge the JD does not contain (the autonomous-robotics shape), must be REACH with
# a commute/relocate flag — surfaced to the operator — NOT silently hard-killed. A
# clear-in-JD disqualifier (explicit on-site, travel %, non-US) still kills.
#
# The verdict itself is the model's judgment (live harness: fit_judge_validation
# .py, needs ANTHROPIC_API_KEY). These offline tests cover the two hermetic
# halves: (a) the prompt instructs this routing, and (b) the pipeline surfaces a
# REACH (rather than cutting a NOT-FIT) on the geography-ambiguous shape.

# A Path-Robotics-shaped JD body: on-domain (robotics), and deliberately SILENT
# on on-site/hybrid/relocation/travel — the disqualifier is NOT in the JD.
_PATH_ROBOTICS_SHAPED_BODY = (
    "Trailhead Robotics builds autonomous robotic welding systems for manufacturing. "
    "As Product Manager, Technical Commercialization you will own the roadmap for "
    "turning our AI-driven welding platform into commercial product, working "
    "across engineering, sales, and customers. You will translate factory-floor "
    "needs into requirements and drive go-to-market for new capabilities. "
    "5+ years in product or technical commercialization for a hardware or "
    "industrial-AI product."
)  # note: no 'on-site', no 'hybrid', no 'relocate', no 'travel %' anywhere.


def test_geography_ambiguous_handled_by_gate_not_killed():
    """The geography-ambiguous routing — a metro listed with no on-site/travel/
    relocation stated in the JD must be surfaced, not silently hard-killed — is the
    deterministic pre-gate's job (location_gate), not the prompt. Verify the gate
    never returns 'kill' on the autonomous-robotics shape; full gate-policy coverage
    lives in tests/test_location_gate.py."""
    from alice.pipeline.location_gate import location_travel_gate
    g = location_travel_gate(
        title="Product Manager, Technical Commercialization",
        body=_PATH_ROBOTICS_SHAPED_BODY, location="Austin, TX", remote_flag=None)
    assert g["status"] != "kill"   # surfaced (ok / reach_flag), never silently cut
    assert "REACH" in fit_judge.PASSING_VERDICTS


def test_parse_reach_carries_commute_flag():
    """A Path-Robotics-shaped REACH response parses to REACH and the
    commute/relocate flag rides through in the reason (so the digest can show
    the operator WHY it is a reach, not just that it is)."""
    judge_text = (
        "VERDICT: REACH\n"
        "CONSTRAINT: location_gate\n"
        "On-domain robotics, no on-site or travel requirement stated in the JD, "
        "but Austin TX may be non-commutable from Columbus OH / relocation question "
        "— surface for the operator to adjudicate.")
    r = fit_judge.parse_verdict(judge_text)
    assert r["verdict"] == "REACH"            # not plain FIT, not NOT-FIT
    assert r["driving_constraint"] == "location_gate"
    assert "non-commutable" in r["reason"].lower()
    assert "relocation" in r["reason"].lower()
    assert r["verdict"] in fit_judge.PASSING_VERDICTS  # it surfaces


def test_geography_ambiguous_verdict_moved_surfaces_not_starved(monkeypatch):
    """Demonstrate the consequence of the verdict moving on the SAME input.

    OLD behavior (hard-kill on external geography) -> NOT-FIT -> CUT (starved).
    NEW behavior (the operator's decision) -> REACH + flag -> SURVIVES drop_not_fit.
    Same listing, drop_not_fit=True both times; the only thing that changes is the
    verdict, and the role goes from silently dropped to surfaced-with-reason."""
    from alice.pipeline import evals

    listing = {
        "id": "path_robotics_pm", "title": "Product Manager, Technical "
        "Commercialization", "company": "Trailhead Robotics",
        "body": _PATH_ROBOTICS_SHAPED_BODY, "location": "Austin, TX",
        "remote_flag": None,
    }

    # OLD: the silent-starvation behavior we are removing.
    monkeypatch.setattr(
        evals, "_judge",
        lambda **k: "VERDICT: NOT-FIT\nCONSTRAINT: location_gate\n"
                    "Austin is too far from Columbus to commute.")
    old = fit_judge.judge_survivors([dict(listing)], drop_not_fit=True)
    assert old == []  # starved: dropped from the digest

    # NEW: the operator's surface-and-annotate decision.
    monkeypatch.setattr(
        evals, "_judge",
        lambda **k: "VERDICT: REACH\nCONSTRAINT: location_gate\n"
                    "On-domain, no on-site/travel in JD, but Austin TX may be "
                    "non-commutable from Columbus OH / relocation question — surface "
                    "for the operator to adjudicate.")
    survivors = [dict(listing)]
    new = fit_judge.judge_survivors(survivors, drop_not_fit=True)
    assert len(new) == 1                          # surfaced, not starved
    assert new[0]["fit_verdict"] == "REACH"
    assert "non-commutable" in new[0]["fit_reason"].lower()
    assert new[0]["driving_constraint"] == "location_gate"


def test_clear_in_jd_onsite_still_killed(monkeypatch):
    """Guardrail on the refinement: a CLEAR-IN-JD disqualifier (explicit on-site)
    must still hard-kill. The refinement only spares the geography-ambiguous case,
    it does NOT weaken clear-in-JD location/travel kills."""
    from alice.pipeline import evals
    monkeypatch.setattr(
        evals, "_judge",
        lambda **k: "VERDICT: NOT-FIT\nCONSTRAINT: location_gate\n"
                    "JD states this role is on-site in Columbus, no remote option.")
    survivors = [{"id": "x", "title": "PM", "company": "Y",
                  "body": "This is an on-site role in Columbus, OH. No remote."}]
    kept = fit_judge.judge_survivors(survivors, drop_not_fit=True)
    assert kept == []
    assert survivors[0]["fit_verdict"] == "NOT-FIT"


# ── PM-title REACH cap ────────────────────────────────────────────────────────
def test_pm_title_cap_helper():
    f = fit_judge
    # non-commercial product roles -> capped
    assert f._is_noncommercial_pm_title("Senior Product Manager")
    assert f._is_noncommercial_pm_title("Technical Product Manager")
    assert f._is_noncommercial_pm_title("Product Owner")
    # commercial / GTM / technical-commercialization PMs -> exempt (CLAUDE.md target)
    assert not f._is_noncommercial_pm_title("Product Manager, Technical Commercialization")
    assert not f._is_noncommercial_pm_title("Commercial Product Manager")
    assert not f._is_noncommercial_pm_title("Product Manager, Growth")
    assert not f._is_noncommercial_pm_title("Group Product Manager, Monetization")
    # not a product-manager title -> never capped
    assert not f._is_noncommercial_pm_title("Senior Account Executive")
    assert not f._is_noncommercial_pm_title("Program Manager")


def test_gate_kill_sets_band_no_none():
    """Regression: the gate-kill early-return must carry band/dimensions too — it
    skips the model, but every result needs a band (7 roles were None before)."""
    r = fit_judge.judge_listing(
        title="Field Sales Engineer", company="Acme",
        body="This role requires 50% travel to customer sites across the region.",
        location="Remote", remote_flag="remote", listing_id="x")
    assert r["verdict"] == "NOT-FIT"
    assert r.get("band") == "NOT-FIT"          # not None
    assert "travel" in r.get("dimensions", {}).get("blockers", [])


def test_pm_title_cap_downgrades_fit_to_reach(monkeypatch):
    """A non-commercial PM the LLM judges FIT is deterministically capped to REACH;
    a commercial PM is exempt and stays FIT. Mirrors the live Path-Robotics finding."""
    from alice.pipeline import evals
    monkeypatch.setattr(evals, "_judge",
        lambda **k: "VERDICT: FIT\nCONSTRAINT: domain_fit\nStrong on-domain fit.")
    noncomm = fit_judge.judge_listing(
        title="Senior Product Manager", company="X",
        body="Fully remote US role building our core product.",
        location="Remote (US)", remote_flag="remote")
    assert noncomm["verdict"] == "REACH"
    assert noncomm["driving_constraint"] == "seniority"
    comm = fit_judge.judge_listing(
        title="Product Manager, Technical Commercialization", company="X",
        body="Fully remote US role owning commercialization and go-to-market.",
        location="Remote (US)", remote_flag="remote")
    assert comm["verdict"] == "FIT"


def test_parse_verdict_tolerates_markdown_emphasis():
    """Models commonly bold the verdict; markdown must not break parsing."""
    assert fit_judge.parse_verdict("VERDICT: **FIT**\nCONSTRAINT: none")["verdict"] == "FIT"
    assert fit_judge.parse_verdict("VERDICT: **NOT-FIT**")["verdict"] == "NOT-FIT"
    assert fit_judge.parse_verdict("VERDICT: `REACH`")["verdict"] == "REACH"
