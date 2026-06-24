"""
Daily delta job sourcer. Finds GENUINELY NEW postings since the last run —
keyed on stable job IDs (not ATS `updated_at`, which board re-indexes corrupt),
across a BROAD universe (not just a curated board list):

  Discovery (broad):   aggregator APIs (Remotive, RemoteOK, Jobicy) + HN "Who is
                       hiring" — index thousands of companies we'd never register
                       by hand. Domain-keyword-gated to the target verticals.
  Verification (deep): curated ATS registry (Greenhouse/Lever/Ashby) — full JDs
                       so comp + the hidden-travel screen are reliable.

Every candidate flows through the encoded calibration (`score_job.py`: $100-250K
band + seniority ceiling) plus a hidden-travel screen (events/on-site/representation)
and a domain gate (advanced mfg / hardware / industrial / software / AI — NOT
cyber/observability/fintech/HR).

State: `seen_jobs` table in pipeline.db. First run seeds it (and shows roles
posted in the last 3 days so there's signal today); later runs emit only new IDs.

Output: output/daily-delta-YYYY-MM-DD.md  +  rows logged to sourcing_log.

Usage:
    python3 scripts/daily_delta.py            # normal daily run
    python3 scripts/daily_delta.py --days 3   # output window for new+dated roles
    python3 scripts/daily_delta.py --dry-run  # don't write DB or file

No fabrication: only real fetched postings are recorded.
"""
import argparse, html, json, os, re, ssl, sqlite3, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
try:
    import certifi; _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()

from alice import repo_paths
from alice.pipeline.score_job import score_listing
from alice.pipeline import fit_judge  # constraint-driven fit-judge over gate-survivors
from alice.pipeline import source_deep as SD  # the ATS board registry + helpers
from alice.pipeline import ats_client  # shared Greenhouse/Ashby/Lever fetch layer

REPO = repo_paths.ROOT
DB = REPO / "pipeline.db"
OUT = REPO / "output"
UA = "job-search-sourcer/2.0 (+personal use)"

# ---- filters (loaded from fit_model.toml [sourcing]) -------
def _sourcing_keywords():
    """The user's target-role + domain filters, loaded from fit_model.toml
    [sourcing] (a different user supplies their own). Fail loud if absent: these
    are user data, never engine defaults."""
    import tomllib
    with open(REPO / "config" / "fit_model.toml", "rb") as f:
        sc = tomllib.load(f).get("sourcing", {})
    for k in ("role_kw", "role_neg", "domain_kw", "domain_neg"):
        if not sc.get(k):
            raise ValueError(f"daily_delta: [sourcing].{k} required.")
    return sc

_SRC = _sourcing_keywords()
ROLE_KW = _SRC["role_kw"]
ROLE_NEG = _SRC["role_neg"]
DOMAIN_KW = _SRC["domain_kw"]
DOMAIN_NEG = _SRC["domain_neg"]
REMOTE_KW = ["remote", "anywhere", "distributed", "us-remote", "remote-us"]
# Hub-bound markers: either an explicit city-suffix tag (" - SF", "(NYC)") OR a
# bare hub-city in the location string. Either way reject unless "Remote" is
# also explicitly in title/location. This catches Ashby `isRemote=true` roles
# whose location is "San Francisco" / "New York" without "Remote" (hub-bound
# regardless of the remote flag — OpenAI's hub policy is the recurring case).
HUB_CITY_RE = re.compile(
    r"(?:\s-\s|\()(SF|NYC|SFO|San Francisco|New York|Boston|Seattle|DC|"
    r"Washington[ ,]?\s*DC|Los Angeles|LA|Chicago|Austin)\b\)?"
    r"|\b(San Francisco|New York(?!\s+Times)|Boston|Seattle|Chicago|Austin|Los Angeles)\b",
    re.I)
# Territory/field AE archetypes (travel-prone even when JD silent)
TERRITORY_RE = re.compile(r"\b(regional|territory|field)\s+(account|sales|ae)\b", re.I)
# Word-boundary regexes so "india" doesn't match "indiana", etc.
US_RE = re.compile(r"\b(united states|usa|u\.s\.a?\.?|us|americas|north america|us-remote)\b", re.I)
US_STATES_RE = re.compile(r"\b(" + "|".join(SD.USST) + r")\b", re.I)
NONUS_RE = re.compile(r"\b(apj|apac|emea|latam|europe|united kingdom|u\.k\.|ireland|germany|"
                      r"france|spain|portugal|poland|netherlands|sweden|india|canada|australia|"
                      r"singapore|japan|korea|brazil|mexico|dubai|uae|manila|philippines|"
                      r"bengaluru|bangalore|berlin|munich|london|paris|toronto|amsterdam|"
                      r"tel aviv|israel|hungary|budapest|"
                      r"italy|italia|milan|rome|indonesia|jakarta|athens|greece|prague|"
                      r"switzerland|zurich|geneva|vienna|austria|denmark|copenhagen|"
                      r"oslo|norway|finland|helsinki|romania|bucharest|hong kong|taiwan|"
                      r"vietnam|hanoi|malaysia|kuala lumpur|thailand|bangkok|south africa|"
                      r"chile|colombia|argentina|peru)\b", re.I)
HIDDEN = SD.HIDDEN_TRAVEL
# TRAVEL_RAW matches travel-presence patterns; _travel_match() adds negation-awareness.
# Exposed as module-level so recall_benchmark and tests can reference the raw pattern.
TRAVEL_RAW = re.compile(
    r"travel\s*(up to|of|approximately|~)?\s*\d{1,2}\s*%"
    r"|\d{1,2}\s*%\s*travel"
    r"|travel\s+(extensively|frequently|required)",
    re.I,
)
# Negation tokens: if any appear in the 5-word window BEFORE a TRAVEL_RAW match,
# the match is treated as negated (e.g. "No travel required", "no overnight travel
# required", "without travel", "zero travel", "no travel").
_TRAVEL_NEG_TOKENS = frozenset(["no", "not", "zero", "without", "never", "n't"])


def _travel_match(text):
    """Return the first non-negated TRAVEL_RAW match object, or None.

    A match is considered negated when a negation token (_TRAVEL_NEG_TOKENS)
    appears in the 5-word window immediately preceding the match start — this
    handles both adjacent negation ('No travel required') and near-adjacent
    negation ('no overnight travel required') without over-reaching.

    DOES NOT negate matches where the negation token appears AFTER the travel
    keyword (e.g. 'travel is not required' — the old regex never matched that
    form either, so behaviour is preserved).
    """
    for m in TRAVEL_RAW.finditer(text):
        before = text[: m.start()].lower()
        words = re.findall(r"\b\w+\b", before)
        window = words[-5:] if len(words) >= 5 else words
        if any(neg in window for neg in _TRAVEL_NEG_TOKENS):
            continue  # negated context — skip this match
        return m
    return None


# Keep TRAVEL as a compat alias for any external code that imports the bare
# compiled pattern (e.g. source_deep). _travel_flags() now routes through
# _travel_match() instead of TRAVEL.search() directly.
TRAVEL = TRAVEL_RAW


_SRC_ERR = []  # diagnostic: records fetch failures so an empty run explains itself


def _get(url, as_json=True, timeout=25):
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if as_json else raw
    except Exception as e:
        host = url.split("/")[2] if "//" in url else url[:30]
        _SRC_ERR.append(f"{host}: {type(e).__name__}: {str(e)[:80]}")
        raise


def _strip(t):
    if not t:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def _role_ok(title):
    t = (title or "").lower()
    if any(n in t for n in ROLE_NEG):
        return False
    return any(k in t for k in ROLE_KW)


def _remote_us_ok(geo, remote_flag=False, body=None):
    """Return True if the role is remote-US-eligible.

    geo = title + location string.  body = full JD text (optional).

    PRIMARY CHECK (title/location only):
      Remote signal must be in title/location (not desc boilerplate); reject
      explicit hybrid/onsite, hub-city titles, and non-US regions.

    BODY EXTENSION:
      When title/location alone are ambiguous (no remote signal in geo) the JD
      body is consulted as a secondary source, subject to two invariants:

        1. REJECTION WINS: if the body contains an onsite/hybrid signal the
           role is DROPPED regardless of any remote mention elsewhere.  A body
           saying "remote-first culture, but this role is onsite 3 days/week"
           must still fail.

        2. STRONG SIGNAL ONLY: only explicit positional remote declarations
           trigger a rescue ("this role is fully remote", "position is remote",
           "remote, US", etc.).  Benefits/culture boilerplate ("remote-first
           culture", "remote work stipend", "we embrace remote work") does NOT
           count — those appear on onsite roles routinely.

    The original title/location path is unchanged; the body path only fires
    when title/location don't already pass on their own.
    """
    g = geo or ""
    if re.search(r"hybrid|on-?site|in[- ]office", g, re.I):
        return False
 # Hub-city marker in title (e.g. "FDE - SF", "(NYC)") reject unless title also says "Remote"
    if HUB_CITY_RE.search(g) and not re.search(r"\bremote\b", g, re.I):
        return False
    has_remote = remote_flag or bool(re.search(r"\bremote\b|\banywhere\b|distributed", g, re.I))
    if not has_remote:
 # Title/location carry no remote signal. Consult body as secondary source.
        if body:
            b = body or ""
 # INVARIANT 1: onsite/hybrid in body DROPS the role unconditionally.
            if re.search(r"hybrid|on-?site|in[- ]office", b, re.I):
                return False
 # INVARIANT 2: relocation language in body DROPS the role.
            if re.search(r"\brelocate\b|\brelocation\b", b, re.I):
                return False
 # Strong body remote signal: positional declarations, not boilerplate.
 # Matches: "fully remote", "this role is remote", "position is remote",
 # "remote (US)", "100% remote", "remote-only", "work remotely from"
 # Does NOT match: "remote-first culture", "remote work stipend",
 # "supports remote work", "embrace remote" (benefits / cultural copy).
            body_remote_strong = bool(re.search(
                r"\bfully\s+remote\b"
                r"|\b100\s*%\s*remote\b"
                r"|\bremote[- ]only\b"
                r"|this\s+(?:role|position|job)\s+is\s+remote"
                r"|position\s+is\s+(?:fully\s+)?remote"
                r"|\bwork\s+(?:fully\s+)?remotely\s+from\b"
                r"|\bremote\s*[,(]\s*(?:US|United\s+States|USA)\b"
                r"|\bremote\s+across\s+the\s+(?:US|United\s+States)\b"
                r"|\bremote\s+within\s+the\s+(?:US|United\s+States)\b",
                b, re.I
            ))
            if not body_remote_strong:
                return False
 # Strong body remote signal present — also apply non-US and hub-city
 # rejection to the body (don't rescue a non-US remote role).
            if NONUS_RE.search(b):
                return False
 # Onsite rejection in body already checked above (INVARIANT 1).
            return True
        return False  # no body available; no rescue
 # Title/location already carry a remote signal — original logic.
    if NONUS_RE.search(g):
        return False         # explicit non-US region wins over ambiguous remote
    if US_RE.search(g) or US_STATES_RE.search(g):
        return True          # explicit US signal
    return True              # bare 'remote'/anywhere — ambiguous, keep


def _domain_blocked(text):
    """Hard exclude (applied to ALL roles, including curated ATS)."""
    s = (text or "").lower()
    return any(n in s for n in DOMAIN_NEG)


def _domain_positive(text):
    """Require a positive domain keyword (applied only to broad aggregator roles)."""
    s = (text or "").lower()
    return any(k in s for k in DOMAIN_KW)


def _travel_flags(desc):
    """Return (travel_flag, hidden_travel) strings.

    travel_flag: matched text from TRAVEL_RAW (negation-aware via _travel_match),
                 or '' if no non-negated match.
    hidden_travel: matched text from HIDDEN (SD.HIDDEN_TRAVEL), unchanged.
    """
    tm = _travel_match(desc or "")
    return (
        tm.group(0) if tm else "",
        HIDDEN.search(desc or "").group(0).strip() if HIDDEN.search(desc or "") else "",
    )


# Auto-grow: when an aggregator/HN posting links to a known ATS, harvest the slug
# so the NEXT run pulls that company full-fidelity (comp + travel screen).
ATS_URL_RE = re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)"
                        r"|jobs\.ashbyhq\.com/([a-z0-9_-]+)"
                        r"|jobs\.lever\.co/([a-z0-9_-]+)", re.I)


def _ats_from_url(url):
    m = ATS_URL_RE.search(url or "")
    if not m:
        return None
    if m.group(1):
        return ("greenhouse", m.group(1).lower())
    if m.group(2):
        return ("ashby", m.group(2).lower())
    if m.group(3):
        return ("lever", m.group(3).lower())
    return None


# ---- Target-file harvest --------------------------------------------------
# Scan targets/*.md (and subdirectories) for ATS board URLs and merge them
# into targets/discovered_slugs.json so _ats_boards() picks them up on the
# very SAME run that harvest runs. MERGE, do not clobber — the auto-grow
# write path (end of run()) also appends to that file; preserve those entries.

# Called at the start of run() before sources are built. Accepts a
# targets_dir argument so tests can pass a synthetic directory.

_HEADING_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)


def _harvest_targets(targets_dir=None, dry_run=False):
    """Scan targets_dir/*.md (recursive) for ATS board URLs.

    Each markdown file is searched line-by-line for an ATS URL matching
    ATS_URL_RE.  The company name comes from the first Markdown heading (# ...)
    in the file; falls back to the stem of the filename.

    Returns list of NEW [name, ats, slug] triples that were not already in
    discovered_slugs.json (or any other _ats_boards() source).  When
    dry_run=True the file is NOT written; the list is returned for inspection.
    """
    td = Path(targets_dir) if targets_dir else REPO / "targets"
    out_path = REPO / "targets" / "discovered_slugs.json"

 # Read existing discovered_slugs to deduplicate.
    existing: list = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except ValueError:
            existing = []
    have = {(r[1], r[2]) for r in existing if len(r) >= 3}

 # Also deduplicate against the static boards already in SD.BOARDS so we
 # don't shadow a curated entry with a lower-quality harvested one.
    have.update({(a, s) for _, a, s in SD.BOARDS})
 # And against yc/vc boards.
    for fname in ("yc_boards.json", "vc_boards.json"):
        p = REPO / "targets" / fname
        if p.exists():
            try:
                for row in json.loads(p.read_text()):
                    if len(row) >= 3:
                        have.add((row[1], row[2]))
            except (ValueError, IndexError):
                pass

    new_entries: list = []
    seen_this_scan: set = set()  # guard against two files for the same company

    for md_path in sorted(td.rglob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

 # Extract company name from first heading.
        m_heading = _HEADING_RE.search(text)
        if m_heading:
            raw = m_heading.group(1).strip()
 # Strip trailing role suffix after em-dash or long dash: "Acme — CPM"
            name = re.split(r"\s+[—–-]{1,2}\s+", raw)[0].strip()
        else:
            name = md_path.stem.replace("-", " ").replace("_", " ").title()

 # Scan every line for an ATS URL.
        for line in text.splitlines():
            hit = _ats_from_url(line)
            if hit and hit not in have and hit not in seen_this_scan:
                new_entries.append([name[:60], hit[0], hit[1]])
                have.add(hit)
                seen_this_scan.add(hit)
                break  # one board per file is sufficient

    if new_entries and not dry_run:
        merged = existing + new_entries
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(merged, indent=1))

    return new_entries


# ---- DB delta state -------------------------------------------------------
def db_init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_jobs (
        source TEXT NOT NULL, external_id TEXT NOT NULL, company TEXT, title TEXT,
        url TEXT, first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_seen TEXT,
        body TEXT, location TEXT, comp_low INTEGER, comp_high INTEGER,
        remote_flag INTEGER, skip_reason TEXT,
        PRIMARY KEY (source, external_id))""")
 # Idempotent in-place migration for an EXISTING pipeline.db created before the
 # body/structured-field columns or the skip_reason column were added. ALTER only
 # the missing columns. Existing rows get NULL skip_reason ("unknown") — a
 # per-cohort re-judge skips those.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen_jobs)").fetchall()}
    for col, decl in (("body", "TEXT"), ("location", "TEXT"),
                      ("comp_low", "INTEGER"), ("comp_high", "INTEGER"),
                      ("remote_flag", "INTEGER"), ("skip_reason", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {col} {decl}")
    conn.commit()


def is_first_run(conn):
    return conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0] == 0


def mark_seen(conn, source, ext_id, company, title, url,
              body=None, location=None, comp_low=None, comp_high=None,
              remote_flag=None):
    """Return True if this (source, id) is NEW (not seen before).

    On the new-insert path, persist the full JD body + structured fields
    (location / comp_low / comp_high / remote_flag) the pipeline already
    fetched — the gates run on the body and the fit-judge consumes it.
    New params default to None so existing callers stay compatible.
    """
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE source=? AND external_id=?",
                       (source, str(ext_id))).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if row:
        conn.execute("UPDATE seen_jobs SET last_seen=? WHERE source=? AND external_id=?",
                     (now, source, str(ext_id)))
        return False
    conn.execute(
        "INSERT INTO seen_jobs (source, external_id, company, title, url, last_seen, "
        "body, location, comp_low, comp_high, remote_flag) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source, str(ext_id), company, title, url, now,
         body, location, comp_low, comp_high,
         (None if remote_flag is None else int(bool(remote_flag)))))
    return True


def set_skip_seen(conn, source, ext_id, reason):
    """Record why a seen row was gate-dropped. The row already exists (mark_seen
    inserted it before the gates ran); passing rows keep skip_reason NULL.
    Diagnostic-only — enables a surgical per-cohort re-judge after a gate fix
    instead of a blind, flood-prone backlog re-judge."""
    conn.execute("UPDATE seen_jobs SET skip_reason=? WHERE source=? AND external_id=?",
                 (reason, source, str(ext_id)))


class JsonState:
    """File-backed seen-store for cloud/routine runs (no sqlite/pipeline.db).
    Same delta semantics as the DB: a (source, id) never seen before is NEW."""
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._was_empty = len(self.data) == 0

    def is_first(self):
        return self._was_empty

    def mark(self, source, ext_id, company, title, url,
             body=None, location=None, comp_low=None, comp_high=None,
             remote_flag=None):
        k = f"{source}|{ext_id}"
        if k in self.data:
            return False
 # Persist the full body + structured fields on first sighting (mirrors
 # mark_seen). New kwargs default to None for backward compatibility.
        self.data[k] = {"company": company, "title": title, "url": url,
                        "body": body, "location": location,
                        "comp_low": comp_low, "comp_high": comp_high,
                        "remote_flag": (None if remote_flag is None
                                        else int(bool(remote_flag)))}
        return True

    def set_skip(self, source, ext_id, reason):
        """Record gate drop-reason on an existing entry. No-op if the key is
        unseen (passing rows simply never get a skip_reason)."""
        k = f"{source}|{ext_id}"
        if k in self.data:
            self.data[k]["skip_reason"] = reason

    def commit(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0))


# ---- sources: deep ATS registry ------------------------------------------
def _ats_boards():
    """Curated registry + YC + auto-discovered on-domain boards."""
    boards = list(SD.BOARDS)
    seen = {(a, s) for _, a, s in boards}
    for fname, tag in [("yc_boards.json", "YC"), ("vc_boards.json", "VC"),
                       ("discovered_slugs.json", "auto")]:
        p = REPO / "targets" / fname
        if not p.exists():
            continue
        try:
            for row in json.loads(p.read_text()):
                name, ats, slug = row[0], row[1], row[2]
                if (ats, slug) not in seen:
                    boards.append((f"{name} ({tag})", ats, slug)); seen.add((ats, slug))
        except (ValueError, IndexError):
            continue
    return boards


def pull_ats():
    """Yield normalized postings from the curated ATS registry + YC boards (on-domain)."""
    for name, ats, slug in _ats_boards():
        try:
            if ats == "greenhouse":
                for j in ats_client.fetch_greenhouse(slug, get=_get):
                    yield {"source": f"gh:{slug}", "ext_id": j.get("id"),
                           "company": name, "title": j.get("title", ""),
                           "url": j.get("absolute_url", ""),
                           "location": (j.get("location") or {}).get("name", ""),
                           "desc": _strip(j.get("content", "")),
                           "base_low": None, "base_high": None, "domain_gate": False}
            elif ats == "ashby":
                for j in ats_client.fetch_ashby(slug, get=_get):
                    if not j.get("isListed", True):
                        continue
                    comp = j.get("compensation") or {}
                    bl = bh = None
                    for c in (comp.get("summaryComponents") or []):
                        if (c.get("compensationType") or "") == "Salary":
                            bl, bh = c.get("minValue"), c.get("maxValue"); break
                    locs = [j.get("location") or ""] + [(s.get("location") or "") for s in (j.get("secondaryLocations") or [])]
                    yield {"source": f"ashby:{slug}", "ext_id": j.get("id"),
                           "company": name, "title": j.get("title", ""),
                           "url": j.get("jobUrl", ""), "location": " / ".join(x for x in locs if x),
                           "remote_flag": bool(j.get("isRemote")),
                           "desc": _strip(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
                           "base_low": bl, "base_high": bh, "domain_gate": False}
            elif ats == "lever":
                for j in ats_client.fetch_lever(slug, get=_get):
                    yield {"source": f"lever:{slug}", "ext_id": j.get("id"),
                           "company": name, "title": j.get("text", ""),
                           "url": j.get("hostedUrl", ""),
                           "location": (j.get("categories") or {}).get("location", ""),
                           "desc": _strip(j.get("descriptionPlain") or j.get("description", "")),
                           "base_low": None, "base_high": None, "domain_gate": False}
        except (URLError, HTTPError, ValueError, OSError):
 # OSError covers TimeoutError / socket.timeout / ssl.SSLError — skip
 # the slow/broken board (it's recorded in _SRC_ERR so an empty run
 # still explains itself) instead of letting the exception escape
 # pull_ats and crash the whole sourcing run. A single board's
 # read-timeout must not take down the entire pass.
            continue
        time.sleep(0.15)


# ---- sources: broad aggregators (domain-gated) ----------------------------
def pull_remotive():
    try:
        data = _get("https://remotive.com/api/remote-jobs?limit=500")
    except Exception:
        return
    for j in data.get("jobs", []):
        yield {"source": "remotive", "ext_id": j.get("id"),
               "company": j.get("company_name", ""), "title": j.get("title", ""),
               "url": j.get("url", ""), "location": j.get("candidate_required_location", ""),
               "desc": _strip(j.get("description", "")), "date": j.get("publication_date", ""),
               "base_low": None, "base_high": None, "domain_gate": True}


def pull_remoteok():
    try:
        data = _get("https://remoteok.com/api")
    except Exception:
        return
    for j in data:
        if not isinstance(j, dict) or not j.get("id"):
            continue
        yield {"source": "remoteok", "ext_id": j.get("id"),
               "company": j.get("company", ""), "title": j.get("position", ""),
               "url": j.get("url", ""), "location": j.get("location", "") or "Remote",
               "desc": _strip(j.get("description", "")), "date": j.get("date", ""),
               "base_low": j.get("salary_min"), "base_high": j.get("salary_max"),
               "domain_gate": True}


def pull_jobicy():
    try:
        data = _get("https://jobicy.com/api/v2/remote-jobs?count=100&geo=usa")
    except Exception:
        return
    for j in data.get("jobs", []):
 # The full body is inline in the same response under `jobDescription`
 # (~8KB) vs the `jobExcerpt` (~200 chars); no secondary fetch needed.
 # Prefer the full body for `desc` so the hidden-travel gate and the
 # persisted body see the whole JD; fall back to the excerpt only if the
 # body is absent.
        yield {"source": "jobicy", "ext_id": j.get("id"),
               "company": j.get("companyName", ""), "title": j.get("jobTitle", ""),
               "url": j.get("url", ""), "location": j.get("jobGeo", ""),
               "desc": _strip(j.get("jobDescription", "") or j.get("jobExcerpt", "")),
               "date": j.get("pubDate", ""),
               "base_low": j.get("annualSalaryMin"), "base_high": j.get("annualSalaryMax"),
               "domain_gate": True}


def pull_himalayas():
    try:
        data = _get("https://himalayas.app/jobs/api?limit=200")
    except Exception:
        return
    for j in data.get("jobs", []):
        locs = j.get("locationRestrictions") or []
        loc = ", ".join(locs) if isinstance(locs, list) else str(locs or "Remote")
        yield {"source": "himalayas", "ext_id": j.get("guid") or j.get("applicationLink") or j.get("title"),
               "company": j.get("companyName", "") or j.get("company", ""),
               "title": j.get("title", ""),
               "url": j.get("applicationLink") or j.get("url", ""),
               "location": loc or "Remote",
 # Full body is inline under `description` (~3.5KB) vs `excerpt`
 # (~400 chars); no secondary fetch. Prefer the full body so the
 # travel gate + persisted body see it.
               "desc": _strip(j.get("description", "") or j.get("excerpt", "")),
               "date": (str(j.get("pubDate", "")) or "")[:10],
               "base_low": j.get("minSalary") or j.get("minSalaryUsd"),
               "base_high": j.get("maxSalary") or j.get("maxSalaryUsd"),
               "domain_gate": True}


def pull_hn():
    """Hacker News 'Who is hiring?' — current month's thread (founder-led / early-stage).
    Best-effort header parse: 'Company | Role | Location | tags'. Domain-gated."""
    try:
        s = _get("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=5")
        sid = None
        for h in s.get("hits", []):
            if "who is hiring" in (h.get("title") or "").lower() and h.get("author") == "whoishiring":
                sid = h["objectID"]; break
        if not sid:
            return
        c = _get(f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{sid}&hitsPerPage=400")
    except Exception:
        return
    for h in c.get("hits", []):
        txt = _strip(h.get("comment_text") or "")
        if not txt:
            continue
        header = txt[:300]
        parts = re.split(r"\s*[|•·–—]\s*", header, maxsplit=4)
        company = (parts[0].strip() if parts else "")[:80]
        role = parts[1].strip() if len(parts) > 1 else ""
        loc = parts[2].strip() if len(parts) > 2 else ""
        if not company or not role:
            continue
        yield {"source": "hn-whoishiring", "ext_id": h.get("objectID"),
               "company": company, "title": role,
               "url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
               "location": (loc + " | " + txt[:160]),  # body often carries REMOTE/US
               "desc": txt[:2000], "date": (h.get("created_at") or "")[:10],
               "base_low": None, "base_high": None, "domain_gate": True}


# ---- main -----------------------------------------------------------------
def _apply_ledger(new_qualified):
    """Trim + tune against the Google Sheet ledger:
    - Drop roles whose exact job_key has a terminal status (always).
    - Drop roles at companies labeled 'not a fit' 3+ times (company-wide suppression
      only after multiple negative labels — one label suppresses just that role).
    - Boost roles at companies with any 'good fit' label.
    Returns (list, ledger_total)."""
    try:
        from alice.persistence import ledger
        if not ledger.available():
            return new_qualified, None
        statuses, notfit_counts, goodfit_counts, total = ledger.load_statuses()
    except Exception as e:
        print(f"[ledger read skipped: {e}]")
        return new_qualified, None
    out = []
    for it in new_qualified:
        key = f"{it.get('source')}|{it.get('ext_id')}"
 # strip "(YC)/(VC)/(auto)" suffix so the company key matches the ledger's stripped form
        comp = re.sub(r"\s*\((YC|VC|auto)\)\s*$", "", (it.get("company") or "").lower())
        if statuses.get(key, "") in ledger.TERMINAL:
            continue
        if notfit_counts.get(comp, 0) >= 3:
            continue
        if goodfit_counts.get(comp, 0) > 0:
            it["score"] = it.get("score", 0) + 10  # learned boost
        out.append(it)
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out, total


def _rationale(it):
    """Concise 'why this qualified' from the scorer's archetype + bonuses + source tag."""
    arch = it.get("archetype") or "?"
    bons = [b for b in (it.get("bonuses") or []) if b][:4]
    src = it.get("source", "")
    fund = ""
    for tag in (" (YC)", " (VC)", " (auto)"):
        if tag.strip("() ") in (it.get("company") or ""):
            fund = tag.strip()
    return f"[{arch}] " + "; ".join(bons) + (f" · via {src}" if src else "") + (f" {fund}" if fund else "")


def _recent_observations_tail():
    """Read feedback/observations.md and return a short tail block listing the
    last N observation entries added in the past 36h. Returns '' if no recent ones."""
    import os, re
    from datetime import datetime, timedelta
    path = str(repo_paths.FEEDBACK / "observations.md")
    if not os.path.exists(path):
        return ""
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return ""
 # entries are split by `\n---\n## YYYY-MM-DD HH:MM` headers
    entries = re.split(r"\n---\n## ", text)
    if len(entries) <= 1:
        return ""
    cutoff = datetime.now() - timedelta(hours=36)
    recent = []
    for raw in entries[1:]:
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", raw)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if ts < cutoff:
            continue
        recent.append(raw.strip())
    if not recent:
        return ""
    out = [f"---", f"📝 You sent {len(recent)} observation(s) in the past 36h (full text: feedback/observations.md):"]
    for r in recent[-3:]:  # show up to 3, newest last
 # take everything after the header line as preview, capped
        body_start = r.find("\n")
        preview = r[body_start:].strip() if body_start >= 0 else r
        preview = preview[:400] + ("..." if len(preview) > 400 else "")
        out.append(f"  • {preview}")
    return "\n".join(out)


def _digest_paused():
    """Check if Jordan has paused digests via 'pause digest' directive."""
    from alice import safe_state
    p = str(repo_paths.FEEDBACK / "digest-prefs.json")
    try:
        prefs = safe_state.atomic_read(p, default={}) or {}
        return prefs.get("paused", False)
    except Exception:
        return False


def _alice_thread_responses():
    """Render new Alice thread responses (from triage_observations) for this digest cycle.
    Only includes threads whose Alice response hasn't been published in a prior digest."""
    import json
    from pathlib import Path
    from alice import safe_state
    state_path = Path(str(repo_paths.FEEDBACK / "digest-published-threads.json"))
    threads_dir = Path(str(repo_paths.FEEDBACK / "threads"))
    if not threads_dir.exists():
        return ""
    try:
        published = set(safe_state.atomic_read(state_path, default=[]) or [])
    except Exception:
        published = set()
    all_threads = sorted(threads_dir.glob("thread-*.md"))
    new = []
    for tp in all_threads:
        tid = tp.stem
        if tid in published:
            continue
        new.append((tid, tp.read_text()))
    if not new:
        return ""
    out = ["OPEN THREADS (my responses to your observations):", ""]
    for tid, content in new:
        m = re.search(r"## Alice's response[^\n]*\n(.+?)(?=\n##|\Z)", content, re.S)
        response = m.group(1).strip() if m else "(no response captured)"
        m2 = re.search(r"## Operator's observation\s*\n(.+?)(?=\n##|\Z)", content, re.S)
        obs = m2.group(1).strip()[:200] if m2 else ""
        out.append(f"  {tid.upper()}")
        out.append(f"  > {obs[:180]}{'...' if len(obs) > 180 else ''}")
        out.append(f"  {response}")
        out.append("")
 # mark as published — atomic union under lock to survive concurrent runs
    new_ids = {t for t, _ in new}
    def mutator(current):
        cur_set = set(current or [])
        return sorted(cur_set | new_ids), None
    safe_state.atomic_update(state_path, mutator, default=[])
    return "\n".join(out)


def _pending_questions_block():
    """Show targeted questions Jordan needs to answer (for in-flight applications)."""
    import json
    from pathlib import Path
    apps = Path(str(repo_paths.APPLICATIONS))
    if not apps.exists():
        return ""
    pending = []
    for app_dir in apps.glob("*/"):
        q_path = app_dir / "targeted-questions.md"
        meta_path = app_dir / ".metadata.json"
        if not q_path.exists() or not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if meta.get("final_generated"):
            continue  # answers already integrated
        company = meta.get("company", app_dir.name)
        role = meta.get("role", "")
        n_answered = len(meta.get("answers_received", []))
        pending.append((company, role, q_path.name, n_answered))
    if not pending:
        return ""
    out = ["TARGETED QUESTIONS WAITING ON YOU:"]
    for c, r, fname, n in pending:
        out.append(f"  • {c} - {r}: see applications/<slug>/{fname}  (you've answered {n} so far; reply 'answer 1:' etc.)")
    return "\n".join(out)


def _debrief_prompts_block():
    try:
        from alice.ops.debrief import get_pending_prompts
        pending = get_pending_prompts()
        if not pending:
            return ""
        out = ["DEBRIEF PROMPTS:"]
        for p in pending:
            out.append("")
            out.append(p["prompt"])
        return "\n".join(out)
    except Exception:
        return ""


def _ledger_push_and_email(new_qualified, ledger_total):
    from alice.jobcfg import load
    if _digest_paused():
        print("[digest: paused per Jordan's 'pause digest' directive]")
        return
    inserted = 0
    expected_job_keys = []
    try:
        import ledger, verify
        if ledger.available():
            items_to_insert = [
                {"company": it.get("company", ""), "role": it.get("title", ""),
                 "comp": it.get("comp", ""), "source": it.get("source", ""),
                 "score": it.get("score", ""), "url": it.get("url", ""),
                 "job_key": f"{it.get('source')}|{it.get('ext_id')}",
                 "rationale": _rationale(it)} for it in new_qualified]
            expected_job_keys = [it["job_key"] for it in items_to_insert]
            inserted = ledger.insert_new(items_to_insert)
 # C2 verifier: fresh-auth col-J scan for the expected job_keys.
            if expected_job_keys:
                vr = verify.verify_sheet_insert(expected_job_keys)
                if not vr.ok:
                    print(f"[VERIFY ERROR sheet_insert: {vr.claim}]")
    except Exception as e:
        print(f"[ledger insert failed: {e}]")
    cfg = load()
    sid = cfg.get("LEDGER_SHEET_ID", "")
    link = f"https://docs.google.com/spreadsheets/d/{sid}" if sid else ""
    today = datetime.now().strftime("%Y-%m-%d")

 # Record daily_delta's own activity (sourcing) so it shows up in the digest.
    try:
        from alice.persistence import activity_log
        if inserted:
            src_summary = f"{inserted} new qualified role{'' if inserted == 1 else 's'} added to ledger"
        else:
            src_summary = "no new qualified roles (nothing cleared domain + calibration gates)"
        activity_log.record(
            step="daily_delta",
            summary=src_summary,
            count=inserted,
            status="ok" if inserted else "noop",
            details={"qualified_today": inserted,
                     "ledger_total": ledger_total},
        )
    except Exception as e:
        print(f"[activity_log: {e}]")

    subj = f"Alice digest {today}: {inserted} new role(s)" + (f" · {ledger_total} in list" if ledger_total is not None else "")
    lines = []

 # 1. FOCUS BLOCK (always at top — Alice's primary discipline)
    try:
        from alice.persistence import focus_enforce
        block = focus_enforce.compute_focus_block()
        lines.append(focus_enforce.render_focus_block(block))
        lines.append("")
        distraction = focus_enforce.compute_distraction_flag()
        if distraction:
            lines.append(focus_enforce.render_distraction_flag(distraction))
            lines.append("")
        disengagement = focus_enforce.compute_disengagement_flag()
        if disengagement:
            lines.append(focus_enforce.render_disengagement_flag(disengagement))
            lines.append("")
    except Exception as e:
        print(f"[focus block render failed: {e}]")

 # 2. ACTIVITY TODAY (full picture of what Alice did across all daily steps)
    try:
        from alice.persistence import activity_log
        activity_section = activity_log.render_activity_section()
        if activity_section:
            lines.append(activity_section)
            lines.append("")
    except Exception as e:
        print(f"[activity section render failed: {e}]")

 # 3. NEW ROLES TODAY (details for any qualified roles)
    if inserted:
        lines.append(f"NEW ROLES TODAY: {inserted} new qualified role(s).")
        lines.append("")
        for it in new_qualified[:12]:
            lines.append(f"  • {it.get('company','')} — {it.get('title','')} | {it.get('comp','n/d')}")
            lines.append(f"    why: {_rationale(it)}")
            lines.append(f"    {it.get('url','')}")
            lines.append("")

 # 3. OPEN THREADS (Alice's responses to observations)
    threads = _alice_thread_responses()
    if threads:
        lines.append(threads)
        lines.append("")

 # 4. TARGETED QUESTIONS WAITING
    q_block = _pending_questions_block()
    if q_block:
        lines.append(q_block)
        lines.append("")

 # 5. DEBRIEF PROMPTS
    d_block = _debrief_prompts_block()
    if d_block:
        lines.append(d_block)
        lines.append("")

 # 5b. EXPERIENCE CAPTURE — pending candidates Jordan needs to confirm
 # Surfacings increment after rendering so silence = rejection after the
 # configured threshold. Confirm/reject directives can be sent via reply
 # ("confirm exp-cand-abc") or via chat (experience_store.parse_and_apply_reply).
    try:
        from alice.persistence import experience_store
        candidates = experience_store.get_pending_candidates()
        if candidates:
            block = experience_store.render_digest_block(candidates)
            if block:
                lines.append(block)
                lines.append("")
            experience_store.mark_digest_surfaced(
                [c["candidate_id"] for c in candidates]
            )
 # Surface contradictions Jordan should resolve
        contradictions = experience_store.find_contradictions()
        if contradictions:
            lines.append("EXPERIENCE CONTRADICTIONS — please resolve:")
            for pair in contradictions[:5]:
                lines.append(
                    f"  • {pair['tag']}: entries {pair['entries']} "
                    f"disagree on metric values {pair['values']}"
                )
            lines.append(
                "  (use experience_store.supersede_entry to mark one stale)"
            )
            lines.append("")
    except Exception as e:
        print(f"[experience candidates section failed: {e}]")

 # 5d. CORRECTION CAPTURE — pending decision-feedback candidates Jordan confirms.
 # Mirrors the experience-store block above with the SAME three-call surface
 # (get_pending_candidates -> render_digest_block -> mark_digest_surfaced).
 # This is render_digest_block's caller: it surfaces captured corrections for
 # confirmation before they auto-expire unseen (DIGEST_EXPIRY_THRESHOLD).
 # Confirm/reject via reply
 # ("confirm corr-cand-abc"), parsed by decision_feedback.parse_and_apply_reply
 # (already wired at the telegram_bot reply path). Additive: renders nothing
 # when there are no pending candidates, so it cannot disrupt the existing
 # digest. Fails loud (prints to cron.log) rather than swallowing.
    try:
        from alice.persistence import decision_feedback
        corr_candidates = decision_feedback.get_pending_candidates()
        if corr_candidates:
            corr_block = decision_feedback.render_digest_block(corr_candidates)
            if corr_block:
                lines.append(corr_block)
                lines.append("")
            decision_feedback.mark_digest_surfaced(
                [c["candidate_id"] for c in corr_candidates]
            )
    except Exception as e:
        print(f"[correction candidates section failed: {e}]")

 # 6. BEHAVIOR PATTERNS
    try:
        from alice.persistence import behavior_patterns
        bp_text = behavior_patterns.render(behavior_patterns.detect_patterns())
        if bp_text:
            lines.append(bp_text)
            lines.append("")
    except Exception as e:
        print(f"[behavior patterns render failed: {e}]")
    if link:
        lines += [
            "Two ways to label (system uses your labels to tune next run):",
            "  (1) Open sheet + tap the status dropdown:",
            "      " + link,
            "  (2) Reply to this email — one job per line:",
            "      northwind enterprise: good fit",
            "      acme flowcad: submitted",
            "      not a fit: example growth cross channel",
            "      materials pending: watershed",
            "      Aliases: good/yes/fit, no/pass/skip, pending/drafting/wip, applied/sent, closed/rejected.",
            "      Ambiguous company names are skipped (logged) — make substrings unique enough to identify one row.",
            "  (3) Anything else in your reply (observations, complaints, ideas) is captured to",
            "      feedback/observations.md and surfaced here the next morning.",
        ]

 # Tail: recent observational feedback you sent in past replies
    tail = _recent_observations_tail()
    if tail:
        lines += ["", tail]
    try:
        import notify_email, notify_telegram, verify
        body = "\n".join(lines)
        ok = notify_email.send(subj, body, digest=True)
 # C2 verifier: IMAP Sent-folder probe.
        if ok:
 # Search by the digest's date substring so the IMAP search is unique.
            vr = verify.verify_email_send(subject_substr=subj[:60])
            if not vr.ok:
                print(f"[VERIFY ERROR email_send (digest): {vr.claim}]")
        try:
            tg_res = notify_telegram.send_with_id(f"Alice digest ready: {subj}")
            if tg_res.get("ok"):
                vr = verify.verify_telegram_send(message_id=tg_res.get("message_id"))
                if not vr.ok:
                    print(f"[VERIFY ERROR telegram_send (digest): {vr.claim}]")
        except Exception as _tge:
            print(f"[telegram digest ping failed: {_tge}]")
    except Exception as e:
        print(f"[email failed: {e}]")


def _prepare_fit_judge(recs):
    """Key-mapping adapter + body-presence partition.

    The survivor recs carry source-schema keys (desc/base_low/base_high/ext_id);
    fit_judge.judge_survivors() reads body/comp_low/comp_high/id. A bare
    judge_survivors(recs) call would read body=None and judge EVERY role against
    fit_judge's "(no JD body available)" fallback -- a silent garbage-verdict
    failure (no error, just wrong answers). This maps the keys explicitly IN
    PLACE (originals preserved for _write_output/ledger), then partitions: recs
    with a usable body are 'judgeable'; body-less recs are annotated
    UNJUDGED-NO-BODY and kept OUT of the judge (loud, not a fake verdict).
    Returns (judgeable, unjudged)."""
    judgeable, unjudged = [], []
    for r in recs:
        r["body"] = r.get("desc")
        r["comp_low"] = r.get("base_low")
        r["comp_high"] = r.get("base_high")
        r["id"] = r.get("ext_id")
        if r.get("body"):
            judgeable.append(r)
        else:
            r["fit_verdict"] = "UNJUDGED-NO-BODY"
            r["fit_reason"] = "no JD body persisted; not sent to fit-judge"
            r["driving_constraint"] = "no_body"
            r["fit_judge_model"] = None
            unjudged.append(r)
    return judgeable, unjudged


def run(dry_run=False, out_days=3, state_path=None, use_ledger=False):
 # State backend: JSON file (cloud/routine) or sqlite pipeline.db (local).
    if state_path:
        store = JsonState(state_path)
        first = store.is_first()
        mark = store.mark
        mark_skip = store.set_skip          # record gate drop-reason
        def _commit():
            if not dry_run:
                store.commit()
        _close = lambda: None
    else:
        conn = sqlite3.connect(DB)
        db_init(conn)
        first = is_first_run(conn)
        def mark(s, i, co, t, u, body=None, location=None,
                 comp_low=None, comp_high=None, remote_flag=None):
            return mark_seen(conn, s, i, co, t, u, body=body, location=location,
                             comp_low=comp_low, comp_high=comp_high,
                             remote_flag=remote_flag)
        def mark_skip(s, i, reason):        # record gate drop-reason
            return set_skip_seen(conn, s, i, reason)
        def _commit():
            if not dry_run:
                conn.commit()
        _close = conn.close
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=out_days if first else 14)).date().isoformat()

 # Harvest targets/*.md -> discovered_slugs.json before sourcing so
 # _ats_boards() (called inside pull_ats()) sees the harvested boards on this
 # very run. dry_run=True skips the file write (no side effects during
 # test/dry-run invocations).
    _harvested = _harvest_targets(dry_run=dry_run)
    if _harvested:
        print(f"[targets-harvest] merged {len(_harvested)} new board(s): "
              + ", ".join(f"{e[0]}={e[1]}:{e[2]}" for e in _harvested))
    sources = [pull_ats(), pull_remotive(), pull_remoteok(), pull_jobicy(),
               pull_himalayas(), pull_hn()]
    known_boards = {(a, s) for _, a, s in _ats_boards()}
    discovered = {}  # (ats, slug) -> company name, harvested from aggregator/HN URLs
    new_qualified, stats = [], {"scanned": 0, "new_ids": 0, "role_skip": 0,
                                "domain_skip": 0, "remote_skip": 0, "killed": 0,
                                "travel_skip": 0, "qualified": 0}
 # Bounded dropped-sample for the rescue judge.
 # Collected only when ALICE_FIT_JUDGE=1; invisible otherwise.
    _rescue_active = os.environ.get("ALICE_FIT_JUDGE", "1") != "0"  # judge is default-on (set ALICE_FIT_JUDGE=0 to disable)
    _DROPPED_SAMPLE_MAX = int(os.environ.get("ALICE_DROPPED_SAMPLE_MAX", "20"))
    dropped_sample: list = []   # bounded; each entry: {**j, skip_reason=...}
    dropped_total = 0           # all drops (before cap), for log

    def _collect_drop(rec, reason):
        """Collect a gate-dropped rec into the bounded rescue sample.

        NO-OP when rescue is inactive. Mutates dropped_total via nonlocal.
        The rec is copied (shallow) so later gate mutations don't affect it.
        """
        nonlocal dropped_total
        dropped_total += 1
 # Persist WHY this role was dropped — ALWAYS, independent of the rescue
 # sample. The seen row already exists (mark() inserted it above).
        try:
            mark_skip(rec.get("source"), rec.get("ext_id"), reason)
        except Exception as _e:
            try:
                import obs; obs.capture(_e, where="daily_delta:_collect_drop:mark_skip")
            except Exception:
                pass  # diagnostic-only; never let skip-reason break the pipeline
        if _rescue_active and len(dropped_sample) < _DROPPED_SAMPLE_MAX:
            dropped_sample.append({**rec, "skip_reason": reason,
                                    "keyword_dropped": True})

    for src in sources:
        for j in src:
            stats["scanned"] += 1
            if j.get("ext_id") is None:
                continue
 # Persist the FULL JD body + structured fields the source already
 # fetched. mark() captures these on the new-insert path so the body
 # is not fetched-then-discarded; the hidden-travel gate runs on the
 # stored body and the fit-judge can consume it.
            is_new = mark(j["source"], j["ext_id"], j.get("company"),
                          j.get("title"), j.get("url"),
                          body=j.get("desc"), location=j.get("location"),
                          comp_low=j.get("base_low"), comp_high=j.get("base_high"),
                          remote_flag=j.get("remote_flag"))
            if not is_new and not first:
                continue  # not new since last run
            stats["new_ids"] += 1 if is_new else 0
            if not _role_ok(j.get("title", "")):
                stats["role_skip"] += 1
                _collect_drop(j, "role_skip")
                continue
            if TERRITORY_RE.search(j.get("title", "")):
                stats["role_skip"] += 1
                _collect_drop(j, "role_skip")
                continue  # territory/field AEs are travel-prone by archetype
            _text = j.get("title", "") + " " + j.get("desc", "")
            if _domain_blocked(_text):
                stats["domain_skip"] += 1
                _collect_drop(j, "domain_skip")
                continue
            if j.get("domain_gate") and not _domain_positive(_text):
                stats["domain_skip"] += 1
                _collect_drop(j, "domain_skip")
                continue
 # auto-grow: harvest the ATS board of any on-domain company found via aggregator/HN
            if j.get("domain_gate"):
                hit = _ats_from_url(j.get("url", ""))
                if hit and hit not in known_boards:
                    discovered[hit] = (j.get("company", "") or "")[:60]
            geo = (j.get("title", "") or "") + " | " + (j.get("location", "") or "")
            if not _remote_us_ok(geo, j.get("remote_flag", False), body=j.get("desc")):
                stats["remote_skip"] += 1
                _collect_drop(j, "remote_skip")
                continue
 # date sanity (when available)
            d = (j.get("date") or "")[:10]
            if d and d < cutoff:
                continue
            listing = {"company": j.get("company", ""), "role_title": j.get("title", ""),
                       "description": j.get("desc", ""), "location": j.get("location", ""),
                       "remote_policy": "remote",
                       "base_salary_low": (j.get("base_low") or None),
                       "base_salary_high": (j.get("base_high") or None)}
            scored = score_listing(listing)
            if scored["tier"] == "killed":
                stats["killed"] += 1
                _collect_drop(j, "killed")
                continue
            tr, hid = _travel_flags(j.get("desc", ""))
            if tr or hid:
                stats["travel_skip"] += 1
                _collect_drop(j, "travel_skip")
                continue  # travel/hidden-travel = drop
            stats["qualified"] += 1
            new_qualified.append({**j, "score": scored["score"], "tier": scored["tier"],
                                  "archetype": scored["archetype"], "track": scored["track"],
                                  "bonuses": scored.get("bonuses", []),
                                  "comp": (f"${j['base_low']:,}-{j['base_high']:,}"
                                           if j.get("base_low") else "n/d")})

    new_qualified.sort(key=lambda x: x["score"], reverse=True)
 # Constraint-driven fit-judge over gate-survivors. Runs only when
 # ALICE_FIT_JUDGE=1. drop_not_fit=False: NOT-FIT roles stay annotated for
 # audit, not cut here.
    if _rescue_active:
        judgeable, unjudged = _prepare_fit_judge(new_qualified)
        try:
            fit_judge.judge_survivors(judgeable, drop_not_fit=False)
        except Exception as e:  # batch-level failure must NOT drop the digest
            for r in judgeable:
                r.setdefault("fit_verdict", "UNJUDGED-JUDGE-ERROR")
                r.setdefault("fit_reason", f"{type(e).__name__}: {e}")
            stats["fit_judge_error"] = f"{type(e).__name__}: {e}"
        stats["fit_judged"] = len(judgeable)
        stats["fit_unjudged_no_body"] = len(unjudged)

 # The corrected BAND is authoritative — NOT-FIT bands are cut from the
 # digest/ledger. Band (dimensional layer) catches competitor/nonrole/gate
 # blockers the raw verdict missed, and keeps pure-build/too-senior as
 # surfaced REACH. Recall-first: unjudged / judge-error roles are KEPT
 # (no fit_band -> falls back to verdict -> not NOT-FIT -> kept).
        _before_cut = len(new_qualified)
        new_qualified[:] = [r for r in new_qualified
                            if (r.get("fit_band") or r.get("fit_verdict")) != "NOT-FIT"]
        stats["fit_judge_cut"] = _before_cut - len(new_qualified)

 # Two-sided rescue — route the bounded dropped sample through the judge
 # so keyword-dropped false-negatives surface. Annotate, don't admit:
 # rescued roles are tagged but NOT injected into new_qualified
 # automatically. Rescue candidates (keyword_dropped AND judge returns
 # FIT/REACH) are logged for operator review.
 # Log dropped_total vs dropped_sampled so silent truncation is visible.
        print(f"[rescue] dropped_total={dropped_total} "
              f"dropped_sampled={len(dropped_sample)} "
              f"(cap={_DROPPED_SAMPLE_MAX})")
        if dropped_sample:
            drop_judgeable, drop_unjudged = _prepare_fit_judge(dropped_sample)
            try:
                fit_judge.judge_survivors(drop_judgeable, drop_not_fit=False)
            except Exception as e:
                for r in drop_judgeable:
                    r.setdefault("fit_verdict", "UNJUDGED-JUDGE-ERROR")
                    r.setdefault("fit_reason", f"{type(e).__name__}: {e}")
                stats["rescue_judge_error"] = f"{type(e).__name__}: {e}"
            rescue_candidates = [
                r for r in drop_judgeable
                if (r.get("fit_band") or r.get("fit_verdict")) in ("FIT", "REACH")
            ]
            stats["rescue_dropped_total"] = dropped_total
            stats["rescue_dropped_sampled"] = len(dropped_sample)
            stats["rescue_judged"] = len(drop_judgeable)
            stats["rescue_unjudged_no_body"] = len(drop_unjudged)
            stats["rescue_candidates"] = len(rescue_candidates)
            if rescue_candidates:
                cands = ", ".join(
                    f"{r.get('title','?')} @ {r.get('company','?')} "
                    f"[{r.get('skip_reason','?')}→{r.get('fit_verdict','?')}]"
                    for r in rescue_candidates
                )
                print(f"[rescue] {len(rescue_candidates)} candidate(s) for review: {cands}")
    ledger_total = None
    if use_ledger:
        new_qualified, ledger_total = _apply_ledger(new_qualified)
 # auto-grow: persist newly-discovered ATS boards for next run's full-fidelity pull
    if discovered and not dry_run:
        path = REPO / "targets" / "discovered_slugs.json"
        existing = json.loads(path.read_text()) if path.exists() else []
        have = {(r[1], r[2]) for r in existing}
        for (ats, slug), name in discovered.items():
            if (ats, slug) not in have:
                existing.append([name or slug, ats, slug])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=1))
    stats["discovered_boards"] = len(discovered)
    stats["anomalies"] = _gate_anomalies(stats)  # durable starvation signal
    if stats["anomalies"]:
        for _a in stats["anomalies"]:
            print(f"[gate-anomaly] {_a}")
    _commit()
    _write_output(today, first, new_qualified, stats, dry_run)
    if use_ledger and not dry_run:
        _ledger_push_and_email(new_qualified, ledger_total)
    _close()
    return new_qualified, stats, first


def _gate_anomalies(stats, min_denom=30):
    """Durable starvation / over-pass detector. So '0 qualified' is never
    indistinguishable from 'a gate is silently starving the pipeline', flag when:
    a single gate drops >90% of new postings, OR >90% pass all gates, OR 0 qualified
    from a meaningful batch. Returns human-readable anomaly strings (empty = healthy).
    The per-row 'which role, which gate' trail lives in seen_jobs.skip_reason;
    this is the aggregate signal that says WHEN to go look."""
    denom = stats.get("new_ids", 0)
    if denom < min_denom:
        return []  # batch too small to judge a rate (avoids noise on quiet runs)
    out = []
    gate_labels = [("role_skip", "role-shape"), ("domain_skip", "off-domain"),
                   ("remote_skip", "not-remote-US"), ("killed", "comp/seniority-killed"),
                   ("travel_skip", "travel")]
    for g, label in gate_labels:
        n = stats.get(g, 0)
        if n / denom > 0.90:
            out.append(f"gate '{label}' dropped {n}/{denom} ({100*n/denom:.0f}%) of new postings "
                       f"— possible over-filtering / starvation (inspect the '{g}' skip_reason cohort)")
    q = stats.get("qualified", 0)
    if q == 0:
        out.append(f"0 qualified from {denom} new postings — starvation or genuinely-nothing-matched; "
                   f"the per-gate drops + seen_jobs.skip_reason isolate which")
    elif q / denom > 0.90:
        out.append(f"{q}/{denom} ({100*q/denom:.0f}%) passed all gates — possible under-filtering")
    return out


def _write_output(today, first, rows, stats, dry_run):
    OUT.mkdir(exist_ok=True)
    lines = [f"# Daily Delta — {today}", ""]
    if first:
        lines.append("> **First run = seeding.** `seen_jobs` was empty, so all current postings "
                     "were recorded as the baseline. Below are qualified roles **posted in the last "
                     "3 days** (signal for today); from tomorrow this file shows only genuinely-new IDs.")
        lines.append("")
    lines.append(f"Scanned {stats['scanned']} postings (curated ATS + YC/auto boards + Remotive + "
                 f"RemoteOK + Jobicy + Himalayas + HN). New IDs this run: {stats['new_ids']}. "
                 f"Auto-discovered boards: {stats.get('discovered_boards', 0)}. "
                 f"Qualified (on-domain, remote-US, example $150-190K base, senior-IC→mgr, remote-first): "
                 f"**{stats['qualified']}**.")
    lines.append("")
    if not rows:
        lines.append("_No new qualified roles. (Either nothing genuinely new since last run, or "
                     "new postings didn't clear domain + calibration + travel gates.)_")
    else:
        lines.append("| Score | Company | Role | Comp | Source | URL |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows[:60]:
            lines.append(f"| {r['score']} | {r['company'][:24]} | {r['title'][:46]} | {r['comp']} | "
                         f"{r['source']} | {r['url']} |")
    lines.append("")
    lines.append(f"_Drops this run — role-shape: {stats['role_skip']}, off-domain: {stats['domain_skip']}, "
                 f"not-remote-US: {stats['remote_skip']}, comp/seniority-killed: {stats['killed']}, "
                 f"travel/hidden-travel: {stats['travel_skip']}._")
    anomalies = stats.get("anomalies")
    if anomalies is None:
        anomalies = _gate_anomalies(stats)
    for a in anomalies:
        lines.append("")
        lines.append(f"> ⚠️ **GATE ANOMALY:** {a}")
    if _SRC_ERR:
        uniq = sorted(set(_SRC_ERR))[:8]
        lines.append("")
        lines.append(f"_Source errors ({len(_SRC_ERR)} total; diagnostic): " + " || ".join(uniq) + "_")
    content = "\n".join(lines)
    if dry_run:
        print(content); return
    (OUT / f"daily-delta-{today}.md").write_text(content)
    print(f"Wrote {OUT / f'daily-delta-{today}.md'}  ({stats['qualified']} qualified)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=3, help="first-run output window (days)")
    ap.add_argument("--state", help="path to JSON seen-store (cloud/routine mode; e.g. state/seen_jobs.json)")
    ap.add_argument("--ledger", action="store_true", help="read/trim/tune + write the Google Sheet ledger and email a nudge")
    a = ap.parse_args()
    try:
        run(dry_run=a.dry_run, out_days=a.days, state_path=a.state, use_ledger=a.ledger)
    except Exception as e:
 # Fail loud: a crashed sourcing run must NOT be indistinguishable from a
 # legit "0 new roles" day. Write an unambiguous failure marker (so the
 # absence of new roles is never the only signal) and exit non-zero so
 # run_daily surfaces it instead of burying it under "=== run complete ===".
        import traceback
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            OUT.mkdir(exist_ok=True)
            (OUT / f"daily-delta-{today}.md").write_text(
                f"# Daily Delta — {today}\n\n"
                f"**SOURCING RUN FAILED — {type(e).__name__}: {str(e)[:300]}**\n\n"
                "_This is a CRASH, not a zero-result day. The sourcing pass did not "
                "complete; do NOT read the absence of new roles as 'nothing new'._\n")
        except Exception:
            pass
        print(f"SOURCING RUN FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
