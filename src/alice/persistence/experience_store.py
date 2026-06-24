"""Experience-capture store — the downstream-trusted source of Jordan's
factual experience-detail for writers and the Stage 3 verifier.

Two population paths, two integrity mechanisms
-----------------------------------------------
The `experience` source class has TWO population paths, both unioned at
`retrieve_for_role`. Each has its own integrity mechanism appropriate
to its input:

  CHAT-CAPTURE        → feedback/experience-store.jsonl
                        Integrity: every `verbatim` field is an exact
                        substring of a real Jordan turn in
                        telegram-history.jsonl. Enforced MECHANICALLY at
                        capture by flag_experience_candidate (raises
                        VerbatimMismatchError on non-substring).

  FILE-AUTHORED       → knowledge/experience/<source>.md
                        Integrity: write-access discipline. Jordan is the
                        only author of files in knowledge/experience/.
                        The file's presence in that directory IS the
                        review evidence. Enforced at the FILE-WRITE step
                        (who can write files there), NOT at parse.
                        See knowledge/experience/README.md for details.

Both guarantee integrity-at-source so downstream consumers (writers,
verifier) can trust the experience source uniformly. The mechanisms
differ because chat-captures CAN be mechanically verified at parse
(substring check); file-authored entries CANNOT (the parser cannot
verify "did Jordan review this?") — so the integrity boundary lives at
write-access, not at parse.

DO NOT add a substring-check or other mechanical integrity verifier at
the parse step for file-authored entries. That's not the right boundary.
The boundary is write-access discipline. Adding a parse-time check would
either reject valid file-authored content or give a false sense of
mechanical guarantee where the real guarantee is human discipline.


Why this exists
---------------
The four-stage prep pipeline (prep_pipeline.py) currently grounds writers
in two things: (1) Jordan's resume variant (templates/) and (2) the JD body.
What the resume variants don't carry — and what writers keep needing — is
the FACTUAL DETAIL Jordan drops in chat: specific events, numbers, named
accounts, qualitative recovery stories, deal mechanics. Those details, if
fabricated, are structurally uncatchable by Stage 3 (the verifier checks
claims against the same JD + resume-variant blob the writers see). So the
fabrications either land in Jordan's outbox or they're caught only by
human read-through.

This module is the SOURCE OF TRUTH writers and the verifier both pull
from. Once an entry lands in feedback/experience-store.jsonl, it is:

  (a) used by Stage 2 writers as "EXPERIENCE EXTRAS" block (parallel to
      the candidate's HISTORY block), and

  (b) added to Stage 3's ground_blob so claims grounded in confirmed
      experience pass, and claims that aren't grounded in any of the
      three sources (JD / company / experience / Jordan's variant) flag.

The store is "downstream-trusted." Neither writers nor the verifier
re-verify entries — a bad entry corrupts both seams. The only way to
prevent that is to guarantee integrity AT CAPTURE.

Capture integrity (critical)
--------------------------------
Every candidate's `verbatim` field MUST be an exact substring of a real
Jordan turn in feedback/telegram-history.jsonl. The substring check is
enforced structurally by `flag_experience_candidate` — it raises
ValueError if the verbatim text is not literally present in the cited
turn's text. Paraphrase is impossible at the API level.

Model interpretation (paraphrase, summary, suggested tags) lives in a
SEPARATE `model_summary` / `suggested_tags` field that NEVER flows to
writers or the verifier. Those fields exist only to help Jordan recognize
the candidate in the morning digest. They are dropped before the
confirmed entry lands in the durable store.

Two triggers
------------
EXPLICIT — Jordan says "remember this", "save this", "log this" or a near
synonym. The current turn (or the prior user turn if the explicit
command is a standalone message) is flagged immediately.

AMBIENT — a Haiku-based detector reviews recent user turns out-of-band
and flags candidates whose content looks like factual experience-detail.
The detector is intentionally LIBERAL (over-flag rather than miss
qualitative stories); the morning confirmation gate is where Jordan prunes.

Ambient uses Haiku rather than regex because qualitative stories without
numbers and without keyword anchors slip through pure regex. Haiku is cheap
enough that false-negatives at capture cost more than over-flagging, so it
is tuned liberal.

Confirmation gate
-----------------
All candidates land in feedback/experience-candidates.jsonl with
`status="pending"`. They surface in the morning digest as a numbered
list with the verbatim quote, the suggested tags, and a snippet of
context. Jordan replies to confirm, edit, or reject; silence for three
digests auto-expires the candidate.

Only confirmed candidates are written to feedback/experience-store.jsonl.

Retrieval (token-budget capped)
-------------------------------
At Stage 1 GROUND time, `retrieve_for_role` returns the entries whose
tags intersect the role's target tags, packed greedy until a token
budget cap is hit. Override on the original "12 entries" rule: a budget
in tokens travels better between large and small entries; 12 short
entries and 1 long entry cost the model very different attention.

Staleness / contradiction
-------------------------
Reused pattern from telegram_bot._text_is_superseded. An entry can be
manually superseded with a pointer to its replacement; superseded
entries are KEPT (audit trail) but SKIPPED during retrieval.

A contradiction detector flags candidate pairs that name the same
account and the same metric token but disagree on the value — surfaced
to Jordan in the morning digest so they resolve which is current.

Context caching
---------------
±2 turns of conversational context are stored with each entry, so a
quote like "we got it back to $14M" retains its referent (what company,
what metric, recovering from what). Without this, a tag-matched entry
delivered to a writer would be unmoored.

Dual-landing in the pipeline
----------------------------
Writers (Stage 2): assemble_prompt injects an EXPERIENCE EXTRAS block
parallel to the history block, token-capped to the retrieval budget.

Verifier (Stage 3): the confirmed entries are appended to ground_blob
so experience-grounded claims pass and ungrounded ones flag. Per-claim
attribution records which of the four sources (JD / company / variant /
experience) grounded each claim, so the .pipeline-metadata.json audit
trail is honest about provenance.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from alice import repo_paths


REPO_ROOT = Path(repo_paths.ROOT)
HISTORY_PATH       = REPO_ROOT / "feedback" / "telegram-history.jsonl"
CANDIDATES_PATH    = REPO_ROOT / "feedback" / "experience-candidates.jsonl"
STORE_PATH         = REPO_ROOT / "feedback" / "experience-store.jsonl"

# Auto-expire candidates that have appeared in this many morning digests
# without a reply (silence = rejection).
DIGEST_EXPIRY_THRESHOLD = 3

# Per-call retrieval budget (rough charstokens conversion: 1 token ≈ 4 chars).
# 12 short entries × ~250 chars = ~750 tokens; one long story = ~1000 tokens.
# 2500 tokens ≈ 10K chars — generous headroom for ~10 short or 2-3 long entries.
DEFAULT_TOKEN_BUDGET = 2500
_CHARS_PER_TOKEN     = 4

# Ambient detector params (Haiku review). Run out-of-band; never on the
# critical path of a user-facing turn.
_AMBIENT_MAX_TOKENS  = 800
_AMBIENT_TASK        = "experience_ambient_review"


# ─── data classes ────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """One staged candidate awaiting Jordan's confirmation in the morning digest."""
    candidate_id:     str
    created_at:       str
    trigger:          str   # "explicit" | "ambient"
    source_turn_ts:   str   # ts of the Jordan turn the verbatim came from
    verbatim:         str   # exact substring of the source turn's text
    context_before:   list  # list of {role, text, ts}
    context_after:    list  # list of {role, text, ts}
    model_summary:    str = ""    # model's interpretation; NEVER flows downstream
    suggested_tags:   list = field(default_factory=list)
    ambient_score:    float | None = None
    digest_count:     int = 0     # incremented each digest surfacing without reply
    status:           str = "pending"  # pending | confirmed | rejected | expired


@dataclass
class Entry:
    """One confirmed entry — the durable, downstream-trusted record."""
    entry_id:         str
    confirmed_at:     str
    from_candidate_id: str
    source_turn_ts:   str
    verbatim:         str
    context_before:   list
    context_after:    list
    tags:             list
    operator_edit_note:   str | None = None
    superseded_by:    str | None = None
    superseded_at:    str | None = None
    superseded_reason: str | None = None


# ─── JSONL plumbing ──────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    """Read all records from a JSONL file. Returns [] if missing or empty."""
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
 # Malformed line — skip rather than corrupt the whole reader.
 # The append-only invariant means this is the line that broke
 # mid-write; subsequent good lines are still valid.
                continue
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON record to a JSONL file. One-line append on POSIX is
    atomic for small writes — no flock needed for the append-only path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _rewrite_jsonl(path: Path, records: list[dict]) -> None:
    """Rewrite the whole JSONL file. Used when updating in-place (status
    changes, digest_count increments, supersession). Atomic via temp + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    tmp_path.replace(path)


# ─── history lookup (the substring-match enforcement seam) ───────────────────

def _find_user_turn(source_turn_ts: str) -> dict | None:
    """Look up a Jordan user turn in telegram-history.jsonl by exact ts match.

    Returns the turn dict (with role/text/ts) or None if not found. Used by
    the capture-integrity check — a verbatim is only valid if it's a
    substring of the cited turn's text.
    """
    for turn in _read_jsonl(HISTORY_PATH):
        if turn.get("ts") == source_turn_ts and turn.get("role") == "user":
            return turn
    return None


def _surrounding_context(source_turn_ts: str, n_before: int = 2,
                          n_after: int = 2) -> tuple[list[dict], list[dict]]:
    """Return (before, after) lists of ±n turns surrounding the source turn.

    Context preserves meaning: a quote like 'we got it back to $14M' needs
    its referent — what account, what metric, recovering from what. Without
    the surrounding turns, the entry is unmoored.
    """
    history = _read_jsonl(HISTORY_PATH)
    target_idx = None
    for i, turn in enumerate(history):
        if turn.get("ts") == source_turn_ts and turn.get("role") == "user":
            target_idx = i
            break
    if target_idx is None:
        return [], []
    before = history[max(0, target_idx - n_before): target_idx]
    after  = history[target_idx + 1: target_idx + 1 + n_after]
 # Keep only the fields the downstream code uses.
    def _slim(t: dict) -> dict:
        return {
            "role": t.get("role", ""),
            "ts":   t.get("ts", ""),
            "text": (t.get("text", "") or "")[:600],
        }
    return [_slim(t) for t in before], [_slim(t) for t in after]


# ─── capture integrity: the critical safeguard ───────────────────────────

class VerbatimMismatchError(ValueError):
    """Raised by flag_experience_candidate when the supplied verbatim is not
    a substring of the cited source turn's text. This is the structural
    enforcement that makes paraphrase impossible at the API level."""


def flag_experience_candidate(
    *,
    verbatim: str,
    source_turn_ts: str,
    trigger: str,
    model_summary: str = "",
    suggested_tags: list[str] | None = None,
    ambient_score: float | None = None,
) -> str:
    """Stage a new candidate. Enforces substring-match against a real Jordan
    turn — raises VerbatimMismatchError otherwise.

    The substring check is the critical safeguard. It runs BEFORE the
    candidate touches disk. There is NO write path that bypasses it.

    Returns the candidate_id assigned to the staged entry.
    """
    if trigger not in ("explicit", "ambient"):
        raise ValueError(
            f"flag_experience_candidate: trigger must be 'explicit' or "
            f"'ambient'; got {trigger!r}"
        )
    if not verbatim or not verbatim.strip():
        raise ValueError("flag_experience_candidate: verbatim must be non-empty")

    turn = _find_user_turn(source_turn_ts)
    if turn is None:
        raise VerbatimMismatchError(
            f"flag_experience_candidate: no user turn with ts={source_turn_ts!r} "
            f"found in {HISTORY_PATH.name}. Verbatim cannot be attributed to a "
            "real source turn. Refusing to stage."
        )

    turn_text = turn.get("text", "") or ""
    if verbatim not in turn_text:
 # The structural API-level rejection. Paraphrase, summary, model
 # interpretation, light edits — anything that breaks substring
 # equality — is rejected here, before disk.
        raise VerbatimMismatchError(
            f"flag_experience_candidate: verbatim is NOT a substring of the "
            f"cited user turn at ts={source_turn_ts}. This is the capture-"
            f"integrity check; paraphrase is impossible at this seam.\n"
            f"  verbatim (len={len(verbatim)}): {verbatim[:200]!r}\n"
            f"  turn_text head (len={len(turn_text)}): {turn_text[:200]!r}"
        )

    before, after = _surrounding_context(source_turn_ts)
    cand = Candidate(
        candidate_id    = f"exp-cand-{uuid.uuid4().hex[:8]}",
        created_at      = datetime.now().isoformat(timespec="seconds"),
        trigger         = trigger,
        source_turn_ts  = source_turn_ts,
        verbatim        = verbatim,
        context_before  = before,
        context_after   = after,
        model_summary   = model_summary or "",
        suggested_tags  = suggested_tags or [],
        ambient_score   = ambient_score,
        digest_count    = 0,
        status          = "pending",
    )
    _append_jsonl(CANDIDATES_PATH, asdict(cand))
    return cand.candidate_id


# ─── explicit trigger detection ──────────────────────────────────────────────

_EXPLICIT_TRIGGER_RE = re.compile(
    r"\b(?:remember\s+(?:this|that)|save\s+(?:this|that)|"
    r"log\s+(?:this|that)|capture\s+(?:this|that)|"
    r"note\s+this|keep\s+this|store\s+this|"
    r"add\s+to\s+(?:experience|memory|store)|"
    r"don'?t\s+forget\s+this)\b",
    re.IGNORECASE,
)


def detect_explicit_trigger(user_text: str) -> bool:
    """Return True if user_text contains an explicit 'remember this' trigger.

    The keyword family is intentionally narrow — "remember this", "save this",
    "log this", "don't forget this" and close synonyms. A loose match would
    fire on casual references ("I remember that...") and pollute candidates.
    """
    if not user_text:
        return False
    return _EXPLICIT_TRIGGER_RE.search(user_text) is not None


# ─── ambient detector (Haiku-based; runs out-of-band) ────────────────────────

_AMBIENT_REVIEW_PROMPT = """\
You are reviewing recent messages Jordan has sent in chat. Your job: identify
FACTUAL EXPERIENCE-DETAIL that would be valuable for resume/cover-letter
writers to ground their claims in. TUNE LIBERAL — over-flag rather than
under-flag. Jordan will confirm or reject in the morning digest.

WHAT TO FLAG (factual experience-detail Jordan has shared):
- specific events (a deal they closed, a save they made, a launch they led)
- specific numbers tied to outcomes (revenue, retention, deal size, %)
- named accounts (companies, customers, vendors they worked with)
- qualitative stories (a recovery, an objection-handling, a key insight)
- specific decisions Jordan made and their outcomes

WHAT TO SKIP:
- preferences / self-framing / opinions ("I prefer X", "I think Y")
- meta-commentary on the search itself ("apply to X", "let's prep Y")
- generic statements without specific factual content
- intent declarations / requests ("can you do X", "remind me about Y")
- emotional or affect-only content with no underlying fact

For each candidate flag, output the EXACT VERBATIM SPAN from Jordan's
message that contains the fact. Do not paraphrase. Do not summarize in
the verbatim_span — that goes in a separate summary field. The verbatim
MUST be an exact substring of the cited turn.

Each input turn is given as:
TURN [ts=<ts>]: <text>

Output JSON only — no markdown fences, no preamble:

{
  "candidates": [
    {
      "source_turn_ts": "<ts of the turn this came from>",
      "verbatim_span":  "<exact substring of that turn's text>",
      "summary":        "<your interpretation; one sentence; for Jordan's review only>",
      "suggested_tags": ["<2-5 tags: account names, metrics, outcomes>"],
      "score":          0.0-1.0
    }
  ]
}

If no candidates, output {"candidates": []}.
"""


def _build_ambient_prompt(turns: list[dict]) -> str:
    """Format the recent Jordan turns into the prompt body."""
    lines = [_AMBIENT_REVIEW_PROMPT, "", "TURNS TO REVIEW:"]
    for t in turns:
        if t.get("role") != "user":
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"\nTURN [ts={t.get('ts','')}]: {text}")
    return "\n".join(lines)


def ambient_review(turns: list[dict], *, min_score: float = 0.4) -> list[str]:
    """Run the Haiku ambient detector over recent Jordan user turns.

    Each candidate the model proposes is validated by the same substring
    check used by flag_experience_candidate — so a hallucinated verbatim
    is rejected at the seam. Only candidates that pass the substring check
    AND score >= min_score are staged.

    Returns the list of candidate_ids that landed in the staging file.
    Intentionally non-fatal on individual rejection: a failed substring
    check on candidate 3 of 5 does not abort the review for the other 4.

    Runs OUT OF BAND — call this from a cron / scheduled task, never on
    the critical path of a user-facing chat turn. The latency budget and
    cost budget for chat have no room for an extra Haiku call per message.
    """
    if not turns:
        return []

    try:
        from alice.llm import llm
    except Exception:
        return []

    prompt = _build_ambient_prompt(turns)
    try:
        res = llm.call(
            _AMBIENT_TASK,
            prompt,
            max_tokens=_AMBIENT_MAX_TOKENS,
 # Haiku tier — fast and cheap. The task is review, not generation.
            tier="cheap",
        )
    except Exception:
        return []

    raw = (res.get("text") or "").strip()
    if not raw:
        return []

 # Strip any markdown fences the model added despite instructions.
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
 # Fallback: find first {...} object
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []

    out_ids: list[str] = []
    for cand in parsed.get("candidates", []):
        try:
            score = float(cand.get("score", 0.0))
        except (ValueError, TypeError):
            score = 0.0
        if score < min_score:
            continue
        try:
            cid = flag_experience_candidate(
                verbatim       = cand.get("verbatim_span", "") or "",
                source_turn_ts = cand.get("source_turn_ts", "") or "",
                trigger        = "ambient",
                model_summary  = cand.get("summary", "") or "",
                suggested_tags = cand.get("suggested_tags", []) or [],
                ambient_score  = score,
            )
            out_ids.append(cid)
        except VerbatimMismatchError:
 # Model hallucinated the verbatim — rejected at the seam.
 # Not fatal; continue with other candidates.
            continue
        except Exception:
            continue
    return out_ids


# ─── confirmation gate ───────────────────────────────────────────────────────

def get_pending_candidates() -> list[dict]:
    """Return all candidates with status='pending'. Used by the digest
    surfacing path and by tests."""
    return [c for c in _read_jsonl(CANDIDATES_PATH)
            if c.get("status") == "pending"]


def get_candidate(candidate_id: str) -> dict | None:
    """Look up a candidate by id (any status)."""
    for c in _read_jsonl(CANDIDATES_PATH):
        if c.get("candidate_id") == candidate_id:
            return c
    return None


def _update_candidate(candidate_id: str, mutate) -> dict | None:
    """Apply `mutate(record_dict)` in place and rewrite the file."""
    records = _read_jsonl(CANDIDATES_PATH)
    updated = None
    for r in records:
        if r.get("candidate_id") == candidate_id:
            mutate(r)
            updated = r
            break
    if updated is not None:
        _rewrite_jsonl(CANDIDATES_PATH, records)
    return updated


def mark_digest_surfaced(candidate_ids: list[str]) -> None:
    """Increment digest_count for each candidate that just appeared in a
    morning digest. After DIGEST_EXPIRY_THRESHOLD surfacings without
    reply, the candidate auto-expires (silence = rejection)."""
    if not candidate_ids:
        return
    records = _read_jsonl(CANDIDATES_PATH)
    changed = False
    for r in records:
        if r.get("candidate_id") in candidate_ids and r.get("status") == "pending":
            r["digest_count"] = int(r.get("digest_count", 0)) + 1
            if r["digest_count"] >= DIGEST_EXPIRY_THRESHOLD:
                r["status"] = "expired"
            changed = True
    if changed:
        _rewrite_jsonl(CANDIDATES_PATH, records)


def confirm_candidate(
    candidate_id: str,
    *,
    edited_tags: list[str] | None = None,
    edited_note: str | None = None,
) -> str:
    """Move a candidate from staging to the durable store.

    `edited_tags` overrides the suggested_tags from the candidate; if not
    supplied, the candidate's suggested_tags are used.

    `edited_note` (optional) is a free-text note from Jordan explaining any
    edit or framing — stored on the entry as operator_edit_note. The verbatim
    itself is NEVER edited at confirm time — that would break the substring
    invariant. To change the captured quote, reject and re-flag with the
    correct span.

    Returns the entry_id of the new durable record. Raises if the candidate
    doesn't exist, has wrong status, or has already been confirmed.
    """
    cand = get_candidate(candidate_id)
    if cand is None:
        raise ValueError(f"confirm_candidate: no candidate with id {candidate_id!r}")
    if cand.get("status") != "pending":
        raise ValueError(
            f"confirm_candidate: candidate {candidate_id!r} is in status "
            f"{cand.get('status')!r}; only 'pending' can be confirmed."
        )

    tags = edited_tags if edited_tags is not None else (cand.get("suggested_tags") or [])
    entry = Entry(
        entry_id          = f"exp-{uuid.uuid4().hex[:8]}",
        confirmed_at      = datetime.now().isoformat(timespec="seconds"),
        from_candidate_id = candidate_id,
        source_turn_ts    = cand.get("source_turn_ts", ""),
        verbatim          = cand.get("verbatim", ""),
        context_before    = cand.get("context_before", []),
        context_after     = cand.get("context_after", []),
        tags              = list(tags),
        operator_edit_note    = edited_note,
    )
    _append_jsonl(STORE_PATH, asdict(entry))

 # Mark the candidate confirmed and link to the entry.
    def _mut(r):
        r["status"] = "confirmed"
        r["confirmed_entry_id"] = entry.entry_id
    _update_candidate(candidate_id, _mut)
    return entry.entry_id


def reject_candidate(candidate_id: str, reason: str = "") -> None:
    """Mark a candidate rejected. It is kept in the staging file (audit
    trail) but never surfaces again and never enters the durable store."""
    cand = get_candidate(candidate_id)
    if cand is None:
        raise ValueError(f"reject_candidate: no candidate with id {candidate_id!r}")
    def _mut(r):
        r["status"] = "rejected"
        if reason:
            r["reject_reason"] = reason
    _update_candidate(candidate_id, _mut)


# ─── store reads + supersession ──────────────────────────────────────────────

def get_all_entries(*, include_superseded: bool = False) -> list[dict]:
    """Return all confirmed entries. Superseded entries are excluded by
    default — they're kept in the store as audit trail but never delivered
    to writers or the verifier."""
    entries = _read_jsonl(STORE_PATH)
    if include_superseded:
        return entries
    return [e for e in entries if not e.get("superseded_at")]


def get_entry(entry_id: str) -> dict | None:
    for e in _read_jsonl(STORE_PATH):
        if e.get("entry_id") == entry_id:
            return e
    return None


def supersede_entry(entry_id: str, *, replaced_by: str | None = None,
                    reason: str = "") -> None:
    """Mark an entry superseded. The replacement entry_id is optional —
    a contradiction Jordan can't currently resolve can be tagged with
    `reason="unresolved-contradiction"` and no `replaced_by`.

    Superseded entries are kept (audit trail) but skipped during retrieval.
    """
    records = _read_jsonl(STORE_PATH)
    found = False
    for r in records:
        if r.get("entry_id") == entry_id:
            r["superseded_at"]     = datetime.now().isoformat(timespec="seconds")
            r["superseded_by"]     = replaced_by
            r["superseded_reason"] = reason
            found = True
            break
    if not found:
        raise ValueError(f"supersede_entry: no entry with id {entry_id!r}")
    _rewrite_jsonl(STORE_PATH, records)


# ─── contradiction detector ──────────────────────────────────────────────────

# Match a dollar token similar to prep_pipeline._DOLLAR_RE but capture for
# normalization. Same-account + same-metric + different value = contradiction.
_CONTRADICTION_DOLLAR_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s?([KkMmBb]?)\+?")


def _normalize_dollar(token: str) -> str | None:
    """Return a canonical 'amount+suffix' form ('14M', '127.5K') or None."""
    m = _CONTRADICTION_DOLLAR_RE.search(token)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2).upper()}"


def find_contradictions() -> list[dict]:
    """Surface entry pairs that name the same tag (account) but disagree on
    a dollar value. v1: account = any tag both entries share that LOOKS like
    a proper-noun account name (capitalized, length >= 3, alpha-prefixed).

    Returns a list of {tag, entries: [entry_a, entry_b], values: [v1, v2]}
    for the morning digest to surface so Jordan resolves.
    """
    entries = get_all_entries()
    pairs: list[dict] = []
    for i, e_a in enumerate(entries):
        a_dollars = {_normalize_dollar(t) for t in
                     _CONTRADICTION_DOLLAR_RE.findall(e_a.get("verbatim", ""))}
        a_dollars.discard(None)
        if not a_dollars:
            continue
        for e_b in entries[i + 1:]:
            shared_tags = set(e_a.get("tags") or []) & set(e_b.get("tags") or [])
            account_tags = [t for t in shared_tags
                            if isinstance(t, str) and len(t) >= 3
                            and t[0].isalpha()]
            if not account_tags:
                continue
            b_dollars = {_normalize_dollar(t) for t in
                         _CONTRADICTION_DOLLAR_RE.findall(e_b.get("verbatim", ""))}
            b_dollars.discard(None)
            if not b_dollars:
                continue
            common = a_dollars & b_dollars
            different = a_dollars ^ b_dollars
 # Only flag when we have at least one common metric and at least
 # one differing metric in the same paired account context.
            if different and not common:
                pairs.append({
                    "tag":     account_tags[0],
                    "entries": [e_a.get("entry_id"), e_b.get("entry_id")],
                    "values":  [sorted(a_dollars), sorted(b_dollars)],
                })
            elif common and different:
                pairs.append({
                    "tag":     account_tags[0],
                    "entries": [e_a.get("entry_id"), e_b.get("entry_id")],
                    "values":  [sorted(a_dollars), sorted(b_dollars)],
                })
    return pairs


# ─── retrieval (token-budget cap) ────────────────────────────────────────────

def _approx_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _format_entry_for_block(e: dict) -> str:
    """Format one entry into the EXPERIENCE EXTRAS block representation.

    Two shapes are handled here:
      - JSONL-backed entries (chat-captured): verbatim + tags + source_turn_ts
        + context_before/after — full referent preservation.
      - File-backed entries (file-authored from knowledge/experience/<source>.md):
        verbatim + source_name + dimension + label (BUILT/ACTUAL/etc) — no chat
        context (these are file-authored facts, not chat quotes).

    Branch on presence of `source_file` key (file-entry signature). The
    unified entry interface lets retrieve_for_role union both sources
    without duplicating packing/budget logic.
    """
    if "source_file" in e:
        return _format_file_entry_for_block(e)

 # JSONL-backed entry — original implementation, unchanged.
    tags = ", ".join(e.get("tags") or [])
    lines = [
        f"[{e.get('entry_id', '?')}] (tags: {tags or '—'})",
        f'  verbatim (Jordan, {e.get("source_turn_ts", "?")}): "{e.get("verbatim", "")}"',
    ]
    before = e.get("context_before") or []
    after  = e.get("context_after") or []
    if before:
        ctx_lines = []
        for t in before:
            who = t.get("role", "")
            txt = (t.get("text", "") or "")[:200]
            ctx_lines.append(f"    [{who}] {txt}")
        lines.append("  context_before:")
        lines.extend(ctx_lines)
    if after:
        ctx_lines = []
        for t in after:
            who = t.get("role", "")
            txt = (t.get("text", "") or "")[:200]
            ctx_lines.append(f"    [{who}] {txt}")
        lines.append("  context_after:")
        lines.extend(ctx_lines)
    return "\n".join(lines)


def _format_file_entry_for_block(e: dict) -> str:
    """Format a file-backed experience entry for the writer prompt.

    File entries are authored beats from knowledge/experience/<source>.md.
    They have no chat-history context (the integrity model is write-access,
    not substring-of-chat). The format surfaces source, dimension, and
    built-vs-actual label so writers and downstream readers can tell what
    they're consuming and at what authority level.
    """
    source = e.get("source_name", "?")
    dimension = e.get("dimension", "?")
    label = e.get("label", "?")
    text = e.get("verbatim", "")
    tags = ", ".join(e.get("tags") or [])
    return (
        f"[file:{source}] (dimension: {dimension}; label: {label}; tags: {tags or '—'})\n"
        f'  fact: "{text}"'
    )


# ─── file-authored experience source loader (A2 — lazy retrieve) ────────────

def _parse_yaml_frontmatter(text: str) -> tuple[dict, str]:
    """Parse minimal YAML-style frontmatter from a markdown file.

    Returns (frontmatter_dict, body). If no frontmatter (file does not
    start with `---\\n`), returns ({}, text).

    Supports the schema our experience files use: scalar `key: value` and
    list keys (`key:` followed by `  - item` lines). Does NOT support
    nested objects — they aren't needed for our schema. Keeping the parser
    minimal avoids pulling in PyYAML for one use.
    """
    if not text.startswith("---\n"):
        return ({}, text)
    end = text.find("\n---\n", 4)
    if end < 0:
        return ({}, text)
    fm_text = text[4:end]
    body = text[end + 5:]
    fm: dict = {}
    current_list_key: str | None = None
    for raw_line in fm_text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
 # List-item continuation: " - foo"
        stripped = raw_line.lstrip()
        if stripped.startswith("- ") and current_list_key is not None:
            fm[current_list_key].append(stripped[2:].strip())
            continue
 # key: value (scalar) OR key: (list)
        if ":" in raw_line:
            key, _, val = raw_line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                fm[key] = []
                current_list_key = key
            else:
                fm[key] = val
                current_list_key = None
    return (fm, body)


def _parse_experience_body(body: str) -> tuple[list[str], dict]:
    """Parse the body of an experience file.

    Returns (canonical_framing_claims, beats_by_dimension).

    canonical_framing_claims: list of strings (bullet items under
                              '## Canonical framing'). Skips placeholders
                              like '(empty…)' so an unpopulated file
                              contributes nothing.

    beats_by_dimension: {dimension_name: [{'label': 'BUILT', 'text': '...'}, ...]}
                        Only beats with a recognized BUILT/DESIGNED/ACTUAL/
                        ASPIRATIONAL prefix are included — the
                        anti-confabulation discipline requires every beat
                        be labeled. Beats without a label are silently
                        dropped (rather than included unlabeled, which
                        would defeat the label-required guard).
    """
    canonical: list[str] = []
    beats: dict[str, list[dict]] = {}

    current_h2: str | None = None
    current_h4: str | None = None

    label_pattern = re.compile(
        r"^(BUILT|DESIGNED|ACTUAL|ASPIRATIONAL)\s*[:\-—]\s*(.+)$",
        re.IGNORECASE,
    )

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
 # Track section state via markdown headers
        if line.startswith("## "):
            current_h2 = line[3:].strip()
            current_h4 = None
            continue
        if line.startswith("#### "):
            current_h4 = line[5:].strip()
            if current_h4 not in beats:
                beats[current_h4] = []
            continue
 # Bullet items
        if line.startswith("- ") or line.startswith("* "):
            content = line[2:].strip()
            if not content:
                continue
 # Skip placeholders like "(empty)" / "(empty in template — ...)"
            if content.startswith("(empty"):
                continue
 # Skip example-shape comment-style placeholders
            if content.startswith("<"):
                continue
 # In Canonical framing section
            if current_h2 and current_h2.lower().startswith("canonical"):
                canonical.append(content)
                continue
 # In a dimension section (under "Beats" parent)
            if current_h4 is not None:
                m = label_pattern.match(content)
                if m:
                    label = m.group(1).upper()
                    text = m.group(2).strip()
                    beats[current_h4].append({"label": label, "text": text})
 # Beats without a recognized label are dropped silently —
 # the anti-confabulation discipline requires labeling.

 # Drop dimensions with no beats
    beats = {k: v for k, v in beats.items() if v}
    return (canonical, beats)


def _load_experience_files(target_tag_set: set[str]) -> tuple[list[dict], list[dict]]:
    """Load file-authored experience entries from knowledge/experience/.

    Returns (entries, canonical_framing_claims).

    entries: synthetic dicts with `verbatim`, `tags`, `source_file`,
             `source_name`, `dimension`, `label`, `confirmed_at` — shaped
             so they flow through the same scoring/packing/verifier path
             as JSONL entries. The `source_file` key is the file-entry
             signature `_format_entry_for_block` branches on.

    canonical_framing_claims: dicts with `source` and `text` — surfaced
                              separately to the writer prompt as the
                              framing-lock block (A-rich design).

    File filtering:
      - Skip files starting with `_` (schema / docs / test entries)
      - Skip README.md
      - Include files whose role_relevance ∩ target_tag_set is non-empty.
        Empty target_tag_set means include all files (broad pull).

    ASPIRATIONAL beats are excluded from the entry pool — per the
    template's anti-confabulation discipline, they are not usable in
    submitted materials (so they should not reach the writer prompt OR
    the verifier as grounding).

    This is the A2 lazy-retrieve path: read at retrieve time, no caching,
    file is canonical for its content. The JSONL stays canonical for
    chat-captures. Both unioned in retrieve_for_role.
    """
    knowledge_dir = REPO_ROOT / "knowledge" / "experience"
    if not knowledge_dir.exists():
        return ([], [])

    entries: list[dict] = []
    canonical_claims: list[dict] = []

    for file_path in sorted(knowledge_dir.glob("*.md")):
 # Schema files, READMEs, and test entries (leading underscore) are skipped.
        if file_path.name.startswith("_") or file_path.name == "README.md":
            continue
        try:
            text = file_path.read_text()
        except Exception:
            continue

        fm, body = _parse_yaml_frontmatter(text)
        role_relevance = {t.lower() for t in (fm.get("role_relevance") or []) if t}

 # Filter by role-relevance intersection with target tags (empty target = include all).
        if target_tag_set and not (role_relevance & target_tag_set):
            continue

        source_name = file_path.stem
        canonical, beats = _parse_experience_body(body)

        for claim in canonical:
            canonical_claims.append({"source": source_name, "text": claim})

        mtime_iso = datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
        rel_path = str(file_path.relative_to(REPO_ROOT))
        for dimension, beats_in_dim in beats.items():
            for beat in beats_in_dim:
                if beat["label"] == "ASPIRATIONAL":
 # Not usable in submitted materials — skip.
                    continue
                dim_tag = dimension.lower().replace(" ", "_").replace("/", "_")
                entries.append({
                    "verbatim":     beat["text"],
                    "tags":         list(role_relevance) + [dim_tag],
                    "confirmed_at": mtime_iso,
                    "source_file":  rel_path,
                    "source_name":  source_name,
                    "dimension":    dimension,
                    "label":        beat["label"],
                })

    return (entries, canonical_claims)


def retrieve_for_role(
    *,
    target_tags: list[str] | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> tuple[str, list[dict], str]:
    """Retrieve confirmed experience entries for a target role.

    Returns (formatted_experience_block, used_entries, canonical_framing_block).

    UNIONS two population paths into the single `experience` source class:
      - JSONL-backed entries (chat-captured at experience-store.jsonl,
        with verbatim-substring-of-chat integrity enforcement)
      - File-backed entries (file-authored at knowledge/experience/<source>.md,
        with write-access integrity discipline)

    Both flow through the same scoring/packing/verifier path. The third
    return element — canonical_framing_block — comes from file sources
    ONLY and surfaces SEPARATELY to the writer prompt as its own block
    (above EXPERIENCE EXTRAS). This is the A-rich design: the canonical
    framing is the framing-lock, guaranteed to reach every writer as the
    framing they render from, not a beat competing for token-budget
    inclusion.

    Filtering: tag-intersection with target_tags. Empty/None target_tags
    means all non-superseded entries are eligible (broad pull). Files in
    knowledge/experience/ also match by their `role_relevance` frontmatter
    intersected with target_tags.

    Packing: greedy by score (tag overlap count) then recency, capped on
    token budget rather than entry count. Token budget = char-count // 4.

    Why token budget over entry count: 12 short entries cost the model
    very different attention than 1 long entry + 11 short. Pure entry-
    count caps either waste budget (cap too low) or blow up prompts (cap
    too high). Override on the original design's '12 entries' rule.
    """
    target_tag_set = {t.lower() for t in (target_tags or []) if t}

 # JSONL-backed (chat-captured) entries — existing path, unchanged.
    jsonl_entries = get_all_entries()

 # File-backed (file-authored) entries + canonical framing claims —
 # new path. A2 lazy retrieve: parse files at this call, no caching.
    file_entries, canonical_claims = _load_experience_files(target_tag_set)

 # Build canonical framing block. Surfaces as its OWN structural block
 # in the writer prompt (above EXPERIENCE EXTRAS) — never flattened in
 # with the beats. This is the framing-lock (A-rich): guaranteed to
 # reach every writer.
    if canonical_claims:
        cf_lines = [
            "CANONICAL FRAMING (locked — render every output from these claims consistently):",
            "",
        ]
        for claim in canonical_claims:
            cf_lines.append(f"- [{claim['source']}] {claim['text']}")
        canonical_framing_block = "\n".join(cf_lines)
    else:
        canonical_framing_block = ""

 # Union both source classes into a single pool for budgeted packing.
    entries = jsonl_entries + file_entries
    if not entries:
        return ("", [], canonical_framing_block)

    def _score(e: dict) -> tuple[int, str]:
 # Score = number of intersecting tags. Tiebreaker = recency
 # (confirmed_at descending — sort uses negated string lex order via
 # the reverse=True below since dates are ISO-sortable).
        e_tags = {t.lower() for t in (e.get("tags") or [])}
        overlap = len(e_tags & target_tag_set) if target_tag_set else 0
        return (overlap, e.get("confirmed_at", ""))

    sorted_entries = sorted(entries, key=_score, reverse=True)

 # When target_tag_set is empty, every entry has overlap=0 — recency wins.
 # When non-empty, prefer entries with at least one tag in common; never
 # deliver a zero-overlap entry if there are higher-scoring candidates.
    if target_tag_set:
        sorted_entries = [e for e in sorted_entries
                          if {t.lower() for t in (e.get("tags") or [])} & target_tag_set]

    block_parts: list[str] = []
    used: list[dict] = []
    running_tokens = 0
    for e in sorted_entries:
        formatted = _format_entry_for_block(e)
        cost = _approx_tokens(formatted)
        if running_tokens + cost > token_budget:
 # Token budget exhausted — return what we have. This is the
 # explicit override on entry-count caps.
            break
        block_parts.append(formatted)
        used.append(e)
        running_tokens += cost

    if not block_parts:
        return ("", [], canonical_framing_block)
    header = (
        "EXPERIENCE EXTRAS (CONFIRMED entries from two source paths: "
        "chat-captured via experience-store.jsonl (Jordan-verbatim with ±2-turn context); "
        "file-authored via knowledge/experience/<source>.md (BUILT/ACTUAL-labeled facts). "
        "Use these to ground specific claims):"
    )
    block = header + "\n\n" + "\n\n".join(block_parts)
    return (block, used, canonical_framing_block)


# ─── per-claim attribution helper (used by Stage 3) ──────────────────────────

def attribute_claim(claim_token: str, sources: dict) -> str | None:
    """Return the name of the first source whose blob contains the claim
    token (substring, normalized). Used by Stage 3 to record per-claim
    attribution in the verify metadata. Returns None if no source grounds
    the claim.

    `sources` is an ordered dict of {source_name: source_text}. Common
    order: 'experience' (most specific), 'history', 'jd', 'company'.
    """
    norm_claim = " ".join(claim_token.split()).lower()
    for name, blob in sources.items():
        if not blob:
            continue
        norm_blob = " ".join(blob.split()).lower()
        if norm_claim in norm_blob:
            return name
 # Dollar normalization mirroring prep_pipeline behavior
        if claim_token.startswith("$"):
            stripped = claim_token.rstrip("+").lower().replace(" ", "")
            if stripped in norm_blob.replace(" ", ""):
                return name
    return None


# ─── digest render ───────────────────────────────────────────────────────────

def render_digest_block(candidates: list[dict] | None = None) -> str:
    """Render the experience-candidates section for the morning digest.

    Jordan replies with 'confirm exp-cand-abc', 'edit exp-cand-abc tags=...'
    or 'reject exp-cand-abc' in the email reply or via chat tools. The
    candidate IDs are short enough to type without copy-paste.
    """
    if candidates is None:
        candidates = get_pending_candidates()
    if not candidates:
        return ""
    lines = [
        "EXPERIENCE CAPTURE — confirm or edit (silence for "
        f"{DIGEST_EXPIRY_THRESHOLD} digests = auto-reject):",
        "",
    ]
    for i, c in enumerate(candidates, start=1):
        cid     = c.get("candidate_id", "?")
        trigger = c.get("trigger", "?")
        verb    = (c.get("verbatim", "") or "").strip()
        tags    = ", ".join(c.get("suggested_tags") or [])
        summary = c.get("model_summary", "")
        score   = c.get("ambient_score")
        dcount  = c.get("digest_count", 0)
        lines.append(f"  {i}. [{cid}] trigger={trigger}"
                     + (f" score={score:.2f}" if isinstance(score, float) else "")
                     + (f" surfacings={dcount}" if dcount else ""))
        lines.append(f'     verbatim: "{verb[:240]}"')
        if summary:
            lines.append(f"     interpreted: {summary[:200]}")
        if tags:
            lines.append(f"     suggested tags: {tags}")
 # Show one line of preceding context if available — preserves referent
        before = c.get("context_before") or []
        if before:
            t = before[-1]
            who = t.get("role", "")
            txt = (t.get("text", "") or "")[:160]
            lines.append(f"     prior turn [{who}]: {txt}")
        lines.append(
            f"     Reply: confirm {cid}  |  reject {cid}  |  edit {cid} tags=tag1,tag2"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


# ─── reply parsing (called from imap_reply / telegram tools) ─────────────────

_REPLY_CONFIRM_RE = re.compile(
    r"\bconfirm\s+(exp-cand-[a-z0-9]+)\b", re.IGNORECASE,
)
_REPLY_REJECT_RE = re.compile(
    r"\breject\s+(exp-cand-[a-z0-9]+)\b", re.IGNORECASE,
)
_REPLY_EDIT_RE = re.compile(
    r"\bedit\s+(exp-cand-[a-z0-9]+)\s+tags\s*=\s*([^\n]+)",
    re.IGNORECASE,
)


def parse_and_apply_reply(reply_text: str) -> dict:
    """Parse the experience-confirmation directives in a reply text and
    apply them. Returns {confirmed: [...], rejected: [...], edited: [...]}.

    Intended to be called from imap_reply and from a chat tool. Idempotent
    on already-applied directives (re-confirming a confirmed candidate is
    a no-op that does not raise — the file scan is the audit).
    """
    out = {"confirmed": [], "rejected": [], "edited": [], "errors": []}
    if not reply_text:
        return out

 # Apply edits first so the tags are in place when we confirm.
    for m in _REPLY_EDIT_RE.finditer(reply_text):
        cid = m.group(1)
        tags_raw = m.group(2).strip()
        tags = [t.strip() for t in re.split(r"[,\s]+", tags_raw) if t.strip()]
        try:
            entry_id = confirm_candidate(cid, edited_tags=tags)
            out["edited"].append({"candidate_id": cid, "entry_id": entry_id,
                                  "tags": tags})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    for m in _REPLY_CONFIRM_RE.finditer(reply_text):
        cid = m.group(1)
 # Skip if already handled by an edit directive
        if any(d["candidate_id"] == cid for d in out["edited"]):
            continue
        try:
            entry_id = confirm_candidate(cid)
            out["confirmed"].append({"candidate_id": cid, "entry_id": entry_id})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    for m in _REPLY_REJECT_RE.finditer(reply_text):
        cid = m.group(1)
        try:
            reject_candidate(cid, reason="operator-reply")
            out["rejected"].append({"candidate_id": cid})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    return out


# ─── self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", action="store_true",
                    help="Show pending candidates")
    ap.add_argument("--entries", action="store_true",
                    help="Show all confirmed entries")
    ap.add_argument("--ambient", action="store_true",
                    help="Run ambient detector against recent Jordan turns")
    ap.add_argument("--ambient-n", type=int, default=20,
                    help="Number of recent turns to review")
    args = ap.parse_args()

    if args.pending:
        for c in get_pending_candidates():
            print(json.dumps(c, indent=2, default=str))
    elif args.entries:
        for e in get_all_entries():
            print(json.dumps(e, indent=2, default=str))
    elif args.ambient:
        history = _read_jsonl(HISTORY_PATH)
        user_turns = [t for t in history if t.get("role") == "user"]
        recent = user_turns[-args.ambient_n:]
        print(f"Reviewing {len(recent)} recent Jordan turns via Haiku...")
        ids = ambient_review(recent)
        print(f"Flagged {len(ids)} candidate(s): {ids}")
    else:
        print(f"experience_store: candidates={len(_read_jsonl(CANDIDATES_PATH))} "
              f"entries={len(_read_jsonl(STORE_PATH))}")
        print(f"  pending={len(get_pending_candidates())}")
