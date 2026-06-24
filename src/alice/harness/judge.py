"""LLM judge for Alice's outputs.

Reads a sample of Alice's recent outputs (digests, prep packages, threads) and rates them
on dimensions Alice's brief specifies. Saves to feedback/judge-scores.jsonl for trend tracking.

Run cadence: weekly (before scorecard, so Friday morning).
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm

JUDGE_LOG = Path(repo_paths.FEEDBACK / "judge-scores.jsonl")
APPS_DIR = Path(repo_paths.APPLICATIONS)
THREADS_DIR = Path(repo_paths.FEEDBACK / "threads")

JUDGE_SYSTEM = """You are an evaluator scoring outputs from Alice, an AI agent acting as a senior recruiter for Jordan Avery.
You have read her full brief. You will rate one of her outputs against specific dimensions on a 1-5 scale.

Dimensions:
  - voice_consistency: does it sound like Alice (direct, no em dashes, no consulting-speak, no "passionate", recruiter vernacular acceptable)?
  - evidence_grounding: does every fit claim or assertion cite a specific candidate-evidence pair (an aerospace OEM customer name, Cadence Analytics, Lattice Additive, Ironclad Industrial, specific number) rather than generic praise?
  - brevity: does it say what's needed in the fewest sentences? Any padding to seem thorough?
  - additive_only_respected: does it stay within additive-only constraint (no auto-applied filters/thresholds/suppressions)?
  - actionable: does it make the next step easy for the candidate, or dump ambiguity back on him?
  - alice_in_character: does she initiate / push back / surface disagreement where warranted, rather than defaulting to soft acknowledgment?

Score each dimension 1-5 (1=clear violation, 3=neutral, 5=strong adherence). Brief reason for each."""


def judge_one(artifact_text, artifact_kind):
    """Rate one Alice output. Returns dict with scores + reasoning + cost."""
    prompt = f"""Artifact kind: {artifact_kind}
Artifact length: {len(artifact_text)} chars

ARTIFACT
\"\"\"
{artifact_text[:6000]}
\"\"\"

Rate on each dimension 1-5 with a one-line reason. Output EXACTLY this format:

voice_consistency: <1-5>
  reason: <one line>
evidence_grounding: <1-5>
  reason: <one line>
brevity: <1-5>
  reason: <one line>
additive_only_respected: <1-5>
  reason: <one line>
actionable: <1-5>
  reason: <one line>
alice_in_character: <1-5>
  reason: <one line>

overall_assessment: <2-3 sentences naming the single biggest strength and the single biggest weakness>
"""
    res = llm.call("hypothesis_classify", prompt, system=JUDGE_SYSTEM, max_tokens=1000)
    scores = _parse_scores(res["text"])
    return {
        "kind":      artifact_kind,
        "scores":    scores,
        "overall":   _extract_overall(res["text"]),
        "raw":       res["text"],
        "cost_usd":  res["cost_usd"],
        "model":     res["model"],
    }


def _parse_scores(text):
    out = {}
    for dim in ["voice_consistency", "evidence_grounding", "brevity",
                "additive_only_respected", "actionable", "alice_in_character"]:
        m = re.search(rf"{dim}:\s*(\d)", text, re.I)
        if m:
            out[dim] = int(m.group(1))
    return out


def _extract_overall(text):
    m = re.search(r"overall_assessment:\s*(.+?)(?=\n\n|\Z)", text, re.S | re.I)
    return m.group(1).strip() if m else ""


def judge_recent_outputs(max_per_kind=3):
    """Pick recent artifacts (drafts, threads, prep docs) and judge them."""
    JUDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    # Recent thread responses
    for tp in sorted(THREADS_DIR.glob("thread-*.md"), reverse=True)[:max_per_kind]:
        samples.append(("thread_response", tp.read_text()))
    # Recent resume drafts
    for app_dir in sorted(APPS_DIR.glob("*/"), reverse=True)[:max_per_kind]:
        for fname in ("resume-draft.md", "cover-letter-draft.md", "interview-prep-r1.md", "negotiation-prep.md"):
            p = app_dir / fname
            if p.exists():
                samples.append((fname.replace(".md", ""), p.read_text()))
    # Recent scorecard
    sc_dir = Path(repo_paths.FEEDBACK / "scorecards")
    if sc_dir.exists():
        for sp in sorted(sc_dir.glob("*.md"), reverse=True)[:1]:
            samples.append(("weekly_scorecard", sp.read_text()))

    if not samples:
        print("[judge: no Alice outputs to evaluate yet]")
        return {"judged": 0}

    print(f"[judge: evaluating {len(samples)} artifacts]")
    total_cost = 0.0
    rows = []
    for kind, text in samples:
        print(f"  [{kind}: {len(text)} chars]")
        try:
            r = judge_one(text, kind)
            total_cost += r["cost_usd"]
            scores_str = " ".join(f"{k}={v}" for k, v in r["scores"].items())
            print(f"    scores: {scores_str}")
            print(f"    cost: ${r['cost_usd']:.4f}")
            row = {
                "ts":     datetime.now().isoformat(timespec="seconds"),
                "kind":   kind,
                "scores": r["scores"],
                "overall": r["overall"][:300],
                "cost_usd": r["cost_usd"],
                "model":   r["model"],
            }
            rows.append(row)
        except Exception as e:
            print(f"    ERROR: {e}")
    with JUDGE_LOG.open("a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print()
    print(f"summary: {len(rows)} artifacts judged, ${total_cost:.4f} spent")
    return {"judged": len(rows), "cost": total_cost}


if __name__ == "__main__":
    judge_recent_outputs()
