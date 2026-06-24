#!/usr/bin/env python3
"""
lead_cuts.py — reason-labeled cut log + the FROZEN_REASON_SET separation (G3, Trap-1 guard).

Every cut carries a reason from a FROZEN set (sourced from fit_model.toml
[learning_layer.reason_set], so the config — not code — is the source of truth).
write_cut() raises ValueError on an off-schema reason. read_fit_cuts() is the ONLY
reader into the human-tuning loop and filters to FIT_LEARNING_REASONS — so viability /
circumstance cuts are structurally INVISIBLE to fit-learning (they can't poison the
corpus that reshapes the rubric). This is the doc §6 contract.
"""
import json, os, tomllib, datetime
from alice import repo_paths

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = str(repo_paths.ROOT)
_CONFIG = os.path.join(_REPO, "config", "fit_model.toml")
_CUTLOG = os.path.join(_REPO, "feedback", "lead-cuts.jsonl")


def _reason_sets(path=_CONFIG):
    with open(path, "rb") as f:
        rs = tomllib.load(f)["learning_layer"]["reason_set"]
    return tuple(rs["fit_reasons"]), tuple(rs["circumstance_reasons"])


FIT_LEARNING_REASONS, CIRCUMSTANCE_REASONS = _reason_sets()
FROZEN_REASON_SET = frozenset(FIT_LEARNING_REASONS) | frozenset(CIRCUMSTANCE_REASONS)


def write_cut(lead_id, reason, *, company="", role="", note="", path=_CUTLOG, ts=None):
    """Append one reason-labeled cut. Raises ValueError on an off-schema reason
    (the structural guarantee that nothing un-categorized enters the corpus)."""
    if reason not in FROZEN_REASON_SET:
        raise ValueError(
            f"off-schema cut reason {reason!r}; must be one of FROZEN_REASON_SET="
            f"{sorted(FROZEN_REASON_SET)} (defined in fit_model.toml [learning_layer.reason_set])")
    rec = {"ts": ts or datetime.datetime.now().isoformat(timespec="seconds"),
           "lead_id": lead_id, "reason": reason, "is_fit_reason": reason in FIT_LEARNING_REASONS,
           "company": company, "role": role, "note": note}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def read_fit_cuts(path=_CUTLOG):
    """The ONLY reader into the tuning loop: returns ONLY fit-reason cuts.
    Circumstance/viability cuts are filtered out — invisible to fit-learning."""
    if not os.path.exists(path):
        return []
    out = []
    for ln in open(path):
        ln = ln.strip()
        if not ln:
            continue
        rec = json.loads(ln)
        if rec.get("reason") in FIT_LEARNING_REASONS:
            out.append(rec)
    return out


if __name__ == "__main__":
    print("FIT_LEARNING_REASONS:", FIT_LEARNING_REASONS)
    print("CIRCUMSTANCE_REASONS:", CIRCUMSTANCE_REASONS)
