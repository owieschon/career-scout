# Alice Production Observability Standard

<!-- clean-docs:purpose -->
Status: production-grade local observability baseline, updated 2026-06-03. Read this page before changing or relying on Alice Production Observability Standard so you can preserve its documented constraints and verify the result against the repository.
<!-- clean-docs:end purpose -->
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->
<!-- clean-docs:allow doc-length reason="This canonical contract stays in one file so its definitions, constraints, and verification criteria remain reviewable together" -->


Alice is a single-user local daemon, but the observability target is production
multi-user SaaS discipline: deterministic health gates, explicit SLOs,
remote-readback where vendors support it, infrastructure/runtime telemetry,
privacy controls, and written runbooks.

## What Is Live

- Sentry emits canaries and failure alerts, and the healthcheck verifies remote
  readback through the Sentry API.
- Phoenix receives the LLM canary span through OpenTelemetry.
- LangSmith receives the same canary span through the OTLP dual-export path.
- Local cost and prediction-span logs are verified every healthcheck.
- PostHog captures sanitized product analytics events server-side.
- `runtime_metrics.py` checks launchd state, PID, restart count, RSS memory,
  CPU sample, daemon uptime, scheduler presence, and disk headroom.
- `observability_artifacts.py` generates retention reports, SLO summaries,
  Prometheus-format metrics, eval summaries, judged eval runs, real-path
  behavior regressions, incident reports, trace examples, and a local dashboard.
- `metrics_http_server.py` serves Prometheus metrics at
  `http://127.0.0.1:9108/metrics`.
- `monitoring/prometheus.yml` and `monitoring/alert_rules.yml` provide a
  scrape-ready Prometheus config and alert rules.
- `alert_routing.py` routes failures to a local audit log and optional Telegram
  or generic webhook endpoints.
- `observability_healthcheck.py` is the gate. Failures emit
  `alice.observability.healthcheck_failed` to Sentry.

## SLOs

The policy source is `config/observability_production_policy.json`.

- Telegram daemon running: 99.5% measured by the 6-hour healthcheck.
- Healthcheck success: >= 99% over trailing 7 days.
- LLM canary delivery: p95 < 60 seconds including trace export.
- Critical error detection: Sentry event emitted and remotely readable within
  15 minutes.
- Cost anomaly detection: single-call anomaly logged and Sentry event emitted
  in the same turn.

## Alerts

Failing healthcheck items are page-worthy for this local service. Sentry is the
primary issue sink. `alert_routing.py` adds:

- `state/observability/alerts.jsonl`: local audit route, always available.
- `ALICE_ALERT_WEBHOOK_URL`: generic JSON webhook route.
- `ALICE_ALERT_TELEGRAM_CHAT_ID` plus `TELEGRAM_BOT_TOKEN`: Telegram alert route.

If only the file route is configured, `alerting.routes` warns but does not fail
the health gate. A real external route is recommended for unattended operation.

Prometheus alert rules live in `monitoring/alert_rules.yml`.

## Runbook
<!-- clean-docs:allow section-length reason="This section keeps one tightly coupled procedure or contract together so readers can verify it without crossing section boundaries" -->

1. Run:

   ```bash
   PYTHONPATH=scripts python3 scripts/observability_healthcheck.py
   ```

2. If `daemon.deploy_guard` fails, restart Alice:

   ```bash
   launchctl kickstart -k gui/$(id -u)/com.jordan.jobsearch.telegram
   ```

3. If Phoenix fails, restart the Phoenix LaunchAgent or run:

   ```bash
   scripts/phoenix_capture.sh start
   ```

4. If Sentry remote readback fails but emit passes, wait 60 seconds and rerun.
   If it still fails, check `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and
   `SENTRY_PROJECT`.

5. If PostHog capture fails, check `ALICE_POSTHOG`, `POSTHOG_API_KEY`, and
   `POSTHOG_HOST`.

6. If infra checks fail, inspect `query_runtime_metrics` through Alice or run:

   ```bash
   PYTHONPATH=scripts python3 -c 'import runtime_metrics, json; print(json.dumps(runtime_metrics.summary(), indent=2))'
   ```

7. Refresh all interview/production observability artifacts:

   ```bash
   PYTHONPATH=scripts python3 scripts/observability_artifacts.py --all --enforce-retention
   ```

8. Run the paid judged eval suite explicitly:

   ```bash
   PYTHONPATH=scripts python3 scripts/observability_artifacts.py --judged-eval
   ```

9. Run the real Alice behavior regression explicitly:

   ```bash
   PYTHONPATH=scripts python3 scripts/alice_behavior_regression.py
   ```

10. Check the Prometheus metrics exporter:

   ```bash
   curl http://127.0.0.1:9108/metrics
   ```

11. Open the local dashboard:

   ```text
   state/observability/dashboard.html
   ```

## Generated Artifacts

Generated under `state/observability/`:

- `latest-healthcheck.json`: latest full healthcheck report.
- `metrics.json`: machine-readable runtime/SLO/cost metrics.
- `metrics.prom`: Prometheus-format local metrics export.
- `slo-summary.json` / `slo-summary.md`: 7-day healthcheck SLO rollup.
- `eval-summary.json` / `eval-summary.md`: Alice regression/eval dataset summary.
- `judged-eval.json` / `judged-eval.md`: paid judged regression run, controlled
  by `ALICE_JUDGED_EVAL` and `ALICE_JUDGED_EVAL_MAX_CASES`.
- `behavior-regression.json` / `behavior-regression.md`: real-path Telegram
  response regression, controlled by `ALICE_BEHAVIOR_REGRESSION` and
  `ALICE_BEHAVIOR_REGRESSION_MAX_CASES`.
- `retention-report.json` / `retention-report.md`: raw-log retention report.
- `trace-to-outcome.md`: trace-to-cost/outcome examples with Phoenix and LangSmith IDs.
- `dashboard.html`: local dashboard for interview/demo review.
- `alerts.jsonl`: routed alert audit log.
- `incidents/*.md`: generated when a healthcheck run has failing checks.

The artifact scheduler is installed at:

```text
~/Library/LaunchAgents/com.jordan.jobsearch.observability-artifacts.plist
```

It runs every 6 hours and at load.

The metrics exporter scheduler is installed at:

```text
~/Library/LaunchAgents/com.jordan.jobsearch.metrics-exporter.plist
```

It keeps `http://127.0.0.1:9108/metrics` available for Prometheus.

## Optional Next Upgrades

These are optional because the local/free production baseline is now implemented:

- Hosted Prometheus/Grafana or Datadog if the operator wants cloud dashboards.
- Paid paging escalation if the operator wants on-call rotations beyond Sentry,
  Telegram, and generic webhooks.
- A larger judged eval suite once more production transcripts are labeled.
