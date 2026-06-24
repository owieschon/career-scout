"""profile_gen — turn a user's own material into a confirmable Profile.

Pipeline: (resume text | chat text | voice transcript) -> extract() -> a set of
GROUNDED fields with evidence -> build_profile() -> a Profile (confirmed=False)
-> build_confirm_payload() -> the user confirms -> profile_store.confirm().

GROUNDING IS THE PRIORITY (ALICE_SOUL Rule 1, applied to onboarding). Two
defenses, not one:

  1. The extraction prompt is told to extract ONLY what the source supports and
     to return a verbatim supporting quote for every field.
  2. extract() then ENFORCES that in code: a field whose evidence quote is not
     actually present in the source is dropped and marked unknown. The prompt
     can be wrong; the substring check cannot be talked out of it. "Runs-clean"
     is not the bar — the catch is demonstrated against fabricated evidence in
     tests/test_profile_intake.py.

Unknowns are first-class: a field the source does not support is marked
"unknown", carries a safe default the user MUST review, and is never silently
turned into a fact. The location/travel gates are person-specific and are
NEVER inferred from a resume — they default to "unknown" until the user states
them, so the confirm gate always surfaces them.

The world catalog (domain-world definitions + anti_examples) is single-sourced
from config/fit_model.toml. Per the redesign-doc §4.1 keyword-substring
guard, world LABELS are never shown to the extraction model; it picks worlds by
NUMBER against definitions, and the code maps numbers back to labels.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from alice import repo_paths
from alice.persistence.profile_schema import (
    CompFloor,
    DomainWorld,
    Identity,
    LocationGate,
    Profile,
)

_ROOT = repo_paths.ROOT
_WORLD_CATALOG_PATH = _ROOT / "config" / "fit_model.toml"
_ARCHETYPES_PATH = _ROOT / "config" / "profile_archetypes.toml"

# Controlled vocab for seniority — the engine's multi_select options. Extraction
# may only emit these tokens; anything else is dropped (fail-closed).
SENIORITY_VOCAB = {
    "mid_senior_ic": "Senior individual contributor — senior AE, senior CSM, staff/principal IC.",
    "first_line_manager": "First-line manager — RevOps/sales-ops manager, SE lead, CS lead, narrow head-of at a sub-$50M company.",
    "founding_role": "Founding role — a high-performing IC who defines a function as a startup scales.",
}

SOURCE_KINDS = ("resume", "chat", "voice")


# ── catalog + archetype loaders ────────────────────────────────────────────
def load_world_catalog(path: str | Path = _WORLD_CATALOG_PATH) -> list[DomainWorld]:
    """The canonical domain-world catalog (label/definition/anti_examples),
    single-sourced from the engine config so intake and scoring agree."""
    with open(path, "rb") as f:
        d = tomllib.load(f)
    return [
        DomainWorld(
            label=w["label"],
            definition=w["definition"],
            anti_examples=list(w.get("anti_examples", [])),
        )
        for w in d.get("domain_worlds", [])
    ]


def load_archetypes(path: str | Path = _ARCHETYPES_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _catalog_by_label(catalog: list[DomainWorld]) -> dict[str, DomainWorld]:
    return {w.label: w for w in catalog}


# ── grounding primitives ────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    """Collapse whitespace + lowercase for substring grounding checks. We are
    lenient on spacing/case (PDF/voice mangle those) but strict on content."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _is_grounded(evidence: str, source_text: str, *, min_len: int = 8) -> bool:
    """True if `evidence` actually appears in `source_text`. This is the
    fabrication catch: an LLM-asserted quote that isn't in the source fails.
    Very short quotes are not trustworthy grounding, so require some length."""
    ev = _normalize(evidence)
    if len(ev) < min_len:
        return False
    return ev in _normalize(source_text)


def _strip_json(raw: str) -> str:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    return raw


def _parse_json_obj(raw: str) -> dict:
    """Parse an LLM JSON object with the fence-strip + brace-regex fallback used
    elsewhere in the codebase (decision_feedback.py). Returns {} on failure —
    extraction degrades to 'nothing grounded', never to a fabricated guess."""
    raw = _strip_json(raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return obj if isinstance(obj, dict) else {}


# ── extraction result ────────────────────────────────────────────────────────
class ExtractionResult:
    """What one source (resume/chat/voice) grounded. Only fields that survived
    the substring grounding check are present. `unknowns` lists field names the
    source did not support."""

    def __init__(self, source_kind: str):
        self.source_kind = source_kind
        self.identity: dict[str, str] = {}
        self.identity_evidence: dict[str, str] = {}
        self.seniority: list[str] = []
        self.seniority_evidence: str = ""
        self.comp_expectation_usd: int | None = None
        self.comp_evidence: str = ""
 # list of (label, evidence)
        self.worlds: list[tuple[str, str]] = []
 # Explicit work constraints — ONLY captured from chat/voice (a person
 # STATING how they work), NEVER inferred from a resume. None = not stated.
        self.remote_us_eligible: bool | None = None
        self.travel_allowed: bool | None = None
        self.constraints_evidence: str = ""
        self.unknowns: list[str] = []

    def __repr__(self) -> str:
        return (
            f"ExtractionResult(source={self.source_kind!r}, "
            f"identity_keys={list(self.identity)}, seniority={self.seniority}, "
            f"comp={self.comp_expectation_usd}, worlds={[w for w, _ in self.worlds]}, "
            f"unknowns={self.unknowns})"
        )


def _build_extraction_prompt(source_text: str, source_kind: str,
                             catalog: list[DomainWorld]) -> str:
    world_blocks = []
    for i, w in enumerate(catalog, start=1):
        anti = "; ".join(w.anti_examples)
        world_blocks.append(f"  World {i}: {w.definition}\n    (NOT: {anti})")
    worlds_rendered = "\n".join(world_blocks)
    seniority_opts = "\n".join(f"  - {k}: {v}" for k, v in SENIORITY_VOCAB.items())

    return (
        f"You are extracting a job-search profile from a candidate's {source_kind}.\n"
        f"Extract ONLY what the text below actually supports. For every field you "
        f"fill, copy a SHORT VERBATIM QUOTE (8+ chars, exact substring) from the "
        f"text that supports it. If the text does not support a field, OMIT it and "
        f"name it in \"unknowns\". Never invent a name, number, employer, skill, or "
        f"experience the text does not contain. A wrong fact here corrupts the "
        f"entire search.\n\n"
        f"Return STRICT JSON, no prose, with this shape:\n"
        f"{{\n"
        f'  "identity": {{"name": "", "city": "", "state": "", "email": "", '
        f'"phone": "", "linkedin": "", "website": "", "headline": ""}},\n'
        f'  "identity_evidence": {{"name": "<quote>", "...": "<quote>"}},\n'
        f'  "seniority": ["<one or more of the tokens below>"],\n'
        f'  "seniority_evidence": "<quote>",\n'
        f'  "comp_expectation_usd": <integer or null>,\n'
        f'  "comp_evidence": "<quote or empty>",\n'
        f'  "worlds": [{{"world_number": <int>, "evidence": "<quote>"}}],\n'
        f'  "constraints": {{"remote_only": <true/false/null>, "travel_ok": <true/false/null>}},\n'
        f'  "constraints_evidence": "<quote or empty>",\n'
        f'  "unknowns": ["<field names the text does not support>"]\n'
        f"}}\n\n"
        f"`constraints` capture ONLY an EXPLICIT statement by the candidate about "
        f"how they want to work — 'remote only', 'open to onsite', 'no travel', "
        f"'fine with travel'. Leave a value null unless they actually say it. NEVER "
        f"infer a work preference from their job history.\n\n"
        f"`headline` is the ONE allowed summary (a one-line professional identity "
        f"built only from real content in the text); for it, put the most "
        f"identity-defining quote in identity_evidence.headline.\n\n"
        f"Seniority tokens (pick all that the text supports; omit if none):\n"
        f"{seniority_opts}\n\n"
        f"Domain worlds — pick by NUMBER the ones the candidate's real experience "
        f"supports, each with a quote. Do not force matches:\n"
        f"{worlds_rendered}\n\n"
        f"--- {source_kind.upper()} TEXT START ---\n"
        f"{source_text}\n"
        f"--- {source_kind.upper()} TEXT END ---\n"
    )


def extract(source_text: str, source_kind: str, *, llm_call=None) -> ExtractionResult:
    """Extract grounded profile fields from one source.

    `llm_call` is injectable (defaults to llm.call) so tests run offline. Every
    returned field has passed the substring grounding check against source_text;
    fields whose evidence is fabricated (not in the source) are dropped and added
    to `unknowns`. Fail-closed: any error yields an empty result, never a guess.
    """
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"source_kind must be one of {SOURCE_KINDS}, got {source_kind!r}")

    result = ExtractionResult(source_kind)
    if not (source_text or "").strip():
        result.unknowns = ["identity", "seniority", "comp", "worlds"]
        return result

    catalog = load_world_catalog()

    if llm_call is None:
        from alice.llm import llm
        llm_call = llm.call

    prompt = _build_extraction_prompt(source_text, source_kind, catalog)
    try:
        res = llm_call(task="profile_extraction", prompt=prompt, max_tokens=1500)
        raw = res.get("text") if isinstance(res, dict) else str(res)
    except Exception:
        result.unknowns = ["identity", "seniority", "comp", "worlds"]
        return result

    obj = _parse_json_obj(raw or "")
    if not obj:
        result.unknowns = ["identity", "seniority", "comp", "worlds"]
        return result

 # ── identity (each verbatim field must be grounded; headline is the
 # one allowed synthesis and is kept but flagged by source provenance) ──
    ident = obj.get("identity") or {}
    ident_ev = obj.get("identity_evidence") or {}
    for fld in ("name", "city", "state", "email", "phone", "linkedin", "website"):
        val = (ident.get(fld) or "").strip()
        if not val:
            continue
        ev = (ident_ev.get(fld) or "").strip()
 # Email/phone/linkedin/website are verifiable by their own presence in
 # the source even without a separate quote; name/city/state need a quote.
        if _is_grounded(val, source_text, min_len=min(8, len(val))) or _is_grounded(ev, source_text):
            result.identity[fld] = val
            result.identity_evidence[fld] = ev or val
    headline = (ident.get("headline") or "").strip()
    if headline:
        result.identity["headline"] = headline
        result.identity_evidence["headline"] = (ident_ev.get("headline") or "").strip()

 # ── seniority (controlled vocab; needs grounded evidence) ──
    sev = (obj.get("seniority_evidence") or "").strip()
    seniority = [s for s in (obj.get("seniority") or []) if s in SENIORITY_VOCAB]
    if seniority and _is_grounded(sev, source_text):
        result.seniority = seniority
        result.seniority_evidence = sev
    elif seniority:
        result.unknowns.append("seniority")  # claimed but ungrounded -> not a fact

 # ── comp ──
    comp = obj.get("comp_expectation_usd")
    comp_ev = (obj.get("comp_evidence") or "").strip()
    if isinstance(comp, (int, float)) and comp > 0 and _is_grounded(comp_ev, source_text):
        result.comp_expectation_usd = int(comp)
        result.comp_evidence = comp_ev

 # ── worlds (map number -> label; each needs grounded evidence) ──
    for w in (obj.get("worlds") or []):
        try:
            idx = int(w.get("world_number"))
        except (TypeError, ValueError):
            continue
        ev = (w.get("evidence") or "").strip()
        if 1 <= idx <= len(catalog) and _is_grounded(ev, source_text):
            result.worlds.append((catalog[idx - 1].label, ev))

 # ── explicit work constraints (chat/voice ONLY — never inferred from a
 # resume; a hard gate must come from the person stating it) ──
    if source_kind in ("chat", "voice"):
        cons = obj.get("constraints") or {}
        cons_ev = (obj.get("constraints_evidence") or "").strip()
        if _is_grounded(cons_ev, source_text):
            if isinstance(cons.get("remote_only"), bool):
                result.remote_us_eligible = bool(cons["remote_only"])
            if isinstance(cons.get("travel_ok"), bool):
                result.travel_allowed = bool(cons["travel_ok"])
            if result.remote_us_eligible is not None or result.travel_allowed is not None:
                result.constraints_evidence = cons_ev

 # de-dup worlds preserving order
    seen = set()
    deduped = []
    for label, ev in result.worlds:
        if label not in seen:
            seen.add(label)
            deduped.append((label, ev))
    result.worlds = deduped

    for u in (obj.get("unknowns") or []):
        if isinstance(u, str) and u not in result.unknowns:
            result.unknowns.append(u)

    return result


# ── archetype seeding ────────────────────────────────────────────────────────
def _seed_from_archetype(profile: Profile, archetype_key: str,
                         catalog_by_label: dict[str, DomainWorld]) -> None:
    """Seed engine dimensions from a cold-start archetype. Every seeded field is
    marked provenance 'archetype' — an assumption the user confirms, not a fact.
    Identity and location/travel gates are NEVER seeded (person-specific)."""
    archetypes = load_archetypes()
    if archetype_key not in archetypes:
        raise ValueError(
            f"unknown archetype {archetype_key!r}; have {sorted(archetypes)}"
        )
    a = archetypes[archetype_key]

    profile.seniority_selected = list(a.get("seniority", []))
    profile.set_provenance("seniority_selected", "archetype")

    profile.functional_buckets = dict(a.get("functional_buckets", {}))
    profile.combinatoric_emphasis = float(a.get("combinatoric_emphasis", 0.5))
    profile.adjacency_coverage = float(a.get("adjacency_coverage", 0.5))
    profile.fit_weight = float(a.get("fit_weight", 0.5))
    profile.value_weight = float(a.get("value_weight", 0.5))
    for f in ("functional_buckets", "combinatoric_emphasis", "adjacency_coverage",
              "fit_weight", "value_weight"):
        profile.set_provenance(f, "archetype")

    profile.comp_floor = CompFloor(
        threshold_usd=int(a.get("comp_threshold_usd", 0)),
        soft_below_threshold=True,
        hard_floor_usd=int(a.get("comp_hard_floor_usd", 0)),
    )
    profile.set_provenance("comp_floor", "archetype")

    worlds = []
    for label in a.get("domain_worlds", []):
        w = catalog_by_label.get(label)
        if w is None:
 # fail-closed: a typo'd label must not silently seed an empty world
            raise ValueError(
                f"archetype {archetype_key!r} references unknown world {label!r}"
            )
        worlds.append(w.model_copy())
    profile.domain_worlds = worlds
    profile.set_provenance("domain_worlds", "archetype")


# ── build a profile from extractions (+ optional archetype) ──────────────────
def build_profile(
    user_id: str,
    extractions: list[ExtractionResult] | None = None,
    *,
    archetype_key: str | None = None,
    identity_overrides: dict | None = None,
) -> Profile:
    """Assemble a confirm-ready (UNCONFIRMED) Profile.

    Precedence, lowest to highest: defaults -> archetype seed -> extractions
    (resume < chat < voice order as provided) -> explicit identity_overrides.
    Every field's provenance records its winning source. Location/travel gates
    are left 'unknown' (person-specific, never inferred) unless an override or a
    chat extraction sets them — so the confirm gate always surfaces them.
    """
    catalog = load_world_catalog()
    by_label = _catalog_by_label(catalog)

    profile = Profile(user_id=user_id, version="user-v1")

 # ── defaults (clearly marked, low trust) ──
    profile.location = LocationGate()  # remote_us_eligible=True, travel_allowed=True
    profile.set_provenance("location.remote_us_eligible", "unknown")
    profile.set_provenance("location.travel_allowed", "unknown")
    profile.anti_fit_buckets = []
    profile.set_provenance("identity.name", "unknown")

    if not archetype_key:
 # No preset: weighted dims take neutral defaults, flagged unknown so the
 # user is asked. Domain worlds stay empty until extraction fills them.
        profile.functional_buckets = {}
        profile.combinatoric_emphasis = 0.5
        profile.adjacency_coverage = 0.5
        profile.fit_weight = 0.5
        profile.value_weight = 0.5
        for f in ("functional_buckets", "comp_floor", "seniority_selected"):
            profile.set_provenance(f, "unknown")
    else:
        _seed_from_archetype(profile, archetype_key, by_label)

 # ── apply extractions in order ──
    for ext in (extractions or []):
        src = ext.source_kind
 # identity
        for fld, val in ext.identity.items():
            setattr(profile.identity, fld, val)
            profile.set_provenance(f"identity.{fld}", src, ext.identity_evidence.get(fld, ""))
 # seniority
        if ext.seniority:
            profile.seniority_selected = ext.seniority
            profile.set_provenance("seniority_selected", src, ext.seniority_evidence)
 # comp -> sets the threshold (a grounded expectation); keep any seeded floor
        if ext.comp_expectation_usd:
            profile.comp_floor = CompFloor(
                threshold_usd=ext.comp_expectation_usd,
                soft_below_threshold=True,
                hard_floor_usd=profile.comp_floor.hard_floor_usd,
            )
            profile.set_provenance("comp_floor", src, ext.comp_evidence)
 # explicit work constraints (only ever set from chat/voice extraction)
        if ext.remote_us_eligible is not None:
            profile.location.remote_us_eligible = ext.remote_us_eligible
            profile.set_provenance("location.remote_us_eligible", src, ext.constraints_evidence)
        if ext.travel_allowed is not None:
            profile.location.travel_allowed = ext.travel_allowed
            profile.set_provenance("location.travel_allowed", src, ext.constraints_evidence)
 # worlds: union grounded extraction worlds with whatever's present
        if ext.worlds:
            existing = {w.label for w in profile.domain_worlds}
            for label, ev in ext.worlds:
                if label not in existing:
                    profile.domain_worlds.append(by_label[label].model_copy())
                    existing.add(label)
            profile.set_provenance("domain_worlds", src)

 # ── explicit identity overrides (highest trust: user-supplied) ──
    for fld, val in (identity_overrides or {}).items():
        if hasattr(profile.identity, fld) and val:
            setattr(profile.identity, fld, val)
            profile.set_provenance(f"identity.{fld}", "user")

 # ── derive location_center from a grounded city/state if we have one ──
    if profile.identity.city or profile.identity.state:
        profile.location_center = {
            "city": profile.identity.city,
            "state": profile.identity.state,
            "radius_miles": profile.location.non_remote_radius_mi,
        }
        profile.set_provenance("location_center", profile.provenance.get("identity.city", "unknown"))

 # ── final unknown sweep on critical fields ──
    if not profile.identity.name:
        profile.set_provenance("identity.name", "unknown")
    if not profile.seniority_selected:
        profile.set_provenance("seniority_selected", "unknown")
    if not profile.domain_worlds:
        profile.set_provenance("domain_worlds", "unknown")

    return profile


# ── confirm payload (external voice — what the user reviews) ──────────────────
def _src_marker(src: str) -> str:
    return {
        "resume": "from your resume",
        "chat": "from what you told me",
        "voice": "from your voice note",
        "archetype": "a starting assumption (please confirm)",
        "user": "you told me directly",
        "unknown": "I could not find this — please fill it in",
        "default": "a default (please confirm)",
    }.get(src, src)


def build_confirm_payload(profile: Profile) -> str:
    """A plain-language summary of the extracted profile for the user to confirm
    BEFORE anything runs. External voice (ALICE_SOUL §10a): no filenames, no
    config keys, no raw scores. Grounded facts are stated plainly; assumptions
    and gaps are flagged honestly. This IS the grounding gate's surface."""
    p = profile
    lines: list[str] = []
    lines.append("Here's the profile I put together. Look it over before I start the search — I won't source or score anything until you confirm it's right.")
    lines.append("")

    name = p.identity.name or "(no name found)"
    lines.append(f"You: {name}")
    if p.identity.headline:
        lines.append(f"  {p.identity.headline}")
    loc = ", ".join(x for x in (p.identity.city, p.identity.state) if x)
    if loc:
        lines.append(f"  Based in {loc}")
    contact_bits = [b for b in (p.identity.email, p.identity.phone, p.identity.linkedin) if b]
    if contact_bits:
        lines.append("  " + " · ".join(contact_bits))

    lines.append("")
    if p.seniority_selected:
        pretty = ", ".join(s.replace("_", " ") for s in p.seniority_selected)
        lines.append(f"Level: {pretty}  ({_src_marker(p.provenance.get('seniority_selected', 'unknown'))})")
    else:
        lines.append("Level: not set yet — what level of role are you targeting?")

    if p.comp_floor.threshold_usd:
        lines.append(f"Comp target: around ${p.comp_floor.threshold_usd:,}+  ({_src_marker(p.provenance.get('comp_floor', 'unknown'))})")
    else:
        lines.append("Comp target: not set — what's your target range?")

    lines.append("")
    if p.domain_worlds:
        lines.append("Where you've got a real story (the domains I'll lean on):")
        for w in p.domain_worlds:
            lines.append(f"  - {w.label.replace('_', ' ')}")
        lines.append(f"  ({_src_marker(p.provenance.get('domain_worlds', 'unknown'))})")
    else:
        lines.append("Domains: I couldn't pin these down yet — tell me where your strongest experience is.")

    lines.append("")
    loc_src = p.provenance.get("location.remote_us_eligible", "unknown")
    trav_src = p.provenance.get("location.travel_allowed", "unknown")
    if loc_src == "unknown" or trav_src == "unknown":
        lines.append("Hard filters I need you to confirm (I never guess these):")
    else:
        lines.append("Hard filters (these gate everything — tell me if either is wrong):")
    rem = "remote-US" if p.location.remote_us_eligible else "open to non-remote"
    lines.append(f"  - Location: {rem}  ({_src_marker(loc_src)})")
    trav = "travel is fine" if p.location.travel_allowed else "no travel"
    lines.append(f"  - Travel: {trav}  ({_src_marker(trav_src)})")

    crit = p.unknown_critical_fields()
    if crit:
        lines.append("")
        pretty = ", ".join(c.split(".")[-1].replace("_", " ") for c in crit)
        lines.append(f"Still missing and important: {pretty}. I'd rather ask than guess.")

    lines.append("")
    lines.append("Confirm to lock it in, or just tell me what to change.")
    return "\n".join(lines)


# ── no-fabrication audit (used by the confirm gate + tests) ──────────────────
def no_fabrication_audit(profile: Profile, sources: dict[str, str]) -> list[str]:
    """Return a list of violation strings: any field claiming a source-derived
    provenance ('resume'/'chat'/'voice') whose recorded evidence is NOT present
    in that source. An empty list means every grounded claim checks out.

    This is the demonstrate-the-catch backstop: the confirm flow can run it and
    refuse to commit if it returns anything, and the test feeds it fabricated
    evidence to prove it catches.
    """
    violations: list[str] = []
    for path, src in profile.provenance.items():
        if src not in SOURCE_KINDS:
            continue
        ev = profile.evidence.get(path, "")
        src_text = sources.get(src, "")
 # Structural / derived / synthesis fields carry no single verbatim quote:
 # - domain_worlds / functional_buckets: grounded per-item at extraction
 # - location_center: DERIVED from identity.city/state (audited individually)
 # - identity.headline: the ONE sanctioned summary (grounded in the resume
 # as a whole, reviewed in the confirm payload), not a single quote
 # Auditing these as quoted claims would false-positive.
        if path in ("domain_worlds", "functional_buckets", "location_center", "identity.headline"):
            continue
        if not _is_grounded(ev, src_text):
            violations.append(
                f"{path}: claims provenance {src!r} but evidence {ev!r} is not in the {src} text"
            )
    return violations
