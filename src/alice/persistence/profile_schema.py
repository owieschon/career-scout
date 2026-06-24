"""Profile schema — the intake/engine contract.

THE PROFILE IS THE CONTRACT between intake and the matcher engine. This module
defines the pydantic shape; it does not rewire the engine. The schema MIRRORS
the dimensions in config/fit_model.toml so a new user's profile slots into the
SAME engine that scores Jordan Avery today.

Mapping to fit_model.toml (and to fit_judge.Constraints, which is the
typed read of that file):

    [gates.location]                 -> LocationGate
    [gates.anti_fit]                 -> anti_fit_buckets
    [selects.seniority]              -> seniority_selected
    [weights.functional_gradient]    -> functional_buckets
    [weights.combinatoric_emphasis]  -> combinatoric_emphasis
    [weights.adjacency_coverage]     -> adjacency_coverage
    [weights.fit_vs_value_tradeoff]  -> fit_weight / value_weight
    [values.location_center]         -> location_center
    [composite.comp_floor]           -> CompFloor
    [[domain_worlds]]                -> domain_worlds

Two fields are ADDITIVE on top of the engine config (they do not exist in
fit_model.toml and round-trip cleanly as defaults):

  - `identity`     — who the person is (name, contact, headline). Jordan Avery's lives
                     in CLAUDE.md; a cloned user's gets extracted at intake.
  - provenance/    — the GROUNDING SPINE. Every field records where it came
    confirmed        from ("resume" | "chat" | "voice" | "archetype" | "user"
                     | "unknown"); `confirmed` gates whether the engine may use
                     the profile at all. See profile_store.load_active — the
                     engine reads ONLY confirmed profiles. This is Rule 1 of
                     ALICE_SOUL applied to onboarding: never source/score on a
                     hallucinated or unconfirmed profile.

`UNKNOWN` is a real, first-class value. When a source does not support a field,
extraction marks its provenance "unknown" and leaves an archetype/default value
in place that the user MUST see and confirm. Unknowns are never silently
fabricated into facts.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# Provenance sources, in increasing order of trust. "unknown" means the field
# was NOT grounded in any user material — it carries a default the user must
# review. "archetype" means a cold-start preset supplied it. "user" means the
# user typed/confirmed it directly.
ProvenanceSource = Literal["resume", "chat", "voice", "archetype", "user", "unknown", "default"]

# Field paths that the confirm gate treats as critical — if any of these is
# "unknown" at confirm time, the confirm payload flags it prominently.
CRITICAL_FIELDS = (
    "identity.name",
    "location.remote_us_eligible",
    "location.travel_allowed",
    "seniority_selected",
    "domain_worlds",
)


class Identity(BaseModel):
    """Who the person is. Not part of the engine config — additive at the
    profile level. Unknown contact fields stay empty strings, never invented."""
    name: str = ""
    city: str = ""
    state: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    website: str = ""
    headline: str = ""  # one-line professional identity, grounded in the resume


class DomainWorld(BaseModel):
    """One portfolio world. Mirrors fit_judge.DomainWorld. The `label` is an
    INTERNAL handle only — never rendered into an engine prompt (the keyword-
    substring guard). The engine reasons over `definition` + `anti_examples`."""
    label: str
    definition: str
    anti_examples: list[str] = Field(default_factory=list)


class LocationGate(BaseModel):
    """[gates.location]. A binary viability gate — non-overridable. For a new
    user the travel sub-fields default OPEN (travel allowed) unless the user
    states a restriction; any per-user travel preference is that user's
    parameter, not a default for everyone."""
    remote_us_eligible: bool = True
    non_remote_locations: list[str] = Field(default_factory=list)
    non_remote_radius_mi: int = 50
    travel_allowed: bool = True
    travel_relaxes_on: str = ""
    travel_reevaluate_after: str = ""


class CompFloor(BaseModel):
    """[composite.comp_floor]. A value (threshold) that modulates the fit-vs-
    value weight. soft_below_threshold = great fit can pull a sub-threshold role
    in anyway. hard_floor = killed regardless of fit below this."""
    threshold_usd: int = 0
    soft_below_threshold: bool = True
    hard_floor_usd: int = 0


class Profile(BaseModel):
    """A complete, engine-consumable fit model for one user.

    Round-trips with config/fit_model.toml via from_engine_toml /
    to_engine_toml_dict. Carries the grounding spine (provenance + confirmed)
    that the engine checks before it will source or score on the profile.
    """
 # ── meta ──
    version: str = "user-v1"
    created: str = ""
    updated: str = ""

 # ── identity (additive, not in engine config) ──
    identity: Identity = Field(default_factory=Identity)

 # ── gates ──
    location: LocationGate = Field(default_factory=LocationGate)
    anti_fit_buckets: list[str] = Field(default_factory=list)

 # ── multi-select ──
    seniority_selected: list[str] = Field(default_factory=list)

 # ── weighted ──
    functional_buckets: dict[str, float] = Field(default_factory=dict)
    combinatoric_emphasis: float = 0.5
    adjacency_coverage: float = 0.5
    fit_weight: float = 0.5
    value_weight: float = 0.5

 # ── fill-in ──
    location_center: dict[str, Any] = Field(default_factory=dict)

 # ── composite ──
    comp_floor: CompFloor = Field(default_factory=CompFloor)

 # ── domain worlds ──
    domain_worlds: list[DomainWorld] = Field(default_factory=list)

 # ── grounding spine (additive) ──
    user_id: str = ""
    confirmed: bool = False
    confirmed_at: str = ""
 # field-path -> ProvenanceSource. e.g. {"identity.name": "resume",
 # "seniority_selected": "chat", "comp_floor": "unknown"}.
    provenance: dict[str, str] = Field(default_factory=dict)
 # evidence quotes backing grounded fields: field-path -> source excerpt.
 # Used by the no-fabrication audit and surfaced in the confirm payload.
    evidence: dict[str, str] = Field(default_factory=dict)

 # ── grounding helpers ──────────────────────────────────────────────────
    def unknown_fields(self) -> list[str]:
        """Field paths whose provenance is 'unknown' — surfaced for confirm."""
        return sorted(p for p, src in self.provenance.items() if src == "unknown")

    def unknown_critical_fields(self) -> list[str]:
        """Critical fields the source material did not support."""
        unknown = set(self.unknown_fields())
        return [f for f in CRITICAL_FIELDS if f in unknown]

    def set_provenance(self, path: str, source: ProvenanceSource, evidence: str = "") -> None:
        self.provenance[path] = source
        if evidence:
            self.evidence[path] = evidence

 # ── round-trip with the engine config ───────────────────────────────────
    @classmethod
    def from_engine_toml(cls, path: str | Path) -> "Profile":
        """Load an fit_model-shaped TOML into a Profile. The inverse of
        to_engine_toml_dict for the engine dimensions. Identity + grounding
        fields are not present in the engine config and take defaults."""
        with open(path, "rb") as f:
            d = tomllib.load(f)
        return cls.from_engine_toml_dict(d)

    @classmethod
    def from_engine_toml_dict(cls, d: dict) -> "Profile":
        gloc = d.get("gates", {}).get("location", {})
        gaf = d.get("gates", {}).get("anti_fit", {})
        fg = d.get("weights", {}).get("functional_gradient", {})
        fvv = d.get("weights", {}).get("fit_vs_value_tradeoff", {})
        comp = d.get("composite", {}).get("comp_floor", {})
        return cls(
            version=d.get("version", "user-v1"),
            created=str(d.get("created", "")),
            updated=str(d.get("updated", "")),
            location=LocationGate(
                remote_us_eligible=bool(gloc.get("remote_us_eligible", True)),
                non_remote_locations=list(gloc.get("non_remote_locations", [])),
                non_remote_radius_mi=int(gloc.get("non_remote_radius_mi", 50)),
                travel_allowed=bool(gloc.get("travel_allowed", True)),
                travel_relaxes_on=str(gloc.get("travel_relaxes_on", "")),
                travel_reevaluate_after=str(gloc.get("travel_reevaluate_after", "")),
            ),
            anti_fit_buckets=list(gaf.get("buckets", [])),
            seniority_selected=list(d.get("selects", {}).get("seniority", {}).get("selected", [])),
            functional_buckets={k: float(v) for k, v in fg.get("buckets", {}).items()},
            combinatoric_emphasis=float(d.get("weights", {}).get("combinatoric_emphasis", {}).get("value", 0.5)),
            adjacency_coverage=float(d.get("weights", {}).get("adjacency_coverage", {}).get("value", 0.5)),
            fit_weight=float(fvv.get("fit_weight", 0.5)),
            value_weight=float(fvv.get("value_weight", 0.5)),
            location_center=dict(d.get("values", {}).get("location_center", {})),
            comp_floor=CompFloor(
                threshold_usd=int(comp.get("threshold_usd", 0)),
                soft_below_threshold=bool(comp.get("soft_below_threshold", True)),
                hard_floor_usd=int(comp.get("hard_floor_usd", 0)),
            ),
            domain_worlds=[
                DomainWorld(
                    label=w["label"],
                    definition=w["definition"],
                    anti_examples=list(w.get("anti_examples", [])),
                )
                for w in d.get("domain_worlds", [])
            ],
        )

    def to_engine_toml_dict(self) -> dict:
        """Render the engine dimensions back into the fit_model.toml nested
        shape. This is fed to the constraints loader so a per-user profile drives
        the SAME engine. Identity + grounding fields are profile-level and
        intentionally excluded (the engine never reads them)."""
        return {
            "version": self.version,
            "created": self.created,
            "updated": self.updated,
            "gates": {
                "location": {
                    "type": "binary_gate",
                    "remote_us_eligible": self.location.remote_us_eligible,
                    "non_remote_locations": self.location.non_remote_locations,
                    "non_remote_radius_mi": self.location.non_remote_radius_mi,
                    "travel_allowed": self.location.travel_allowed,
                    "travel_relaxes_on": self.location.travel_relaxes_on,
                    "travel_reevaluate_after": self.location.travel_reevaluate_after,
                },
                "anti_fit": {"type": "binary_gate", "buckets": self.anti_fit_buckets},
            },
            "selects": {"seniority": {"type": "multi_select", "selected": self.seniority_selected}},
            "weights": {
                "functional_gradient": {"type": "weighted", "buckets": self.functional_buckets},
                "combinatoric_emphasis": {"type": "weighted", "value": self.combinatoric_emphasis},
                "adjacency_coverage": {"type": "weighted", "value": self.adjacency_coverage},
                "fit_vs_value_tradeoff": {
                    "type": "weighted",
                    "fit_weight": self.fit_weight,
                    "value_weight": self.value_weight,
                },
            },
            "values": {"location_center": {"type": "fill_in", **self.location_center}},
            "composite": {
                "comp_floor": {
                    "type": "composite",
                    "threshold_usd": self.comp_floor.threshold_usd,
                    "soft_below_threshold": self.comp_floor.soft_below_threshold,
                    "hard_floor_usd": self.comp_floor.hard_floor_usd,
                }
            },
            "domain_worlds": [
                {"label": w.label, "definition": w.definition, "anti_examples": w.anti_examples}
                for w in self.domain_worlds
            ],
        }
