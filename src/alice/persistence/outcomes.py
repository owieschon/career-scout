"""Outcome loop store (docs/FIT_STRATEGY_SPINE.md §5).

Append-only JSONL of REAL application outcomes, decomposed by funnel stage +
terminal reason, carrying the dimensional label + channel. This is the ground
truth that grounds the preference model in reality — the B3-flywheel seed.

Decompose, never binarize: "no offer" is NOT a fit-negative. An intermediate
positive (interview secured) is a label in its own right; the terminal "no" is
attributed to a subsystem (fit / performance / comp / withdrew / ghost) so the
loop never teaches the wrong lesson. Records are HUMAN-sourced (HITL), not
LLM-generated — `source` defaults to operator_hitl.
"""
import json
import os
from alice import repo_paths

_HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(str(repo_paths.ROOT), "state", "outcomes.jsonl")

FUNNEL_STAGES = ["applied", "response", "screen", "interview", "offer"]
TERMINAL_REASONS = ["fit_reject", "performance", "comp", "withdrew", "ghost", "pending"]


def record(*, company, role, reached, terminal=None, terminal_reason=None,
           channel=None, dimensions=None, channel_detail=None, notes=None,
           recorded_date=None, source="operator_hitl", job_key=None):
    """Append one outcome event. `reached` = furthest funnel stage reached;
    `terminal_reason` ∈ TERMINAL_REASONS. Returns the record."""
    if reached not in FUNNEL_STAGES:
        raise ValueError(f"reached must be one of {FUNNEL_STAGES}, got {reached!r}")
    if terminal_reason and terminal_reason not in TERMINAL_REASONS:
        raise ValueError(f"terminal_reason must be one of {TERMINAL_REASONS}")
    reached_idx = FUNNEL_STAGES.index(reached)
 # job_key MUST be the ledger job_key (= the fit-judge's listing_id, the key
 # record_prediction_span uses) for annotate_outcome to find the prediction span
 # when tracing is on. The company|role fallback is for manual entries only.
    rec = {
        "job_key": job_key or f"{company}|{role}".lower(),
        "company": company, "role": role,
        "funnel": {s: (i <= reached_idx) for i, s in enumerate(FUNNEL_STAGES)},
        "reached": reached,
        "terminal": terminal, "terminal_reason": terminal_reason,
        "channel": channel, "channel_detail": channel_detail,
        "dimensions": dimensions or {},
        "notes": notes, "source": source, "recorded_date": recorded_date,
    }
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
 # Reconciled outcome loop: this decomposed log is the SOURCE OF TRUTH; feed the
 # Phoenix B3 flywheel (telemetry.annotate_outcome) from it. No-op when tracing
 # off. The mapping preserves decomposition — reaching interview = the fit was
 # borne out ('advanced'), even on a no-offer, unless terminal_reason==fit_reject.
    try:
        from alice.observability import telemetry
        telemetry.annotate_outcome(rec["job_key"], _phoenix_status(reached, terminal_reason))
    except Exception:
        pass
    return rec


def _phoenix_status(reached, terminal_reason):
    """Decomposed outcome -> telemetry._OUTCOME_MAP status vocabulary."""
    if terminal_reason == "fit_reject":
        return "not a fit"          # rejected (0.0): the fit prediction was wrong
    if reached == "offer":
        return "offer"              # offer (1.0)
    if reached == "interview":
        return "interviewing"       # advanced (1.0): fit confirmed; no-offer was non-fit
    if reached == "screen":
        return "first screen scheduled"   # advanced (0.75)
    return "submitted"              # applied/response = in-flight (0.5)


def load(path=None):
    p = path or PATH
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]
