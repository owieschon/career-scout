"""Tiny config loader. Reads ~/.config/job-search/config.env (KEY=VALUE lines),
with environment variables taking precedence. Keeps secrets out of the repo."""
import os
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "job-search" / "config.env"


def load():
    cfg = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg
