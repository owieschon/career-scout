# Sourcing Calibration

<!-- clean-docs:purpose -->
Generic, non-identifying matching rules for the sourcing engine. No personal data; matching is rule-based, not identity-based.
<!-- clean-docs:end purpose -->


## Hard gates (a role must pass all)
- **Location:** remote-first preference within the US. Reject relocation-required and non-US-only.
- **Total comp band:** an example target band (e.g. $150k–$190k base, USD). Reject roles topping out well below the floor; penalize bands starting well above the ceiling. (Illustrative example values, not a real preference.)
- **Seniority:** senior IC → first-line manager. Reject VP / Head-of / Director-requiring-10+-years (too senior) and Associate / SMB / entry / SDR / BDR (too junior). Exception: founding / first-commercial-hire at a seed/Series-A company.

## Domain scope
- **In:** advanced manufacturing, hardware, industrial, industrial-IoT, additive / 3D-printing, CAD / PLM / engineering software, robotics, B2B software, AI/ML.
- **Out (no track record; screens out on domain):** cybersecurity, observability / data-infrastructure, fintech / payments, HR-tech.
- Generic "technical B2B SaaS + good comp + remote" is **not** a substitute for domain fit.

## Target role shapes (senior IC → manager)
Revenue Operations · Sales Operations · GTM / Commercial / Business Operations · Deal Desk · Revenue/Sales Strategy · Customer Success (with expansion) · inside/desk Solutions Engineer / Solutions Architect · Technical Account Manager · commercial Product Manager · GTM Engineer · Founding GTM.

## Output
Ranked digest of matching public postings: company, role, comp (if disclosed), source, and a direct link to each posting.

## Feedback loop — labeling roles
Two paths to label a role's status; both update the Google Sheet, both feed the next run's scoring (3+ "not a fit" labels on one company → company-wide suppression).

1. **Open the sheet on your phone:** tap the status dropdown in column G (`new / good fit / not a fit / materials pending / submitted / closed`).
2. **Reply to the digest email:** mix structured commands and freeform feedback in the same email. The parser handles both:

   **Structured (status commands)** — one job per line. `:`, `=`, or space all work as the separator.
   ```
   northwind systems enterprise: good fit
   boreal flowcad submitted
   not a fit: openai growth cross channel
   materials pending: watershed
   redis csm applied
   ```
   - **Status aliases:** good/yes/fit/interested → `good fit`; no/pass/skip/reject → `not a fit`; pending/drafting/wip → `materials pending`; submitted/applied/sent → `submitted`; closed/rejected/dead → `closed`.
   - **Role abbreviations expand automatically:** `csm` → Customer Success Manager, `ae` → Account Executive, `se` → Solutions/Sales Engineer, `sa` → Solutions Architect, `tam` → Technical Account Manager, `fde` → Forward Deployed Engineer, `pm` → Product Manager, `revops` → Revenue Operations.
   - **Substring matching is token-based + AND**: every word in your reply must appear somewhere in the row's company+role (any order). Type just enough words to uniquely identify the row; ambiguous matches (e.g. `openai` matching 8 rows) are skipped and logged, not auto-applied.
   - Quoted lines (`>` prefix, "On X wrote:", iOS signatures) are ignored.
   - Processed messages get IMAP-flagged (idempotent — re-runs skip them).

   **Unstructured (observations, complaints, ideas)** — anything in the reply that isn't a status command is captured verbatim to `feedback/observations.md` with timestamp + reply-subject header. The next morning's digest shows a tail block: `📝 You sent N observation(s) in the past 36h` with previews of each. You can write things like *"hub-bound roles keep slipping through"* or *"the Loopwork CAD bullet was a false positive"* and they'll land in the log instead of being silently dropped. The operator reviews `feedback/observations.md` during the weekly review.

`scripts/imap_reply.py` runs every morning at 2pm before `daily_delta.py`, so labels you sent overnight are applied before today's surfacing pass.
