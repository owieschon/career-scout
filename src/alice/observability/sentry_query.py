#!/usr/bin/env python3
"""Query remote Sentry for Alice events without printing secrets.

Requires SENTRY_AUTH_TOKEN in ~/.config/job-search/config.env or the process
environment. SENTRY_ORG and SENTRY_PROJECT are optional: when absent, this
script lists accessible orgs/projects and selects the project whose numeric ID
matches the SENTRY_DSN project id.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from alice import repo_paths

ROOT = repo_paths.ROOT
SCRIPTS = ROOT / "scripts"

from alice import jobcfg

API_BASE = "https://sentry.io/api/0"


def _cfg() -> dict[str, str]:
    return jobcfg.load()


def dsn_project_id(dsn: str) -> str:
    parsed = urllib.parse.urlparse(dsn or "")
    if not parsed.path:
        return ""
    return parsed.path.strip("/").split("/")[-1]


def _redact_url(url: str) -> str:
    return urllib.parse.urlsplit(url)._replace(query="[REDACTED]").geturl() if "?" in url else url


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def api_get(path: str, *, token: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Sentry API HTTP {e.code} for {_redact_url(url)}: {body}") from e


def discover_project(token: str, cfg: dict[str, str]) -> tuple[str, str, dict[str, Any]]:
    org_slug = cfg.get("SENTRY_ORG") or cfg.get("SENTRY_ORG_SLUG")
    project_slug = cfg.get("SENTRY_PROJECT") or cfg.get("SENTRY_PROJECT_SLUG")
    if org_slug and project_slug:
        project = api_get(f"/projects/{org_slug}/{project_slug}/", token=token)
        return org_slug, project_slug, project

    want_project_id = cfg.get("SENTRY_PROJECT_ID") or dsn_project_id(cfg.get("SENTRY_DSN", ""))
    if not want_project_id:
        raise RuntimeError("No SENTRY_PROJECT_ID and no project id could be inferred from SENTRY_DSN.")

    orgs = api_get("/organizations/", token=token)
    for org in orgs:
        slug = org.get("slug")
        if not slug:
            continue
        projects = api_get(f"/organizations/{slug}/projects/", token=token, params={"per_page": 100})
        for project in projects:
            if str(project.get("id")) == str(want_project_id):
                return slug, project.get("slug"), project
    raise RuntimeError(f"No accessible Sentry project matched project id {want_project_id}.")


def summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "eventID": event.get("eventID") or event.get("id"),
        "title": event.get("title") or event.get("message"),
        "culprit": event.get("culprit"),
        "level": event.get("level"),
        "dateCreated": event.get("dateCreated") or event.get("dateReceived"),
        "tags": {
            tag.get("key"): tag.get("value")
            for tag in event.get("tags", [])
            if tag.get("key") in {"component", "where", "environment", "release"}
        },
        "metadata": {
            k: event.get("metadata", {}).get(k)
            for k in ("type", "value", "filename", "function")
            if event.get("metadata", {}).get(k)
        },
    }


def query_events(token: str, org: str, project: str, *, query: str, since_minutes: int, limit: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=since_minutes)
    events = api_get(
        f"/projects/{org}/{project}/events/",
        token=token,
        params={
            "query": query,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "per_page": min(max(limit, 1), 100),
        },
    )
    return [summarize_event(event) for event in events[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default='message:"alice.security" OR message:"alice.observability"')
    parser.add_argument("--since-minutes", type=int, default=180)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args(argv)

    cfg = _cfg()
    token = cfg.get("SENTRY_AUTH_TOKEN") or cfg.get("SENTRY_API_TOKEN")
    if not token:
        print(json.dumps({
            "ok": False,
            "missing": ["SENTRY_AUTH_TOKEN"],
            "has_dsn": bool(cfg.get("SENTRY_DSN")),
            "dsn_project_id": dsn_project_id(cfg.get("SENTRY_DSN", "")),
        }, indent=2, sort_keys=True))
        return 2

    org, project, project_rec = discover_project(token, cfg)
    if args.discover_only:
        print(json.dumps({
            "ok": True,
            "org": org,
            "project": project,
            "project_id": project_rec.get("id"),
            "project_name": project_rec.get("name"),
        }, indent=2, sort_keys=True))
        return 0

    events = query_events(
        token,
        org,
        project,
        query=args.query,
        since_minutes=args.since_minutes,
        limit=args.limit,
    )
    print(json.dumps({
        "ok": True,
        "org": org,
        "project": project,
        "query": args.query,
        "events": events,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
