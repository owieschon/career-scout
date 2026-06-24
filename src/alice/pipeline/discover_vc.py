"""
VC-portfolio board discovery (periodic). Same pattern as discover_yc.py but fed
by a curated seed of on-domain portfolio companies (targets/vc_seed.json) rather
than the YC directory — because VC job boards (Consider/Getro) are JS-rendered
and not cleanly scrapable. Resolves each seed company to its public ATS board
(Greenhouse/Lever/Ashby) and writes targets/vc_boards.json, which daily_delta.py
folds into its full-fidelity ATS pull.

Seed format (targets/vc_seed.json): list of objects
    {"name": "Company", "fund": "BCV", "website": "https://company.com"}
(`website` optional but improves slug resolution.)

Run weekly-ish:  python3 scripts/discover_vc.py
"""
import json, re, time
from pathlib import Path
from alice.pipeline.discover_yc import _try_board  # reuse the ATS probe (greenhouse/ashby/lever)
from alice import repo_paths

REPO = repo_paths.ROOT
SEED = REPO / "targets" / "vc_seed.json"
OUT = REPO / "targets" / "vc_boards.json"


def _candidates(entry):
    """Likely ATS slugs from website domain-root + name."""
    cands = []
    site = entry.get("website") or ""
    m = re.search(r"https?://(?:www\.)?([^./]+)\.", site)
    if m:
        cands.append(m.group(1).lower())
    name = (entry.get("name") or "").lower()
    cands.append(re.sub(r"[^a-z0-9]", "", name))
    cands.append(re.sub(r"[^a-z0-9]", "", name) + "inc")
    seen, out = set(), []
    for x in cands:
        if x and x not in seen and len(x) > 1:
            seen.add(x); out.append(x)
    return out


def run():
    if not SEED.exists():
        print(f"No seed at {SEED}; nothing to resolve."); return []
    seed = json.loads(SEED.read_text())
    print(f"VC seed: {len(seed)} on-domain portfolio companies to resolve.")
    resolved, attempts = [], 0
    for entry in seed:
        hit = None
        for slug in _candidates(entry)[:3]:
            attempts += 1
            hit = _try_board(slug)
            if hit:
                break
            time.sleep(0.05)
        if hit:
            ats, slug, n = hit
            resolved.append([entry.get("name"), ats, slug, entry.get("fund", ""), n])
            print(f"  ✓ {entry.get('name','')[:26]:26} {entry.get('fund',''):8} -> {ats}:{slug} ({n} jobs)")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(resolved, indent=1))
    print(f"\nResolved {len(resolved)}/{len(seed)} to ATS boards in {attempts} probes. Wrote {OUT}")
    return resolved


if __name__ == "__main__":
    run()
