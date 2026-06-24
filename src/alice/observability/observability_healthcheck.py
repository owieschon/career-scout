#!/usr/bin/env python3
"""Live observability gate for Alice.

Checks the surfaces the operator expects to be live:
  - daemon deploy guard is not stale
  - Sentry SDK + DSN can emit a canary event
  - Phoenix HTTP collector is up and stores a canary span
  - LangSmith receives the same canary span via OTLP dual-export
  - the LLM chokepoint logs the canary call and records its prediction span

No secret values are printed. Run from the repo root.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from alice import repo_paths

ROOT = repo_paths.ROOT
SCRIPTS = ROOT / "scripts"

TIME_COST_LOG = ROOT / "feedback" / "time-cost-log.jsonl"
PREDICTION_SPANS = ROOT / "feedback" / "prediction-spans.jsonl"
PHOENIX_URL = os.environ.get("ALICE_PHOENIX_ENDPOINT", "http://localhost:6006").rstrip("/")
PROJECT = os.environ.get("ALICE_TRACE_PROJECT", "alice")
METRICS_URL = os.environ.get("ALICE_METRICS_URL", "http://127.0.0.1:9108/metrics")


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _check(name: str, status: str, detail: str = "", **data: Any) -> Check:
    return Check(name=name, status=status, detail=detail, data={k: v for k, v in data.items() if v is not None})


def langsmith_run_id_for_span(span_id: str) -> str:
    span_bytes = bytes.fromhex(span_id.zfill(16))
    return str(uuid.UUID(bytes=b"\x00" * (16 - len(span_bytes)) + span_bytes))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return dt.astimezone()
    except Exception:
        return None


def check_config() -> list[Check]:
    from alice import jobcfg
    from alice.observability import obs

    cfg = jobcfg.load()
    return [
        _check("config.sentry_dsn", "pass" if bool(cfg.get("SENTRY_DSN") or os.environ.get("SENTRY_DSN")) else "fail"),
        _check("config.langsmith_api_key", "pass" if bool(cfg.get("LANGSMITH_API_KEY")) else "fail"),
        _check("config.langsmith_project", "pass" if bool(cfg.get("LANGSMITH_PROJECT", "alice")) else "fail",
               project=cfg.get("LANGSMITH_PROJECT", "alice")),
        _check("config.sentry_sdk", "pass" if obs.available() else "fail"),
    ]


def check_posthog() -> Check:
    try:
        from alice.observability import product_analytics
        status = product_analytics.status()
        data = {k: v for k, v in status.items() if k != "detail"}
        if not status["enabled_flag"]:
            return _check("posthog.product_analytics", "warn", "ALICE_POSTHOG not enabled", **data)
        if not status["api_key_configured"]:
            return _check("posthog.product_analytics", "warn", "POSTHOG_API_KEY missing", **data)
        if not status["sdk_importable"]:
            return _check("posthog.product_analytics", "fail", "posthog SDK not importable", **data)
        ok = product_analytics.init("observability_healthcheck")
        emitted = product_analytics.capture(
            "alice_observability_healthcheck_completed",
            {"surface": "healthcheck", "ok": True},
        )
        product_analytics.flush()
        status = product_analytics.status()
        data = {k: v for k, v in status.items() if k != "detail"}
        return _check(
            "posthog.product_analytics",
            "pass" if ok and emitted else "fail",
            "canary captured" if ok and emitted else "capture returned false",
            **data,
        )
    except Exception as e:
        return _check("posthog.product_analytics", "fail", f"{type(e).__name__}: {e}")


def check_posthog_readback() -> Check:
    try:
        from alice.observability import product_analytics

        status = product_analytics.status()
        if not status.get("personal_api_key_configured") or not status.get("project_id_configured"):
            return _check(
                "posthog.remote_readback",
                "warn",
                "POSTHOG_PERSONAL_API_KEY and POSTHOG_PROJECT_ID required for API readback",
                personal_api_key_configured=status.get("personal_api_key_configured"),
                project_id_configured=status.get("project_id_configured"),
            )
        result = {"ok": True, "events": []}
        for _ in range(20):
            result = product_analytics.query_events("alice_observability_healthcheck_completed", limit=10)
            if not result.get("ok"):
                return _check("posthog.remote_readback", "fail", result.get("error", "query failed"))
            if result.get("events"):
                break
            time.sleep(3)
        found = bool(result.get("events"))
        return _check(
            "posthog.remote_readback",
            "pass" if found else "fail",
            "healthcheck event found via PostHog API" if found else "no healthcheck events returned via PostHog API",
            events_seen=len(result.get("events") or []),
        )
    except Exception as e:
        return _check("posthog.remote_readback", "fail", f"{type(e).__name__}: {e}")


def check_deploy_guard() -> Check:
    try:
        from alice.ops import deploy_guard

        status = deploy_guard.check_for_stale_code()
        return _check(
            "daemon.deploy_guard",
            "fail" if status.get("stale") else "pass",
            status.get("reason", ""),
            loaded_commit=(status.get("loaded_commit") or "")[:12],
            current_head=(status.get("current_head") or "")[:12],
            loaded_at=status.get("loaded_at"),
        )
    except Exception as e:
        return _check("daemon.deploy_guard", "fail", f"{type(e).__name__}: {e}")


def check_launchd_tracing() -> Check:
    label = "com.operator.jobsearch.telegram"
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        return _check("daemon.launchd_tracing", "warn", f"{type(e).__name__}: {e}")
    if proc.returncode != 0:
        return _check("daemon.launchd_tracing", "warn", "launchctl print failed")
    out = proc.stdout
    has_tracing = "ALICE_TRACING => 1" in out or '"ALICE_TRACING" => "1"' in out
    running = "state = running" in out
    pid = None
    for line in out.splitlines():
        if line.strip().startswith("pid ="):
            pid = line.split("=", 1)[1].strip()
            break
    return _check(
        "daemon.launchd_tracing",
        "pass" if has_tracing and running else "fail",
        "launchd service running with ALICE_TRACING=1" if has_tracing and running else "launchd service missing running/tracing signal",
        pid=pid,
    )


def check_infra_runtime() -> list[Check]:
    try:
        from alice.observability import runtime_metrics

        report = runtime_metrics.summary()
        checks = []
        for rec in report["checks"]:
            checks.append(_check(
                rec["name"],
                rec["status"],
                rec.get("detail", ""),
                **rec.get("data", {}),
            ))
        return checks
    except Exception as e:
        return [_check("infra.runtime_metrics", "fail", f"{type(e).__name__}: {e}")]


def check_observability_artifacts() -> list[Check]:
    try:
        from alice.observability import observability_artifacts
        from alice import jobcfg

        max_age_hours = 24
        now = _now_local()
        cfg = jobcfg.load()
        judged_eval_enabled = str(cfg.get("ALICE_JUDGED_EVAL", os.environ.get("ALICE_JUDGED_EVAL", ""))).lower() in {"1", "true", "yes"}
        behavior_regression_enabled = str(cfg.get("ALICE_BEHAVIOR_REGRESSION", os.environ.get("ALICE_BEHAVIOR_REGRESSION", ""))).lower() in {"1", "true", "yes"}
        paths = {
            "artifacts.metrics_export": observability_artifacts.METRICS_JSON,
            "artifacts.prometheus_export": observability_artifacts.METRICS_PROM,
            "artifacts.slo_summary": observability_artifacts.SLO_JSON,
            "artifacts.eval_summary": observability_artifacts.EVAL_JSON,
            "artifacts.retention_report": observability_artifacts.RETENTION_JSON,
            "artifacts.escalation_policy": observability_artifacts.ESCALATION_POLICY_JSON,
            "artifacts.audit_evidence": observability_artifacts.AUDIT_EVIDENCE_JSON,
            "artifacts.enterprise_readiness": observability_artifacts.ENTERPRISE_READINESS_JSON,
            "artifacts.dashboard": observability_artifacts.DASHBOARD_HTML,
            "artifacts.trace_examples": observability_artifacts.TRACE_MD,
            "artifacts.prometheus_config": observability_artifacts.PROMETHEUS_CONFIG,
            "artifacts.prometheus_alert_rules": observability_artifacts.PROMETHEUS_RULES,
        }
        if judged_eval_enabled:
            paths["artifacts.judged_eval"] = observability_artifacts.JUDGED_EVAL_JSON
        if behavior_regression_enabled:
            paths["artifacts.behavior_regression"] = observability_artifacts.BEHAVIOR_REGRESSION_JSON
        checks = []
        for name, path in paths.items():
            if not path.exists():
                checks.append(_check(name, "fail", "artifact missing", path=str(path.relative_to(ROOT))))
                continue
            age_hours = (now - datetime.fromtimestamp(path.stat().st_mtime).astimezone()).total_seconds() / 3600
            status = "pass" if age_hours <= max_age_hours else "fail"
            detail = "artifact fresh" if age_hours <= max_age_hours else "artifact stale"
            if name == "artifacts.behavior_regression":
                try:
                    payload = json.loads(path.read_text())
                    if payload.get("status") != "pass":
                        status = "fail"
                        detail = f"behavior regression status={payload.get('status')}"
                except Exception as e:
                    status = "fail"
                    detail = f"could not parse behavior regression: {type(e).__name__}"
            if name == "artifacts.enterprise_readiness":
                try:
                    payload = json.loads(path.read_text())
                    if payload.get("local_status") != "pass":
                        status = "fail"
                        detail = f"enterprise readiness local_status={payload.get('local_status')}"
                except Exception as e:
                    status = "fail"
                    detail = f"could not parse enterprise readiness: {type(e).__name__}"
            if name == "artifacts.audit_evidence":
                try:
                    payload = json.loads(path.read_text())
                    if payload.get("status") != "pass":
                        status = "fail"
                        detail = f"audit evidence status={payload.get('status')}"
                except Exception as e:
                    status = "fail"
                    detail = f"could not parse audit evidence: {type(e).__name__}"
            checks.append(_check(
                name,
                status,
                detail,
                path=str(path.relative_to(ROOT)),
                age_hours=round(age_hours, 2),
                max_age_hours=max_age_hours,
            ))
        return checks
    except Exception as e:
        return [_check("artifacts.observability", "fail", f"{type(e).__name__}: {e}")]


def check_metrics_exporter() -> Check:
    try:
        with urlopen(METRICS_URL, timeout=5) as resp:
            text = resp.read().decode(errors="replace")
            ok = resp.status < 500 and "alice_healthcheck_failed_checks" in text
            return _check(
                "metrics.prometheus_http_exporter",
                "pass" if ok else "fail",
                f"HTTP {resp.status}",
                url=METRICS_URL,
                bytes=len(text),
            )
    except Exception as e:
        return _check("metrics.prometheus_http_exporter", "fail", f"{type(e).__name__}: {e}", url=METRICS_URL)


def check_alert_routing() -> Check:
    try:
        from alice.notify import alert_routing

        health = alert_routing.healthcheck()
        result = alert_routing.route_alert(
            severity="info",
            title="alice.alert_route_healthcheck",
            summary="Dry-run alert route healthcheck.",
            dry_run=True,
        )
        external = health.get("external_route_configured")
        status = "pass" if result.get("ok") and external else "warn" if result.get("ok") else "fail"
        detail = "external alert route configured" if external else "file audit route only; configure ALICE_ALERT_WEBHOOK_URL or ALICE_ALERT_TELEGRAM_CHAT_ID for external routing"
        return _check("alerting.routes", status, detail, routes=health.get("routes"), dry_run_ok=result.get("ok"))
    except Exception as e:
        return _check("alerting.routes", "fail", f"{type(e).__name__}: {e}")


def check_phoenix_up() -> Check:
    try:
        with urlopen(PHOENIX_URL, timeout=5) as resp:
            return _check("phoenix.http", "pass" if resp.status < 500 else "fail", f"HTTP {resp.status}", url=PHOENIX_URL)
    except Exception as e:
        return _check("phoenix.http", "fail", f"{type(e).__name__}: {e}", url=PHOENIX_URL)


def check_sentry(run_key: str) -> Check:
    from alice.observability import obs

    if not obs.available():
        return _check("sentry.canary", "fail", "Sentry SDK or DSN unavailable")
    try:
        obs.init("observability_healthcheck")
        ok = obs.capture_message(
            f"alice.observability.canary {run_key}",
            level="info",
            extras={"run_key": run_key, "surface": "healthcheck"},
            where="observability_healthcheck",
        )
        event_id = None
        try:
            import sentry_sdk

            sentry_sdk.flush(timeout=10)
            event_id = sentry_sdk.last_event_id()
        except Exception:
            pass
        return _check("sentry.canary", "pass" if ok else "fail", "canary queued and flushed", event_id=event_id)
    except Exception as e:
        return _check("sentry.canary", "fail", f"{type(e).__name__}: {e}")


def check_sentry_readback(run_key: str) -> Check:
    try:
        from alice import jobcfg
        from alice.observability import sentry_query

        cfg = jobcfg.load()
        token = cfg.get("SENTRY_AUTH_TOKEN") or cfg.get("SENTRY_API_TOKEN")
        if not token:
            return _check(
                "sentry.remote_readback",
                "warn",
                "SENTRY_AUTH_TOKEN unavailable; can emit but cannot query remote events",
                dsn_project_id=sentry_query.dsn_project_id(cfg.get("SENTRY_DSN", "")),
            )
        org, project, project_rec = sentry_query.discover_project(token, cfg)
        query = "alice.observability.canary"
        last_count = 0
        for _ in range(20):
            events = sentry_query.query_events(
                token,
                org,
                project,
                query=query,
                since_minutes=15,
                limit=20,
            )
            last_count = len(events)
            matching = [
                event for event in events
                if run_key in (event.get("title") or "")
            ]
            if matching:
                return _check(
                    "sentry.remote_readback",
                    "pass",
                    "canary found via Sentry API",
                    org=org,
                    project=project,
                    project_id=project_rec.get("id"),
                    event_id=matching[0].get("eventID"),
                )
            time.sleep(3)
        return _check(
            "sentry.remote_readback",
            "fail",
            "canary was emitted but not found via Sentry API",
            org=org,
            project=project,
            events_seen=last_count,
        )
    except Exception as e:
        return _check("sentry.remote_readback", "fail", f"{type(e).__name__}: {e}")


def _latest_prediction_for(job_key: str, since: datetime) -> dict[str, Any] | None:
    found = None
    for rec in _load_jsonl(PREDICTION_SPANS):
        if rec.get("job_key") != job_key:
            continue
        rec_ts = _parse_ts(rec.get("ts", ""))
        if rec_ts and rec_ts < since:
            continue
        found = rec
    return found


def _latest_cost_for(job_key: str, since: datetime) -> dict[str, Any] | None:
    found = None
    for rec in _load_jsonl(TIME_COST_LOG):
        if rec.get("job_key") != job_key:
            continue
        rec_ts = _parse_ts(rec.get("ts", ""))
        if rec_ts and rec_ts < since:
            continue
        found = rec
    return found


def _span_id_from_phoenix_span(span: Any) -> str | None:
    for attr in ("span_id", "context", "span_context"):
        val = getattr(span, attr, None)
        if isinstance(val, str):
            return val
        nested = getattr(val, "span_id", None)
        if isinstance(nested, str):
            return nested
    if isinstance(span, dict):
        for key in ("span_id", "spanId"):
            if isinstance(span.get(key), str):
                return span[key]
        ctx = span.get("context") or span.get("span_context") or {}
        if isinstance(ctx, dict):
            return ctx.get("span_id") or ctx.get("spanId")
    return None


def query_phoenix_span(span_id: str, started_at: datetime) -> Check:
    try:
        from phoenix.client import Client

        client = Client(base_url=PHOENIX_URL)
        spans = client.spans.get_spans(
            project_identifier=PROJECT,
            start_time=started_at - timedelta(seconds=5),
            limit=200,
            timeout=10,
        )
        for span in spans:
            phoenix_span_id = _span_id_from_phoenix_span(span)
            if phoenix_span_id and phoenix_span_id.zfill(len(span_id)) == span_id:
                return _check(
                    "phoenix.span_delivery",
                    "pass",
                    "span found in Phoenix",
                    span_id=span_id,
                    phoenix_span_id=phoenix_span_id,
                )
        return _check("phoenix.span_delivery", "fail", "span not found in Phoenix", span_id=span_id, spans_seen=len(spans))
    except Exception as e:
        return _check("phoenix.span_delivery", "fail", f"{type(e).__name__}: {e}", span_id=span_id)


def query_langsmith_span(span_id: str) -> Check:
    try:
        from alice import jobcfg
        from langsmith import Client

        key = jobcfg.load().get("LANGSMITH_API_KEY")
        if not key:
            return _check("langsmith.span_delivery", "fail", "LANGSMITH_API_KEY unavailable")
        run_id = langsmith_run_id_for_span(span_id)
        client = Client(api_key=key)
        last_err = ""
        for _ in range(8):
            try:
                run = client.read_run(run_id)
                return _check("langsmith.span_delivery", "pass", "run found in LangSmith", run_id=str(run.id))
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(2)
        return _check("langsmith.span_delivery", "fail", last_err, run_id=run_id)
    except Exception as e:
        return _check("langsmith.span_delivery", "fail", f"{type(e).__name__}: {e}")


def failing_checks(checks: list[Check]) -> list[Check]:
    return [check for check in checks if check.status == "fail"]


def alert_failures(run_key: str, checks: list[Check]) -> bool:
    failures = failing_checks(checks)
    if not failures:
        return False
    try:
        from alice.observability import obs

        if not obs.available():
            return False
        obs.init("observability_healthcheck")
        ok = obs.capture_message(
            "alice.observability.healthcheck_failed",
            level="error",
            where="observability_healthcheck",
            extras={
                "run_key": run_key,
                "failures": [
                    {
                        "name": check.name,
                        "status": check.status,
                        "detail": check.detail[:300],
                    }
                    for check in failures
                ],
            },
        )
        try:
            import sentry_sdk

            sentry_sdk.flush(timeout=10)
        except Exception:
            pass
        try:
            from alice.notify import alert_routing
            alert_routing.route_alert(
                severity="critical",
                title="alice.observability.healthcheck_failed",
                summary=", ".join(check.name for check in failures[:10]),
                run_key=run_key,
                checks=[asdict(check) for check in failures],
            )
        except Exception:
            pass
        return bool(ok)
    except Exception:
        return False


def run_llm_canary(run_key: str, started_at: datetime, skip_llm: bool) -> tuple[list[Check], str | None]:
    if skip_llm:
        return [_check("llm.canary", "warn", "skipped by --skip-llm")], None

    os.environ["ALICE_TRACING"] = "1"
    os.environ.setdefault("ALICE_TRACE_PROJECT", PROJECT)
    from alice.observability import telemetry

    telemetry.init_tracing()
    checks = [_check("telemetry.bootstrap", "pass" if telemetry.is_on() else "fail")]
    if not telemetry.is_on():
        return checks, None

    job_key = f"observability-healthcheck-{run_key}"
    try:
        from alice.llm import llm

        result = llm.call(
            "observability_healthcheck",
            "Reply with exactly: OK",
            max_tokens=16,
            temperature=0.0,
            session_id="observability-healthcheck",
            job_key=job_key,
        )
        text = (result.get("text") or "").strip()
        checks.append(_check("llm.canary", "pass" if "OK" in text else "fail", f"model returned {text[:40]!r}"))
    except Exception as e:
        telemetry.shutdown_tracing(10_000)
        checks.append(_check("llm.canary", "fail", f"{type(e).__name__}: {e}"))
        return checks, None

    telemetry.shutdown_tracing(30_000)

    cost_rec = _latest_cost_for(job_key, started_at)
    checks.append(_check(
        "local.cost_log",
        "pass" if cost_rec and cost_rec.get("ok") else "fail",
        "cost log contains canary" if cost_rec else "cost log missing canary",
        task=(cost_rec or {}).get("task"),
        model=(cost_rec or {}).get("model"),
        cost_usd=(cost_rec or {}).get("cost_usd"),
    ))
    pred_rec = _latest_prediction_for(job_key, started_at)
    span_id = (pred_rec or {}).get("span_id")
    checks.append(_check(
        "local.prediction_span",
        "pass" if span_id else "fail",
        "prediction span recorded" if span_id else "prediction span missing",
        span_id=span_id,
        job_key=job_key,
    ))
    return checks, span_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-llm", action="store_true", help="Skip the paid/network LLM canary.")
    args = parser.parse_args(argv)

    started_at = _now_local()
    run_key = started_at.strftime("%Y%m%dT%H%M%S")
    checks: list[Check] = []
    checks.extend(check_config())
    checks.append(check_deploy_guard())
    checks.append(check_launchd_tracing())
    checks.extend(check_infra_runtime())
    checks.extend(check_observability_artifacts())
    checks.append(check_metrics_exporter())
    checks.append(check_alert_routing())
    checks.append(check_phoenix_up())
    checks.append(check_sentry(run_key))
    llm_checks, span_id = run_llm_canary(run_key, started_at, args.skip_llm)
    checks.extend(llm_checks)
    if span_id:
        checks.append(query_phoenix_span(span_id, started_at))
        checks.append(query_langsmith_span(span_id))
    checks.append(check_sentry_readback(run_key))
    checks.append(check_posthog())
    checks.append(check_posthog_readback())

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "run_key": run_key,
        "checks": [asdict(c) for c in checks],
    }
    try:
        from alice.observability import observability_artifacts
        observability_artifacts.persist_healthcheck_report(report)
    except Exception:
        pass
    alert_sent = alert_failures(run_key, checks)
    if alert_sent:
        report["failure_alert"] = "sent"
    try:
        from alice.observability import observability_artifacts
        observability_artifacts.persist_healthcheck_report(report)
        observability_artifacts.create_incident_report(report)
    except Exception:
        pass
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
