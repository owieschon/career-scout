#!/usr/bin/env bash
#
# stats.sh -- human-run summary of rejection statistics.
#
# Reads rails/evidence/stats.jsonl (append-only, written by verify.sh and
# gate_stop.py) and reports how often each check fired and which dispatches
# attract the most rejections. Read-only; safe to run any time.
#
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$ROOT/rails/verifier/stats.py" summary "$ROOT"
