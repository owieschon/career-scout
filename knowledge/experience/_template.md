---
type: project
summary: One-line description of what this source is.
role_relevance:
  - revops
  - tam
---

<!--
This is the schema template for per-source experience-knowledge files.
Copy this template to <source>.md (e.g. cadence-analytics.md, lattice-additive.md, early-stage-startup.md)
and populate. The leading underscore on this filename marks it as schema,
not a real source — file-loaders skip files starting with `_`.

VALID `type` VALUES: project, startup, job (a source can list multiple
                     as a comma-separated string, e.g. "project,startup").

role_relevance: tags used by experience_store.retrieve_for_role to decide
                whether this source is relevant to a given prep job. Use
                the same vocabulary as _target_tags_for_role in
                prep_pipeline.py (e.g. revops, tam, applied-ai-engineer,
                fde, founding-ae, sales-engineering).

──────────────────────────────────────────────────────────────────────────
HEADER NOTES — apply to every source file in this directory.

1. Beats are RAW grounded facts, output-agnostic. Writers render resume
   bullets / cover claims / interview stories / outreach hooks FROM these
   facts. Beats themselves are NOT pre-rendered prose for one output shape.

2. Anti-confabulation guard: every beat must be grounded in actual fact
   and labeled with one of:
     BUILT          — exists in code; has shipped
     DESIGNED       — designed but not yet built
     ACTUAL         — happened (for jobs: real result)
     ASPIRATIONAL   — goal not yet achieved
   Beats labeled ASPIRATIONAL are NOT usable in submitted materials —
   the writer should never render them as claims. Nothing aspirational
   stated as real. A false beat is a documented false claim — these
   become written, submitted resume/cover material.

3. Cross-attribution guard: the per-source-file boundary IS the guard.
   This file is <source>.md — do NOT include facts from other sources.
   Alice infrastructure is not Cadence Analytics; Lattice Additive results
   are not Ironclad Industrial results. Each file holds facts for its
   source only.
──────────────────────────────────────────────────────────────────────────
-->

# <Source name>

<!-- clean-docs:purpose -->
Use this template when adding a fictional experience source to the retrieval corpus. It requires provenance, claim status, and source-specific boundaries so writers can retrieve grounded examples without turning aspirations or another source's results into submitted claims.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow near-duplicate reason="Experience records share this template's headings and guard language while each file remains the canonical home for one fictional source's distinct facts" -->
<!-- clean-docs:allow doc-length reason="The <Source name> reader path stays in one file because splitting it would separate its operating context from its verification material" -->

## Canonical framing

<!--
The 1-2 vetted core claims about this source that every prep output
renders consistently from. THIS IS THE FRAMING-LOCK.

Surfaced to the writer prompt as its OWN structural block (above the
EXPERIENCE EXTRAS block) so writers render from the same locked core
instead of each re-deriving framing and drifting.

Populated per-source from the operator's reviewed decision (e.g. for a project
source: "Source is an agent-engineering-forward project; ML is one
validated component, not the headline").

Example shape — DO NOT use literally:
  - <Source> is a [agent-engineering-forward / commercial-led / etc] [project/role]
  - The differentiated/scarce thing about <Source> is [X]
-->

(empty in template — populate per-source from reviewed framing)

## Beats

<!--
Each beat is concrete and quotable (target: "AUC 0.99" concreteness).
Label every beat with BUILT / DESIGNED / ACTUAL / ASPIRATIONAL.

A source uses ONLY the dimensions relevant to its type:
  - projects / startups → use the TECHNICAL dimensions below
  - jobs                → use the COMMERCIAL dimensions below
A source that is both (e.g. type: project,startup) may use technical
dimensions. Omit dimensions with no beats — do not leave empty sections.
-->

### Technical dimensions (projects/startups)

#### Agent design
<!-- Architecture, autonomy model, tool design, orchestration -->
(empty)

#### Agent build
<!-- What was built; stack; engineering approach -->
(empty)

#### Agent test
<!-- Validation, smoke tests, testing discipline -->
(empty)

#### Agent monitor
<!-- Production monitoring, observability, error tracking -->
(empty)

#### Agent deploy
<!-- Deployment stack, hosting, pipelines, daily recompute -->
(empty)

#### Systems / infrastructure / security
<!-- Data model, ERP/CRM integration, security posture (RLS, provenance, gating) -->
(empty)

#### ML / data science
<!-- Model work — captured as one component, not the headline -->
(empty)

#### Full-stack / product
<!-- Platform, UI, deployment surface -->
(empty)

### Commercial dimensions (jobs)

#### Accounts
<!-- Named accounts, deal sizes, customer relationships -->
(empty)

#### Results
<!-- Quantified outcomes; ACTUAL only, not aspirational -->
(empty)

#### Domain
<!-- Industry, vertical, buyer type — credibility material -->
(empty)

#### Methodology
<!-- MEDDIC / Challenger / structured-discovery — how the work was done -->
(empty)

#### Scope
<!-- Territory, account count, quota, multi-region coverage -->
(empty)
