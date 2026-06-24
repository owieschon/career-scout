"""Alice quality evaluators — the shared measurement instrument.

Judge Alice's outputs against rubrics, per turn / per trace. This is the layer
model comparison and prompt/harness comparison both depend on: you cannot
compare quality without a quality measure. This module builds the measure; it
does not turn on continuous per-turn evaluation (that has its own cost budget).

Three evaluators:

  1. trust_correction. Did the user CORRECT/CONTRADICT a prior Alice claim,
     and did Alice UPDATE (good) or RE-ASSERT the contradicted claim (FAIL —
     including "you're right" then repeating the contradicted value)? LLM judge.

  2. did_she_answer. Did Alice address what the user actually asked, or redirect
     to her own agenda? LLM judge.

  3. voice (DETERMINISTIC, no LLM). Alice.md voice rules: no em-dash, no
     consulting-speak. Exact string checks = zero cost, fully reliable.

Notes:
  - trust_correction is inherently MULTI-TURN (correction in turn N, re-assertion
    in N+1). The captured trace spans truncate input.value and may be ephemeral,
    so trust_correction evaluates conversation WINDOWS from the untruncated,
    persistent telegram-history.jsonl (the source the spans derive from).
  - grounding detectors live in grounding.py (detect_category_mismatch,
    detect_specific_claims_without_tools) and are wrappable for a span-level
    grounding eval where tool_calls exist (see eval_grounding). Voice rules are
    prose in Alice.md, ported here as deterministic checks.
  - Judges use a cheap model (haiku) at temperature 0.
"""
import re

from alice import repo_paths

_JUDGE_MODEL = "claude-haiku-4-5-20251001"  # cheap judge; overridden per-call


# ─────────────────────────────────────────────────────────────────────────────
# 3. voice — DETERMINISTIC (no LLM). Alice.md: no em-dash, no consulting-speak.
# ─────────────────────────────────────────────────────────────────────────────
_CONSULTING_SPEAK = [
    "synergy", "synergies", "leverage", "leveraged", "leveraging",
    "deliver value", "at scale", "passionate about", "circle back",
    "low-hanging fruit", "move the needle", "boil the ocean", "value-add",
    "value add", "best-in-class", "deep dive into", "drill down",
    "touch base", "thought leadership", "deliverables",
]


def eval_voice(text):
    """Deterministic voice check. Returns {label: pass|fail, violations: [...]}."""
    text = text or ""
    violations = []
    if "—" in text:  # em-dash U+2014
        violations.append("em-dash present (Jordan does not use em-dashes)")
    low = text.lower()
    hits = sorted({w for w in _CONSULTING_SPEAK if w in low})
    if hits:
        violations.append("consulting-speak: " + ", ".join(hits))
    return {"evaluator": "voice",
            "label": "pass" if not violations else "fail",
            "violations": violations}


# ─────────────────────────────────────────────────────────────────────────────
# Shared cheap LLM judge.
# ─────────────────────────────────────────────────────────────────────────────
def _judge(task, system, prompt, max_tokens=140, model=None, job_key=None):
    from alice.llm import llm
    res = llm.call(task=task, prompt=prompt, system=system,
                   model=model or _JUDGE_MODEL, max_tokens=max_tokens, temperature=0.0,
                   job_key=job_key)   # threads the prediction id into the prediction span
    return (res.get("text") or "").strip()


def _verdict(judge_text):
    """Parse 'VERDICT: PASS|FAIL' (fail-safe: unparseable -> 'error', not a false pass)."""
    seg = judge_text.upper().split("VERDICT:")
    if len(seg) < 2:
        return "error"
    head = seg[1].strip()[:12]
    if head.startswith("FAIL"):
        return "fail"
    if head.startswith("PASS"):
        return "pass"
    return "error"


# ─────────────────────────────────────────────────────────────────────────────
# 2. did_she_answer — LLM judge.
# ─────────────────────────────────────────────────────────────────────────────
_ANSWER_SYS = (
    "You evaluate a job-search assistant. Given a USER message and the ASSISTANT "
    "response, decide whether the assistant ADDRESSED what the user actually asked "
    "or said.\n"
    "  PASS: the response engages the user's actual message.\n"
    "  FAIL: the assistant ignored the user's message, answered a different "
    "question, or redirected to its own agenda instead of responding.\n"
    "Reply EXACTLY with a line 'VERDICT: PASS' or 'VERDICT: FAIL', then one short "
    "reason line."
)


def eval_did_she_answer(user_message, assistant_response):
    out = _judge("eval_did_she_answer", _ANSWER_SYS,
                 f"USER:\n{user_message}\n\nASSISTANT:\n{assistant_response}\n\nVerdict:")
    return {"evaluator": "did_she_answer", "label": _verdict(out), "judge": out}


# ─────────────────────────────────────────────────────────────────────────────
# 1. trust_correction — LLM judge over a conversation WINDOW.
# ─────────────────────────────────────────────────────────────────────────────
_TRUST_SYS = (
    "You evaluate whether a job-search assistant accepts USER CORRECTIONS. You are "
    "given a short transcript window. Focus on the LAST user message and the "
    "assistant's reply to it.\n"
    "Step 1: In that last user message, is the user CORRECTING or CONTRADICTING a "
    "factual claim the assistant made earlier in the window (a status, a value, a "
    "state — e.g. 'no, that has not been submitted')?\n"
    "  - If there is NO such correction: VERDICT: PASS (nothing to test).\n"
    "Step 2: If there IS a correction, did the assistant's reply ACCEPT and UPDATE "
    "to the user's stated fact, or did it RE-ASSERT / insist on its prior "
    "now-contradicted claim? Note: saying 'you're right' and then repeating the "
    "contradicted claim COUNTS AS RE-ASSERTING (FAIL).\n"
    "  accepted + updated to the user's fact  -> VERDICT: PASS\n"
    "  re-asserted / insisted on the contradicted claim -> VERDICT: FAIL\n"
    "Reply EXACTLY with 'VERDICT: PASS' or 'VERDICT: FAIL', then one short reason."
)


def eval_trust_correction(window_turns):
    """window_turns: list of {role, text} for a conversation window, ending with
    the user correction + the assistant reply being judged."""
    transcript = "\n".join(
        f"[{t.get('role')}] {(t.get('text') or '')[:600]}" for t in window_turns)
    out = _judge("eval_trust_correction", _TRUST_SYS,
                 f"TRANSCRIPT WINDOW:\n{transcript}\n\nVerdict:")
    return {"evaluator": "trust_correction", "label": _verdict(out), "judge": out}


# ─────────────────────────────────────────────────────────────────────────────
# grounding — thin wrapper over the detectors in grounding.py. Needs tool_calls;
# specialized to the file-type/category-mismatch class. Available for span-level
# grounding eval.
# ─────────────────────────────────────────────────────────────────────────────
def eval_grounding(*, user_text, tool_calls_with_results, response_text):
    try:
        from alice.pipeline import grounding
        cm = grounding.detect_category_mismatch(
            user_text=user_text,
            tool_calls_with_results=tool_calls_with_results,
            response_text=response_text)
        return {"evaluator": "grounding",
                "label": "fail" if cm else "pass",
                "detail": cm}
    except Exception as e:
        return {"evaluator": "grounding", "label": "error", "detail": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# History helpers — load conversation windows for the multi-turn evaluators.
# ─────────────────────────────────────────────────────────────────────────────
_HISTORY = str(repo_paths.FEEDBACK / "telegram-history.jsonl")


def load_history(path=_HISTORY):
    import json
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def windows_ending_in_user_then_alice(turns, size=6):
    """Yield (window, alice_reply) for each user->alice adjacent pair, with up to
    `size` preceding turns of context. The window ends at the alice reply being
    judged; the immediately-preceding user turn is the candidate correction."""
    for i in range(1, len(turns)):
        if turns[i].get("role") == "alice" and turns[i - 1].get("role") == "user":
            start = max(0, i - size + 1)
            yield turns[start:i + 1], turns[i]
