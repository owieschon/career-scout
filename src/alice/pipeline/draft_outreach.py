"""Outreach drafting — triggered when a focus role moves to 'submitted'.

For each newly-submitted application without outreach-drafts.md yet, Alice:
  1. Reads applications/<slug>/outreach-targets.md (if pre-seeded; e.g., a hiring-manager contact intel)
  2. Identifies additional targets from public company sources (best-effort)
  3. Drafts a LinkedIn DM + cold email per target, in Jordan's voice
  4. Suggests send order + timing
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger

APPS_DIR = Path(str(repo_paths.APPLICATIONS))
WARM_PATHS_FILE = Path(str(repo_paths.FEEDBACK / "warm-paths-personal.md"))


def _slug(company, role):
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def _load_warm_paths_block() -> str:
    """Return the user-curated section of feedback/warm-paths-personal.md
    for injection into the outreach prompt, or '' if the file is empty or
    contains only the template scaffolding.

    Decision: "populated" = at least one non-comment, non-blank list-item
    or paragraph line outside the example HTML comments. If only the
    section headers + example comments are present, treat as empty and
    return ''. Alice does NOT fabricate warm paths from an empty file.
    """
    if not WARM_PATHS_FILE.exists():
        return ""
    raw = WARM_PATHS_FILE.read_text()
 # Strip HTML comment blocks (the examples) so we evaluate populated content.
    no_comments = re.sub(r"<!--[\s\S]*?-->", "", raw)
 # Real content lines: non-empty, not pure markdown chrome (# / --- / placeholder parens).
    real_lines = []
    for line in no_comments.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("---"):
            continue
 # Skip parenthesized "Empty until populated" placeholders.
        if re.match(r"^\([^)]+\)\.?$", stripped):
            continue
        real_lines.append(stripped)
 # Drop the "How Alice references this file" instructional paragraph by
 # checking we have at least one list-item or attribution line that's
 # NOT chrome. List-item heuristic: starts with "-" or "*".
    item_lines = [l for l in real_lines if l.startswith(("-", "*"))]
    if not item_lines:
        return ""
 # Populated. Return the whole file (Alice gets full context including
 # section headers — the model is good at scanning structured prose).
    return raw


def _newly_submitted():
    """Find sheet rows with status='submitted' where applications/<slug>/outreach-drafts.md doesn't exist yet."""
    ws = ledger._ws()
    rows = ws.get_all_records()
    out = []
    for i, r in enumerate(rows, start=2):
        if (r.get("status") or "").strip().lower() != "submitted":
            continue
        company = r.get("company", "")
        role = r.get("role", "")
        slug = _slug(company, role)
        pkg_dir = APPS_DIR / slug
        drafts = pkg_dir / "outreach-drafts.md"
        if drafts.exists():
            continue
        out.append({"row_idx": i, "row": r, "slug": slug, "pkg_dir": pkg_dir})
    return out


def generate_outreach(row, pkg_dir):
    """Generate outreach-drafts.md (and outreach-targets.md if not pre-seeded)."""
    company = row.get("company", "")
    role = row.get("role", "")
    url = row.get("url", "")
    pkg_dir.mkdir(parents=True, exist_ok=True)

 # Pre-seeded targets (e.g., a named hiring-manager contact)?
    targets_path = pkg_dir / "outreach-targets.md"
    pre_seeded_targets = targets_path.read_text() if targets_path.exists() else None

 # Warm-path inventory (Jordan-curated, never auto-edited). Pulled only
 # when populated; if the file has only template scaffolding, the
 # injection is silent — Alice will NOT fabricate warm paths from an
 # empty seed file. See feedback/warm-paths-personal.md for usage.
    warm_paths_block = _load_warm_paths_block()

 # Read application-strategy + cover-letter-final for voice/context
    strategy = ""
    cover = ""
    for fname in ("application-strategy.md", "cover-letter-final.md", "cover-letter-draft.md"):
        p = pkg_dir / fname
        if p.exists():
            if "strategy" in fname:
                strategy = p.read_text()
            elif "cover" in fname and not cover:
                cover = p.read_text()

    brief = llm.load_alice_brief()
    prompt = f"""Draft the outreach package for Jordan's just-submitted application at {company}.

ROLE
Company: {company}
Title: {role}
URL: {url}

PRE-SEEDED TARGETS (Jordan-surfaced contact intel — if present, USE these as primary targets)
{pre_seeded_targets if pre_seeded_targets else "(none — generate targets from public knowledge of company)"}

JORDAN'S WARM-PATH INVENTORY (from feedback/warm-paths-personal.md — Jordan-curated network. SCAN for any
name + company pair that resolves {company}. If a match exists, ROUTE the warm path; surface the
relationship snippet attached. If no match, omit the warm-path section entirely — do NOT fabricate.)
{warm_paths_block if warm_paths_block else "(warm-path file is unpopulated — proceed with public-research targets only; do not invent warm paths)"}

APPLICATION STRATEGY (your own earlier note)
{strategy[:2000] if strategy else "(no prior strategy doc)"}

COVER LETTER OPENING (for voice consistency)
{cover[:1000] if cover else "(no cover letter)"}

YOUR JOB
Produce `outreach-drafts.md` with the following structure for each target (up to 4 targets max):

## TARGET: <Name>, <Role>
**Why this target:** <1 sentence>
**Channel:** <LinkedIn DM | cold email | warm intro request via X>
**Send timing:** <e.g., "within 24h of application", "3-day wait after warm intro request">

### Draft message
<the actual message text, ready for Jordan to copy-paste and send>
<Constraints: LinkedIn DM under 1000 chars, cold email under 150 words.>
<Jordan's voice: direct, specific, no em dashes, no "passionate", no consulting-speak.>
<Open with something SPECIFIC about THIS person or company, not a generic interest hook.>
<Reference one specific Jordan-evidence pair (a Lattice Additive customer example, Cadence Analytics, etc.).>
<One-line ask. No multi-paragraph asks.>

If pre-seeded targets specify a warm-path (e.g., a hiring manager via a mutual connection),
follow that path exactly. Don't override Jordan's network intel.

End with a SEND ORDER section:
## Send order + timing
1. <target> — <when>
2. <target> — <when, conditional on previous response>
...

If no pre-seeded contact intel exists and you can't credibly identify decision-makers
from public knowledge, write a brief note explaining the gap and recommend Jordan
share LinkedIn URLs for hiring manager + recruiter via 'paste <substring>: <url>' directive.
"""
    print(f"    drafting outreach (model: {llm.MODEL_FOR_TASK['outreach_draft']})...")
    res = llm.call("outreach_draft", prompt, system=brief, max_tokens=2500)
    drafts_path = pkg_dir / "outreach-drafts.md"
    drafts_path.write_text(res["text"])
    return {"path": str(drafts_path), "cost": res["cost_usd"]}


def main():
    newly = _newly_submitted()
    if not newly:
        print("[outreach: no newly-submitted apps without drafts]")
        try:
            from alice.persistence import activity_log
            activity_log.record(step="draft_outreach",
                                summary="no newly-submitted apps awaiting drafts",
                                count=0, status="noop")
        except Exception as e:
            print(f"[activity_log: {e}]")
        return
    total = 0.0
    drafted = []
    for entry in newly:
        company = entry["row"].get("company", "")
        role = entry["row"].get("role", "")
        print(f"  [outreach: drafting for {company} - {role}]")
        try:
            res = generate_outreach(entry["row"], entry["pkg_dir"])
            total += res["cost"]
            drafted.append(f"{company} ({role})")
            print(f"    wrote {res['path']} (${res['cost']:.4f})")
        except Exception as e:
            print(f"    ERROR: {e}")
    print()
    print(f"summary: {len(newly)} outreach packages drafted, ${total:.4f} spent")
    try:
        from alice.persistence import activity_log
        activity_log.record(
            step="draft_outreach",
            summary=f"{len(drafted)} outreach package{'' if len(drafted) == 1 else 's'} drafted ({', '.join(drafted[:3])}{'...' if len(drafted) > 3 else ''})",
            count=len(drafted), cost=total,
            details={"drafted": drafted},
        )
    except Exception as e:
        print(f"[activity_log: {e}]")


if __name__ == "__main__":
    main()
