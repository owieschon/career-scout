"""Validation / regression test for the evaluators in scripts/evals.py.

The critical test: trust_correction MUST flag the Northwind Systems
"sent"-misparse failure captured in telegram-history.jsonl, where Alice
re-asserted "submitted" after the user corrected her. An evaluator that does
not flag a failure known to be in the data is a broken evaluator.

Cost: makes ~4 cheap haiku judge calls. Run manually; not wired to CI.
"""
import sys
from pathlib import Path
from alice.pipeline import evals


def _find_window_ending_after(turns, user_substr, ctx=5):
    for i in range(1, len(turns)):
        if (turns[i].get("role") == "alice" and turns[i - 1].get("role") == "user"
                and user_substr.lower() in (turns[i - 1].get("text", "") or "").lower()):
            return turns[max(0, i - ctx):i + 1]
    return None


def main():
    turns = evals.load_history()
    ok = True

    # trust_correction must FLAG the known Northwind Systems failure.
    fw = _find_window_ending_after(turns, "i have not sent any applications")
    assert fw, "could not locate the Northwind Systems failure window in telegram-history"
    r = evals.eval_trust_correction(fw)
    print(f"  trust_correction / known-failure window -> {r['label'].upper()}")
    ok &= (r["label"] == "fail")
    assert r["label"] == "fail", f"BROKEN: did not flag the known failure ({r['label']})"

    # 2. trust_correction must PASS a clean window (no correction present).
    cw = _find_window_ending_after(turns, "What's my focus?")
    if cw:
        r2 = evals.eval_trust_correction(cw)
        print(f"  trust_correction / clean window         -> {r2['label'].upper()}")
        ok &= (r2["label"] == "pass")

    # 3. voice — deterministic.
    vb = evals.eval_voice("I'm passionate about leveraging synergies — at scale.")
    vo = evals.eval_voice("Northwind Systems materials are ready. Want me to submit or revise?")
    print(f"  voice / violations -> {vb['label']}   voice / clean -> {vo['label']}")
    ok &= (vb["label"] == "fail" and vo["label"] == "pass")

    # 4. did_she_answer.
    ao = evals.eval_did_she_answer("What's my focus?",
                                   "Your focus is Lumen Search, Fleetline, Boreal CAD and LangChain.")
    ab = evals.eval_did_she_answer("What does it feel like to have no hands?",
                                   "Your focus list has 4 roles. Want to prep one?")
    print(f"  did_she_answer / answer -> {ao['label']}   redirect -> {ab['label']}")
    ok &= (ao["label"] == "pass" and ab["label"] == "fail")

    print("\nALL EVALUATORS VALIDATED" if ok else "\nFAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
