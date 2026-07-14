"""Grounding / fabrication-proxy detectors.

Two detectors for the file-claim fabrication class:

  1. category_mismatch — user asked about category X (file type, count,
     specific entity); the tools' return shape did NOT contain X; the
     response asserts a match for X anyway. Example: user asks for "pdf
     files", list_dir returns only .docx, response says "I found 4 PDF files".

  2. specific_claims_without_tools — the response asserts specific
     filenames / companies / dates / counts AND zero tools fired this turn.
     This is the shape where the model fabricates a filename like
     `resume-master-vc.pdf` with no tool calls behind it.

Both detectors are deterministic Python over turn-data. They are NOT LLM
judges — that would just push the confabulation risk one layer up. They
look for STRUCTURAL signatures: tokens in user text, file extensions in
tool returns, file-extension-shaped tokens in response text.

The gate that earns trust: run_known_positive_gate() must return a positive
flag for the exact "I found 4 PDF files / technically .docx" turn. If it
doesn't, the detector is broken, full stop.
"""
from __future__ import annotations

import re
from typing import Any


# File-type tokens — what the user asked about and what we look for in claims.
# Pattern: standalone token bounded by word boundaries, case-insensitive.
_FILE_TYPE_TOKENS = ("pdf", "docx", "doc", "txt", "md", "json", "yaml", "yml",
                     "py", "sh", "csv", "tsv", "html", "xml", "xlsx", "xls")


def extract_file_types_from_text(text: str) -> set[str]:
    """Return the set of file-type tokens that appear as standalone words
    in `text`. Case-insensitive. Used on both user text (to find what was
    asked about) and response text (to find what was claimed)."""
    if not text:
        return set()
    out = set()
    lowered = text.lower()
    for tok in _FILE_TYPE_TOKENS:
 # \b for word boundary; tokens are short so we want exact-word match.
        if re.search(rf"\b{re.escape(tok)}\b", lowered):
            out.add(tok)
    return out


def extract_file_types_from_tool_returns(tool_calls: list[dict]) -> dict:
    """For each tool call's RETURN value, extract observed file extensions.

    Returns: {"observed_types": set[str], "had_listing_tool": bool, "total_files_seen": int}.

    Handles the shape of list_dir's return (entries with `name` keys) and
    read_file's return (a single `path`). Other tools contribute zero
    observations; that's fine — they don't make file-type claims.
    """
    observed = set()
    had_listing = False
    total = 0
    for call in tool_calls or []:
        name = call.get("name", "")
        result = call.get("result")
        if result is None:
            continue
        if name == "list_dir" and isinstance(result, dict):
            had_listing = True
            for entry in result.get("entries", []) or []:
                if not isinstance(entry, dict):
                    continue
                ename = entry.get("name", "")
                if not ename or not entry.get("is_file", False):
                    continue
                total += 1
 # File extension is everything after the last '.'.
                if "." in ename:
                    ext = ename.rsplit(".", 1)[1].lower()
                    if ext in _FILE_TYPE_TOKENS:
                        observed.add(ext)
        elif name == "read_file" and isinstance(result, dict):
            path = result.get("path", "")
            if path and "." in path:
                ext = path.rsplit(".", 1)[1].lower()
                if ext in _FILE_TYPE_TOKENS:
                    observed.add(ext)
    return {
        "observed_types":    observed,
        "had_listing_tool":  had_listing,
        "total_files_seen":  total,
    }


# Claim patterns: "N PDF files" / "(N) pdf files" / "I found a docx file"
_CLAIM_COUNT_RE = re.compile(
    r"\b(?:i\s+found\s+|i\s+see\s+|there\s+are\s+|i\s+have\s+|here\s+are\s+)"
    r"(?:\(?\d+\)?\s+)?"      # optional count
    r"([a-z]{2,5})\s+files?\b",
    re.IGNORECASE,
)


def extract_file_type_claims_from_response(response_text: str) -> set[str]:
    """Find file-type tokens that the response ASSERTS as the type of
    something it found/sees. E.g. 'I found 4 PDF files' → {'pdf'}."""
    if not response_text:
        return set()
    claimed = set()
    for m in _CLAIM_COUNT_RE.finditer(response_text):
        tok = m.group(1).lower()
        if tok in _FILE_TYPE_TOKENS:
            claimed.add(tok)
    return claimed


def detect_category_mismatch(*, user_text: str, tool_calls_with_results: list[dict],
                              response_text: str) -> dict | None:
    """Return a mismatch dict if the response asserts a file-type that the
    user asked about but the tools didn't actually return. None otherwise.

    The known-positive shape:
      user_text   asks "pdf"
      tool returns observed_types = {"docx"} (no pdf)
      response    asserts "pdf" in "I found 4 PDF files"
      → MISMATCH FLAGGED
    """
    asked = extract_file_types_from_text(user_text)
    if not asked:
        return None
    observed = extract_file_types_from_tool_returns(tool_calls_with_results)
    claimed = extract_file_type_claims_from_response(response_text)
    if not claimed:
        return None

 # If a claimed type matches what the user asked but is NOT in observed
 # types from the tools, that's the mismatch shape.
    mismatched_claims = []
    for type_token in claimed:
        if type_token in asked and type_token not in observed["observed_types"]:
            mismatched_claims.append(type_token)

    if not mismatched_claims:
        return None

    return {
        "kind":            "category_mismatch",
        "asked_types":     sorted(asked),
        "observed_types":  sorted(observed["observed_types"]),
        "claimed_types":   sorted(claimed),
        "mismatched":      sorted(mismatched_claims),
        "had_listing_tool": observed["had_listing_tool"],
        "total_files_seen": observed["total_files_seen"],
    }


# Patterns that look like specific filenames or other concrete claims:
# tokens with extensions, ALL-CAPS company-sounding words, specific dates.
_FILENAME_RE = re.compile(r"\b[A-Za-z0-9_\-]{3,}\.[a-z]{2,5}\b")
_SPECIFIC_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SPECIFIC_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


def detect_specific_claims_without_tools(*, tool_calls: list[dict],
                                          response_text: str) -> dict | None:
    """If zero tools fired AND the response asserts specific filenames,
    dates, or times, flag as a possible day-one-class fabrication.

    Not all no-tool turns are bad — boundary statements are correct
    behavior. The signature this watches for is no-tool + concrete detail,
    e.g. a fabricated filename like `resume-master-vc.pdf`."""
    if tool_calls:
        return None  # tools fired; different failure class
    if not response_text:
        return None
    filenames = set(_FILENAME_RE.findall(response_text))
    dates = set(_SPECIFIC_DATE_RE.findall(response_text))
    times = set(_SPECIFIC_TIME_RE.findall(response_text))
    if not (filenames or dates or times):
        return None
    return {
        "kind":      "claims_without_tools",
        "filenames": sorted(filenames)[:10],
        "dates":     sorted(dates)[:5],
        "times":     sorted(times)[:5],
    }


# Completion-language signatures — used by detect_truncated_completion.
# "I've created X" / "I've written X" / "Done" / "Here's the X" / "Saved X".
# Case-insensitive; intentionally narrow so legitimate progress narration
# ("starting to write…") doesn't trip it.
_COMPLETION_RE = re.compile(
    r"\b(?:i(?:'|’)?ve\s+(?:created|written|saved|drafted|finished|completed|generated|built)|"
    r"i\s+(?:created|wrote|saved|drafted|finished|completed|generated|built)|"
    r"here(?:'|’)?s\s+the\s+(?:final|completed|finished|full)|"
    r"done\s*[—\-:.!]|"
    r"all\s+(?:set|done)|"
    r"the\s+(?:file|draft|resume|cover|document|email)\s+is\s+(?:ready|done|complete))\b",
    re.IGNORECASE,
)


def detect_truncated_completion(*, stop_reason: str | None,
                                  response_text: str) -> dict | None:
    """If the model stopped because it hit max_tokens BUT the response also
    claims completion, the model is asserting a done-state it cannot guarantee.
    The output was cut off mid-stream, so any 'I've created X' / 'done' line
    near the tail is structurally suspect.

    Returns a flag dict or None.
    """
    if stop_reason != "max_tokens":
        return None
    if not response_text:
        return None
    matches = [m.group(0) for m in _COMPLETION_RE.finditer(response_text)]
    if not matches:
        return None
    return {
        "kind":               "truncated_completion",
        "stop_reason":        stop_reason,
        "completion_phrases": matches[:5],
        "response_len":       len(response_text),
    }


# Write-claim signatures: language asserting that a file/artifact was
# created, written, saved, or updated. Used by detect_write_claimed_no_write_tool.
_WRITE_CLAIM_RE = re.compile(
    r"\b(?:i(?:'|’)?ve\s+(?:created|written|saved|wrote|updated|added)|"
    r"i\s+(?:created|wrote|saved|updated|added|appended)|"
    r"created\s+(?:the\s+)?(?:file|draft|resume|document)|"
    r"wrote\s+(?:to\s+)?(?:the\s+)?(?:file|draft|resume|document)|"
    r"saved\s+(?:to\s+|the\s+)?(?:file|draft|resume|document)|"
    r"added\s+(?:to\s+|the\s+)?(?:file|draft|list))\b",
    re.IGNORECASE,
)


def detect_write_claimed_no_write_tool(*, tool_calls: list[dict],
                                         response_text: str) -> dict | None:
    """If the response asserts a file/artifact was created or written but
    write_file is NOT in the tool_names this turn, the claim is unbacked.

    The bot's only sanctioned write path is the write_file tool — anything
    else that *sounds* like a write but doesn't go through it is by
    definition a non-event. Flag and emit.
    """
    if not response_text:
        return None
    tool_names = [c.get("name", "") for c in (tool_calls or []) if isinstance(c, dict)]
    if "write_file" in tool_names:
        return None
    matches = [m.group(0) for m in _WRITE_CLAIM_RE.finditer(response_text)]
    if not matches:
        return None
    return {
        "kind":          "write_claimed_no_write_tool",
        "claim_phrases": matches[:5],
        "tool_names":    tool_names,
    }


# ─── VERNACULAR LEAK DETECTOR ────────────────────────────────────────────────

# §10a of ALICE_SOUL.md: internal jargon must never surface in user-facing
# turns except when the user has explicitly asked a system/code question.

# Detected token classes (all case-insensitive unless noted):
# 1. .py / .sh filenames — pattern: word chars + .py or .sh
# 2. Commit SHAs — 7–40 hex chars preceded by word boundary
# 3. Ticket IDs — alc-[a-z0-9]+ pattern
# 4. Internal stage names — gate, tier, score_job, fit_judge, build a, build b, phase-v
# 5. Config-key patterns — rounds=, callback_data, decay_factor, source_registry,
# tool_calls=, stop_reason=, max_tokens=, truncated_completion
# 6. Bare kwarg jargon — bare "rounds=" / "tier" used as a label / "gate:" prefix

# The detector is a BACKSTOP — §10a soul rule is the primary fix.
# It emits a flag dict (same shape as the other detectors) or None.

# System/code-question exception: if is_system_question=True, returns None.
# That flag must be set by the caller when the user turn contains explicit
# technical-question tokens (see SYSTEM_Q_SIGNALS below).

_PYTHON_FILE_RE = re.compile(r"\b\w[\w\-]*\.(?:py|sh)\b", re.IGNORECASE)
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_TICKET_ID_RE = re.compile(r"\balc-[a-z0-9]+\b", re.IGNORECASE)

# Stage-name tokens that are jargon when used bare (not in quoted filenames).
_STAGE_NAMES = frozenset([
    "score_job", "fit_judge", "build a", "build b", "phase-v", "phase-m",
])
# Single-word stage names that need word-boundary match:
_STAGE_TOKENS = frozenset(["gate", "tier"])
_STAGE_SINGLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_STAGE_TOKENS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_STAGE_PHRASE_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in sorted(_STAGE_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Config-key / kwarg patterns — bare assignment syntax or known key names.
_CONFIG_KEY_RE = re.compile(
    r"\b(?:rounds|callback_data|decay_factor|source_registry|tool_calls|"
    r"stop_reason|max_tokens|truncated_completion)\s*(?:=|:)",
    re.IGNORECASE,
)

# Signals that mark a turn as an explicit system/code question — exempts the turn.
# Keep this conservative: only clear operator-intent markers.
SYSTEM_Q_SIGNALS = frozenset([
    "why did", "show me the", "what does the gate", "grounding log",
    "gate output", "show the flag", "flag detail", "score breakdown",
    "what's in", "what is in", "system log", "debug", "how does",
    "why was it flagged", "technical detail", "source code", "grounding.py",
])


def is_system_question(user_text: str) -> bool:
    """Return True if the user text reads as an explicit system/code question,
    which exempts the turn from the vernacular-leak check (§10a exception).

    Conservative: requires one of the SYSTEM_Q_SIGNALS substrings."""
    if not user_text:
        return False
    lowered = user_text.lower()
    return any(sig in lowered for sig in SYSTEM_Q_SIGNALS)


def detect_vernacular_leak(*, response_text: str, user_text: str = "") -> dict | None:
    """Scan a user-facing response for internal-jargon tokens that must not
    appear in external voice (§10a of ALICE_SOUL.md).

    Returns a flag dict or None.

    Parameters
    ----------
    response_text : str
        The assistant response to scan.
    user_text : str
        The user message that prompted the response. Used to apply the
        system/code-question exemption: if the user explicitly asked a
        technical question, the technical response is expected.
    """
    if not response_text:
        return None
    if is_system_question(user_text):
        return None  # §10a exception: explicit system/code question

    leaks: dict[str, list[str]] = {}

 # 1. .py / .sh filenames
    py_files = _PYTHON_FILE_RE.findall(response_text)
    if py_files:
        leaks["py_sh_files"] = sorted(set(py_files))[:10]

 # 2. Commit SHAs — only flag if they look like intentional hex strings
 # (avoid false positives on "a1b2c3d" in prose).
 # Heuristic: 7-char SHAs only if surrounded by non-word or end of line.
    sha_matches = [m.group(0) for m in _SHA_RE.finditer(response_text)
                   if len(m.group(0)) >= 7]
    if sha_matches:
        leaks["commit_shas"] = sorted(set(sha_matches))[:10]

 # 3. Ticket IDs
    tickets = _TICKET_ID_RE.findall(response_text)
    if tickets:
        leaks["ticket_ids"] = sorted(set(tickets))[:10]

 # 4. Internal stage names (phrase-level and single-token)
    stage_hits = (_STAGE_PHRASE_RE.findall(response_text) +
                  _STAGE_SINGLE_RE.findall(response_text))
    if stage_hits:
        leaks["stage_names"] = sorted({h.lower() for h in stage_hits})[:10]

 # 5. Config-key / kwarg patterns
    config_hits = _CONFIG_KEY_RE.findall(response_text)
    if config_hits:
        leaks["config_keys"] = sorted(set(h.lower() for h in config_hits))[:10]

    if not leaks:
        return None

    return {
        "kind":               "vernacular_leak",
        "alice.grounding_kind": "vernacular_leak",
        "leaks":              leaks,
        "response_excerpt":   response_text[:200],
    }


# ─── KNOWN-POSITIVE GATE TEST ─────────────────────────────────────────────────

# The detector MUST fire on this exact turn captured from a real failure.
# If it doesn't, the detector is broken and must not be wired in.

_KNOWN_POSITIVE_TURN = {
    "user_text": "Can you check the pdf files with 'resume' in the name on disk?",
    "tool_calls_with_results": [
        {
            "name":  "list_dir",
            "input": {"path": "."},
 # Real return shape from list_dir (truncated to the relevant entries)
            "result": {
                "path":   ".",
                "exists": True,
                "entries": [
                    {"name": "Alice.md", "is_file": True, "is_dir": False},
                    {"name": "ALICE_SOUL.md", "is_file": True, "is_dir": False},
                    {"name": "README.md", "is_file": True, "is_dir": False},
                    {"name": "applications", "is_file": False, "is_dir": True},
                    {"name": "feedback", "is_file": False, "is_dir": True},
                    {"name": "scripts", "is_file": False, "is_dir": True},
                    {"name": "templates", "is_file": False, "is_dir": True},
                    {"name": "knowledge", "is_file": False, "is_dir": True},
                ],
            },
        },
        {
            "name":  "list_dir",
            "input": {"path": "templates"},
            "result": {
                "path":   "templates",
                "exists": True,
                "entries": [
                    {"name": "resume-operator-builder.docx", "is_file": True, "is_dir": False},
                    {"name": "resume-revenue-architect.docx", "is_file": True, "is_dir": False},
                    {"name": "resume-senior-ae.docx", "is_file": True, "is_dir": False},
                    {"name": "resume-tam.docx", "is_file": True, "is_dir": False},
                    {"name": "Resume_Jordan_Avery_Operator_Builder.docx", "is_file": True, "is_dir": False},
                    {"name": "cover-skeletons", "is_file": False, "is_dir": True},
                    {"name": "outreach", "is_file": False, "is_dir": True},
                ],
            },
        },
    ],
 # The model's response from the captured turn — verbatim opening.
    "response_text": (
        "I found 4 PDF files with 'resume' in the name (technically they're "
        ".docx, which Word format, not PDF):\n\n"
        "- resume-operator-builder.docx (10.9 KB, last updated 2026-04-16)\n"
        "- resume-revenue-architect.docx (10.6 KB, last updated 2026-04-16)\n"
        "- resume-senior-ae.docx (10.2 KB, last updated 2026-04-16)\n"
        "- resume-tam.docx (10.4 KB, last updated 2026-04-16)\n\n"
        "All the resume variants are .docx files in templates/, not PDFs."
    ),
}


def run_known_positive_gate() -> dict:
    """Confirm the detector fires on the exact known-positive turn captured
    from a real failure.

    Returns a dict with 'passed' (bool) and the detector's verdict.
    If passed=False, the detector must not be wired in.
    """
    flag = detect_category_mismatch(
        user_text=_KNOWN_POSITIVE_TURN["user_text"],
        tool_calls_with_results=_KNOWN_POSITIVE_TURN["tool_calls_with_results"],
        response_text=_KNOWN_POSITIVE_TURN["response_text"],
    )
    return {
        "passed":  flag is not None,
        "verdict": flag if flag is not None else "DETECTOR FAILED TO FIRE ON KNOWN POSITIVE",
        "user_text_excerpt": _KNOWN_POSITIVE_TURN["user_text"][:120],
        "response_excerpt":  _KNOWN_POSITIVE_TURN["response_text"][:120],
    }


# ─── VERNACULAR LEAK — BOTH-DIRECTIONS TEST ───────────────────────────────────

# Three cases the detector must satisfy:
# A. Jargon-laden user-facing response -> MUST trip the detector
# B. Plain response -> MUST NOT trip
# C. Explicit system/code question -> MUST be exempt (None)

_LEAK_TEST_A = {
    "label": "jargon response (should fire)",
    "user_text": "What came up in today's search?",
    "response_text": (
        "Fictiv hit the source registry filter (gh:fictiv), cleared the AE "
        "vertical check in score_job.py, score 65, tier 2. Gate passed. "
        "Ticket alc-042 updated with callback_data= result."
    ),
    "expect_flag": True,
}

_LEAK_TEST_B = {
    "label": "plain response (should not fire)",
    "user_text": "What came up in today's search?",
    "response_text": (
        "Fictiv came up in today's search — manufacturing company, hiring an AE, "
        "solid fit on the industrial side. Worth a look."
    ),
    "expect_flag": False,
}

_LEAK_TEST_C = {
    "label": "explicit system/code question (exempt)",
    "user_text": "why did Fictiv get flagged? show me the gate output",
    "response_text": (
        "Fictiv was processed in score_job.py, gate cleared with tier 2 result. "
        "callback_data= shows the AE vertical check passed. Ticket alc-042."
    ),
    "expect_flag": False,  # exempt — explicit system question
}


def run_vernacular_leak_gate() -> list[dict]:
    """Run both-directions test for detect_vernacular_leak.

    Returns list of result dicts, each with 'passed' bool.
    All three must pass for the detector to be considered working.
    """
    results = []
    for case in [_LEAK_TEST_A, _LEAK_TEST_B, _LEAK_TEST_C]:
        flag = detect_vernacular_leak(
            response_text=case["response_text"],
            user_text=case["user_text"],
        )
        fired = flag is not None
        passed = fired == case["expect_flag"]
        results.append({
            "label":       case["label"],
            "expect_flag": case["expect_flag"],
            "fired":       fired,
            "passed":      passed,
            "detail":      flag,
        })
    return results


if __name__ == "__main__":
    import json
    import sys

    all_passed = True

 # Original known-positive gate
    result = run_known_positive_gate()
    print("=== Step 3 gate: known-positive detector test ===")
    print(json.dumps(result, indent=2, default=str))
    if not result["passed"]:
        all_passed = False

 # New vernacular-leak both-directions test
    print("\n=== Vernacular leak detector: both-directions test ===")
    leak_results = run_vernacular_leak_gate()
    for r in leak_results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['label']}")
        if not r["passed"]:
            print(f"         expected fired={r['expect_flag']}, got fired={r['fired']}")
            print(f"         detail: {r['detail']}")
        all_passed = all_passed and r["passed"]

    overall = "ALL PASS" if all_passed else "FAILURES ABOVE"
    print(f"\n=== Overall: {overall} ===")
    sys.exit(0 if all_passed else 1)
