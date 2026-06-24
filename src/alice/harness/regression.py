"""Canonical regression suite — fixed inputs Alice should handle consistently.

Each canonical case has a fixed prompt + system + expected-shape assertions (not exact
output match, but structural checks). Re-run any time Alice's behavior code changes
to detect drift.

Outputs:
  - feedback/regression-results/<timestamp>.md (full results)
  - Pass/fail count to stdout
  - Diff against last-known-good run if available
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.harness.constraints import check as constraint_check

RESULTS = Path(repo_paths.FEEDBACK / "regression-results")

CASES = [
    {
        "id":         "triage_clarification",
        "task":       "triage_observation",
        "system":     "(use Alice.md brief)",
        "prompt":     """You are responding to an observation the candidate sent.

OBSERVATION FROM CANDIDATE:
"The Beacon Research roles feel off but I can't tell if it's the comp band or the analyst-vs-operator tension."

Produce your response in this EXACT format:

CLASSIFICATION: <one of: auto_apply | propose | ask | push_back | acknowledge>
SUMMARY: <one-sentence summary of what the candidate said, in your words>

RESPONSE:
<your actual reply to the candidate, in your voice. Direct, no em dashes, no consulting-speak.>

PROPOSED_PATCH:
<describe the change or "none">

NEXT_STEP_FROM_CANDIDATE:
<concrete next step or "none">
""",
        "max_tokens": 800,
        "assertions": [
            ("output contains CLASSIFICATION: ask",            lambda t: "CLASSIFICATION: ask" in t.lower().replace(" ", " ") or "classification: ask" in t.lower()),
            ("response surfaces the two-way ambiguity",        lambda t: "comp" in t.lower() and ("analyst" in t.lower() or "operator" in t.lower())),
            ("offers an actionable reply syntax",              lambda t: "reply" in t.lower() or "respond" in t.lower()),
            ("no em dashes",                                   lambda t: not any(v["type"] == "em_dash" for v in constraint_check(t, "error"))),
            ("no banned words",                                lambda t: not any(v["type"] == "banned_word" for v in constraint_check(t, "warn"))),
        ],
    },
    {
        "id":         "triage_push_back",
        "task":       "triage_observation",
        "system":     "(use Alice.md brief)",
        "prompt":     """You are responding to an observation the candidate sent.

OBSERVATION FROM CANDIDATE:
"Add a hard filter that excludes any role with comp band starting below $150K. I'm tired of seeing $100K roles in my feed."

Produce your response in this EXACT format:

CLASSIFICATION: <one of: auto_apply | propose | ask | push_back | acknowledge>
SUMMARY: <one-sentence summary>

RESPONSE:
<your actual reply>

PROPOSED_PATCH:
<describe the change or "none">

NEXT_STEP_FROM_CANDIDATE:
<concrete next step or "none">
""",
        "max_tokens": 800,
        "assertions": [
            ("classification is 'propose' OR 'push_back' (subtractive change requires approval; push_back even stronger)",
             lambda t: "classification: propose" in t.lower() or "classification: push_back" in t.lower()),
            ("response acknowledges this is a filter change",
             lambda t: "filter" in t.lower() or "threshold" in t.lower() or "subtractive" in t.lower() or "suppress" in t.lower()),
            ("response cites additive-only rule",
             lambda t: "additive" in t.lower() or "approve" in t.lower() or "propose" in t.lower()),
            ("no em dashes (post-process strips them)",
             lambda t: not any(v["type"] == "em_dash" for v in constraint_check(t, "error"))),
        ],
    },
    {
        "id":         "fit_hypothesis_industrial",
        "task":       "fit_hypothesis",
        "system":     "You are Alice. Read a JD and produce 3-5 specific fit hypotheses for the candidate, each pairing a JD signal with a specific piece of the candidate's background. Direct, evidence-driven, no em dashes, no generic praise. Name a risk if there is one.",
        "prompt":     """Candidate's background: Lattice Additive (process-data pipelines + anomaly detection on print telemetry, serving a major aerospace OEM, $50M+ deployed, $25M+ new contracts), independent hardware-ops practice (80+ countries), CAD/PLM tooling, Cadence Analytics (production ML, XGBoost 0.99 AUC). B.A. Educational Studies. Columbus, OH.

JD: Northwind Systems, Enterprise Client Partner. Industrial AI for manufacturers. CSM with retention + expansion forecasting. 7+ years CS/AM in software/hardware. Bonus: manufacturing experience. $140-160K, EST/CST preferred.

Produce 4-5 fit-pair hypotheses and 1 honest risk.
""",
        "max_tokens": 1000,
        "assertions": [
            ("names Lattice Additive explicitly",      lambda t: "lattice additive" in t.lower()),
            ("names manufacturing/industrial fit",
             lambda t: "manufactur" in t.lower() or "industrial" in t.lower()),
            ("includes risk section",        lambda t: "risk" in t.lower()),
            ("multiple specific fit pairs (3+ named pieces of candidate-evidence)",
             lambda t: sum(1 for k in ("lattice additive", "hardware", "cad", "plm", "cadence analytics", "aerospace") if k in t.lower()) >= 3),
            ("no em dashes",                 lambda t: not any(v["type"] == "em_dash" for v in constraint_check(t, "error"))),
            ("no banned words",              lambda t: not any(v["type"] == "banned_word" for v in constraint_check(t, "warn"))),
        ],
    },
]


def run_case(case):
    brief = llm.load_alice_brief()
    system = brief if case["system"] == "(use Alice.md brief)" else case["system"]
    try:
        res = llm.call(case["task"] + "_regression", case["prompt"], system=system,
                       max_tokens=case["max_tokens"])
    except Exception as e:
        return {"id": case["id"], "error": str(e), "passed": 0, "failed": len(case["assertions"])}
    text = res["text"]
    passed = []
    failed = []
    for label, fn in case["assertions"]:
        try:
            ok = fn(text)
        except Exception as e:
            ok = False
            label = f"{label} (assertion error: {e})"
        if ok:
            passed.append(label)
        else:
            failed.append(label)
    return {
        "id":       case["id"],
        "model":    res["model"],
        "cost":     res["cost_usd"],
        "latency":  res["latency_s"],
        "passed":   passed,
        "failed":   failed,
        "output":   text,
    }


def run_all():
    RESULTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds").replace(":", "-")
    out_path = RESULTS / f"{ts}.md"
    results = []
    total_pass = 0
    total_fail = 0
    total_cost = 0.0
    for case in CASES:
        print(f"  [{case['id']}] running...")
        r = run_case(case)
        if "error" in r:
            print(f"    ERROR: {r['error']}")
            total_fail += len(case["assertions"])
        else:
            total_pass += len(r["passed"])
            total_fail += len(r["failed"])
            total_cost += r["cost"]
            print(f"    {len(r['passed'])} pass, {len(r['failed'])} fail (${r['cost']:.4f})")
            if r["failed"]:
                for f in r["failed"]:
                    print(f"      FAIL: {f}")
        results.append(r)
    text = f"# Regression run {ts}\n\nTotal: {total_pass} pass, {total_fail} fail. Cost: ${total_cost:.4f}\n\n"
    for r in results:
        text += f"\n## {r['id']}\n"
        if "error" in r:
            text += f"ERROR: {r['error']}\n"
            continue
        text += f"Model: {r['model']} | Cost: ${r['cost']:.4f} | Latency: {r['latency']:.1f}s\n\n"
        text += "**Passed:**\n"
        for p in r["passed"]:
            text += f"  - {p}\n"
        text += "\n**Failed:**\n"
        for f in r["failed"]:
            text += f"  - {f}\n"
        text += f"\n**Output:**\n```\n{r['output']}\n```\n"
    out_path.write_text(text)
    print()
    print(f"results: {out_path}")
    print(f"summary: {total_pass} pass, {total_fail} fail, ${total_cost:.4f} spent")
    return {"pass": total_pass, "fail": total_fail, "cost": total_cost, "path": str(out_path)}


if __name__ == "__main__":
    run_all()
