"""oos_eval.py — out-of-sample validation of the fit_judge + model operating-point.

WHY THIS EXISTS
---------------
The fixture sets (fit_regression_set / recall_benchmark / seed-labels) are the
judge's CALIBRATION data — ~59 distinct roles the judge was tuned on, and the
whole ledger is in-sample too. Measuring the judge on the data it was tuned on
overstates accuracy. To answer "can we trust the judge?"
and "which model should fit_judge use?" honestly, we need roles the judge has
NEVER seen.

This harness pulls REAL postings from the last N days (read-only, via
source_multi's board pullers), dedups against everything in-sample, random-
samples, and runs the judge. It is built around two facts about out-of-sample
data:

  * We have NO ground-truth labels for fresh roles. So "accuracy" is only
    measurable where it is OBJECTIVE — the score_job kill-criteria (travel,
    onsite/hybrid, comp>$250K band, SDR/BDR, competitor). For FIT-vs-REACH
    discrimination there is no truth here; that is why Stage A sets aside the
    judge's FIT picks to a separate ledger tab for the operator to label (closing the
    loop into REAL out-of-sample ground truth).

  * What we CAN measure without labels:
      - WOBBLE / self-consistency: judge each role N times; an item whose
        verdict flips run-to-run is a STABILITY signal, not an accuracy one.
        This isolates the part of apparent error that is LLM nondeterminism
        rather than genuine error: each role is judged N times and the verdict
        flips are counted (model_sweep, by contrast, uses only the first run's
        verdict).
      - OBJECTIVE-NOT-FIT recall: of roles score_job flags with a hard kill,
        how many does the judge also cut? A FIT verdict on a kill-flagged role
        is a false-FIT (the dangerous error).
      - INTER-MODEL agreement (Stage B): does a cheaper model agree with the
        production judge's modal verdict, and how stable is it?

USAGE
-----
  python3 scripts/oos_eval.py --assemble --since 30 --n-roles 50 [--seed 7]
  python3 scripts/oos_eval.py --stage-a   [--repeats 5]
  python3 scripts/oos_eval.py --set-aside           # write FIT picks to ledger tab
  python3 scripts/oos_eval.py --stage-b   [--repeats 3] [--models a b c]

State is cached in OOS_SET_FILE between stages so each stage is independent and
re-runnable.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path


from alice.pipeline import source_multi
from alice.pipeline.score_job import score_listing
from alice.pipeline import model_sweep
from alice.pipeline import fit_judge

OOS_SET_FILE = Path("/tmp/oos_eval_set.json")
STAGE_A_FILE = Path("/tmp/oos_eval_stage_a.json")
STAGE_B_FILE = Path("/tmp/oos_eval_stage_b.json")

BASELINE_MODEL = model_sweep.BASELINE_MODEL if hasattr(model_sweep, "BASELINE_MODEL") \
    else "claude-haiku-4-5-20251001"

# US-provider candidates for the operating-point sweep (production control first).
DEFAULT_CANDIDATES = [
    "claude-haiku-4-5-20251001",            # control (current production)
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash",
    "meta-llama/llama-3.3-70b-instruct",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "openai/gpt-4o",
    "google/gemini-2.5-pro",
]

MAX_WORKERS = 6


# ─── in-sample url set (everything the judge has seen) ──────────────────────────

def _norm_url(u) -> str:
    u = str(u or "").strip().lower().rstrip("/")
    return u.split("?")[0]


def _in_sample_urls() -> set[str]:
    urls: set[str] = set()
    fixtures = [
        "tests/fixtures/fit_regression_set.jsonl",
        "tests/fixtures/recall_benchmark.jsonl",
        "tests/fixtures/recall_benchmark.seed-labels.jsonl",
    ]
    for f in fixtures:
        p = Path(f)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("url"):
                    urls.add(_norm_url(r["url"]))
 # entire ledger is in-sample
    try:
        from alice.persistence import ledger
        for r in ledger._ws().get_all_records():
            if str(r.get("url", "")).startswith("http"):
                urls.add(_norm_url(r["url"]))
    except Exception as e:
        print(f"[warn] could not read ledger for in-sample dedup: {e}", file=sys.stderr)
    return urls


# ─── assemble ───────────────────────────────────────────────────────────────────

def assemble(since_days: int, n_roles: int, seed: int) -> list[dict]:
    """Pull recent postings WITH body, dedup vs in-sample, random-sample n_roles.

    Returns label dicts in the shape model_sweep._judge_label expects, plus
    objective kill metadata from score_job and the source url.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    in_sample = _in_sample_urls()
    print(f"[assemble] in-sample urls to exclude: {len(in_sample)}")

    raw: list[dict] = []
    for display, ats, slug in source_multi.REGISTRY:
        puller = source_multi.PULLERS.get(ats)
        if not puller:
            continue
        try:
            rows = puller(slug, cutoff)
        except Exception as e:  # noqa
            print(f"[assemble] {display}/{ats}:{slug} fetch error: {e}", file=sys.stderr)
            continue
        for r in rows:
            listing = dict(r)
            listing["company"] = display
            listing["source"] = f"{ats}:{slug}"
            url = listing.get("posting_url", "")
            if _norm_url(url) in in_sample:
                continue
            body = listing.get("description", "") or ""
            if len(body.strip()) < 120:        # need enough JD text for a real judgment
                continue
            scored = score_listing(listing)
            raw.append({
                "id": f"oos-{len(raw):03d}",
                "company": display,
                "source": listing["source"],
                "url": url,
                "jd_snapshot": {
                    "title": listing.get("role_title", ""),
                    "body": body,
                    "location": listing.get("location"),
                    "comp_low": listing.get("base_salary_low"),
                    "comp_high": listing.get("base_salary_high"),
                    "remote_flag": listing.get("remote_policy"),
                },
                "kills": scored.get("kills", []),
                "score": scored.get("score"),
                "track": scored.get("track"),
                "travel_intensity": scored.get("travel_intensity"),
            })
        time.sleep(0.2)

    print(f"[assemble] fetched {len(raw)} fresh out-of-sample roles (body>=120 chars, deduped)")
    rng = random.Random(seed)
    rng.shuffle(raw)
    sample = raw[:n_roles]
    OOS_SET_FILE.write_text(json.dumps(
        {"since_days": since_days, "seed": seed, "n_requested": n_roles,
         "n_available": len(raw), "assembled_at": datetime.now().isoformat(timespec="seconds"),
         "roles": sample}, indent=2))
    print(f"[assemble] sampled {len(sample)} (seed={seed}) -> {OOS_SET_FILE}")
    kc = Counter(("kill" if r["kills"] else "clean") for r in sample)
    print(f"[assemble] objective kill-flagged in sample: {dict(kc)}")
    return sample


def _load_set() -> list[dict]:
    if not OOS_SET_FILE.exists():
        sys.exit(f"no OOS set at {OOS_SET_FILE} — run --assemble first")
    return json.loads(OOS_SET_FILE.read_text())["roles"]


# ─── shared: judge a set with one model, N repeats, threaded ────────────────────

def _judge_set(roles: list[dict], model: str, repeats: int) -> dict:
    """For each role, judge `repeats` times. Returns per-role run lists +
    aggregate cost/latency. Threaded across (role, attempt) pairs."""
    tasks = [(r, i) for r in roles for i in range(repeats)]
    per_role: dict[str, list[dict]] = {r["id"]: [] for r in roles}
    costs: list[float] = []
    lats: list[float] = []

    def _one(role, _i):
        return role["id"], model_sweep._judge_label(role, model)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(_one, r, i) for (r, i) in tasks]
        for fut in as_completed(futs):
            rid, res = fut.result()
            per_role[rid].append(res)
            costs.append(res.get("cost_usd", 0.0))
            lats.append(res.get("latency_s", 0.0))
    return {"per_role": per_role, "costs": costs, "latencies": lats}


def _modal(verdicts: list[str]) -> str:
    return Counter(verdicts).most_common(1)[0][0] if verdicts else "?"


def _consistent(verdicts: list[str]) -> bool:
    return len(set(verdicts)) == 1


# ─── Stage A: baseline judge, wobble + objective accuracy ───────────────────────

def stage_a(repeats: int):
    roles = _load_set()
    print(f"[stage-a] baseline judge ({BASELINE_MODEL}) x {repeats} on {len(roles)} OOS roles...")
    t0 = time.time()
    out = _judge_set(roles, BASELINE_MODEL, repeats)
    elapsed = time.time() - t0

    rows = []
    wobble = 0
    modal_dist = Counter()
    objective_kills = [r for r in roles if r["kills"]]
    kill_caught = 0
    false_fit = []     # judge FIT/REACH on a kill-flagged role
    for r in roles:
        runs = out["per_role"][r["id"]]
        verdicts = [x["verdict"] for x in runs]
        modal = _modal(verdicts)
        cons = _consistent(verdicts)
        if not cons:
            wobble += 1
        modal_dist[modal] += 1
        if r["kills"]:
            if modal == "NOT-FIT":
                kill_caught += 1
            else:
                false_fit.append((r, modal, verdicts))
        rows.append({
            "id": r["id"], "company": r["company"], "title": r["jd_snapshot"]["title"],
            "url": r["url"], "kills": r["kills"], "score": r["score"],
            "verdicts": verdicts, "modal": modal, "consistent": cons,
            "reason": runs[0].get("reason", ""),
            "driving_constraint": runs[0].get("driving_constraint", ""),
        })

    n = len(roles)
    total_cost = sum(out["costs"])
    report = {
        "model": BASELINE_MODEL, "repeats": repeats, "n_roles": n,
        "verdict_modal_dist": dict(modal_dist),
        "wobble_roles": wobble, "wobble_rate": round(wobble / n, 4) if n else 0,
        "objective_kill_flagged": len(objective_kills),
        "objective_kill_caught_NOTFIT": kill_caught,
        "objective_kill_recall": round(kill_caught / len(objective_kills), 4) if objective_kills else None,
        "false_fit_on_killed": [
            {"id": r["id"], "company": r["company"], "title": r["jd_snapshot"]["title"],
             "kills": r["kills"], "modal": m, "verdicts": v} for (r, m, v) in false_fit],
        "total_cost_usd": round(total_cost, 4),
        "cost_per_call_usd": round(total_cost / max(1, len(out["costs"])), 5),
        "latency_mean_s": round(statistics.mean(out["latencies"]), 2) if out["latencies"] else None,
        "elapsed_s": round(elapsed, 1),
        "rows": rows,
    }
    STAGE_A_FILE.write_text(json.dumps(report, indent=2))
    _print_stage_a(report)
    return report


def _print_stage_a(rep: dict):
    print("\n" + "=" * 70)
    print(f"STAGE A — baseline fit_judge ({rep['model']}), {rep['repeats']}x per role, "
          f"n={rep['n_roles']} OUT-OF-SAMPLE roles")
    print("=" * 70)
    print(f"  modal verdict distribution : {rep['verdict_modal_dist']}")
    print(f"  WOBBLE (verdict flips across runs): {rep['wobble_roles']}/{rep['n_roles']} "
          f"= {rep['wobble_rate']*100:.1f}%   (temp=0.0)")
    print(f"  objective kill-flagged roles      : {rep['objective_kill_flagged']}")
    if rep["objective_kill_recall"] is not None:
        print(f"  judge cut them (NOT-FIT)          : {rep['objective_kill_caught_NOTFIT']}"
              f"/{rep['objective_kill_flagged']}  (recall {rep['objective_kill_recall']*100:.0f}%)")
    if rep["false_fit_on_killed"]:
        print(f"  ⚠ FALSE-FIT on kill-flagged roles  : {len(rep['false_fit_on_killed'])}")
        for ff in rep["false_fit_on_killed"]:
            print(f"      - {ff['company']} / {ff['title']}  modal={ff['modal']} kills={ff['kills']}")
    print(f"  cost: ${rep['total_cost_usd']} total, ${rep['cost_per_call_usd']}/call  "
          f"| latency {rep['latency_mean_s']}s mean | {rep['elapsed_s']}s wall")
    fits = [r for r in rep["rows"] if r["modal"] == "FIT" and not r["kills"]]
    reaches = [r for r in rep["rows"] if r["modal"] == "REACH" and not r["kills"]]
    print(f"\n  → {len(fits)} FIT (clean) + {len(reaches)} REACH (clean) → eligible for set-aside review")
    for r in fits:
        flag = "" if r["consistent"] else "  (WOBBLED)"
        print(f"      FIT   {r['company']} / {r['title']}{flag}")


# ─── set aside FIT picks to a separate ledger tab ───────────────────────────────

def set_aside(include_reach: bool = False):
    if not STAGE_A_FILE.exists():
        sys.exit("run --stage-a first")
    rep = json.loads(STAGE_A_FILE.read_text())
    keep = {"FIT"} | ({"REACH"} if include_reach else set())
    picks = [r for r in rep["rows"] if r["modal"] in keep and not r["kills"]]
    if not picks:
        print("[set-aside] no clean FIT picks to set aside")
        return
    from alice.persistence import ledger
    ws_main = ledger._ws()
    ss = ws_main.spreadsheet
    tab = "OOS Review (Alice)"
    try:
        sheet = ss.worksheet(tab)
    except Exception:
        sheet = ss.add_worksheet(title=tab, rows=200, cols=10)
        sheet.append_row(["found_date", "company", "role", "verdict", "consistent",
                           "score", "url", "judge_reason", "operator_decision"])
    today = datetime.now().date().isoformat()
    appended = 0
    existing = {(_norm_url(r.get("url"))) for r in sheet.get_all_records()}
    for r in picks:
        if _norm_url(r["url"]) in existing:
            continue
        sheet.append_row([today, r["company"], r["title"], r["modal"],
                          "yes" if r["consistent"] else "WOBBLED", r["score"],
                          r["url"], (r["reason"] or "")[:300], ""])
        appended += 1
    print(f"[set-aside] wrote {appended} role(s) to ledger tab '{tab}' for review "
          f"({len(picks)-appended} already present)")


# ─── Stage B: model operating-point sweep ───────────────────────────────────────

def stage_b(repeats: int, models: list[str]):
    roles = _load_set()
 # baseline modal from Stage A (reference for agreement); recompute if absent
    base_modal: dict[str, str] = {}
    if STAGE_A_FILE.exists():
        for r in json.loads(STAGE_A_FILE.read_text())["rows"]:
            base_modal[r["id"]] = r["modal"]

    print(f"[stage-b] sweeping {len(models)} models x {repeats} on {len(roles)} OOS roles...")
    table = []
    for model in models:
        t0 = time.time()
        out = _judge_set(roles, model, repeats)
        elapsed = time.time() - t0
        wobble = 0
        modal_dist = Counter()
        agree = 0
        agree_denom = 0
        errors = 0
        per_role_modal = {}
        for r in roles:
            runs = out["per_role"][r["id"]]
            verdicts = [x["verdict"] for x in runs]
            errors += sum(1 for x in runs if x.get("error"))
            modal = _modal(verdicts)
            per_role_modal[r["id"]] = modal
            modal_dist[modal] += 1
            if not _consistent(verdicts):
                wobble += 1
            if r["id"] in base_modal:
                agree_denom += 1
                if modal == base_modal[r["id"]]:
                    agree += 1
        n = len(roles)
        total_cost = sum(out["costs"])
        table.append({
            "model": model,
            "modal_dist": dict(modal_dist),
            "wobble_rate": round(wobble / n, 4) if n else 0,
            "agreement_with_production": round(agree / agree_denom, 4) if agree_denom else None,
            "errors": errors,
            "cost_per_call_usd": round(total_cost / max(1, len(out["costs"])), 6),
            "total_cost_usd": round(total_cost, 4),
            "latency_mean_s": round(statistics.mean(out["latencies"]), 2) if out["latencies"] else None,
            "latency_std_s": round(statistics.pstdev(out["latencies"]), 2) if len(out["latencies"]) > 1 else 0,
            "elapsed_s": round(elapsed, 1),
        })
        m = table[-1]
        print(f"  {model:42s} agree={str(m['agreement_with_production']):6} "
              f"wobble={m['wobble_rate']*100:4.1f}% ${m['cost_per_call_usd']:.5f}/call "
              f"{m['latency_mean_s']}s err={errors}")

    STAGE_B_FILE.write_text(json.dumps({"repeats": repeats, "n_roles": len(roles),
                                        "baseline": BASELINE_MODEL, "table": table}, indent=2))
    _print_stage_b(table)
    return table


def _print_stage_b(table: list[dict]):
    print("\n" + "=" * 96)
    print("STAGE B — model operating-point (agreement vs production judge's modal verdict, OOS)")
    print("=" * 96)
    hdr = f"{'model':42s} {'agree':>6} {'wobble':>7} {'$/call':>9} {'lat_s':>6} {'err':>4}"
    print(hdr)
    print("-" * 96)
    for m in sorted(table, key=lambda x: -(x["agreement_with_production"] or 0)):
        ag = f"{m['agreement_with_production']*100:.0f}%" if m["agreement_with_production"] is not None else "n/a"
        print(f"{m['model']:42s} {ag:>6} {m['wobble_rate']*100:6.1f}% "
              f"{m['cost_per_call_usd']:>9.5f} {m['latency_mean_s']:>6} {m['errors']:>4}")


# ─── cli ─────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assemble", action="store_true")
    ap.add_argument("--stage-a", action="store_true")
    ap.add_argument("--set-aside", action="store_true")
    ap.add_argument("--include-reach", action="store_true", help="set-aside REACH too")
    ap.add_argument("--stage-b", action="store_true")
    ap.add_argument("--since", type=int, default=30)
    ap.add_argument("--n-roles", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--models", nargs="*", default=None)
    a = ap.parse_args()

    if a.assemble:
        assemble(a.since, a.n_roles, a.seed)
    if a.stage_a:
        stage_a(a.repeats)
    if a.set_aside:
        set_aside(include_reach=a.include_reach)
    if a.stage_b:
        stage_b(a.repeats, a.models or DEFAULT_CANDIDATES)
    if not any([a.assemble, a.stage_a, a.set_aside, a.stage_b]):
        ap.print_help()


if __name__ == "__main__":
    main()
