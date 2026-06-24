"""Tests proving each safety gate refuses its forbidden action.

Each of the four positive code gates must demonstrably refuse its forbidden
action: a git push, an irreversible delete, a shell escape, and a self-edit
are each attempted and must be refused.

The write_file allowlist is also checked: it refuses an out-of-bounds write
and permits an in-bounds one.

Run: python3 scripts/harness/check_guards.py
Expected: PASS — every refusal raises ForbiddenAction; every permitted
operation passes through silently. A guard that swallows a refusal is a
broken guard, so refusals must fail loud.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

from alice import guards  # noqa: E402


def _expect_refused(label: str, fn, *args, **kwargs) -> bool:
    """Assert fn raises ForbiddenAction. Return True on pass."""
    try:
        fn(*args, **kwargs)
    except guards.ForbiddenAction as e:
        print(f"  PASS — {label}: refused (\"{str(e)[:80]}...\")")
        return True
    except Exception as e:
        print(f"  FAIL — {label}: raised wrong exception type {type(e).__name__}: {e}")
        return False
    print(f"  FAIL — {label}: did NOT raise (should have refused)")
    return False


def _expect_allowed(label: str, fn, *args, **kwargs) -> bool:
    """Assert fn returns without raising. Return True on pass."""
    try:
        fn(*args, **kwargs)
        print(f"  PASS — {label}: allowed (silent return)")
        return True
    except Exception as e:
        print(f"  FAIL — {label}: raised {type(e).__name__}: {e}")
        return False


# ─── 1. git push refusal ─────────────────────────────────────────────────────

def test_git_push() -> int:
    print("\n[1/5] git push refusal")
    pass_count = 0
    pass_count += _expect_refused("plain push",
                                  guards.assert_no_git_push, ["push", "origin", "main"])
    pass_count += _expect_refused("push with -f",
                                  guards.assert_no_git_push, ["push", "-f"])
    pass_count += _expect_refused("git -C path push",
                                  lambda: guards.refuse_shell_if_git_push("git -C /tmp push origin main"))
    pass_count += _expect_refused("git --git-dir push",
                                  lambda: guards.refuse_shell_if_git_push("git --git-dir=/tmp/.git push"))
    pass_count += _expect_refused("env-prefix git push",
                                  lambda: guards.refuse_shell_if_git_push("FOO=bar git push"))
    pass_count += _expect_refused("sudo git push",
                                  lambda: guards.refuse_shell_if_git_push("sudo git push origin main"))
    # negative: these should NOT be refused
    pass_count += _expect_allowed("git status (not push)",
                                  guards.assert_no_git_push, ["status"])
    pass_count += _expect_allowed("git commit (not push)",
                                  guards.assert_no_git_push, ["commit", "-m", "I am about to push the button"])
    pass_count += _expect_allowed("unparseable string is not-git",
                                  lambda: guards.refuse_shell_if_git_push("echo hello"))
    return pass_count


# ─── 2. irreversible delete refusal ──────────────────────────────────────────

def test_delete() -> int:
    print("\n[2/5] irreversible delete refusal")
    pass_count = 0
    pass_count += _expect_refused("delete feedback/focus.json",
                                  guards.assert_no_irreversible_delete,
                                  guards.REPO_ROOT / "feedback" / "focus.json")
    pass_count += _expect_refused("delete applications/foo",
                                  guards.assert_no_irreversible_delete,
                                  guards.REPO_ROOT / "applications" / "foo")
    pass_count += _expect_refused("delete scripts/llm.py",
                                  guards.assert_no_irreversible_delete,
                                  guards.REPO_ROOT / "scripts" / "llm.py")
    # with token: allowed
    pass_count += _expect_allowed("delete with approval token",
                                  guards.assert_no_irreversible_delete,
                                  guards.REPO_ROOT / "feedback" / "focus.json",
                                  operator_approval_token="approved-by-operator")
    # outside protected tree: allowed (not our gate's concern)
    pass_count += _expect_allowed("delete /tmp/foo (outside repo)",
                                  guards.assert_no_irreversible_delete,
                                  "/tmp/foo")
    return pass_count


# ─── 3. arbitrary shell refusal ──────────────────────────────────────────────

def test_shell() -> int:
    print("\n[3/5] arbitrary shell refusal")
    pass_count = 0
    pass_count += _expect_refused("bash", guards.assert_no_arbitrary_shell, "/bin/bash")
    pass_count += _expect_refused("sh", guards.assert_no_arbitrary_shell, "sh")
    pass_count += _expect_refused("python3 -c", guards.assert_no_arbitrary_shell, "/usr/bin/python3")
    pass_count += _expect_refused("any arbitrary binary", guards.assert_no_arbitrary_shell, "/usr/bin/curl")
    return pass_count


# ─── 4. self-edit refusal ────────────────────────────────────────────────────

def test_self_edit() -> int:
    print("\n[4/5] self-edit refusal")
    pass_count = 0
    pass_count += _expect_refused("Alice.md",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "Alice.md")
    pass_count += _expect_refused("ALICE_SOUL.md",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "ALICE_SOUL.md")
    pass_count += _expect_refused("scripts/llm.py",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "scripts" / "llm.py")
    pass_count += _expect_refused("scripts/safe_state.py",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "scripts" / "safe_state.py")
    pass_count += _expect_refused("nested scripts path",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "scripts" / "harness" / "foo.py")
    # negative: writes outside scripts/ and not the brief/soul → allowed
    pass_count += _expect_allowed("feedback/foo.json (not self-edit)",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "feedback" / "foo.json")
    pass_count += _expect_allowed("README.md (not self-edit target)",
                                  guards.assert_no_self_edit,
                                  guards.REPO_ROOT / "README.md")
    return pass_count


# ─── 5. write_file allowlist ─────────────────────────────────────────────────

def test_write_allowlist() -> int:
    print("\n[5/5] write_file path allowlist")
    pass_count = 0
    # Permitted subtrees
    pass_count += _expect_allowed("feedback/observations.md",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "feedback" / "observations.md")
    pass_count += _expect_allowed("applications/foo/draft.md",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "applications" / "foo" / "draft.md")
    pass_count += _expect_allowed("targets/companies/x.md",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "targets" / "companies" / "x.md")
    pass_count += _expect_allowed("output/report.md",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "output" / "report.md")
    pass_count += _expect_allowed("knowledge/entry.md",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "knowledge" / "entry.md")
    # Refused
    pass_count += _expect_refused("templates/resume-x.docx",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "templates" / "resume.docx")
    pass_count += _expect_refused("outside repo root /tmp/x",
                                  guards.assert_write_allowed,
                                  Path("/tmp/x"))
    pass_count += _expect_refused("/etc/passwd",
                                  guards.assert_write_allowed,
                                  Path("/etc/passwd"))
    pass_count += _expect_refused("scripts/foo.py (self-edit)",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "scripts" / "foo.py")
    pass_count += _expect_refused("Alice.md (self-edit)",
                                  guards.assert_write_allowed,
                                  guards.REPO_ROOT / "Alice.md")
    return pass_count


def main() -> int:
    print("=== guards.py: positive safety gate refusal tests ===")
    p1 = test_git_push()
    p2 = test_delete()
    p3 = test_shell()
    p4 = test_self_edit()
    p5 = test_write_allowlist()
    total = p1 + p2 + p3 + p4 + p5

    # Each test_* prints PASS/FAIL inline; tally expected counts.
    expected = 9 + 5 + 4 + 7 + 10  # = 35
    print(f"\n=== {total}/{expected} subtests passed ===")
    if total != expected:
        print("FAIL — at least one gate did not refuse / permit as expected.")
        return 1
    print("PASS — every gate refused every forbidden action and permitted the negatives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
