# Comp From the Employer Side

<!-- clean-docs:purpose -->
*Most candidates negotiate against a model of comp that doesn't match how the company actually sets, approves, and flexes the offer. This document covers what's really happening on the other side of the table: where the bands come from, who can override them, and where real use actually exists.*
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow doc-length reason="The Comp From the Employer Side reader path stays in one file because splitting it would separate its operating context from its verification material" -->


## Where Comp Bands Come From

Companies don't make up comp bands. They buy them from survey vendors. The big ones:

- **Radford (Aon)**: the dominant tech survey. Used by Anthropic, OpenAI, Stripe, Snowflake, most mid-to-large B2B SaaS, most Series C+ startups. Covers US, EU, India, and a handful of other markets. Updated semi-annually.
- **OptionImpact / Pave / Carta Total Comp**: equity-focused, popular at Series A-C startups. OptionImpact (Advanced HR) was the historical leader; Pave and Carta took share over the last 5 years and now dominate the early-stage market.
- **Mercer, Willis Towers Watson, Aon (non-Radford)**: enterprise and traditional industries. Industrial, manufacturing, healthcare companies use these. The operator will see these at industrial companies of the additive-manufacturing stage.
- **Levels.fyi**: crowdsourced, not used by companies for setting bands but used by candidates and recruiters for sanity-checking. Skews tech, skews IC, skews newer-grad.

A company subscribes to one or two of these, picks a **percentile target** for the market (50th-75th is common; "we pay at the 75th percentile" is a common pitch), and uses the survey output to set bands by level and function for each metro.

This means: at any well-organized company, when a recruiter quotes you "the band for this role is $140-$180K base," that number came from a Radford report from 6 months ago, adjusted by the company's targeting policy. It is not arbitrary. It also is not immovable.

## Leveling Frameworks

The L3-L7 vocabulary is mostly Google-derived but has spread broadly. Common mappings:

- **L3 / E3 / IC2**: fresh grad or 1-2 years experience. "Junior."
- **L4 / IC3**: 3-5 years, can own scoped work without close supervision. "Mid" or first "Senior."
- **L5 / IC4**: 5-8+ years, owns major projects, mentors juniors. "Senior" or "Staff" depending on company.
- **L6 / IC5**: "Staff" or "Senior Staff." Cross-team scope, technical or commercial gravity. Small headcount.
- **L7 / IC6**: "Principal." Org-wide impact.

For the operator targeting Sr IC → first-line manager, the relevant bands are L5 IC, L5/M5 manager, or occasionally L4 with senior title at smaller companies. The specific level matters because **comp bands attach to levels, not titles**. A "Senior AE" at company A might be L4 with $130K base, while at company B it's L5 with $170K base. Same title, very different comp.

Manager track has its own ladder, usually M3 (first-line, manages 3-8 ICs) → M4 (manages managers) → M5 (director). M3 typically pays slightly more than the equivalent L5 IC but caps lower.

## How Recruiters Handle "What's Your Expected Comp"

The early-anchor game is real and goes both ways.

**Recruiter playbook:**
- Ask for current comp and/or expected comp in the first call.
- If you give a number below band, they offer at the bottom of band and pocket the savings as a "great hire under budget."
- If you give a number above band, they may flag you as misaligned and pass, OR they may push the comp committee to stretch the band ("this candidate is outside band but the HM really wants them").

**Candidate playbook (what actually works):**
- Don't volunteer current comp unless asked directly. In states where it's illegal to ask (California, Colorado, NY, Washington, Massachusetts, Illinois, more states adding annually), they legally can't.
- When asked expected comp, the cleanest answers are:
  - "I'd like to understand the role and the band before committing to a number. What's the band for this level?"
  - If pushed: "Based on the role scope, somewhere in the $X to $Y range feels right, but I'm open if total package is competitive."
- The trap is giving a precise number too early. $150K signals you'd accept $150K. "$150-$180K depending on equity and base/OTE split" leaves room.

**What recruiters actually have visibility into:**
- The band for the level (definite)
- Comparable recent offers (usually)
- The flexibility they're allowed to use without escalation (yes, see below)
- The company's overall comp philosophy and exception process (yes)

## What "Competing Offer" Actually Does

The biggest use move in negotiation, and the most misunderstood.

A real competing offer with a higher number triggers:

1. **Recompetition.** The recruiter brings the offer to the HM and sometimes to the comp committee. They decide whether to match, beat, or hold.
2. **A comp band exception process** at most companies. Going above band requires VP/CFO approval. Usually possible but slow (3-7 business days).
3. **Sometimes a no-match.** The company decides you're not worth the stretch, and the offer holds. This happens more than candidates expect, especially when the original offer was already at the top of band.

**What doesn't work:**
- Fake competing offers. Recruiters have heard this a thousand times and can usually tell. They'll ask for specifics: which company, what level, what's the base/equity split. Lying here, when caught, blows up the offer entirely.
- Vague "I'm in late-stage process at another company." Has minimal use value. Recruiters discount this to near-zero unless you can name the company and stage.
- A "competing offer" from a company at a much lower tier than the current one. ($90K offer from a no-name doesn't move a $160K Anthropic offer.)

**What does work:**
- A real written offer from a comparable company that you'd actually accept. The recruiter relays it to the HM, and either they match or they don't.
- Multiple processes at late stage. If the current offer is from company A and you're at final-round at company B (named, comparable), that's enough to ask the recruiter for a "best and final" because you need to decide.
- Specific, factual framing: "I have an offer at $X total from [Company]. I prefer this role for [specific reasons]. Can you get to $Y?"

## Equity: RSUs, ISOs, NSOs, Refresh Grants

Where most candidates lose money is not understanding the equity component.

**RSUs (Restricted Stock Units).** Public-company equivalent of stock. You receive shares on a vesting schedule, valued at the stock price on vest date. Standard schedule: 4 years, 1-year cliff, then monthly or quarterly. Some companies (Meta, Amazon) use 5-year back-loaded schedules. Taxable as ordinary income at vest.

**ISOs (Incentive Stock Options).** Private-company standard. You receive the right to buy shares at a strike price (set at FMV at grant). Standard: 4 years, 1-year cliff, monthly vesting after. Exercise creates AMT liability. You buy the stock; you don't own it for free.

**NSOs (Non-Qualified Stock Options).** Like ISOs but taxed differently (ordinary income on exercise, not AMT). Common for non-employees or large grants exceeding the ISO $100K/year limit.

**The math that matters:**

- Public-company RSU offer: the dollar value at offer time is real (with stock price variance). $80K/year RSU = roughly $80K/year of real comp, adjusting for tax.
- Private-company option offer: the dollar value at offer time is theoretical. $200K of options at a Series C valuation might be worth $0 (company fails), $200K (modest exit), or $2M (big exit). Most are closer to $0 than to $2M. Plan accordingly.
- Strike price matters. A grant of 10,000 options at $1 strike when 409A is $10 is worth $90K paper value. The same 10,000 options at $9 strike is worth $10K paper.

**Refresh grants.** At public tech companies, expect a refresh grant in year 2 or 3 (Meta, Google, Amazon, etc.). The refresh is typically 25-50% of the initial grant, also 4-year vest. This means your second-year comp from the same job is usually higher than year one. Recruiters often don't volunteer this; ask.

**IPO/acquisition mechanics.** Most private-company equity is illiquid until an exit. Tender offers (secondary sales) sometimes provide partial liquidity at Series D+. After IPO, there's typically a 6-month lockup. After acquisition, options either accelerate (if there's a single-trigger or double-trigger acceleration clause) or convert to acquirer equity on the same vesting schedule.

## Sign-On Bonuses and Clawbacks

Sign-on bonuses are a recruiter's flex tool. They:

- Don't affect the comp band (one-time payment)
- Don't require comp committee approval up to a ceiling (usually $20K-$50K)
- Are easy to give as a closer

**Clawbacks.** Most sign-on bonuses come with a clawback clause: if you leave within 12 months, you repay 100% (sometimes pro-rated). Read the clause. Public-company clawbacks are usually 1 year at 100%. Some startups push to 2 years pro-rated. Negotiate this down if it's onerous.

## Sales Comp: OTE, Splits, Accelerators, Ramp

For the operator's Track 1 (AE), the structure differs from IC comp.

**OTE (On-Target Earnings).** Base + variable assuming 100% quota attainment. A $200K OTE role might be $120K base + $80K variable.

**Common splits:**
- 50/50 (base/variable), aggressive, common at SMB/Mid-Market
- 60/40, most common at Enterprise
- 70/30, common for Senior AEs or longer sales cycles
- 80/20, usually overlay/specialist roles, not direct quota

**Accelerators.** Above 100% attainment, variable pay accelerates. Typical: 1.5x payout for 100-120%, 2x for 120%+. Some companies cap at 200%; some don't cap.

**Ramp.** New AEs get a guaranteed variable for the first 1-2 quarters while pipeline builds. Standard ramp: full variable in Q1 regardless of attainment, then 50-75% guarantee in Q2, then full quota in Q3. **Ramp is highly negotiable.** A 2-quarter ramp at 100% is meaningfully better than 1 quarter.

**Quota.** Negotiable in some companies, not in others. Hard for an incoming AE to push quota down, but possible to negotiate territory, segment, or pipeline coverage at start.

## How Offers Get Approved Internally

Useful to know because it explains the timing and the flex.

1. **Recruiter drafts offer** in the comp tool (CompTool, Pave, custom internal tool).
2. **HM approves** the offer recommendation.
3. **Comp/HRBP review** if anything is non-standard.
4. **Skip-level / VP approval** for offers in band. usually fast.
5. **Comp committee or CFO approval** for offers above band or with unusual terms.
6. **Offer letter generated** and sent.

Standard offers (within band, standard sign-on, standard equity) move in 24-48 hours. Above-band offers take 3-7 business days. If the recruiter says "let me check on that and get back to you tomorrow," they're escalating, not stalling.

## What Flexibility Recruiters Actually Have

Approximate, varies by company:

- **Base salary:** can flex within band without escalation (often a $5-15K range). Above band requires comp committee.
- **Equity:** more flexibility than base, often 25-50% upside without escalation at startups.
- **Sign-on bonus:** big flex tool, usually $5K-$50K available without escalation.
- **Start date:** highly flexible, usually no approval needed.
- **PTO / remote / title:** varies. Title can be a free flex ("Senior" vs "Sr Manager") that doesn't cost anything.
- **Severance / equity acceleration clauses:** legal review required, more friction.

The "right" things to ask for, in order of friction:

1. Higher base (within band, easy ask)
2. More sign-on (easy ask, no band issue)
3. More equity (medium ask)
4. Above-band base (hard ask, needs justification)
5. Equity acceleration on change-of-control (hard at large companies, common at startups)

## Negotiating Without Burning Rapport

The thing nobody tells candidates: recruiters expect you to negotiate. They will not be offended. They will, however, remember candidates who negotiate badly.

**Bad negotiation:**
- Aggressive demands without justification
- Comparing the offer to other companies in a way that sounds entitled
- Threatening to walk repeatedly
- Going dark and then re-engaging with new asks
- Asking for everything at once with no priority signal

**Good negotiation:**
- Specific asks with specific reasoning
- "I'm excited about the role. There are a few things I'd like to discuss before I can sign. Can we talk through them?"
- Prioritized list: "Most important to me is base. After that, equity. I'm flexible on sign-on."
- Quick response times (24-48 hours, not a week of silence)
- Honest framing: "I have another offer at $X. I prefer this role but need to bridge that gap."

## What This Means for the Operator

- An example total-comp band (e.g. $150k–$190k) spans roughly L4 base to L5+ total at most B2B SaaS. A clean anchor in early conversations is "I'm targeting total comp in an example range with flexibility on base/equity split depending on stage of company." This positions the candidate in the middle of the band, which gives room to negotiate up. (Treat the dollar figures as illustrative examples, not a real preference.)
- For Track 4 (AI-native), expect comp at the top of band at frontier-AI companies and middle of band at less-funded AI startups. Equity is the variable that will swing the most. A Series B AI company's options package is much higher variance than a public-company RSU package.
- For Track 1 (AE), the OTE math matters more than base. A $130K base + $130K variable role at a company with strong ramp and reasonable territory beats a $160K base + $40K variable role with no ramp every time, assuming the candidate can hit quota.
- A remote-first preference means weighting roles whose variable is not heavily tied to in-person customer activity (booth time, customer site visits as part of quota).
- The "what's your current comp" question: salary-history bans vary by state and city, so confirm the specific jurisdiction rather than assuming a protection. The cleaner move is to deflect with the "I'd like to understand the role first" framing rather than relying on a legal protection.
- Sign-on bonuses are the easiest flex to ask for at offer stage. Even $10K of sign-on is meaningful and costs the recruiter nothing in band exception.
