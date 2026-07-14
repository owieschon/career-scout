# AI Screening Tools in Hiring

<!-- clean-docs:purpose -->
*AI shows up in hiring in three places: sourcing/matching, async video screening, and resume parsing. None of it is as smart as the vendor decks claim. Most of it is unfaccounted for by candidates, who either over-engineer for the wrong signal or assume the system is dumber than it is.* Read this page before changing or relying on AI Screening Tools in Hiring so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow doc-length reason="The AI Screening Tools in Hiring reader path stays in one file because splitting it would separate its operating context from its verification material" -->


## HireVue and Async Video Interviews

HireVue is the dominant async video interview platform in 2024-2026. The mechanics: candidate gets a link, records video responses to pre-set questions (usually 3-7), and the recordings get scored.

What HireVue's AI actually measures has shifted significantly. Through roughly 2019, the platform analyzed:
- **Speech content**: transcription of what you said, scored against a job-specific competency rubric
- **Prosody**: tone, pace, vocal variation
- **Facial expression and "micro-expressions"**

The facial analysis got dialed back hard after 2019-2020 due to EEOC scrutiny, ACLU complaints, and a string of bias studies showing the visual analysis disadvantaged candidates by race and disability. HireVue formally dropped facial analysis from new assessments in early 2021 and publicly committed to content-and-voice-only scoring for most clients. By 2024, the public position is "natural language processing on the transcript plus optional voice features."

What this means for candidates in 2026:
- **What you say matters most.** The transcript is the primary scored input. Structure your answers like written responses: clear thesis, specific evidence, clean close.
- **Voice features still matter for some clients.** Confidence in tone, even pace, audible energy. Mumbling and long hesitation pauses still hurt.
- **Visual scoring is mostly off, but humans review the videos.** A hiring manager or recruiter will watch you. Eye contact with the camera (not the screen), neutral background, reasonable lighting still matter for the human pass.

How to actually do well on a HireVue:
- Read the question, take the prep time (usually 30-60 seconds), write 3 bullets on scratch paper
- Speak to the camera lens, not the preview window
- Aim for 60-90 seconds per answer unless told otherwise
- Open with a one-sentence thesis, give one specific example with numbers, close with the lesson
- Don't try to game speech rate or "smile more." The 2026 systems mostly aren't measuring those, and overcorrecting reads worse on the human pass
- Re-record if you fumble badly. The systems generally allow 2-3 takes per question

## Paradox / Olivia

Paradox makes Olivia, a conversational AI used heavily in high-volume hiring (retail, QSR, frontline) and increasingly in corporate roles. It handles:

- **Initial screening conversations** via chat or SMS
- **Knockout questions** ("Are you authorized to work in the US," "Are you willing to work weekends," etc.)
- **Scheduling coordination**
- **Document collection and basic onboarding**

For senior-IC roles, Olivia is mostly visible at the scheduling and reminders stage, not the screening stage. Where it does screen, it's checking for hard requirements (location, work authorization, salary expectation range, availability) and routing accordingly.

The key insight: Olivia is a knockout filter, not an evaluator. Candidates fail Olivia by answering "no" to a hard requirement, not by giving a weak answer to a soft one. There's nothing to game; just answer the hard questions truthfully and move on.

## Phenom

Phenom is a candidate experience platform used by larger enterprises (Walmart, Mastercard, etc.). It powers:

- **Career site personalization** (job recommendations based on browsing behavior)
- **Candidate-to-job matching** scores shown internally to recruiters
- **Talent CRM** for nurturing passive candidates

For a candidate, Phenom is mostly invisible. The relevant thing is that when you apply at a Phenom-powered career site, you may be tagged for related future roles. This is why some candidates get auto-emails about new roles months later.

## Eightfold

Eightfold is the dominant AI-driven internal mobility and sourcing platform at large enterprises. It does two things:

1. **External sourcing**: scans public profiles, builds candidate matches against open reqs, surfaces "passive" candidates to recruiters
2. **Internal mobility**: helps existing employees find their next role inside the company, often with AI-suggested career paths

Eightfold's matching is based on "capabilities inferred from career history" rather than keyword matching. The claim is it can recognize that a "Customer Success Manager" at one company maps to "Account Manager" at another. In practice it's better than keyword matching but still misses heavily on unconventional career paths.

For a candidate, Eightfold matters when you're being sourced. Recruiters at Eightfold-using companies often reach out with "I saw your profile and you'd be a strong match for X." That's an Eightfold-generated lead. It's not a deep evaluation; it's a "this profile pattern-matches the JD" signal.

## Workday's AI Screening

Workday is the largest HCM/ATS at enterprise scale. Its AI features (rebranded several times, currently "Workday AI" or "Illuminate" in some packages) include:

- **Resume parsing and field extraction**
- **Candidate-to-req matching scores** shown to recruiters
- **Skill inference** from job titles and descriptions

Workday's screening is mostly used at the high-volume top of funnel at large companies. For senior roles, the matching scores are visible to recruiters but rarely dispositive. The recruiter still does a human review.

The persistent myth: Workday "rejects" candidates automatically based on keyword matching. The reality at most enterprises: Workday flags candidates as low/medium/high match. The recruiter chooses what to do. High-volume reqs get filtered by match score; specialized senior reqs typically don't.

## LinkedIn AI

LinkedIn Recruiter has multiple AI-assisted features in 2026:

- **AI-assisted search**: natural language search instead of boolean operators
- **Candidate match scores**: 0-100 scores on how well a profile matches a saved search
- **AI-drafted outreach messages**: recruiter sees a pre-drafted InMail
- **Profile suggestions** to candidates ("add this skill to be more discoverable")

The candidate-facing implication: LinkedIn's algorithm rewards profiles with the right keywords, completeness, and recent activity. A profile that hasn't been updated in 18 months gets surfaced less. A profile with a current title that matches common search patterns gets surfaced more.

The biggest LinkedIn AI lever for candidates: the "Open to Work" private signal (visible only to recruiters, not the green ring). Turning this on increases recruiter outreach by a large multiple. The green ring is a separate setting and signals more publicly.

## The Truth About "ATS Keyword Scoring"

The advice industry has built an enormous edifice on "optimize your resume for the ATS." Most of it is wrong or out of date.

The reality:
- **Greenhouse, Lever, Ashby**: do not score resumes. They parse them, store them, and present them to humans. There is no AI rejection.
- **Workday, iCIMS, Taleo (at large enterprise scale)**: do score resumes against the req. Recruiters can see the scores. Recruiters often ignore the scores for senior roles.
- **Job board scrapers (Indeed, ZipRecruiter)**: their internal matching does use keyword scoring to surface jobs to candidates and candidates to employers. This is different from the company's ATS.

What this means: at startups and most mid-market companies (Greenhouse/Lever/Ashby), there is no keyword filter to game. Your resume is read by a human. At large enterprises (Workday), keyword matching is a hint to recruiters but rarely dispositive for senior roles. The exception is high-volume entry-level roles, where keyword filtering does meaningfully filter.

What does help on resumes regardless: writing bullets that include the actual nouns of the job (the function name, the tool name, the metric name). Not because the AI is scoring keywords, but because the human is skimming for them in 30 seconds.

## Whether AI Screening Can Be Gamed

Mostly no, but with nuance.

- **HireVue**: you can't game the system into thinking a weak answer is strong. You can get more comfortable with the format, prep your stories, and present cleanly. That's not gaming; that's preparation.
- **ATS keyword scoring**: stuffing keywords in white text or copy-pasting the JD doesn't work and often flags as suspicious. Including the real nouns in real context works.
- **LinkedIn profile**: keyword optimization works for discovery (you show up in more searches). It does not make recruiters reach out to candidates whose actual experience doesn't fit.

Where the arms race is heating up: candidate-side AI use.

## Candidate-Side AI

In 2024-2026 the candidate-side use of AI has exploded:

- **ChatGPT/Claude for cover letters and resume bullets**: now near-universal. Recruiters have developed pattern recognition for AI-generated cover letters (specific phrasings, structure, tone). Many recruiters now openly say they ignore cover letters entirely because AI made them low-signal.
- **AI interview coaches** (e.g., Final Round AI, Yoodli, Interview Prep AI): tools that listen to your interview and suggest improvements in real time. Some candidates have used these during live remote interviews; some companies are starting to ban this explicitly in interview consent forms.
- **AI resume tailoring**: tools that auto-rewrite resume bullets per job description. Common, often produces resumes that look like they were written by committee.
- **AI-generated portfolio work**: this is the new frontier. Engineers submitting take-homes done largely by Claude/Copilot. Honest in 2026, since most engineers will use AI tools on the job. Companies are increasingly designing take-homes around process and judgment rather than output.

The detection arms race:
- Recruiters and HMs are getting better at spotting AI-written cover letters. The tell is usually too-clean structure and generic specificity ("I was particularly drawn to your innovative approach to X").
- Some companies use AI-generated-content detectors. These have high false-positive rates and most companies don't act on them as a hard filter.
- Live interview AI assistance is increasingly being detected via gaze patterns and pause structures. Some companies now do "show me your other screens" checks.

The 2026 norm seems to be settling around: candidates can and should use AI as a thinking partner and drafting tool. Submitting work that is verbatim AI output, or using live AI assistance during interviews, is increasingly considered cheating. The line is fuzzy.

## The Legal Landscape

Important regulations and frameworks affecting AI hiring tools in 2026:

- **NYC Local Law 144 (AEDT)**: effective July 2023, requires employers using "automated employment decision tools" in NYC to conduct annual bias audits, publish results, and notify candidates. Compliance has been patchy. The law applies broadly to tools that "substantially assist or replace" hiring decisions.
- **EU AI Act**: classifies AI used in employment decisions as "high risk." Requires risk assessments, human oversight, transparency to candidates. Phased enforcement through 2026. Affects any company hiring in the EU using AI tools.
- **EEOC guidance (May 2023)**: clarified that employers using AI vendors can still be liable under Title VII for disparate impact. Pushed vendors to publish bias audits.
- **Illinois AI Video Interview Act**: requires candidate consent and disclosure for AI-analyzed video interviews.
- **California, Colorado, Maryland**: various proposed/passed bills on AI in hiring through 2024-2026. The patchwork is real.

The practical implication: any sophisticated employer in 2026 is documenting their AI hiring tool use and has human review in the loop. The dystopian "AI rejects you with no human seeing your resume" scenario is more rare than the discourse suggests, especially at companies subject to NYC or EU rules. It does still happen at high-volume entry-level hiring.

## What This Means for the Operator

- **Stop worrying about ATS keyword scoring for senior IC roles.** At Greenhouse/Lever/Ashby companies (most targets), a human reads the resume. Write for the human.
- **HireVue is a transcript game.** If a target company uses HireVue, prep 3-4 STAR-format stories with concrete numbers from the desktop 3D-printing and Lattice Additive roles. Speak to the camera, take the prep time, structure cleanly.
- **The Cadence Analytics portfolio is the moat against AI-generated-application skepticism.** Recruiters in 2026 are pattern-matching against AI-written cover letters. A live portfolio link and a specific build story cuts through that immediately.
- **For FDE/Applied AI roles (Track 4), expect take-homes designed to detect over-reliance on AI tools.** The right posture is honest: "I used AI coding tools heavily on this, here's what I did vs what the tool did, here's where I made the judgment calls." Hiding AI use signals worse than transparent use.
- **Don't use live AI interview assistance.** The detection is improving and getting caught ends the process. Use AI for prep, not for live cover.
