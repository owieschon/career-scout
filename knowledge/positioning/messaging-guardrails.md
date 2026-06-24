Claims ledger and messaging guardrails: the rename-up table (describe hard work by its recognized name) and the banned-claims list (over-claims and framing violations to never render). Grounded in a code-evidence audit.

# Messaging Guardrails

Companion to `positioning-spine.md`. Full evidence with file:line is in the capability-inventory evidence doc. This file is the operational do/don't every writer checks before rendering a claim.

> Note: the operator persona ("Jordan Avery") is a fictional engineer used to demonstrate the engine. Not anyone's real history.

## Evidence-tier discipline

Every claim must map to a tier. Only PROVEN/ACTUAL claims go into submitted materials.
- **PROVEN / BUILT** — exists in code on disk, traced to file:line. Render freely.
- **ACTUAL** — a real result that happened (commercial outcomes). Render freely.
- **SUGGESTED** — referenced/configured, not clearly hands-on. Render only with hedge ("familiarity with"), never as hands-on.
- **ABSENT / ASPIRATIONAL** — not found, or a goal not achieved. **Never render as real.**

## DO — rename hard work up to its recognized term

The persona systematically under-describes senior work in junior language. Use the right-hand column.

| What the persona built (plain) | Render as (recognized term) | Tier |
|---|---|---|
| Hand-rolled multi-pass LLM loop across 3 transports | **Custom agent orchestration; provider-agnostic LLM engineering** | PROVEN |
| Built an MCP server (Python in-process + TS dual-transport) | **MCP server engineering (a current agent-integration protocol)** | PROVEN |
| Manual `cache_control` breakpoint management | **LLM cost/latency optimization via prompt caching** | PROVEN |
| `guards.py` fail-loud gates + git-argv parser | **Agent safety guardrails / safe-autonomy engineering** | PROVEN |
| AST-walk + pytest against fresh `git archive` checkout | **Pre-push code-integrity gate (release engineering, static analysis)** | PROVEN |
| Manual OTel/Phoenix spans on a raw-HTTP LLM client | **LLMOps observability instrumentation** | PROVEN |
| Hand-written Postgres RLS, three-tier | **Multi-tenant data isolation via Row-Level Security** | PROVEN |
| XGBoost inference re-implemented in TypeScript | **Dependency-free edge/serverless ML inference** | PROVEN |
| Custom Levenshtein + 315-pattern regex grammar | **Entity resolution / fuzzy matching engine; domain-specific parsing** | PROVEN |
| BM25 + MiniLM dense + RRF | **Hybrid retrieval (RAG) with Reciprocal Rank Fusion** | PROVEN |
| `sksurv` RandomSurvivalForest + lifelines Cox | **Survival / time-to-event modeling** | PROVEN |
| correlation + chi-square + ablation feature audit | **Data-leakage detection; model-validation rigor** | PROVEN |
| isotonic-calibrated probabilities | **Probability calibration** | PROVEN |
| `fit_judge` semantic-worlds scoring | **LLM-judge / eval design resistant to keyword-matching bias** | PROVEN |

## DO — the claim-hard list (lead with these)

1. **Two production AI agent systems built end to end** (Cadence Analytics platform agents + Alice), with orchestration, MCP tools, guardrails, and observability.
2. **Full-stack TypeScript + Python system**, CI-gated, multi-tenant Postgres/RLS, edge ML inference.
3. **Churn/retention ML with leakage discipline** — strong AUC / crossval catch-rate, survival analysis, calibration. (Render as supporting, per agentic-first rule.)
4. **Hybrid RAG** (BM25 + dense + RRF) with a self-built eval harness.
5. **The SKU resolution engine** — entity resolution over a ~10K-SKU catalog (the under-titled moat; describe with the screenable one-liner in the capability inventory).
6. **The GTM/customer half** — Lattice Additive executive-outcome loop, eight-figure new contracts, a major aerospace OEM and a defense prime.

## DON'T — banned claims and framing violations

Never render any of these. They are over-claims, framing-rule violations, or unverified.

1. **"Bayesian human-input update layer for credibility-weighted predictions."** RETIRED — no such code exists; the real HITL module explicitly never overrides predictions. This is theater. If feedback/HITL comes up, say: "HITL label-capture and model-accuracy tracking that builds a ground-truth corpus for retraining."
2. **"invoices."** The dataset is **transactions** (posted_invoice/credit_memo/backlog_order). Say "transactions," never "invoices."
3. **The manufacturer-employer as a Cadence Analytics customer/tenant/pilot/design partner/engagement/reference account.** It is the persona's operating-role employer and the platform's problem-source. Never "platform pilot," "rebuilt the CRM for the customer," "platform engagement." Refer to it generically as "a heavy-duty industrial parts manufacturer."
4. **Naming the manufacturer-employer.** Always generic: "a heavy-duty industrial parts manufacturer."
5. **Unconfirmed precise revenue/lead-time figures.** Do not render specific dollar/lead-time figures that are not grounded in the persona's evidence. Use rounded illustrative figures only.
6. **Customer dashboards as "built."** The verb drifts between resume variants ("repurposed" vs "built"). Use "instrumented outcome metrics using Kibana/Grafana" rather than overclaiming authorship.
7. **Bare ERP experience ("NetSuite / Epicor / P21 / Sage 500") as hands-on integration.** Evidence is end-user + data-export ingestion + schema literacy, NOT API integration. Render as "ERP familiarity / schema literacy," never as "integrated against NetSuite/Epicor."
8. **Anomaly detection as a "standing production system."** It is a manually-triggered batch (the file self-labels "prototype"). Render as "per-cluster IsolationForest on production data feeding cue cards," not "automated anomaly pipeline."
9. **SHAP / multi-label churn as production.** They live in experiments; the production model is binary XGBoost with gain/permutation importance. Do not claim SHAP-in-production.
10. **"CRM rebuild (React + Supabase)" as the answer to "build AI agents."** This is THE recurring inversion. The answer to "build AI agents with/for customers" is Cadence Analytics' agent layer (MCP tools, multi-pass orchestration, guardrails), not the CRM surface.
11. **Cadence Analytics paying customers / revenue / users.** Pre-revenue. Never imply otherwise.
12. **Skills the audit marked ABSENT** (do not list): PyTorch-at-scale, PyMC/Bayesian, Optuna, LangChain/LangGraph/Relay Agents, FAISS/Pinecone/pgvector, OpenAI/Gemini official SDKs, Loopwork/Zapier/Clay/Retool, Tableau/Power BI/Looker authoring, R, formal HPO. (Some of these can be ADDED cheaply; until then, not on the list. See the capability inventory's gap section.)

## Resume ↔ code mismatches already flagged (resolve, don't paper over)

- Title spread (Manufacturing Tech Consultant ... Customer-facing AI builder for the same work) is the headline inconsistency. The positioning spine resolves it: one identity, archetype-varied emphasis.
- An earlier draft leaned on Lattice Additive sales numbers and called Cadence Analytics a "CRM rebuild" — both the inversion (DON'T #10) and the framing risk. Regenerate against the spine.
