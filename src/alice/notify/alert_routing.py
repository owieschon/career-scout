#!/usr/bin/env python3
"""Alert routing for Alice observability failures.

Sentry remains the primary issue sink. This module adds vendor-neutral routes
that work without a paid paging product: local audit file, Telegram direct
message, and generic JSON webhook targets such as Slack or Discord.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from alice import jobcfg
from alice import repo_paths

ROOT = repo_paths.ROOT
STATE_DIR = ROOT / "state"
ALERT_LOG = STATE_DIR / "observability" / "alerts.jsonl"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _cfg() -> dict[str, str]:
    cfg = jobcfg.load()
    for key, value in os.environ.items():
        if key.startswith("ALICE_ALERT_") or key in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}:
            cfg[key] = value
    return cfg


def configured_routes() -> list[str]:
    cfg = _cfg()
    routes = ["file"]
    if cfg.get("ALICE_ALERT_WEBHOOK_URL"):
        routes.append("webhook")
    if cfg.get("TELEGRAM_BOT_TOKEN") and (cfg.get("ALICE_ALERT_TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")):
        routes.append("telegram")
    return routes


def _write_file(payload: dict[str, Any]) -> dict[str, Any]:
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return {"route": "file", "ok": True, "path": _display_path(ALERT_LOG)}


def _post_json(url: str, payload: dict[str, Any], *, timeout: int = 10) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, default=str).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return {"status": resp.status}


def _send_webhook(cfg: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    url = cfg.get("ALICE_ALERT_WEBHOOK_URL")
    if not url:
        return {"route": "webhook", "ok": False, "detail": "ALICE_ALERT_WEBHOOK_URL missing"}
    try:
        response = _post_json(url, payload)
        return {"route": "webhook", "ok": response["status"] < 400, "status": response["status"]}
    except Exception as e:
        return {"route": "webhook", "ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}


def _send_telegram(cfg: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.get("ALICE_ALERT_TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"route": "telegram", "ok": False, "detail": "telegram token/chat missing"}
    text = payload.get("text") or payload.get("summary") or "Alice alert"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = _post_json(url, {"chat_id": chat_id, "text": text[:3900]})
        return {"route": "telegram", "ok": response["status"] < 400, "status": response["status"]}
    except Exception as e:
        return {"route": "telegram", "ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}


def route_alert(
    *,
    severity: str,
    title: str,
    summary: str,
    checks: list[dict[str, Any]] | None = None,
    run_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = _cfg()
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "service": "alice",
        "severity": severity,
        "title": title,
        "summary": summary,
        "text": f"{severity.upper()}: {title}\n{summary}",
        "run_key": run_key,
        "checks": checks or [],
        "dry_run": dry_run,
    }
    results = [_write_file(payload)]
    if dry_run:
        for route in configured_routes():
            if route != "file":
                results.append({"route": route, "ok": True, "dry_run": True})
        return {"ok": all(r.get("ok") for r in results), "routes": results}
    if cfg.get("ALICE_ALERT_WEBHOOK_URL"):
        results.append(_send_webhook(cfg, payload))
    if cfg.get("TELEGRAM_BOT_TOKEN") and (cfg.get("ALICE_ALERT_TELEGRAM_CHAT_ID") or cfg.get("TELEGRAM_CHAT_ID")):
        results.append(_send_telegram(cfg, payload))
    return {"ok": all(r.get("ok") for r in results), "routes": results}


def healthcheck() -> dict[str, Any]:
    routes = configured_routes()
    return {
        "ok": "file" in routes,
        "routes": routes,
        "file_path": _display_path(ALERT_LOG),
        "external_route_configured": any(r in routes for r in ("webhook", "telegram")),
    }


def main() -> int:
    result = route_alert(
        severity="info",
        title="alice.alert_route_canary",
        summary="Alert routing canary.",
        dry_run=os.environ.get("ALICE_ALERT_DRY_RUN", "1") != "0",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
