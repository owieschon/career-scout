"""Concurrency-safe single-slot JSON state file primitives.

Used by every single-slot JSON state file in `feedback/`:
focus.json, pending-confirmation.json, prep-queue.json, triage-state.json,
digest-prefs.json, digest-published-threads.json, cost-alert-state.json,
readiness-last.json.

Append-only JSONL logs are already concurrency-safe (one-line append on POSIX
is atomic for small writes) and do not need this module.

Approach: fcntl.flock advisory locking + temp-file + os.replace. This is
simpler than converting every state file to an event log, and sufficient for
the concurrency introduced by parallel subagents.

The hold-time for the exclusive lock is the JSON parse-mutate-serialize-write
window only — never an external fetch. Callers do expensive reads (sheet API,
network) BEFORE entering atomic_update so the lock is held for milliseconds.

Lockfile sidecar (`<path>.lock`) is used rather than locking the data file
itself. This decouples the lock from rename-replace semantics: os.replace
swaps the inode, so a lock held on the old file would not protect a writer
about to start. The sidecar is created once and reused.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional, Tuple


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextmanager
def _flock(path: Path, exclusive: bool):
    """Advisory lock on the sidecar lock file. Block until acquired."""
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
 # O_CREAT so the lockfile exists even if path itself doesn't yet
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, flag)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def atomic_read(path: Path | str, default: Any = None) -> Any:
    """Read JSON under a shared lock. Returns default if file missing.

    Raises json.JSONDecodeError if the file exists but is malformed.
    Callers that expect "missing OR malformed → default" should wrap or
    catch explicitly — silent fallback would hide a fail-loud violation.
    """
    path = Path(path)
    if not path.exists():
        return default
    with _flock(path, exclusive=False):
        return json.loads(path.read_text())


def atomic_write(path: Path | str, data: Any) -> None:
    """Write JSON atomically under an exclusive lock.

    Sequence: acquire LOCK_EX → write to temp file in same dir → fsync →
    os.replace → release lock. The temp + replace makes the swap atomic at
    the filesystem level; a concurrent reader sees either the old file or
    the new file, never a half-written one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path, exclusive=True):
        _write_atomic_inplace(path, data)


def _write_atomic_inplace(path: Path, data: Any) -> None:
    """Atomic write helper. Caller already holds the lock."""
 # NamedTemporaryFile keeps the file open; we want to fsync then close
 # before rename, so we manage the fd manually.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        raise


def atomic_update(
    path: Path | str,
    mutate_fn: Callable[[Any], Tuple[Any, Any]],
    default: Any = None,
    skip_write_if_unchanged: bool = False,
) -> Any:
    """Read-modify-write atomically under an exclusive lock.

    mutate_fn receives the current state (or `default` if file absent) and
    must return a tuple `(new_state, return_value)`. The new_state is
    written; return_value is returned to the caller.

    If skip_write_if_unchanged=True and `new_state == old_state`, no write
    happens — this preserves the focus.py "don't bump mtime for noops"
    discipline (don't emit state changes that didn't happen).

    Use atomic_update for any operation that reads state, modifies it, and
    writes it back — collapsed under a single lock to eliminate the TOCTOU
    window between separate _load/_save calls.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path, exclusive=True):
        if path.exists():
            current = json.loads(path.read_text())
        else:
            current = default
        new_state, retval = mutate_fn(current)
        if skip_write_if_unchanged and new_state == current:
            return retval
        _write_atomic_inplace(path, new_state)
        return retval
