"""Positive safety code gates for Alice's write and action surface.

Each function fences a hard-safety constraint by checking it at the
operation's execute path, so the gate holds regardless of which tool calls it.

Guard rule: each function raises `ForbiddenAction` (a subclass of
PermissionError) with a specific, surfaceable message — never returns
silently, never logs-and-skips. This is P2 (fail loud): a guard that
"silently refused" looks identical to a guard that's broken.

To wire into a tool: at the very top of the tool's execute path, call the
relevant guard. If it returns, the operation is allowed. If it raises,
the tool surfaces the refusal to Alice's runtime, which surfaces it to the operator.

The no-third-party-send constraint is enforced positively by hardcoded
config destinations in notify_email/notify_telegram. It is NOT re-checked
here — that gate lives at the send site. This module covers the constraints
that aren't already code-enforced.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from alice import repo_paths
from typing import Iterable, Sequence


REPO_ROOT = Path(repo_paths.ROOT).resolve()


class ForbiddenAction(PermissionError):
    """An action was refused by a safety gate. The message names which gate
    and why, so Alice can surface a specific refusal to the operator."""


# ─── 1. No git push ───────────────────────────────────────────────────────────

# Pattern matches `git push`, `git -C foo push`, `git --git-dir=... push`,
# `git push --force`, etc. The matcher walks the argv after `git`, skipping
# `-c key=val`, `-C path`, `--git-dir=...`, `--work-tree=...` options, and
# checks if the next bare token is `push`. This avoids both false positives
# (e.g. `git commit -m "push notification"`) and false negatives.
_GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace",
                               "--exec-path", "--super-prefix", "-c"}
_GIT_GLOBAL_OPTS_BOOLEAN = {"--no-pager", "--bare", "--no-replace-objects",
                            "--literal-pathspecs", "--glob-pathspecs",
                            "--noglob-pathspecs", "--icase-pathspecs",
                            "--no-optional-locks", "--paginate", "-p"}


def _git_subcommand(argv: Sequence[str]) -> str | None:
    """Return the first non-option subcommand (e.g. 'push', 'commit'), or None."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_GLOBAL_OPTS_BOOLEAN:
            i += 1
            continue
        if tok in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith(("--git-dir=", "--work-tree=", "--namespace=",
                           "--exec-path=", "--super-prefix=")):
            i += 1
            continue
        if tok.startswith("-c") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
 # Unknown flag — keep going.
            i += 1
            continue
        return tok
    return None


def assert_no_git_push(argv: Sequence[str]) -> None:
    """Refuse if argv corresponds to `git ... push ...`.

    argv must NOT include the leading `git` token; pass the post-binary args.
    For a full command, callers should split with shlex and skip the binary
    name (or use `refuse_shell_if_git_push` below).
    """
    sub = _git_subcommand(argv)
    if sub == "push":
        raise ForbiddenAction(
            "guard:no_git_push refused `git push`. Alice may not push to any "
            "remote. The current dev session may push by hand."
        )


def refuse_shell_if_git_push(cmd: str) -> None:
    """Refuse if `cmd` is a shell string that would invoke `git push`.

    Shell-string variant for the write_file/exec tools. Uses shlex
    to tokenize; if tokenization fails the input is rejected (a shell string
    we can't parse is exactly the case we shouldn't trust)."""
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        raise ForbiddenAction(
            f"guard:no_git_push refused unparseable shell input ({e}). "
            "Tokenizable, single-line commands only."
        )
    if not tokens:
        return
 # Strip leading env-var assignments (FOO=bar git push) and `sudo`.
    while tokens and (tokens[0] == "sudo" or re.match(r"^[A-Z_][A-Z0-9_]*=", tokens[0])):
        tokens = tokens[1:]
    if not tokens:
        return
    binary = os.path.basename(tokens[0])
    if binary == "git":
        assert_no_git_push(tokens[1:])


# ─── 2. No autonomous irreversible delete ────────────────────────────────────

# Anything outside an explicit operator-approval flow that would delete state
# files, application content, scripts, or the safe_state lock sidecars must
# be refused. Any Alice tool that deletes files routes through
# assert_no_irreversible_delete, which refuses without an operator-issued
# token. This module defines the gate and the protected-path predicate.

_PROTECTED_DELETE_SUBTREES = (
    REPO_ROOT / "applications",
    REPO_ROOT / "feedback",
    REPO_ROOT / "scripts",
    REPO_ROOT / "templates",
    REPO_ROOT / "targets",
    REPO_ROOT / "output",
    REPO_ROOT / "knowledge",
    REPO_ROOT / "ops",
    REPO_ROOT / ".beads",
    REPO_ROOT / ".git",
)


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_protected_from_delete(path: Path | str) -> bool:
    p = Path(path)
    return any(_is_under(p, sub) for sub in _PROTECTED_DELETE_SUBTREES)


def assert_no_irreversible_delete(path: Path | str, *,
                                   operator_approval_token: str | None = None) -> None:
    """Refuse deletion of any protected path unless the operator issued an approval
    token for THIS specific path. Any non-empty token is accepted."""
    if not is_protected_from_delete(path):
        return  # outside protected subtrees; not our gate's problem
    if operator_approval_token:
        return
    raise ForbiddenAction(
        f"guard:no_irreversible_delete refused delete of {Path(path)}. "
        "Path is under a protected subtree (applications/, feedback/, "
        "scripts/, templates/, targets/, output/, knowledge/, ops/, "
        ".beads/, .git/). An operator-issued approval token is required to "
        "delete from these subtrees."
    )


# ─── 3. No arbitrary shell ───────────────────────────────────────────────────

# The only sanctioned shell surface is `scripts/self_inspection.py`'s strict
# allowlist (read-only inspection: ls / git log / git diff / etc., capped at
# 10s, shell=False, predefined argv shapes). Any tool that does NOT route
# through self_inspection must not introduce a general shell path.

# This gate is a check the future write/action tools call before they would
# spawn a subprocess. They pass the proposed argv0; this raises unless argv0
# is on the explicitly-sanctioned binary list (currently empty — the only
# shell that exists is self_inspection's, which calls subprocess directly
# without going through this gate).

_SANCTIONED_SHELL_BINARIES = frozenset({
 # populated only by deliberate code review; currently nothing belongs here
})


def assert_no_arbitrary_shell(argv0: str) -> None:
    """Refuse arbitrary subprocess spawns from Alice's tool surface.

    Use at the top of any tool that's tempted to call subprocess.run with
    LLM-supplied input. The sanctioned read-only allowlist
    (self_inspection.py) does NOT route through this gate — it has its own
    internal allowlist with shell=False and predefined argv shapes. This
    gate is the second line of defense for tools that aren't self_inspection.
    """
    binary = os.path.basename(argv0)
    if binary in _SANCTIONED_SHELL_BINARIES:
        return
    raise ForbiddenAction(
        f"guard:no_arbitrary_shell refused subprocess spawn of {binary!r}. "
        "The only sanctioned shell surface is scripts/self_inspection.py's "
        "internal read-only allowlist. Adding a new sanctioned binary "
        "requires deliberate code review and an explicit allowlist update."
    )


# ─── 4. No self-edit of brief / soul / scripts ───────────────────────────────

_SELF_EDIT_FORBIDDEN = (
    REPO_ROOT / "Alice.md",
    REPO_ROOT / "ALICE_SOUL.md",
)
_SELF_EDIT_FORBIDDEN_TREE = (
    REPO_ROOT / "scripts",
)


def is_self_edit_target(path: Path | str) -> bool:
    p = Path(path).resolve()
    if any(p == f.resolve() for f in _SELF_EDIT_FORBIDDEN):
        return True
    if any(_is_under(p, sub) for sub in _SELF_EDIT_FORBIDDEN_TREE):
        return True
    return False


def assert_no_self_edit(path: Path | str) -> None:
    """Refuse writes to Alice.md, ALICE_SOUL.md, or anything under scripts/.

    These are her brief, her soul, and her own code. A tool that lets Alice
    rewrite them is identity-corruption-shaped — see Soul section 2.7
    (personality as defense)."""
    if is_self_edit_target(path):
        raise ForbiddenAction(
            f"guard:no_self_edit refused write to {Path(path)}. "
            "Alice may not edit her own brief (Alice.md), her own soul "
            "(ALICE_SOUL.md), or her own code (scripts/). The operator edits those, "
            "or a deliberate dev session does."
        )


# ─── 5. write_file path allowlist ────────────────────────────────────────────

# The write_file tool writes only under repo root and only to the subtrees
# Alice owns. templates/ (master resume variants) is OUT —
# corrupting a template would corrupt every future application package.

_WRITE_ALLOWED_TREES = (
    REPO_ROOT / "applications",
    REPO_ROOT / "feedback",
    REPO_ROOT / "targets",
    REPO_ROOT / "output",
    REPO_ROOT / "knowledge",
)


def is_write_allowed(path: Path | str) -> bool:
    """Return True only if `path` is inside repo root AND inside one of the
    allowed subtrees AND is not a self-edit target."""
    p = Path(path)
    try:
        p_resolved = p.resolve()
    except (OSError, RuntimeError):
        return False
    if not _is_under(p_resolved, REPO_ROOT):
        return False
    if is_self_edit_target(p):
        return False
    return any(_is_under(p_resolved, t) for t in _WRITE_ALLOWED_TREES)


def assert_write_allowed(path: Path | str) -> None:
    """Refuse writes outside the allowlist. Combines self-edit and tree
    checks so callers only need one gate at the top of write_file."""
    p = Path(path)
    assert_no_self_edit(p)  # raise the more specific reason first
    if not is_write_allowed(p):
        raise ForbiddenAction(
            f"guard:write_file_allowlist refused write to {p}. "
            "Permitted subtrees: applications/, feedback/, targets/, "
            "output/, knowledge/ — all under repo root, and none of "
            "Alice's own brief/soul/code."
        )
