# How ATS Actually Works

<!-- sourcebound:purpose -->
*Evidence class: unsourced ATS parser, ranking, and product-behavior notes.*
<!-- sourcebound:end purpose -->

Parser and ranking descriptions require product documentation or dated tests
before use as facts; see [`knowledge/README.md`](../README.md).


## What an ATS Actually Is

An Applicant Tracking System is a database of candidates with a workflow layer on top. The "AI screening" piece most candidates worry about is usually one feature among dozens, and at most companies it is either turned off, weakly weighted, or used only for initial sorting on high-volume roles.

The major ATSes the operator will encounter:

- **Workday.** Used by enterprise (5,000+ employees). Notoriously bad parser. Form-heavy application flow that asks candidates to re-enter resume content into structured fields. Recruiter UX is decent but slow.
- **Greenhouse.** Used by most growth-stage startups ($100M+ ARR or Series C+). Strong scorecard and interview-kit features. Parsing is acceptable. Most modern AI-native companies use this or Ashby.
- **Lever.** Common at Series A-C startups. Lightweight, sourcer-friendly. Parsing is decent.
- **Ashby.** Newer, gaining share at hot AI startups (Anthropic, Ramp, Linear-adjacent). Strong analytics, clean candidate experience. Parsing is good.
- **iCIMS.** Enterprise legacy. Used at Fortune 500. Painful candidate flow, dated UX.
- **Taleo (Oracle).** Old enterprise ATS, declining share but still common at industrial companies (where the operator's manufacturing background gives entry). Worst parser of the group. Mandatory account creation, multi-page application forms.
- **SmartRecruiters, Jobvite, BambooHR.** Smaller share, used at mid-market.

## Resume Parsing: What Actually Happens

When you submit a resume, the ATS runs it through a parser. The major parsing engines are **Sovren** (now owned by Textkernel), **Daxtra**, **HireAbility**, and **Burning Glass / Lightcast**. Greenhouse and Lever use third-party parsers; Workday has its own (which is part of why it's bad).

The parser tries to extract:

- Name, contact info, location
- Work history with company, title, dates, and bulleted descriptions
- Education with school, degree, field, dates
- Skills (matched against an internal taxonomy)
- Certifications, languages, security clearances

**Where parsers consistently fail:**

- Two-column layouts. The parser reads top-to-bottom, left-to-right, and frequently merges content from adjacent columns into the wrong section.
- Tables. Parsed as either gibberish or skipped entirely.
- Headers and footers. Often dropped. Never put contact info only in a header.
- Text inside images, icons, or graphic elements. Invisible to the parser.
- Non-standard section headings ("My Journey" instead of "Experience"). Parsers look for specific section keywords.
- Date formats outside `MM/YYYY` or `Month YYYY`. Inconsistent parsing.
- Fancy fonts or text rendered as vector paths (some design tools do this). Sometimes OCR'd, sometimes lost.

**PDF vs DOCX:** Modern parsers handle both reasonably well, but DOCX parses more reliably because it has explicit structural metadata. PDFs generated from Google Docs or Word are usually fine. PDFs exported from Figma, Canva, or design tools are often a disaster. When in doubt, submit DOCX.

## Keyword Matching: The Truth

The internet is full of "your resume needs an 80% keyword match score." This is mostly nonsense, with one exception.

**What's nonsense:**
- The idea that ATSes auto-reject resumes below a keyword threshold. Most ATSes don't auto-reject anyone. They rank.
- The idea that you should stuff invisible white-text keywords into your resume. This gets caught by any modern parser (they normalize text color) and will get you flagged in the recruiter's view.
- Third-party "ATS scanners" (Jobscan, Resume Worded, etc.) that score your resume against a JD. They use crude keyword overlap, not the actual ATS algorithm. Their scores are not what the recruiter sees.

**What's real:**
- Recruiters use the ATS's search function to filter the candidate pool by keyword. If they search "Salesforce CPQ" and your resume doesn't contain those exact words, you don't appear in the filtered list. This is the real "keyword match" mechanism. Not an auto-reject, but you become invisible to a recruiter who's filtering.
- Some ATSes (Greenhouse, Ashby, modern Workday) now run semantic matching using embeddings. "revenue operations" and "RevOps" cluster together, "AE" and "Account Executive" are recognized as equivalent. But this is uneven across vendors and not something to rely on. Spell things out both ways at least once.
- High-volume roles sometimes use AI-screening tools (Eightfold, Paradox, HireVue's screening layer, or custom OpenAI-based scorers) that rank candidates. The ranking is opaque, biased toward keyword density, and recruiters override it routinely. Don't optimize too hard for the scorer.

## Knockout Questions

The hard filter on most ATSes is not the AI score. It's the knockout question set.

Common knockouts:
- "Are you authorized to work in [country] without sponsorship?" If you answer no and the company doesn't sponsor, you're auto-rejected.
- "Will you require relocation?" Yes → auto-reject for remote-only or location-pinned roles.
- "Years of experience in [X]?" Less than threshold → auto-reject.
- "Are you willing to travel up to [X]%?" No → auto-reject.

**Travel knockout is worth watching.** Some applications ask "willing to travel up to 25%?" as a knockout, even when the role itself only requires 5-10% in practice. A "no" answer here ends the application. For a candidate with a remote-first preference, the right move is to weight remote-first roles, or to find a path around the knockout (HM outreach, referral) rather than answer dishonestly on the form.

## How the Recruiter View Differs from Candidate View

Worth understanding because it changes resume strategy:

- The candidate sees a clean career page with the JD and an Apply button.
- The recruiter sees a queue of candidates with: name, current title/company, location, source channel (referral, LinkedIn, careers page), application date, knockout question answers, ATS rank score, and a one-line resume preview (usually the first 100 chars of the summary or first job title).
- Most recruiters do NOT read your resume from inside the ATS. They click out to the PDF preview. So the ATS-extracted text matters for search/filter, but the visual layout matters for the actual review.

This means: optimize the parsed text for search filterability, AND optimize the visual document for human readability. These goals do not conflict if you write the resume cleanly.

## Ranking Algorithms

Most ATSes assign each candidate a 0-100 score against the req. The score is usually:

- 40-60% keyword overlap with the JD
- 10-20% title similarity
- 10-20% tenure/seniority match
- 10-30% "fit signals" (school, prior employers, location, source channel)

The recruiter sees the score but is not bound by it. In practice, internal recruiters override ATS rankings constantly. A referred candidate with a low score gets reviewed; a high-score cold applicant with a weird resume might get skipped.

The score matters most when the recruiter is doing first-pass triage on a 500+ applicant pile. It matters least when the recruiter is reviewing 20-50 applicants for a specialized role.

## Resume Formatting: What Actually Works

The rules that hold up across all major ATSes:

1. **Single column.** Skip the sidebar. Skip the two-column "skills on the left, experience on the right" template.
2. **Standard section headings.** "Experience," "Education," "Skills." Not "Career Highlights" or "Professional Journey."
3. **Standard date format.** `Mar 2020 - Present` or `03/2020 - Present`. Consistent across all entries.
4. **No tables.** Use line breaks and tabs/spaces for alignment instead.
5. **No headers/footers** for content that matters. Page numbers are fine.
6. **Common fonts.** Calibri, Arial, Helvetica, Garamond, Times New Roman, Georgia, or system fonts like SF Pro. Skip stylized display fonts.
7. **10-12pt body text.** Bigger gets parsed fine but looks juvenile to recruiters.
8. **File name matters.** `Jordan-Avery-Resume.pdf` is correct. `resume_final_v7.pdf` is amateur. Recruiters search their downloads folder by name.
9. **Length.** One page if under 10 years experience, two pages otherwise. A candidate at 10+ years should run two pages without padding.
10. **No photo.** US-norm. EU/LATAM differ. For the operator's US search, no photo.

## Writing for Parser AND Human

The compromise that works:

- Lead each bullet with a verb and a concrete number. "Closed $3.2M ARR in industrial verticals across 12 accounts." Not "Responsible for sales."
- Include the JD's exact technical/tool keywords in your skills section (e.g., "Salesforce, HubSpot, Outreach, Gong, Looker, dbt, SQL, Python") only for tools you've actually used.
- Spell out acronyms once: "Customer Success Manager (CSM)" or "Forward Deployed Engineer (FDE)."
- Repeat key terms 2-3 times across the resume in different contexts. Once in summary, once in a bullet, once in skills.
- Don't keyword-stuff. Recruiters can smell it instantly and it tanks your credibility on the human read.

## What This Means for the Operator

- Maintain the four resume variants (`templates/resume-operator-builder.docx`, `revenue-architect.docx`, `senior-ae.docx`, `tam.docx`) in DOCX format and export PDFs only for submission. The DOCX is the source of truth and parses more reliably.
- For each application, do a 5-minute keyword pass: pull 8-12 specific technical and domain terms from the JD, and confirm they appear in the resume in natural language. Skip if they don't actually describe the experience.
- Knockout questions on travel are an auto-reject risk for a remote-first candidate. Read the application form before investing time. If a travel knockout is hard-coded and the role isn't truly remote, weight other roles or go direct to the HM instead.
- For Track 4 (AI-native), most companies use Greenhouse or Ashby. Both parse cleanly. Don't overengineer the resume; spend the time on a strong one-paragraph cover note in the application instead.
- The "ATS keyword score" tools (Jobscan etc.) are not worth paying for. They optimize for the wrong target. Trust the variant resumes and the JD-specific keyword pass.
- File naming convention: `Jordan-Avery-{Track}-Resume-{YYYY-MM}.pdf`. Recruiters who download it can find it again.
