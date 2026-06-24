"""Concurrent-write test for safe_state.

Proves the locking holds under real concurrent writers. Uses
multiprocessing.Process — multiple OS processes incrementing the same
counter — because parallel subagents are separate processes, not threads,
so threads would not exercise the real contention.

Run: python3 scripts/harness/check_safe_state.py
Expected: PASS with N processes each doing K increments; final == N*K.
Also shows BASELINE (no locking) racing → lost updates, as a control.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

# Make scripts/ importable when run directly.
HERE = Path(__file__).resolve().parent

from alice import safe_state  # noqa: E402


def _bump_safe(path_str: str, iterations: int) -> None:
    """Worker: increment counter via atomic_update (race-safe)."""
    path = Path(path_str)
    for _ in range(iterations):
        def mutator(state):
            state = state or {"counter": 0}
            state["counter"] = state.get("counter", 0) + 1
            return state, None
        safe_state.atomic_update(path, mutator, default={"counter": 0})


def _bump_unsafe(path_str: str, iterations: int) -> None:
    """Worker: increment counter via naive read-modify-write (NOT safe).
    Tolerates corrupt reads (they prove the race even harder) — they
    short-circuit one increment instead of crashing the worker."""
    path = Path(path_str)
    for _ in range(iterations):
        try:
            if path.exists():
                state = json.loads(path.read_text())
            else:
                state = {"counter": 0}
        except json.JSONDecodeError:
            # Read landed mid-write (truncated file). Skip this iteration —
            # this is exactly the kind of lost-update the locking prevents.
            continue
        # Yield to maximize race chance.
        time.sleep(0.0001)
        state["counter"] = state.get("counter", 0) + 1
        try:
            path.write_text(json.dumps(state, indent=2))
        except (OSError, json.JSONDecodeError):
            continue


def _run(workers: int, iterations: int, target_fn) -> int:
    """Spawn `workers` processes each calling target_fn(path, iterations)
    against the same file. Return the final counter value."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "counter.json"
        procs = [
            mp.Process(target=target_fn, args=(str(path), iterations))
            for _ in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        for p in procs:
            assert p.exitcode == 0, f"worker died: exit={p.exitcode}"
        return json.loads(path.read_text())["counter"]


def main() -> int:
    # On macOS the default start method "spawn" forks a fresh interpreter and
    # re-imports this module — we have to guard with __main__.
    mp.set_start_method("spawn", force=True)

    workers = 8
    iterations = 50
    expected = workers * iterations

    print(f"\n=== safe_state concurrent-write test ===")
    print(f"workers={workers} iterations={iterations} expected_final={expected}\n")

    # Control: baseline with no locking should race and lose updates
    # (unless the kernel scheduler is unusually serial — in which case
    # the control may "pass," in which case we increase contention).
    baseline = _run(workers, iterations, _bump_unsafe)
    print(f"BASELINE (no locking)     : final = {baseline} "
          f"({'EXPECTED to lose updates' if baseline < expected else 'no race observed; raise contention to be sure'})")
    if baseline >= expected:
        # Increase contention with more workers.
        baseline = _run(workers * 2, iterations, _bump_unsafe)
        print(f"BASELINE (16 workers)     : final = {baseline} (control)")

    # Real test: with safe_state, final MUST equal expected.
    actual = _run(workers, iterations, _bump_safe)
    print(f"safe_state.atomic_update  : final = {actual}")

    if actual == expected:
        print(f"\nPASS — {actual}/{expected} writes survived across {workers} processes.")
        return 0
    else:
        print(f"\nFAIL — {actual}/{expected} writes survived; "
              f"{expected - actual} updates were lost. Locking did NOT hold.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
