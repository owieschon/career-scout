"""Profile storage — local JSON now, a clean seam for Supabase later.

This is deliberately a thin, swappable backend. It persists locally and exposes
the SAME interface a Supabase per-user profile table would implement, so the
swap is a one-file change and the intake/engine code above it does not move.

THE GROUNDING GATE lives here: `load_active` returns a profile ONLY if it is
confirmed. The matcher engine calls load_active / require_active and therefore
can never source or score on a draft, an unconfirmed, or a hallucinated profile.
Drafts are visible only to the intake/confirm flow.

Layout (local backend):
    state/profiles/<user_id>.json   — one profile per user (draft or confirmed)

A user has at most one stored profile. Re-running intake overwrites the draft
(confirmed=False) until the user confirms; confirming flips the flag in place.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alice import safe_state
from alice.persistence.profile_schema import Profile
from alice import repo_paths

# Repo-root-anchored so daemon, cron, and tests all resolve the same dir
# regardless of cwd (mirrors llm.py's absolute _LOG anchoring).
_ROOT = repo_paths.ROOT
PROFILES_DIR = _ROOT / "state" / "profiles"


class ProfileNotConfirmed(Exception):
    """Raised by require_active when a profile exists but is not yet confirmed.
    The engine treats this as a hard stop — onboarding must finish first."""


class ProfileMissing(Exception):
    """Raised by require_active when no profile exists for the user at all."""


def _safe(user_id: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in (user_id or "default"))


def _path(user_id: str) -> Path:
    return PROFILES_DIR / f"{_safe(user_id)}.json"


def _sources_path(user_id: str) -> Path:
    return PROFILES_DIR / f"{_safe(user_id)}.sources.json"


def save_sources(user_id: str, sources: dict) -> None:
    """Persist the raw intake sources (resume/chat/voice text) alongside the
    draft, so post-confirm steps (resume-variant derivation, re-extraction on an
    edit) can re-read the originals without asking the user to re-upload. PII;
    gitignored with the rest of state/profiles/."""
    safe_state.atomic_write(_sources_path(user_id), sources)


def load_sources(user_id: str) -> dict:
    return safe_state.atomic_read(_sources_path(user_id), default={}) or {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_draft(profile: Profile) -> Profile:
    """Persist a profile as an UNCONFIRMED draft. Never sets confirmed=True —
    confirming is a separate, explicit step (the gate). Stamps `updated`."""
    profile.confirmed = False
    profile.confirmed_at = ""
    profile.updated = _now()
    if not profile.created:
        profile.created = profile.updated
    safe_state.atomic_write(_path(profile.user_id), profile.model_dump())
    return profile


def load_draft(user_id: str) -> Profile | None:
    """Load whatever is stored for the user (draft OR confirmed). For the
    intake/confirm flow only — the engine must use load_active."""
    raw = safe_state.atomic_read(_path(user_id), default=None)
    if raw is None:
        return None
    return Profile.model_validate(raw)


def confirm(user_id: str) -> Profile:
    """Flip a stored draft to confirmed (the commit half of confirm-then-commit).

    Raises ProfileMissing if there is nothing to confirm. Returns the confirmed
    profile. This is the ONLY path that sets confirmed=True; intake never does.
    """
    confirmed_at = _now()

    def mutator(state):
        if state is None:
            raise ProfileMissing(f"no profile to confirm for user {user_id!r}")
        state["confirmed"] = True
        state["confirmed_at"] = confirmed_at
        state["updated"] = confirmed_at
        return state, state

    raw = safe_state.atomic_update(_path(user_id), mutator, default=None)
    return Profile.model_validate(raw)


def discard_draft(user_id: str) -> bool:
    """Delete a user's stored profile (used on confirm-cancel). Returns True if
    something was removed. Only meaningful for drafts; a confirmed profile would
    also be removed, so callers should guard if that matters."""
    removed = False
    for p in (_path(user_id), _sources_path(user_id)):
        if p.exists():
            p.unlink()
            removed = True
    return removed


def load_active(user_id: str) -> Profile | None:
    """THE GROUNDING GATE. Return the profile ONLY if it is confirmed; else None.

    The engine reads through this. A draft, an unconfirmed extraction, or no
    profile at all all return None — so the engine cannot run on ungrounded
    state. Fail-closed by construction."""
    prof = load_draft(user_id)
    if prof is None or not prof.confirmed:
        return None
    return prof


def require_active(user_id: str) -> Profile:
    """Like load_active but fail LOUD (P2) with a specific reason. For engine
    entry points that must not proceed without a confirmed profile."""
    prof = load_draft(user_id)
    if prof is None:
        raise ProfileMissing(
            f"no profile for user {user_id!r}; onboarding (intake) has not run"
        )
    if not prof.confirmed:
        raise ProfileNotConfirmed(
            f"profile for user {user_id!r} exists but is not confirmed; "
            f"the user must review and confirm the extracted profile before any "
            f"sourcing or scoring (grounding gate)"
        )
    return prof
