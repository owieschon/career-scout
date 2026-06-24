"""Offline tests for the multi-modal intake -> profile pipeline.

No LLM, no network: the LLM call and the voice transcriber are injected. Covers:
  1. Profile <-> engine-config round-trip (the schema-is-the-contract proof).
  2. Extraction DROPS fabricated evidence (the grounding catch, demonstrated
     against a hallucinating model, not just asserted to exist).
  3. The confirm gate: a draft is invisible to the engine; only confirm opens it.
  4. no_fabrication_audit catches a tampered profile.
  5. Archetypes load + seed.
  6. End-to-end: a synthetic new user -> confirmed, working profile.
  7. Resume variants derive from an uploaded resume (incl. thin-track honesty).
  8. to_engine_toml_dict is consumable by the same loader shape the engine uses.

Mirrors the repo test convention: put scripts/ on sys.path first.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

from alice.pipeline import fit_judge  # noqa: E402
from alice.persistence import generate_resume_variants  # noqa: E402
from alice.persistence import intake  # noqa: E402
from alice.persistence import profile_gen  # noqa: E402
from alice.persistence import profile_schema  # noqa: E402
from alice.persistence import profile_store  # noqa: E402
from alice.persistence.profile_schema import Profile  # noqa: E402

CONFIG = SCRIPTS.parent / "config" / "fit_model.toml"


# ── synthetic source material ────────────────────────────────────────────────
SYNTH_RESUME = """\
Jordan Avery
Columbus, OH | jordan.avery@example.com | (614) 555-0100 | linkedin.com/in/jordanavery

Summary: Senior account executive with 8 years selling industrial automation
software to manufacturers.

Experience:
- Senior Account Executive, Acme Industrial Software (2019-2026). Closed $4M ARR
  across factory-floor manufacturing accounts. Managed technical deployments and QBRs.
- Account Executive, Widgets Inc (2016-2019).

Skills: Salesforce, manufacturing, technical sales, customer success.
"""

SYNTH_CHAT = "I'm fully remote only, no travel, and targeting around 135k."


def _resume_extraction_json(*, with_fabrication=False):
    """Canned extraction the stub model 'returns' for the resume. When
    with_fabrication=True it includes an ungrounded world + an ungrounded
    website, which the grounding check must drop."""
    worlds = [
        {"world_number": 1, "evidence": "factory-floor manufacturing accounts"},
        {"world_number": 4, "evidence": "Managed technical deployments and QBRs"},
    ]
    identity = {
        "name": "Jordan Avery", "city": "Columbus", "state": "OH",
        "email": "jordan.avery@example.com", "phone": "(614) 555-0100",
        "linkedin": "linkedin.com/in/jordanavery", "website": "",
        "headline": "Senior account executive selling industrial automation software to manufacturers",
    }
    identity_ev = {
        "name": "Jordan Avery", "city": "Columbus, OH", "state": "Columbus, OH",
        "email": "jordan.avery@example.com", "phone": "(614) 555-0100",
        "linkedin": "linkedin.com/in/jordanavery",
        "headline": "Senior account executive with 8 years selling industrial automation",
    }
    if with_fabrication:
        # World 9 = aerospace_rocketry_defense — NOT supported by the resume.
        worlds.append({"world_number": 9, "evidence": "led aerospace rocket launches at a major aerospace OEM"})
        identity["website"] = "github.com/totally-made-up"
        identity_ev["website"] = "github.com/totally-made-up"  # not in resume
    return {
        "identity": identity,
        "identity_evidence": identity_ev,
        "seniority": ["mid_senior_ic"],
        "seniority_evidence": "Senior Account Executive, Acme Industrial Software",
        "comp_expectation_usd": None,
        "comp_evidence": "",
        "worlds": worlds,
        "unknowns": ["comp"],
    }


def _chat_extraction_json():
    return {
        "identity": {}, "identity_evidence": {},
        "seniority": [], "seniority_evidence": "",
        "comp_expectation_usd": 135000,
        "comp_evidence": "targeting around 135k",
        "worlds": [],
        "constraints": {"remote_only": True, "travel_ok": False},
        "constraints_evidence": "fully remote only, no travel",
        "unknowns": [],
    }


def make_stub_llm(*, with_fabrication=False):
    """A stub llm.call. Branches on which source the prompt is extracting and on
    the resume-variant task; returns canned JSON / markdown."""
    def _call(task=None, prompt="", max_tokens=1024, **kwargs):
        if task == "resume_variant_derive":
            ok = "TRACK_OK"
            # senior-ae has lots to stand on; tam is thin in this resume
            if "customer-success or post-sales" in prompt:
                ok = "THIN_TRACK: little post-sales ownership in the source"
            return {"text": f"# Jordan Avery\nSenior AE\n\n## Experience\n- Acme Industrial Software\n\n{ok}"}
        # profile_extraction: decide by the source-kind line in the prompt
        if "CHAT TEXT START" in prompt:
            return {"text": json.dumps(_chat_extraction_json())}
        if "VOICE TEXT START" in prompt:
            return {"text": json.dumps(_chat_extraction_json())}
        return {"text": json.dumps(_resume_extraction_json(with_fabrication=with_fabrication))}
    return _call


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Point profile + template storage at a temp dir so tests never touch the
    real state/ or templates/."""
    monkeypatch.setattr(profile_store, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(generate_resume_variants, "TEMPLATES_DIR", tmp_path / "templates")
    yield


# ── 1. round-trip ──────────────────────────────────────────────────────────────
def test_profile_round_trip_with_engine_config():
    prof = Profile.from_engine_toml(CONFIG)
    c = fit_judge.load_constraints(str(CONFIG))
    assert prof.version == c.version  # round-trip preserves the engine-config version (version-agnostic, survives operator-vN bumps)
    assert prof.location.remote_us_eligible == c.remote_us_eligible
    assert prof.location.travel_allowed == c.travel_allowed
    assert prof.location.travel_relaxes_on == c.travel_relaxes_on
    assert prof.comp_floor.threshold_usd == c.comp_threshold_usd == 190000
    assert prof.comp_floor.hard_floor_usd == c.comp_hard_floor_usd == 150000
    assert list(prof.seniority_selected) == list(c.seniority_selected)
    assert prof.functional_buckets == c.functional_buckets
    assert len(prof.domain_worlds) == len(c.domain_worlds)
    assert {w.label for w in prof.domain_worlds} == {w.label for w in c.domain_worlds}


def test_to_engine_toml_dict_is_consumable():
    """A profile's engine dict must re-load through the same shape the engine
    reads — proving a generated profile plugs into the SAME engine."""
    prof = Profile.from_engine_toml(CONFIG)
    d = prof.to_engine_toml_dict()
    reloaded = Profile.from_engine_toml_dict(d)
    assert reloaded.to_engine_toml_dict() == d
    # and the domain-world labels survive intact (the contract the engine consumes)
    assert {w.label for w in reloaded.domain_worlds} == {w.label for w in prof.domain_worlds}


# ── 2. grounding catch ──────────────────────────────────────────────────────────
def test_extraction_keeps_grounded_fields():
    ext = profile_gen.extract(SYNTH_RESUME, "resume", llm_call=make_stub_llm())
    assert ext.identity["name"] == "Jordan Avery"
    assert ext.seniority == ["mid_senior_ic"]
    labels = [w for w, _ in ext.worlds]
    assert "industrial_manufacturing" in labels
    assert "technical_account_mgmt" in labels


def test_extraction_drops_fabricated_evidence():
    """The model hallucinates an aerospace world and a fake website; neither
    quote is in the resume, so both must be dropped. This is the catch."""
    ext = profile_gen.extract(SYNTH_RESUME, "resume",
                              llm_call=make_stub_llm(with_fabrication=True))
    labels = [w for w, _ in ext.worlds]
    assert "aerospace_rocketry_defense" not in labels, "fabricated world survived grounding"
    assert "website" not in ext.identity, "ungrounded website survived grounding"
    # the real grounded fields still came through
    assert ext.identity["name"] == "Jordan Avery"
    assert "industrial_manufacturing" in labels


def test_headline_synthesis_does_not_block_intake():
    """The headline is the one sanctioned summary; a paraphrased (non-verbatim)
    headline must not trip the grounding audit and block the whole intake."""
    def paraphrased_headline(task=None, prompt="", max_tokens=1024, **k):
        obj = _resume_extraction_json()
        obj["identity"]["headline"] = "Industrial software seller who runs technical deployments"
        obj["identity_evidence"]["headline"] = "a paraphrase not verbatim in the resume"
        return {"text": json.dumps(obj)}
    out = intake.run_intake("u_head", resume_text=SYNTH_RESUME, llm_call=paraphrased_headline)
    assert out["profile"].identity.headline.startswith("Industrial software seller")


def test_no_fabrication_audit_catches_tampered_profile():
    prof = profile_gen.build_profile(
        "u_audit",
        [profile_gen.extract(SYNTH_RESUME, "resume", llm_call=make_stub_llm())],
    )
    # clean profile passes
    assert profile_gen.no_fabrication_audit(prof, {"resume": SYNTH_RESUME}) == []
    # tamper: claim a resume-grounded name with evidence not in the resume
    prof.set_provenance("identity.name", "resume", "this quote is not in the resume at all")
    violations = profile_gen.no_fabrication_audit(prof, {"resume": SYNTH_RESUME})
    assert any("identity.name" in v for v in violations)


# ── 3 + 6. confirm gate + end-to-end ─────────────────────────────────────────────
def test_confirm_gate_blocks_until_confirmed():
    user = "u_gate"
    out = intake.run_intake(
        user, resume_text=SYNTH_RESUME, chat_text=SYNTH_CHAT,
        archetype_key="senior_ae", llm_call=make_stub_llm(),
    )
    assert out["profile"].confirmed is False
    # engine-facing gate: a draft is invisible
    assert profile_store.load_active(user) is None
    with pytest.raises(profile_store.ProfileNotConfirmed):
        profile_store.require_active(user)
    # confirm opens it
    confirmed = intake.confirm_intake(user)
    assert confirmed.confirmed is True
    active = profile_store.load_active(user)
    assert active is not None
    assert active.identity.name == "Jordan Avery"


def test_end_to_end_synthetic_user():
    user = "u_e2e"
    out = intake.run_intake(
        user, resume_text=SYNTH_RESUME, chat_text=SYNTH_CHAT,
        archetype_key="senior_ae", llm_call=make_stub_llm(),
    )
    prof = out["profile"]
    # grounded identity from the resume
    assert prof.identity.name == "Jordan Avery"
    assert prof.identity.email == "jordan.avery@example.com"
    # seniority grounded from resume (overrides archetype seed)
    assert prof.seniority_selected == ["mid_senior_ic"]
    assert prof.provenance["seniority_selected"] == "resume"
    # comp grounded from chat
    assert prof.comp_floor.threshold_usd == 135000
    assert prof.provenance["comp_floor"] == "chat"
    # worlds: archetype seed unioned with grounded resume worlds, >=1
    labels = {w.label for w in prof.domain_worlds}
    assert "industrial_manufacturing" in labels
    assert "technical_account_mgmt" in labels
    # confirm payload is plain language (external voice): no filenames/keys
    assert "Jordan Avery" in out["confirm_text"]
    for leak in (".py", ".toml", "provenance", "world_number", "comp_floor"):
        assert leak not in out["confirm_text"], f"internal token {leak!r} leaked into confirm text"
    # commit and verify the engine can read it
    intake.confirm_intake(user)
    active = profile_store.require_active(user)
    assert active.confirmed and active.confirmed_at


def test_location_travel_left_unknown_not_inferred():
    """Location/travel are person-specific; a RESUME must never set them. They
    stay 'unknown' so the confirm gate surfaces them."""
    out = intake.run_intake("u_loc", resume_text=SYNTH_RESUME, llm_call=make_stub_llm())
    prof = out["profile"]
    assert prof.provenance["location.remote_us_eligible"] == "unknown"
    assert prof.provenance["location.travel_allowed"] == "unknown"
    assert "Hard filters I need you to confirm" in out["confirm_text"]


def test_constraints_captured_from_chat_only():
    """An EXPLICIT chat statement ('remote only, no travel') sets the hard gate,
    grounded, provenance 'chat'. A resume never does."""
    out = intake.run_intake("u_con", resume_text=SYNTH_RESUME, chat_text=SYNTH_CHAT,
                            llm_call=make_stub_llm())
    prof = out["profile"]
    assert prof.location.remote_us_eligible is True
    assert prof.location.travel_allowed is False
    assert prof.provenance["location.travel_allowed"] == "chat"
    assert prof.provenance["location.remote_us_eligible"] == "chat"


def test_resume_constraints_are_ignored():
    """Even if a model returns constraints on a RESUME extraction, they are not
    honored — a hard gate must come from an explicit statement, not job history."""
    def resume_with_constraints(task=None, prompt="", max_tokens=1024, **k):
        obj = _resume_extraction_json()
        obj["constraints"] = {"remote_only": False, "travel_ok": True}
        obj["constraints_evidence"] = "factory-floor manufacturing accounts"  # real substring, wrong field
        return {"text": json.dumps(obj)}
    ext = profile_gen.extract(SYNTH_RESUME, "resume", llm_call=resume_with_constraints)
    assert ext.remote_us_eligible is None
    assert ext.travel_allowed is None


# ── 5. archetypes ────────────────────────────────────────────────────────────────
def test_archetypes_load_and_seed():
    arche = profile_gen.load_archetypes()
    assert set(arche) >= {"revops_operator", "ai_engineer", "senior_ae", "tam_cs"}
    prof = profile_gen.build_profile("u_arch", [], archetype_key="ai_engineer")
    assert "founding_role" in prof.seniority_selected
    assert prof.provenance["seniority_selected"] == "archetype"
    assert len(prof.domain_worlds) >= 1
    # every seeded world resolves to a real catalog world (no empty/typo worlds)
    catalog = {w.label for w in profile_gen.load_world_catalog()}
    assert {w.label for w in prof.domain_worlds} <= catalog


def test_archetype_unknown_label_fails_loud(monkeypatch):
    bad = {"broken": {"seniority": ["mid_senior_ic"], "domain_worlds": ["no_such_world"]}}
    monkeypatch.setattr(profile_gen, "load_archetypes", lambda *a, **k: bad)
    with pytest.raises(ValueError):
        profile_gen.build_profile("u_bad", [], archetype_key="broken")


# ── 7. resume variants ───────────────────────────────────────────────────────────
def test_resume_variants_derive_with_thin_flag():
    variants = generate_resume_variants.derive_variants(
        SYNTH_RESUME, "u_var", llm_call=make_stub_llm(), write=True,
    )
    assert set(variants) == set(generate_resume_variants.TRACKS)
    assert variants["senior-ae"]["thin"] is False
    assert variants["senior-ae"]["markdown"]
    # tam track flagged thin honestly, and the marker is stripped from the file
    assert variants["tam"]["thin"] is True
    assert "THIN_TRACK" not in variants["senior-ae"]["markdown"]
    assert "TRACK_OK" not in variants["senior-ae"]["markdown"]
    assert Path(variants["senior-ae"]["path"]).exists()


def test_auto_derive_variants_after_intake():
    """run_intake persists sources; derive_variants_for re-reads the resume and
    produces variants (the 'auto-derived from the uploaded resume' DoD)."""
    user = "u_auto"
    intake.run_intake(user, resume_text=SYNTH_RESUME, llm_call=make_stub_llm())
    assert profile_store.load_sources(user).get("resume")
    variants = intake.derive_variants_for(user, llm_call=make_stub_llm())
    assert set(variants) == set(generate_resume_variants.TRACKS)
    assert variants["senior-ae"]["markdown"]


def test_derive_variants_noop_without_resume():
    """Chat/voice-only onboarding: no resume -> no variants, no error."""
    user = "u_novar"
    intake.run_intake(user, chat_text=SYNTH_CHAT, archetype_key="senior_ae",
                      llm_call=make_stub_llm())
    assert intake.derive_variants_for(user, llm_call=make_stub_llm()) == {}


def test_cancel_clears_draft_and_sources():
    user = "u_cancel"
    intake.run_intake(user, resume_text=SYNTH_RESUME, llm_call=make_stub_llm())
    assert profile_store.load_draft(user) is not None
    assert intake.cancel_intake(user) is True
    assert profile_store.load_draft(user) is None
    assert profile_store.load_sources(user) == {}


def test_intake_requires_some_source():
    with pytest.raises(intake.IntakeError):
        intake.run_intake("u_empty", llm_call=make_stub_llm())


def test_parse_resume_rejects_unknown_type(tmp_path):
    bad = tmp_path / "resume.rtf"
    bad.write_text("hi")
    with pytest.raises(intake.IntakeError):
        intake.parse_resume(bad)


def test_voice_uses_injected_transcriber(tmp_path):
    """Voice path with an injected transcriber needs no network/key."""
    voice_file = tmp_path / "note.ogg"
    voice_file.write_bytes(b"fake-audio")
    out = intake.run_intake(
        "u_voice", voice_path=str(voice_file),
        transcriber=lambda p: SYNTH_CHAT, llm_call=make_stub_llm(),
    )
    # chat-shaped extraction grounds the comp number from the transcript
    assert out["profile"].comp_floor.threshold_usd == 135000
    assert out["profile"].provenance["comp_floor"] == "voice"


def test_voice_missing_key_fails_loud(monkeypatch):
    with pytest.raises(intake.IntakeError):
        intake.transcribe_voice("/tmp/x.ogg", cfg={})


# ── 8. telegram confirm-callback parsing (pure) ─────────────────────────────────
def test_confirm_callback_parsing():
    from alice.notify import intake_telegram as it
    assert it.parse_confirm_callback("pf:commit:12345") == ("commit", "12345")
    assert it.parse_confirm_callback("pf:cancel:javery") == ("cancel", "javery")
    # user_id may itself contain a colon-free string; only the first split matters
    assert it.parse_confirm_callback("pf:commit:u_e2e") == ("commit", "u_e2e")
    # not ours / malformed
    assert it.parse_confirm_callback("conf:abc:1") is None
    assert it.parse_confirm_callback("pf:bogus:1") is None
    assert it.parse_confirm_callback("pf:commit:") is None
    assert it.parse_confirm_callback("") is None
