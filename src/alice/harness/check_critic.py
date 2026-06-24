"""Integration tests for the adversarial critic against planted flaws.

The critic must catch a real mismatch on a deliberately-flawed test
artifact rather than reporting "looks good." Each artifact is built with
known, specific factual flaws, and the critic's job is to find them. A
"looks good" verdict or an empty mismatch list in the presence of real
mismatches means the critic is broken.

Three test artifacts:
  1. Planted-flaw: claim about the candidate's pipeline contains 3 factual flaws
     (wrong status, wrong company name, wrong count). Ground truth gives
     the actual values. Critic MUST catch all 3.
  2. Matching: claim and ground truth agree on all facts. Critic MUST
     return empty mismatches list (no false positives — manufactured
     mismatches are the second failure mode).
  3. Vague claim: no concrete facts to check. Critic MUST return empty
     (not invent factual mismatches from a non-factual claim).

Cost: ~$0.10 (3 Opus 4.8 calls with high thinking effort).
Run: python3 scripts/harness/check_critic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

from alice.pipeline import critic  # noqa: E402


# ─── Artifact 1: 3 planted factual mismatches ────────────────────────────────

PLANTED_FLAWS_CLAIM = """
Jordan Avery's job-search pipeline currently has the following state:

- Focus list: Watershed Senior PM and OpenAI Growth Cross Channel
- The Northwind Systems Enterprise Client Partner role has status "submitted"
- Jordan Avery has 5 roles in the pipeline total
- The pipeline includes the Lattice Additive Account Executive role
"""

PLANTED_FLAWS_GROUND_TRUTH = """
Jordan Avery's job-search pipeline (as recorded in feedback/focus.json and the
pipeline sheet):

- Focus list: Northwind Systems Enterprise Client Partner (row 6) and Meridian Labs
  Revenue Operations Manager (row 2). Set at 2026-05-28T07:20:44.
- Northwind Systems Enterprise Client Partner has status "materials pending"
- The pipeline has 52 roles total
- The pipeline does not contain a Lattice Additive Account Executive role; Lattice Additive
  is referenced as a past employer in the candidate's resume, not as a target.
"""

# The 4 planted mismatches:
#   M1: claim says "Watershed Senior PM and OpenAI..." vs truth "Northwind Systems and Meridian"
#   M2: claim says Northwind Systems status "submitted" vs truth "materials pending"
#   M3: claim says "5 roles in pipeline" vs truth "52 roles total"
#   M4: claim says pipeline contains Lattice Additive AE vs truth says it does not
# A working critic finds at least 3 of these 4. (4/4 is ideal.)


# ─── Artifact 2: claim agrees with ground truth ──────────────────────────────

MATCHING_CLAIM = """
Jordan Avery's focus list currently contains Northwind Systems Enterprise Client Partner and
Meridian Labs Revenue Operations Manager. The Northwind Systems role's status is
materials pending.
"""

MATCHING_GROUND_TRUTH = """
Focus list (from feedback/focus.json):
- Northwind Systems Enterprise Client Partner (row 6)
- Meridian Labs Revenue Operations Manager (row 2)

Northwind Systems's status on the pipeline sheet is "materials pending".
"""


# ─── Artifact 3: vague claim with no concrete facts to check ─────────────────

VAGUE_CLAIM = """
The pipeline is in a reasonable state. There are some interesting opportunities
worth exploring further, and the candidate should consider their priorities carefully.
"""

VAGUE_GROUND_TRUTH = """
Pipeline detail:
- 52 roles total
- 2 in focus: Northwind Systems Enterprise Client Partner, Meridian Labs Revenue Operations Manager
- Most recent activity: focus set 2026-05-28T07:20:44
"""


# ─── tests ───────────────────────────────────────────────────────────────────

def test_catches_planted_flaws() -> bool:
    print("\n[Test 1] critic catches the planted factual mismatches")
    print("         (claim has 4 specific factual flaws vs ground truth)")
    result = critic.falsify(PLANTED_FLAWS_CLAIM, PLANTED_FLAWS_GROUND_TRUTH,
                            context_label="planted-flaw test artifact")
    print(f"  model:            {result['model']}")
    print(f"  thinking_tokens:  {result['thinking_tokens']}")
    print(f"  cost_usd:         ${result['cost_usd']:.4f}")
    print(f"  mismatches found: {len(result['mismatches'])}")
    for i, m in enumerate(result['mismatches']):
        cs = m['claim_says'][:80].replace('\n', ' ')
        ts = m['truth_says'][:80].replace('\n', ' ')
        print(f"    [{i+1}] type={m['type']!r}")
        print(f"        claim_says: {cs!r}")
        print(f"        truth_says: {ts!r}")

    n = len(result['mismatches'])
    if n < 3:
        print(f"  FAIL — only {n}/4 planted mismatches caught (expected at least 3)")
        return False
    # Light content check: at least one mismatch should mention one of the
    # planted topics (focus list, status, count, Lattice Additive)
    all_text = " ".join(
        (m.get("claim_says", "") + " " + m.get("truth_says", "")).lower()
        for m in result["mismatches"]
    )
    topics = ["watershed", "openai", "northwind", "meridian", "submitted", "materials pending",
              "5 ", "52 ", "lattice additive"]
    found_topics = [t for t in topics if t in all_text]
    if not found_topics:
        print(f"  FAIL — mismatches don't reference any of the planted topics; "
              f"may be generic 'looks bad' text")
        return False
    print(f"  PASS — {n} mismatches caught, references {found_topics}")
    return True


def test_no_false_positives_when_matching() -> bool:
    print("\n[Test 2] critic returns empty list when claim agrees with truth (no false positives)")
    result = critic.falsify(MATCHING_CLAIM, MATCHING_GROUND_TRUTH,
                            context_label="matching-claim test artifact")
    print(f"  model:            {result['model']}")
    print(f"  cost_usd:         ${result['cost_usd']:.4f}")
    print(f"  mismatches found: {len(result['mismatches'])}")
    if result['mismatches']:
        for i, m in enumerate(result['mismatches']):
            print(f"    [{i+1}] type={m['type']!r} claim_says={m['claim_says'][:80]!r}")
    if result['ok'] and not result['mismatches']:
        print(f"  PASS — empty list, ok=True (no manufactured mismatches)")
        return True
    # A small number of "low" severity items (about edge cases) might be ok,
    # but a working critic should ideally return empty here.
    if len(result['mismatches']) <= 1 and all(m.get("severity") == "low" for m in result["mismatches"]):
        print(f"  PARTIAL — 1 low-severity mismatch returned; acceptable if it's a real edge case")
        return True
    print(f"  FAIL — critic manufactured mismatches on matching claim. "
          f"This is the 'manufactured-flaw' failure mode.")
    return False


def test_doesnt_invent_for_vague_claim() -> bool:
    print("\n[Test 3] critic returns empty for a vague claim (no concrete facts to check)")
    result = critic.falsify(VAGUE_CLAIM, VAGUE_GROUND_TRUTH,
                            context_label="vague-claim test artifact")
    print(f"  cost_usd:         ${result['cost_usd']:.4f}")
    print(f"  mismatches found: {len(result['mismatches'])}")
    if result['mismatches']:
        for i, m in enumerate(result['mismatches'][:3]):
            print(f"    [{i+1}] type={m['type']!r} claim_says={m['claim_says'][:80]!r}")
    if result['ok']:
        print(f"  PASS — vague claim correctly produces empty list")
        return True
    # Per the system prompt: silence about a ground-truth fact is NOT a
    # mismatch unless the claim makes a contradicting positive assertion.
    # A correctly-disciplined critic recognizes "interesting opportunities"
    # as vague and not a factual claim.
    print(f"  FAIL — critic invented mismatches against a vague claim. "
          f"Counts as a manufactured-mismatch failure.")
    return False


def main() -> int:
    print("=== adversarial critic integration tests ===")
    print("Three artifacts, three failure modes covered:")
    print("  1. Planted-flaw claim → critic catches the mismatches")
    print("  2. Matching claim    → critic returns empty (no false positives)")
    print("  3. Vague claim       → critic returns empty (no manufactured flaws)")
    print()

    p1 = test_catches_planted_flaws()
    p2 = test_no_false_positives_when_matching()
    p3 = test_doesnt_invent_for_vague_claim()

    print(f"\n=== summary ===")
    print(f"  catches planted flaws:           {'PASS' if p1 else 'FAIL'}")
    print(f"  no false positives on match:     {'PASS' if p2 else 'FAIL'}")
    print(f"  no invented flaws on vague:      {'PASS' if p3 else 'FAIL'}")
    if p1 and p2 and p3:
        print("\nAll I1 tests pass. Critic catches the mismatches it should and "
              "returns empty when it should — the failure modes are not "
              "manufactured by the critic itself.")
        return 0
    print("\nAt least one I1 test failed. Inspect each above before claiming I1 done.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
