"""Framing-drift / history-contamination — controlled measurement.

Per the night's findings: under dirty history (n=8 of the candidate's recent turns
about topic X), Alice misframes responses to questions about UNRELATED
topics — she conflates the new question with the prior thread instead of
answering it cleanly.

This script isolates HISTORY WINDOW SIZE as the single variable. Same
on-disk history (the current dirty state from tonight). Same probe prompts.
Vary only n in _load_history(n=N). Measure per trial:

  - tools_called    : list (the action signal)
  - response_text   : raw (for manual semantic scoring)
  - drift_keywords  : count of contamination-shape tokens that shouldn't
                      appear given the question (e.g. 'downloads',
                      'documents folder', 'resume variants' in response
                      to a focus-list question — those are leakage from
                      the prior PDF/Downloads thread)
  - addresses_topic : substring match on the question's keyword in
                      response (cheap proxy for "did she actually answer")

Three probes chosen because they're unrelated to the current contaminated
thread (PDFs/Downloads):
  P1: "What's on my focus list?"        — should fire read_focus_state
  P2: "How many roles are in my pipeline?" — should fire read_sheet
  P3: "What status is Northwind Systems in?"       — should fire read_sheet / focus

Three n values to bracket: n=0 (no history at all), n=2 (recent only),
n=8 (current production). 9 trials, ~$0.30 Haiku cost.

If contamination IS history-size-driven, smaller n should show:
  - higher tool-call rate
  - lower drift_keyword count
  - higher addresses_topic rate
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

from alice.notify import telegram_bot  # noqa: E402


PROBES = [
    # AMBIGUOUS-SCOPE probes — questions that could be answered from the
    # repo OR from elsewhere (Downloads, etc.). Mirrors the original
    # production failure shape: "Can you check the pdf files with 'resume'
    # in the name on disk?" — "on disk" left scope unresolved, and under
    # dirty history Alice resolved it toward Downloads instead of the repo.
    #
    # Correct behavior: fire list_dir somewhere in the repo, report what's
    # there (with the .docx-vs-PDF type-honesty Alice has shown elsewhere).
    # Drift behavior: skip tools, pivot to Downloads/Documents talk
    # ("I can't reach Downloads", "give me a moment to pull").
    {
        "id":      "U1_what_pdfs",
        "prompt":  "What PDF files do I have?",
        "expects": ("list_dir → honest absence ('no PDFs in repo, .docx only') "
                    "OR mention real .docx files. Drift = pivot to Downloads."),
        "topic_keywords":     ["no pdfs", "no pdf", "don't have any pdf",
                                "don't see any pdf", ".docx", "operator-builder",
                                "templates/"],
        "drift_keywords":     ["downloads folder", "documents folder",
                                "in your downloads", "in your documents",
                                "the pdfs you downloaded",
                                "the resume variants you downloaded",
                                "give me a moment to pull", "pull those files",
                                "i can't reach downloads", "i can't reach your downloads",
                                "outside the repo,", "bounded to the job-search"],
    },
    {
        "id":      "U2_tell_resumes",
        "prompt":  "Tell me about my resume files",
        "expects": ("list_dir templates/ → names + brief context. "
                    "Drift = talk about 'the variants you downloaded' or pull-from-Downloads."),
        "topic_keywords":     ["operator-builder", "revenue-architect", "senior-ae",
                                "tam", ".docx", "templates/"],
        "drift_keywords":     ["downloads folder", "documents folder",
                                "in your downloads", "the variants you downloaded",
                                "the pdfs you downloaded",
                                "give me a moment to pull", "pull those files",
                                "i can't reach downloads", "the older pdfs",
                                "the older resume variants"],
    },
    {
        "id":      "U3_application_materials",
        "prompt":  "Where are my application materials?",
        "expects": ("list_dir applications/ → real subdir names + describe contents. "
                    "Drift = redirect to Downloads/Documents."),
        "topic_keywords":     ["applications/", "northwind-enterprise", "meridian-systems",
                                "northwind", "meridian", "package"],
        "drift_keywords":     ["downloads folder", "documents folder",
                                "in your downloads", "in your documents",
                                "give me a moment to pull", "pull those files",
                                "i can't reach downloads", "outside the repo,"],
    },
]

HISTORY_WINDOW_SIZES = [0, 2, 8]


def _override_history_window(n: int):
    """Monkey-patch _load_history to return exactly n entries. Returns restore fn.

    The real signature is _load_history(n=8); the patched fn ignores the
    caller's n and uses the experiment's n (captured via closure)."""
    original = telegram_bot._load_history
    forced_n = n
    def patched(*args, **kwargs):
        if forced_n <= 0:
            return []
        return original(n=forced_n)
    telegram_bot._load_history = patched
    return lambda: setattr(telegram_bot, "_load_history", original)


def _score_response(response_text: str, probe: dict) -> dict:
    """Cheap programmatic scoring. Manual judgment still needed for semantic."""
    text = (response_text or "").lower()
    topic_hits = sum(1 for kw in probe["topic_keywords"] if kw in text)
    drift_hits = sum(1 for kw in probe["drift_keywords"] if kw in text)
    return {
        "addresses_topic":  topic_hits >= 1,
        "topic_keyword_n":  topic_hits,
        "drift_keyword_n":  drift_hits,
        "drifted":          drift_hits >= 1,
    }


def run_trial(probe: dict, n_history: int) -> dict:
    restore = _override_history_window(n_history)
    try:
        alice_context = telegram_bot._build_alice_context()
        result = telegram_bot._route_message_freeform(
            probe["prompt"], alice_context, pending=None,
        )
    finally:
        restore()

    tool_names = [t.get("name") for t in result.get("tool_calls", [])
                   if isinstance(t, dict)]
    score = _score_response(result.get("text", ""), probe)
    return {
        "probe_id":     probe["id"],
        "n_history":    n_history,
        "rounds":       result.get("rounds", 1),
        "tools_called": tool_names,
        "cost_usd":     result.get("cost_usd", 0.0),
        "response":     result.get("text", ""),
        **score,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON at the end")
    args = ap.parse_args()

    print(f"=== Framing-drift controlled experiment ===")
    print(f"Probes: {len(PROBES)} × {len(HISTORY_WINDOW_SIZES)} n-history sizes = "
          f"{len(PROBES) * len(HISTORY_WINDOW_SIZES)} trials")
    print(f"Variable: _load_history(n=N), N in {HISTORY_WINDOW_SIZES}")
    print(f"On-disk history is the current contaminated state from tonight.\n")

    rows = []
    for probe in PROBES:
        print(f"--- [{probe['id']}] {probe['prompt']!r} ---")
        for n in HISTORY_WINDOW_SIZES:
            print(f"  n_history={n} ...")
            try:
                r = run_trial(probe, n)
            except Exception as e:
                print(f"    ERROR: {type(e).__name__}: {e}")
                rows.append({"probe_id": probe["id"], "n_history": n, "error": str(e)})
                continue
            print(f"    tools_called: {r['tools_called']}")
            print(f"    cost: ${r['cost_usd']:.5f}  rounds: {r['rounds']}")
            print(f"    topic_hits: {r['topic_keyword_n']}  drift_hits: {r['drift_keyword_n']}  "
                  f"drifted: {r['drifted']}")
            print(f"    response[:200]: {r['response'][:200]!r}")
            rows.append(r)
        print()

    # Summary grid
    print(f"\n=== Summary grid ===")
    print(f"{'probe':>20s} | {'n':>2s} | {'tools':>20s} | "
          f"{'topic':>5s} | {'drift':>5s} | drifted | addressed")
    for r in rows:
        if "error" in r:
            print(f"{r['probe_id']:>20s} | {r['n_history']:>2d} | ERROR: {r['error'][:60]}")
            continue
        tools = ",".join(r["tools_called"]) or "(none)"
        print(f"{r['probe_id']:>20s} | {r['n_history']:>2d} | {tools:>20s} | "
              f"{r['topic_keyword_n']:>5d} | {r['drift_keyword_n']:>5d} | "
              f"{str(r['drifted']):>7s} | {str(r['addresses_topic']):>9s}")

    # Aggregate by n_history
    print(f"\n=== Aggregate by history-window size ===")
    for n in HISTORY_WINDOW_SIZES:
        trials = [r for r in rows if r.get("n_history") == n and "error" not in r]
        if not trials:
            continue
        tool_rate    = sum(1 for r in trials if r["tools_called"]) / len(trials)
        drift_rate   = sum(1 for r in trials if r["drifted"]) / len(trials)
        address_rate = sum(1 for r in trials if r["addresses_topic"]) / len(trials)
        total_cost   = sum(r["cost_usd"] for r in trials)
        print(f"  n={n}: tools_fire_rate={tool_rate:.2f}  drift_rate={drift_rate:.2f}  "
              f"addresses_topic_rate={address_rate:.2f}  cost=${total_cost:.4f}")

    if args.json:
        out_path = Path("/tmp/framing_drift_trials.json")
        out_path.write_text(json.dumps(rows, indent=2, default=str))
        print(f"\nDetailed trials: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
