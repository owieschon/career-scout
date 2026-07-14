# Alice's Recruiting Knowledge Base

<!-- clean-docs:purpose -->
Alice is the operator persona's job-search assistant. To advise the operator well, she needs insider knowledge about how hiring actually works from the employer side, recruiters, ATSes, hiring managers, comp, referrals, AI screening, and the current market. Read this page before changing or relying on Alice's Recruiting Knowledge Base so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->


This directory is that knowledge base.

## Structure

```
knowledge/
  README.md                : this file
  sources.md               : every external source attempted, with access status
  recruiting-ops/          : how recruiters source, screen, prioritize
  ats-systems/             : how ATS/CRM parsing, ranking, knockouts work
  interview-methodology/   : structured interviewing, scorecards, panels
  comp-negotiation/        : bands, leveling, offer mechanics from employer side
  hiring-manager-lens/     : what HMs really evaluate vs the posted requirements
  referral-dynamics/       : internal referrals, bonuses, tracking
  ai-screening/            : HireVue, Paradox, Phenom, AI resume parsers
  market-intelligence/     : 2024-2026 hiring market, remote vs hybrid, comp trends
```

## How Alice uses it

`scripts/llm.py::load_alice_brief()` is augmented by `load_knowledge_index()` which assembles a one-page index of every document under `knowledge/`. The index is appended to Alice's system prompt for every Telegram chat call so Alice knows what knowledge she has and can cite it when advising the operator.

When Alice needs deep content on a topic mid-conversation, she can reference the specific file path in her response. (Future enhancement: lazy-load specific files on demand.)

## How to maintain it

- Add new knowledge files as plain Markdown inside the right subdirectory.
- Each file should open with a one-line summary (used in the index).
- After adding or updating files, run `python3 scripts/build_knowledge_index.py` (if/when it exists), until then the loader walks the tree at call time.
- Sources that are blocked, paywalled, or require credentials go in `sources.md` with a note about what the operator would need to provide to unblock them.

## Tone

These documents are written for Alice, who advises the operator persona. They should read like notes from an experienced talent partner: specific, opinionated where the field has a clear answer, careful where it doesn't. No marketing fluff. No corporate hedging. Insider perspective.
