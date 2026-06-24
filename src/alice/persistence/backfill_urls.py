#!/usr/bin/env python3
"""
backfill_urls.py — resolve REAL posting URLs for the role_scan-shortlist rows I inserted
into the live ledger (they went in URL-less), and write them to column I.

NO FABRICATION: a URL is written ONLY if it is a real posting fetched from a public ATS
board (Greenhouse/Ashby/Lever) whose title matches the ledger role. Unresolved rows are
left blank and reported for WebSearch/manual follow-up.
"""
import re, json, requests
from alice.jobcfg import load
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = load().get("LEDGER_SHEET_ID", "")
UA = {"User-Agent": "alice-url-backfill/1.0", "Accept": "application/json"}

# best-effort ATS org-slug candidates per company (tried in order, across all 3 ATSs)
SLUGS = {
    "Canals": ["canals"], "Cencora": ["cencora"], "Hexion": ["hexion"],
    "RELEX": ["relexsolutions", "relex"], "Zilliant": ["zilliant"],
    "Unanet": ["unanet"], "Deltek": ["deltek"], "STERIS": ["steris"],
    "iHerb": ["iherb"], "J.D. Power": ["jdpower", "jdpowercareers"],
    "Qualified": ["qualified", "qualifiedcom"], "Revenue.io": ["revenueio", "revenue"],
    "CSAA Insurance": ["csaa", "csaainsurance"], "Cresta": ["cresta"],
    "Wipfli": ["wipfli"], "KeyBank": ["keybank", "key"], "Seeq": ["seeq", "seeqcorporation"],
    "Cognigy (NICE)": ["cognigy"], "Replicant": ["replicant", "replicantai"],
    "Senseye (Siemens)": ["senseye", "siemens"], "Syncari": ["syncari"],
    "Modern Health": ["modernhealth"],
}


def norm(s): return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())


def tokens(s): return set(norm(s).split()) - {"the","a","of","and","for","to","-","ii","i"}


def title_match(role, posting_title):
    rt, pt = tokens(role), tokens(posting_title)
    if not rt: return False
    return len(rt & pt) / len(rt) >= 0.5


def try_greenhouse(slug):
    try:
        d = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", headers=UA, timeout=15)
        if d.status_code != 200: return []
        return [(j.get("title",""), j.get("absolute_url","")) for j in d.json().get("jobs",[])]
    except Exception: return []


def try_ashby(slug):
    try:
        d = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=15)
        if d.status_code != 200: return []
        return [(j.get("title",""), j.get("jobUrl") or j.get("applyUrl","")) for j in d.json().get("jobs",[])]
    except Exception: return []


def try_lever(slug):
    try:
        d = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=UA, timeout=15)
        if d.status_code != 200: return []
        return [(j.get("text",""), j.get("hostedUrl","")) for j in d.json()]
    except Exception: return []


def resolve(company, role):
    for slug in SLUGS.get(company, [norm(company).replace(" ","")]):
        for fetch in (try_greenhouse, try_ashby, try_lever):
            postings = fetch(slug)
            for title, url in postings:
                if url and title_match(role, title):
                    return url, f"{fetch.__name__}:{slug}"
    return None, None


def main():
    creds = Credentials.from_service_account_file(load()["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    ws = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    rows = ws.get_all_records()
    updates, resolved, unresolved = [], [], []
    for i, r in enumerate(rows):
        if "role_scan shortlist" not in str(r.get("source","")) or str(r.get("url","")).strip():
            continue
        comp, role = r.get("company",""), r.get("role","")
        url, via = resolve(comp, role)
        if url:
            updates.append({"range": f"I{i+2}", "values": [[url]]})  # col I = url; +2 for header+0-index
            resolved.append((comp, role, via, url))
        else:
            unresolved.append((comp, role))
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    print(f"RESOLVED + WRITTEN: {len(resolved)}")
    for c, ro, via, u in resolved: print(f"  [{via}] {c} — {ro[:34]} -> {u[:70]}")
    print(f"\nUNRESOLVED ({len(unresolved)}) — left blank, need WebSearch/manual:")
    for c, ro in unresolved: print(f"  {c} — {ro[:44]}")


if __name__ == "__main__":
    main()
