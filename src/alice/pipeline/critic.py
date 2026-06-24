"""Adversarial critic.

Job: find SPECIFIC FACTUAL MISMATCHES between a claim and a ground truth.
NOT to review. NOT to evaluate quality. NOT to "see if it looks good."

A critic that returns "looks good" is broken. A critic that returns
"the claim says X, the source says Y, mismatch" is working. The success
metric is mismatches caught against deliberately-flawed test artifacts —
not whether the output reads like a thoughtful review.

The structural reason this is its own module: the failure mode of LLM
self-review is producing confident-sounding evaluation text that has no
grounding. Naming the critic adversarial AND scoping its output to "list
specific mismatches" defangs that failure mode. The critic is allowed —
required, even — to return [] when there are no mismatches. Empty list
is correct; manufactured-mismatch is wrong.

Public surface:
    falsify(claim, ground_truth)              → returns dict with mismatches
    falsify(claim, ground_truth, schema=...)  → enforces a stricter shape

For now, ground_truth is passed as a string. A Phase-2 variant where the
critic gets tool-using independent access to files/sheet/tool results
will be added once the synchronous version has proven its discipline.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from alice.llm import llm


_SYSTEM_PROMPT = """You are an adversarial fact-checker. Your job is NOT to
review, evaluate, or summarize. Your job is to find SPECIFIC FACTUAL
MISMATCHES between a CLAIM and a GROUND TRUTH.

Rules:
1. Only mismatches that can be pinned to specific text in BOTH the claim
   and the ground truth count. If you cannot quote both sides, it does
   not count as a mismatch.
2. If there are no factual mismatches, return an empty mismatches list.
   "No mismatches found" is the correct answer when the claim agrees
   with the ground truth. Do NOT manufacture mismatches to look useful.
3. Do NOT produce evaluative commentary. Do NOT say "looks good" or
   "needs improvement." Do NOT score the claim. Do NOT suggest fixes.
   Your output is mismatches or nothing.
4. Differences in style, voice, framing, or emphasis are NOT mismatches.
   Only factual disagreements about what is true.
5. The claim being SILENT about a fact in the ground truth is NOT a
   mismatch unless the claim makes a contradicting positive assertion.

Return ONLY a JSON object of the form:
{
  "mismatches": [
    {
      "claim_says": "<exact or near-exact text from the claim>",
      "truth_says": "<exact or near-exact text from the ground truth>",
      "type":       "<one of: factual_contradiction | wrong_number | wrong_name | wrong_date | wrong_attribution | other>",
      "severity":   "<one of: high | medium | low>"
    },
    ...
  ]
}

No other text. No prose around the JSON. No prefixes like "Here is the result:".
"""


def _parse_strict_json(text: str) -> dict:
    """Tolerate trailing whitespace and prose after a valid JSON object.
    Same pattern as the chat parser's raw_decode discipline (handles LLMs
    that occasionally append commentary after the JSON they were told not
    to append to).
    """
    text = text.strip()
    if not text:
        raise ValueError("critic returned empty text")
    decoder = json.JSONDecoder()
 # If the text begins with a code fence, strip it.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    obj, _idx = decoder.raw_decode(text)
    if not isinstance(obj, dict):
        raise ValueError(f"critic output is not a JSON object: {type(obj).__name__}")
    return obj


def falsify(claim: str, ground_truth: str,
            *,
            task: str = "adversarial_critic",
            context_label: str | None = None,
            max_tokens: int = 4096) -> dict:
    """Run the adversarial critic. Returns:
        {
          "mismatches":   [...],   # may be empty (== "no flaws found")
          "ok":           bool,    # True iff mismatches is []
          "raw_response": str,     # the model's raw text (for audit)
          "model":        str,
          "rounds":       int,
          "thinking_tokens": int,
          "cost_usd":     float,
          "checked_at":   iso ts,
        }

    `task` defaults to "adversarial_critic" which select_call_config maps to
    Opus 4.8 + effort=high. Override only for testing or cost experiments —
    cheap-tier critics tend to manufacture "looks good" output, which is
    exactly the failure mode this module exists to prevent.
    """
    if not claim.strip():
        raise ValueError("falsify: claim is empty; nothing to check")
    if not ground_truth.strip():
        raise ValueError("falsify: ground_truth is empty; cannot falsify against nothing")

    label = f" ({context_label})" if context_label else ""
    user_prompt = (
        f"CLAIM{label}:\n"
        f"\"\"\"\n{claim.strip()}\n\"\"\"\n\n"
        f"GROUND TRUTH:\n"
        f"\"\"\"\n{ground_truth.strip()}\n\"\"\"\n\n"
        f"Find every factual mismatch. Quote both sides. Return JSON only."
    )

    result = llm.call(
        task,
        user_prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )

    raw = result["text"]
    try:
        parsed = _parse_strict_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
 # Fail loud: a critic that returns un-parseable output is broken.
 # We don't try to extract from prose — that's an invitation to
 # manufactured mismatches getting through partial parses.
        raise RuntimeError(
            f"critic.falsify: model returned un-parseable JSON. "
            f"This violates the JSON-only contract in the system prompt. "
            f"Raw (truncated): {raw[:300]!r}"
        ) from e

    mismatches = parsed.get("mismatches", [])
    if not isinstance(mismatches, list):
        raise RuntimeError(
            f"critic.falsify: 'mismatches' field is not a list: "
            f"{type(mismatches).__name__}"
        )

 # Light validation of each mismatch — surfaces malformed entries before
 # callers act on them.
    for i, m in enumerate(mismatches):
        if not isinstance(m, dict):
            raise RuntimeError(f"critic.falsify: mismatch[{i}] is not a dict")
        for required in ("claim_says", "truth_says", "type"):
            if required not in m:
                raise RuntimeError(
                    f"critic.falsify: mismatch[{i}] missing '{required}': {m}"
                )

    return {
        "mismatches":      mismatches,
        "ok":              len(mismatches) == 0,
        "raw_response":    raw,
        "model":           result["model"],
        "rounds":          result["rounds"],
        "thinking_tokens": result.get("thinking_tokens", 0),
        "cost_usd":        result["cost_usd"],
        "checked_at":      datetime.now().isoformat(timespec="seconds"),
    }
