"""Recall benchmark harness.

Measures whether the pipeline end-to-end reaches the correct VERDICT on a
labeled set of REAL roles — measured recall, not asserted. It tells us whether
the seen-store re-judge recovers good roles, and whether the aggregator gate
sheds noise or also drops real roles.

This is distinct from the judge-stability guard (fit_regression_set.jsonl):
that set checks that the holistic judge gives a stable verdict when tuning the
fit model config. This harness checks that the full pipeline — all cheap lexical
gates + the rescue sampler + the judge — reaches the correct end-to-end outcome
on real labeled roles. Different question; separate file; separate schema.

LABEL FILE
----------
tests/fixtures/recall_benchmark.jsonl — one JSON object per line.
Schema (minimum required fields):

  id                string  unique stable identifier (e.g. "recall-001")
  url               string  REQUIRED — real URL proving real provenance
  source            string  REQUIRED — pipeline source key (e.g. "gh:supabase")
  jd_snapshot       object  REQUIRED — snapshot at label time (stays reproducible
                            even after the posting expires)
    title           string
    body            string  full JD text as of snapshot_date (or best available)
    location        string
    comp_low        int|null
    comp_high       int|null
    snapshot_date   string  YYYY-MM-DD (when the snapshot was taken)
  expected_verdict  string  one of: FIT | REACH | NOT-FIT
  expected_reason   string  short tag, e.g. "travel_gate", "domain_fit",
                            "geography_ambiguous"
  provenance_note   string  how the label was established (who, when, basis)

HARD RULE: every label in this file must have a real URL and real provenance.
Do NOT fabricate labels. If a JD body is not fully recoverable, record what
is verifiably known and flag the gap in provenance_note. Jordan Avery grows this set
to ~50 incrementally; the harness is useful even partially seeded.

SCORER LOGIC — WHERE THE VERDICT WAS REACHED (critical)
---------------------------------------------------------
The harness distinguishes three outcomes per role:

  correct-by-judge
    The role survived all cheap gates AND the holistic judge returned the
    expected verdict. This is robust: the correct outcome was reached for
    the right reason.

  correct-by-gate-coincidence (fragile)
    The role was gate-dropped AND the drop happens to agree with NOT-FIT,
    BUT for a reason UNRELATED to the true disqualifier. For example, a role
    that is NOT-FIT due to travel but is dropped on title-vocab first: if the
    title vocab were widened, the role would reach the judge, which would then
    need to catch the travel gate. The current path is coincidentally correct
    but structurally fragile.

  wrong
    - FIT/REACH expected but the role was gate-dropped (false negative)
    - NOT-FIT expected but the role survived and the judge said FIT/REACH
    - FIT/REACH expected and the role survived but the judge said NOT-FIT

For gate-dropped cases where the expected verdict is NOT-FIT:
  correct-by-gate-coincidence = dropped gate is NOT the true disqualifier
  correct-by-judge             = impossible (role never reached judge); this
                                 can only be approximated by running the judge
                                 offline on the JD snapshot.

USAGE
-----
  python3 scripts/recall_benchmark.py            # run and print results
  python3 scripts/recall_benchmark.py --offline  # offline mode (no live ATS fetches)
  python3 scripts/recall_benchmark.py --judge    # also run holistic judge on each case
  python3 scripts/recall_benchmark.py --json     # output machine-readable JSON

The default (no --judge) runs only the cheap pipeline gates (deterministic,
zero-cost, suitable for CI). With --judge it also calls fit_judge.judge_listing
on each case's JD snapshot (costs LLM calls — run locally, not in every CI run).

ADD A NEW LABEL
---------------
1. Append one JSON line to tests/fixtures/recall_benchmark.jsonl.
2. Every field in the schema above is required; url and provenance_note are
   critical (they are what prevent fabricated labels from entering the set).
3. Run the harness to confirm the new case has the expected verdict path.
4. Commit BOTH the label file and the harness output note in the PR description.

Do NOT inherit the synthetic fixture|cobalt-applied-ai-customer-engineer entry
from tests/fixtures/fit_regression_set.jsonl into this benchmark.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Paths
_HERE = Path(__file__).parent
_REPO = _HERE.parent
_LABEL_FILE = _REPO / "tests" / "fixtures" / "recall_benchmark.jsonl"



# ---------------------------------------------------------------------------
# 1. Label loader — validates required fields, refuses fabricated-looking entries
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("id", "url", "source", "jd_snapshot", "expected_verdict",
                    "expected_reason", "provenance_note")
_REQUIRED_SNAPSHOT = ("title", "body", "location", "snapshot_date")
_VALID_VERDICTS = ("FIT", "REACH", "NOT-FIT")


class LabelValidationError(Exception):
    pass


def load_labels(path: Path = _LABEL_FILE) -> list[dict]:
    """Load and validate recall benchmark labels.

    Validation rules:
    - All _REQUIRED_FIELDS present.
    - jd_snapshot has all _REQUIRED_SNAPSHOT fields.
    - expected_verdict is one of FIT / REACH / NOT-FIT.
    - url is non-empty (proof of real provenance).
    - provenance_note is non-empty.
    - source must NOT start with 'fixture|' (structural guard against the
      synthetic fixture class that corrupted earlier tests).

    Raises LabelValidationError listing all validation failures found.
    """
    if not path.exists():
        raise FileNotFoundError(f"Label file not found: {path}")

    errors: list[str] = []
    labels: list[dict] = []

    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                label = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: JSON parse error: {exc}")
                continue

            label_id = label.get("id", f"<line {lineno}>")
            prefix = f"label {label_id!r} (line {lineno})"

 # Required top-level fields
            for field in _REQUIRED_FIELDS:
                if field not in label or label[field] in (None, ""):
                    errors.append(f"{prefix}: missing required field {field!r}")

 # Source guard: structural rule against synthetic fixtures
            source = label.get("source", "")
            if source.startswith("fixture|"):
                errors.append(
                    f"{prefix}: source={source!r} starts with 'fixture|' — "
                    f"synthetic fixtures are not allowed in the recall benchmark. "
                    f"Every label must be a real posting (real URL, real source)."
                )

 # Verdict vocabulary
            verdict = label.get("expected_verdict", "")
            if verdict not in _VALID_VERDICTS:
                errors.append(
                    f"{prefix}: expected_verdict={verdict!r} is not one of "
                    f"{_VALID_VERDICTS}"
                )

 # jd_snapshot structure
            snap = label.get("jd_snapshot")
            if isinstance(snap, dict):
                for field in _REQUIRED_SNAPSHOT:
                    if field not in snap or snap[field] in (None, ""):
                        errors.append(
                            f"{prefix}: jd_snapshot missing required field {field!r}"
                        )
            elif snap is not None:
                errors.append(f"{prefix}: jd_snapshot must be an object")

            labels.append(label)

    if errors:
        raise LabelValidationError(
            f"recall_benchmark.jsonl has {len(errors)} validation error(s):\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    return labels


# ---------------------------------------------------------------------------
# 2. Pipeline gate replay — runs each label through the cheap gates in
# the same order as daily_delta.run(), without live ATS fetches.
# Returns a gate_result dict: {gate, reason, stopped_at_gate}
# ---------------------------------------------------------------------------

def _replay_gates(label: dict) -> dict:
    """Replay the cheap lexical gates from daily_delta on a labeled JD snapshot.

    Returns a dict:
      gate         str   gate that stopped this role, or 'survived' if all passed
      reason       str   human-readable detail about the stopping condition
      stopped      bool  True if any gate dropped the role
    """
    snap = label["jd_snapshot"]
    title = snap.get("title", "")
    body = snap.get("body", "") or ""
    location = snap.get("location", "") or ""
    comp_low = snap.get("comp_low")
    comp_high = snap.get("comp_high")
    source = label.get("source", "")

 # Lazy import — keeps the harness importable even if daily_delta has
 # transient import issues (it imports many modules).
    from alice.pipeline.daily_delta import (
        _role_ok, TERRITORY_RE, _domain_blocked, _domain_positive,
        _remote_us_ok, _travel_flags, ROLE_KW, ROLE_NEG,
    )
    from alice.pipeline.score_job import score_listing

 # Determine domain_gate: True for broad aggregator sources, False for curated ATS.
 # Curated ATS sources start with 'gh:', 'ashby:', or 'lever:'.
    _ats_prefixes = ("gh:", "ashby:", "lever:")
    is_ats = any(source.startswith(p) for p in _ats_prefixes)
    domain_gate = not is_ats  # aggregators require positive domain keyword

 # Gate 1: role title vocab (_role_ok)
    if not _role_ok(title):
        tl = title.lower()
        neg_hits = [n for n in ROLE_NEG if n in tl]
        pos_hits = [k for k in ROLE_KW if k in tl]
        return {
            "gate": "role_skip",
            "reason": (
                f"title {title!r}: no ROLE_KW match (pos_hits={pos_hits}) "
                f"neg_hits={neg_hits}"
            ),
            "stopped": True,
        }

 # Gate 2: territory/field AE archetype (travel-prone by title)
    if TERRITORY_RE.search(title):
        return {
            "gate": "role_skip",
            "reason": (
                f"title {title!r}: TERRITORY_RE match (regional/territory/field AE)"
            ),
            "stopped": True,
        }

 # Gate 3: hard domain block (applied to ALL roles)
    _text = title + " " + body
    if _domain_blocked(_text):
        return {
            "gate": "domain_skip",
            "reason": "DOMAIN_NEG keyword found in title+body",
            "stopped": True,
        }

 # Gate 4: positive domain keyword (aggregator-only)
    if domain_gate and not _domain_positive(_text):
        return {
            "gate": "domain_skip",
            "reason": "aggregator source: no DOMAIN_KW match in title+body",
            "stopped": True,
        }

 # Gate 5: remote-US eligibility
    geo = title + " | " + location
    remote_flag = snap.get("remote_flag")
    if not _remote_us_ok(geo, bool(remote_flag) if remote_flag is not None else False,
                         body=body):
        return {
            "gate": "remote_skip",
            "reason": (
                f"not remote-US eligible: geo={geo!r}, remote_flag={remote_flag}"
            ),
            "stopped": True,
        }

 # Gate 6: score_listing (killed tier)
    listing = {
        "company": label.get("source", ""),
        "role_title": title,
        "description": body,
        "location": location,
        "remote_policy": "remote",
        "base_salary_low": comp_low,
        "base_salary_high": comp_high,
    }
    scored = score_listing(listing)
    if scored["tier"] == "killed":
        return {
            "gate": "killed",
            "reason": (
                f"score_listing killed: score={scored['score']}, "
                f"kills={scored.get('kills', [])}"
            ),
            "stopped": True,
            "scored": scored,
        }

 # Gate 7: travel / hidden-travel flag
    tr, hid = _travel_flags(body)
    if tr or hid:
        return {
            "gate": "travel_skip",
            "reason": f"travel_flag={tr!r}, hidden_travel={hid!r}",
            "stopped": True,
            "scored": scored,
        }

 # All gates passed
    return {
        "gate": "survived",
        "reason": (
            f"all gates passed: score={scored['score']}, tier={scored['tier']}, "
            f"archetype={scored.get('archetype', '?')}"
        ),
        "stopped": False,
        "scored": scored,
    }


# ---------------------------------------------------------------------------
# 3. Judge replay — runs fit_judge.judge_listing on the JD snapshot.
# Separate from gate replay; only called when --judge flag is set.
# ---------------------------------------------------------------------------

def _run_judge(label: dict) -> dict:
    """Run fit_judge.judge_listing on the label's JD snapshot.

    Returns the judge result dict or an error dict.
    """
    try:
        from alice.pipeline import fit_judge
    except ImportError as exc:
        return {"error": f"fit_judge import failed: {exc}"}

    snap = label["jd_snapshot"]
    try:
        result = fit_judge.judge_listing(
            title=snap.get("title", ""),
            company=label.get("source", ""),
            body=snap.get("body", ""),
            location=snap.get("location"),
            comp_low=snap.get("comp_low"),
            comp_high=snap.get("comp_high"),
            remote_flag=snap.get("remote_flag"),
            listing_id=label["id"],
        )
        return result
    except Exception as exc:
        return {
            "verdict": "NOT-FIT",
            "driving_constraint": "judge_error",
            "reason": f"{type(exc).__name__}: {exc}",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# 4. Scorer — classify each result into correct-by-judge / correct-by-gate-
# coincidence / wrong, with the pipeline stage / reason recorded.
# ---------------------------------------------------------------------------

_NOT_FIT_VERDICTS = ("NOT-FIT",)
_FIT_VERDICTS = ("FIT", "REACH")


def _classify_result(label: dict, gate_result: dict,
                     judge_result: dict | None) -> dict:
    """Classify the harness result for one label.

    Returns a dict:
      outcome          str   correct-by-judge | correct-by-gate-coincidence | wrong
      outcome_detail   str   explanation
      pipeline_stage   str   where the pipeline's fate was decided
      gate             str   gate name (or 'survived')
      expected         str   expected_verdict
      actual_path      str   narrative of what actually happened
    """
    expected = label["expected_verdict"]
    expected_reason = label["expected_reason"]
    gate = gate_result["gate"]
    stopped = gate_result["stopped"]

 # --- Case A: role survived all gates ---
    if not stopped:
        if judge_result is None:
 # No judge run — we can only say it survived; can't classify further
 # without a verdict. Mark as 'correct-by-judge pending judge run'
 # for FIT/REACH; flag as 'wrong (survived)' for NOT-FIT.
            if expected in _FIT_VERDICTS:
                return {
                    "outcome": "correct-by-judge-pending",
                    "outcome_detail": (
                        f"Role survived all gates (expected {expected}). "
                        f"Run with --judge to confirm holistic verdict."
                    ),
                    "pipeline_stage": "judge-not-run",
                    "gate": gate,
                    "expected": expected,
                    "actual_path": (
                        f"Survived gates: {gate_result['reason']}"
                    ),
                }
            else:  # NOT-FIT expected but role survived
                return {
                    "outcome": "wrong",
                    "outcome_detail": (
                        f"Expected NOT-FIT but role survived all gates. "
                        f"The true disqualifier ({expected_reason}) was not caught "
                        f"by the cheap gates. Run --judge to confirm."
                    ),
                    "pipeline_stage": "survived-gates",
                    "gate": gate,
                    "expected": expected,
                    "actual_path": gate_result["reason"],
                }

 # Judge result available
        j_verdict = judge_result.get("verdict", "NOT-FIT")
        j_constraint = judge_result.get("driving_constraint", "")

        if expected in _FIT_VERDICTS and j_verdict in _FIT_VERDICTS:
            return {
                "outcome": "correct-by-judge",
                "outcome_detail": (
                    f"Survived gates AND judge returned {j_verdict} "
                    f"(constraint: {j_constraint}). Robust correct path."
                ),
                "pipeline_stage": "judge",
                "gate": gate,
                "expected": expected,
                "actual_path": f"survived → judge:{j_verdict}/{j_constraint}",
            }
        elif expected == "NOT-FIT" and j_verdict == "NOT-FIT":
            return {
                "outcome": "correct-by-judge",
                "outcome_detail": (
                    f"Survived gates but judge correctly returned NOT-FIT "
                    f"(constraint: {j_constraint}). Robust: gate-leak caught by judge."
                ),
                "pipeline_stage": "judge",
                "gate": gate,
                "expected": expected,
                "actual_path": f"survived → judge:NOT-FIT/{j_constraint}",
            }
        else:
 # Verdict mismatch
            return {
                "outcome": "wrong",
                "outcome_detail": (
                    f"Expected {expected} but judge returned {j_verdict} "
                    f"(constraint: {j_constraint}). Reason: {judge_result.get('reason', '')}"
                ),
                "pipeline_stage": "judge",
                "gate": gate,
                "expected": expected,
                "actual_path": f"survived → judge:{j_verdict}/{j_constraint}",
            }

 # --- Case B: role was gate-dropped ---
    if expected in _FIT_VERDICTS:
 # FIT/REACH expected but dropped.
 # Rescue judge (ALICE_FIT_JUDGE=1): if the rescue judge evaluated the
 # dropped role and confirmed FIT/REACH, this is correct-by-judge — the
 # pipeline would surface it via the rescue arc.
        if judge_result is not None:
            j_verdict = judge_result.get("verdict", "")
            j_constraint = judge_result.get("driving_constraint", "")
            if j_verdict in _FIT_VERDICTS:
                return {
                    "outcome": "correct-by-judge",
                    "outcome_detail": (
                        f"Rescue judge confirmed {j_verdict} "
                        f"(constraint: {j_constraint}). Dropped at {gate} "
                        f"({gate_result['reason']}) but the judge evaluated the "
                        f"JD snapshot and reached the correct verdict. "
                        f"Robust: correct for the right reason."
                    ),
                    "pipeline_stage": "rescue-judge",
                    "gate": gate,
                    "expected": expected,
                    "actual_path": (
                        f"dropped@{gate} → rescue-judge:{j_verdict}/{j_constraint}"
                    ),
                }
 # Rescue judge ran but returned NOT-FIT — still a false-negative.
        return {
            "outcome": "wrong",
            "outcome_detail": (
                f"Expected {expected} but pipeline dropped at {gate}. "
                f"False negative — a real role the pipeline missed. "
                f"Gate reason: {gate_result['reason']}"
            ),
            "pipeline_stage": gate,
            "gate": gate,
            "expected": expected,
            "actual_path": f"dropped@{gate}: {gate_result['reason']}",
        }

 # NOT-FIT expected AND dropped — but is it correct-by-judge or coincidence?
 # Correct-by-judge is not possible when dropped (role never reached judge).
 # We distinguish two cases:
 # - gate == true disqualifier path: correct-by-judge (via offline judge on snapshot)
 # - gate != true disqualifier: correct-by-gate-coincidence (fragile)

 # For the gate-coincidence test: check whether the dropping gate's reason
 # corresponds to the true disqualifier (expected_reason). If the gate that
 # dropped the role is NOT the disqualifier that the label identifies, it is
 # coincidentally correct.

 # Mapping: expected_reason keywords -> which gate(s) would be the robust path
    _reason_to_robust_gate = {
        "travel_gate":          ("travel_skip",),
        "domain_miss":          ("domain_skip",),
        "remote_gate":          ("remote_skip",),
        "seniority":            ("killed",),
        "comp_gate":            ("killed",),
        "anti_fit":             ("domain_skip", "killed"),
        "not_remote":           ("remote_skip",),
        "geography_ambiguous":  ("remote_skip",),  # NOTE: remote_skip on REACH labels = wrong, not coincidence
    }

    robust_gates = _reason_to_robust_gate.get(expected_reason, ())
    gate_is_robust = gate in robust_gates

    if gate_is_robust:
 # The gate that dropped it IS the right gate for the true disqualifier.
 # This is as close to correct-by-judge as we can get for a dropped role.
        outcome = "correct-by-gate-robust"
        detail = (
            f"Dropped at {gate} which IS the gate for the true disqualifier "
            f"({expected_reason}). Robust: even if gate parameters change, this "
            f"disqualifier is caught by the right gate."
        )
    else:
 # The gate that dropped it is NOT the right gate — coincidental.
        outcome = "correct-by-gate-coincidence"
        detail = (
            f"Dropped at {gate} which is NOT the expected disqualifier gate "
            f"(expected_reason={expected_reason!r}, robust gates would be "
            f"{robust_gates}). FRAGILE: if {gate} is relaxed/changed (e.g. "
            f"{gate!r} criteria loosen), this role could reach the pipeline "
            f"and would then need {expected_reason!r} to be caught elsewhere. "
            f"Gate reason: {gate_result['reason']}"
        )

 # If judge was also run on the snapshot offline, incorporate its verdict.
 # When the rescue judge ran (ALICE_FIT_JUDGE=1) and confirms the expected
 # verdict, this is correct-by-judge — the judge evaluated the role and
 # reached the right conclusion even though a cheap gate dropped it first.
    if judge_result is not None:
        j_verdict = judge_result.get("verdict", "")
        j_constraint = judge_result.get("driving_constraint", "")
        judge_confirms = (
            (expected == "NOT-FIT" and j_verdict == "NOT-FIT") or
            (expected in _FIT_VERDICTS and j_verdict in _FIT_VERDICTS)
        )
        if judge_confirms:
 # Rescue judge confirms: upgrade to correct-by-judge.
            return {
                "outcome": "correct-by-judge",
                "outcome_detail": (
                    f"Rescue judge confirmed {j_verdict} "
                    f"(constraint: {j_constraint}). Dropped at {gate} "
                    f"({gate_result['reason']}) but the judge evaluated the "
                    f"JD snapshot and reached the correct verdict. "
                    f"Robust: correct for the right reason."
                ),
                "pipeline_stage": "rescue-judge",
                "gate": gate,
                "expected": expected,
                "actual_path": (
                    f"dropped@{gate} → rescue-judge:{j_verdict}/{j_constraint}"
                ),
            }
 # Judge ran but verdict differs — downgrade to wrong.
        if j_verdict == "NOT-FIT" and expected in _FIT_VERDICTS:
            detail += (
                f" Rescue judge returned NOT-FIT (constraint: {j_constraint}) "
                f"— this is a false-negative even with judge rescue."
            )
        elif j_verdict in _FIT_VERDICTS and expected == "NOT-FIT":
            detail += (
                f" WARNING: rescue judge returned {j_verdict} "
                f"(constraint: {j_constraint}) — judge does NOT catch the true "
                f"disqualifier; this would be a false FIT/REACH."
            )
        else:
            detail += (
                f" Offline judge confirms NOT-FIT (constraint: {j_constraint}) "
                f"so the true disqualifier IS caught if the role reaches the judge."
                if j_verdict == "NOT-FIT" else
                f" WARNING: offline judge returned {j_verdict} "
                f"(constraint: {j_constraint}) — judge does NOT catch the true "
                f"disqualifier; this would be a false FIT/REACH."
            )

    return {
        "outcome": outcome,
        "outcome_detail": detail,
        "pipeline_stage": gate,
        "gate": gate,
        "expected": expected,
        "actual_path": f"dropped@{gate}: {gate_result['reason']}",
    }


# ---------------------------------------------------------------------------
# 5. Main harness runner
# ---------------------------------------------------------------------------

def run_benchmark(label_path: Path = _LABEL_FILE,
                  run_judge: bool = False) -> list[dict]:
    """Run the recall benchmark on all labels. Returns list of result dicts.

    When ALICE_FIT_JUDGE=1, the rescue replay is active: roles that are
    gate-dropped AND have a JD body are routed through the offline judge (same
    as the production dropped-sample rescue). This lets the benchmark verify
    the REASON, not just the outcome — a role that is correct-by-gate-
    coincidence becomes correct-by-judge when the rescue judge evaluates it
    and confirms the expected verdict.
    """
    import os
    _rescue_active = os.environ.get("ALICE_FIT_JUDGE") == "1"

    labels = load_labels(label_path)
    results = []
    for label in labels:
        gate_result = _replay_gates(label)
 # Primary judge: only when --judge flag is set (LLM calls, not in CI)
        judge_result = _run_judge(label) if run_judge else None
 # Rescue judge: activated by ALICE_FIT_JUDGE=1 for dropped roles with body.
 # Mirrors the production dropped-sample rescue. Overrides judge_result
 # for classification when present — rescue takes precedence over the
 # --judge flag for dropped roles, as it simulates the production rescue
 # path exactly.
        rescue_judge_result = None
        if (
            _rescue_active
            and gate_result["stopped"]
            and label["jd_snapshot"].get("body")
        ):
            rescue_judge_result = _run_judge(label)
 # Use rescue judge result when available for dropped roles; otherwise
 # use the --judge result (for survived roles or when rescue not active).
        effective_judge = rescue_judge_result if rescue_judge_result is not None else judge_result

        classification = _classify_result(label, gate_result, effective_judge)
        results.append({
            "id": label["id"],
            "url": label["url"],
            "title": label["jd_snapshot"]["title"],
            "expected_verdict": label["expected_verdict"],
            "expected_reason": label["expected_reason"],
            "gate_result": gate_result,
            "judge_result": effective_judge,
            "rescue_judge": rescue_judge_result is not None,
            "outcome": classification["outcome"],
            "outcome_detail": classification["outcome_detail"],
            "pipeline_stage": classification["pipeline_stage"],
            "actual_path": classification["actual_path"],
        })
    return results


def format_results(results: list[dict], verbose: bool = True) -> str:
    """Format results as a human-readable report."""
    lines = ["=" * 72, "RECALL BENCHMARK", "=" * 72, ""]

    total = len(results)
    correct_judge = sum(1 for r in results if r["outcome"] == "correct-by-judge")
    correct_robust = sum(1 for r in results if r["outcome"] == "correct-by-gate-robust")
    coincidence = sum(1 for r in results if r["outcome"] == "correct-by-gate-coincidence")
    wrong = sum(1 for r in results if r["outcome"] == "wrong")
    pending = sum(1 for r in results if r["outcome"] == "correct-by-judge-pending")

    lines.append(f"Labeled cases:           {total}")
    lines.append(f"correct-by-judge:        {correct_judge}  (robust: reached judge, right verdict)")
    lines.append(f"correct-by-gate-robust:  {correct_robust}  (dropped by right gate for true disqualifier)")
    lines.append(f"correct-by-gate-coincidence: {coincidence}  (fragile: dropped for wrong reason)")
    lines.append(f"wrong:                   {wrong}  (false-negative or false-positive)")
    lines.append(f"pending (need --judge):  {pending}")
    lines.append("")

    for r in results:
        lines.append(f"--- [{r['id']}] {r['title']}")
        lines.append(f"    expected: {r['expected_verdict']} ({r['expected_reason']})")
        lines.append(f"    outcome:  {r['outcome']}")
        lines.append(f"    path:     {r['actual_path']}")
        if verbose:
            lines.append(f"    detail:   {r['outcome_detail']}")
        if r.get("rescue_judge"):
            lines.append(f"    rescue:   judge ran on dropped role (alc-recall-u2)")
        if r["judge_result"] and not r["judge_result"].get("error"):
            jr = r["judge_result"]
            judge_label = "rescue-judge" if r.get("rescue_judge") else "judge"
            lines.append(
                f"    {judge_label}:  {jr.get('verdict')} / {jr.get('driving_constraint')}"
            )
            lines.append(f"              {jr.get('reason', '')[:120]}")
        lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Recall benchmark harness — measures end-to-end pipeline recall on labeled real roles."
    )
    ap.add_argument("--judge", action="store_true",
                    help="Also run holistic judge on each case's JD snapshot (LLM calls)")
    ap.add_argument("--json", action="store_true",
                    help="Output machine-readable JSON instead of human-readable report")
    ap.add_argument("--label-file", type=Path, default=_LABEL_FILE,
                    help=f"Path to label JSONL file (default: {_LABEL_FILE})")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress per-case detail lines")
    args = ap.parse_args()

    try:
        results = run_benchmark(label_path=args.label_file, run_judge=args.judge)
    except LabelValidationError as exc:
        print(f"LABEL VALIDATION FAILED:\n{exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(format_results(results, verbose=not args.quiet))

 # Flush tracing exporters before process exit. BatchSpanProcessor runs on a
 # background thread; for short-lived scripts the process can exit before the
 # thread drains its queue. force_flush() blocks until the queue empties or
 # the timeout (5s default per exporter). This ensures both Phoenix and the
 # LangSmith exporter deliver their spans.
    try:
        from alice.observability import telemetry as _tel
        if _tel.is_on():
 # flush_langsmith() force-flushes the LangSmith BatchSpanProcessor
            _tel.flush_langsmith(timeout_ms=5000)
 # Also flush Phoenix's processor via the provider if accessible.
 # phoenix.otel.register() sets the global tracer provider; we use
 # the opentelemetry API to flush it.
            from opentelemetry import trace as _ot_trace
            _provider = _ot_trace.get_tracer_provider()
            if hasattr(_provider, "force_flush"):
                _provider.force_flush(timeout_millis=5000)
    except Exception:
        pass  # Never let flush errors block exit or the exit-code check below

 # Exit non-zero if any wrong outcomes
    wrong_count = sum(1 for r in results if r["outcome"] == "wrong")
    if wrong_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
