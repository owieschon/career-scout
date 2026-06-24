# Recall benchmark — label template (alc-7em)

The recall benchmark (`scripts/recall_benchmark.py`) replays the full sourcing +
gating pipeline (and, with `ALICE_FIT_JUDGE=1`, the fit-judge) over a held set of
roles **you have confirmed are correctly classified**, and reports what fraction
survive to the right outcome. It is the number that tells whether gate/judge fixes
actually move recall or just shuffle noise.

**These labels are your HITL ground truth — not LLM-generated.** To add a role,
append one JSON object per line to `tests/fixtures/recall_benchmark.jsonl`.
Current set: 43 cases; target ~50. Use **verbatim** JD bodies (paste the real
posting text — the benchmark guards against fabricated/empty bodies).

## Fields

| field | what | example |
|---|---|---|
| `id` | unique, `recall-NNN` or `cal56-NN` | `"recall-044"` |
| `url` | the real posting URL (non-empty; fabrication guard rejects empty) | `"https://job-boards.greenhouse.io/acme/jobs/123"` |
| `source` | board tag: `gh:slug` / `ashby:slug` / `lever:slug` / aggregator name | `"gh:acme"` |
| `jd_snapshot.title` | exact role title | `"Senior Solutions Engineer"` |
| `jd_snapshot.body` | **VERBATIM** JD text (requirements, comp, remote/travel language) | `"Acme is ... Fully remote (US). $140-180K. No travel."` |
| `jd_snapshot.location` | as posted | `"Remote"` or `"Columbus, OH"` |
| `jd_snapshot.comp_low` / `comp_high` | integers or `null` | `140000` / `null` |
| `jd_snapshot.snapshot_date` | date you captured it | `"2026-06-02"` |
| `expected_verdict` | **your** label: `FIT` \| `REACH` \| `NOT-FIT` | `"FIT"` |
| `expected_reason` | the driver: `domain_fit` \| `functional_fit` \| `geography_ambiguous` \| `comp` \| `travel_gate` \| `seniority` \| `stale_or_nonrole` | `"domain_fit"` |
| `provenance_note` | one line: why you labeled it this way | `"On-domain industrial SE, remote-US, comp in band."` |

## Filled example (copy this shape)

```json
{"id":"recall-044","url":"https://job-boards.greenhouse.io/acme/jobs/123","source":"gh:acme","jd_snapshot":{"title":"Senior Solutions Engineer","body":"Acme builds industrial IoT for manufacturers. Seeking a Senior SE to own technical pre-sales and customer architecture. 5+ yrs SE/SA. Fully remote, US-eligible. Compensation $150,000-$190,000 + equity. No travel required.","location":"Remote","comp_low":150000,"comp_high":190000,"snapshot_date":"2026-06-02"},"expected_verdict":"FIT","expected_reason":"domain_fit","provenance_note":"On-domain (industrial IoT), remote-US, comp in band, no travel — clean FIT."}
```

## Blank to fill (one per line, append to recall_benchmark.jsonl)

```json
{"id":"recall-0NN","url":"","source":"","jd_snapshot":{"title":"","body":"","location":"","comp_low":null,"comp_high":null,"snapshot_date":"2026-06-02"},"expected_verdict":"","expected_reason":"","provenance_note":""}
```

After you append rows, run: `ALICE_FIT_JUDGE=1 python3 scripts/recall_benchmark.py`
(or tell me and I'll run it + report the recall number).
