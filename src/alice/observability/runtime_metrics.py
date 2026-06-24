"""Local infrastructure/runtime metrics for Alice.

This is the repo-local Layer 1 surface: enough process, disk, and launchd
state to operate Alice as a service on this machine, without pretending a
full Prometheus/Datadog backend exists.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from alice import repo_paths

REPO_ROOT = repo_paths.ROOT   # portable, not hardcoded
STARTUP_FILE = REPO_ROOT / "state" / "deploy-guard-startup.json"

DEFAULT_THRESHOLDS = {
    "telegram_max_rss_mb": 750,
    "telegram_max_cpu_pct": 80.0,
    "telegram_max_runs": 25,
    "repo_min_disk_free_gb": 2.0,
    "telegram_min_uptime_seconds": 20,
}


def _run(args: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _parse_launchctl_print(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {"raw_available": bool(output)}
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("state ="):
            parsed["state"] = s.split("=", 1)[1].strip()
        elif s.startswith("pid ="):
            try:
                parsed["pid"] = int(s.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif s.startswith("runs ="):
            try:
                parsed["runs"] = int(s.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif "ALICE_TRACING => 1" in s or '"ALICE_TRACING" => "1"' in s:
            parsed["alice_tracing"] = True
    parsed.setdefault("alice_tracing", False)
    return parsed


def launchd_service(label: str) -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{label}"
    code, out, err = _run(["launchctl", "print", target], timeout=10)
    rec = {
        "label": label,
        "ok": code == 0,
        "error": err.strip()[:200] if code != 0 else "",
    }
    if code == 0:
        rec.update(_parse_launchctl_print(out))
    return rec


def _ps_metrics(pid: int | None) -> dict[str, Any]:
    if not pid:
        return {}
    code, out, err = _run(["ps", "-o", "rss=,%cpu=,etime=", "-p", str(pid)], timeout=5)
    if code != 0 or not out.strip():
        return {"ps_error": err.strip()[:160] or "ps returned no rows"}
    parts = out.strip().split(None, 2)
    metrics: dict[str, Any] = {}
    try:
        metrics["rss_mb"] = round(int(parts[0]) / 1024, 1)
    except Exception:
        pass
    try:
        metrics["cpu_pct"] = float(parts[1])
    except Exception:
        pass
    if len(parts) >= 3:
        metrics["etime"] = parts[2]
    return metrics


def _startup_snapshot() -> dict[str, Any]:
    try:
        return json.loads(STARTUP_FILE.read_text())
    except Exception:
        return {}


def _uptime_seconds(snapshot: dict[str, Any]) -> float | None:
    loaded_at = snapshot.get("loaded_at")
    if not loaded_at:
        return None
    try:
        started = datetime.fromisoformat(loaded_at)
        return max(0.0, (datetime.now() - started).total_seconds())
    except Exception:
        return None


def disk_metrics(path: Path = REPO_ROOT) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "total_gb": round(usage.total / (1024 ** 3), 2),
        "used_gb": round(usage.used / (1024 ** 3), 2),
        "free_gb": round(usage.free / (1024 ** 3), 2),
        "free_pct": round((usage.free / usage.total) * 100, 2) if usage.total else 0.0,
    }


def collect() -> dict[str, Any]:
    telegram = launchd_service("com.operator.jobsearch.telegram")
    healthcheck = launchd_service("com.operator.jobsearch.observability-healthcheck")
    artifacts = launchd_service("com.operator.jobsearch.observability-artifacts")
    metrics_exporter = launchd_service("com.operator.jobsearch.metrics-exporter")
    snapshot = _startup_snapshot()
    telegram.update(_ps_metrics(telegram.get("pid")))
    uptime = _uptime_seconds(snapshot)
    if uptime is not None:
        telegram["uptime_seconds"] = round(uptime, 1)
    if snapshot:
        telegram["loaded_commit"] = (snapshot.get("loaded_commit") or "")[:12]
        telegram["loaded_at"] = snapshot.get("loaded_at")
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "services": {
            "telegram": telegram,
            "observability_healthcheck": healthcheck,
            "observability_artifacts": artifacts,
            "metrics_exporter": metrics_exporter,
        },
        "disk": disk_metrics(),
        "thresholds": dict(DEFAULT_THRESHOLDS),
    }


def evaluate(metrics: dict[str, Any], thresholds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    telegram = metrics.get("services", {}).get("telegram", {})
    healthcheck = metrics.get("services", {}).get("observability_healthcheck", {})
    artifacts = metrics.get("services", {}).get("observability_artifacts", {})
    metrics_exporter = metrics.get("services", {}).get("metrics_exporter", {})
    disk = metrics.get("disk", {})
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, **data: Any) -> None:
        checks.append({
            "name": name,
            "status": "pass" if ok else "fail",
            "detail": detail,
            "data": {k: v for k, v in data.items() if v is not None},
        })

    add(
        "infra.telegram_running",
        telegram.get("ok") and telegram.get("state") == "running" and bool(telegram.get("pid")),
        "telegram launchd service running",
        pid=telegram.get("pid"),
        state=telegram.get("state"),
    )
    add(
        "infra.telegram_tracing_env",
        bool(telegram.get("alice_tracing")),
        "telegram launchd service has ALICE_TRACING=1",
    )
    add(
        "infra.telegram_restart_count",
        int(telegram.get("runs") or 0) <= int(t["telegram_max_runs"]),
        "launchd run count within threshold",
        runs=telegram.get("runs"),
        max_runs=t["telegram_max_runs"],
    )
    add(
        "infra.telegram_memory",
        float(telegram.get("rss_mb") or 0.0) <= float(t["telegram_max_rss_mb"]),
        "RSS memory within threshold",
        rss_mb=telegram.get("rss_mb"),
        max_rss_mb=t["telegram_max_rss_mb"],
    )
    add(
        "infra.telegram_cpu",
        float(telegram.get("cpu_pct") or 0.0) <= float(t["telegram_max_cpu_pct"]),
        "CPU sample within threshold",
        cpu_pct=telegram.get("cpu_pct"),
        max_cpu_pct=t["telegram_max_cpu_pct"],
    )
    uptime = telegram.get("uptime_seconds")
    add(
        "infra.telegram_uptime",
        uptime is None or float(uptime) >= float(t["telegram_min_uptime_seconds"]),
        "daemon uptime beyond immediate restart window",
        uptime_seconds=uptime,
        min_uptime_seconds=t["telegram_min_uptime_seconds"],
    )
    add(
        "infra.healthcheck_scheduler",
        healthcheck.get("ok"),
        "observability healthcheck launchd service is installed/queryable",
        state=healthcheck.get("state"),
    )
    add(
        "infra.artifact_scheduler",
        artifacts.get("ok"),
        "observability artifact launchd service is installed/queryable",
        state=artifacts.get("state"),
    )
    add(
        "infra.metrics_exporter",
        metrics_exporter.get("ok") and metrics_exporter.get("state") == "running",
        "prometheus metrics exporter launchd service is running",
        state=metrics_exporter.get("state"),
        pid=metrics_exporter.get("pid"),
    )
    add(
        "infra.repo_disk_free",
        float(disk.get("free_gb") or 0.0) >= float(t["repo_min_disk_free_gb"]),
        "repo volume has disk headroom",
        free_gb=disk.get("free_gb"),
        min_free_gb=t["repo_min_disk_free_gb"],
    )
    return checks


def summary() -> dict[str, Any]:
    metrics = collect()
    return {"metrics": metrics, "checks": evaluate(metrics)}
