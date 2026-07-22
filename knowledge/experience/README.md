# `knowledge/experience/` — file-authored experience source

<!-- sourcebound:purpose -->
Use this page when adding fictional experience records to the retrieval engine. It defines how file-authored records join chat-captured records so contributors can add examples without mixing sources or presenting synthetic history as real.
<!-- sourcebound:end purpose -->


This directory holds **file-authored experience-knowledge** about the
operator persona's work history (projects, startups, jobs). Files here are a second
population path into the prep pipeline's `experience` ground source —
alongside the existing chat-capture path in `feedback/experience-store.jsonl`.

Both paths feed the same first-class `experience` source class that the
GROUND stage's writers and verifier already consume. See
`src/alice/persistence/experience_store.py` for the union-at-retrieve logic.

## Two integrity models — different mechanisms, same guarantee

The `experience` source has TWO population paths, with TWO different
integrity mechanisms appropriate to their inputs:

| Population path | Integrity mechanism | Enforcement location |
|---|---|---|
| Chat-capture (`experience-store.jsonl`) | Verbatim-substring of an operator turn in `telegram-history.jsonl` | At capture (`flag_experience_candidate` raises if substring check fails) |
| File-authored (this directory) | Write-access discipline — only the operator authors these files | At file-write (who can write here), NOT at parse |

Both guarantee **integrity at the source so downstream consumers can
trust the experience source uniformly.** The mechanisms differ because:

- Chat-capture entries CAN be mechanically verified at parse time (is
  this string a substring of a chat turn? yes/no — the parser checks).
- File-authored entries CANNOT be mechanically verified at parse time
  (the parser cannot verify "did the operator review this?"). So the integrity
  boundary lives at the **file-write step**: only the operator writes files
  here, and the file's presence in this directory IS the review evidence.

**For future maintainers:** do NOT add a substring-check or other
mechanical integrity verifier at the parse step for file-authored
entries. That's not the right boundary. The boundary is write-access
discipline (only the operator authors here). Adding a parse-time check would either reject valid
file-authored content or give a false sense of mechanical guarantee
where the real guarantee is human discipline.

## File structure

Each source gets one file: `<source>.md` (e.g. `cadence-analytics.md`, `lattice-additive.md`).

Follow `_template.md` for the schema. The schema has four parts:

1. **YAML frontmatter:** `type`, `summary`, `role_relevance` (tags for retrieval).
2. **Canonical framing:** 1-2 vetted core claims that surface to writers
   as their own structural block. THIS IS THE FRAMING-LOCK — every
   writer renders from these consistently.
3. **Beats:** organized by dimension, each labeled
   BUILT / DESIGNED / ACTUAL / ASPIRATIONAL. Raw facts, output-agnostic.
4. **Filename conventions:**
   - `<source>.md` — a real source file (e.g. `cadence-analytics.md`)
   - `_<anything>.md` — schema / docs / test files (skipped by the loader)
   - `README.md` — this file (skipped by the loader)

## How the loader uses these files

`experience_store.retrieve_for_role(target_tags=...)` reads
`knowledge/experience/*.md` lazily at retrieve time (no caching, no
write-time ingest, no parallel store — the file IS canonical for its
content; the JSONL is canonical for chat-captures; both are unioned at
read).

Filtering: only files whose `role_relevance` tags intersect with
`target_tags` are included. Empty `target_tags` matches all.

The canonical_framing surfaces to writers as its own block ABOVE
the EXPERIENCE EXTRAS block (the framing-lock — guaranteed to reach
every writer as the framing they render from, not a beat competing
for inclusion). Beats flow into EXPERIENCE EXTRAS as usual.

The Stage 3 verifier picks up file-backed entries through the same
`experience` attribution slot as JSONL-backed entries — no separate
source class.

## Build status

- `_template.md` — schema definition (this file's sibling)
- `README.md` — this file (integrity gate documentation)
- Populated with synthetic example sources (`cadence-analytics.md`,
  `lattice-additive.md`, `ironclad-industrial.md`, `alice.md`) for a
  fictional engineer; the content demonstrates the schema and retrieval
  engine and is not anyone's real history.
