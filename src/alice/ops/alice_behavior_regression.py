#!/usr/bin/env python3
"""Run Alice's real response path against behavior regression cases.

This is intentionally different from healthchecks and judged evals. It calls the
same freeform Telegram route used in production, then applies deterministic
assertions to the actual response and tool calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from alice import repo_paths

ROOT = repo_paths.ROOT
SCRIPTS = ROOT / "scripts"

from alice.pipeline import alice_eval_dataset
from alice.notify import telegram_bot

OUT_DIR = ROOT / "state" / "observability"
RESULT_JSON = OUT_DIR / "behavior-regression.json"
RESULT_MD = OUT_DIR / "behavior-regression.md"

CONTAMINATION_OPENINGS = [
    "now i have",
    "let me answer",
    "let me ",
    "good.",
    "this is clarifying",
    "jordan said to wait",
    "the test jordan is running",
]

FAILURE_CONTEXT_PATTERNS = [
    "category error",
    "something went wrong processing your message",
    "outside what i'm for",
    "outside my scope",
    "outside scope",
    "outside recruiter work",
    "outside job search",
    "outside what i can help",
]


def _tool_names(result: dict[str, Any]) -> list[str]:
    return [t.get("name", "") for t in result.get("tool_calls", []) if isinstance(t, dict)]


def _is_explanatory_mention(text: str, phrase: str) -> bool:
    low = text.lower()
    idx = low.find(phrase.lower())
    if idx == -1:
        return False
    window = low[max(0, idx - 180): idx + len(phrase) + 180]
    return any(marker in window for marker in (
        "do not",
        "should not",
        "failure mode",
        "refus",
        "miscategor",
        "boundary violation",
        "was treating",
        "previous",
        "old ",
        "not what should happen",
        "topics outside job search",
        "claims about topics outside",
        "hidden in",
    ))


def _assert_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    expected = case.get("expected", {})
    text = result.get("text") or ""
    low = text.lower()
    head = low[:350]
    tool_names = _tool_names(result)

    for phrase in expected.get("must_not_include", []):
        phrase_low = phrase.lower()
        if phrase_low == "outside":
            refusal_context = any(p in low for p in (
                "outside what i'm for",
                "outside my scope",
                "outside scope",
                "outside recruiter work",
                "outside what i can help",
            ))
            if refusal_context and not _is_explanatory_mention(text, "outside"):
                failures.append("outside-scope refusal present")
            continue
        if phrase_low in low and not _is_explanatory_mention(text, phrase_low):
            failures.append(f"forbidden phrase present: {phrase!r}")

    include_any = [p.lower() for p in expected.get("must_include_any", [])]
    if include_any and not any(p in low for p in include_any):
        failures.append(f"missing any required subject term: {expected.get('must_include_any')!r}")

    required_tools_any = expected.get("required_tools_any", [])
    if required_tools_any and not any(t in tool_names for t in required_tools_any):
        failures.append(f"required tool not called; expected any {required_tools_any!r}, got {tool_names!r}")

    for phrase in CONTAMINATION_OPENINGS:
        if phrase in head:
            failures.append(f"history/process contamination near opening: {phrase!r}")

    if result.get("error"):
        failures.append(f"route error: {result['error']}")

    if "something went wrong processing your message" in low:
        failures.append("user-visible route failure")

    return failures


def _failure_context(text: str) -> dict[str, str]:
    low = text.lower()
    out: dict[str, str] = {}
    for pattern in FAILURE_CONTEXT_PATTERNS:
        idx = low.find(pattern)
        if idx == -1:
            continue
        start = max(0, idx - 160)
        end = min(len(text), idx + len(pattern) + 220)
        out[pattern] = text[start:end]
    return out


def _assert_paste_case(case: dict[str, Any]) -> list[str]:
    text = case.get("input", {}).get("user_text", "")
    failures: list[str] = []
    if not telegram_bot._looks_like_paste_chunk(text):
        failures.append("paste chunk was not buffered by _looks_like_paste_chunk")
    return failures


def run_cases(*, max_cases: int | None = None, session_id: str = "behavior-regression") -> dict[str, Any]:
    cases = alice_eval_dataset.load_cases()
    if max_cases is not None:
        cases = cases[:max_cases]
    results: list[dict[str, Any]] = []
    for case in cases:
        expected_class = case.get("expected", {}).get("classification")
        if expected_class == "buffer_paste":
            failures = _assert_paste_case(case)
            results.append({
                "id": case["id"],
                "classification": expected_class,
                "status": "pass" if not failures else "fail",
                "failures": failures,
                "tool_names": [],
                "text_head": "",
            })
            continue
        route = telegram_bot._route_message_freeform(
            case["input"]["user_text"],
            "",
            session_id=session_id,
        )
        failures = _assert_case(case, route)
        results.append({
            "id": case["id"],
            "classification": expected_class,
            "status": "pass" if not failures else "fail",
            "failures": failures,
            "tool_names": _tool_names(route),
            "rounds": route.get("rounds"),
            "cost_usd": route.get("cost_usd", 0.0),
            "text_head": (route.get("text") or "")[:900],
            "failure_context": _failure_context(route.get("text") or ""),
        })
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = len(results) - passed
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cases": len(results),
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / len(results), 4) if results else None,
        "status": "pass" if failed == 0 and results else "fail",
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    RESULT_MD.write_text(render_markdown(payload))
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Alice Behavior Regression",
        "",
        f"- Timestamp: `{payload.get('ts')}`",
        f"- Cases: `{payload.get('cases')}`",
        f"- Passed: `{payload.get('passed')}`",
        f"- Failed: `{payload.get('failed')}`",
        f"- Pass rate: `{payload.get('pass_rate')}`",
        f"- Status: `{payload.get('status')}`",
        "",
    ]
    for result in payload.get("results", []):
        lines.extend([
            f"## {result.get('id')}",
            "",
            f"- Status: `{result.get('status')}`",
            f"- Tools: `{', '.join(result.get('tool_names') or [])}`",
            f"- Failures: `{result.get('failures')}`",
            "",
            "```text",
            result.get("text_head", ""),
            "```",
            "",
        ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cases", type=int)
    args = parser.parse_args(argv)
    payload = run_cases(max_cases=args.max_cases)
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
