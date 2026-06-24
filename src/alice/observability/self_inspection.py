"""Alice's read-only self-inspection (Layer 4 / item 4).

Strict allowlist: a hardcoded set of safe read-only commands scoped to Alice's
own repo paths. NO shell metacharacters, NO piping, NO write/exec commands,
NO arbitrary user-supplied paths.

Surface used by telegram_bot.py /changes /log /diff /show /inspect commands.

Design rule: the most general action surface gets the tightest constraint.
This module is the "general shell" — therefore it is the tightest gate.
"""
import re
import subprocess
from pathlib import Path
from alice import repo_paths

# Two repos Alice may inspect — and nothing else.
MAIN_REPO = Path(repo_paths.ROOT)
STATE_REPO = Path(repo_paths.FEEDBACK)

# Token-shape allowlist for path arguments (no /, no .., no shell metachars).
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")
_FORBIDDEN_FRAGMENTS = ("..", "//", "|", ";", "&", "$", "`", "\n", "\r", "\\")

# Refuse to inspect ANY path outside these two trees.
_ALLOWED_ROOTS = (MAIN_REPO, STATE_REPO)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _path_is_safe(p: str) -> bool:
    """A path arg is safe iff it has no shell metachars, no '..', and resolves
    under one of the two allowed roots."""
    if not p:
        return False
    if any(frag in p for frag in _FORBIDDEN_FRAGMENTS):
        return False
    if not _SAFE_PATH_RE.match(p):
        return False
    return True


def _resolve_within_allowed(repo: Path, rel: str) -> Path | None:
    """Resolve `rel` against `repo`, refuse anything that escapes the root."""
    if not _path_is_safe(rel):
        return None
    candidate = (repo / rel).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    return candidate


def _run(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command with no shell, hard timeout, captured output. Truncated to 3500 chars."""
    try:
        res = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
        out = (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "[timeout]"
    except FileNotFoundError as e:
        return 127, f"[not found: {e}]"
    if len(out) > 3500:
        out = out[:3500] + "\n...(truncated)"
    return res.returncode, out


def _choose_repo(repo_key: str) -> Path | None:
    """Map a user-facing key ('main' or 'state') to the actual path."""
    if repo_key == "main":
        return MAIN_REPO
    if repo_key == "state":
        return STATE_REPO
    return None


# ─── allowlisted commands ─────────────────────────────────────────────────────

def git_log(repo_key: str = "main", n: int = 10) -> str:
    """git log --oneline -n N on the named repo. N capped at 50."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    n = max(1, min(int(n), 50))
    _, out = _run(["git", "log", "--oneline", "-n", str(n)], repo)
    return out or "(no output)"


def git_status(repo_key: str = "main") -> str:
    """git status --short on the named repo."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    _, out = _run(["git", "status", "--short"], repo)
    return out or "(working tree clean)"


def git_diff(repo_key: str = "main", target: str = "HEAD~1") -> str:
    """git diff <target> on the named repo. Target restricted to a safe shape
    (HEAD, HEAD~N, a commit SHA, or a branch name)."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    if not re.match(r"^(HEAD(~\d{1,3})?|[0-9a-fA-F]{7,40}|[A-Za-z0-9_/\-.]{1,60})$", target):
        return f"[refusing unsafe target {target!r}]"
    if any(frag in target for frag in _FORBIDDEN_FRAGMENTS):
        return f"[refusing unsafe target {target!r}]"
    _, out = _run(["git", "diff", "--stat", target], repo)
    return out or "(no diff)"


def git_show(repo_key: str = "main", commit: str = "HEAD") -> str:
    """git show --stat <commit> on the named repo."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    if not re.match(r"^(HEAD(~\d{1,3})?|[0-9a-fA-F]{7,40})$", commit):
        return f"[refusing unsafe commit ref {commit!r}]"
    _, out = _run(["git", "show", "--stat", commit], repo)
    return out or "(no output)"


def ls(repo_key: str, rel_path: str = ".") -> str:
    """ls -la for a path inside the named repo."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    p = _resolve_within_allowed(repo, rel_path)
    if p is None:
        return f"[refusing unsafe path {rel_path!r}]"
    if not p.exists():
        return f"[path does not exist: {rel_path!r}]"
    _, out = _run(["ls", "-la", str(p)], repo)
    return out or "(empty)"


def cat(repo_key: str, rel_path: str, max_bytes: int = 3500) -> str:
    """Read a file from inside the named repo. Truncated to max_bytes."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    p = _resolve_within_allowed(repo, rel_path)
    if p is None:
        return f"[refusing unsafe path {rel_path!r}]"
    if not p.exists():
        return f"[path does not exist: {rel_path!r}]"
    if not p.is_file():
        return f"[not a file: {rel_path!r}]"
    try:
        data = p.read_bytes()
    except Exception as e:
        return f"[read failed: {e}]"
    text = data[:max_bytes].decode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text += "\n...(truncated)"
    return text


def stat_file(repo_key: str, rel_path: str) -> str:
    """stat output for a path inside the named repo."""
    repo = _choose_repo(repo_key)
    if repo is None:
        return f"[unknown repo {repo_key!r}; use 'main' or 'state']"
    p = _resolve_within_allowed(repo, rel_path)
    if p is None:
        return f"[refusing unsafe path {rel_path!r}]"
    if not p.exists():
        return f"[path does not exist: {rel_path!r}]"
    _, out = _run(["stat", str(p)], repo)
    return out or "(no output)"


# ─── higher-level summaries ───────────────────────────────────────────────────

def recent_changes_summary(n: int = 10) -> str:
    """Combined view: main repo recent log + state repo recent log."""
    main_log = git_log("main", n=n)
    state_log = git_log("state", n=n)
    main_status = git_status("main")
    state_status = git_status("state")
    return (
        f"=== MAIN REPO ({MAIN_REPO}) ===\n"
        f"Recent commits:\n{main_log}\n"
        f"Working tree:\n{main_status}\n\n"
        f"=== STATE REPO ({STATE_REPO}) ===\n"
        f"Recent commits:\n{state_log}\n"
        f"Working tree:\n{state_status}"
    )


if __name__ == "__main__":
    print(recent_changes_summary(5))
