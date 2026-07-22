# Alice's Recruiting Knowledge Base

<!-- sourcebound:purpose -->
Use this index to inspect the recruiting-knowledge inputs available to Alice and their evidence status; uncited market and vendor pages are hypothesis notebooks, not verified facts.
<!-- sourcebound:end purpose -->


> **Evidence status:** [`sources.md`](sources.md) records no completed source
> extraction for the current recruiting and market pages. Until a claim has a
> source URL and access date, treat it as an unverified hypothesis. Synthetic
> persona and experience files are configuration examples, not real history.


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

`src/alice/llm/llm.py::load_alice_brief()` calls
`_build_knowledge_index()`, which assembles a one-page index of the Markdown
documents under `knowledge/`. The index is appended to Alice's system prompt
for Telegram chat calls. The prompt instructs Alice to label uncited,
time-sensitive claims as unverified and cite the file path when using them.

When Alice needs deep content on a topic mid-conversation, she can reference the specific file path in her response. (Future enhancement: lazy-load specific files on demand.)

## How to maintain it

- Add new knowledge files as plain Markdown inside the right subdirectory.
- Each file should open with a one-line summary (used in the index).
- No generated index step is required; the loader walks the tree at call time.
- Bind factual external claims to a source URL and access date. Sources that are
  blocked, paywalled, or require credentials belong in `sources.md`; leave
  unsupported text labeled as hypothesis material.

## Tone

These documents are written for Alice, who advises the operator persona. Keep
verified facts separate from hypotheses, name uncertainty directly, and avoid
marketing claims or unsupported authority.
