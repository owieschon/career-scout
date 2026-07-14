---
type: project,startup
summary: Cadence Analytics, an end-to-end B2B revenue-intelligence AI system (LLM agents + full-stack + ML), used here as a synthetic example source. The proof the operator persona can build AI agents, not just advise on them.
role_relevance:
  # Technical lanes only. Cadence Analytics still RETRIEVES for every role via the
  # builder baseline in prep_pipeline._target_tags_for_role (so it is always
  # AVAILABLE as technical credibility), but it only RANKS HIGH for builder/AI
  # lanes. Deliberately NOT tagged with commercial tags (sales/ae/revenue/cs/tam/etc.)
  # so it does NOT crowd the actual GTM evidence (lattice-additive.md) out of the token
  # budget on sales/CS roles. Emphasis is governed by the framing-lock, not by
  # over-tagging.
  - builder
  - ml
  - applied-ai
  - applied-ai-engineer
  - fde
  - founding-ae
  - implementation
  - gtm-engineer
  - solutions-engineering
---

<!-- Synthetic example data — a fictional engineer (Jordan Avery), used to demonstrate the retrieval engine. Not anyone's real history. -->

<!--
Synthetic example source. Beats are fictional and illustrative, written to match
the schema in _template.md and exercise the retrieval/writer pipeline. Numbers are
deliberately round example values, not real metrics. Cross-attribution guard:
Alice-agent facts live in alice.md, Lattice Additive in lattice-additive.md, the manufacturer
analytics/domain in ironclad-industrial.md. This file holds Cadence Analytics facts only.
-->

# Cadence Analytics

<!-- clean-docs:purpose -->
Use this record when the retrieval pipeline needs fictional evidence about the Cadence Analytics agent platform. It preserves the platform's engineering and revenue-operations framing so writers can select role-relevant examples without attributing another source's results to it.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow near-duplicate reason="Experience records share the template's headings and guard language while this file remains the canonical home for Cadence Analytics fictional facts" -->

## Canonical framing

<!-- FRAMING-LOCK. Every writer renders from these, for every role, every output. -->

- **Cadence Analytics is a multi-agent revenue-operations platform: autonomous agents handle the manual revenue-ops workflows that eat reps' time, and an ML signal layer takes the guesswork out of customer and margin signals.** The agents are the product and the headline. ML is one validated component, not the lead. Cadence Analytics is NOT "a CRM rebuild," NOT "a churn-prediction tool," NOT a sales side-project. When a JD asks for someone who can "build AI agents with/for customers," "design and ship agentic systems," or "go from business problem to deployed AI," Cadence Analytics IS that evidence.
- **RENDER ORDER (do not invert): lead with the AGENTIC engineering, then full-stack delivery, then ML as a validated signal.** The differentiated, hard-to-fake, market-scarce capability is the agent work: multi-pass LLM orchestration, in-process and dual-transport MCP tool servers, multi-model routing, safety guardrails, the same agent re-implemented across three transports (plus a second production agent, Alice, see alice.md). The ML (gradient-boosting / survival / calibration with leakage discipline) is real and supporting, but it is the more commoditized and more credential-scrutinized part of the story. A code-evidence read makes ML metrics legible and tempting to headline; resist that pull. Leading with ML undersells the persona and points at the crowded, degree-gated ML-engineer lane; leading with agents points at the agent-engineering lane where the skill is scarce and where the target roles actually live.
- **MATCH THE READER (do not over-apply).** The agentic-first order above governs BUILDER / AI-engineer lanes (Applied AI Engineer, Founding Engineer, FDE, AI Solutions Engineer). For GTM / sales / CS / RevOps / AE lanes, the agentic and ML work is technical CREDIBILITY and differentiation that SUPPORTS a GTM-led narrative — it is NOT the headline. There, lead with the revenue/outcome story (lattice-additive.md, ironclad-industrial.md) and bring Cadence Analytics in as "and I can build the systems, not just sell them." Never stuff agent-engineering detail (MCP, three transports, guardrails, prompt-cache breakpoints) into a role that does not ask for it; the depth of technical detail must scale to how technical the role and the reader are. When unsure of the lane, mirror the JD's own language.
- **The scarce combination: the persona built this entire agentic stack end to end AND came from the customer/GTM side of this exact domain** (sold into manufacturing/distribution before building for it). Most people who can ship agents cannot talk to the buyer; most who can talk to the buyer cannot ship agents. This persona does both. That combination is the position, in every lane.
- **Claims discipline:** prefer the recognized technical term over plain description (see knowledge/positioning/messaging-guardrails.md). Say "transactions," never "invoices."

## Beats

### Technical dimensions (projects/startups)

#### Agent design
- BUILT — Multi-pass LLM agent that reasons over structured account state: one session per (account, time-point), sequential analysis passes preserving conversation state, hypothesis-driven prompting with on-demand evidence tools. Use case: any "agent that investigates and produces a decision" role.
- BUILT — Multi-model routing inside one session: a cheap model for pattern-reading passes, a stronger model for critique and structured prediction, swapped mid-session. Use case: cost-aware agent design, LLM cost/latency optimization.
- BUILT — Tool-use design: ~7 read-only data-layer tools plus a structured `submit_prediction` output tool, served through an in-process MCP server. Use case: tool/function-calling agent design, MCP integration.
- BUILT — Agent safety as design: a `PreToolUse` allowlist hook denies any tool outside an approved set as defense-in-depth. Use case: agent guardrails / safe autonomy.

#### Agent build
- BUILT — Same multi-pass agent implemented across THREE transports: an agent SDK (sessions, MCP tools, hooks); a raw HTTP client against an OpenAI-shaped function-calling endpoint; and a raw Messages HTTP API. Demonstrates transport-independent agent engineering, not framework dependence. Use case: "build on LLM APIs," provider-agnostic AI engineering.
- BUILT — Hand-written tool-calling loop (no high-level orchestration framework), with manual prompt-cache breakpoint management against the block limit for cost/latency, plus trajectory capture and per-call cost/token accounting. Use case: LLM cost optimization, prompt caching, fine-tuning data pipelines.
- BUILT — Built an MCP server (in-process, exposing data tools) on the Python side AND a separate dual-transport TypeScript MCP server (stdio + Streamable HTTP, JWT-authenticated, schema-validated). MCP is a current agent-integration protocol; the persona ships real servers on it. Use case: agent-platform / AI-integration roles.

#### Agent test
- BUILT — ~60-case pytest integration suite against a real ~10,000-SKU catalog fixture for the SKU resolution subsystem (see SKU beats below), covering canonical/typo/ambiguity/proprietary/voice/adversarial inputs. Use case: testing discipline for AI systems.
- BUILT — Self-built evaluation harnesses: a word-error-rate (WER) scorer with fixtures for the ASR/retrieval experiment, plus structured model comparisons. Use case: LLM/RAG eval engineering.

#### Agent monitor
- BUILT — Error monitoring wired across three surfaces: the API (Node), the Python ML layer (PII off), and the browser (RUM with source-map upload). Standing alert digest cadence. Use case: production observability for AI products.

#### Agent deploy
- BUILT — Dependency-free gradient-boosting inference re-implemented as a TypeScript tree-walker so the model runs serverless with no Python or ONNX runtime ("zero native dependencies"). A senior architecture decision: ships ML to the edge without a Python service. Use case: edge ML, serverless AI deployment.
- BUILT — CI on every push: GitHub Actions running `tsc --noEmit` + the TS test runner and pytest as parallel jobs; a daily production recompute cron on a deliberate off-peak schedule with loud-failure gating. Use case: MLOps / CI-CD for ML.
- BUILT — Edge worker in production (inbound-mail capture worker) plus serverless web deploys. Use case: edge/serverless infra.

#### Systems / infrastructure / security
- BUILT — Multi-tenant data isolation via Postgres Row-Level Security, three-tier role-scoped access (hand-written RLS policies, not an ORM convenience). Use case: secure multi-tenant SaaS, data governance.
- BUILT — Postgres schema operated through ~100 hand-managed migrations with a custom migrate runner; raw `pg` (no ORM). Use case: backend/data-platform engineering.
- BUILT — Revenue-truth architecture: the platform owns revenue computation; clients read via API and never store credentials locally; document-type model (posted_invoice / credit_memo / backlog_order) with reconciliation rules. Use case: data-integrity / financial-data systems.
- BUILT — Bidirectional email logging via reply-all proxy, a 3-phase architecture toward M365/Google Workspace integration. Use case: GTM/CRM data integration.

#### ML / data science
- BUILT — Production churn/retention model: gradient-boosting, ~40 features, isotonic-calibrated probabilities, served via the TS tree-walker. Reported with a crossval catch-rate as the honest headline metric, not a single AUC. Use case: applied ML, churn/retention modeling, revenue intelligence.
- BUILT — Survival analysis for time-to-churn: a random-survival-forest fit with Cox PH and Kaplan-Meier baselines; survival probabilities (90/180/365d) written to the predictions table and surfaced in the UI. Use case: survival/time-to-event modeling.
- BUILT — Per-cluster anomaly detection: an isolation-forest fit per behavioral cluster on real production data, scores written to the predictions table and consumed by the cue-card layer. Honest scope: it runs as a manually-triggered batch (self-labeled "prototype"), not yet a standing automated pipeline. Use case: anomaly detection on real data.
- BUILT — ML validation rigor: temporal cross-validation (TimeSeriesSplit) and a dedicated leakage-diagnostics workflow (feature-vs-target correlation thresholds, independence tests, ablation studies). The leakage discipline is the standout: most practitioners skip it. Use case: trustworthy ML, model validation.
- BUILT — KMeans behavioral segmentation feeding ~30 feature extractors (recency/frequency/monetary, trend acceleration, order regularity, seasonal deviation, dormancy/cliff-drop, product concentration, territory risk) over a DuckDB feature pipeline. Use case: feature engineering, customer segmentation.

#### RAG / retrieval
- BUILT — Hybrid retrieval: sparse BM25 unioned with dense embeddings (a small sentence-transformer, normalized cosine), fused via Reciprocal Rank Fusion, then a reranker and LLM-mediated final selection. Built to match free-form call/email mentions to catalog SKUs; the same stack is general-purpose RAG/search/entity-matching. Iterated from additive-boost to RRF (shows real iteration). Use case: RAG engineering, semantic search, entity resolution.

#### SKU resolution engine (Cadence Analytics subsystem)
- BUILT — A multi-module Python engine that resolves free-form rep/voice input into canonical manufacturer part numbers over a ~10,000-active-SKU catalog. The under-titled, high-value capability: it has no clean job title but it is exactly what distribution-AI / part-search companies sell.
- BUILT — SKU decoding via a hand-authored regex grammar: a few hundred compiled patterns in an ordered dispatch table; invertible (a constructor rebuilds SKUs so parse(construct(extract(x))) == x). Use case: domain-specific parsing, information extraction.
- BUILT — Matching engine: custom-implemented Levenshtein (rolling-row DP, early exit) in a multi-tier cascade with family-prefix candidate bucketing; rule-based disambiguation scoring field-matches + log-scaled sales popularity + recency, with a calibrated high/medium/low confidence gradient and a hard "never invent a SKU" guarantee. Use case: fuzzy matching, entity resolution, record linkage.
- BUILT — Voice/free-text front-end (`normalizer.py`): NATO-phonetic decoding, spoken-number/fraction parsing, unit handling. Rules-based, no embeddings. Use case: voice ingestion, NL-to-structured parsing.

#### Full-stack / product
- BUILT — React + Vite + TypeScript SPA, TypeScript-first (CI gates on `tsc`), with a hand-built `components/ui/` design system, hand-rolled raw-SVG charting (Catmull-Rom bezier paths, CSS-variable theming), and a CSS keyframe animation system. No high-level UI/chart libraries: the capability is in the CSS/SVG/React fundamentals. Use case: senior frontend / full-stack product engineering.
- BUILT — Telephony: a CPaaS integration with caller-ID native validation, call-XML generation, webhook-signature verification with tests, and a full calling UI. Use case: comms/CPaaS integration, click-to-call product surfaces.
- BUILT — Express API with security middleware (helmet, rate-limit, JWT, bcrypt); transactional email. Use case: backend product engineering.

## Status / honesty notes
<!-- Synthetic example. Keep the writer honest within the example; never render aspirational beats as real. -->
- ASPIRATIONAL (do NOT render as real): paying customers, revenue, named design partners. Cadence Analytics is, in this example, pre-revenue; the manufacturer-employer is the problem-source, not a customer.
- Scope-bounded (render precisely if mentioned): the SKU engine resolves text to canonical SKU; it does NOT do competitor-to-internal cross-referencing, durable learned-mapping persistence (in-memory only), or end-to-end quote/order placement. Anomaly detection is a manually-run batch, not an automated pipeline.
