# External Sources, Attempted and Logged

Status legend:
- **accessible**, pulled successfully, content extracted into a subdirectory file
- **partial**, fetched but limited (snippet only, listing page, etc.)
- **needs-credentials**, paywalled or login-gated; the operator can unblock by providing credentials
- **blocked-bot-traffic**, site blocks automated fetches; would need manual extraction or RSS
- **needs-research**, not yet attempted

Updated: 2026-05-28 (initial build; second-pass ingestion attempt blocked, see note below).

> **2026-05-28 ingestion attempt:** Alice attempted to fetch all Tier 1, 2, and 3 sources in a single pass. WebFetch, WebSearch, and curl-via-Bash were all denied by the harness permission layer in this session. No content was extracted. To unblock the next pass, the operator needs to either (a) allow `WebFetch` + `WebSearch` for this project in `.claude/settings.json`, or (b) allow `curl` in Bash so Alice can hit public JSON endpoints (Reddit, vendor blogs) directly. Recommended minimal allow: `WebFetch` for the domains listed below, plus `Bash(curl:*)` scoped to the same domains.

---

## Tier 1, Practitioner

| Source | URL | Value | Status | Unlock |
|---|---|---|---|---|
| Recruiting Brainfood | recruitingbrainfood.com | Weekly digest of recruiting industry, sourcing tactics, vendor news, practitioner debates | blocked-permissions (2026-05-28) | Allow WebFetch in settings, or forward the newsletter archive |
| Loxo blog | loxo.co/blog | Modern recruiting platform; tactical content on sourcing, ATS+CRM workflows | blocked-permissions (2026-05-28) | Allow WebFetch in settings |
| SourceCon | sourcecon.com | Deep sourcing tradecraft, Boolean strings, X-ray, AI sourcing | blocked-permissions (2026-05-28) | Allow WebFetch in settings |
| Greg Savage blog | gregsavage.com.au | 30-year recruiter; sharp opinions on industry dynamics | blocked-permissions (2026-05-28) | Allow WebFetch in settings |
| Reddit r/recruiting | reddit.com/r/recruiting | What recruiters actually say to each other | blocked-permissions (2026-05-28) | Allow `Bash(curl:*)` for old.reddit.com or allow WebFetch |
| Reddit r/recruitinghell | reddit.com/r/recruitinghell | Candidate-side war stories; reveals worst-case ATS / interview behaviors | blocked-permissions (2026-05-28) | Same as above |

## Tier 2, Structured / Vendor

| Source | URL | Value | Status | Unlock |
|---|---|---|---|---|
| SHRM | shrm.org | Authoritative HR practice; comp surveys, legal guidance | needs-credentials | SHRM membership ($229/yr) |
| LinkedIn Talent Solutions | linkedin.com/business/talent | Hiring trend reports, Talent Insights data | blocked-permissions (2026-05-28) | Allow WebFetch; some content still gated behind LinkedIn login |
| Greenhouse blog | greenhouse.io/blog | ATS vendor; structured interviewing, scorecards | blocked-permissions (2026-05-28) | Allow WebFetch |
| Lever blog | lever.co/blog | ATS+CRM vendor; talent-acquisition workflows | blocked-permissions (2026-05-28) | Allow WebFetch |
| Josh Bersin | joshbersin.com | HR industry analyst; market research | blocked-permissions (2026-05-28) | Allow WebFetch for free posts; deeper reports need Bersin Academy |
| HBR hiring | hbr.org | Hiring + leadership longform | needs-credentials | HBR digital sub ($120/yr), 4 free articles/month |

## Tier 3, Current Landscape

| Source | URL | Value | Status | Unlock |
|---|---|---|---|---|
| Levels.fyi | levels.fyi | Verified comp data, leveling by company | blocked-permissions (2026-05-28) | Allow WebFetch |
| Glassdoor | glassdoor.com | Salary + interview review data | blocked-bot-traffic | Login wall on most pages; consider Bing cache |
| HireVue | hirevue.com | Async video interview vendor; how AI evaluates candidates | blocked-permissions (2026-05-28) | Allow WebFetch |
| Paradox | paradox.ai | Conversational AI for recruiting (Olivia) | blocked-permissions (2026-05-28) | Allow WebFetch |
| Phenom | phenom.com | Talent experience platform; career site personalization | blocked-permissions (2026-05-28) | Allow WebFetch |

## Other candidates worth queueing

- **Ask a Manager** (askamanager.org), Alison Green's column; hiring-manager / candidate scenarios.
- **Workology** (workology.com), HR podcast + blog, recruiting tech reviews.
- **ERE** (ere.net), Practitioner-focused; some paywalled.
- **The Muse / The Cut career columns**, candidate-facing but reveal what's working.
- **Hung Lee's "Recruiting Brainfood" podcast**, interview transcripts often summarize industry shifts.
- **GitHub's "Octoverse" reports**, for engineering hiring signal.
- **State of DevOps / State of AI Engineering surveys**, leveling expectations.

## How to unblock credential-gated sources

The operator runs a tight budget, so subscription unlocks need to clear a value test:
- SHRM ($229), useful if Alice is regularly cited as HR-policy authority. Probably skip.
- HBR ($120), broad utility beyond hiring. Worth it if the operator reads it anyway.
- Levels.fyi, free tier sufficient for comp data structure understanding.
- LinkedIn Recruiter, out of scope; ~$8,800/yr enterprise tool.

Most valuable free-tier additions: Reddit JSON API archives, public recruiter blogs, and the major ATS vendor blogs. These can be ingested with a simple periodic fetch script.
