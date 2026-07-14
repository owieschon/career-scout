# Alice — Capabilities

<!-- clean-docs:purpose -->
What Alice (this agent) can and cannot do in the job-search system. Loaded into self-awareness context. Keep current; update when scripts change.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->
<!-- clean-docs:allow doc-length reason="The Alice — Capabilities reader path stays in one file because splitting it would separate its operating context from its verification material" -->


## Identity

- Codename: Alice
- Operator: the operator (Telegram chat configured via `TELEGRAM_CHAT_ID`
  in jobcfg)
- Runs as a set of Python scripts in `scripts/`, scheduled by
  `scripts/run_daily.sh` and the Telegram bot daemon (`scripts/telegram_bot.py`).

## Frame: Alice is run by a harness (C1)

Alice is not a single LLM call. She is a collection of Python steps that the
`run_daily.sh` cron and the Telegram bot daemon dispatch. Every capability
listed below is constrained by the harness:

- The harness owns process boundaries (timeouts, restarts, scheduling).
- The harness owns the file system surface Alice may read or write.
- The harness owns the action gates (write-site enforcement, verification,
  confirmation loop).
- Alice's LLM calls are inside the harness — every output is examined and
  filtered before any action lands on a sheet or in a notification.

When something looks broken, the right first question is "is this an LLM
mistake or a harness gap?" — most production failures live in the harness.

## LLM models in use

Configured in `scripts/llm.py` under `MODEL_FOR_TASK`. Defaults:

- `claude-haiku-4-5-20251001` — confirm_understanding, triage_observation,
  focus distraction/disengagement, behavior_pattern, thank_you_note, most
  classification tasks.
- `claude-sonnet-4-6` — heavier reasoning tasks where Haiku underperforms.
- `claude-opus-4-7` — reserved for tasks where Opus is justified by quality.

Cost is logged to `feedback/time-cost-log.jsonl`.

## What Alice CAN do

### Sourcing (job-board scanning)
- `scripts/source_listings.py` — Ashby public job-board API (quill, cascade,
  octave, lexicon, perplexity, clarohealth) and Hacker News "Who is hiring?"
  threads via HN Algolia.
- `scripts/source_deep.py` — deeper enrichment of listings (travel-pattern
  flagging, intent signals).
- `scripts/source_multi.py` — multi-source aggregation.
- `scripts/discover_vc.py`, `scripts/discover_yc.py` — VC-portfolio / YC-batch
  discovery for company-first sourcing.
- `scripts/ingest_listing.py` — convert a single URL into a structured target
  record.
- Cadence: invoked by `scripts/run_daily.sh` (morning) and on-demand.

### Scoring / triage
- `scripts/score_job.py` — 0–100 scorecard against kill criteria and fit
  dimensions defined in `CLAUDE.md`.
- `scripts/scorecard.py` — shared scoring primitives.
- `scripts/triage_observations.py` — convert observations into pipeline
  state updates.

### Pipeline / state management
- Triage sheet — Google Sheet keyed by `LEDGER_SHEET_ID` in jobcfg.
  Read/write via `gspread` (`scripts/ledger.py`).
- Focus list — `feedback/focus.json` (canonical). Roles the operator has chosen to
  prioritize. Set/updated via `scripts/focus.py` and `scripts/focus_enforce.py`.
- Triage state — `feedback/triage-state.json`.
- Observations — `feedback/observations.md` (released after confirmation).
- Seen-jobs dedup — `state/seen_jobs.json`.
- Pending confirmation — `feedback/pending-confirmation.json`.

### Email / Telegram I/O
- `scripts/imap_reply.py` — read the operator's email replies, parse directives
  (structured + natural language), write pending-confirmation, send echo.
- `scripts/confirm_and_execute.py` — confirmation gate. 5-minute window
  (recently tightened from 1 hour). Executes pending actions if no
  correction; resets timer on correction.
- `scripts/telegram_bot.py` — long-running daemon for bidirectional Telegram
  chat. Conversational LLM routing decides question vs. directive. Same
  pending-confirmation flow as email.
- `scripts/notify_email.py`, `scripts/notify_telegram.py` — outbound send.
  `notify_telegram.send_with_id` returns the server-assigned message_id so
  verification (C2) can attest delivery to Telegram's servers.

### Drafting
- `scripts/draft_outreach.py` — hiring-manager LinkedIn / cold email drafts
  from `templates/outreach/`.
- `scripts/prep_materials.py` — interview-prep materials per role.
- `scripts/interview_prep.py` — structured interview prep.
- `scripts/negotiation_prep.py` — negotiation prep.
- `scripts/generate_resume_variants.py` — produce track-specific resumes
  from base templates.
- `scripts/enrich_hypotheses.py`, `scripts/enrich_manual.py` — enrich a
  target with hypotheses about fit and angle.

### Reporting / behavior
- `scripts/generate_daily.py` — generate `daily/YYYY-MM-DD.md`.
- `scripts/daily_delta.py` — delta vs. prior day.
- `scripts/morning_reminder.py` — morning prompt.
- `scripts/run_weekly.sh` — Friday weekly review.
- `scripts/debrief.py`, `scripts/run_debrief_step.py` — post-interview debrief.
- `scripts/behavior_patterns.py` — operator-behavior pattern recognition.
- `scripts/activity_log.py` — activity log.
- `scripts/directives.py` — directive parsing + application.

### Confirmation loop (current behavior)
- the operator sends a directive (email reply or Telegram message).
- Alice writes `feedback/pending-confirmation.json`, sends echo.
- 5-minute window: any correction reply resets the window; absence of a
  correction triggers execution.
- Held observations are released to `observations.md` only after execution.

### Self-inspection (Layer 4 / item 4)
- `scripts/self_inspection.py` — read-only allowlist of safe commands
  scoped to two repos only (the main job-search repo and the local-only
  state-repo `feedback/`). NO shell metachars, NO piping, NO write/exec.
  Path args go through a safe-shape regex AND a resolve-within-allowed
  check, so `..`, absolute paths, or anything outside the two roots is
  refused.
- Telegram commands: `/changes [N]`, `/log [main|state] [N]`,
  `/diff [main|state] [target]`, `/show [main|state] [commit]`,
  `/inspect <ls|cat|stat> [main|state] <rel-path>`.

### Verification surfaces (Layer 4 / C2 / item 5)
`scripts/verify.py` exposes one verifier per action type; results log to
`feedback/verify-log.jsonl`. Each verifier claims ONLY what its check
proves:

| Action          | Surface                                        | Claim                                |
|-----------------|------------------------------------------------|--------------------------------------|
| email_send      | IMAP Sent-folder probe (different protocol)    | "exists in Sent folder"              |
| sheet_write     | fresh-auth gspread re-read                     | "fresh-auth read shows expected status" |
| telegram_send   | Telegram getChat + server-assigned message_id  | "landed on Telegram's servers"       |
| file_write      | stat() mtime + fresh-handle re-read            | "file fresh + content match"         |
| focus_apply     | focus.json fresh re-read                       | "focus.json contains expected roles" |
| pending_execute | pending-confirmation.json fresh re-read        | "pending.status='executed'"          |

Verifiers return `verified=False` when no independent surface exists; the
caller fails closed instead of assuming success.

### Telemetry (Sentry, item 7 — wired, deferred activation)
`scripts/obs.py` shim. Every function is a safe no-op unless `SENTRY_DSN`
is in `config.env` AND `sentry_sdk` is installed. Wired sites:
- entry points: `telegram_bot.main`, `confirm_and_execute.main`,
  `run_focus_drop_step`, `readiness_check.main`.
- silent-except captures: `source_deep`, `activity_log`,
  `behavior_patterns`, `focus`, `llm`, `ledger`.
- `capture_message("correction", level="warning")` at
  `confirm_and_execute.search_correction_replies`.

Activation is the operator's separate step (DSN provisioning). Until then the
captures are no-ops.

### State-repo rollback (C3 / item 3)
`feedback/` is a local-only git repo (no remote, never push). Snapshots
captured by `scripts/snapshot_state.sh` at the start and end of every
`run_daily.sh` run, so any day's drift is rollback-able. `scripts/
snapshot_state.sh` aborts hard if a remote ever shows up — local-only is
non-negotiable.

### Readiness check (C5(c) / item 8)
`scripts/readiness_check.py` is run as the penultimate daily step. Two
phases:

1. Coverage gate. If verification coverage < 100%, short-circuit with
   "NOT READY — coverage X%".
2. Conjunction. If coverage = 100%, evaluate each member separately
   (no composite score):
   - correction_rate (corrections/day in 30-day window)
   - grounding_violation_count
   - silent_failure_count (verify-log ok=False, verified=True)
   - novel_failure_mode_rate
   - unauthorized_write_count (blocked attempts)

the operator is notified only on headline change (Addendum 3 silent-poll).

## What Alice CANNOT do
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

- **Cannot send job applications.** Drafts only. the operator submits.
- **Cannot send outreach without review.** Drafts only. the operator sends.
- **Cannot apply on the operator's behalf** (explicit rule in `CLAUDE.md`).
- **Cannot browse arbitrary sites.** Only the sources wired into
  `source_*.py` and pages reachable via `requests` + the JD-fetch path.
  Sites that require JS rendering, login, or have aggressive bot-blocks
  are out of reach without Playwright/manual help.
- **Cannot set TERMINAL_GATED statuses on the sheet without the operator
  authorization** (item 2 — write-site enforcement). The gated set is
  `{submitted, interviewed, interviewing, offer, offered, negotiating,
  rejected, rejected-by-us, withdrawn, closed}`. Alice retains autonomous
  control for non-terminal statuses (`new`, `good fit`, `not a fit`,
  `materials pending`, `sourced`, `screening`, `first screen scheduled`).
  Attempts to write a gated status without `authorized=True` raise
  `UnauthorizedStatusWrite` and journal to
  `feedback/sheet-write-blocked.jsonl`.
- **Cannot drop a focus role on the basis of an unauthorized Alice write.**
  `focus.auto_drop_submitted` consults `ledger.last_write_for_row`; if the
  triggering status was an unauthorized Alice write, the drop is skipped
  and the operator is announced loudly (item 6a).
- **Cannot fabricate experience, metrics, or credentials.**
- **Cannot describe Ironclad Industrial as a Cadence Analytics customer,
  tenant, design partner, or pilot.** Ironclad Industrial is the
  inspiration / problem-source / early validator for Cadence Analytics,
  not a customer relationship. The whole class of customer-relationship
  claims is forbidden (customer, tenant, design partner, pilot, founding
  customer, reference account, etc.), not just any single term. Describe
  Cadence Analytics accurately when it comes up: a multi-agent platform
  where ML takes the guesswork out of customer and margin signals, and
  agents handle the manual revenue-ops workflows that eat time.
- **Cannot use em dashes** in drafts addressed to or written as the operator.
- **Cannot bypass the confirmation loop.** All destructive state changes
  go through `pending-confirmation.json` with the 5-minute window.
- **Cannot run arbitrary shell.** The self-inspection surface is an
  allowlist; anything outside it is refused.
- **Cannot render resume / cover-letter to `.docx` / `.pdf`.** Today the
  generator emits markdown only. Conversion is a roadmap item.
- **Cannot scan non-operator inbound email.** `imap_inbound` is not built;
  email reading is limited to the operator's own replies via the Gmail Sent
  conversation thread. Roadmap item.
- **Cannot run failure-injection probes.** The harness for systematic
  failure simulation is not yet built; sequenced after C2 verifiers are
  battle-tested. Roadmap item.
- **Cannot spawn subagents.** Deferred — see `roadmap/deferred-features.md`.
- **Cannot run Ralph (self-repeating) loops.** Deferred — see roadmap.
- **Cannot self-modify behavioral rules.** Deferred — see roadmap.
- **Cannot take proactive actions beyond notification.** Deferred — see
  roadmap.

## State-grounding invariant (Layer 3)

Alice has NO ability to assert pipeline state (focus list, role statuses,
counts, who's submitted, what's in prep) from memory or prior conversation
turns. Every state claim must come from a fresh file read on the current
message turn. `telegram_bot._build_alice_context` re-reads
`feedback/focus.json` and fetches the Google triage sheet on every
message, annotates each section with source-and-freshness, and a HARD
INVARIANT in the router system prompt forbids state-claims not backed
by those fresh blocks. If a state file or the sheet is unreadable,
Alice must say "I can't confirm current state" instead of guessing.

## Write-site enforcement (Layer 4 — item 2)

Layer 3 reads cannot rescue Alice from her own bad writes. The Boreal CAD bug was
Alice writing `status="submitted"` herself, then reading that value back
confidently, then auto-dropping from focus, then reporting it as fact —
Layer 3 was "correct" the whole time because it only enforces grounding on
reads, not on writes. Item 2 closed the loop at the write site:

- `ledger.update_status` and `update_status_batch` require
  `authorized=True` for any status in `TERMINAL_GATED`.
- Every write journals to `feedback/sheet-write-log.jsonl` with
  `{ts, row_idx, status, authorized, source}`.
- Every blocked attempt journals to `feedback/sheet-write-blocked.jsonl`
  with a stack snippet.
- `ledger.last_write_for_row()` exposes the audit trail to downstream
  consumers like `focus.auto_drop_submitted`.

## Verification + fail-closed (Layer 4 — item 5)

Per Addendum 2: verify what you can, fail closed on what you can't. Every
action group in `confirm_and_execute.execute_pending` runs the matching
verifier from `scripts/verify.py` after the action lands. Failures append
to the execute_pending errors list, get printed as VERIFY ERROR, and
journal to `feedback/verify-log.jsonl`. Verifiers that have no independent
surface (unusual today, since all six action types have one) return
`verified=False` so the caller treats it as unverified rather than
asserting success.

## Correction logging

`self/correction-log.csv` is an append-only measurement log of times
the operator has corrected Alice. Columns: date, what_alice_said, what_was_true,
category. The readiness check (item 8) consumes this log. When Sentry is
active, every detected correction reply also fires
`capture_message("correction", level="warning")` (item 7). No automated
rule-extraction is wired up yet — the log is for human review and
measurement.

## How everything stacks (do not weaken)

The protections layer; none of them are a substitute for any other:

1. **Layer 3 grounding** — read state fresh before asserting.
2. **5-minute confirmation window** — destructive actions wait for the operator.
3. **Write-site enforcement (item 2)** — Alice cannot autonomously set
   terminal statuses, period.
4. **Verification per action (item 5 / C2)** — verify what landed
   independently of how it was sent.
5. **Telemetry (item 7 / Sentry)** — fail closed and capture when
   verification is impossible.
6. **State-repo rollback (item 3 / C3)** — local-only snapshots before
   and after every run, so any bad day is reversible.
7. **C4 announce gates (item 6)** — auto_drop is no longer silent; Boreal CAD
   class write-bugs surface loudly.
8. **Readiness check (item 8 / C5(c))** — measures whether the above are
   working, doesn't relax their thresholds.

Each layer survives independently. The Boreal CAD-class of failure required all
of (3), (4), (6), (7) to land before Alice could be trusted to write the
status herself again — and (7) is gated on the readiness check showing 30
days of green.

## Definition of done — HARD GATE (Layer 4 / shipping discipline)

A runtime capability is NOT "shipped" until `feedback/verify-log.jsonl`
(or the equivalent live-evidence surface for that capability) shows that
the running bot exercised it through its real invocation path.

This is non-negotiable. Unit tests passing is necessary, not sufficient.

The shipped-vs-live seam has failed three times in this project (git
tool, Sentry config, C2 verifier wiring). Each failure traced to the
same shortcut: code presence was graded as completion, no live exercise
was run, the gap surfaced later as silent broken behavior. The gate
closes that shortcut.

### What counts as live evidence

The acceptable evidence depends on the capability's action class:

| Capability class           | Live evidence                                                |
|----------------------------|--------------------------------------------------------------|
| email send                 | verify-log.jsonl entry kind=email_send, ok=true, verified=true |
| telegram send              | verify-log.jsonl entry kind=telegram_send, ok=true, verified=true |
| sheet status write         | verify-log.jsonl entry kind=sheet_write, ok=true, verified=true |
| sheet insert               | verify-log.jsonl entry kind=sheet_insert, ok=true, verified=true |
| file write                 | verify-log.jsonl entry kind=file_write, ok=true, verified=true |
| focus apply                | verify-log.jsonl entry kind=focus_apply, ok=true, verified=true |
| pending execute            | verify-log.jsonl entry kind=pending_executed, ok=true, verified=true |
| cron / scheduled step      | activity-log entry with timestamp inside the expected window |
| state-repo snapshot        | `git log -1` on `feedback/.git` showing snapshot since ship  |
| telemetry / observability  | SDK confirms event ingested (or, if no DSN, explicit "no-op" log line on entry-point execution) |
| knowledge-base content     | live grep against the bot's actual loaded context surfaces the content |

For every category above, the evidence must come from a *real* invocation
of the production code path — not a pytest fixture, not a one-shot script
that bypasses the harness, not the developer hand-running the verifier in
isolation. The bot or cron itself must have exercised the path.

### When evidence cannot be obtained

If a path has no available live-evidence surface (rare; usually means a
verifier is missing), the capability is *not* shipped. The work item
remains open. The acceptable resolutions are:

1. Build the missing verifier (preferred — closes the gap).
2. Mark the capability with `verification_status: live_evidence_unavailable`
   in its activity-log entry, so the readiness check and audits surface
   it as un-shipped.

Falling back to "the code exists, trust it" is not a resolution.

### Audit cadence

Every quarter, audit the inventory of shipped capabilities against the
live-evidence standard. Anything that cannot produce evidence is moved
back to in-progress until evidence is produced. See
`roadmap/shipping-audit-2026-05.md` for the most recent audit.
