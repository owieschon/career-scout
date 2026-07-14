Canonical positioning spine: the single source of truth every application artifact (sourcing, strategy, outreach, resume, cover letter) derives from, so messaging stops drifting per-role.

# Positioning Spine

<!-- clean-docs:purpose -->
Use this reference when configuring how the fictional candidate's grounded experience is rendered for different role families. It separates stable source facts from audience-specific emphasis so writers can adapt a package without inventing credentials or crossing source boundaries.
<!-- clean-docs:end purpose -->

## The problem this file fixes

The operator persona's career and job-search messaging has a consistency problem: the SAME work gets framed as everything from "Manufacturing Technology Consultant" to "Customer-facing AI builder," depending on the target. A reviewer comparing two applications sees two different people. The mechanism: each artifact reverse-engineers a narrative from the JD on top of sales-heavy resume masters, with no canonical spine. Two failure modes recur:

1. **Inverted credit allocation.** Genuinely senior work (agent orchestration, multi-tenant RLS, guardrails, edge ML inference) gets described in junior terms, while a couple of resume lines reach for theater (a "Bayesian update layer" that does not exist). Claim the hard real things by their proper names; drop the theater.
2. **Wrong headline.** Even when the technical depth surfaces, ML floats to the top (it is the most legible, quantified part) and the agentic engineering gets buried. ML is the crowded, credential-gated lane; agentic engineering is the scarce, hot lane where the target roles live.

This spine resolves both. Every writer renders from it. Emphasis varies by archetype; **facts and core identity never do.**

## Resolved identity (one line)

**An operator-builder who ships production AI agents and came from the customer side of the industries they build for.** Two production agent systems built end to end (Cadence Analytics, Alice), full-stack + ML depth underneath, and 10+ years selling/serving sophisticated B2B/industrial buyers (a major aerospace OEM and a defense prime at Lattice Additive; global markets at a desktop 3D-printing manufacturer).

## The thesis (core narrative, constant across roles)

The persona is the rare person who can **build the AI agent AND talk to the buyer it serves.** Most people who can ship agentic systems cannot sit in front of a customer; most customer-facing people cannot ship. They do both, in a domain (manufacturing/distribution/industrial B2B) they know from the inside. The builder half is proven in code (see `knowledge/experience/cadence-analytics.md`, `alice.md`); the customer half is proven in revenue (see `lattice-additive.md`). The combination is the position.

## GLOBAL RENDER RULES (apply to every artifact)

1. **Agentic-first ordering. Do not invert.** Lead with the agent engineering (multi-pass orchestration, MCP tool servers, multi-model routing, guardrails, three-transport implementations, two production agents). Then full-stack delivery. Then ML as a *validated supporting signal*, never the headline. ML metrics (AUC, concordance) are legible and tempting to lead with: resist. (Enforced in `cadence-analytics.md`'s framing-lock.)
2. **Position by capability + combination, not by title or credential.** The persona has no CS degree and a sales-titled history. Leading with titles invites the weakest comparison. Lead with what has demonstrably been built and the rare pairing. The portfolio is the credential.
3. **Domain is a differentiator, not a liability.** Never write "my background is manufacturing, will I translate?" Write "I build AI for the industrial/distribution buyers I spent a decade selling to." Aerospace/defense/industrial buyers are higher-complexity, not lower.
4. **Claim hard, with proof; never inflate.** Use the recognized technical term (see `messaging-guardrails.md` rename-up table). Never render a banned/over-claim. An honest scope-boundary beats a generous overstatement.
5. **Voice:** direct, specific, evidence-driven. No consulting-speak. No em dashes. Confident without bravado. Technical for technical readers; business-outcome for hiring managers/execs.

> Note: the operator persona ("Jordan Avery") is a fictional engineer used to demonstrate the retrieval/composition engine. Not anyone's real history.

## Archetype-to-framing menu

All framings draw from the SAME evidence base and obey the global rules. They differ in emphasis and which sources lead, never in facts. Pick the framing for the role's archetype; do not improvise a new one.

### A. Applied AI / Agent Engineer (Track 4, HIGHEST UPSIDE)
- **Lead:** Cadence Analytics and Alice as two production agent systems. Multi-pass orchestration, MCP servers, multi-model routing, guardrails, hybrid RAG, edge inference.
- **Sources:** cadence-analytics.md (agent beats first), alice.md, then ML/full-stack as supporting.
- **The combination angle:** "builds agents AND understands the GTM/revenue domain they automate."
- **Title targets:** Applied AI Engineer, AI Engineer, Founding/Forward-Deployed (remote), Member of Technical Staff. Resume variant: operator-builder.

### B. Founding Engineer / first technical hire (seed/Series-A)
- **Lead:** "built a whole multi-agent platform end to end" (Cadence Analytics), plus Alice. Generalist range: agents, full-stack, ML, infra, edge deploy.
- **The combination angle:** can also talk to early customers (the GTM half) — a founding-team multiplier.
- **Sources:** cadence-analytics.md (breadth), alice.md, lattice-additive.md (customer credibility). Resume variant: operator-builder.

### C. AI Solutions Engineer / Forward-Deployed (REMOTE only)
- **Lead:** the combination directly. Builds the solution AND runs the executive-outcome loop. Cadence Analytics (build) + Lattice Additive (executive QBRs, instrumented outcomes, expansion).
- **Travel:** remote-first preference; verify travel expectations per posting, as this archetype is the highest travel risk. (Sourcing title/keyword taxonomy is kept outside the repo to avoid colliding with the sourcing agent.)
- **Sources:** cadence-analytics.md + lattice-additive.md co-lead. Resume variant: operator-builder.

### D. GTM Engineer / RevOps / Revenue Architect (Track 2)
- **Lead:** builder-who-knows-revenue. Cadence Analytics' revenue-ops automation + the manufacturer revenue analytics (~$800K surfaced, plan adopted) + Lattice Additive outcome loop. Agentic automation of revenue workflows is the modern GTM-engineer story.
- **Sources:** cadence-analytics.md (agent + revenue-ops framing), ironclad-industrial.md (analytics), lattice-additive.md. Resume variant: revenue-architect.

### E. TAM / AI Outcomes / Senior CS / Implementation (Track 3)
- **Lead:** the customer-outcome loop (Lattice Additive: executive sponsor, instrumented metric, expansion), MADE UNUSUAL by the fact that the persona can actually build the agents the customer deploys (Cadence Analytics/Alice). This is the lane where the JD says "build agents with customers" and the persona is one of the few CS-track candidates who genuinely can.
- **CRITICAL:** do NOT answer "build agents" with "CRM rebuild." Answer it with Cadence Analytics' agent layer. (This lane is exactly where the inversion happened before.)
- **Sources:** lattice-additive.md (outcome loop) + cadence-analytics.md (agent beats — pulled in because it is tagged cs/tam). Resume variant: tam.

### F. Senior/Founding AE (Track 1)
- **Lead:** the revenue track record (Lattice Additive eight-figure new contracts, strong account growth; desktop 3D-printing global markets), differentiated by technical credibility (the persona builds the product category they would sell). Industrial/manufacturing vertical fit.
- **Sources:** lattice-additive.md, (desktop-3dp.md when written), cadence-analytics.md (technical credibility). Resume variant: senior-ae.

### G. Bridge consulting (Track 5, runway)
- **Lead:** RevOps/AI build-out for manufacturing/distribution SMBs. Cadence Analytics + the manufacturer analytics as the proof. Short-duration, remote.

## Comp + location positioning

- Remote-first preference (US, Columbus OH-based). Prioritize **location-agnostic payers** to avoid a Midwest geo-haircut.
- Target an example builder-lane band (e.g. $150k–$190k base); treat as an illustrative example, not a gate. Equity = upside, not floor.
- **Do not anchor to sales comp history.** Lead with demonstrated technical scope and the builder/GTM combination; let the role's market band set the frame.

## The consistency contract

Every artifact Alice generates (application-strategy, outreach, resume-draft, cover-letter-draft) must:
1. derive its frame from the archetype menu above (not improvise),
2. obey the global render rules (agentic-first, capability-not-title, domain-as-edge, no inflation, voice),
3. ground every claim in the experience sources + `messaging-guardrails.md`,
4. never render a banned claim or an ASPIRATIONAL beat as real.

If a JD does not fit an archetype cleanly, default to the combination thesis (build + GTM) and the agentic-first ordering.
