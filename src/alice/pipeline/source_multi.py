"""
Multi-ATS sourcing extension. Pulls live postings from Ashby, Greenhouse, and
Lever public job-board APIs for a curated registry of target companies, applies
freshness + remote + role filters, and scores
each via the existing score_job.py scorecard.

It is
read-only: it prints results as JSON and does NOT write to the pipeline DB.
Aggregation/ingest decisions happen upstream after human-in-the-loop review.

Usage:
    python3 scripts/source_multi.py --since 14
    python3 scripts/source_multi.py --since 14 --registry extra_slugs.json

Hard rules (same as the brief):
    - Never fabricate. A wrong slug returns empty/error → recorded as such, not invented.
    - Read-only. No DB writes, no applications.
"""
import argparse
import html
import json
import re
import ssl
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

from alice.pipeline.score_job import score_listing
from alice.pipeline import ats_client  # shared Greenhouse/Ashby/Lever fetch layer

USER_AGENT = "job-search-sourcer/1.1 (+personal use)"

# Broader role keyword set for this task (RevOps/AE/CS/SE/Founding GTM/PM-at-hardware).
ROLE_KEYWORDS = [
    "account executive", "senior account executive", "enterprise account executive",
    "strategic account executive", "founding account executive", "founding ae",
    "head of sales", "account manager", "enterprise account manager",
    "revenue operations", "revops", "sales operations", "sales ops",
    "revenue architect", "gtm engineer", "gtm ops", "gtm operations",
    "commercial operations", "business operations", "head of revenue",
    "growth engineer", "forward deployed", "solutions engineer",
    "solutions consultant", "solutions architect", "applied ai",
    "technical account manager", "implementation engineer", "sales engineer",
    "technical sales", "customer success", "client success", "founding gtm",
    "go-to-market", "product manager",
]

# Titles to hard-skip even if a keyword matched (junior / off-track).
NEGATIVE_TITLE = [
    "sdr", "bdr", "sales development", "business development representative",
    "intern", "marketing operations", "people operations", "recruiting",
]

REMOTE_PATTERNS = [
    "remote", "anywhere", "distributed", "us remote", "remote (us", "remote-first",
]
US_LOCATION_PATTERNS = [
    "united states", "usa", "u.s.", " us ", "(us)", "us-based", "us only",
    "remote (us", "us remote", "remote us", "new york", "san francisco",
    "los angeles", "chicago", "boston", "seattle", "austin", "denver", "portland",
    "miami", "atlanta", "dallas", "houston", "nyc", "ohio", "columbus",
    "california", "texas", "colorado", "washington", "florida", "illinois",
    "massachusetts", "georgia", "north america",
]
NON_US_ONLY_PATTERNS = [
    "emea", "apac", "latam", "london", "paris", "munich", "berlin", "dublin",
    "amsterdam", "madrid", "barcelona", "lisbon", "warsaw", "prague", "stockholm",
    "zurich", "tokyo", "singapore", "sydney", "toronto", "vancouver",
    "mexico city", "são paulo", "bangalore", "mumbai", "delhi", "tel aviv",
    "remote (emea", "remote (uk", "remote (eu", "remote (apac", "remote - emea",
]

# Registry: (display_name, ats, slug). Wrong slugs simply yield nothing.
# Seeded with high-confidence guesses; the API is the source of truth.
REGISTRY = [
 # --- AI-native (mostly Ashby) ---
    ("Quill Code", "ashby", "quill"),
    ("Cascade AI", "ashby", "cascade"),
    ("Octave AI", "ashby", "octave"),
    ("Lexicon AI", "ashby", "lexicon"),
    ("Perplexity", "ashby", "perplexity"),
    ("Claro Health", "ashby", "clarohealth"),
    ("Vellum", "ashby", "vellum"),
    ("Baseten", "ashby", "baseten"),
    ("Modal", "ashby", "modal"),
    ("Hex", "ashby", "hex"),
    ("Clay", "ashby", "clay"),
    ("Watershed", "ashby", "watershed"),
    ("Cresta", "ashby", "cresta"),
    ("Writer", "ashby", "writer"),
    ("Lumen Search", "greenhouse", "lumen"),
    ("Anthropic", "greenhouse", "anthropic"),
    ("Hebbia", "greenhouse", "hebbia"),
    ("Together AI", "ashby", "together"),
    ("Attio", "ashby", "attio"),
    ("Replit", "greenhouse", "replit"),
 # --- Industrial / manufacturing AI ---
    ("Flowstate", "greenhouse", "flowstate"),
    ("MachineMetrics", "greenhouse", "machinemetrics"),
    ("Northwind Systems", "greenhouse", "northwind"),
    ("Bright Machines", "greenhouse", "brightmachines"),
    ("Sight Machine", "greenhouse", "sightmachine"),
    ("Halcyon Manufacturing", "lever", "halcyon"),
    ("Trailhead Robotics", "greenhouse", "trailheadrobotics"),
    ("Standard Bots", "greenhouse", "standardbots"),
    ("Cobalt Automation", "ashby", "cobalt"),
    ("Uptake", "greenhouse", "uptake"),
 # --- Aerospace / defense / additive / CAD / marketplaces ---
    ("Anduril", "greenhouse", "andurilindustries"),
    ("Shield AI", "greenhouse", "shieldai"),
    ("Lumen Search", "greenhouse", "lumenwork"),
    ("Hadrian", "ashby", "hadrian"),
    ("Saronic", "ashby", "saronic"),
    ("Markforged", "greenhouse", "markforged"),
    ("Carbon", "greenhouse", "carbon"),
    ("Vertex Manufacturing", "greenhouse", "vertexmfg"),
    ("Fictiv", "greenhouse", "fictiv"),
    ("Shapr3D", "lever", "shapr3d"),
    ("Forge Parts", "ashby", "forgeparts"),
    ("nTopology", "greenhouse", "ntopology"),
]


def _http_get(url, timeout=20, as_json=True):
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if as_json else raw


def _strip_html(t):
    if not t:
        return ""
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _title_ok(title):
    t = (title or "").lower()
    if any(n in t for n in NEGATIVE_TITLE):
        return False
    return any(k in t for k in ROLE_KEYWORDS)


def _loc_ok(loc_text):
    s = (loc_text or "").lower()
    if not s:
        return True  # unknown — let scorecard/remote flag decide
    is_remote = any(p in s for p in REMOTE_PATTERNS)
    us_signal = any(p in s for p in US_LOCATION_PATTERNS)
    non_us_only = any(p in s for p in NON_US_ONLY_PATTERNS)
    if us_signal:
        return True
    if non_us_only and not us_signal:
        return False
    return is_remote or True  # ambiguous → keep, scorecard handles


# --- Ashby ---
def pull_ashby(slug, cutoff):
    out = []
    for j in ats_client.fetch_ashby(slug, get=_http_get):
        if not j.get("isListed", True):
            continue
        if not _title_ok(j.get("title", "")):
            continue
        locs = [j.get("location") or ""] + [(sl.get("location") or "") for sl in (j.get("secondaryLocations") or [])]
        loc_text = " / ".join([l for l in locs if l])
        remote = bool(j.get("isRemote")) or ((j.get("workplaceType") or "").lower() == "remote")
        if not _loc_ok(loc_text) and not remote:
            continue
        pub = j.get("publishedAt")
        pub_dt = None
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                pass
        if pub_dt and pub_dt < cutoff:
            continue
        comp = j.get("compensation") or {}
        base_low = base_high = None
        for c in ((comp.get("summaryComponents") or []) if isinstance(comp, dict) else []):
            if (c.get("compensationType") or "").lower() == "salary" or (c.get("compensationTierSummary") or "").lower().startswith("base"):
                if c.get("minValue") is not None:
                    base_low = int(c["minValue"])
                if c.get("maxValue") is not None:
                    base_high = int(c["maxValue"])
                break
        out.append({
            "role_title": j.get("title", ""),
            "posting_url": j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}",
            "location": loc_text, "remote_policy": "remote" if remote or "remote" in loc_text.lower() else "",
            "base_salary_low": base_low, "base_salary_high": base_high,
            "description": j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            "published_at": pub, "_date_iso": pub_dt.date().isoformat() if pub_dt else None,
        })
    return out


# --- Greenhouse ---
def pull_greenhouse(slug, cutoff):
    out = []
    for j in ats_client.fetch_greenhouse(slug, get=_http_get):
        if not _title_ok(j.get("title", "")):
            continue
        loc_text = (j.get("location") or {}).get("name", "") if isinstance(j.get("location"), dict) else ""
        if not _loc_ok(loc_text):
            continue
        upd = j.get("updated_at") or j.get("first_published")
        upd_dt = None
        if upd:
            try:
                upd_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            except ValueError:
                pass
        if upd_dt and upd_dt < cutoff:
            continue
        desc = _strip_html(j.get("content", ""))
        out.append({
            "role_title": j.get("title", ""),
            "posting_url": j.get("absolute_url", ""),
            "location": loc_text, "remote_policy": "remote" if "remote" in loc_text.lower() else "",
            "base_salary_low": None, "base_salary_high": None,
            "description": desc, "published_at": upd,
            "_date_iso": upd_dt.date().isoformat() if upd_dt else None,
            "_date_is_updated": True,
        })
    return out


# --- Lever ---
def pull_lever(slug, cutoff):
    out = []
    for j in ats_client.fetch_lever(slug, get=_http_get):
        title = j.get("text", "")
        if not _title_ok(title):
            continue
        cats = j.get("categories") or {}
        loc_text = cats.get("location", "") or ""
        wtype = (j.get("workplaceType") or "").lower()
        if not _loc_ok(loc_text) and wtype != "remote":
            continue
        created = j.get("createdAt")
        c_dt = None
        if created:
            try:
                c_dt = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass
        if c_dt and c_dt < cutoff:
            continue
        out.append({
            "role_title": title,
            "posting_url": j.get("hostedUrl", ""),
            "location": loc_text, "remote_policy": "remote" if wtype == "remote" or "remote" in loc_text.lower() else "",
            "base_salary_low": None, "base_salary_high": None,
            "description": j.get("descriptionPlain") or _strip_html(j.get("description", "")),
            "published_at": created, "_date_iso": c_dt.date().isoformat() if c_dt else None,
        })
    return out


PULLERS = {"ashby": pull_ashby, "greenhouse": pull_greenhouse, "lever": pull_lever}


def run(since_days=14, registry=None):
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    registry = registry or REGISTRY
    results = []
    resolved, empty, errored = [], [], []
    for display, ats, slug in registry:
        puller = PULLERS.get(ats)
        if not puller:
            errored.append((display, ats, slug, "unknown ats"))
            continue
        try:
            rows = puller(slug, cutoff)
        except (URLError, HTTPError) as e:
            errored.append((display, ats, slug, f"{e}"))
            continue
        except Exception as e:  # noqa
            errored.append((display, ats, slug, f"parse: {e}"))
            continue
        if not rows:
            empty.append((display, ats, slug))
            continue
        resolved.append((display, ats, slug, len(rows)))
        for r in rows:
            listing = dict(r)
            listing["company"] = display
            listing["source"] = f"{ats}:{slug}"
            scored = score_listing(listing)
            results.append({
                "company": display, "role": listing["role_title"],
                "source": listing["source"], "url": listing["posting_url"],
                "date": listing.get("_date_iso"), "date_is_updated": listing.get("_date_is_updated", False),
                "location": listing["location"], "remote": listing["remote_policy"],
                "base_low": listing["base_salary_low"], "base_high": listing["base_salary_high"],
                "score": scored["score"], "tier": scored["tier"], "track": scored["track"],
                "archetype": scored["archetype"], "travel_intensity": scored["travel_intensity"],
                "kills": scored["kills"], "reasoning": scored["reasoning"],
            })
        time.sleep(0.3)

    results.sort(key=lambda x: x["score"], reverse=True)
    print(json.dumps({
        "cutoff": cutoff.date().isoformat(),
        "resolved": resolved, "empty": empty, "errored": errored,
        "results": results,
    }, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=14)
    ap.add_argument("--registry", help="optional JSON file: [[display,ats,slug],...]")
    args = ap.parse_args()
    reg = None
    if args.registry:
        reg = [tuple(x) for x in json.loads(Path(args.registry).read_text())]
    run(since_days=args.since, registry=reg)


if __name__ == "__main__":
    main()
