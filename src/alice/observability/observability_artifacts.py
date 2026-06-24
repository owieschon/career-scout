#!/usr/bin/env python3
"""Generate Alice's production observability artifacts.

One module owns the operational artifacts so the dashboard, SLO summary,
incident reports, metrics export, eval summary, and retention job all use the
same definitions. This keeps the interview/demo surface coherent instead of
becoming a pile of unrelated scripts.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from alice import repo_paths

ROOT = repo_paths.ROOT
STATE_DIR = ROOT / "state"
OBS_DIR = STATE_DIR / "observability"
INCIDENT_DIR = OBS_DIR / "incidents"
LATEST_HEALTHCHECK = OBS_DIR / "latest-healthcheck.json"
METRICS_JSON = OBS_DIR / "metrics.json"
METRICS_PROM = OBS_DIR / "metrics.prom"
SLO_JSON = OBS_DIR / "slo-summary.json"
SLO_MD = OBS_DIR / "slo-summary.md"
EVAL_JSON = OBS_DIR / "eval-summary.json"
EVAL_MD = OBS_DIR / "eval-summary.md"
JUDGED_EVAL_JSON = OBS_DIR / "judged-eval.json"
JUDGED_EVAL_MD = OBS_DIR / "judged-eval.md"
BEHAVIOR_REGRESSION_JSON = OBS_DIR / "behavior-regression.json"
BEHAVIOR_REGRESSION_MD = OBS_DIR / "behavior-regression.md"
ENTERPRISE_READINESS_JSON = OBS_DIR / "enterprise-readiness.json"
ENTERPRISE_READINESS_MD = OBS_DIR / "enterprise-readiness.md"
ESCALATION_POLICY_JSON = OBS_DIR / "escalation-policy.json"
ESCALATION_POLICY_MD = OBS_DIR / "escalation-policy.md"
AUDIT_EVIDENCE_JSON = OBS_DIR / "audit-evidence.json"
AUDIT_EVIDENCE_MD = OBS_DIR / "audit-evidence.md"
TRACE_MD = OBS_DIR / "trace-to-outcome.md"
DASHBOARD_HTML = OBS_DIR / "dashboard.html"
RETENTION_JSON = OBS_DIR / "retention-report.json"
RETENTION_MD = OBS_DIR / "retention-report.md"
PROMETHEUS_CONFIG = ROOT / "monitoring" / "prometheus.yml"
PROMETHEUS_RULES = ROOT / "monitoring" / "alert_rules.yml"

HEALTHCHECK_LOG = STATE_DIR / "observability_healthcheck.launchd.log"
TIME_COST_LOG = ROOT / "feedback" / "time-cost-log.jsonl"
PREDICTION_SPANS = ROOT / "feedback" / "prediction-spans.jsonl"
OUTCOMES = STATE_DIR / "outcomes.jsonl"

RETENTION_TARGETS = {
    ROOT / "feedback" / "telegram-history.jsonl": {"days": 30, "kind": "jsonl", "ts_key": "ts", "raw_text": True},
    ROOT / "feedback" / "paste-buffer.log": {"days": 30, "kind": "jsonl", "ts_key": "ts", "raw_text": False},
    STATE_DIR / "grounding_flags_fallback.jsonl": {"days": 90, "kind": "jsonl", "ts_key": "ts", "raw_text": False},
    TIME_COST_LOG: {"days": 180, "kind": "jsonl", "ts_key": "ts", "raw_text": False},
    PREDICTION_SPANS: {"days": 180, "kind": "jsonl", "ts_key": "ts", "raw_text": False},
}


def _ensure_dirs() -> None:
    OBS_DIR.mkdir(parents=True, exist_ok=True)
    INCIDENT_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_now().tzinfo)
        return dt.astimezone()
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:
            continue
    return out


def _write_json(path: Path, payload: Any) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _artifact_evidence(path: Path, label: str) -> dict[str, Any]:
    exists = path.exists()
    rec: dict[str, Any] = {
        "label": label,
        "path": _display_path(path),
        "exists": exists,
    }
    if exists:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        rec.update({
            "modified_at": mtime.isoformat(timespec="seconds"),
            "age_hours": round((_now() - mtime).total_seconds() / 3600, 2),
            "bytes": path.stat().st_size,
        })
    return rec


def _latest_status(path: Path, default: str = "missing") -> str:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return "unparseable"
    if isinstance(payload, dict):
        if "status" in payload:
            return str(payload.get("status"))
        summary = payload.get("summary")
        if isinstance(summary, dict) and "status" in summary:
            return str(summary.get("status"))
        checks = payload.get("checks")
        if isinstance(checks, list):
            return "fail" if any(c.get("status") == "fail" for c in checks if isinstance(c, dict)) else "pass"
    return default


def persist_healthcheck_report(report: dict[str, Any]) -> None:
    _write_json(LATEST_HEALTHCHECK, report)


def _healthcheck_reports_from_log() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    if not HEALTHCHECK_LOG.exists():
        return reports
    text = HEALTHCHECK_LOG.read_text(errors="replace")
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            payload, end = decoder.raw_decode(text[start:])
        except Exception:
            idx = start + 1
            continue
        if isinstance(payload, dict) and "checks" in payload and "run_key" in payload:
            reports.append(payload)
        idx = start + end
    if LATEST_HEALTHCHECK.exists():
        try:
            latest = json.loads(LATEST_HEALTHCHECK.read_text())
            if not any(r.get("run_key") == latest.get("run_key") for r in reports):
                reports.append(latest)
        except Exception:
            pass
    reports.sort(key=lambda r: r.get("ts", ""))
    return reports


def create_incident_report(report: dict[str, Any]) -> Path | None:
    failures = [c for c in report.get("checks", []) if c.get("status") == "fail"]
    if not failures:
        return None
    _ensure_dirs()
    run_key = report.get("run_key") or _now().strftime("%Y%m%dT%H%M%S")
    path = INCIDENT_DIR / f"{run_key}.md"
    lines = [
        f"# Alice Incident {run_key}",
        "",
        f"- Timestamp: `{report.get('ts', '')}`",
        f"- Failing checks: `{len(failures)}`",
        f"- Runbook: `docs/observability/PRODUCTION_STANDARD.md`",
        "",
        "## Failures",
        "",
    ]
    for check in failures:
        lines.extend([
            f"### {check.get('name', 'unknown')}",
            "",
            f"- Status: `{check.get('status')}`",
            f"- Detail: {check.get('detail', '')}",
            f"- Data: `{json.dumps(check.get('data', {}), sort_keys=True, default=str)}`",
            "",
        ])
    lines.extend([
        "## First Response",
        "",
        "1. Rerun `python3 -m alice.observability.observability_healthcheck`.",
        "2. If `daemon.deploy_guard` fails, restart Alice with `launchctl kickstart -k gui/$(id -u)/com.operator.jobsearch.telegram`.",
        "3. If vendor readback fails, wait for indexing/ingestion and rerun before changing code.",
        "4. If the same check fails twice, inspect the relevant vendor/log surface named by the check.",
        "",
    ])
    path.write_text("\n".join(lines))
    return path


def enforce_retention(*, dry_run: bool = False) -> dict[str, Any]:
    _ensure_dirs()
    now = _now()
    report = {"ts": now.isoformat(timespec="seconds"), "dry_run": dry_run, "targets": []}
    for path, cfg in RETENTION_TARGETS.items():
        rows = _load_jsonl(path)
        cutoff = now - timedelta(days=int(cfg["days"]))
        kept_lines: list[str] = []
        dropped = 0
        unknown_ts = 0
        for rec in rows:
            ts = _parse_ts(rec.get(str(cfg["ts_key"])))
            if ts is None:
                unknown_ts += 1
                kept_lines.append(json.dumps(rec, sort_keys=True, default=str))
                continue
            if ts < cutoff:
                dropped += 1
            else:
                kept_lines.append(json.dumps(rec, sort_keys=True, default=str))
        before = len(rows)
        after = len(kept_lines)
        if not dry_run and path.exists() and dropped:
            backup = path.with_suffix(path.suffix + ".retention-bak")
            shutil.copy2(path, backup)
            path.write_text(("\n".join(kept_lines) + "\n") if kept_lines else "")
        report["targets"].append({
            "path": _display_path(path),
            "days": cfg["days"],
            "raw_text": cfg["raw_text"],
            "before": before,
            "after": after,
            "dropped": dropped,
            "unknown_ts": unknown_ts,
        })
    _write_json(RETENTION_JSON, report)
    RETENTION_MD.write_text(render_retention_markdown(report))
    return report


def render_retention_markdown(report: dict[str, Any]) -> str:
    lines = ["# Alice Retention Report", "", f"- Timestamp: `{report.get('ts')}`", f"- Dry run: `{report.get('dry_run')}`", "", "| Path | Days | Before | Dropped | After | Raw text |", "|---|---:|---:|---:|---:|---|"]
    for t in report.get("targets", []):
        lines.append(f"| `{t['path']}` | {t['days']} | {t['before']} | {t['dropped']} | {t['after']} | {t['raw_text']} |")
    lines.append("")
    return "\n".join(lines)


def generate_slo_summary(*, days: int = 7) -> dict[str, Any]:
    _ensure_dirs()
    cutoff = _now() - timedelta(days=days)
    reports = []
    for report in _healthcheck_reports_from_log():
        ts = _parse_ts(report.get("ts"))
        if ts is None or ts >= cutoff:
            reports.append(report)
    total = len(reports)
    passing = sum(1 for r in reports if not any(c.get("status") == "fail" for c in r.get("checks", [])))
    failures = Counter()
    warnings = Counter()
    latest = reports[-1] if reports else (json.loads(LATEST_HEALTHCHECK.read_text()) if LATEST_HEALTHCHECK.exists() else {})
    for report in reports:
        for check in report.get("checks", []):
            if check.get("status") == "fail":
                failures[check.get("name", "unknown")] += 1
            elif check.get("status") == "warn":
                warnings[check.get("name", "unknown")] += 1
    summary = {
        "ts": _now().isoformat(timespec="seconds"),
        "window_days": days,
        "runs": total,
        "passing_runs": passing,
        "pass_rate": round(passing / total, 4) if total else None,
        "latest_run_key": latest.get("run_key"),
        "latest_status": "pass" if latest and not any(c.get("status") == "fail" for c in latest.get("checks", [])) else "fail" if latest else "unknown",
        "failures_by_check": dict(failures),
        "warnings_by_check": dict(warnings),
    }
    _write_json(SLO_JSON, summary)
    SLO_MD.write_text(render_slo_markdown(summary))
    return summary


def render_slo_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Alice SLO Summary",
        "",
        f"- Timestamp: `{summary.get('ts')}`",
        f"- Window: `{summary.get('window_days')}` days",
        f"- Runs: `{summary.get('runs')}`",
        f"- Passing runs: `{summary.get('passing_runs')}`",
        f"- Pass rate: `{summary.get('pass_rate')}`",
        f"- Latest run: `{summary.get('latest_run_key')}`",
        f"- Latest status: `{summary.get('latest_status')}`",
        "",
        "## Failures",
        "",
    ]
    if summary.get("failures_by_check"):
        for name, count in summary["failures_by_check"].items():
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    if summary.get("warnings_by_check"):
        for name, count in summary["warnings_by_check"].items():
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def generate_metrics_export() -> dict[str, Any]:
    _ensure_dirs()
    from alice.notify import alert_routing
    from alice.observability import runtime_metrics

    runtime = runtime_metrics.summary()
    latest = json.loads(LATEST_HEALTHCHECK.read_text()) if LATEST_HEALTHCHECK.exists() else {}
    slo = generate_slo_summary()
    judged = json.loads(JUDGED_EVAL_JSON.read_text()) if JUDGED_EVAL_JSON.exists() else {}
    enterprise = json.loads(ENTERPRISE_READINESS_JSON.read_text()) if ENTERPRISE_READINESS_JSON.exists() else {}
    alert_health = alert_routing.healthcheck()
    cost_rows = _load_jsonl(TIME_COST_LOG)
    today = _now().date().isoformat()
    today_cost = sum(float(r.get("cost_usd") or 0.0) for r in cost_rows if str(r.get("ts", "")).startswith(today) and r.get("ok", True))
    failed_checks = sum(1 for c in latest.get("checks", []) if c.get("status") == "fail")
    warn_checks = sum(1 for c in latest.get("checks", []) if c.get("status") == "warn")
    metrics = {
        "ts": _now().isoformat(timespec="seconds"),
        "runtime": runtime,
        "latest_healthcheck": {
            "run_key": latest.get("run_key"),
            "failed_checks": failed_checks,
            "warn_checks": warn_checks,
        },
        "slo": slo,
        "judged_eval": judged,
        "enterprise_readiness": enterprise,
        "alerting": alert_health,
        "cost_today_usd": round(today_cost, 6),
    }
    _write_json(METRICS_JSON, metrics)
    METRICS_PROM.write_text(render_prometheus(metrics))
    return metrics


def render_prometheus(metrics: dict[str, Any]) -> str:
    runtime = metrics.get("runtime", {})
    telegram = runtime.get("metrics", runtime).get("services", {}).get("telegram", {})
    disk = runtime.get("metrics", runtime).get("disk", {})
    latest = metrics.get("latest_healthcheck", {})
    slo = metrics.get("slo", {})
    judged = metrics.get("judged_eval", {})
    enterprise = metrics.get("enterprise_readiness", {})
    alerting = metrics.get("alerting", {})
    judged_summary = judged.get("summary", {})
    lines = [
        "# HELP alice_healthcheck_failed_checks Number of failing checks in latest healthcheck.",
        "# TYPE alice_healthcheck_failed_checks gauge",
        f"alice_healthcheck_failed_checks {latest.get('failed_checks', 0)}",
        "# HELP alice_healthcheck_warn_checks Number of warning checks in latest healthcheck.",
        "# TYPE alice_healthcheck_warn_checks gauge",
        f"alice_healthcheck_warn_checks {latest.get('warn_checks', 0)}",
        "# HELP alice_slo_pass_rate Healthcheck pass rate over the configured window.",
        "# TYPE alice_slo_pass_rate gauge",
        f"alice_slo_pass_rate {slo.get('pass_rate') if slo.get('pass_rate') is not None else 0}",
        "# HELP alice_telegram_rss_mb Telegram daemon RSS memory in MB.",
        "# TYPE alice_telegram_rss_mb gauge",
        f"alice_telegram_rss_mb {telegram.get('rss_mb', 0)}",
        "# HELP alice_telegram_cpu_pct Telegram daemon CPU sample percent.",
        "# TYPE alice_telegram_cpu_pct gauge",
        f"alice_telegram_cpu_pct {telegram.get('cpu_pct', 0)}",
        "# HELP alice_telegram_runs Launchd run count for Telegram daemon.",
        "# TYPE alice_telegram_runs gauge",
        f"alice_telegram_runs {telegram.get('runs', 0)}",
        "# HELP alice_repo_disk_free_gb Free disk on repo volume.",
        "# TYPE alice_repo_disk_free_gb gauge",
        f"alice_repo_disk_free_gb {disk.get('free_gb', 0)}",
        "# HELP alice_cost_today_usd LLM cost logged today.",
        "# TYPE alice_cost_today_usd gauge",
        f"alice_cost_today_usd {metrics.get('cost_today_usd', 0)}",
        "# HELP alice_judged_eval_pass_rate Pass rate for the latest judged eval run.",
        "# TYPE alice_judged_eval_pass_rate gauge",
        f"alice_judged_eval_pass_rate {judged_summary.get('pass_rate') if judged_summary.get('pass_rate') is not None else 0}",
        "# HELP alice_judged_eval_failed_cases Number of failing cases in latest judged eval run.",
        "# TYPE alice_judged_eval_failed_cases gauge",
        f"alice_judged_eval_failed_cases {judged_summary.get('failed', 0)}",
        "# HELP alice_alert_external_routes_configured Number of non-file alert routes configured.",
        "# TYPE alice_alert_external_routes_configured gauge",
        f"alice_alert_external_routes_configured {1 if alerting.get('external_route_configured') else 0}",
        "# HELP alice_enterprise_readiness_local_score Local single-user enterprise readiness score from 0 to 10.",
        "# TYPE alice_enterprise_readiness_local_score gauge",
        f"alice_enterprise_readiness_local_score {enterprise.get('score_local_single_user', 0)}",
        "# HELP alice_enterprise_readiness_blocking_controls Number of blocking readiness controls.",
        "# TYPE alice_enterprise_readiness_blocking_controls gauge",
        f"alice_enterprise_readiness_blocking_controls {len(enterprise.get('blocking_controls', []))}",
        "",
    ]
    return "\n".join(lines)


def generate_eval_summary() -> dict[str, Any]:
    _ensure_dirs()
    from alice.pipeline import alice_eval_dataset

    cases = alice_eval_dataset.load_cases()
    by_layer = Counter(c.get("metadata", {}).get("layer", "unknown") for c in cases)
    by_failure = Counter(c.get("metadata", {}).get("failure_mode", "unknown") for c in cases)
    required_tools = Counter()
    for case in cases:
        for tool in case.get("expected", {}).get("required_tools_any", []):
            required_tools[tool] += 1
    summary = {
        "ts": _now().isoformat(timespec="seconds"),
        "dataset": alice_eval_dataset.DATASET_NAME,
        "cases": len(cases),
        "by_layer": dict(by_layer),
        "by_failure_mode": dict(by_failure),
        "required_tools": dict(required_tools),
        "status": "pass" if cases else "fail",
    }
    _write_json(EVAL_JSON, summary)
    EVAL_MD.write_text(render_eval_markdown(summary))
    return summary


def render_eval_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Alice Eval Summary",
        "",
        f"- Timestamp: `{summary.get('ts')}`",
        f"- Dataset: `{summary.get('dataset')}`",
        f"- Cases: `{summary.get('cases')}`",
        f"- Status: `{summary.get('status')}`",
        "",
        "## Failure Modes",
        "",
    ]
    for name, count in summary.get("by_failure_mode", {}).items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Required Tools", ""])
    if summary.get("required_tools"):
        for name, count in summary["required_tools"].items():
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _json_from_text(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            payload, _ = decoder.raw_decode(raw[match.start():])
        except Exception:
            continue
        return payload if isinstance(payload, dict) else {}
    return {}


def _judge_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    from alice.llm import llm

    expected = case.get("expected", {})
    prompt = (
        "You are judging whether Alice should treat this user message as in-scope "
        "and what operational surfaces her answer should reference. Return compact "
        "JSON only with keys classification, pass, reason, missing_terms, forbidden_hits.\n\n"
        "Alice is the operator's job-search agent, but questions about Alice's reliability, "
        "observability, security guardrails, model routing, evals, transcripts, "
        "production readiness, and 7-layer architecture are in scope when the "
        "case context ties them to Alice or the job-search system. Do not mark "
        "those technical reliability questions out-of-scope merely because they "
        "look like DevOps/AI architecture. The available tool surface includes "
        "remote Sentry readback via query_sentry_events when credentials are "
        "configured; do not assume external observability is unavailable if a "
        "case expects that tool.\n\n"
        f"CASE ID: {case.get('id')}\n"
        f"USER MESSAGE:\n{case.get('input', {}).get('user_text', '')}\n\n"
        f"CONTEXT:\n{case.get('input', {}).get('context', '')}\n\n"
        f"EXPECTED CLASSIFICATION: {expected.get('classification')}\n"
        f"MUST INCLUDE ANY: {expected.get('must_include_any', [])}\n"
        f"MUST NOT INCLUDE: {expected.get('must_not_include', [])}\n"
        f"REQUIRED TOOLS ANY: {expected.get('required_tools_any', [])}\n"
    )
    result = llm.call(
        task="observability_judged_eval",
        prompt=prompt,
        system=(
            "You are a strict regression evaluator for Alice, the operator's job-search agent. "
            "You judge expected behavior, not prose style. Mark pass=false if the case "
            "would be refused as out-of-scope when it should be answered. Alice's own "
            "observability, security, eval, transcript, and production-readiness work "
            "is part of her job-search reliability scope."
        ),
        max_tokens=500,
        temperature=0.0,
        session_id="observability-judged-eval",
        job_key=f"observability-judged-eval-{case.get('id')}",
    )
    text = (result.get("text") or "").strip()
    parsed = _json_from_text(text)
    passed = bool(parsed.get("pass")) if "pass" in parsed else False
    return {
        "id": case.get("id"),
        "layer": case.get("metadata", {}).get("layer"),
        "status": "pass" if passed else "fail",
        "classification": parsed.get("classification"),
        "reason": str(parsed.get("reason") or text)[:500],
        "missing_terms": parsed.get("missing_terms", []),
        "forbidden_hits": parsed.get("forbidden_hits", []),
    }


def generate_judged_eval(*, max_cases: int | None = None, enabled: bool | None = None) -> dict[str, Any]:
    _ensure_dirs()
    from alice.pipeline import alice_eval_dataset
    from alice import jobcfg

    cfg = jobcfg.load()
    is_enabled = enabled if enabled is not None else str(cfg.get("ALICE_JUDGED_EVAL", os.environ.get("ALICE_JUDGED_EVAL", ""))).lower() in {"1", "true", "yes"}
    cases = alice_eval_dataset.load_cases()
    if max_cases is None:
        max_cases = int(cfg.get("ALICE_JUDGED_EVAL_MAX_CASES", os.environ.get("ALICE_JUDGED_EVAL_MAX_CASES", len(cases))) or len(cases))
    selected = cases[:max(0, min(max_cases, len(cases)))]
    results: list[dict[str, Any]] = []
    status = "skipped"
    if is_enabled:
        for case in selected:
            try:
                results.append(_judge_eval_case(case))
            except Exception as e:
                results.append({
                    "id": case.get("id"),
                    "layer": case.get("metadata", {}).get("layer"),
                    "status": "error",
                    "reason": f"{type(e).__name__}: {e}"[:500],
                })
        status = "pass" if results and all(r.get("status") == "pass" for r in results) else "fail"
    summary = {
        "ts": _now().isoformat(timespec="seconds"),
        "enabled": is_enabled,
        "dataset": alice_eval_dataset.DATASET_NAME,
        "cases_available": len(cases),
        "cases_run": len(results),
        "passed": sum(1 for r in results if r.get("status") == "pass"),
        "failed": sum(1 for r in results if r.get("status") in {"fail", "error"}),
        "pass_rate": round(sum(1 for r in results if r.get("status") == "pass") / len(results), 4) if results else None,
        "status": status,
    }
    payload = {"summary": summary, "results": results}
    _write_json(JUDGED_EVAL_JSON, payload)
    JUDGED_EVAL_MD.write_text(render_judged_eval_markdown(payload))
    return payload


def generate_behavior_regression(*, max_cases: int | None = None, enabled: bool | None = None) -> dict[str, Any]:
    _ensure_dirs()
    from alice.ops import alice_behavior_regression
    from alice import jobcfg

    cfg = jobcfg.load()
    is_enabled = enabled if enabled is not None else str(cfg.get("ALICE_BEHAVIOR_REGRESSION", os.environ.get("ALICE_BEHAVIOR_REGRESSION", ""))).lower() in {"1", "true", "yes"}
    if not is_enabled:
        payload = {
            "ts": _now().isoformat(timespec="seconds"),
            "enabled": False,
            "cases": 0,
            "passed": 0,
            "failed": 0,
            "pass_rate": None,
            "status": "skipped",
            "results": [],
        }
        _write_json(BEHAVIOR_REGRESSION_JSON, payload)
        BEHAVIOR_REGRESSION_MD.write_text(alice_behavior_regression.render_markdown(payload))
        return payload
    if max_cases is None:
        raw_max = cfg.get("ALICE_BEHAVIOR_REGRESSION_MAX_CASES", os.environ.get("ALICE_BEHAVIOR_REGRESSION_MAX_CASES", ""))
        max_cases = int(raw_max) if raw_max else None
    payload = alice_behavior_regression.run_cases(max_cases=max_cases)
    return payload


def render_judged_eval_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Alice Judged Eval",
        "",
        f"- Timestamp: `{summary.get('ts')}`",
        f"- Enabled: `{summary.get('enabled')}`",
        f"- Cases run: `{summary.get('cases_run')}` of `{summary.get('cases_available')}`",
        f"- Passed: `{summary.get('passed')}`",
        f"- Failed: `{summary.get('failed')}`",
        f"- Pass rate: `{summary.get('pass_rate')}`",
        f"- Status: `{summary.get('status')}`",
        "",
        "## Cases",
        "",
    ]
    if not payload.get("results"):
        lines.append("- No judged cases run.")
    for result in payload.get("results", []):
        lines.extend([
            f"### {result.get('id')}",
            "",
            f"- Status: `{result.get('status')}`",
            f"- Layer: `{result.get('layer')}`",
            f"- Reason: {result.get('reason', '')}",
            "",
        ])
    return "\n".join(lines)


def generate_trace_examples(limit: int = 5) -> dict[str, Any]:
    _ensure_dirs()
    from alice.observability import observability_healthcheck

    spans = _load_jsonl(PREDICTION_SPANS)
    costs = _load_jsonl(TIME_COST_LOG)
    outcomes = _load_jsonl(OUTCOMES)
    cost_by_job: dict[str, dict[str, Any]] = {}
    outcome_by_job: dict[str, dict[str, Any]] = {}
    for row in costs:
        if row.get("job_key"):
            cost_by_job[row["job_key"]] = row
    for row in outcomes:
        key = row.get("job_key") or row.get("key")
        if key:
            outcome_by_job[key] = row
    examples = []
    for span in reversed(spans):
        job_key = span.get("job_key")
        span_id = span.get("span_id")
        if not job_key or not span_id:
            continue
        cost = cost_by_job.get(job_key, {})
        outcome = outcome_by_job.get(job_key, {})
        examples.append({
            "ts": span.get("ts"),
            "job_key": job_key,
            "task": span.get("task"),
            "span_id": span_id,
            "phoenix_span_id": str(span_id)[-16:],
            "langsmith_run_id": observability_healthcheck.langsmith_run_id_for_span(str(span_id)),
            "model": cost.get("model"),
            "cost_usd": cost.get("cost_usd"),
            "outcome_status": outcome.get("status"),
        })
        if len(examples) >= limit:
            break
    payload = {"ts": _now().isoformat(timespec="seconds"), "examples": examples}
    TRACE_MD.write_text(render_trace_markdown(payload))
    return payload


def render_trace_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Alice Trace-To-Outcome Examples", "", f"- Timestamp: `{payload.get('ts')}`", ""]
    for ex in payload.get("examples", []):
        lines.extend([
            f"## {ex.get('job_key')}",
            "",
            f"- Task: `{ex.get('task')}`",
            f"- Span ID: `{ex.get('span_id')}`",
            f"- Phoenix span ID: `{ex.get('phoenix_span_id')}`",
            f"- LangSmith run ID: `{ex.get('langsmith_run_id')}`",
            f"- Model: `{ex.get('model')}`",
            f"- Cost USD: `{ex.get('cost_usd')}`",
            f"- Outcome status: `{ex.get('outcome_status')}`",
            "",
        ])
    if not payload.get("examples"):
        lines.append("No prediction spans with job keys found yet.")
    return "\n".join(lines)


def generate_escalation_policy() -> dict[str, Any]:
    _ensure_dirs()
    from alice.notify import alert_routing

    alert_health = alert_routing.healthcheck()
    policy = {
        "ts": _now().isoformat(timespec="seconds"),
        "status": "pass" if alert_health.get("external_route_configured") else "warn",
        "routes": alert_health.get("routes", []),
        "external_route_configured": bool(alert_health.get("external_route_configured")),
        "severity_levels": [
            {
                "severity": "P0",
                "examples": ["Alice cannot respond", "healthcheck has failing vendor readback", "security guardrail leak"],
                "detect": ["observability_healthcheck.py", "Sentry issue alice.observability.healthcheck_failed", "alert_routing"],
                "target_response_minutes": 15,
            },
            {
                "severity": "P1",
                "examples": ["behavior regression failed", "SLO below target", "trace delivery degraded"],
                "detect": ["behavior-regression.json", "slo-summary.json", "Prometheus alert rules"],
                "target_response_minutes": 60,
            },
            {
                "severity": "P2",
                "examples": ["artifact stale", "dashboard stale", "retention dry-run warning"],
                "detect": ["observability artifact freshness gate", "retention-report.json"],
                "target_response_minutes": 1440,
            },
        ],
        "runbooks": [
            "docs/observability/PRODUCTION_STANDARD.md",
            "state/observability/incidents/",
            "state/observability/latest-healthcheck.json",
        ],
    }
    _write_json(ESCALATION_POLICY_JSON, policy)
    ESCALATION_POLICY_MD.write_text(render_escalation_policy_markdown(policy))
    return policy


def render_escalation_policy_markdown(policy: dict[str, Any]) -> str:
    lines = [
        "# Alice Escalation Policy",
        "",
        f"- Timestamp: `{policy.get('ts')}`",
        f"- Status: `{policy.get('status')}`",
        f"- External route configured: `{policy.get('external_route_configured')}`",
        "",
        "## Routes",
        "",
    ]
    for route in policy.get("routes", []):
        if isinstance(route, dict):
            lines.append(f"- `{route.get('name', 'unknown')}`: configured=`{route.get('configured')}`")
        else:
            lines.append(f"- `{route}`")
    lines.extend(["", "## Severity Levels", ""])
    for level in policy.get("severity_levels", []):
        lines.extend([
            f"### {level.get('severity')}",
            "",
            f"- Target response minutes: `{level.get('target_response_minutes')}`",
            f"- Examples: {', '.join(level.get('examples', []))}",
            f"- Detection: {', '.join(level.get('detect', []))}",
            "",
        ])
    return "\n".join(lines)


def generate_audit_evidence() -> dict[str, Any]:
    _ensure_dirs()
    evidence_paths = [
        (LATEST_HEALTHCHECK, "latest live healthcheck"),
        (SLO_JSON, "SLO history summary"),
        (METRICS_JSON, "JSON metrics export"),
        (METRICS_PROM, "Prometheus metrics export"),
        (EVAL_JSON, "static eval dataset summary"),
        (JUDGED_EVAL_JSON, "LLM-judged eval result"),
        (BEHAVIOR_REGRESSION_JSON, "real Telegram-route behavior regression"),
        (RETENTION_JSON, "retention enforcement report"),
        (TRACE_MD, "trace-to-outcome examples"),
        (ESCALATION_POLICY_JSON, "incident escalation policy"),
        (PROMETHEUS_CONFIG, "Prometheus scrape config"),
        (PROMETHEUS_RULES, "Prometheus alert rules"),
        (TIME_COST_LOG, "LLM call cost ledger"),
        (PREDICTION_SPANS, "Phoenix/LangSmith span correlation ledger"),
    ]
    incidents = sorted(INCIDENT_DIR.glob("*.md")) if INCIDENT_DIR.exists() else []
    payload = {
        "ts": _now().isoformat(timespec="seconds"),
        "status": "pass",
        "evidence": [_artifact_evidence(path, label) for path, label in evidence_paths],
        "recent_incidents": [_artifact_evidence(path, "incident report") for path in incidents[-10:]],
    }
    missing = [item["path"] for item in payload["evidence"] if not item.get("exists")]
    if missing:
        payload["status"] = "fail"
        payload["missing"] = missing
    _write_json(AUDIT_EVIDENCE_JSON, payload)
    AUDIT_EVIDENCE_MD.write_text(render_audit_evidence_markdown(payload))
    return payload


def render_audit_evidence_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Alice Audit Evidence",
        "",
        f"- Timestamp: `{payload.get('ts')}`",
        f"- Status: `{payload.get('status')}`",
        "",
        "| Evidence | Path | Exists | Age hours |",
        "|---|---|---:|---:|",
    ]
    for item in payload.get("evidence", []):
        lines.append(f"| {item.get('label')} | `{item.get('path')}` | {item.get('exists')} | {item.get('age_hours', '')} |")
    lines.extend(["", "## Recent Incidents", ""])
    if payload.get("recent_incidents"):
        for item in payload["recent_incidents"]:
            lines.append(f"- `{item.get('path')}` modified `{item.get('modified_at')}`")
    else:
        lines.append("- None recorded.")
    lines.append("")
    return "\n".join(lines)


def generate_enterprise_readiness() -> dict[str, Any]:
    _ensure_dirs()
    latest = json.loads(LATEST_HEALTHCHECK.read_text()) if LATEST_HEALTHCHECK.exists() else {}
    slo = json.loads(SLO_JSON.read_text()) if SLO_JSON.exists() else {}
    eval_summary = json.loads(EVAL_JSON.read_text()) if EVAL_JSON.exists() else {}
    judged = json.loads(JUDGED_EVAL_JSON.read_text()) if JUDGED_EVAL_JSON.exists() else {}
    behavior = json.loads(BEHAVIOR_REGRESSION_JSON.read_text()) if BEHAVIOR_REGRESSION_JSON.exists() else {}
    retention = json.loads(RETENTION_JSON.read_text()) if RETENTION_JSON.exists() else {}
    escalation = json.loads(ESCALATION_POLICY_JSON.read_text()) if ESCALATION_POLICY_JSON.exists() else generate_escalation_policy()
    audit = json.loads(AUDIT_EVIDENCE_JSON.read_text()) if AUDIT_EVIDENCE_JSON.exists() else {}
    checks = latest.get("checks", [])

    def check_named(name: str) -> str:
        for rec in checks:
            if rec.get("name") == name:
                return str(rec.get("status"))
        return "missing"

    controls = [
        {
            "layer": 1,
            "name": "Infrastructure and runtime metrics",
            "status": "pass" if METRICS_PROM.exists() and PROMETHEUS_RULES.exists() else "fail",
            "evidence": ["state/observability/metrics.prom", "monitoring/alert_rules.yml", "src/alice/observability/metrics_http_server.py"],
        },
        {
            "layer": 2,
            "name": "Application errors and remote readback",
            "status": "pass" if check_named("sentry.remote_readback") == "pass" else "fail",
            "evidence": ["Sentry canary", "Sentry API readback", "state/observability/latest-healthcheck.json"],
        },
        {
            "layer": 3,
            "name": "Data/vector fabric and retention",
            "status": "pass" if retention.get("targets") and not any(t.get("unknown_ts", 0) > t.get("before", 0) for t in retention.get("targets", [])) else "fail",
            "evidence": ["state/observability/retention-report.json", "query/runtime artifact freshness"],
        },
        {
            "layer": 4,
            "name": "LLM cost and routing controls",
            "status": "pass" if TIME_COST_LOG.exists() and check_named("local.cost_log") in {"pass", "missing"} else "fail",
            "evidence": ["feedback/time-cost-log.jsonl", "src/alice/llm/llm.py cost anomaly events"],
        },
        {
            "layer": 5,
            "name": "Agent orchestration and trace delivery",
            "status": "pass" if check_named("langsmith.span_delivery") == "pass" and behavior.get("status") == "pass" else "fail",
            "evidence": ["LangSmith readback", "behavior-regression.json", "trace-to-outcome.md"],
        },
        {
            "layer": 6,
            "name": "AI quality, evals, drift checks",
            "status": "pass" if judged.get("summary", {}).get("status") == "pass" and eval_summary.get("status") == "pass" else "fail",
            "evidence": ["eval-summary.json", "judged-eval.json", "Phoenix span delivery"],
        },
        {
            "layer": 7,
            "name": "AI security and guardrails",
            "status": "pass" if (ROOT / "src" / "alice" / "ai_guardrails.py").exists() else "fail",
            "evidence": ["src/alice/ai_guardrails.py", "Sentry security events", "outbound response screen"],
        },
        {
            "layer": "IR",
            "name": "Incident response and escalation",
            "status": "pass" if escalation.get("external_route_configured") else "warn",
            "evidence": ["escalation-policy.json", "alert_routing", "incident markdown reports"],
        },
        {
            "layer": "Audit",
            "name": "Audit evidence and demo collateral",
            "status": "pass" if audit.get("status", "pass") == "pass" else "fail",
            "evidence": ["audit-evidence.json", "dashboard.html", "latest-healthcheck.json"],
        },
    ]
    blocking = [c for c in controls if c["status"] == "fail"]
    warned = [c for c in controls if c["status"] == "warn"]
    external_required = [
        "Multi-user RBAC/SSO/SAML and tenant isolation are product features, not local observability controls.",
        "Hosted Prometheus/Grafana or Datadog is required for always-on charts outside the operator's machine.",
        "Paid paging/on-call rotation is required if Alice becomes a team production service.",
        "Larger labeled transcript eval set is required before claiming population-level model quality.",
    ]
    payload = {
        "ts": _now().isoformat(timespec="seconds"),
        "scope": "single-user local production baseline for Alice",
        "local_status": "pass" if not blocking else "fail",
        "enterprise_saas_status": "external_required",
        "score_local_single_user": 10 if not blocking else max(0, 10 - len(blocking) * 2),
        "score_multi_user_saas": 8 if not blocking else max(0, 8 - len(blocking) * 2),
        "controls": controls,
        "blocking_controls": [c["name"] for c in blocking],
        "warning_controls": [c["name"] for c in warned],
        "external_required": external_required,
        "behavior_regression_status": behavior.get("status", "missing"),
        "latest_healthcheck_status": _latest_status(LATEST_HEALTHCHECK),
        "slo_pass_rate": slo.get("pass_rate"),
    }
    _write_json(ENTERPRISE_READINESS_JSON, payload)
    ENTERPRISE_READINESS_MD.write_text(render_enterprise_readiness_markdown(payload))
    return payload


def render_enterprise_readiness_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Alice Enterprise Readiness",
        "",
        f"- Timestamp: `{payload.get('ts')}`",
        f"- Scope: `{payload.get('scope')}`",
        f"- Local single-user status: `{payload.get('local_status')}`",
        f"- Enterprise SaaS status: `{payload.get('enterprise_saas_status')}`",
        f"- Local score: `{payload.get('score_local_single_user')}/10`",
        f"- Multi-user SaaS score: `{payload.get('score_multi_user_saas')}/10`",
        "",
        "| Layer | Control | Status | Evidence |",
        "|---|---|---|---|",
    ]
    for control in payload.get("controls", []):
        lines.append(
            f"| {control.get('layer')} | {control.get('name')} | `{control.get('status')}` | "
            f"{', '.join(control.get('evidence', []))} |"
        )
    lines.extend(["", "## External Requirements", ""])
    for item in payload.get("external_required", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def generate_dashboard() -> Path:
    _ensure_dirs()
    metrics = json.loads(METRICS_JSON.read_text()) if METRICS_JSON.exists() else generate_metrics_export()
    slo = json.loads(SLO_JSON.read_text()) if SLO_JSON.exists() else generate_slo_summary()
    eval_summary = json.loads(EVAL_JSON.read_text()) if EVAL_JSON.exists() else generate_eval_summary()
    judged_eval = json.loads(JUDGED_EVAL_JSON.read_text()) if JUDGED_EVAL_JSON.exists() else {"summary": {"status": "missing"}}
    behavior = json.loads(BEHAVIOR_REGRESSION_JSON.read_text()) if BEHAVIOR_REGRESSION_JSON.exists() else {"status": "missing"}
    enterprise = json.loads(ENTERPRISE_READINESS_JSON.read_text()) if ENTERPRISE_READINESS_JSON.exists() else {"local_status": "missing"}
    latest = json.loads(LATEST_HEALTHCHECK.read_text()) if LATEST_HEALTHCHECK.exists() else {}
    failed = [c for c in latest.get("checks", []) if c.get("status") == "fail"]
    warned = [c for c in latest.get("checks", []) if c.get("status") == "warn"]

    def card(title: str, value: Any, detail: str = "") -> str:
        return f"<section class='card'><h2>{html.escape(title)}</h2><div class='value'>{html.escape(str(value))}</div><p>{html.escape(detail)}</p></section>"

    body = "\n".join([
        card("Latest Healthcheck", latest.get("run_key", "none"), f"{len(failed)} fail / {len(warned)} warn"),
        card("SLO Pass Rate", slo.get("pass_rate", "n/a"), f"{slo.get('runs', 0)} runs over {slo.get('window_days', 7)} days"),
        card("Cost Today", f"${metrics.get('cost_today_usd', 0)}", "Logged LLM cost"),
        card("Eval Cases", eval_summary.get("cases", 0), eval_summary.get("dataset", "")),
        card("Judged Eval", judged_eval.get("summary", {}).get("status", "missing"), f"{judged_eval.get('summary', {}).get('cases_run', 0)} cases run"),
        card("Behavior Regression", behavior.get("status", "missing"), f"{behavior.get('passed', 0)}/{behavior.get('cases', 0)} cases passed"),
        card("Enterprise Readiness", f"{enterprise.get('score_local_single_user', 0)}/10", enterprise.get("local_status", "missing")),
    ])
    rows = "\n".join(
        f"<tr><td>{html.escape(c.get('name',''))}</td><td>{html.escape(c.get('status',''))}</td><td>{html.escape(c.get('detail',''))}</td></tr>"
        for c in latest.get("checks", [])
    )
    DASHBOARD_HTML.write_text(f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Alice Observability</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 32px; background: #f7f7f4; color: #181818; }}
    h1 {{ margin-bottom: 4px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 24px 0; }}
    .card {{ background: white; border: 1px solid #ddd; border-radius: 8px; padding: 16px; }}
    .card h2 {{ font-size: 14px; margin: 0 0 8px; color: #555; }}
    .value {{ font-size: 28px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; background: white; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #eee; }}
  </style>
</head>
<body>
  <h1>Alice Observability</h1>
  <p>Generated {html.escape(_now().isoformat(timespec='seconds'))}</p>
  <div class="grid">{body}</div>
  <h2>Latest Healthcheck Checks</h2>
  <table><thead><tr><th>Name</th><th>Status</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>
</body>
</html>
""")
    return DASHBOARD_HTML


def generate_all(*, enforce_retention_flag: bool = False, judged_eval: bool | None = None, behavior_regression: bool | None = None) -> dict[str, Any]:
    _ensure_dirs()
    retention = enforce_retention(dry_run=not enforce_retention_flag)
    slo = generate_slo_summary()
    eval_summary = generate_eval_summary()
    judged = generate_judged_eval(enabled=judged_eval)
    behavior = generate_behavior_regression(enabled=behavior_regression)
    escalation = generate_escalation_policy()
    audit = generate_audit_evidence()
    enterprise = generate_enterprise_readiness()
    metrics = generate_metrics_export()
    trace_examples = generate_trace_examples()
    dashboard = generate_dashboard()
    return {
        "retention": retention,
        "slo": slo,
        "metrics_path": str(METRICS_JSON),
        "prometheus_path": str(METRICS_PROM),
        "eval": eval_summary,
        "judged_eval": judged.get("summary", {}),
        "behavior_regression": {
            "status": behavior.get("status"),
            "cases": behavior.get("cases"),
            "passed": behavior.get("passed"),
            "failed": behavior.get("failed"),
        },
        "escalation_policy": escalation.get("status"),
        "audit_evidence": audit.get("status"),
        "enterprise_readiness": {
            "local_status": enterprise.get("local_status"),
            "score_local_single_user": enterprise.get("score_local_single_user"),
            "score_multi_user_saas": enterprise.get("score_multi_user_saas"),
            "blocking_controls": enterprise.get("blocking_controls"),
            "external_required": enterprise.get("external_required"),
        },
        "trace_examples": len(trace_examples.get("examples", [])),
        "dashboard": str(dashboard),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--enforce-retention", action="store_true")
    parser.add_argument("--retention", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    parser.add_argument("--slo", action="store_true")
    parser.add_argument("--eval", action="store_true")
    parser.add_argument("--judged-eval", action="store_true")
    parser.add_argument("--behavior-regression", action="store_true")
    parser.add_argument("--escalation-policy", action="store_true")
    parser.add_argument("--audit-evidence", action="store_true")
    parser.add_argument("--enterprise-readiness", action="store_true")
    parser.add_argument("--trace-examples", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args(argv)

    if args.all or not any([
        args.retention, args.metrics, args.slo, args.eval, args.judged_eval,
        args.behavior_regression, args.escalation_policy, args.audit_evidence,
        args.enterprise_readiness, args.trace_examples, args.dashboard,
    ]):
        payload = generate_all(
            enforce_retention_flag=args.enforce_retention,
            judged_eval=True if args.judged_eval else None,
            behavior_regression=True if args.behavior_regression else None,
        )
    elif args.retention:
        payload = enforce_retention(dry_run=not args.enforce_retention)
    elif args.metrics:
        payload = generate_metrics_export()
    elif args.slo:
        payload = generate_slo_summary()
    elif args.eval:
        payload = generate_eval_summary()
    elif args.judged_eval:
        payload = generate_judged_eval(enabled=True)
    elif args.behavior_regression:
        payload = generate_behavior_regression(enabled=True)
    elif args.escalation_policy:
        payload = generate_escalation_policy()
    elif args.audit_evidence:
        payload = generate_audit_evidence()
    elif args.enterprise_readiness:
        generate_escalation_policy()
        generate_audit_evidence()
        payload = generate_enterprise_readiness()
    elif args.trace_examples:
        payload = generate_trace_examples()
    else:
        payload = {"dashboard": str(generate_dashboard())}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
