"""model_sweep.py — fit_judge benchmark across candidate models.

For each candidate model, runs fit_judge on the recall_benchmark labels and
records (quality_score, cost_usd, latency_s ± spread). Latency is sampled
N_SAMPLES times per model to report mean + spread (std-dev). Quality is
verdict-accuracy vs. the calibrated benchmark labels.

DESIGN PRINCIPLES
-----------------
- Additive: builds on llm.py's OpenRouter path without touching any live
  routing table (TIER_FOR_TASK / MODEL_FOR_TASK). The operator applies a routing
  change in Phase O after reviewing the frontier table.
- Regression-proof: the Anthropic-native control (current production model
  for fit_judge = claude-haiku-4-5-20251001) is always in the sweep so the
  table has a baseline to compare against.
- PII-safe: recall_benchmark labels are public job postings — no resume PII
  flows through this sweep. OpenRouter data_collection=deny is still set as a
  precaution (see llm._build_or_payload).
- Fail-open: if a model call errors, that run is marked as an error, the error
  is recorded, and the sweep continues with the remaining models.

USAGE
-----
  python3 scripts/model_sweep.py                 # run full sweep, print table
  python3 scripts/model_sweep.py --json          # machine-readable JSON
  python3 scripts/model_sweep.py --n-samples 1   # faster (less latency data)
  python3 scripts/model_sweep.py --models claude-haiku-4-5-20251001 openai/gpt-4o

CANDIDATE MODELS
----------------
The default sweep covers:
  Anthropic (native):
    claude-haiku-4-5-20251001   (current production fit_judge model)
    claude-sonnet-4-6           (medium tier)
    claude-opus-4-8             (expensive tier — the ceiling)
  OpenRouter (US-hosted, no-retain):
    openai/gpt-4o
    openai/gpt-4o-mini
    nvidia/llama-3.1-nemotron-70b-instruct
    google/gemini-flash-1.5
    google/gemini-pro-1.5
    meta-llama/llama-3.3-70b-instruct

OUTPUT
------
Prints a ranked frontier table:
  model | verdict-accuracy | $/call (mean) | latency_mean_s | latency_std_s | notes

Gold-standard flag: models that are cheaper than the current production model
AND match or exceed its verdict-accuracy are flagged as "SAFE-DOWNGRADE".
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).parent
_REPO = _HERE.parent

from alice.llm import llm
from alice.jobcfg import load as _load_cfg
from alice.pipeline.recall_benchmark import load_labels, _LABEL_FILE
from alice.pipeline import fit_judge as _fit_judge

# Pre-load constraints once — pure I/O, no LLM, no side effects.
# Used by _judge_label to build the system prompt identically to fit_judge.judge_listing.
_CONSTRAINTS = None

def _get_constraints():
    global _CONSTRAINTS
    if _CONSTRAINTS is None:
        _CONSTRAINTS = _fit_judge.load_constraints()
    return _CONSTRAINTS


# ─── Default candidate model list ────────────────────────────────────────────

CANDIDATE_MODELS = [
 # Anthropic-native (ordered cheapest most expensive)
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
 # OpenRouter: OpenAI
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
 # OpenRouter: Nvidia (Nemotron — US-hosted via DeepInfra/Together)
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
 # OpenRouter: Google (current generation)
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
 # OpenRouter: Meta
    "meta-llama/llama-3.3-70b-instruct",
]

# Current production model for fit_judge — the baseline to beat.
CURRENT_PROD_MODEL = "claude-haiku-4-5-20251001"

# Default number of latency samples per model per label.
N_SAMPLES_DEFAULT = 3


# ─── Judge invocation ─────────────────────────────────────────────────────────

def _judge_label(label: dict, model: str) -> dict:
    """Run fit_judge on one label with a specific model override.

    Bypasses evals._judge (which hardcodes the model) and calls llm.call
    directly with model=<candidate>, using the same system+prompt that
    fit_judge.judge_listing would build. This is the ONLY path that diverges
    from the normal fit_judge call; the system prompt and user prompt are
    byte-identical to what production uses.

    Returns: {verdict, driving_constraint, reason, cost_usd, latency_s,
              model, error (if any)}
    """
    snap = label["jd_snapshot"]
    constraints = _get_constraints()
    system = _fit_judge.build_judge_system(constraints)
    prompt = _fit_judge.build_judge_prompt(
        title=snap.get("title", ""),
        company=label.get("source", ""),
        body=snap.get("body", ""),
        location=snap.get("location"),
        comp_low=snap.get("comp_low"),
        comp_high=snap.get("comp_high"),
        remote_flag=snap.get("remote_flag"),
    )
    t0 = time.time()
    try:
        res = llm.call(
            task="fit_judge",
            prompt=prompt,
            system=system,
            max_tokens=400,
            model=model,
            temperature=0.0,
        )
        latency = time.time() - t0
        judge_text = (res.get("text") or "").strip()
        parsed = _fit_judge.parse_verdict(judge_text)
        return {
            "verdict":            parsed["verdict"],
            "driving_constraint": parsed["driving_constraint"],
            "reason":             parsed.get("reason", ""),
            "cost_usd":           res.get("cost_usd", 0.0),
            "latency_s":          res.get("latency_s", latency),
            "model":              model,
        }
    except Exception as exc:
        return {
            "verdict":            "NOT-FIT",
            "driving_constraint": "judge_error",
            "reason":             f"{type(exc).__name__}: {exc}",
            "error":              str(exc),
            "latency_s":          time.time() - t0,
            "cost_usd":           0.0,
            "model":              model,
        }


# ─── Per-model sweep ──────────────────────────────────────────────────────────

def _sweep_model(labels: list[dict], model: str, n_samples: int) -> dict:
    """Run the fit_judge sweep for one model across all labels.

    For latency sampling: each label is judged n_samples times. The verdict
    from the FIRST sample is used for accuracy; all samples contribute to
    latency stats. This gives N × n_samples data points for latency while
    keeping the verdict from a fresh (non-cached) first call.

    Returns:
      {model, accuracy, correct, total, mean_cost_usd, latencies: [...],
       lat_mean, lat_std, results: [...], errors: [...]}
    """
    all_latencies: list[float] = []
    all_costs: list[float] = []
    results: list[dict] = []
    errors: list[str] = []

    for label in labels:
        expected = label["expected_verdict"]
        label_runs: list[dict] = []

        for i in range(max(1, n_samples)):
            r = _judge_label(label, model)
            label_runs.append(r)
            all_latencies.append(r.get("latency_s", 0.0))
            all_costs.append(r.get("cost_usd", 0.0))
            if r.get("error"):
                errors.append(f"[{label['id']}] attempt {i}: {r['error']}")

 # Use first run's verdict for accuracy measurement.
        first = label_runs[0]
        got_verdict = first.get("verdict", "")
 # Accuracy: exact match OR both in (FIT, REACH) — the benchmark treats
 # both as "passing" verdicts, so a FIT-expected/REACH-actual pair is
 # a near-miss but not wrong for the purposes of the sweep table.
 # Strict accuracy: exact match only.
        exact_match = (got_verdict == expected)
 # Lenient accuracy: FIT/REACH both count as "passing"
        _passing = {"FIT", "REACH"}
        lenient_match = (got_verdict in _passing and expected in _passing) or (
            got_verdict == expected
        )
        results.append({
            "id": label["id"],
            "expected": expected,
            "got":      got_verdict,
            "exact":    exact_match,
            "lenient":  lenient_match,
            "constraint": first.get("driving_constraint", ""),
            "reason":   first.get("reason", "")[:120],
            "error":    first.get("error"),
        })

    total = len(results)
    exact_correct = sum(1 for r in results if r["exact"])
    lenient_correct = sum(1 for r in results if r["lenient"])

    n = len(all_latencies)
    lat_mean = sum(all_latencies) / n if n else 0.0
    lat_variance = sum((x - lat_mean) ** 2 for x in all_latencies) / n if n > 1 else 0.0
    lat_std = math.sqrt(lat_variance)

    n_costs = len(all_costs)
    mean_cost = sum(all_costs) / n_costs if n_costs else 0.0

    return {
        "model":          model,
        "accuracy_exact": exact_correct / total if total else 0.0,
        "accuracy_lenient": lenient_correct / total if total else 0.0,
        "correct_exact":  exact_correct,
        "correct_lenient": lenient_correct,
        "total":          total,
        "mean_cost_usd":  round(mean_cost, 6),
        "lat_mean_s":     round(lat_mean, 3),
        "lat_std_s":      round(lat_std, 3),
        "latencies":      [round(l, 3) for l in all_latencies],
        "results":        results,
        "errors":         errors,
    }


# ─── Full sweep ───────────────────────────────────────────────────────────────

def run_sweep(models: list[str] | None = None,
              label_path: Path = _LABEL_FILE,
              n_samples: int = N_SAMPLES_DEFAULT) -> list[dict]:
    """Run the sweep across all candidate models. Returns a list of per-model
    result dicts (see _sweep_model), sorted by lenient accuracy descending."""
    if models is None:
        models = CANDIDATE_MODELS

    labels = load_labels(label_path)
    print(f"[sweep] {len(labels)} labels, {len(models)} models, {n_samples} sample(s)/label",
          file=sys.stderr)

    sweep_results = []
    for i, model in enumerate(models, 1):
        print(f"[sweep] [{i}/{len(models)}] {model} ...", file=sys.stderr, end="", flush=True)
        t_model = time.time()
        result = _sweep_model(labels, model, n_samples)
        elapsed = time.time() - t_model
        print(
            f" accuracy={result['accuracy_exact']:.0%} ({result['correct_exact']}/{result['total']}) "
            f"cost=${result['mean_cost_usd']:.5f}/call "
            f"lat={result['lat_mean_s']:.2f}s±{result['lat_std_s']:.2f} "
            f"[{elapsed:.1f}s total]",
            file=sys.stderr,
        )
        sweep_results.append(result)

    return sweep_results


# ─── Frontier analysis ────────────────────────────────────────────────────────

def _analyze_frontier(results: list[dict], current_model: str) -> dict:
    """Identify safe-downgrade candidates and the recommended operating point.

    Safe-downgrade: cheaper than the current production model AND lenient
    accuracy >= current model's lenient accuracy.

    Returns analysis dict with:
      current_baseline: the baseline row
      safe_downgrades: list of model rows that are cheaper + equal/better accuracy
      recommendation: string
    """
    current = next((r for r in results if r["model"] == current_model), None)
    if not current:
        return {"error": f"Current model {current_model!r} not in results"}

    curr_cost = current["mean_cost_usd"]
    curr_acc = current["accuracy_lenient"]

    safe = [
        r for r in results
        if r["model"] != current_model
        and r["mean_cost_usd"] < curr_cost
        and r["accuracy_lenient"] >= curr_acc
    ]
    safe_sorted = sorted(safe, key=lambda r: (r["mean_cost_usd"], -r["accuracy_lenient"]))

 # Best accuracy overall (ceiling)
    best_acc = max(r["accuracy_lenient"] for r in results)
    best_models = [r for r in results if r["accuracy_lenient"] == best_acc]

    return {
        "current_baseline": current,
        "safe_downgrades":  safe_sorted,
        "ceiling_accuracy": best_acc,
        "ceiling_models":   [r["model"] for r in best_models],
    }


# ─── Formatting ──────────────────────────────────────────────────────────────

def format_table(results: list[dict], current_model: str) -> str:
    """Render the frontier table and analysis as a human-readable string."""
    analysis = _analyze_frontier(results, current_model)
    safe_models = {r["model"] for r in analysis.get("safe_downgrades", [])}
    ceiling_models = set(analysis.get("ceiling_models", []))

    lines = [
        "",
        "=" * 90,
        "PHASE M — FIT_JUDGE MODEL SWEEP FRONTIER TABLE",
        "=" * 90,
        "",
        f"{'MODEL':<48} {'ACC(L)':<8} {'ACC(E)':<8} {'$/call':<10} {'LAT(s)':<8} {'LAT±':<7} {'NOTES'}",
        "-" * 90,
    ]

 # Sort: lenient accuracy desc, then cost asc
    sorted_results = sorted(results, key=lambda r: (-r["accuracy_lenient"], r["mean_cost_usd"]))
    for r in sorted_results:
        notes = []
        if r["model"] == current_model:
            notes.append("CURRENT-PROD")
        if r["model"] in safe_models:
            notes.append("SAFE-DOWNGRADE")
        if r["model"] in ceiling_models and r["model"] != current_model:
            notes.append("CEILING")
        if r["errors"]:
            notes.append(f"{len(r['errors'])} ERR")
        notes_str = " | ".join(notes)
        lines.append(
            f"{r['model']:<48} "
            f"{r['accuracy_lenient']:>5.0%}   "
            f"{r['accuracy_exact']:>5.0%}   "
            f"${r['mean_cost_usd']:<9.5f} "
            f"{r['lat_mean_s']:<7.2f}  "
            f"{r['lat_std_s']:<6.2f}  "
            f"{notes_str}"
        )

    lines += ["-" * 90, ""]

 # Per-model verdict detail
    lines += ["VERDICT DETAIL PER MODEL", "-" * 60]
    for r in sorted_results:
        lines.append(f"  {r['model']}")
        for res in r["results"]:
            exact_tag = "OK" if res["exact"] else "MISS"
            lenient_tag = "OK(L)" if (res["lenient"] and not res["exact"]) else ""
            err_tag = f"ERROR: {res['error'][:60]}" if res.get("error") else ""
            lines.append(
                f"    [{res['id']}] expected={res['expected']:8s} "
                f"got={res['got']:8s} [{exact_tag}]{lenient_tag and ' ' + lenient_tag} "
                f"constraint={res['constraint']:20s} {err_tag}"
            )
            if res.get("reason") and not res.get("error"):
                lines.append(f"      reason: {res['reason'][:100]}")
        lines.append("")

 # Analysis section
    lines += ["=" * 90, "FRONTIER ANALYSIS", "=" * 90, ""]
    if "error" in analysis:
        lines.append(f"ERROR: {analysis['error']}")
    else:
        curr = analysis["current_baseline"]
        lines += [
            f"Current production model: {curr['model']}",
            f"  Lenient accuracy: {curr['accuracy_lenient']:.0%}  Exact: {curr['accuracy_exact']:.0%}",
            f"  Mean cost/call:   ${curr['mean_cost_usd']:.5f}",
            f"  Latency:          {curr['lat_mean_s']:.2f}s ± {curr['lat_std_s']:.2f}s",
            "",
        ]

        if analysis["safe_downgrades"]:
            lines.append("SAFE-DOWNGRADE candidates (cheaper + accuracy >= current):")
            for r in analysis["safe_downgrades"]:
                savings_pct = (1 - r["mean_cost_usd"] / curr["mean_cost_usd"]) * 100 if curr["mean_cost_usd"] > 0 else 0
                lines.append(
                    f"  {r['model']:<48}  "
                    f"acc={r['accuracy_lenient']:.0%}  "
                    f"${r['mean_cost_usd']:.5f}/call  "
                    f"({savings_pct:.0f}% cheaper)  "
                    f"lat={r['lat_mean_s']:.2f}s"
                )
        else:
            lines.append("No safe-downgrade candidates found.")

        lines += [
            "",
            f"Ceiling accuracy: {analysis['ceiling_accuracy']:.0%} "
            f"({', '.join(analysis['ceiling_models'])})",
            "",
        ]

    lines += [
        "ROUTING RECOMMENDATION (Phase O input — not applied here):",
        "",
        _build_recommendation(results, analysis, current_model),
        "",
        "NOTE: Live routing table (TIER_FOR_TASK/MODEL_FOR_TASK) is UNCHANGED.",
        "      Apply the routing change in Phase O after the operator reviews this table.",
        "=" * 90,
    ]
    return "\n".join(lines)


def _build_recommendation(results: list[dict], analysis: dict,
                           current_model: str) -> str:
    """Synthesize a routing recommendation from the frontier data."""
    if "error" in analysis:
        return f"Cannot generate recommendation: {analysis['error']}"

    safe = analysis.get("safe_downgrades", [])
    curr = analysis["current_baseline"]

    if not safe:
 # Check if a better but more expensive model exists
        better = [r for r in results
                  if r["accuracy_lenient"] > curr["accuracy_lenient"]
                  and r["model"] != current_model]
        if better:
            best = sorted(better, key=lambda r: -r["accuracy_lenient"])[0]
            return (
                f"No cheaper model matches current accuracy. "
                f"Best available upgrade: {best['model']} "
                f"({best['accuracy_lenient']:.0%} vs {curr['accuracy_lenient']:.0%} current, "
                f"${best['mean_cost_usd']:.5f}/call). "
                f"Recommend keeping current model ({current_model}) for now."
            )
        return (
            f"No cheaper model matches current accuracy and no accuracy upgrade found. "
            f"Keep {current_model} as the fit_judge model."
        )

 # Primary recommendation: cheapest safe-downgrade.
 # Secondary: pick by lowest latency among ties if latency matters.
    cheapest = safe[0]
    fastest_safe = min(safe, key=lambda r: r["lat_mean_s"])
    lines = [
        f"fit_judge (worker tier): replace {current_model} with {cheapest['model']}",
        f"  Accuracy: {cheapest['accuracy_lenient']:.0%} lenient (current: {curr['accuracy_lenient']:.0%})",
        f"  Cost:     ${cheapest['mean_cost_usd']:.5f}/call vs ${curr['mean_cost_usd']:.5f}/call current",
        (f"  Savings:  {(1 - cheapest['mean_cost_usd']/curr['mean_cost_usd'])*100:.0f}% per call"
         if curr["mean_cost_usd"] > 0 else ""),
        f"  Latency:  {cheapest['lat_mean_s']:.2f}s ± {cheapest['lat_std_s']:.2f}s (current: {curr['lat_mean_s']:.2f}s)",
    ]
    if fastest_safe["model"] != cheapest["model"]:
        lines += [
            "",
            f"  Latency-optimized alternative (same accuracy, slightly higher cost):",
            f"    {fastest_safe['model']}",
            f"    cost=${fastest_safe['mean_cost_usd']:.5f}/call  "
            f"latency={fastest_safe['lat_mean_s']:.2f}s ± {fastest_safe['lat_std_s']:.2f}s",
        ]

 # Phase O note
    lines += [
        "",
        "Phase O routing note:",
        "  fit_judge is a 'cheap' worker task (no multi-turn, short fixed output).",
        "  The orchestrator (telegram_chat / complex_reasoning) should stay Anthropic-native",
        "  to preserve the tool-loop, extended thinking, and tracing infrastructure.",
        "  Only the fit_judge leaf call is the safe-downgrade target.",
    ]
    return "\n".join(l for l in lines if l)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="fit_judge benchmark across candidate models."
    )
    ap.add_argument("--models", nargs="+", default=None,
                    help="Subset of models to sweep (default: all CANDIDATE_MODELS)")
    ap.add_argument("--n-samples", type=int, default=N_SAMPLES_DEFAULT,
                    help=f"Number of latency samples per label per model (default: {N_SAMPLES_DEFAULT})")
    ap.add_argument("--json", action="store_true",
                    help="Output machine-readable JSON (results + analysis)")
    ap.add_argument("--label-file", type=Path, default=_LABEL_FILE,
                    help=f"Path to benchmark label JSONL (default: {_LABEL_FILE})")
    ap.add_argument("--current-model", default=CURRENT_PROD_MODEL,
                    help=f"Current production model (baseline, default: {CURRENT_PROD_MODEL})")
    args = ap.parse_args()

    try:
        results = run_sweep(
            models=args.models,
            label_path=args.label_file,
            n_samples=args.n_samples,
        )
    except Exception as exc:
        print(f"SWEEP ERROR: {exc}", file=sys.stderr)
        raise

    analysis = _analyze_frontier(results, args.current_model)

    if args.json:
        print(json.dumps({
            "results": results,
            "analysis": analysis,
            "current_model": args.current_model,
        }, indent=2, default=str))
    else:
        print(format_table(results, args.current_model))


if __name__ == "__main__":
    main()
