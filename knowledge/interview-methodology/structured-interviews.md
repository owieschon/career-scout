# Structured Interviews: What Companies Actually Do

<!-- sourcebound:purpose -->
*Evidence class: unsourced interview-method and company-practice notes with uncited research claims.*
<!-- sourcebound:end purpose -->

Research coefficients and company-practice descriptions require primary-source
citations before use as facts; see [`knowledge/README.md`](../README.md).


## The Research Behind Structured Interviews

The thing every interview team has read (or claims to have read) is the **Schmidt & Hunter 1998 meta-analysis** on predictive validity of selection methods. The headline finding: unstructured interviews have a validity coefficient of ~0.38, structured interviews ~0.51, and combined with work samples or cognitive tests, the predictive power jumps significantly.

A 2016 reanalysis by Sackett et al. tempered some of those numbers (true validities are lower than 1998 estimates after correcting for range restriction), but the core finding holds: **structured interviews predict job performance better than chatting with the candidate.**

Google's internal research, particularly the Project Oxygen and Project Aristotle work, plus Laszlo Bock's published findings in *Work Rules!*, drove much of the modern structured interview movement in tech. Google found that:

- Unstructured "gut feel" hiring was no better than chance for predicting performance
- Brainteasers (the famous "how many golf balls fit in a school bus" questions) had zero predictive validity and were retired
- Four interviews was the sweep spot; the marginal predictive power of interviews 5+ was negligible
- Structured behavioral interviews plus work samples were the highest-signal combination

Most growth-stage tech companies adopted some version of this. Most non-tech companies and many enterprise teams still run unstructured "tell me about yourself" chats.

## Behavioral Interviewing: STAR, CAR, SAO

The dominant format for soft-skill assessment. The interviewer asks a question like "tell me about a time you handled a difficult customer," and they're listening for a specific structure in your answer.

The three common frameworks (mostly interchangeable):

- **STAR**. Situation, Task, Action, Result. The most widely taught.
- **CAR**. Context, Action, Result. Compressed STAR.
- **SAO**. Situation, Action, Outcome. Same idea.

What interviewers are actually scoring:

1. Specificity. do you have a real story with real names and numbers, or are you describing a generalized pattern?
2. Your role. what *you* did, not what "we" did. Behavioral interviewers will interrupt and ask "but what did *you* specifically do?" when candidates over-rely on the team voice.
3. Outcome with evidence. measurable result, ideally with a number.
4. Reflection. what would you do differently? This is the question that distinguishes mid-level from senior candidates.

Common candidate mistakes:
- Choosing the wrong story (a five-minute story for a two-minute question)
- Burying the result at the end and running out of time
- Vague outcome language ("the customer was happy" rather than "renewed at $180K, up from $120K")
- Talking about a team win without ever naming their specific contribution

A senior candidate (the operator's level) should have **8-12 stories prepared** that can each be flexed across multiple competencies. The same "I unblocked a stalled deal with a defense prime" story can answer questions about influence, technical depth, persistence, customer empathy, or deal mechanics depending on how it's framed.

## Scorecards and Competency Frameworks

The dominant scorecard model in modern tech recruiting is **Greenhouse's scorecard framework**, which most growth-stage companies use even if they're not on Greenhouse ATS.

A typical scorecard for an AE role:

- Discovery and qualification (1-4 scale)
- Communication / executive presence (1-4)
- Domain knowledge (1-4)
- Closing / commercial instincts (1-4)
- Coachability / growth mindset (1-4)
- Overall recommendation (Strong Yes / Yes / No / Strong No)

For technical roles (Forward Deployed, Solutions Engineering), add:

- Technical depth in [specific stack]
- Customer-facing technical communication
- Problem decomposition
- Code/system design quality

Each interviewer is assigned **specific competencies** to assess. The HM interviews for one set, the cross-functional partner for another, the skip-level for a third. This is why candidates sometimes feel like a later interview "didn't go into the technical stuff", that interviewer wasn't supposed to, and they were scoring something else entirely.

The 1-4 scale is intentional. Most scorecards skip a true neutral midpoint (no 3 on a 1-5) because forcing interviewers to commit either above or below the bar produces sharper signal in debriefs.

## Leveling and Calibration

Calibration is the meeting that happens (usually weekly) where the recruiting team and hiring committee align on what "Senior" actually means for the company. Without calibration, interviewer A and interviewer B will score the same candidate differently, and the company ends up with inconsistent hires.

For a Senior IC role:

- L4 / Senior typically requires 5+ years of progressively scoped work, ownership of significant projects without close supervision, ability to mentor junior staff, demonstrated impact on team or org metrics
- L5 / Staff requires the above plus cross-team influence, technical or strategic depth that's organizationally rare, ability to set direction
- L6 / Principal requires org-wide impact, hiring/mentoring at scale, and is usually a small headcount

For the operator targeting Sr IC → first-line manager, the calibration discussion is about whether 10+ years of cross-functional experience (sales + CS + program mgmt + now ML/full-stack) reads as L5 or L4 at the target company. The answer differs dramatically by company.

## Panel Dynamics and Debriefs

After all interviews complete, the panel runs a **debrief** (sometimes called a hiring huddle, hiring committee meeting, or pow-wow depending on company). This is where the actual hire/no-hire decision gets made.

How it works at a well-run debrief:

1. Each interviewer goes around and shares their scorecard verbally, starting with overall recommendation.
2. **Critical**: most modern teams ask interviewers to share their recommendation *before* hearing others, to prevent anchoring bias. Some require written scorecards to be submitted before debrief.
3. Dissent is investigated. If 3 interviewers say "Strong Hire" and 1 says "No Hire," the no-hire vote usually wins or at least triggers another conversation. A single confident "No" can sink a candidate.
4. The recruiter facilitates and writes up the decision.

How it works at a poorly-run debrief:

- Hiring manager goes first, anchors the room
- Junior interviewers defer
- Decisions get made on vibes
- Recruiter pushes for "let's move forward" because they need to fill the req

**Implication for candidates:** you do not need every interviewer to love you, but you need to clear the bar for each one. A single weak interview can kill the candidacy even if the others are strong.

## Topgrading (Bradford Smart's Method)

Used at some private equity portfolio companies, certain industrial firms, and pockets of mid-market B2B. Less common in modern tech but the operator will encounter it given the manufacturing/industrial background.

The signature Topgrading move is the **chronological deep-dive interview**, which can last 2-4 hours and walks through every role on the resume in chronological order. For each role:

- Why did you take this job?
- What were you brought in to do?
- What were the high points and low points?
- What did your manager think of you?
- Why did you leave?
- **The "would-you-hire-again" question:** "When I call your former manager [Name], on a scale of 1-10, how would they rate your performance? And what would they say your weaknesses were?"

The "would-you-hire-again" question is the Topgrading signature. The interviewer will then sometimes actually call that former manager (with your permission). Sandbagging or inflating in the interview gets exposed immediately when references contradict.

How to prepare: have honest, specific answers for every role, including weaknesses your former manager actually called out. Sanitized "my biggest weakness is perfectionism" answers fail.

## Case Interviews, Role Plays, Work Samples

For the operator's tracks, these will appear:

- **Sales role play** (Track 1, AE roles). You're given a fake company, told you're the AE, and the interviewer plays a customer. Usually a discovery call or demo. They're scoring discovery question quality, objection handling, and whether you can keep control of the call. Common at Salesforce, HubSpot, and most growth-stage B2B SaaS.
- **Mutual close plan / deal review** (Track 1, Sr AE). You're asked to walk through a recent deal in detail. They want to hear deal mechanics, MEDDIC/MEDDPICC, multi-threading, and how you handled procurement.
- **Analytical case / SQL test** (Track 2, RevOps; Track 4, FDE/Applied AI). You're given data and asked to find insights or build a model. Sometimes live-coded, sometimes a take-home.
- **Take-home work sample** (Track 4). Increasingly common at AI-native companies. Expect 4-8 hours of work for a "should take you 2 hours" prompt. Don't lowball the time investment if the company is a top target.
- **Live system design or architecture review** (Track 4, senior FDE). Whiteboard a system that solves a specific customer problem.

Work samples have higher predictive validity than any other interview type per Schmidt & Hunter. They are also where many candidates underprepare. If a company offers a work sample, treat it as the most important step.

## The Truth About "Culture Fit"

Culture fit is the most abused concept in interviewing. Two versions exist:

1. **Legitimate culture-fit assessment.** Does this person work well in our specific operating model. async vs sync, written vs verbal, high-autonomy vs structured, etc.? This is a real signal and well-run companies assess it explicitly with behavioral questions.
2. **Pattern-matching against the existing team.** "Would I want to have a beer with this person?" Often illegal in effect (proxies for age, race, gender, class). Banned at well-run companies but rampant elsewhere, especially in unstructured interviews.

Red flag: any company where the "culture interview" is described as "just a conversation, no pressure." This is almost always (1) unstructured, (2) vibes-based, and (3) where bias enters.

## Interviewer Training (Or Lack Thereof)

The dirty secret: most interviewers have never been trained.

- Tech: Google, Meta, Amazon, Stripe, Anthropic, etc. have mandatory interviewer training (4-8 hours minimum). Their interviewers are usually pretty good.
- Most growth-stage startups (Series B-D): some training, often a 30-60 minute video. Quality varies wildly.
- Enterprise / non-tech: usually no training at all. The interviewer is going off whatever they remember from their own interview 10 years ago.
- Founders and execs: often the worst interviewers in the company. They've usually never been trained, and their position makes feedback hard.

Read the interview team. If they're asking sharp, behavioral, evidence-based questions, you're at a trained company. If they're asking "where do you see yourself in 5 years?" you're not.

## Red Flags That Eliminate Candidates

From debrief notes across companies:

- Speaking poorly about a previous employer or manager (one mention is recoverable, two is fatal)
- Vague answers when pushed for specifics three times in a row
- Inflated metrics that don't survive light pressure
- Misalignment between resume and verbal account (numbers don't match, dates don't match, title doesn't match)
- Showing up late to a remote interview without a heads-up
- Being unprepared on basic company facts (recent funding, product, target customer)
- Not asking any questions, or asking only about compensation and benefits
- Negative energy or visible irritation, especially with junior interviewers
- Asking the interviewer to repeat the same question multiple times without engaging

## What This Means for the Operator

- Build the 8-12 story bank now, before interviews start. Use the Lattice Additive aerospace/defense deals, the desktop 3D-printing global expansion, the industrial-manufacturer revenue work, and the Cadence Analytics build (a multi-agent platform built to take the guesswork out of customer-and-margin signals via ML and handle the manual revenue-ops workflows via agents, originated from patterns observed across industrial businesses) as the core anchor stories. Each should have specific numbers, customer descriptions where allowed, and a clear "what I personally did."
- For Track 4 (AI-native), expect a take-home work sample. Treat it as the highest-use step. The Cadence Analytics build and production ML work give real material to draw on, but the take-home is where the thesis lives or dies.
- For Track 1 (AE), prepare a sales role play and a deal walkthrough. The deal walkthrough should be MEDDIC-structured even if the company doesn't ask in that language.
- Industrial-adjacent companies (where the manufacturing credibility plays) are more likely to run Topgrading. Have honest, specific answers for the "what would your manager say" question for every role going back across the full work history.
- The single dissent kills the candidate. Be sharp with every interviewer in the panel, including the cross-functional partner who "just wants to chat." That conversation is being scored.
