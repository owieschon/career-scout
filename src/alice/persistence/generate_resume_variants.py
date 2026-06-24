"""generate_resume_variants — derive track-tailored resume variants from an
uploaded resume.

A profile slots a person into the matcher; their tracks (AE, RevOps, TAM/CS,
AI/FDE) each want a differently-emphasized resume. This derives one variant per
track FROM THE UPLOADED RESUME — re-ordering and re-emphasizing real content for
the track's reader, never inventing experience the source doesn't contain
(ALICE_SOUL: "Fabricates by drifting from real history into fabricated claims"
is the named failure of Prepare; this guards against it).

Output is markdown per variant, written under templates/<user_id>/. Markdown is
the portable seam; rendering to .docx is a downstream concern (Phase G storage /
the existing tailoring pipeline), not invented here. Each variant carries a
"thin-track" flag when the source resume gives a track little to stand on, so
the user is told honestly rather than handed a padded resume.

The LLM call is injectable for offline tests.
"""
from __future__ import annotations

import re
from pathlib import Path
from alice import repo_paths

_ROOT = repo_paths.ROOT
TEMPLATES_DIR = _ROOT / "templates"

# Track key -> (display name, who reads it, what to emphasize). Mirrors the
# resume variants named in CLAUDE.md so a clone's tracks line up with the candidate's.
TRACKS = {
    "operator-builder": {
        "display": "Operator / Builder (AI-native, FDE, applied-AI)",
        "reader": "an engineering or founder hiring manager at an AI-native company",
        "emphasize": "shipped production software/ML, end-to-end ownership, building under ambiguity, technical depth",
    },
    "revenue-architect": {
        "display": "Revenue Architect (RevOps / Sales-Ops)",
        "reader": "a RevOps or revenue-leadership hiring manager",
        "emphasize": "revenue systems, CRM/pipeline/forecasting ownership, GTM analytics, sales-tech stack, process built",
    },
    "senior-ae": {
        "display": "Senior / Founding AE",
        "reader": "a sales-leadership hiring manager",
        "emphasize": "quota attainment, deal sizes, expansion, technical-buyer credibility, named logos closed",
    },
    "tam": {
        "display": "TAM / Senior CS / Implementation",
        "reader": "a customer-success or post-sales hiring manager",
        "emphasize": "technical deployment, QBRs, retention/expansion, customer-engineering translation",
    },
}


def _variants_dir(user_id: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (user_id or "default"))
    return TEMPLATES_DIR / safe


def _build_prompt(resume_text: str, track_key: str) -> str:
    t = TRACKS[track_key]
    return (
        f"Re-frame the resume below for {t['reader']}. Emphasize: {t['emphasize']}.\n\n"
        f"HARD RULES:\n"
        f"- Use ONLY facts, employers, dates, titles, and numbers that appear in "
        f"the source resume. Do NOT invent or inflate any experience, metric, or "
        f"credential. If the source doesn't support a strong claim for this track, "
        f"leave it out — a thinner honest resume beats a padded one.\n"
        f"- Keep it in the candidate's voice. No consulting-speak (no 'synergies', "
        f"'leveraged', 'passionate about'). No em dashes.\n"
        f"- Re-order and re-weight; you may tighten phrasing, but every line must "
        f"trace to something in the source.\n\n"
        f"Return markdown with a header (name + contact + a one-line summary aimed "
        f"at this reader), then sections (Experience, Skills, etc.). At the very "
        f"end, on its own line, output exactly one of:\n"
        f"  THIN_TRACK: <one short reason> — if the source gives this track little "
        f"to stand on.\n"
        f"  TRACK_OK — if the source supports a credible resume for this track.\n\n"
        f"--- SOURCE RESUME START ---\n{resume_text}\n--- SOURCE RESUME END ---\n"
    )


def derive_variant(resume_text: str, track_key: str, *, llm_call=None) -> dict:
    """Derive a single track variant. Returns
    {"track": key, "markdown": str, "thin": bool, "thin_reason": str}.
    Fails loud on an unknown track; degrades to thin/empty on LLM failure."""
    if track_key not in TRACKS:
        raise ValueError(f"unknown track {track_key!r}; have {sorted(TRACKS)}")
    if not (resume_text or "").strip():
        return {"track": track_key, "markdown": "", "thin": True,
                "thin_reason": "no source resume text"}

    if llm_call is None:
        from alice.llm import llm
        llm_call = llm.call

    prompt = _build_prompt(resume_text, track_key)
    try:
        res = llm_call(task="resume_variant_derive", prompt=prompt, max_tokens=2000)
        md = (res.get("text") if isinstance(res, dict) else str(res)) or ""
    except Exception as e:
        return {"track": track_key, "markdown": "", "thin": True,
                "thin_reason": f"generation failed: {e}"}

    md = md.strip()
    thin = False
    thin_reason = ""
    m = re.search(r"^THIN_TRACK:\s*(.+)$", md, flags=re.MULTILINE)
    if m:
        thin = True
        thin_reason = m.group(1).strip()
 # strip the trailing marker line(s) from the saved markdown
    md = re.sub(r"^\s*(THIN_TRACK:.*|TRACK_OK)\s*$", "", md, flags=re.MULTILINE).strip()
    return {"track": track_key, "markdown": md, "thin": thin, "thin_reason": thin_reason}


def derive_variants(
    resume_text: str,
    user_id: str = "default",
    *,
    tracks: list[str] | None = None,
    write: bool = True,
    llm_call=None,
) -> dict:
    """Derive all (or selected) track variants from one uploaded resume.

    Returns {track_key: variant_dict}. When write=True, saves each non-empty
    variant to templates/<user_id>/resume-<track>.md.
    """
    track_keys = tracks or list(TRACKS)
    out: dict[str, dict] = {}
    dest = _variants_dir(user_id)
    for tk in track_keys:
        variant = derive_variant(resume_text, tk, llm_call=llm_call)
        out[tk] = variant
        if write and variant["markdown"]:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"resume-{tk}.md").write_text(variant["markdown"])
            variant["path"] = str(dest / f"resume-{tk}.md")
    return out
