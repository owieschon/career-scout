"""Refetch each sheet row's JD and write a narrative fit-hypothesis to the notes column.

Maps specific JD phrases (regex hits) → specific Jordan-background evidence.
Designed to produce dense, role-specific reasoning — not generic praise.

Background dimensions queried:
  industrial    — Lattice Additive / Ironclad Industrial credibility
  enterprise    — major aerospace OEM / defense-prime named accounts
  ml_builder    — Cadence Analytics production ML + full-stack
  retention     — Cadence Analytics is a B2B retention/expansion platform
  hardware      — Lattice Additive hardware ops
  cad_plm       — engineering-software CAD/PLM experience
  additive      — Lattice Additive metal AM specifically
  ai_native     — Cadence Analytics builder track for AI-native FDE/SA roles
  educator      — training/enablement instincts
  operator      — operator-builder narrative for early-stage roles
  east_central  — Eastern/Central timezone preference
"""
import re, sys, urllib.parse
from datetime import datetime

from pathlib import Path
from alice.pipeline.source_deep import get, strip
from alice.persistence import ledger

# JD signal patterns (background tag, hypothesis sentence)
SIGNALS = [
 # --- industrial / manufacturing core ---
    (re.compile(r"industrial\s+ai|manufacturing|manufacturers?|production line|factory|plant floor|shop floor|machinery|industrial iot|industrial[- ]grade", re.I),
     "industrial",
     "Industrial/manufacturing language in JD lines up with Lattice Additive (metal additive for aerospace OEMs), Ironclad Industrial (heavy-duty production parts), and engineering-software (CAD/PLM sold to engineering/manufacturing buyers) — you speak this buyer's language without translation."),

    (re.compile(r"downtime|yield|throughput|oee|asset (?:cost|utilization)|predictive maintenance|machine health|reliability", re.I),
     "industrial",
     "ROI vocabulary (downtime/yield/asset cost/maintenance) is the exact math you sold to major aerospace OEMs and defense primes at Lattice Additive — these aren't unfamiliar concepts you'd need to grow into."),

 # --- AI-native / ML / Cadence Analytics builder credibility ---
    (re.compile(r"(?:agentic|generative|applied)\s+ai|foundation model|llm|machine learning|production ml|ml platform|data platform|inference|embeddings|rag", re.I),
     "ml_builder",
     "AI/ML stack matches your Cadence Analytics builder track — you ship production ML (training pipelines, evaluation, serving) for a real B2B revenue-intelligence product. You're a peer on this terminology, not a learner."),

    (re.compile(r"forward[- ]deployed|solutions (?:engineer|architect)|applied (?:ai|ml) engineer|deployed engineer", re.I),
     "ai_native",
     "Forward-deployed/SA shape is Track 4 (your highest-upside track per project rubric). Cadence Analytics — built solo from spec to production — is the credibility artifact most candidates lack here."),

 # --- retention / expansion / revenue intelligence ---
    (re.compile(r"retention|renewal|expansion|upsell|cross[- ]sell|net retention|nrr|gross retention|churn|at[- ]risk", re.I),
     "retention",
     "Retention/expansion is Cadence Analytics's core product thesis. You built the system that surfaces at-risk accounts and expansion candidates from transaction + communication + human signal — you've thought harder about this problem than most CSMs because you had to design the model."),

    (re.compile(r"customer health|health score|usage data|adoption metric|product analytics|customer outcomes", re.I),
     "retention",
     "'Customer health → commercial outcome' mapping is the literal Cadence Analytics architecture. You don't approach this as folk wisdom; you've operationalized it in code."),

    (re.compile(r"qbr|executive (?:business )?review|executive sponsor|business review", re.I),
     "enterprise",
     "QBR/executive-review motion matches the cadence you ran at Lattice Additive into aerospace OEMs and defense primes (engineering leadership). Comfortable in CTO/VP-Eng rooms, not green at exec exposure."),

 # --- hardware specifically ---
    (re.compile(r"hardware|device|firmware|sensor|edge device|embedded|iot device", re.I),
     "hardware",
     "Hardware fluency comes from production-hardware fleet ops (global install base) and Lattice Additive (metal AM platform). Most SaaS-only candidates can't ground 'hardware customer success' in real operational experience."),

    (re.compile(r"cad|plm|product lifecycle|engineering software|simulation|cae|cam|3d printing|additive manufactur", re.I),
     "cad_plm",
     "CAD/PLM/simulation/additive vocabulary directly overlaps engineering-software (CAD/PLM) and Lattice Additive (metal AM). The engineering-tools buyer profile is one you've quota'd against."),

 # --- enterprise sales motion ---
    (re.compile(r"enterprise (?:account|customer|client)|strategic account|named account|six[- ]figure|seven[- ]figure|complex sales|multi[- ]stakeholder", re.I),
     "enterprise",
     "Enterprise-account motion matches your Lattice Additive book (aerospace OEMs are multi-stakeholder, multi-million-dollar, multi-quarter cycles). Not learning how to run enterprise — you've run it."),

    (re.compile(r"spacex|lockheed|relativity space|boeing|raytheon|northrop|aerospace prime", re.I),
     "enterprise",
     "Named-customer overlap with your direct Lattice Additive book — this is unique referential credibility very few CS/AM candidates can claim."),

 # --- operator / builder for early stage ---
    (re.compile(r"founding|first commercial hire|build (?:the |our )?(?:gtm|sales|cs)|0\s*[-→]\s*1|zero[- ]to[- ]one|series a|seed[- ]stage", re.I),
     "operator",
     "Founding/first-commercial-hire shape matches the operator-builder profile: you stood up Cadence Analytics, shipped the product, run the pilot — you've done 0→1 commercial work, not just executed inside someone else's machine."),

 # --- educator / enablement ---
    (re.compile(r"enablement|training|onboarding|customer education|certification|curriculum|technical writer|documentation", re.I),
     "educator",
     "Customer-enablement bias aligns with your years explaining technical products to engineering buyers (production hardware / Lattice Additive / engineering-software). Pedagogy isn't an aspiration; it's your default mode."),

 # --- revops / sales ops / quote-to-cash ---
    (re.compile(r"revops|revenue operations|sales operations|sales ops|forecast|pipeline (?:health|hygiene)|crm|salesforce|gainsight|hubspot|outreach|salesloft", re.I),
     "ml_builder",
     "RevOps tool fluency (Salesforce/Gainsight/HubSpot/forecast hygiene) is what Cadence Analytics is built ON TOP OF — you've not only used these systems, you've parsed their schemas and built a model that reasons over them."),

 # --- timezone preference ---
    (re.compile(r"east(?:ern)?\s*(?:us|coast|time)|eastern\s+time|et\b|edt|est\b|central\s+(?:us|time)|cst|cdt|cst\b", re.I),
     "east_central",
     "Eastern/Central timezone preference makes Columbus, OH a bullseye, not just acceptable — fewer scheduling friction points than for West-coast hires."),

 # --- pilot / design partner / early customer ---
    (re.compile(r"pilot|design partner|reference (?:customer|account)|case study|land[- ]and[- ]expand|champion[- ]building", re.I),
     "operator",
     "Pilot/design-partner/champion-building motion is what you're actively running with Cadence Analytics's first customer — the muscle is warm, not theoretical."),

 # --- mid-market / SMB tier ---
    (re.compile(r"mid[- ]?market|smb|small[- ]business|growth[- ]stage customer", re.I),
     "industrial",
     "Mid-market/SMB profile maps to a production-hardware buyer base (engineers/shop owners running their own P&L) — direct selling experience to that buyer, not just enterprise."),
]

OPERATOR_TRACKS = {
    "ae": "Track 1 (Senior AE — SaaS $50M-$500M ARR with manufacturing/industrial vertical)",
    "revops": "Track 2 (RevOps / Sales Ops / Revenue Architect)",
    "tam": "Track 3 (TAM / Senior CS / Implementation Engineer)",
    "fde": "Track 4 (Forward Deployed / Solutions / Applied AI — your highest-upside track)",
    "consulting": "Track 5 (bridge consulting)",
}


_GH_KNOWN_SLUGS = {  # company-host → greenhouse board slug
    "northwind.com": "northwind", "flowstate.co": "flowstate", "fleetline.com": "fleetline",
    "simscale.com": "simscale", "lakeforge.com": "lakeforge",
    "watershed.com": "watershed", "linear.app": "linear",
}


def fetch_jd(url):
    """Best-effort JD body fetch — Greenhouse (multi-URL-shape), Ashby (case-tolerant)."""
    if not url:
        return None
    host = urllib.parse.urlparse(url).netloc.lower()

 # 1) Greenhouse via gh_jid query param
    m = re.search(r"gh_jid=(\d+)", url)
    if m:
        jid = m.group(1)
        candidates = []
        if host in _GH_KNOWN_SLUGS: candidates.append(_GH_KNOWN_SLUGS[host])
        parts = host.replace("www.", "").split(".")
        candidates += [parts[0], parts[-2] if len(parts) > 1 else parts[0]]
        for slug in dict.fromkeys(candidates):
            try:
                j = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
                return strip(j.get("content", "") or "")
            except Exception:
                continue

 # 2) "Friendly" company careers URL with trailing job ID — northwind.com/careers/<slug>-<digits>/
    m = re.search(r"/careers/[^/]*?-(\d{6,})/?$", url)
    if m:
        jid = m.group(1)
        candidates = []
        if host in _GH_KNOWN_SLUGS: candidates.append(_GH_KNOWN_SLUGS[host])
        parts = host.replace("www.", "").split(".")
        candidates += [parts[0]]
        for slug in dict.fromkeys(candidates):
            try:
                j = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
                return strip(j.get("content", "") or "")
            except Exception:
                continue

 # 3) Greenhouse direct (boards.greenhouse.io / job-boards.greenhouse.io)
    m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        slug, jid = m.group(1), m.group(2)
        try:
            j = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
            return strip(j.get("content", "") or "")
        except Exception:
            return None

 # 4) Ashby (slug may be Mixed-Case in URL; API needs exact case — try as-given + lowered)
    m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([a-f0-9-]+)", url)
    if m:
        slug, uid = m.group(1), m.group(2)
        for s in (slug, slug.lower(), slug.capitalize()):
            try:
                board = get(f"https://api.ashbyhq.com/posting-api/job-board/{s}?includeCompensation=true")
                for j in board.get("jobs", []):
                    if uid in (j.get("jobUrl") or "") or uid == j.get("id"):
                        return strip(j.get("descriptionPlain") or j.get("descriptionHtml", "") or "")
            except Exception:
                continue
        return None

    return None


def mine(desc, role, company):
    if not desc:
        return None
    hits = []
    seen_tags = set()
    for pattern, tag, sentence in SIGNALS:
        if pattern.search(desc) and tag not in seen_tags:
            hits.append((tag, sentence))
            seen_tags.add(tag)
    return hits


def hypothesis(hits, company, role):
    if not hits:
        return ""
    lines = [f"FIT HYPOTHESIS — {company} · {role}"]
    for i, (tag, sentence) in enumerate(hits[:5], 1):  # cap at top 5 to keep dense
        lines.append(f"  {i}. {sentence}")
    return "\n".join(lines)


def main():
    ws = ledger._ws()
    rows = ws.get_all_records()
    print(f"sheet: {len(rows)} rows")

    updates = []  # (row_idx, hypothesis_text)
    failed = []   # (row_idx, company, role, url, reason)

    for idx, r in enumerate(rows, start=2):
        company = r.get("company", "")
        role = r.get("role", "")
        url = r.get("url", "")
        if not url:
            failed.append((idx, company, role, url, "no url"))
            continue
        try:
            desc = fetch_jd(url)
        except Exception as e:
            desc = None
        if not desc:
            failed.append((idx, company, role, url, "refetch failed"))
            continue
        hits = mine(desc, role, company)
        if not hits:
            failed.append((idx, company, role, url, "no signal hits"))
            continue
        text = hypothesis(hits, company, role)
        updates.append((idx, text))
        print(f"  row {idx}: {company[:18]:18} {role[:32]:32} → {len(hits)} signals")

    print()
    print(f"hypothesis written for {len(updates)} rows")
    print(f"could not enrich: {len(failed)} rows")
    for f in failed:
        print(f"  row {f[0]}: {f[1][:18]:18} {f[2][:32]:32} ({f[4]})")

 # Write to column H (notes) — col index 8
    if updates:
        from gspread.utils import rowcol_to_a1
        cells = []
        for row_idx, text in updates:
            cells.append({"range": rowcol_to_a1(row_idx, 8), "values": [[text]]})
        ws.batch_update(cells, value_input_option="RAW")
        print(f"wrote {len(updates)} notes cells.")


if __name__ == "__main__":
    main()
