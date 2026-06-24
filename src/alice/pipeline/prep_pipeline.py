"""Prep-package generation as a gated four-stage pipeline.

The structural answer to the recurring fabrication failures: do not let the
model generate until ground truth is in hand; generate in dependency order
not in one tool-loop blast; verify each artifact against the retrieved
ground; assemble each stage's prompt fresh with only what that stage needs.

Stages:

  1. GROUND  — retrieve JD body, Jordan Avery's history, and company research.
               Halt if any required input is missing or fails verification.
               Loop governor: dedup retrieval attempts, no-progress detector,
               graceful partial results.

  2. WRITE   — generate artifacts in dependency order (resume → cover +
               strategy + questions). Each artifact is its own LLM call
               with a stage-scoped prompt. No multi-file tool-loop.

  3. VERIFY  — for each artifact, check that specific factual claims have
               grounding in Stage 1's retrieved material. v1 is
               pattern-based (numbers, dates, specific company names,
               specific candidate-experience claims). Flagged claims are
               stripped or marked.

  4. ASSEMBLE — every stage above gets exactly the context it needs,
               nothing more. The chat path's _build_alice_context is a
               separate kitchen-sink because chat is open-ended; here
               each stage's input is precisely scoped at the seam.

The pipeline is callable from two surfaces with identical behavior:

  - chat (via tools.py generate_application_package — the freeform path
    decides to invoke it; the pipeline runs the same gates regardless)

  - cron (prep_materials.process_queue calls run_pipeline for each entry
    instead of inlining the LLM calls)

Both surfaces share one structure. A surface-specific patch that "makes
this case work" is the failure mode this avoids.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


from alice.llm import llm
from alice import repo_paths

REPO_ROOT = repo_paths.ROOT
APPS_DIR = REPO_ROOT / "applications"
TEMPLATES = REPO_ROOT / "templates"
COMPANIES_DIR = REPO_ROOT / "targets" / "companies"


# ─── result types ────────────────────────────────────────────────────────────

@dataclass
class GroundResult:
    """Stage 1 output. `halted=True` means generation must NOT proceed."""
    jd_body:           str | None = None
    jd_source:         str | None = None
    jd_chars:          int = 0
    operator_variant:      str | None = None
    operator_history:      str | None = None
    company_name:      str = ""
    company_research:  str | None = None
    company_research_source: str | None = None
    company_research_incomplete: bool = False
 # Experience-store dual-landing: writers see this block in their prompt;
 # the verifier also pulls these entries into ground_blob so claims
 # grounded in confirmed experience pass Stage 3. The `experience`
 # source class has TWO population paths unioned at retrieve:
 # - chat-captured (feedback/experience-store.jsonl) — verbatim-substring integrity
 # - file-authored (knowledge/experience/<source>.md) — write-access integrity
 # Both flow through experience_extras_entries; the file-authored path
 # ALSO produces canonical_framing_block (A-rich design) which surfaces
 # to writers as its OWN block above EXPERIENCE EXTRAS — the framing-lock.
    experience_extras_block:    str = ""
    experience_extras_entries:  list[dict] = field(default_factory=list)
    canonical_framing_block:    str = ""
 # Primitives basis-material (knowledge/primitives/primitives.jsonl): the
 # grounded, tagged atoms the writers compose from. Replaces the prose master
 # as the PRIMARY source (operator_history becomes supplementary). Treated as a
 # grounding source by the verifier, same as experience/canonical_framing.
    primitives_block:           str = ""
    retrieval_log:     list[dict] = field(default_factory=list)
    halted:            bool = False
    halt_reason:       str | None = None

    def required_complete(self) -> bool:
        return bool(self.jd_body) and bool(self.operator_history)


@dataclass
class WriteResult:
    """Stage 2 output. Generated artifacts as raw text, in dependency order."""
    resume:    str | None = None
    cover:     str | None = None
    strategy:  str | None = None
    questions: str | None = None
    outreach:  str | None = None
    artifacts_generated: list[str] = field(default_factory=list)
    artifact_costs:      dict = field(default_factory=dict)
    artifact_models:     dict = field(default_factory=dict)
    halted:    bool = False
    halt_reason: str | None = None


@dataclass
class VerifyVerdict:
    """Stage 3 per-artifact verdict."""
    artifact:        str
    total_claims:    int = 0
    grounded_claims: int = 0
    flagged_claims:  list[dict] = field(default_factory=list)
    stripped_count:  int = 0
 # Per-source attribution counter. Records which of the grounded sources
 # actually grounded each claim — so the audit trail is honest about
 # provenance (vs. just "grounded yes/no"). Sources: 'experience',
 # 'history', 'jd', 'company'.
    attribution:     dict = field(default_factory=dict)

    def passed(self) -> bool:
        return len(self.flagged_claims) == 0


@dataclass
class VerifyResult:
    """Stage 3 output across all artifacts."""
    verdicts: list[VerifyVerdict] = field(default_factory=list)
    overall_grounded_pct: float = 0.0
    overall_flagged_count: int = 0
 # Banned-framing / anonymization breaches (CLAUDE.md hard rules). Distinct
 # from ordinary ungrounded flags: a grounded-but-banned claim (e.g. Ironclad
 # Industrial framed as a Cadence Analytics customer/design-partner) is a HARD block, not a
 # soft flag — run_pipeline withholds the drafts when this is > 0.
    banned_framing_count: int = 0
 # Value-led warnings (North Star #4): resume/cover opens with a tool before
 # any outcome. Soft quality nudge — does NOT fail passed() or withhold drafts.
    value_led_warnings: int = 0
 # Protest-language warnings: the writer used lone-wolf / defensive / overclaim
 # filler ("solo", "single-handedly", "not just a...", "strongest"). These read
 # as insecurity and undercut the claim. Soft nudge surfaced to Jordan Avery — does NOT
 # withhold (flag-to-human, not auto-block). The corpus is clean; this catches
 # the WRITER re-introducing it at generation time.
    voice_protest_warnings: int = 0
 # AI-residue warnings: the generated prose contains LLM tells (consulting-speak
 # from evals._CONSULTING_SPEAK, or an em-dash that slipped past llm._post_process).
 # Soft nudge surfaced to Jordan Avery, same class as protest/value-led — does NOT withhold
 # (withhold is banned_framing only). Universal Layer-1 voice gate on the live path:
 # evals.eval_voice already exists and was used offline; this wires it into verify.
    voice_residue_warnings: int = 0

    def passed(self) -> bool:
        return self.overall_flagged_count == 0 and self.banned_framing_count == 0


@dataclass
class PipelineResult:
    """Top-level pipeline output."""
    slug:        str
    company:     str
    role:        str
    pkg_dir:     str
    ground:      GroundResult
    write:       WriteResult | None = None
    verify:      VerifyResult | None = None
    files_written: list[str] = field(default_factory=list)
    total_cost:  float = 0.0
    halted_at_stage: str | None = None
    halt_reason: str | None = None
    started_at:  str = ""
    finished_at: str = ""

    def succeeded(self) -> bool:
        return self.halted_at_stage is None and bool(self.files_written)


# ─── slug / helpers ──────────────────────────────────────────────────────────

def slugify(company: str, role: str = "") -> str:
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def _read_docx(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        from docx import Document
        d = Document(str(path))
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())
    except Exception:
        return ""


_PRIMITIVES_PATH = REPO_ROOT / "knowledge" / "primitives" / "primitives.jsonl"


def _load_primitives_block() -> str:
    """Load the primitives library as a basis-material block for the writers.

    The whole corpus is small (~40 atoms / ~3K tokens), so we hand the writer
    ALL of it and let it pick — that is the design ("the AI hand-picks the best
    parts"), and a deterministic pre-filter is a worse picker than the model.
    Tags travel with each atom as metadata the writer reads, not as a gate.
    Returns "" if the library is absent (pipeline falls back to the prose master).
    """
    if not _PRIMITIVES_PATH.exists():
        return ""
    import json as _json
    by_type: dict[str, list[str]] = {}
    for line in _PRIMITIVES_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            p = _json.loads(line)
        except Exception:
            continue
        t = p.get("tags", {})
        meta = (f"archetypes={','.join(t.get('archetypes', []))} "
                f"domains={','.join(t.get('domains', []))} "
                f"themes={','.join(t.get('themes', []))}")
        by_type.setdefault(p.get("type", "other"), []).append(
            f"  [{p.get('id')}] ({p.get('provenance')}, strength {p.get('strength')}) "
            f"{p.get('claim')}\n      tags: {meta}")
    if not by_type:
        return ""
    order = ["proof_point", "accomplishment", "capability", "experience", "skill", "domain"]
    out = ["PRIMITIVES (Jordan Avery's grounded, tagged atoms — the BASIS MATERIAL):"]
    for t in order + [k for k in by_type if k not in order]:
        if t in by_type:
            out.append(f"\n# {t.upper()}")
            out.extend(by_type[t])
    return "\n".join(out)


def _baseline_retrieval_tags() -> list[str]:
    """The user's always-available 'lead competency' evidence tags, read from
    fit_model.toml [prep].baseline_retrieval_tags. Jordan Avery is a builder ->
    ["builder","applied-ai"]; a non-builder user supplies their own (possibly
    empty -> no baseline injection). Fail-soft to [] if the config/section is
    absent: a missing baseline means 'no always-on seed', not a crash."""
    import os
    import tomllib
    p = os.path.join(str(repo_paths.ROOT),
                     "config", "fit_model.toml")
    try:
        with open(p, "rb") as f:
            return list(tomllib.load(f).get("prep", {}).get("baseline_retrieval_tags", []))
    except FileNotFoundError:
        return []


_BASELINE_TAGS = _baseline_retrieval_tags()


def _target_tags_for_role(archetype: str, role: str, company: str) -> list[str]:
    """Derive retrieval tags for the experience store from the role context.

    Tags are matched against entry tags (lowercased intersection in
    experience_store.retrieve_for_role). Archetype names map to broad
    competency tags; the role title contributes domain-specific tokens;
    the company name itself counts (in case Jordan Avery has direct experience
    with a related account). Conservative — keeps the surface small so
    tag overlap is meaningful.
    """
 # Builder baseline: a LIGHT availability signal (2 tags only). The candidate's build
 # evidence (Cadence Analytics, Alice) should be AVAILABLE for every role so it can serve
 # as technical credibility, including the "Other" fallback that would otherwise
 # yield zero experience entries. But it must NOT dominate:
 # on commercial roles (AE/CS/RevOps) the archetype's own commercial tags add
 # 5-6 matches to the GTM sources (lattice-additive/ironclad), which then outrank the
 # 2-tag-matching builder sources so the GTM evidence leads and is not crowded
 # out of the token budget. On builder roles the archetype adds builder/ml/
 # applied-ai/fde, boosting the builder sources to the top. Emphasis is thus
 # governed by archetype + the experience-file framing-lock, not a heavy seed.
    tags: list[str] = list(_BASELINE_TAGS)
    a = (archetype or "").lower()
    if "ae" in a:
        tags += ["sales", "ae", "revenue", "renewal", "expansion", "pipeline"]
    if "revops" in a or "rev ops" in a or "revenue" in a:
        tags += ["revops", "sales-ops", "forecasting", "pipeline", "retention"]
    if "tam" in a or "cs" in a:
        tags += ["cs", "tam", "retention", "renewal", "implementation"]
    if "fde" in a or "applied" in a:
        tags += ["fde", "builder", "ml", "applied-ai", "implementation"]
 # Title-derived tokens (light)
    for token in re.findall(r"[a-zA-Z][a-zA-Z\-]{3,}", role or ""):
        tags.append(token.lower())
    if company:
        tags.append(company.strip().lower())
 # Dedup while preserving order
    seen = set()
    out = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _pick_variant(archetype: str) -> str:
    mapping = {
        "AE":        "resume-senior-ae.docx",
        "RevOps":    "resume-revenue-architect.docx",
        "TAM / CS":  "resume-tam.docx",
        "FDE":       "resume-operator-builder.docx",
    }
    return mapping.get(archetype, "resume-operator-builder.docx")


# ─── STAGE 1 — GROUND ────────────────────────────────────────────────────────

# Minimum-length thresholds. Below these we treat the retrieval as failed.
_MIN_JD_CHARS    = 400
_MIN_HISTORY_CHARS = 500
_MIN_COMPANY_CHARS = 300

# Loop governor params for Stage 1.
_MAX_RETRIEVAL_STEPS    = 8
_MAX_NO_PROGRESS_STEPS  = 2

# Web-research budget for the company-deep-dive step.
_WEB_RESEARCH_MAX_USES   = 5      # server-side cap on web_search calls
_WEB_RESEARCH_MAX_TOKENS = 2000   # output cap for the structured profile

# The five bounded dimensions Path A.5 probes — keep tight; "research
# everything" is the failure mode this design refuses.
_RESEARCH_DIMENSIONS = ("PRODUCT", "CUSTOMERS", "POSITION", "FOCUS", "PEOPLE")


def _web_research_company(*, company: str, role: str, jd_url: str,
                          jd_excerpt: str) -> tuple[str | None, str | None, dict]:
    """Path A.5: bounded company research via Anthropic server-side web_search.

    Returns (research_text, error, dim_counts). research_text is the model's
    structured output if the call succeeded; error is a short string when the
    call failed; dim_counts is {confirmed, unclear, not_found} derived from
    parsing the output.

    No tool_executor is needed — web_search_20250305 is a server-side tool;
    Anthropic runs the searches inline and returns results in the same
    response, so llm.call's existing terminal-branch handling applies.
    """
    host = ""
    try:
        host = urllib.parse.urlparse(jd_url).netloc or ""
    except Exception:
        pass

    prompt = (
        f"Research the company {company!r} for an application packet.\n\n"
        f"Context — Jordan Avery is applying to a {role!r} role; JD URL host is "
        f"{host or '(unknown)'}. JD excerpt for grounding only (do NOT repeat "
        f"the JD as 'research'):\n\n{jd_excerpt}\n\n"
        f"Use the web_search tool (≤{_WEB_RESEARCH_MAX_USES} searches) to find:\n\n"
        "  1. PRODUCT    — what they actually sell, in one plain sentence.\n"
        "  2. CUSTOMERS  — who buys it (segment, ICP, named customers if public).\n"
        "  3. POSITION   — market stage / funding round / primary competitors.\n"
        "  4. FOCUS      — direction in the LAST 12 MONTHS: funding announcements,\n"
        "                  product launches, leadership changes, public strategy.\n"
        "  5. PEOPLE     — hiring manager OR VP of the relevant function; team leads\n"
        "                  if findable on LinkedIn or company about-page.\n\n"
        "OUTPUT FORMAT — exactly this shape, no preamble, no closing remarks:\n\n"
        "# COMPANY RESEARCH — <company name>\n\n"
        "## PRODUCT\n"
        "<CONFIRMED|UNCLEAR|NOT_FOUND>: <one sentence; if CONFIRMED include a source URL in brackets>\n\n"
        "## CUSTOMERS\n"
        "<CONFIRMED|UNCLEAR|NOT_FOUND>: <one sentence; source URL if CONFIRMED>\n\n"
        "## POSITION\n"
        "<CONFIRMED|UNCLEAR|NOT_FOUND>: <one or two sentences; source URL if CONFIRMED>\n\n"
        "## FOCUS\n"
        "<CONFIRMED|UNCLEAR|NOT_FOUND>: <2-4 bullets of recent items; each with source URL if CONFIRMED>\n\n"
        "## PEOPLE\n"
        "<CONFIRMED|UNCLEAR|NOT_FOUND>: <names + titles found; source URL if CONFIRMED>\n\n"
        "## OPERATOR_NEEDS_TO_CONFIRM\n"
        "<1-3 numbered questions for Jordan Avery — things only he can answer (warm contacts, prior context, opinions)>\n\n"
        "HARD RULES:\n"
        "- If you didn't find a dimension on the web in this turn, mark it NOT_FOUND.\n"
        "- Do NOT paraphrase or guess from the JD excerpt; that's not research.\n"
        "- Do NOT invent URLs. Only cite URLs returned by web_search.\n"
        "- One sentence per dimension (FOCUS may be 2-4 bullets). No prose padding.\n"
    )

    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": _WEB_RESEARCH_MAX_USES,
    }]

    try:
        res = llm.call(
            "company_deep_dive",
            prompt,
            max_tokens=_WEB_RESEARCH_MAX_TOKENS,
            tools=tools,
        )
    except Exception as e:
        return (None, f"{type(e).__name__}: {e}", {"confirmed": 0, "unclear": 0, "not_found": 0})

    text = (res.get("text") or "").strip()
    if not text:
        return (None, "empty response from llm.call", {"confirmed": 0, "unclear": 0, "not_found": 0})

    dim_counts = _count_research_dimensions(text)
    return (text, None, dim_counts)


def _count_research_dimensions(text: str) -> dict:
    """Parse the structured research output and count per-dimension verdicts.

    A dimension counts as CONFIRMED only if its section header is followed by
    a CONFIRMED: line. NOT_FOUND and UNCLEAR are tracked too — both signal
    'don't make confident claims about this dimension downstream'.
    """
    counts = {"confirmed": 0, "unclear": 0, "not_found": 0}
    for dim in _RESEARCH_DIMENSIONS:
 # Find the section header and grab the first non-blank line after it.
        m = re.search(
            rf"^##\s+{dim}\s*$([\s\S]*?)(?=^##\s|\Z)",
            text, re.MULTILINE,
        )
        if not m:
            counts["not_found"] += 1
            continue
        body = m.group(1).strip()
        if re.search(r"\bCONFIRMED\b", body):
            counts["confirmed"] += 1
        elif re.search(r"\bUNCLEAR\b", body):
            counts["unclear"] += 1
        else:
 # default to NOT_FOUND when the verdict marker is missing
            counts["not_found"] += 1
    return counts


def _confirmed_sections_of(research_text: str) -> str:
    """Return only the CONFIRMED-marked sections of structured research text.

    Used by Stage 3 to ensure NOT_FOUND / UNCLEAR sections cannot falsely
    serve as grounding for claims in generated artifacts.
    """
    if not research_text:
        return ""
    out_parts: list[str] = []
    for dim in _RESEARCH_DIMENSIONS:
        m = re.search(
            rf"^##\s+{dim}\s*$([\s\S]*?)(?=^##\s|\Z)",
            research_text, re.MULTILINE,
        )
        if not m:
            continue
        body = m.group(1).strip()
        if re.search(r"\bCONFIRMED\b", body):
            out_parts.append(f"## {dim}\n{body}")
    return "\n\n".join(out_parts)


def _persist_jd_body(*, company: str, role: str, url: str, jd_body: str,
                     log: Callable) -> None:
    """Write the raw fetched JD body to applications/<slug>/jd-body.txt.

    Wrapped: a failure here MUST NOT halt grounding. The pipeline still
    has jd_body in memory; persistence is an opportunistic side-channel.

    Why this lives at fetch time:
      - Sources rot. Greenhouse roles get pulled, Ashby URLs 404, ATS
        platforms expire postings. A submitted application 90 days back
        whose JD is no longer fetchable means the sourcing matcher
        can't score future Jordan Avery against what it was asking for.
      - It composes with downstream tooling — "which roles asked for
        skill X" scans, late-stage interview re-prep, post-mortem.
      - It is INDEPENDENT of the URL staying live: the file is the truth.

    Idempotency: writes-through on each successful fetch (URL may have
    been updated). The new fetch IS the authoritative body — the prior
    file is replaced. Audit trail lives in .pipeline-metadata.json.
    """
    if not jd_body:
        return
    slug = slugify(company, role)
    pkg_dir = APPS_DIR / slug
    try:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        out_path = pkg_dir / "jd-body.txt"
 # Small header gives a future reader (a year out, no context)
 # the URL + fetch timestamp without disturbing the JD body
 # itself. Sourcing-matcher reads should strip the header.
        header = (
            f"# JD body persisted by prep_pipeline._persist_jd_body\n"
            f"# url:        {url}\n"
            f"# fetched_at: {datetime.now().isoformat(timespec='seconds')}\n"
            f"# company:    {company}\n"
            f"# role:       {role}\n"
            f"# chars:      {len(jd_body)}\n"
            f"# ---\n"
        )
        out_path.write_text(header + jd_body)
        log("persist_jd_body", {"path": str(out_path)}, "ok",
            chars=len(jd_body))
    except Exception as e:
 # Non-fatal — log and continue. Stage 1 still has jd_body in memory.
        log("persist_jd_body", {"slug": slug}, "error",
            error=f"{type(e).__name__}: {e}")


def stage_ground(*, company: str, role: str, url: str, archetype: str,
                 ledger_row: dict | None = None) -> GroundResult:
    """Stage 1: gather + verify required inputs.

    Required (HALT if missing):
      - jd_body (≥ _MIN_JD_CHARS chars from a real fetch)
      - operator_history (≥ _MIN_HISTORY_CHARS chars from a resume variant)

    Best-effort (incomplete-flagged if missing):
      - company_research (from targets/companies/<slug>.md if present;
        otherwise JD-body-only with incomplete=True so Stage 2 knows
        not to make confident company-thesis claims)

    Loop governor: each retrieval attempt is logged. We dedup by
    (action, args). After _MAX_NO_PROGRESS_STEPS attempts that yield
    no new information, we stop attempting and return what we have.
    """
    ground = GroundResult(company_name=company)
    seen_attempts: set[tuple] = set()
    no_progress_streak = 0
    steps = 0

    def _log(action: str, args: dict, outcome: str, chars: int = 0,
             error: str | None = None) -> None:
        ground.retrieval_log.append({
            "step":    steps,
            "action":  action,
            "args":    args,
            "outcome": outcome,
            "chars":   chars,
            "error":   error,
            "ts":      datetime.now().isoformat(timespec="seconds"),
        })

 # ─── Required input 1: JD body ───────────────────────────────────────
    if not url:
        ground.halted = True
        ground.halt_reason = "Stage 1 halt: no JD URL in sheet row. Cannot fetch a JD that doesn't exist."
        return ground

    attempt_key = ("fetch_jd", url)
    if attempt_key not in seen_attempts:
        seen_attempts.add(attempt_key)
        steps += 1
        try:
            from alice.pipeline.enrich_hypotheses import fetch_jd
            jd = fetch_jd(url)
        except Exception as e:
            jd = None
            _log("fetch_jd", {"url": url}, "error", error=f"{type(e).__name__}: {e}")
            no_progress_streak += 1
        else:
            if jd and len(jd) >= _MIN_JD_CHARS:
                ground.jd_body = jd
                ground.jd_source = url
                ground.jd_chars = len(jd)
                _log("fetch_jd", {"url": url}, "ok", chars=len(jd))
                no_progress_streak = 0
 # JD-body persistence: writes the raw fetched JD to
 # applications/<slug>/jd-body.txt so the body survives
 # the role being taken down on the source site. The
 # sourcing-matcher (and any later "which roles asked for
 # skill X" scan) can read these as a stable corpus.
 # Wrapped — a write failure must NOT halt grounding.
                _persist_jd_body(company=company, role=role, url=url,
                                  jd_body=jd, log=_log)
            elif jd:
                _log("fetch_jd", {"url": url}, "short", chars=len(jd),
                     error=f"jd shorter than {_MIN_JD_CHARS} chars")
                no_progress_streak += 1
            else:
                _log("fetch_jd", {"url": url}, "empty",
                     error="fetch_jd returned None — host not in supported map")
                no_progress_streak += 1

    if ground.jd_body is None:
        ground.halted = True
        ground.halt_reason = (
            f"Stage 1 halt: JD body fetch failed for {url}. "
            "Cannot generate application materials without a real JD. "
            "Either the URL is unsupported by enrich_hypotheses.fetch_jd "
            "(only Greenhouse + Ashby are wired), or the role was taken down. "
            "Paste the JD body into the chat and I can proceed from there, "
            "or extend fetch_jd to cover this URL shape."
        )
        return ground

 # ─── Required input 2: Jordan Avery's history ───────────────────────────────
    variant_name = _pick_variant(archetype)
    variant_path = TEMPLATES / variant_name
    attempt_key = ("read_resume_variant", variant_name)
    if attempt_key not in seen_attempts:
        seen_attempts.add(attempt_key)
        steps += 1
        variant_text = _read_docx(variant_path)
        if variant_text and len(variant_text) >= _MIN_HISTORY_CHARS:
            ground.operator_variant = variant_name
            ground.operator_history = variant_text
            _log("read_resume_variant", {"variant": variant_name}, "ok",
                 chars=len(variant_text))
            no_progress_streak = 0
 # Primitives basis-material — the PRIMARY source the writers compose
 # from. Loaded alongside the prose master (which stays as supplementary
 # fallback). Non-fatal: absence just means prose-master-only grounding.
            try:
                ground.primitives_block = _load_primitives_block()
                if ground.primitives_block:
                    _log("load_primitives", {}, "ok",
                         chars=len(ground.primitives_block))
            except Exception as e:
                _log("load_primitives", {}, "error", error=str(e))
        else:
            _log("read_resume_variant", {"variant": variant_name},
                 "empty" if not variant_text else "short",
                 chars=len(variant_text) if variant_text else 0,
                 error=f"variant text shorter than {_MIN_HISTORY_CHARS} chars")
            no_progress_streak += 1

    if ground.operator_history is None:
        ground.halted = True
        ground.halt_reason = (
            f"Stage 1 halt: Jordan Avery's resume variant {variant_name!r} could not "
            "be read or is empty. Cannot draft a resume without the source "
            "of truth for Jordan Avery's history. Check templates/ on disk."
        )
        return ground

 # ─── Best-effort input 3: company research ───────────────────────────
 # Path A: targets/companies/<slug>.md (candidate-curated or prior research)
    slug = slugify(company)
    company_file = COMPANIES_DIR / f"{slug}.md"
    attempt_key = ("read_company_file", str(company_file))
    if attempt_key not in seen_attempts:
        seen_attempts.add(attempt_key)
        steps += 1
        if company_file.exists():
            text = company_file.read_text()
            if len(text) >= _MIN_COMPANY_CHARS:
                ground.company_research = text
                ground.company_research_source = f"targets/companies/{slug}.md"
                ground.company_research_incomplete = False
                _log("read_company_file", {"path": str(company_file)}, "ok",
                     chars=len(text))
                no_progress_streak = 0
            else:
                _log("read_company_file", {"path": str(company_file)}, "short",
                     chars=len(text))
                no_progress_streak += 1
        else:
            _log("read_company_file", {"path": str(company_file)}, "missing")
            no_progress_streak += 1

 # Path A.5: web research via Anthropic server-side web_search.
 # Fires only when Path A missed. Bounded: one llm.call, ≤5 search uses,
 # max_tokens=2000. Output is structured (CONFIRMED / UNCLEAR / NOT_FOUND
 # per dimension); fabrication-from-absence is explicitly forbidden in
 # the prompt and policed structurally by the parser.
    if ground.company_research is None:
        attempt_key = ("web_research", slug)
        if attempt_key not in seen_attempts:
            seen_attempts.add(attempt_key)
            steps += 1
            research_text, research_err, dim_counts = _web_research_company(
                company=company, role=role, jd_url=url,
                jd_excerpt=(ground.jd_body or "")[:1500],
            )
            if research_text and dim_counts.get("confirmed", 0) >= 1:
 # Cache to targets/companies/<slug>.md so subsequent runs
 # hit Path A (no re-spend, Jordan Avery can hand-edit).
                COMPANIES_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    company_file.write_text(research_text)
                except Exception as e:
                    _log("cache_company_research", {"path": str(company_file)},
                         "error", error=f"{type(e).__name__}: {e}")
                ground.company_research = research_text
                ground.company_research_source = "web_research_v1"
 # incomplete=True only if any dimension came back NOT_FOUND /
 # UNCLEAR — writers should still skip confident claims there.
                ground.company_research_incomplete = (
                    dim_counts.get("not_found", 0) + dim_counts.get("unclear", 0) > 0
                )
                _log("web_research", {"slug": slug}, "ok",
                     chars=len(research_text),
                     error=None)
                no_progress_streak = 0
            else:
                _log("web_research", {"slug": slug},
                     "empty" if not research_text else "no_confirmed",
                     chars=len(research_text or ""),
                     error=(research_err or
                            "web_research returned no CONFIRMED dimensions; "
                            "falling through to JD-only path"))
                no_progress_streak += 1

 # Path B (fallback): synthesize from JD body alone, flag incomplete
    if ground.company_research is None:
 # No-progress check before falling back — if the governor has hit
 # the streak limit, we still return; this branch is the graceful
 # partial path the spec calls for.
        steps += 1
        ground.company_research = ground.jd_body[:3000]
        ground.company_research_source = "jd_body_only"
        ground.company_research_incomplete = True
        _log("synthesize_from_jd", {}, "fallback_partial",
             chars=len(ground.company_research),
             error=(f"no targets/companies/{slug}.md and web_research "
                    "produced no confirmed dimensions; using JD body as the "
                    "only signal for what the company does. Stage 2 will "
                    "avoid confident company-thesis claims under this "
                    "incomplete flag."))

 # ─── Best-effort input 4: experience extras ──────────────────────────
 # Pull confirmed experience-store entries whose tags intersect target
 # tags derived from archetype + role + company. Token-budget capped.
 # Never halts — if the store is empty or no entries match, writers
 # proceed on the variant + JD + company-research blob as before.
    try:
        from alice.persistence import experience_store
        target_tags = _target_tags_for_role(archetype, role, company)
        steps += 1
        block, used_entries, canonical_framing_block = experience_store.retrieve_for_role(
            target_tags=target_tags,
        )
        ground.experience_extras_block    = block
        ground.experience_extras_entries  = used_entries
        ground.canonical_framing_block    = canonical_framing_block
        _log("retrieve_experience", {"target_tags": target_tags},
             "ok" if (used_entries or canonical_framing_block) else "empty",
             chars=len(block),
             canonical_framing_chars=len(canonical_framing_block))
    except Exception as e:
        _log("retrieve_experience", {}, "error",
             error=f"{type(e).__name__}: {e}")

 # Governor enforcement: if we've spun without progress, stop.
 # (For the current stage shape we already exit fast on success;
 # this check is the structural place to add web-search retry loops.)
    if no_progress_streak >= _MAX_NO_PROGRESS_STEPS and not ground.required_complete():
        ground.halted = True
        ground.halt_reason = (
            f"Stage 1 halt: governor stopped retrieval after "
            f"{no_progress_streak} no-progress steps. Required inputs "
            "still missing — refusing to fabricate from absence."
        )
        return ground

    if steps > _MAX_RETRIEVAL_STEPS:
 # Defensive — current code doesn't loop, but if a future addition
 # adds retrieval steps, this catches runaway.
        ground.halted = True
        ground.halt_reason = (
            f"Stage 1 halt: retrieval step cap ({_MAX_RETRIEVAL_STEPS}) exceeded."
        )
        return ground

    return ground


# ─── STAGE 4 — ASSEMBLE (used by Stage 2 and Stage 3) ────────────────────────
# Stage 4 is the assemble layer; it appears before Stage 2 in this file
# because Stage 2 calls it.

def assemble_prompt(*, stage: str, ground: GroundResult,
                    prior_artifacts: dict | None = None,
                    company: str = "", role: str = "",
                    url: str = "", archetype: str = "",
                    rationale: str = "") -> str:
    """Build a tight, per-stage prompt. No accumulated chat sediment.

    `stage` is one of: 'resume', 'cover', 'questions', 'strategy', 'verify'.
    Each stage gets exactly what it needs — the JD body, Jordan Avery's history,
    and (for cover/strategy/questions/verify) any prior artifacts that
    need to stay consistent.

    This is the "Stage 4 — ASSEMBLE" layer in the pipeline. The chat
    path has its own assembly (_build_alice_context) because chat is
    open-ended; here the inputs are bounded by what the stage actually
    needs.
    """
    jd_excerpt = (ground.jd_body or "")[:6000]
    history_excerpt = (ground.operator_history or "")[:5000]
    company_block = ""
    if ground.company_research:
        src = ground.company_research_source or ""
        if src == "web_research_v1":
            tag = ("(SOURCE: web_research_v1 — structured profile with "
                   "CONFIRMED / UNCLEAR / NOT_FOUND verdicts per dimension. "
                   "ONLY make confident claims about dimensions marked "
                   "CONFIRMED. For UNCLEAR or NOT_FOUND dimensions, either "
                   "leave a [FILL: <specific question>] placeholder or omit. "
                   "Do NOT paraphrase from the JD to fill a NOT_FOUND gap.)")
        elif src == "jd_body_only":
            tag = ("(SOURCE: synthesized from JD body alone — INCOMPLETE; "
                   "do NOT make confident claims about company strategy, "
                   "investors, leadership, or non-JD-stated direction)")
        else:
            tag = "(SOURCE: targets/companies file — candidate-curated or cached research)"
        company_block = (
            f"\n\nCOMPANY CONTEXT {tag}:\n"
            f"{ground.company_research[:3000]}"
        )

 # Canonical framing block — A-rich design. Surfaces ABOVE
 # EXPERIENCE EXTRAS as its own structural block. This is the
 # framing-lock: 1-2 vetted core claims per source that every writer
 # must render from consistently. Sourced from
 # knowledge/experience/<source>.md frontmatter. Guaranteed to reach
 # every writer (not subject to entry-pack token budget), so framing
 # cannot silently drift to a stereotyped story under packing pressure.
    canonical_framing_section = ""
    if ground.canonical_framing_block:
        canonical_framing_section = (
            "\n\n" + ground.canonical_framing_block +
            "\n\nHOW TO USE CANONICAL FRAMING: these are LOCKED core claims "
            "that you (and every other writer in this prep job) must render "
            "from CONSISTENTLY. Lead with them. Do not silently substitute "
            "an alternate framing — even if the JD's language pulls toward "
            "one. The canonical framing is the agreed core; the rest of the "
            "draft elaborates from it. Stage 3 verification treats these "
            "claims as grounding (same as EXPERIENCE EXTRAS below)."
        )

 # Experience-store block — sourced from BOTH chat-capture
 # (experience-store.jsonl) and file-authored (knowledge/experience/)
 # via the unioned retrieve_for_role. Parallel to JORDAN AVERY'S HISTORY; the
 # verifier treats these entries as grounding (so claims that lean on
 # them pass Stage 3). Token-capped at retrieval time.
    experience_block = ""
    if ground.experience_extras_block:
        experience_block = (
            "\n\n" + ground.experience_extras_block +
            "\n\nHOW TO USE EXPERIENCE EXTRAS: anchor specific factual claims "
            "in these CONFIRMED entries — both candidate-verbatim chat captures "
            "and file-authored facts. Quote-to-claim mapping is fine; "
            "bundling several entries into one specific claim is fine. The "
            "Stage 3 verifier treats these as valid grounding sources "
            "alongside the JD and JORDAN AVERY'S HISTORY."
        )

 # Primitives basis-material — the PRIMARY source the writers compose from.
 # Free composition: the writer picks the atoms most relevant to THIS role and
 # may connect/angle them; it must not fabricate concrete facts (numbers,
 # employers, credentials) absent from every atom. New true facts surface to
 # Jordan Avery at review (he is the oracle who promotes worthy ones to new primitives).
    primitives_section = ""
    if ground.primitives_block:
        primitives_section = (
            "\n\n" + ground.primitives_block +
            "\n\nHOW TO USE PRIMITIVES (your PRIMARY basis material): compose from "
            "these atoms. Pick the few MOST relevant to THIS role and lead with "
            "them. You are encouraged to CONNECT atoms and angle them for the role "
            "(position the rare combination as one capability; lead with value, not "
            "tools). Use the `tags` only as a hint to relevance. HARD RULE: do not "
            "fabricate a concrete fact (a number, employer, credential, or capability) "
            "that is not in some atom; where you need one that is missing, leave a "
            "[FILL: <specific question>] placeholder. Reasonable paraphrase and "
            "role-relevant framing are expected and fine. The Stage 3 verifier treats "
            "these atoms as grounding."
        )

    base = (
        f"ROLE:\n"
        f"  Company:   {company}\n"
        f"  Title:     {role}\n"
        f"  URL:       {url}\n"
        f"  Archetype: {archetype}\n"
        f"  Rationale: {rationale}\n\n"
        f"JD BODY (verbatim from {ground.jd_source}, {ground.jd_chars} chars retrieved):\n"
        f"{jd_excerpt}"
        f"{primitives_section}"
        f"\n\nOPERATOR'S HISTORY (supplementary — source: templates/{ground.operator_variant}, "
        f"verbatim; the PRIMITIVES above are the primary basis, this is extra context "
        f"and a voice reference):\n"
        f"{history_excerpt}"
        f"{company_block}"
        f"{canonical_framing_section}"
        f"{experience_block}"
        "\n\nGLOBAL VOICE RULE (applies to every section you write): no lone-wolf / "
        "defensive / overclaim filler — do not write 'solo', 'single-handedly', "
        "'personally', 'by myself', 'not just a...', or 'strongest / world-class / "
        "cutting-edge'. They read as insecurity and undercut the claim. State what "
        "Jordan Avery built and owned (hands-on, full ownership, end to end) and let the "
        "breadth prove he did it. Honest scoping ('schema literacy, not a built "
        "integration') is fine and encouraged."
    )

    if stage == "resume":
        return base + (
            "\n\nYOUR JOB: write a resume DRAFT for Jordan Avery targeted at this role.\n\n"
            "CONSTRAINTS\n"
            "- Plain markdown, no docx structure\n"
            "- Top section: name + contact + headline (1 line) + summary (3-4 sentences)\n"
            "- Then experience section: each role with company, title, dates, 3-5 bullets\n"
            "- Then technical/skills section\n"
            "- Then education\n"
            "- Total length: fits one printed page if rendered (~700 words)\n"
            "- Jordan Avery's voice: direct, evidence-driven, no em dashes, no 'passionate', "
            "no 'leveraged', no 'synergies' (see also the GLOBAL VOICE RULE above)\n"
            "- Bullets lead with outcome, not activity\n"
            "- Numbers and names whenever defensible — drawn from the PRIMITIVES "
            "(primary), or JORDAN AVERY'S HISTORY / EXPERIENCE EXTRAS. If you need a specific "
            "fact none of them contain, leave a [FILL: <specific question>] "
            "placeholder rather than inventing.\n"
            "- Do not fabricate a concrete fact (number, employer, credential) absent "
            "from the PRIMITIVES, JORDAN AVERY'S HISTORY, and EXPERIENCE EXTRAS.\n"
            "- The resume is LARGELY COMPANY-AGNOSTIC. Present Jordan Avery's real experience "
            "angled toward THIS role's needs (read the JD for required signals), but "
            "company-specific claims (about the target company's product, customers, "
            "strategy, leadership, market position) belong in the COVER LETTER and "
            "STRATEGY, not the resume. The resume's job is scannable evidence of Jordan Avery; "
            "the cover/strategy's job is to argue the company-specific fit.\n"
        )

    if stage == "cover":
        prior_resume = (prior_artifacts or {}).get("resume", "")
        return base + (
            f"\n\nRESUME DRAFT YOU JUST WROTE (for fact-consistency reference only):\n"
            f"{prior_resume[:3000]}\n\n"
            "YOUR JOB: write a cover letter DRAFT for Jordan Avery targeted at this role.\n\n"
            "CONSTRAINTS\n"
            "- First person, Jordan Avery's voice\n"
            "- No em dashes, no 'passionate', no consulting-speak (see also the "
            "GLOBAL VOICE RULE above)\n"
            "- Opens with a specific reason for THIS role specifically (not generic interest)\n"
            "- Names 2-3 candidate-evidence pairs (from the PRIMITIVES above, or JORDAN AVERY'S "
            "HISTORY) that map to JD signals\n"
            "- Closes with what Jordan Avery would bring in the first 90 days\n"
            "- Sign off plainly (no 'Sincerely yours' theater)\n"
            "- 3-5 short paragraphs\n"
            "- Do not invent candidate-experience. [FILL: ...] placeholders where needed.\n"
            "- INDEPENDENT NARRATIVE VOICE. Reference the resume's facts and the angle "
            "it chose, but do NOT reuse its phrasing or collapse its bullets verbatim. "
            "The cover is a narrative argument for the fit; the resume is scannable "
            "evidence. Different jobs, different prose. Fact-consistency yes; "
            "text-reuse no.\n"
            "- Company-specific claims (about the target company's product, customers, "
            "strategy, leadership) belong HERE in the cover — anchor them in the COMPANY "
            "CONTEXT block above (CONFIRMED dimensions only) and tie them to Jordan Avery's "
            "evidence, not the resume's prose.\n"
        )

    if stage == "questions":
        prior_resume = (prior_artifacts or {}).get("resume", "")
        prior_cover  = (prior_artifacts or {}).get("cover", "")
        return base + (
            f"\n\nRESUME DRAFT (above) contains [FILL: ...] placeholders:\n"
            f"{prior_resume[:3000]}\n\n"
            f"COVER LETTER DRAFT contains [FILL: ...] placeholders:\n"
            f"{prior_cover[:1500]}\n\n"
            "YOUR JOB: list the 5-12 targeted questions Jordan Avery needs to answer to fill "
            "every [FILL: ...] placeholder. Each question narrow, specific, answerable "
            "in 1-2 sentences. Number them.\n\n"
            "OUTPUT FORMAT (exactly this)\n"
            "1. <question>\n"
            "   placeholder: [FILL: <verbatim placeholder from above>]\n"
            "2. <question>\n"
            "   placeholder: [FILL: <...>]\n"
            "...\n"
        )

    if stage == "outreach_targets":
 # outreach_targets is JD-driven research — it identifies decision-makers
 # and warm-path categories from the role context. It does NOT consume
 # resume / cover / strategy / questions; sequencing it parallel to
 # resume (right after GROUND) keeps the dependency graph honest. The
 # preservation rule (don't overwrite a pre-seeded outreach-targets.md,
 # e.g., Northwind Systems Jim Viris warm path) is enforced at the write site, not
 # here — the prompt is identical regardless.
        return base + (
            "\n\nYOUR JOB: research outreach targets for Jordan Avery's application to "
            "this role. Identify likely decision-makers and surface warm-path "
            "categories Jordan Avery should investigate.\n\n"
            "OUTPUT FORMAT — exactly these sections, no preamble:\n\n"
            "## DECISION-MAKER TARGETS (public-source-identifiable)\n"
            "For each: name (if identifiable from JD or commonly-known role at "
            "company), title, why-them (likely hiring manager / likely first-"
            "screen interviewer / function owner), where-found citation (e.g. "
            "\"company team page\", \"LinkedIn search recommended for VP CS at "
            "<company>\"), and one specific reason this person matters for THIS "
            "role.\n"
            "Be honest when you can't identify by name — say \"likely hiring "
            "manager is the head of <function>; identifiable via LinkedIn "
            "search at <company>\". Don't fabricate names.\n\n"
            "## WARM-PATH CATEGORIES TO INVESTIGATE\n"
            "List the connection categories worth Jordan Avery searching his own "
            "network for:\n"
            "- alma mater (note specific schools that show up in company "
            "leadership / engineering)\n"
            "- prior-employer overlap (companies in <company>'s history Jordan Avery "
            "has worked at or near)\n"
            "- industry mutual-connection candidates (verticals Jordan Avery has "
            "credibility in that overlap with this company's customer base)\n"
            "- mutual investor/board connections if applicable\n\n"
            "## CANDIDATE-PROVIDED WARM PATHS\n"
            "(Leave this section as a labeled placeholder for Jordan Avery to fill in "
            "via chat with personal-network intel: \"I know <name>\", \"<name> "
            "is my <relationship>\", \"connected via <person> on LinkedIn\". "
            "Format the placeholder so it's clear Jordan Avery should write here.)\n\n"
            "## RECOMMENDED APPROACH\n"
            "One paragraph: should Jordan Avery apply cold, pursue warm intro first, "
            "or both in parallel? Justified by the targets + paths above. "
            "Honest when the answer is \"apply cold, no warm path obvious "
            "yet\".\n\n"
            "CONSTRAINTS\n"
            "- Direct, evidence-driven, no em dashes\n"
            "- Don't fabricate names of specific people — identify by role/"
            "title if name not known\n"
            "- Don't pad with generic outreach advice\n"
        )

    if stage == "strategy":
        prior_resume    = (prior_artifacts or {}).get("resume", "")
        prior_cover     = (prior_artifacts or {}).get("cover", "")
        prior_questions = (prior_artifacts or {}).get("questions", "")
        return base + (
            f"\n\nRESUME DRAFT (consistent narrative with this strategy):\n"
            f"{prior_resume[:2500]}\n\n"
            f"COVER LETTER DRAFT (consistent narrative with this strategy):\n"
            f"{prior_cover[:1500]}\n\n"
            f"TARGETED QUESTIONS — [FILL: ...] items Jordan Avery hasn't yet confirmed:\n"
            f"{prior_questions[:2000]}\n\n"
            "YOUR JOB: write the application-strategy note for Jordan Avery's screen prep.\n\n"
            "Cover:\n"
            "1. THE STORY YOU'RE TELLING — the narrative arc the resume + cover both support\n"
            "2. TRANSFER ANGLES YOU'RE LEANING ON — JD-signal-to-candidate-evidence pairs that "
            "justify the fit (must match what's in the resume/cover above)\n"
            "3. EMPHASIZE AT FIRST SCREEN — 2-3 things Jordan Avery should make sure to mention if not asked\n"
            "4. KNOWN RISKS the hiring manager will surface — and how to pre-empt\n"
            "5. WHAT'S STILL SOFT IN YOUR STORY — the open [FILL: ...] items above are "
            "things Jordan Avery needs to confirm before the screen. Call out the top 2-3 that "
            "would most weaken the narrative if asked and Jordan Avery can't answer crisply. "
            "Tell Jordan Avery specifically what to prepare for each.\n\n"
            "CONSTRAINTS\n"
            "- Direct, evidence-driven, no em dashes\n"
            "- Specific, not generic\n"
            "- This is Jordan Avery's screen prep, not marketing copy\n"
            "- STAY CONSISTENT with the resume + cover above. If they emphasize X, "
            "this strategy must emphasize X. If they don't claim Y, this strategy "
            "must not claim Y.\n"
        )

    raise ValueError(f"assemble_prompt: unknown stage {stage!r}")


# ─── STAGE 2 — WRITE ─────────────────────────────────────────────────────────

def stage_write(*, ground: GroundResult, company: str, role: str, url: str,
                archetype: str, rationale: str = "") -> WriteResult:
    """Stage 2: generate artifacts in dependency order.

    Order: resume → cover (sees resume) → questions (sees resume + cover) →
    strategy (sees resume + cover). Each artifact is its own llm.call with
    a stage-scoped prompt. Consistency becomes a property of generation
    order, not hope.
    """
    if ground.halted or not ground.required_complete():
        return WriteResult(
            halted=True,
            halt_reason=(
                "Stage 2 cannot run: Stage 1 did not complete. "
                f"({ground.halt_reason or 'required inputs missing'})"
            ),
        )

    write = WriteResult()
    brief = llm.load_alice_brief()

 # Artifact 1: resume
    resume_prompt = assemble_prompt(
        stage="resume", ground=ground, company=company, role=role,
        url=url, archetype=archetype, rationale=rationale,
    )
    print(f"  [stage_write: resume (model: {llm.MODEL_FOR_TASK.get('resume_draft', '?')})]")
 # max_tokens must cover BOTH adaptive-thinking tokens AND the visible draft.
 # opus-4-7 thinking alone consumed the old 2500 cap (stop_reason=max_tokens,
 # empty text). Give the resume real headroom for thinking + output.
    res = llm.call("resume_draft", resume_prompt, system=brief, max_tokens=10000)
    write.resume = res["text"]
    write.artifact_costs["resume"]  = res["cost_usd"]
    write.artifact_models["resume"] = res["model"]
    write.artifacts_generated.append("resume")
    if res.get("stop_reason") == "max_tokens":
 # Fail loud per P2: the resume was truncated. We still proceed but
 # flag at the result level so callers see the incomplete state.
        write.halt_reason = (
            (write.halt_reason or "") +
            f" [resume hit max_tokens — content truncated]"
        )

 # Artifact 2: cover letter (sees resume)
    cover_prompt = assemble_prompt(
        stage="cover", ground=ground,
        prior_artifacts={"resume": write.resume},
        company=company, role=role, url=url, archetype=archetype,
        rationale=rationale,
    )
    print(f"  [stage_write: cover (model: {llm.MODEL_FOR_TASK.get('cover_letter_draft', '?')})]")
 # Headroom for adaptive thinking + the visible cover (old 1500 cap was fully
 # consumed by thinking once grounding got richer, yielding empty text).
    res = llm.call("cover_letter_draft", cover_prompt, system=brief, max_tokens=6000)
    write.cover = res["text"]
    write.artifact_costs["cover"]  = res["cost_usd"]
    write.artifact_models["cover"] = res["model"]
    write.artifacts_generated.append("cover")
    if res.get("stop_reason") == "max_tokens":
        write.halt_reason = (
            (write.halt_reason or "") +
            f" [cover hit max_tokens]"
        )

 # Artifact 3: targeted questions (sees both)
    questions_prompt = assemble_prompt(
        stage="questions", ground=ground,
        prior_artifacts={"resume": write.resume, "cover": write.cover},
        company=company, role=role, url=url, archetype=archetype,
        rationale=rationale,
    )
    print(f"  [stage_write: questions (model: {llm.MODEL_FOR_TASK.get('targeted_questions', '?')})]")
    res = llm.call("targeted_questions", questions_prompt, system=brief, max_tokens=1200)
    write.questions = res["text"]
    write.artifact_costs["questions"]  = res["cost_usd"]
    write.artifact_models["questions"] = res["model"]
    write.artifacts_generated.append("questions")

 # Artifact 4: application strategy (sees resume + cover + questions)
    strategy_prompt = assemble_prompt(
        stage="strategy", ground=ground,
        prior_artifacts={
            "resume":    write.resume,
            "cover":     write.cover,
            "questions": write.questions,
        },
        company=company, role=role, url=url, archetype=archetype,
        rationale=rationale,
    )
    print(f"  [stage_write: strategy (model: {llm.MODEL_FOR_TASK.get('application_strategy', '?')})]")
    res = llm.call("application_strategy", strategy_prompt, system=brief, max_tokens=1500)
    write.strategy = res["text"]
    write.artifact_costs["strategy"]  = res["cost_usd"]
    write.artifact_models["strategy"] = res["model"]
    write.artifacts_generated.append("strategy")

 # Artifact 5: outreach targets (parallel-to-resume — depends only on GROUND).
 # Preservation rule lives in run_pipeline at the write site: if a
 # pre-seeded outreach-targets.md exists on disk (e.g., Northwind Systems's hand-
 # curated Jim Viris warm path), skip overwriting. We still generate the
 # content here so callers that ignore the disk file can use write.outreach;
 # the disk-write decision is per-file at the file boundary.
    outreach_prompt = assemble_prompt(
        stage="outreach_targets", ground=ground,
        company=company, role=role, url=url, archetype=archetype,
        rationale=rationale,
    )
    print(f"  [stage_write: outreach_targets (model: {llm.MODEL_FOR_TASK.get('application_strategy', '?')})]")
    res = llm.call("application_strategy", outreach_prompt, system=brief, max_tokens=1500)
    write.outreach = res["text"]
    write.artifact_costs["outreach"]  = res["cost_usd"]
    write.artifact_models["outreach"] = res["model"]
    write.artifacts_generated.append("outreach")

    return write


# ─── STAGE 3 — VERIFY ────────────────────────────────────────────────────────

# Pattern-based v1 verifier. The principle (which converges to claim-level
# faithfulness in v2): every concrete factual claim in a generated artifact
# should either (a) appear in JORDAN AVERY'S HISTORY, (b) appear in the JD body, or
# (c) be a generic phrasing that asserts no specific fact. The detectors
# below catch the highest-frequency fabrication shapes from the live record:

# - dollar amounts not in ground ("$5M", "$25M+", "$800K")
# - specific company names not in JD or history (a named aerospace OEM)
# - specific date ranges / time spans ("2022-2023", "10 years")
# - "the candidate has X years of Y" claims where Y is not in ground

_DOLLAR_RE = re.compile(r"\$\s?\d+(?:\.\d+)?\s?[KkMmBb]?\+?")
_YEAR_RANGE_RE = re.compile(r"\b(?:19|20)\d{2}\s?[-–to]+\s?(?:19|20|present|Present|PRESENT)\d{0,2}\b")
_X_YEARS_RE = re.compile(r"\b(\d+)\+?\s+years?\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?%\+?\b")
_LARGE_NUM_RE = re.compile(r"\b\d{2,3}(?:,\d{3})+\b")  # comma-separated (e.g. 100,000)


def _claims_in(text: str) -> list[tuple[str, str]]:
    """Return [(claim_type, claim_token), ...] for every claim-like token."""
    if not text:
        return []
    claims = []
    for m in _DOLLAR_RE.finditer(text):
        claims.append(("dollar", m.group(0).strip()))
    for m in _YEAR_RANGE_RE.finditer(text):
        claims.append(("date_range", m.group(0).strip()))
    for m in _X_YEARS_RE.finditer(text):
        claims.append(("x_years", m.group(0).strip()))
    for m in _PERCENT_RE.finditer(text):
        claims.append(("percent", m.group(0).strip()))
    for m in _LARGE_NUM_RE.finditer(text):
        claims.append(("large_num", m.group(0).strip()))
    return claims


def _claim_appears_in_ground(claim_token: str, ground_blob: str) -> bool:
    """A claim is grounded if it appears verbatim (with light normalization)
    in any retrieved ground material. This is the conservative check; v2
    would allow paraphrase / unit conversion."""
    if not ground_blob:
        return False
 # Normalize whitespace and case for comparison
    norm_blob = " ".join(ground_blob.split()).lower()
    norm_claim = " ".join(claim_token.split()).lower()
    if norm_claim in norm_blob:
        return True
 # Tolerate '$25M+' vs '$25 million+' and similar
    if claim_token.startswith("$"):
 # Strip + and try
        stripped = claim_token.rstrip("+").lower().replace(" ", "")
        if stripped in norm_blob.replace(" ", ""):
            return True
    return False


def _company_claim_lines(text: str, company: str) -> list[str]:
    """Return sentences from artifact text that make a claim about the company.

    Heuristic: any line containing the company name (case-insensitive) OR
    "<Company>'s ..." possessive. These are the lines that should be
    grounded in company_research's CONFIRMED sections, not just in the JD.
    """
    if not text or not company:
        return []
    cname = company.strip()
    if not cname:
        return []
 # Split on sentence boundaries; keep medium-coarse so possessive phrases
 # stay attached to their assertion. Filter to lines that name the company.
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\[])", text)
    needle = cname.lower()
    return [s.strip() for s in sentences if needle in s.lower() and s.strip()]


# ─── Banned framing / anonymization (project HARD rules) ─────────────────────
# Ironclad Industrial must NEVER be (a) named or identifiable, or (b) framed as
# a Cadence Analytics customer / tenant / pilot / design-partner. It is the
# INSPIRATION and problem-source for Cadence Analytics, not a customer
# relationship. Grounding does not catch this: a confabulated "design-partner
# relationship with one manufacturer" can score highly grounded yet be a hard
# violation. These checks run independently of grounding and trigger a withhold
# (drafts not shipped).
_GR_NAME = re.compile(r"\bironclad(?:[\s\-]?industrial)?\b", re.I)
# A reference to the (anonymized) manufacturer. The bare word "manufacturer" is
# enough for the framing check below — but only when paired with a relationship
# term, so an application TO a manufacturing company is not false-flagged.
_MANUFACTURER_REF = re.compile(r"\bmanufacturer\b|\bironclad(?:[\s\-]?industrial)?\b", re.I)
# STRONG relationship terms: applied to "the/a manufacturer" these are
# unambiguously the banned Cadence-relationship framing (a target manufacturing
# company would never be the candidate's "design partner / pilot / tenant").
_STRONG_REL = re.compile(
    r"\b(design[\s\-]?partner(?:ship)?|pilot(?:ed|\s+customer|\s+program|\s+partner)?|"
    r"\btenant\b|beta\s+(?:customer|partner|tester)|first\s+customer|paying\s+customer)\b", re.I)
# WEAK relationship terms (customer/client) are ambiguous — only banned when the
# sentence also invokes Cadence Analytics (i.e. asserts the manufacturer is
# Cadence's customer).
_WEAK_REL = re.compile(r"\b(customers?|clients?|subscribers?|accounts?)\b", re.I)
_CADENCE_CTX = re.compile(r"\bcadence(?:\s+analytics)?\b", re.I)


# Protest / lone-wolf / overclaim filler the WRITER must not generate. These read
# as insecurity and undercut the very claim they attach to; the confident version
# states what Jordan Avery did and trusts the reader. The same regex keeps the
# primitives corpus clean — applied here to drafts so the cron cannot re-introduce
# it at generation time. Word-boundaried to avoid false hits (e.g. "alone" inside
# "standalone"). Honest *scoping* negation ("not a built integration") is
# deliberately NOT matched — only confidence-protesting "not just / not merely" +
# lone-wolf + overclaim terms.
# "on my own" / "all by myself" are deliberately EXCLUDED — they false-fire on
# possessive noun phrases ("guardrails on my own agents" = agents I built, not
# lone-wolf). "by myself" is unambiguous; the rare adverbial "on my own" is left
# to prompt guidance + Jordan Avery's review.
_PROTEST_RE = re.compile(
    r"\b(solo|single-?handed(?:ly)?|personally|by (?:my|him)self|"
    r"not just (?:a|an|the)|not merely|not only a|"
    r"world-?class|cutting-?edge|state-of-the-art|best-in-class|unmatched|"
    r"strongest (?:available )?evidence)\b", re.I)


def _protest_language_hits(text: str) -> list[dict]:
    """Detect lone-wolf / defensive / overclaim filler in a draft. Returns
    {"type": "protest_language", "token": <phrase + context>} hits. Warning-level:
    surfaced to Jordan Avery, never auto-stripped (flag-to-human)."""
    hits: list[dict] = []
    if not text:
        return hits
    for m in _PROTEST_RE.finditer(text):
        a, b = max(0, m.start() - 30), min(len(text), m.end() + 30)
        hits.append({"type": "protest_language",
                     "token": f"...{text[a:b].strip()}..."})
    return hits


def _banned_framing_hits(text: str) -> list[dict]:
    """Detect CLAUDE.md banned-framing / anonymization violations in an artifact.

    Returns a list of {"type", "token"} hits:
      - "anonymization_breach": the literal Ironclad Industrial name appears.
      - "gr_customer_framing": a manufacturer reference paired with a relationship
        term that asserts the banned customer/pilot/design-partner relationship.
    Conservative by design: weak terms (customer/client) only fire alongside an
    explicit Cadence Analytics mention, so applying to a manufacturing company is
    not flagged.
    """
    hits: list[dict] = []
    if not text:
        return hits
    for m in _GR_NAME.finditer(text):
        a, b = max(0, m.start() - 50), min(len(text), m.end() + 50)
        hits.append({"type": "anonymization_breach", "token": text[a:b].strip()})
    for sent in re.split(r"(?<=[.!?])\s+", text):
        if not _MANUFACTURER_REF.search(sent):
            continue
        strong = _STRONG_REL.search(sent)
        weak_in_cadence_ctx = _WEAK_REL.search(sent) and _CADENCE_CTX.search(sent)
        if strong or weak_in_cadence_ctx:
            hits.append({"type": "gr_customer_framing", "token": sent.strip()[:180]})
    return hits


# ─── Value-led check (positioning North Star #4: value, not tools) ───────────
# A resume summary / cover OPENING that leads with a tool/tech name before any
# business outcome reads as "junior engineer describing plumbing" (per the
# strategy spine + Jordan Avery's own framing). Quality WARNING, not a withhold — tools
# deeper in the body are fine; only the headline region is checked.
_TOOL_TOKENS = re.compile(
    r"\b(xgboost|random survival forest|random forest|react|supabase|cloudflare|"
    r"twilio|duckdb|opentelemetry|\botel\b|vercel|\bmcp\b|postgres(?:ql)?|langchain|"
    r"langgraph|kubernetes|docker|pytorch|tensorflow|scikit)\b", re.I)
_VALUE_SIGNAL = re.compile(
    r"\$[\d,]|\d+\s*%|\b(surfaced|recovered|grew|drove|sold|cut|closed|reduced|increased|"
    r"adopted|shipped|delivered|saved|won|expanded|retained|generated|built\s+a)\b", re.I)


def _leads_with_tools(text: str, artifact: str) -> dict | None:
    if artifact not in ("resume", "cover") or not text:
        return None
    head = text[:280]
    m = _TOOL_TOKENS.search(head)
    if not m:
        return None
    if _VALUE_SIGNAL.search(head[:m.end()]):   # a value signal leads or co-leads -> fine
        return None
    return {"type": "leads_with_tools",
            "token": f"{artifact} opens with '{m.group(0)}' before any outcome — "
                     f"lead with value created; demote the tool to credibility"}


def stage_verify(*, write: WriteResult, ground: GroundResult,
                 company: str = "") -> VerifyResult:
    """Stage 3: faithfulness check.

    For each artifact, extract claim-like tokens; verify each appears in
    the retrieved ground (Jordan Avery's history OR the JD body). Flagged claims
    are reported per-artifact. Stripping is not done automatically here
    (would corrupt the artifact text); we report so the caller decides
    whether to ship as-is, regenerate, or surface to Jordan Avery.

    Company-research grounding rule: when `company_research_source` is
    `web_research_v1`, only the CONFIRMED-marked sections enter the
    ground_blob. UNCLEAR / NOT_FOUND sections are explicitly excluded so a
    "NOT_FOUND: PEOPLE" line cannot ground a fabricated "VP of CS" claim.
    For candidate-curated files and JD-only fallback, the existing behavior
    (include all research text) is preserved.
    """
    result = VerifyResult()
    if write.halted or not write.artifacts_generated:
        return result

 # CONFIRMED-only gating for web_research_v1; full text otherwise.
    if ground.company_research_source == "web_research_v1":
        company_ground = _confirmed_sections_of(ground.company_research or "")
    else:
        company_ground = ground.company_research or ""

 # Experience-store dual-landing: every confirmed entry's verbatim becomes
 # grounding for Stage 3, parallel to its role in Stage 2's prompt. This
 # is the seam that closes the loop — a writer that anchors a claim in a
 # confirmed verbatim cannot be flagged by the verifier; a writer that
 # fabricates a number not in the variant + JD + company-confirmed +
 # experience blob WILL be flagged.

 # The canonical_framing block is ALSO experience-source grounding: writers
 # render claims from it (it's surfaced as its own block above EXPERIENCE
 # EXTRAS in Stage 2), so the verifier must treat those claims as grounded.
 # Adding canonical_framing_block to experience_ground keeps the seam
 # closed across both file-authored population paths (beats AND framing).
    experience_ground = "\n\n".join(
        e.get("verbatim", "") for e in (ground.experience_extras_entries or [])
        if e.get("verbatim")
    )
    if ground.canonical_framing_block:
        experience_ground = (experience_ground + "\n\n" + ground.canonical_framing_block).strip()

 # Per-source attribution map. Ordered most-specific first: experience
 # (candidate-confirmed verbatim) > history (resume variant) > company
 # (confirmed company research) > jd (JD body). attribute_claim returns
 # the first source name that contains the claim token.
    from alice.persistence import experience_store
    sources_for_attribution = {
        "primitives": ground.primitives_block or "",   # PRIMARY basis material
        "experience": experience_ground,
        "history":    ground.operator_history or "",
        "company":    company_ground,
        "jd":         ground.jd_body or "",
    }

    ground_blob = (
        (ground.primitives_block or "") + "\n\n" +
        (ground.operator_history or "") + "\n\n" +
        (ground.jd_body or "") + "\n\n" +
        company_ground + "\n\n" +
        experience_ground
    )

    artifacts = {
        "resume":    write.resume,
        "cover":     write.cover,
        "strategy":  write.strategy,
        "questions": write.questions,
        "outreach":  write.outreach,
    }

    total_claims = 0
    total_grounded = 0
    total_flagged = 0

    for name, text in artifacts.items():
        if not text:
            continue
        verdict = VerifyVerdict(artifact=name)
        for ctype, token in _claims_in(text):
            verdict.total_claims += 1
            if _claim_appears_in_ground(token, ground_blob):
                verdict.grounded_claims += 1
 # Record which specific source grounded this claim. The
 # attribution map is the audit trail Jordan Avery reads in
 # .pipeline-metadata.json to see how much of the artifact
 # leans on experience-store entries vs. JD vs. variant.
                src = experience_store.attribute_claim(
                    token, sources_for_attribution
                )
                if src:
                    verdict.attribution[src] = verdict.attribution.get(src, 0) + 1
            else:
                verdict.flagged_claims.append({
                    "type":  ctype,
                    "token": token,
                })

 # Company-claim detector: sentences mentioning the company name must
 # have at least one substantive noun-token grounded in company_ground.
 # v1 substantive check is light — any 5+ char alphanumeric token in
 # the sentence (excluding the company name itself and common prose
 # words) that appears in company_ground passes. Sentences with zero
 # grounded substantive tokens are flagged as "company_claim_ungrounded".
        if company and company_ground:
            company_ground_norm = " ".join(company_ground.split()).lower()
            common_prose = {
                "their", "these", "those", "where", "which", "while", "since",
                "about", "their", "every", "first", "second", "third", "operator",
                "company", "companies", "platform", "product", "customer",
                "customers", "market", "team", "teams", "would", "could",
                "should", "after", "before", "during", "really", "actually",
                "moving", "running", "having", "being", "going",
            }
            for sent in _company_claim_lines(text, company):
                tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9\-]{4,}\b", sent)
                cname_lc = company.strip().lower()
                substantive = [
                    t for t in tokens
                    if t.lower() not in common_prose
                    and t.lower() != cname_lc
                    and t.lower() not in cname_lc
                ]
                if not substantive:
                    continue  # purely framing sentence — nothing to check
                grounded_any = any(
                    t.lower() in company_ground_norm for t in substantive
                )
                verdict.total_claims += 1
                if grounded_any:
                    verdict.grounded_claims += 1
                else:
 # Cap the flagged-snippet length so verdicts stay readable.
                    verdict.flagged_claims.append({
                        "type":  "company_claim_ungrounded",
                        "token": sent[:160],
                    })

 # Banned-framing / anonymization check — independent of grounding.
 # A grounded claim can still be a project-rule violation (Ironclad Industrial named
 # or framed as a Cadence Analytics customer). These count separately and force a
 # withhold downstream; they also surface as flagged claims for the audit.
        for hit in _banned_framing_hits(text):
            verdict.flagged_claims.append(hit)
            result.banned_framing_count += 1

 # Value-led check (North Star #4) — warning only, no withhold.
        vh = _leads_with_tools(text, name)
        if vh:
            verdict.flagged_claims.append(vh)
            result.value_led_warnings += 1

 # Protest-language check — warning only, no withhold. Catches the writer
 # re-introducing lone-wolf / defensive / overclaim filler at generation
 # time (the corpus is clean; the model composes fresh prose).
        for ph in _protest_language_hits(text):
            verdict.flagged_claims.append(ph)
            result.voice_protest_warnings += 1

 # AI-residue check (Layer-1 universal voice gate) — warning only, no
 # withhold. Reuses the deterministic evals.eval_voice (em-dash +
 # consulting-speak) inline here as well as in offline evals.
        from alice.pipeline import evals
        for v in evals.eval_voice(text)["violations"]:
            verdict.flagged_claims.append({"type": "ai_residue", "token": v})
            result.voice_residue_warnings += 1

        result.verdicts.append(verdict)
        total_claims += verdict.total_claims
        total_grounded += verdict.grounded_claims
        total_flagged += len(verdict.flagged_claims)

    if total_claims > 0:
        result.overall_grounded_pct = round(100 * total_grounded / total_claims, 1)
    result.overall_flagged_count = total_flagged
    return result


# ─── TOP-LEVEL PIPELINE ──────────────────────────────────────────────────────

def run_pipeline(*, company: str, role: str, url: str, archetype: str,
                 rationale: str = "", row_idx: int | None = None,
                 row: dict | None = None, write_to_disk: bool = True,
                 verify_only: bool = False) -> PipelineResult:
    """Top-level entry. Runs Stage 1 (GROUND), Stage 2 (WRITE), Stage 3
    (VERIFY), and writes artifacts to disk in applications/<slug>/.

    Halts at any stage that fails its gate. Returns the PipelineResult
    with halted_at_stage set so callers can report honestly.
    """
    started = datetime.now().isoformat(timespec="seconds")
    slug = slugify(company, role)
    pkg_dir = APPS_DIR / slug
    result = PipelineResult(
        slug=slug, company=company, role=role,
        pkg_dir=str(pkg_dir), ground=GroundResult(),
        started_at=started,
    )

 # Stage 1
    print(f"  [pipeline {slug}: stage 1 GROUND]")
    ground = stage_ground(company=company, role=role, url=url,
                          archetype=archetype, ledger_row=row)
    result.ground = ground
    if ground.halted:
        result.halted_at_stage = "GROUND"
        result.halt_reason = ground.halt_reason
        result.finished_at = datetime.now().isoformat(timespec="seconds")
        return result

 # Stage 2
    print(f"  [pipeline {slug}: stage 2 WRITE]")
    write = stage_write(ground=ground, company=company, role=role,
                        url=url, archetype=archetype, rationale=rationale)
    result.write = write
    if write.halted:
        result.halted_at_stage = "WRITE"
        result.halt_reason = write.halt_reason
        result.finished_at = datetime.now().isoformat(timespec="seconds")
        return result

 # Stage 3
    print(f"  [pipeline {slug}: stage 3 VERIFY]")
    verify = stage_verify(write=write, ground=ground, company=company)
    result.verify = verify
    print(f"    grounded: {verify.overall_grounded_pct}%  flagged: {verify.overall_flagged_count}")
    for v in verify.verdicts:
        if v.flagged_claims:
            print(f"    {v.artifact}: {len(v.flagged_claims)} flag(s) — "
                  f"{[c['token'] for c in v.flagged_claims[:6]]}")

 # HARD block: banned framing / anonymization breach (CLAUDE.md). A grounded
 # but banned draft must NEVER reach disk as a shippable artifact — shipping a
 # CLAUDE.md violation is worse than shipping nothing. Withhold the drafts and
 # write a BLOCKED report so Jordan Avery sees what tripped and can fix the source.
    banned = verify.banned_framing_count > 0
    if banned:
        result.halted_at_stage = "VERIFY"
        result.halt_reason = (
            f"banned framing / anonymization breach ({verify.banned_framing_count} "
            f"hit(s)) — drafts withheld (project hard rule: Ironclad Industrial is never "
            f"named or framed as a Cadence Analytics customer/pilot/design-partner)")
        print(f"    ⛔ BANNED FRAMING: {verify.banned_framing_count} hit(s) — withholding drafts")

 # Write to disk
    if write_to_disk and not verify_only:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        if banned:
            lines = ["# ⛔ DRAFTS WITHHELD — banned framing / anonymization breach", "",
                     result.halt_reason or "", "",
                     "## Offending claims", ""]
            for v in verify.verdicts:
                for c in v.flagged_claims:
                    if c.get("type") in ("anonymization_breach", "gr_customer_framing"):
                        lines.append(f"- **[{v.artifact}] {c['type']}**: {c['token']}")
            lines += ["", "Fix the source (experience files / resume masters / framing-locks), "
                      "then re-run. Drafts are intentionally not written."]
            (pkg_dir / "BLOCKED.md").write_text("\n".join(lines))
            result.files_written.append(str(pkg_dir / "BLOCKED.md"))
        else:
            (pkg_dir / "resume-draft.md").write_text(write.resume or "")
            result.files_written.append(str(pkg_dir / "resume-draft.md"))
            (pkg_dir / "cover-letter-draft.md").write_text(write.cover or "")
            result.files_written.append(str(pkg_dir / "cover-letter-draft.md"))
            (pkg_dir / "targeted-questions.md").write_text(write.questions or "")
            result.files_written.append(str(pkg_dir / "targeted-questions.md"))
            (pkg_dir / "application-strategy.md").write_text(write.strategy or "")
            result.files_written.append(str(pkg_dir / "application-strategy.md"))
 # Preservation rule for outreach-targets.md (mirrors prep_materials.py
 # behavior at lines 236-241): if Jordan Avery pre-seeded this file with warm-
 # path intel he hand-curated (e.g., Northwind Systems's Erin Jim Viris path),
 # do not overwrite. The generated text still flows back to callers
 # via write.outreach if they want to consume it directly.
        outreach_path = pkg_dir / "outreach-targets.md"
        if banned:
            pass  # withheld with the other drafts
        elif outreach_path.exists():
            print(f"    outreach-targets.md exists (pre-seeded, not overwriting)")
        else:
            outreach_path.write_text(write.outreach or "")
            result.files_written.append(str(outreach_path))

 # Metadata + verification report
        meta = {
            "slug":          slug,
            "company":       company,
            "role":          role,
            "url":           url,
            "archetype":     archetype,
            "rationale":     rationale,
            "row_idx":       row_idx,
            "pipeline_version": "v1",
            "started_at":    started,
            "finished_at":   datetime.now().isoformat(timespec="seconds"),
            "ground": {
                "jd_chars":              ground.jd_chars,
                "jd_source":             ground.jd_source,
                "operator_variant":          ground.operator_variant,
                "company_research_source": ground.company_research_source,
                "company_research_incomplete": ground.company_research_incomplete,
                "experience_entries_used": [
                    e.get("entry_id") for e in ground.experience_extras_entries
                ],
                "experience_entries_count": len(ground.experience_extras_entries),
                "retrieval_steps":       len(ground.retrieval_log),
            },
            "write": {
                "artifacts_generated":   write.artifacts_generated,
                "artifact_costs":        write.artifact_costs,
                "artifact_models":       write.artifact_models,
            },
            "verify": {
                "overall_grounded_pct":  verify.overall_grounded_pct,
                "overall_flagged_count": verify.overall_flagged_count,
                "banned_framing_count":  verify.banned_framing_count,
                "value_led_warnings":    verify.value_led_warnings,
                "voice_protest_warnings": verify.voice_protest_warnings,
                "drafts_withheld":       banned,
                "verdicts":              [asdict(v) for v in verify.verdicts],
            },
        }
        (pkg_dir / ".pipeline-metadata.json").write_text(
            json.dumps(meta, indent=2)
        )
        result.files_written.append(str(pkg_dir / ".pipeline-metadata.json"))

    result.total_cost = sum(write.artifact_costs.values())
    result.finished_at = datetime.now().isoformat(timespec="seconds")
    return result


# ─── self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """Self-test: run Stage 1 against a real sheet row and report.
    Does NOT run Stage 2/3 (those cost LLM dollars). Use the CLI flag
    --full to run the whole pipeline against a chosen row substring.
    """
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--substr", default=None,
                    help="Company substring to find in sheet")
    ap.add_argument("--full", action="store_true",
                    help="Run all three stages (default: stage 1 only)")
    ap.add_argument("--verify-only", action="store_true",
                    help="Run pipeline but don't write artifacts to disk")
    args = ap.parse_args()

    if not args.substr:
        print("usage: prep_pipeline.py --substr <company-substring> [--full] [--verify-only]")
        sys.exit(2)

    from alice.persistence import ledger
    from alice.notify.imap_reply import _match_sheet_row
    ws = ledger._ws()
    rows = ws.get_all_records()
    hits = _match_sheet_row(args.substr, rows)
    if not hits:
        print(f"no match for {args.substr!r}")
        sys.exit(1)
    if len(hits) > 1:
        print(f"ambiguous match for {args.substr!r}: {len(hits)} hits")
        for idx, r in hits[:5]:
            print(f"  row {idx}: {r.get('company')} | {r.get('role')}")
        sys.exit(1)
    row_idx, row = hits[0]
    company = row.get("company", "")
    role    = row.get("role", "")
    url     = row.get("url", "")
    rationale = row.get("rationale", "")
    am = re.match(r"\[([^\]]+)\]", rationale)
    archetype = am.group(1) if am else "Other"

    print(f"=== pipeline against row {row_idx}: {company} | {role} ===")
    if args.full:
        result = run_pipeline(
            company=company, role=role, url=url, archetype=archetype,
            rationale=rationale, row_idx=row_idx, row=row,
            write_to_disk=not args.verify_only,
            verify_only=args.verify_only,
        )
        print()
        print(f"=== RESULT ===")
        print(f"halted_at: {result.halted_at_stage}")
        if result.halt_reason:
            print(f"halt_reason: {result.halt_reason}")
        print(f"files_written: {len(result.files_written)}")
        for f in result.files_written:
            print(f"  - {f}")
        print(f"total_cost: ${result.total_cost:.4f}")
        if result.verify:
            print(f"verify: {result.verify.overall_grounded_pct}% grounded, "
                  f"{result.verify.overall_flagged_count} flagged")
            if result.verify.value_led_warnings:
                print(f"    ⚠ value-led: {result.verify.value_led_warnings} opening(s) lead with tools")
            if result.verify.voice_protest_warnings:
                print(f"    ⚠ protest-language: {result.verify.voice_protest_warnings} "
                      f"lone-wolf/defensive/overclaim phrase(s) — review and tighten")
    else:
 # Stage 1 only — costs nothing, exercises the halt logic
        ground = stage_ground(company=company, role=role, url=url,
                              archetype=archetype, ledger_row=row)
        print()
        print(f"=== STAGE 1 RESULT ===")
        print(f"halted: {ground.halted}")
        if ground.halt_reason:
            print(f"halt_reason: {ground.halt_reason}")
        print(f"jd_chars: {ground.jd_chars}")
        print(f"operator_variant: {ground.operator_variant}")
        print(f"company_research_source: {ground.company_research_source}")
        print(f"company_research_incomplete: {ground.company_research_incomplete}")
        print(f"retrieval_log ({len(ground.retrieval_log)} steps):")
        for step in ground.retrieval_log:
            print(f"  - {step}")
