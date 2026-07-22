---
type: project
summary: Alice, a from-scratch autonomous job-search agent (raw-HTTP LLM harness, safety guardrails, LLMOps observability). The operator persona's second production agent system and the strongest "trustworthy autonomous agent" evidence. Synthetic example source.
role_relevance:
  - builder
  - ml
  - applied-ai
  - applied-ai-engineer
  - fde
  - implementation
  - founding-ae
---

<!-- Synthetic example data — a fictional engineer (Jordan Avery), used to demonstrate the retrieval engine. Not anyone's real history. -->

<!--
Synthetic example source. Beats are fictional and illustrative, written to match the
schema and exercise the pipeline. Cross-attribution guard: Alice is NOT Cadence
Analytics. This file holds Alice-agent facts only. Use carefully in materials: Alice
is "the agent helping run this search," which is itself a credible agent-engineering
portfolio piece, but never imply it is a commercial product. Numbers are round example
values, not real metrics.
-->

# Alice (autonomous job-search agent)

<!-- sourcebound:purpose -->
Use this record when the retrieval pipeline needs fictional evidence about the Alice agent's design, safety gates, tests, or observability. It keeps those claims separate from commercial-product experience so writers can select technical examples without implying customers or revenue.
<!-- sourcebound:end purpose -->

## Canonical framing

<!-- FRAMING-LOCK -->

- **Alice is a from-scratch, production autonomous agent that the operator persona designed, built, and operates: a raw-HTTP LLM harness with a tool-calling loop, layered safety guardrails, and real LLMOps observability.** It is the cleanest evidence of *trustworthy* agent engineering: not just "I called an LLM," but "I built the gates, verification, and tracing that let an autonomous agent take real actions safely." Use for AI-engineer, agent-infrastructure, AI-safety-adjacent, and any "build agents we can trust in production" role.
- **The scarce thing: the persona builds the guardrails most people skip.** Fail-loud action gates, a pre-push code-integrity gate, and manual observability spans on a raw-HTTP client (where auto-instrumentation does not exist). This is senior agent-platform thinking, demonstrated, not described.
- Never render Alice as a commercial product or claim users/revenue. It is the persona's own operating agent.
- **MATCH THE READER (do not over-apply).** Same rule as Cadence Analytics: lead with Alice only on BUILDER / AI-engineer / agent-infrastructure / AI-safety lanes. On GTM / sales / CS / RevOps / AE lanes, Alice is supporting credibility ("I build agents you can trust in production"), NOT the headline, and its internals (guards.py, OTel spans, AST pre-push gate) should not be detailed for a non-technical reader. Never lead a non-technical role with Alice's architecture.

## Beats

### Technical dimensions (projects/startups)

#### Agent design
- BUILT — Multi-step orchestrator: a daily pipeline of sequential steps (source, score, triage, prep materials, outreach, debrief) plus a long-running Telegram bot daemon, scheduled by launchd. Use case: agent orchestration, autonomous workflows.
- BUILT — Tool registry + guard-hooked dispatch: ~15 registered tools (read_sheet, read/list/write_file, focus mutations, observations) with per-tool guard hooks at the dispatch site. Use case: tool-calling agent design.
- BUILT — Structure-over-keywords scoring engine: a versioned TOML fit model (binary gates, multi-select seniority, weighted dimensions, semantic "domain worlds") consumed by an LLM judge that runs only on cheap-gate survivors, with a deliberate "label never reaches the prompt" guard against keyword-matching drift. Use case: LLM-judge / eval design, semantic classification.

#### Agent build
- BUILT — Raw-`urllib` LLM client (no SDK): hand-built tool-use/tool-result loop, three-tier model + thinking-budget routing across cheap/standard/strong tiers, per-task model pinning, full cost accounting. Use case: provider-agnostic LLM engineering, cost-aware routing.

#### Agent test
- BUILT — ~100 pytest test functions across the suite, including a few dozen safety-invariant tests exercising the guardrails and a recall-benchmark fixture for the scoring model. Use case: testing discipline for autonomous agents.

#### Agent monitor
- BUILT — LLMOps observability: OpenTelemetry manual spans opened at each `llm.call` (provider, model, tokens, cost, run/session IDs, tool round-trips), self-disabling unless tracing is enabled, with an on-demand collector. Plus error monitoring with cron monitors. Instrumenting a raw-HTTP client by hand is the senior version of observability. Use case: LLMOps, AI observability engineering.

#### Systems / infrastructure / security
- BUILT — Safety guardrails (`guards.py`): five fail-loud gates raising on violation (no-git-push with a real git-argv parser, no-irreversible-delete of protected paths, no-arbitrary-shell, no-self-edit of behavioral files, write-path allowlist). Designed "fail loud, never log-and-skip." Use case: agent safety / guardrails / safe autonomy.
- BUILT — Pre-push code-integrity gate: catches the "commit that does not contain its own dependencies" bug class by running an AST import-walk plus pytest against a fresh `git archive` checkout of the to-be-pushed HEAD. Use case: release engineering, CI design, static analysis.
- BUILT — State-grounding invariant + verification surfaces: every action type has an independent verifier (IMAP Sent-probe, fresh-auth sheet re-read, Telegram server message-id, file stat+re-read) that claims only what its check proves and fails closed when no surface exists. Use case: verification/attestation design, reliable agents.

#### Agent deploy
- BUILT — launchd daemons (daily runner, weekly review, KeepAlive Telegram bot), built around macOS Full-Disk-Access/TCC constraints; local-only state git repo with snapshot-before/after every run for rollback. Use case: production agent operations.

#### Integrations
- BUILT — Telegram bot (async, bidirectional), email SMTP/IMAP (digest-gated email discipline), Google Sheets as the live pipeline ledger, and ATS-API sourcing against public job-board JSON endpoints. Use case: integration engineering, GTM tooling.

## Status / honesty notes
- This is synthetic example data. Hygiene gaps to note (do not claim as strengths): no dependency manifest yet, no CI wired in Alice itself, a large single-file module. Never present the example repo state as production-polished.
