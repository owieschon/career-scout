"""
YC board discovery (periodic, not daily). The Work-at-a-Startup jobs board is
auth-gated, but the YC OSS company directory is fully public. This pulls it,
filters to the target on-domain, currently-HIRING companies, resolves each to a
public ATS board (Greenhouse / Lever / Ashby), and writes the resolved boards to
targets/yc_boards.json — which daily_delta.py then folds into its ATS pull so YC
roles get full-fidelity treatment (comp + hidden-travel screen).

Run weekly-ish:  python3 scripts/discover_yc.py [--max 200] [--recent-only]
"""
import argparse, json, re, ssl, time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from alice import repo_paths
try:
    import certifi; _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

REPO = repo_paths.ROOT
OUT = REPO / "targets" / "yc_boards.json"
UA = "job-search-sourcer/2.1 (+personal use)"
YC_DIR = "https://yc-oss.github.io/api/companies/all.json"

TIGHT_INDUSTRIES = ["manufacturing and robotics", "industrials"]
HW_TAGS = ["hardware", "3d printing", "additive", "supply chain", "aerospace",
           "robotics", "manufacturing", "cnc", "iot", "climate", "energy",
           "materials", "engineering", "industrial", "logistics", "construction"]


def _get_json(url, timeout=12):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout, context=_SSL) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _on_domain(c):
    inds = [str(x).lower() for x in (c.get("industries") or [])]
    blob = (str(c.get("subindustry", "")) + " " + " ".join(c.get("tags", []) or [])
            + " " + (c.get("one_liner") or "")).lower()
    return any(t in inds for t in TIGHT_INDUSTRIES) or any(t in blob for t in HW_TAGS)


def _candidates(c):
    """Likely ATS slugs: website domain-root + YC slug + name-normalized."""
    cands = []
    site = c.get("website") or ""
    m = re.search(r"https?://(?:www\.)?([^./]+)\.", site)
    if m:
        cands.append(m.group(1).lower())
    if c.get("slug"):
        cands.append(re.sub(r"[^a-z0-9]", "", c["slug"].lower()))
    cands.append(re.sub(r"[^a-z0-9]", "", (c.get("name") or "").lower()))
    seen, out = set(), []
    for x in cands:
        if x and x not in seen and len(x) > 1:
            seen.add(x); out.append(x)
    return out


def _try_board(slug):
    """Return (ats, slug, n_jobs) for the first ATS where the slug resolves with jobs."""
    probes = [
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
        ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"),
        ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
    ]
    for ats, url in probes:
        try:
            data = _get_json(url, timeout=8)
        except Exception:  # timeouts, SSL, 404, JSON — skip this probe
            continue
        n = len(data) if ats == "lever" and isinstance(data, list) else len((data or {}).get("jobs", []))
        if n > 0:
            return ats, slug, n
    return None


def run(max_companies=180, recent_only=True):
    d = _get_json(YC_DIR, timeout=30)
    sel = [c for c in d if c.get("isHiring") and c.get("status") != "Inactive" and _on_domain(c)]
    if recent_only:
        sel = [c for c in sel if any(b in (c.get("batch") or "") for b in ["2023", "2024", "2025", "2026"])]
 # newest batches first
    sel.sort(key=lambda c: c.get("launched_at", 0), reverse=True)
    sel = sel[:max_companies]
    print(f"YC OSS: {len(d)} companies; {len(sel)} on-domain hiring (recent={recent_only}) to resolve.")

    resolved, attempts = [], 0
    for c in sel:
        hit = None
        for slug in _candidates(c)[:2]:  # cap candidates per company
            attempts += 1
            hit = _try_board(slug)
            if hit:
                break
            time.sleep(0.05)
        if hit:
            ats, slug, n = hit
            resolved.append([c.get("name"), ats, slug, c.get("batch"), n])
            print(f"  ✓ {c.get('name')[:26]:26} {c.get('batch'):14} -> {ats}:{slug} ({n} jobs)")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(resolved, indent=1))
    print(f"\nResolved {len(resolved)}/{len(sel)} to ATS boards in {attempts} probes. Wrote {OUT}")
    return resolved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=180)
    ap.add_argument("--recent-only", action="store_true", default=True)
    ap.add_argument("--all-batches", dest="recent_only", action="store_false")
    a = ap.parse_args()
    run(max_companies=a.max, recent_only=a.recent_only)


if __name__ == "__main__":
    main()
