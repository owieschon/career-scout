# Deferred features

These are autonomous behaviors that are NOT live in Alice yet (or are live but
gated). Each entry names what it would deliver, the measurable preconditions
for promoting it to autonomous status, and current status.

The rule: a behavior promotes only when the conjunction of preconditions
holds. No composite scores. No averages. Each precondition is evaluated
separately by `scripts/readiness_check.py` (item 8).

---

## `focus.auto_drop_submitted` — autonomous focus-list trimming

**Delivers:** keeps the focus list at active roles only by removing roles whose
sheet status has moved past `submitted` (or other terminal states).

**Status:** LIVE but GATED (item 2 + item 6a).
- Will not drop a role whose triggering status was written by Alice without
  the operator authorization (see `ledger.last_write_for_row` + the
  `TERMINAL_GATED` set).
- Every drop announces to the operator via Telegram and email (no silent drops).
- Every skip-unauthorized announces with the source of the suspect write.

**Preconditions for unsupervised operation (i.e. removing the announcements):**
1. 30 consecutive days of zero Boreal CAD-class events
   (`auto_drop_skipped_unauthorized` rows in `focus.json.version_history`
   for any role).
2. 30 consecutive days of `sheet-write-blocked.jsonl` showing zero
   unauthorized terminal-status write attempts.
3. Verification coverage for `sheet_write` action type = 100% (already true
   per `verify.ACTION_VERIFICATION_COVERAGE`).

---

## `ledger.insert_new` — autonomous source-to-sheet insertion

**Delivers:** new roles surfaced by `source_deep` / `source_multi` are inserted
into the sheet at row 2 with `status="new"`.

**Status:** LIVE, GRANDFATHERED.
- Mitigation: status="new" means human review (the operator labels good fit / not a
  fit on the sheet before any downstream action).
- No gating added because the write is bounded — only status="new" is ever
  written by this path. New rows cannot cascade into focus, prep, or
  outreach without the operator marking them.

**Preconditions for tighter behavior (e.g. duplicate-detection autonomy):**
- Track false-positive insertion rate. If false positives stay below 5% over
  30 days, can promote duplicate suppression to autonomous.
- Currently NOT instrumented — adding insertion-quality telemetry is a
  follow-on.

---

## Ralph loops (self-repeating multi-step plans)

**Delivers:** Alice spins her own loop on a multi-step task (e.g. "keep
sourcing until 3 new tier-1 leads"). Today she does one pass per cron tick.

**Status:** DEFERRED — unsafe until self-assessment is trustworthy.

**Why deferred:** a Ralph loop multiplies any silent-failure surface by the
number of iterations. Alice's failure-mode telemetry (item 7 Sentry +
verification log) is brand new; we have no baseline novel-mode rate to
measure against. Promoting before that baseline exists invites
unbounded-loop incidents that look like "she did something for an hour" in
the logs.

**Preconditions for promotion:**
1. ≥ 90 days of Sentry telemetry with zero novel failure modes.
2. Verification coverage = 100% for every action a loop would invoke.
3. A separate kill-switch surface that Alice cannot itself disable
   (e.g. a file on disk the operator owns, checked at every iteration).
4. Per-loop step budget enforced at the loop driver layer.

---

## Subagent spawning

**Delivers:** Alice spawns dedicated subagents (e.g. one per role in prep) to
parallelize work.

**Status:** DEFERRED — multiplies silent-failure surface area.

**Why deferred:** each subagent is a new context with its own grounding,
verification, and write-site enforcement. Today those exist in one place;
duplicating them is the same problem as the Ralph loop times N.

**Preconditions for promotion:**
1. Ralph loops promoted first (same telemetry conditions).
2. A shared write-site enforcement + verification surface that subagents
   share (i.e. they all go through `ledger.update_status` and `verify.py`,
   not their own implementations).
3. Subagent output reviewed by the parent agent (Alice) before any sheet
   or notification write.

---

## Self-modification of behavioral rules

**Delivers:** Alice edits her own brief / corrections / capability docs based
on observed performance.

**Status:** DEFERRED — collides with grounding and human-curated discipline.

**Why deferred:** the entire correction loop exists because Alice's
self-model is unreliable on the topics that matter (state, verification,
fail-closed behavior). Letting her self-edit risks her silently weakening
the rules that catch her mistakes. The Layer 3 grounding invariant and the
write-site enforcement are EXPLICITLY rules Alice cannot self-modify.

**Preconditions for promotion (intentionally hard):**
1. ≥ 6 months of zero rule-relaxation drift in human review of
   correction-log entries.
2. Every proposed self-edit reviewed by the operator with a clear diff before
   it applies.
3. An independent verifier that confirms Layer 3 + write-site + 5-min
   timer remain in effect after every self-edit.
4. Rollback via the state-repo if any of (1-3) fail.

---

## Proactive autonomous actions beyond notification

**Delivers:** Alice does things on her own initiative beyond what the daily
cron flow + operator-directed prep produces (e.g. she decides to send outreach to
a new contact, or kill a role from the sheet).

**Status:** DEFERRED — premature until verification proven.

**Why deferred:** verification is brand-new (item 5). Without a track record
of the verifiers actually catching real failures, we don't know whether they
work. Proactive actions without proven verifiers is exactly the Boreal CAD bug at
scale.

**Preconditions for promotion:**
1. ≥ 90 days of verification surfaces having actually fired on real failures
   (i.e. non-zero `verify-log.jsonl` entries with `ok=false, verified=true`,
   followed by correct human/system response).
2. Correction rate trend in `self/correction-log.csv` showing a 30-day
   moving average decline.
3. operator-issued readiness signal (item 8 readiness check shows GREEN across
   all conjunction members for ≥ 30 days).

---

## Summary

Nothing in this list weakens the live floors:
- Layer 3 state-grounding invariant (no state claims without fresh read + ts)
- 5-minute confirmation window
- Write-site enforcement on TERMINAL_GATED statuses
- Independent verification per action type

Everything in this list stacks on top of those floors only after the
preconditions are independently demonstrated.
