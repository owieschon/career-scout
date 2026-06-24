"""Shared input-reader: "what does the operator want from this message".

Two readers of the operator's input — intent declarations and off-domain /
direct-question detection — consume this ONE classifier rather than each
defining its own. It populates both the `intent_declaration` and the
`is_direct_question` paths.

Fail-safe default (consistent with telegram_bot._select_relevant_observations'
default-to-exclusion): when nothing is clearly identifiable, return None / empty.
Never assume an intent the operator did not state.
"""
import re

# The intent vocabulary. The three not-advancing states are intentionally
# distinct — they differ in re-surface behavior:
# active = the operator is working it now (a sit is not disengagement)
# deliberating = deciding; sit is intentional (soft eventual re-surface)
# holding = intentionally parked (total mute, no re-surface)
# waiting = ball in their court (expected sit; flag only if cold)
# done = decided, not advancing (soft no, no terminal write)
INTENT_STATES = ("active", "deliberating", "holding", "waiting", "done")

# A single-line declaration like "holding: boreal flowcad" / "waiting = northwind".
# Public (no underscore) so directives.py consumes this classifier rather than
# defining a second one.
INTENT_DECL_RE = re.compile(
    r"^\s*(active|deliberating|holding|waiting|done)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Topic tokens — the shared home (seeded from telegram_bot._INTENT_TOPIC_RE) so
# the chat path and the reply path read topics the same way.
_TOPIC_RE = re.compile(
    r"\b(focus|prep|status|sheet|pipeline|resume|cover|application|"
    r"draft|outreach|score|interview|disclosure|travel|budget|comp|"
    r"runway|track|kill|criteria|warm|path|intent|holding|deliberating|waiting)\b",
    re.IGNORECASE,
)


def parse_intent_declaration(message):
    """Return {'intent': <state>, 'substr': <role substring>} if `message` is a
    single-line intent declaration, else None.

    Fail-safe: None when not clearly an intent declaration. Never guesses.
    """
    if not message:
        return None
    m = INTENT_DECL_RE.match(message.strip())
    if not m:
        return None
    return {"intent": m.group(1).lower(), "substr": m.group(2).strip()}


# ── Off-domain direct-question detector ───────────────────────────────────────
# The suppression trigger is NOT "a question" (that would strip context on
# "what's my focus?", which NEEDS it). It is an OFF-DOMAIN question — one whose
# answer does not depend on the job pipeline. The topic signal does the work:
# any pipeline-topic token => on-domain => keep context. Fail-safe: uncertain =>
# keep context (degrade to status-quo, never over-suppress).
_QUESTION_SHAPE_RE = re.compile(
    r"\?\s*$"  # ends with a question mark
    r"|^\s*(what|who|whom|whose|which|when|where|why|how|"
    r"do|does|did|are|is|am|can|could|would|will|should|have|has|"
    r"tell me|describe|explain)\b",
    re.IGNORECASE,
)

_OFFDOMAIN_SYSTEM = (
    "You classify a message sent to a job-search assistant. An OFF-DOMAIN "
    "question is one whose answer does NOT depend on the user's job pipeline, "
    "focus list, role statuses, applications, comp, or outreach data — e.g. "
    "questions about the assistant itself, meta/general/personal questions, or "
    "casual conversation. An ON-DOMAIN question is answered by the job-search "
    "data. Reply with ONE word: OFFDOMAIN or ONDOMAIN. When unsure, reply "
    "ONDOMAIN."
)


def _is_offdomain_semantic(message):
    """Haiku backstop. Returns True ONLY when confidently OFF-DOMAIN.

    Fail-safe: any error OR anything that is not an explicit OFFDOMAIN verdict
    resolves to False — i.e. keep the focus-context. A backstop that errored-open
    (True on failure) would SUPPRESS context on an error, which is the
    over-suppression failure this design exists to avoid.
    """
    try:
        from alice.llm import llm
        result = llm.call(
            task="offdomain_question_check",
            prompt=f"Message:\n{message.strip()}\n\nVerdict (OFFDOMAIN or ONDOMAIN):",
            system=_OFFDOMAIN_SYSTEM,
            max_tokens=8,
            temperature=0.0,
        )
        verdict = (result.get("text") or "").strip().upper()
        first = verdict.split()[0] if verdict.split() else ""
        return first == "OFFDOMAIN"  # anything else (incl. uncertainty) -> keep context
    except Exception as e:
        print(f"[operator_intent: offdomain backstop failed: {e!r}; keeping context]")
        return False


def detect_direct_question(message, topics):
    """Is `message` an OFF-DOMAIN direct question (answer independent of the job
    pipeline)? If True, the caller suppresses focus-context so the question
    dominates the generation.

    Fail-safe (uncertain KEEPS context):
      topics non-empty            -> on-domain          -> False (keep context)
      not question-shaped          -> not a question     -> False (keep context)
      question-shaped + no topics  -> Haiku backstop; True only if CONFIDENT
                                      off-domain, else False (keep context)
    'what's my focus?' carries the 'focus' topic -> always keeps context.
    """
    msg = (message or "").strip()
    if not msg:
        return False
    if topics:                                  # any pipeline topic -> on-domain
        return False
    if not _QUESTION_SHAPE_RE.search(msg):      # not question-shaped
        return False
    return _is_offdomain_semantic(msg)          # gated Haiku, fail-safe to False


def read_operator_intent(message):
    """The shared input-reader both the intent and direct-question paths consume.

    Returns:
      {
        "intent_declaration": {"intent", "substr"} | None,
        "topics":             set[str],
        "is_direct_question": bool,
      }

    `is_direct_question` is True only for OFF-DOMAIN questions (see
    detect_direct_question). The Haiku backstop is gated — it fires only for
    question-shaped, no-pipeline-topic messages, so most turns cost nothing.
    """
    msg = message or ""
    topics = set(t.lower() for t in _TOPIC_RE.findall(msg))
    return {
        "intent_declaration": parse_intent_declaration(msg),
        "topics": topics,
        "is_direct_question": detect_direct_question(msg, topics),
    }
