from pathlib import Path


from alice.observability import runtime_metrics


def test_parse_launchctl_print_extracts_service_state():
    parsed = runtime_metrics._parse_launchctl_print("""
state = running
pid = 123
runs = 4
environment = {
    ALICE_TRACING => 1
}
""")

    assert parsed["state"] == "running"
    assert parsed["pid"] == 123
    assert parsed["runs"] == 4
    assert parsed["alice_tracing"] is True


def test_evaluate_flags_core_infra_checks():
    metrics = {
        "services": {
            "telegram": {
                "ok": True,
                "state": "running",
                "pid": 123,
                "alice_tracing": True,
                "runs": 2,
                "rss_mb": 100,
                "cpu_pct": 1.5,
                "uptime_seconds": 100,
            },
            "observability_healthcheck": {"ok": True, "state": "waiting"},
            "observability_artifacts": {"ok": True, "state": "waiting"},
            "metrics_exporter": {"ok": True, "state": "running", "pid": 456},
        },
        "disk": {"free_gb": 10},
    }

    checks = runtime_metrics.evaluate(metrics)

    assert all(check["status"] == "pass" for check in checks)
    assert {check["name"] for check in checks} >= {
        "infra.telegram_running",
        "infra.telegram_memory",
        "infra.repo_disk_free",
    }


def test_evaluate_fails_restart_threshold():
    metrics = {
        "services": {
            "telegram": {
                "ok": True,
                "state": "running",
                "pid": 123,
                "alice_tracing": True,
                "runs": 99,
                "rss_mb": 100,
                "cpu_pct": 1.5,
                "uptime_seconds": 100,
            },
            "observability_healthcheck": {"ok": True},
            "observability_artifacts": {"ok": True},
            "metrics_exporter": {"ok": True, "state": "running", "pid": 456},
        },
        "disk": {"free_gb": 10},
    }

    checks = runtime_metrics.evaluate(metrics)
    by_name = {check["name"]: check for check in checks}

    assert by_name["infra.telegram_restart_count"]["status"] == "fail"
