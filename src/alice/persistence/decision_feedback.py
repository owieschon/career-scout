"""Decision-feedback / correction store — durable record of where Jordan's
reads disagreed with Alice's.

Why this exists
---------------
Alice makes calls. Some land, some don't. When Jordan pushes back ("you're
wrong about X", "that's not what I said", "no, the situation is Y"),
that's a calibration signal — but today it lives only in chat history,
unindexed, unaggregated. By Friday it's six rapport-turns deep and
effectively invisible to the scorecard.

This module is the structured audit trail. Captured corrections support
ONE downstream use: PATTERN-SURFACING — across a recent window, where
did Alice's reads diverge from Jordan's? On what topics? What category of
error?

CRITICAL BOUNDARY (critical)
--------------------------------
This is a record of CORRECTIONS — not a predictive model of Jordan.

  - It captures what Jordan corrected, descriptively, so Alice can see her
    own error patterns.
  - It MUST NOT become a behavioral forecast of Jordan ("Jordan tends to
    reject X, so I should preempt"). That's the tiny-data trap and it's
    explicitly out of scope.
  - The `outcome` field is OPTIONAL and DESCRIPTIVE — what happened, if
    known. There is no outcome-PREDICTOR built on top of it.

Keep this descriptive, never predictive. Reread this comment before adding
any reader of the store that's not a pattern-surfacing aggregator.

Capture integrity (critical, mirrors experience_store)
----------------------------------------------------------
Every candidate's `operator_correction` field MUST be an exact substring of
a real Jordan turn in feedback/telegram-history.jsonl. The `alice_claim`
field (when alice_turn_ts is supplied) MUST be an exact substring of
the cited assistant turn. The substring check is enforced structurally
by `flag_correction_candidate` — it raises VerbatimMismatchError if
either verbatim text is not literally present in its cited turn.

A garbled record of what Jordan corrected — Alice's paraphrase masquerading
as Jordan's words — is a corrupt calibration signal. Worse than no record.
The substring seam makes paraphrase impossible at the API level.

Two triggers
------------
EXPLICIT — Jordan types a phrase like "log this correction", "you were
wrong about X", "that's wrong", "not what I said", "you're wrong",
"correction:". The current Jordan turn is the source of operator_correction;
the most recent prior assistant turn is the source of alice_claim.
Captured immediately, but the substring check still gates the write.

AMBIENT — a Haiku-based reviewer reads recent assistant+Jordan turn pairs
out-of-band, looking for disagreement / override / factual correction
patterns Jordan didn't flag verbally. The detector is intentionally
LIBERAL on candidate flagging; the morning digest is the pruning gate.

Categories (used for pattern bins)
----------------------------------
Defined small and stable so the Friday scorecard can aggregate cleanly
without a free-form-tag cardinality explosion:

  factual    — Alice asserted X; reality is Y. ("the vendor has 700 not 200")
  judgment   — Alice's read of a situation was off. ("that lead is hot" / "no")
  framing    — Alice characterized something in a way Jordan rejected.
  scope      — Alice exceeded her remit (life-coaching, therapist drift, etc).
  tone       — Alice's tone/register was wrong for the moment.
  other      — Doesn't fit any of the above; surfaced for manual recat.

Confirmation gate
-----------------
EXPLICIT-trigger candidates are higher confidence (Jordan actively asked
for the capture) but STILL land in the staging file and require digest
confirmation. The verbatim might still be the wrong span — the
substring check guarantees it's REAL, not that it's the RIGHT real span.

AMBIENT candidates always require confirmation. Silence for three
digests auto-expires (same threshold as experience_store).

Pattern-surfacing
-----------------
`pattern_summary(window_days=7)` returns a structured aggregate suitable
for the Friday scorecard:

  - count_by_category over the window
  - one representative verbatim per category (Jordan's words, not Alice's)
  - count_by_topic-token within each category (rough — extracted from
    Jordan's verbatim, not a learned embedding)

That's the structured "across recent corrections, here's where my reads
diverged from yours" pull the scorecard does.

Dual-surface wiring
-------------------
Chat path: telegram_bot detects explicit trigger → injects an anchor
naming recent turns by ts so the model can call flag_correction_candidate
with valid source_turn_ts values. parse_and_apply_reply handles the
"confirm corr-cand-xxx" reply directives.

Scorecard path: scorecard._correction_patterns() pulls
pattern_summary() and injects it into the metrics_summary block.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from alice import repo_paths


REPO_ROOT = Path(repo_paths.ROOT)
HISTORY_PATH       = REPO_ROOT / "feedback" / "telegram-history.jsonl"
CANDIDATES_PATH    = REPO_ROOT / "feedback" / "decision-feedback-candidates.jsonl"
STORE_PATH         = REPO_ROOT / "feedback" / "decision-feedback.jsonl"

# Auto-expire candidates that have appeared in this many morning digests
# without a reply (silence = rejection). Matches experience_store.
DIGEST_EXPIRY_THRESHOLD = 3

# Ambient detector params (Haiku review). Run out-of-band, never on the
# critical path of a user-facing turn.
_AMBIENT_MAX_TOKENS  = 800
_AMBIENT_TASK        = "decision_feedback_ambient_review"

# The closed set of categories. Stable + small so pattern aggregation
# remains meaningful with low sample counts. New categories require a
# code change — by design.
VALID_CATEGORIES = ("factual", "judgment", "framing", "scope", "tone", "other")


# ─── data classes ────────────────────────────────────────────────────────────

@dataclass
class CorrectionCandidate:
    """One staged candidate awaiting Jordan's confirmation in the morning digest."""
    candidate_id:     str
    created_at:       str
    trigger:          str   # "explicit" | "ambient"
 # The two anchored turns. alice_turn_ts may be None for standalone
 # corrections ("by the way you were wrong about X yesterday") where
 # the prior assistant turn isn't the one being corrected — in that
 # case alice_claim stores Jordan's framing of what Alice claimed, but
 # then alice_turn_ts MUST be None and we don't enforce substring on
 # alice_claim. Jordan's words are the essential seam; Alice's words
 # are only ever pulled verbatim from a real turn she actually emitted.
    alice_turn_ts:    str | None
    alice_claim:      str
    operator_turn_ts:     str
    operator_correction:  str
    category:         str   # one of VALID_CATEGORIES
    context_before:   list  # list of {role, text, ts}
    context_after:    list
    model_summary:    str = ""    # Alice-side gloss; NEVER aggregated for patterns
    ambient_score:    float | None = None
    digest_count:     int = 0
    status:           str = "pending"  # pending | confirmed | rejected | expired


@dataclass
class Correction:
    """One confirmed correction — the durable, queryable record."""
    decision_id:      str
    confirmed_at:     str
    from_candidate_id: str
    alice_turn_ts:    str | None
    alice_claim:      str
    operator_turn_ts:     str
    operator_correction:  str
    category:         str
    context_before:   list
    context_after:    list
 # Optional, descriptive — filled later by an explicit "outcome corr-xxx ..."
 # directive when Jordan sees how things played out. Never predicted.
    outcome:          str | None = None
    outcome_at:       str | None = None
    operator_edit_note:   str | None = None


# ─── JSONL plumbing (mirrors experience_store; intentional duplication) ──────

def _read_jsonl(path: Path) -> list[dict]:
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
                continue
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _rewrite_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    tmp_path.replace(path)


# ─── history lookup (the substring-match enforcement seam) ───────────────────

def _find_turn(turn_ts: str, role: str) -> dict | None:
    """Look up a turn by ts + role. Used by the capture-integrity check.

    Role check matters: an alice_claim must come from an 'assistant' turn,
    and an operator_correction must come from a 'user' turn. Cross-role
    citation is a category error and is rejected the same way paraphrase is.
    """
    for turn in _read_jsonl(HISTORY_PATH):
        if turn.get("ts") == turn_ts and turn.get("role") == role:
            return turn
    return None


def _surrounding_context(turn_ts: str, role: str, n_before: int = 2,
                          n_after: int = 2) -> tuple[list[dict], list[dict]]:
    """±n turns around the cited turn. Preserves the referent of the
    correction — "no, Lumen Search has 700" needs the surrounding turns to make
    sense of WHAT was being talked about."""
    history = _read_jsonl(HISTORY_PATH)
    target_idx = None
    for i, turn in enumerate(history):
        if turn.get("ts") == turn_ts and turn.get("role") == role:
            target_idx = i
            break
    if target_idx is None:
        return [], []
    before = history[max(0, target_idx - n_before): target_idx]
    after  = history[target_idx + 1: target_idx + 1 + n_after]
    def _slim(t: dict) -> dict:
        return {
            "role": t.get("role", ""),
            "ts":   t.get("ts", ""),
            "text": (t.get("text", "") or "")[:600],
        }
    return [_slim(t) for t in before], [_slim(t) for t in after]


# ─── capture integrity: the critical safeguard ───────────────────────────

class VerbatimMismatchError(ValueError):
    """Raised by flag_correction_candidate when a supplied verbatim is not
    a substring of its cited turn's text. Structural enforcement that
    makes paraphrase impossible at the API level."""


def flag_correction_candidate(
    *,
    operator_correction: str,
    operator_turn_ts: str,
    alice_claim: str = "",
    alice_turn_ts: str | None = None,
    category: str = "other",
    trigger: str = "explicit",
    model_summary: str = "",
    ambient_score: float | None = None,
) -> str:
    """Stage a new correction candidate. Substring-checks BOTH verbatim
    fields when a turn is cited.

    Returns the candidate_id assigned to the staged entry.

    Raises:
      ValueError on bad trigger / category / empty operator_correction.
      VerbatimMismatchError on substring failure (the structural seam).
    """
    if trigger not in ("explicit", "ambient"):
        raise ValueError(
            f"flag_correction_candidate: trigger must be 'explicit' or "
            f"'ambient'; got {trigger!r}"
        )
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"flag_correction_candidate: category must be one of "
            f"{VALID_CATEGORIES}; got {category!r}"
        )
    if not operator_correction or not operator_correction.strip():
        raise ValueError("flag_correction_candidate: operator_correction must be non-empty")
    if not operator_turn_ts or not operator_turn_ts.strip():
        raise ValueError("flag_correction_candidate: operator_turn_ts is required")

 # Substring check on Jordan's correction — the primary calibration signal.
    operator_turn = _find_turn(operator_turn_ts, role="user")
    if operator_turn is None:
        raise VerbatimMismatchError(
            f"flag_correction_candidate: no user turn with ts={operator_turn_ts!r} "
            f"found in {HISTORY_PATH.name}. operator_correction cannot be attributed "
            "to a real source turn. Refusing to stage."
        )
    operator_text = operator_turn.get("text", "") or ""
    if operator_correction not in operator_text:
        raise VerbatimMismatchError(
            f"flag_correction_candidate: operator_correction is NOT a substring of "
            f"the cited user turn at ts={operator_turn_ts}. The capture-integrity "
            "check rejected paraphrase at the API level.\n"
            f"  operator_correction (len={len(operator_correction)}): {operator_correction[:200]!r}\n"
            f"  operator_text head (len={len(operator_text)}): {operator_text[:200]!r}"
        )

 # Substring check on Alice's claim (only when a turn is cited; alice_claim
 # without alice_turn_ts is a free-text reconstruction Alice can store but
 # cannot anchor to a real turn — see the docstring on the citation pair).
    if alice_turn_ts:
        alice_turn = _find_turn(alice_turn_ts, role="assistant")
        if alice_turn is None:
            raise VerbatimMismatchError(
                f"flag_correction_candidate: no assistant turn with ts="
                f"{alice_turn_ts!r} found in {HISTORY_PATH.name}. alice_claim "
                "cannot be attributed to a real assistant turn. Refusing to stage."
            )
        alice_text = alice_turn.get("text", "") or ""
        if alice_claim and alice_claim not in alice_text:
            raise VerbatimMismatchError(
                f"flag_correction_candidate: alice_claim is NOT a substring of "
                f"the cited assistant turn at ts={alice_turn_ts}. Refusing to stage.\n"
                f"  alice_claim (len={len(alice_claim)}): {alice_claim[:200]!r}\n"
                f"  alice_text head (len={len(alice_text)}): {alice_text[:200]!r}"
            )

    before, after = _surrounding_context(operator_turn_ts, role="user")
    cand = CorrectionCandidate(
        candidate_id    = f"corr-cand-{uuid.uuid4().hex[:8]}",
        created_at      = datetime.now().isoformat(timespec="seconds"),
        trigger         = trigger,
        alice_turn_ts   = alice_turn_ts,
        alice_claim     = alice_claim or "",
        operator_turn_ts    = operator_turn_ts,
        operator_correction = operator_correction,
        category        = category,
        context_before  = before,
        context_after   = after,
        model_summary   = model_summary or "",
        ambient_score   = ambient_score,
        digest_count    = 0,
        status          = "pending",
    )
    _append_jsonl(CANDIDATES_PATH, asdict(cand))
    return cand.candidate_id


# ─── explicit trigger detection ──────────────────────────────────────────────

# Narrower than it could be. "I remember you said you were wrong" is NOT a
# correction trigger; "you were wrong about X" is. The seam keeps a casual
# reference from spawning a phantom candidate.

# Note: trailing \b removed because "correction:" ends in a non-word char,
# which would block the boundary check. Leading \b is enough to stop
# midword false-positives.
_EXPLICIT_TRIGGER_RE = re.compile(
    r"\b(?:log\s+this\s+correction|"
    r"you(?:'re|\s+are|\s+were)\s+wrong(?:\s+about)?|"
    r"that(?:'s|\s+is)\s+(?:not\s+(?:what\s+i\s+said|right|correct)|wrong|incorrect)|"
    r"not\s+what\s+i\s+said|"
    r"correction:|"
    r"that(?:'s|\s+is)\s+wrong|"
    r"you\s+(?:got|have)\s+(?:that|this)\s+wrong|"
    r"you\s+misread\s+(?:that|this|me)|"
    r"that(?:'s|\s+is)\s+a\s+misread)",
    re.IGNORECASE,
)


def detect_explicit_trigger(user_text: str) -> bool:
    """Return True if user_text contains an explicit correction trigger."""
    if not user_text:
        return False
    return _EXPLICIT_TRIGGER_RE.search(user_text) is not None


# ─── ambient detector (Haiku-based; out-of-band) ─────────────────────────────

_AMBIENT_REVIEW_PROMPT = """\
You are reviewing pairs of (assistant_turn, user_turn) from a chat. Your
job: identify cases where the USER is CORRECTING the assistant — pushing
back on a factual claim, judgment, framing, scope, or tone.

TUNE LIBERAL — surface the candidate even if uncertain. Jordan confirms or
rejects in the morning digest, so the cost of a false flag is one
review-click; the cost of missing a real correction is a lost calibration
signal forever.

WHAT TO FLAG (a USER correcting the ASSISTANT):
- factual disagreement ("no, X is Y not Z", "that's not the number")
- judgment override ("you're reading this wrong, the situation is...")
- framing rejection ("that's not how I'd describe it / what I meant")
- scope objection ("that's not your call to make", "drop the therapy")
- tone pushback ("don't cheerlead", "stop hedging")

WHAT TO SKIP:
- agreement / confirmation / "yes / right / exactly"
- clarification requests ("what do you mean by X?")
- new topic starts ("ok, separately —")
- generic complaints with no specific assistant claim to point at
- the assistant agreeing with itself / hedging — not a USER correction

For each candidate, output:
  - operator_turn_ts: ts of the USER turn that contains the correction
  - operator_correction_span: EXACT VERBATIM SUBSTRING of that user turn's
    text — the words Jordan used to correct. Do not paraphrase. Do not
    summarize in this field; summary goes in a separate field. The
    span MUST be an exact substring.
  - alice_turn_ts: ts of the ASSISTANT turn being corrected (the
    most recent assistant turn before the user correction)
  - alice_claim_span: EXACT VERBATIM SUBSTRING of that assistant turn —
    the specific words/claim Jordan is pushing back on. Must be a substring.
  - category: one of [factual, judgment, framing, scope, tone, other]
  - summary: one sentence — your gloss for Jordan's review only
  - score: 0.0-1.0 confidence this is a correction

Each input is given as:
TURN [role=<role> ts=<ts>]: <text>

Output JSON only — no markdown fences, no preamble:

{
  "candidates": [
    {
      "operator_turn_ts":         "<ts>",
      "operator_correction_span": "<exact substring of that user turn>",
      "alice_turn_ts":        "<ts>",
      "alice_claim_span":     "<exact substring of that assistant turn>",
      "category":             "factual|judgment|framing|scope|tone|other",
      "summary":              "<one sentence>",
      "score":                0.0-1.0
    }
  ]
}

If no corrections detected: {"candidates": []}.
"""


def _build_ambient_prompt(turns: list[dict]) -> str:
    lines = [_AMBIENT_REVIEW_PROMPT, "", "TURNS TO REVIEW (chronological):"]
    for t in turns:
        role = t.get("role", "")
        ts   = t.get("ts", "")
        text = (t.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"\nTURN [role={role} ts={ts}]: {text}")
    return "\n".join(lines)


def ambient_review(turns: list[dict], *, min_score: float = 0.5) -> list[str]:
    """Run the Haiku ambient detector over recent (assistant, user) turn
    pairs. The substring check at the seam catches hallucinated spans.

    Returns the list of candidate_ids that landed in staging.

    OUT OF BAND ONLY. Never on the chat critical path — the latency and
    cost budget for an interactive Jordan reply does not include an extra
    Haiku review call.
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
            tier="cheap",
        )
    except Exception:
        return []

    raw = (res.get("text") or "").strip()
    if not raw:
        return []

    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
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
        category = (cand.get("category") or "other").strip().lower()
        if category not in VALID_CATEGORIES:
            category = "other"
        try:
            cid = flag_correction_candidate(
                operator_correction = cand.get("operator_correction_span", "") or "",
                operator_turn_ts    = cand.get("operator_turn_ts", "") or "",
                alice_claim     = cand.get("alice_claim_span", "") or "",
                alice_turn_ts   = cand.get("alice_turn_ts") or None,
                category        = category,
                trigger         = "ambient",
                model_summary   = cand.get("summary", "") or "",
                ambient_score   = score,
            )
            out_ids.append(cid)
        except VerbatimMismatchError:
 # Model hallucinated a span — rejected at the seam. Not fatal
 # for the rest of the batch.
            continue
        except Exception:
            continue
    return out_ids


# ─── confirmation gate ───────────────────────────────────────────────────────

def get_pending_candidates() -> list[dict]:
    """Return all candidates with status='pending'."""
    return [c for c in _read_jsonl(CANDIDATES_PATH)
            if c.get("status") == "pending"]


def get_candidate(candidate_id: str) -> dict | None:
    for c in _read_jsonl(CANDIDATES_PATH):
        if c.get("candidate_id") == candidate_id:
            return c
    return None


def _update_candidate(candidate_id: str, mutate) -> dict | None:
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
    """Increment digest_count. After threshold, status flips to expired."""
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


def confirm_correction(
    candidate_id: str,
    *,
    edited_category: str | None = None,
    edited_note: str | None = None,
) -> str:
    """Promote a pending candidate to a durable correction.

    The verbatim fields are NEVER edited at confirmation — that would
    break the substring invariant. To change a captured span, reject
    and re-flag with the right span.

    Returns the decision_id of the new durable record.
    """
    cand = get_candidate(candidate_id)
    if cand is None:
        raise ValueError(f"confirm_correction: no candidate with id {candidate_id!r}")
    if cand.get("status") != "pending":
        raise ValueError(
            f"confirm_correction: candidate {candidate_id!r} is in status "
            f"{cand.get('status')!r}; only 'pending' can be confirmed."
        )

    category = edited_category if edited_category else cand.get("category", "other")
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"confirm_correction: category must be one of {VALID_CATEGORIES}; "
            f"got {category!r}"
        )

    rec = Correction(
        decision_id       = f"corr-{uuid.uuid4().hex[:8]}",
        confirmed_at      = datetime.now().isoformat(timespec="seconds"),
        from_candidate_id = candidate_id,
        alice_turn_ts     = cand.get("alice_turn_ts"),
        alice_claim       = cand.get("alice_claim", ""),
        operator_turn_ts      = cand.get("operator_turn_ts", ""),
        operator_correction   = cand.get("operator_correction", ""),
        category          = category,
        context_before    = cand.get("context_before", []),
        context_after     = cand.get("context_after", []),
        operator_edit_note    = edited_note,
    )
    _append_jsonl(STORE_PATH, asdict(rec))

    def _mut(r):
        r["status"] = "confirmed"
        r["confirmed_decision_id"] = rec.decision_id
    _update_candidate(candidate_id, _mut)
    return rec.decision_id


def reject_correction(candidate_id: str, reason: str = "") -> None:
    """Mark a candidate rejected. Kept in staging as audit, never surfaces."""
    cand = get_candidate(candidate_id)
    if cand is None:
        raise ValueError(f"reject_correction: no candidate with id {candidate_id!r}")
    def _mut(r):
        r["status"] = "rejected"
        if reason:
            r["reject_reason"] = reason
    _update_candidate(candidate_id, _mut)


# ─── outcome attachment (DESCRIPTIVE only) ───────────────────────────────────

def attach_outcome(decision_id: str, outcome_text: str) -> None:
    """Attach a descriptive outcome to a confirmed correction.

    DESIGN BOUNDARY: outcomes are FREE-TEXT and DESCRIPTIVE. They are
    surfaced in pattern_summary alongside Jordan's correction so a reader
    can see "Alice said X, Jordan corrected to Y, here's what actually
    happened." They are NOT inputs to any predictive scoring or
    behavior model. If a future reader tries to aggregate outcomes
    into a 'success rate' or feed them into a recommender, that's the
    tiny-data trap arriving. Push back.
    """
    records = _read_jsonl(STORE_PATH)
    found = False
    for r in records:
        if r.get("decision_id") == decision_id:
            r["outcome"]    = outcome_text
            r["outcome_at"] = datetime.now().isoformat(timespec="seconds")
            found = True
            break
    if not found:
        raise ValueError(f"attach_outcome: no correction with id {decision_id!r}")
    _rewrite_jsonl(STORE_PATH, records)


# ─── store reads + queries ───────────────────────────────────────────────────

def get_all_corrections() -> list[dict]:
    return _read_jsonl(STORE_PATH)


def get_correction(decision_id: str) -> dict | None:
    for r in _read_jsonl(STORE_PATH):
        if r.get("decision_id") == decision_id:
            return r
    return None


def query_by_category(category: str) -> list[dict]:
    """Return all confirmed corrections in the named category."""
    if category not in VALID_CATEGORIES:
        raise ValueError(f"query_by_category: bad category {category!r}")
    return [r for r in get_all_corrections() if r.get("category") == category]


def recent_corrections(window_days: int = 7) -> list[dict]:
    """Return confirmed corrections within the trailing window."""
    cutoff = datetime.now() - timedelta(days=window_days)
    out = []
    for r in get_all_corrections():
        ts = r.get("confirmed_at", "")
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if t >= cutoff:
            out.append(r)
    return out


# ─── pattern surfacing (the Friday scorecard's structured pull) ──────────────

# Very rough topic extraction — pulls capitalized noun tokens and dollar
# tokens out of Jordan's correction text. The scorecard surface that uses
# this is a HUMAN-FACING aggregate; precision doesn't need to be high. A
# missed cluster is fine — Jordan sees the verbatim quote underneath.
_TOPIC_TOKEN_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z][A-Za-z0-9]{2,}|\$\d+(?:\.\d+)?[KMB]?)\b"
)
# Stopwords — common verbatim-leading capitalized words that aren't topics
_TOPIC_STOPWORDS = {
    "I", "Im", "Ive", "Id", "Ill", "You", "Youre", "Youve", "Your",
    "The", "This", "That", "These", "Those", "Not", "No", "Yes",
    "But", "And", "Or", "Actually", "Wait", "Hey", "Ok", "OK", "Okay",
    "What", "Where", "When", "Why", "How", "Who", "Which",
}


def _extract_topic_tokens(text: str) -> list[str]:
    if not text:
        return []
    tokens = _TOPIC_TOKEN_RE.findall(text)
    out = []
    seen = set()
    for tok in tokens:
        if tok in _TOPIC_STOPWORDS:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def pattern_summary(window_days: int = 7) -> dict:
    """Structured pull for the Friday scorecard.

    Returns:
      {
        "window_days":         int,
        "total":               int,
        "count_by_category":   {cat: int, ...},
        "by_category": {
          cat: {
            "count":         int,
            "representative_quote":  str,    # Jordan's words
            "representative_outcome": str|None,
            "topic_tokens":  [str, ...],
          },
          ...
        },
      }

    "representative_quote" is the most recent confirmed correction in the
    category; "topic_tokens" is the deduped union of rough topic tokens
    across all corrections in the category, within the window.

    DESIGN BOUNDARY: this returns a *descriptive aggregate*. The
    scorecard prompt is what turns it into prose. If you find yourself
    adding a 'predicted_operator_response' field here, stop — see the module
    docstring.
    """
    recent = recent_corrections(window_days)
    by_cat: dict = {}
    for r in recent:
        cat = r.get("category", "other")
        by_cat.setdefault(cat, []).append(r)

    out_by_cat: dict = {}
    for cat, rows in by_cat.items():
 # Most recent first for representative_quote
        rows_sorted = sorted(rows, key=lambda r: r.get("confirmed_at", ""),
                             reverse=True)
        head = rows_sorted[0]
        topic_tokens: list[str] = []
        seen = set()
        for r in rows_sorted:
            for tok in _extract_topic_tokens(r.get("operator_correction", "")):
                if tok.lower() in seen:
                    continue
                seen.add(tok.lower())
                topic_tokens.append(tok)
        out_by_cat[cat] = {
            "count":                   len(rows),
            "representative_quote":    head.get("operator_correction", ""),
            "representative_outcome":  head.get("outcome"),
            "topic_tokens":            topic_tokens[:10],  # cap for readability
        }
    return {
        "window_days":       window_days,
        "total":             len(recent),
        "count_by_category": {c: len(rs) for c, rs in by_cat.items()},
        "by_category":       out_by_cat,
    }


def render_pattern_summary(window_days: int = 7) -> str:
    """Human-readable pattern summary for the scorecard prompt block.

    A `summary["total"] == 0` window returns the empty string — the
    scorecard caller can decide whether to inject a 'no corrections
    captured this week' line or simply omit the section.
    """
    summary = pattern_summary(window_days)
    if summary["total"] == 0:
        return ""
    lines = [
        f"CORRECTION PATTERNS (confirmed in last {window_days} days; "
        f"total: {summary['total']}):",
    ]
    for cat in VALID_CATEGORIES:
        if cat not in summary["by_category"]:
            continue
        block = summary["by_category"][cat]
        lines.append(f"  [{cat}] count={block['count']}")
        quote = (block.get("representative_quote", "") or "").strip()
        if quote:
            lines.append(f'    most-recent Jordan quote: "{quote[:240]}"')
        if block.get("representative_outcome"):
            lines.append(f"    outcome (descriptive): "
                         f"{block['representative_outcome'][:200]}")
        if block.get("topic_tokens"):
            lines.append(f"    topics: {', '.join(block['topic_tokens'])}")
    return "\n".join(lines)


# ─── digest render ───────────────────────────────────────────────────────────

def render_digest_block(candidates: list[dict] | None = None) -> str:
    """Render the correction-candidates section for the morning digest.

    Jordan replies with 'confirm corr-cand-abc', 'reject corr-cand-abc', or
    'edit corr-cand-abc category=<cat>'.
    """
    if candidates is None:
        candidates = get_pending_candidates()
    if not candidates:
        return ""
    lines = [
        "CORRECTION CAPTURE — confirm or edit (silence for "
        f"{DIGEST_EXPIRY_THRESHOLD} digests = auto-reject):",
        "",
    ]
    for i, c in enumerate(candidates, start=1):
        cid     = c.get("candidate_id", "?")
        trigger = c.get("trigger", "?")
        cat     = c.get("category", "?")
        verb    = (c.get("operator_correction", "") or "").strip()
        claim   = (c.get("alice_claim", "") or "").strip()
        summary = c.get("model_summary", "")
        score   = c.get("ambient_score")
        dcount  = c.get("digest_count", 0)
        lines.append(f"  {i}. [{cid}] trigger={trigger} category={cat}"
                     + (f" score={score:.2f}" if isinstance(score, float) else "")
                     + (f" surfacings={dcount}" if dcount else ""))
        if claim:
            lines.append(f'     alice said: "{claim[:200]}"')
        lines.append(f'     jordan corrected: "{verb[:240]}"')
        if summary:
            lines.append(f"     interpreted: {summary[:200]}")
        lines.append(
            f"     Reply: confirm {cid}  |  reject {cid}  "
            f"|  edit {cid} category=<one of {','.join(VALID_CATEGORIES)}>"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


# ─── reply parsing (chat tools + imap_reply) ─────────────────────────────────

_REPLY_CONFIRM_RE = re.compile(
    r"\bconfirm\s+(corr-cand-[a-z0-9]+)\b", re.IGNORECASE,
)
_REPLY_REJECT_RE = re.compile(
    r"\breject\s+(corr-cand-[a-z0-9]+)\b", re.IGNORECASE,
)
_REPLY_EDIT_RE = re.compile(
    r"\bedit\s+(corr-cand-[a-z0-9]+)\s+category\s*=\s*([A-Za-z]+)",
    re.IGNORECASE,
)
# Outcome attachment lives on the durable record (corr-xxx, not corr-cand-xxx).
_REPLY_OUTCOME_RE = re.compile(
    r"\boutcome\s+(corr-[a-z0-9]+)\s*:\s*([^\n]+)", re.IGNORECASE,
)


def parse_and_apply_reply(reply_text: str) -> dict:
    """Parse and apply correction-related directives in a reply.

    Returns {confirmed, rejected, edited, outcomes, errors}. Idempotent
    on already-applied directives.
    """
    out = {"confirmed": [], "rejected": [], "edited": [], "outcomes": [],
           "errors": []}
    if not reply_text:
        return out

 # Edits first so category is in place when confirm runs.
    for m in _REPLY_EDIT_RE.finditer(reply_text):
        cid = m.group(1)
        cat = m.group(2).strip().lower()
        try:
            decision_id = confirm_correction(cid, edited_category=cat)
            out["edited"].append({"candidate_id": cid,
                                  "decision_id": decision_id,
                                  "category": cat})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    for m in _REPLY_CONFIRM_RE.finditer(reply_text):
        cid = m.group(1)
        if any(d["candidate_id"] == cid for d in out["edited"]):
            continue
        try:
            decision_id = confirm_correction(cid)
            out["confirmed"].append({"candidate_id": cid,
                                     "decision_id": decision_id})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    for m in _REPLY_REJECT_RE.finditer(reply_text):
        cid = m.group(1)
        try:
            reject_correction(cid, reason="operator-reply")
            out["rejected"].append({"candidate_id": cid})
        except ValueError as e:
            out["errors"].append({"candidate_id": cid, "error": str(e)})

    for m in _REPLY_OUTCOME_RE.finditer(reply_text):
        did = m.group(1)
        text = m.group(2).strip()
        try:
            attach_outcome(did, text)
            out["outcomes"].append({"decision_id": did, "outcome": text})
        except ValueError as e:
            out["errors"].append({"decision_id": did, "error": str(e)})

    return out


# ─── self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", action="store_true",
                    help="Show pending candidates")
    ap.add_argument("--corrections", action="store_true",
                    help="Show all confirmed corrections")
    ap.add_argument("--patterns", action="store_true",
                    help="Show pattern summary for the last 7 days")
    ap.add_argument("--ambient", action="store_true",
                    help="Run ambient detector against recent turn pairs")
    ap.add_argument("--ambient-n", type=int, default=30,
                    help="Number of recent turns to review")
    args = ap.parse_args()

    if args.pending:
        for c in get_pending_candidates():
            print(json.dumps(c, indent=2, default=str))
    elif args.corrections:
        for r in get_all_corrections():
            print(json.dumps(r, indent=2, default=str))
    elif args.patterns:
        print(render_pattern_summary(7) or "(no corrections in window)")
    elif args.ambient:
        history = _read_jsonl(HISTORY_PATH)
        recent = history[-args.ambient_n:]
        print(f"Reviewing {len(recent)} recent turns via Haiku...")
        ids = ambient_review(recent)
        print(f"Flagged {len(ids)} candidate(s): {ids}")
    else:
        print(f"decision_feedback: candidates={len(_read_jsonl(CANDIDATES_PATH))} "
              f"corrections={len(_read_jsonl(STORE_PATH))}")
        print(f"  pending={len(get_pending_candidates())}")
