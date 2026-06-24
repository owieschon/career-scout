#!/usr/bin/env bash
# On-demand Phoenix collector for Alice tracing capture windows.
#
# Tracing model (F1, sub-option (b1) "on-demand"):
#   - The telegram daemon has ALICE_TRACING=1 PERMANENTLY in its launchd plist,
#     so it always TRIES to emit spans. (The span machinery is light; only the
#     EXPORT needs a collector.)
#   - This collector is brought up ONLY during a measurement/capture window
#     (read_sheet baseline, model-routing comparison, prompt/harness experiments)
#     and taken down afterward.
#   - When this collector is DOWN, the daemon's spans drop HARMLESSLY — the
#     BatchSpanProcessor queues/drops without ever blocking or raising into
#     llm.call (the fail-safe). So tracing-on-permanently + collector-on-demand
#     is safe, with no per-campaign daemon restart.
#
# Usage:
#   scripts/phoenix_capture.sh start    # bring the collector up (start a capture window)
#   scripts/phoenix_capture.sh stop     # take it down (end the window) -> resting state
#   scripts/phoenix_capture.sh status   # is it up?
#
# Phoenix UI + OTLP endpoint: http://localhost:6006  (telemetry.py default).
set -euo pipefail

PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PHOENIX_ENTRY="$ROOT/src/alice/observability/phoenix_local_server.py"
STATE="$ROOT/state"
PIDFILE="$STATE/phoenix.pid"
LOG="$STATE/phoenix.log"
PORT=6006

_alive() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if _alive; then echo "phoenix already up (pid $(cat "$PIDFILE")) on :$PORT"; exit 0; fi
    mkdir -p "$STATE"
    : > "$LOG"
    env PHOENIX_HOST=127.0.0.1 "$PY" "$PHOENIX_ENTRY" serve >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "phoenix starting (pid $(cat "$PIDFILE")), log $LOG"
    # wait briefly for the port to answer
    for _ in $(seq 1 20); do
      if ! _alive; then
        echo "phoenix failed to start — check $LOG"
        exit 1
      fi
      if curl -s -o /dev/null --max-time 1 "http://localhost:$PORT" 2>/dev/null; then
        echo "phoenix UP on http://localhost:$PORT"; exit 0
      fi
    done
    echo "phoenix launched but http://localhost:$PORT not answering yet — check $LOG"
    ;;
  stop)
    if _alive; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; fi
    rm -f "$PIDFILE"
    echo "phoenix stopped (resting state — daemon spans now drop harmlessly)"
    ;;
  status)
    if _alive; then echo "phoenix UP (pid $(cat "$PIDFILE")) on :$PORT"
    else echo "phoenix DOWN (resting — daemon ALICE_TRACING=1 still set; spans drop until a window opens)"; fi
    ;;
  *)
    echo "usage: $0 start|stop|status"; exit 1 ;;
esac
