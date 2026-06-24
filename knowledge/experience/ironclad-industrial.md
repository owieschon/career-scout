---
type: job
summary: Ironclad Industrial, a heavy-duty industrial parts manufacturer, where the operator persona ran the revenue analytics that the company acted on and that became the problem-source for Cadence Analytics. Holds the manufacturing/distribution domain credibility. Synthetic example source.
role_relevance:
  - ml
  - applied-ai
  - builder
  - revops
  - forecasting
  - retention
  - revenue
  - expansion
  - cs
  - tam
  - implementation
  - sales-ops
---

<!-- Synthetic example data — a fictional engineer (Jordan Avery), used to demonstrate the retrieval engine. Not anyone's real history. -->

<!--
Synthetic example source (job). Beats are fictional and illustrative work results in
an operating analytics role at Ironclad Industrial, on Ironclad's own data. They are
NOT a Cadence Analytics customer engagement. The productionized ML/agent system built
afterward lives in cadence-analytics.md. This file holds the manufacturer-side analytics + domain
facts only. Numbers are deliberately round example values, not real metrics.
-->

# Ironclad Industrial (heavy-duty industrial parts manufacturer) — operating role

## Canonical framing

<!-- FRAMING-LOCK -->

- **Framing rule (no customer claim):** Ironclad Industrial is the persona's operating-role employer AND the problem-source / inspiration / early validator for Cadence Analytics. It is NEVER a Cadence Analytics customer, tenant, pilot, design partner, or reference account. Do not write "Cadence Analytics pilot" or "Cadence Analytics engagement." The honest line is: "In my operating role at Ironclad Industrial I ran the analysis that became the foundation for Cadence Analytics."
- **Wording rule:** the dataset is **transactions** (posted_invoice / credit_memo / backlog_order document types), NOT "invoices." Say "transactions," never "invoices."
- **Why this source matters: it is the domain credibility.** The persona's manufacturing/distribution/ERP-install-base credibility (knows the buyers, the data, the ERPs) is what makes the persona uncommon among AI builders. For vertical-AI roles in manufacturing/distribution/supply-chain/RevOps, this is a double-fit with the builder evidence in cadence-analytics.md. Frame the domain as the differentiator, not a liability to translate away.

## Beats

### Commercial dimensions (jobs)

#### Results
- ACTUAL — Ran a DuckDB analysis across roughly 200,000 transactions (a decade of history) and surfaced about $800K in churned revenue, with the top revenue risks ranked for action. Use case: revenue analytics, churn/retention analysis, data-driven decision support.
- ACTUAL — Produced a strategic revenue plan that company leadership adopted. The point is the adoption, not the slide: leadership acted on the recommendation. Use case: business-impact / outcome-attribution evidence.
- ACTUAL — Mapped a few hundred buying-group transitions and quantified roughly $2M in recoverable revenue; built dead-account intelligence over ~1,500 lapsed accounts (the majority still in business, buying from competitors). Use case: GTM analytics, territory/segment strategy.

#### Domain
- ACTUAL — Heavy-duty industrial parts / aftermarket: distributors and shops, territory reps, SKU-level parts catalogs, ERP-driven order data. The persona operated inside this data and these buyer relationships. Use case: manufacturing/distribution/industrial domain credibility.
- ACTUAL — Working knowledge of the distribution ERP install base Cadence Analytics targets (Eclipse, Prophet 21 / P21, NAV-class systems) and how transaction data is shaped by ERP schema. Use case: ERP-adjacent AI, vertical-AI for distribution. (Knowledge/schema-literacy, NOT a claim of having integrated against these ERPs' APIs.)

#### Methodology
- ACTUAL — DuckDB-on-ERP-exports analytics pipeline: ingest BI / ERP transaction exports, model document types, reconcile, compute revenue truth, surface ranked risks. Use case: analytics engineering on real business data.

#### Scope
- ACTUAL — Company-wide revenue dataset (roughly 3,000 accounts, a decade of history). End-to-end analytical work owned by the persona, leadership-facing.

## Status / honesty notes
<!-- Synthetic example. The figures are illustrative round numbers, not real metrics. -->
- This is synthetic example data analyzed in the persona's operating role. Never imply a vendor/customer relationship with Cadence Analytics.
- ERP claims are end-user + data + schema literacy, NOT integrated-against-API. The hedged "ERP familiarity / schema literacy" phrasing is the honest one.
