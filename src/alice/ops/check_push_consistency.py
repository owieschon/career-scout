#!/usr/bin/env python3
"""Pre-push self-consistency check — the structural fix for the integrity-hole
bug class (a commit that does not contain its own dependencies), which recurred
~5 times this session and was always caught POST-hoc. This catches it BEFORE a
push reaches origin.

Two flavors of the bug:
  (A) IMPORT flavor: committed code imports a module that is not committed
      (1A's decision_feedback, the Move-3 telemetry sweep, the Phoenix commit).
  (B) CONTRACT flavor: a caller against a callee whose committed signature does
      not match (7f7fa6f's 3-tuple unpack of a 2-tuple return).

Runs against a FRESH CHECKOUT of the to-be-pushed HEAD (git archive into a temp
dir = committed content ONLY, no working-tree files to mask a missing dep).

  1. IMPORT flavor — static resolution (definitive, catches LAZY imports too):
     for every committed scripts/*.py, AST-collect imports of names that ARE
     local modules (exist as scripts/*.py in the working tree). If a committed
     file imports a local module that is NOT in the fresh checkout -> BLOCK.
     This is the authoritative import-flavor catcher; it sees function-level
     (lazy) imports that a plain `import X` would never execute.

  2. CONTRACT flavor — run the test harness (pytest) against the fresh checkout.
     A 7f7fa6f-style arity mismatch is a RUNTIME error, so only a test that
     EXECUTES the path (or a type-checker) catches it. The harness is the
     authoritative backstop here.

  3. pyright --warnings — a CHEAP first-pass for the contract flavor, WHEN
     installed and WHEN types are tight enough to see the mismatch. Optional;
     skipped (with a note) if pyright is absent. NEVER relied on alone.

Exit 0 = consistent (push proceeds). Exit non-zero = BLOCK.

BYPASS POLICY (critical): the only escape hatch is `git push --no-verify`,
which is the operator's deliberate human override. Dispatch agents MUST NOT --no-verify
past a block — STOP and surface to the operator. (Encoded in CLAUDE.md.)
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from alice import repo_paths

REPO = repo_paths.ROOT

# Daemon/cron entry points the daemon actually executes. run_daily launches its
# pipeline steps as SUBPROCESSES (not imports), so an import-graph traversal from
# these roots alone would miss the steps — _subprocess_seeds() recovers them.
_EXPLICIT_ROOTS = {"telegram_bot", "run_daily", "imap_reply"}
_SCRIPTS_PATH_RE = re.compile(r"scripts/([A-Za-z_][A-Za-z0-9_]*)\.py")


def _run(args, cwd=None, **kw):
    return subprocess.run(args, cwd=str(cwd or REPO), capture_output=True, text=True, **kw)


def _fresh_checkout(rev: str) -> Path:
    """git archive <rev> into a temp dir = committed content only."""
    tmp = Path(tempfile.mkdtemp(prefix="prepush-"))
    p = subprocess.run(f"git archive {rev} | tar -x -C {tmp}",
                       shell=True, cwd=str(REPO))
    if p.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"git archive {rev} failed")
    return tmp


def _local_module_universe() -> set:
    """Every name that IS a local module (scripts/*.py stem) in the WORKING tree —
    the reference for 'this import should resolve to a committed file', which
    cleanly distinguishes local modules (must be committed) from installed
    third-party packages (assumed present, not our concern)."""
    return {p.stem for p in (REPO / "scripts").glob("*.py")}


def _imported_local(pyf: Path, local_universe: set) -> set:
    """Top-level-module names imported by pyf that ARE local modules. Walks the
    full AST, so it sees function-level (LAZY) imports — e.g. tools.py's lazy
    `import prep_pipeline` (the 7f7fa6f module) — that a runtime import wouldn't
    execute."""
    try:
        tree = ast.parse(pyf.read_text(encoding="utf-8"))
    except Exception:
        return set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    return {nm for nm in imported if nm in local_universe}


def _subprocess_seeds(checkout: Path) -> set:
    """Modules launched as `scripts/X.py` string literals INSIDE a list node —
    the `[PY, "scripts/X.py", ...]` subprocess call-site form run_daily uses.
    AST-scoped to ast.List so docstring usage examples ('python3 scripts/foo.py')
    are NOT mistaken for launches (that over-broad match would re-include the
    an orphaned module and defeat reachable-only scoping)."""
    seeds = set()
    for pyf in (checkout / "scripts").rglob("*.py"):
        try:
            tree = ast.parse(pyf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                for el in node.elts:
                    if isinstance(el, ast.Constant) and isinstance(el.value, str):
                        m = _SCRIPTS_PATH_RE.search(el.value)
                        if m:
                            seeds.add(m.group(1))
    return seeds


def check_import_resolution(checkout: Path, local_universe: set) -> list:
    """Return (file, missing_module) holes — but only for files REACHABLE from
    the real entry points (daemon roots + subprocess-launched steps + their
    transitive imports). Scoped this way deliberately: every historical hole
    (1A, 7f7fa6f, Move-3, Phoenix) lived in reachable code that actually broke a
    path; an orphaned file's broken import (e.g. a stale module importing the
    gitignored ingest_listing) never executes and is dead-code rot, not the
    daemon-breaking bug class — so it is filed for cleanup, not push-blocked."""
    committed = {p.stem for p in (checkout / "scripts").glob("*.py")}
 # imported-local map for every committed top-level module (one parse each).
    imp_map = {p.stem: _imported_local(p, local_universe)
               for p in (checkout / "scripts").glob("*.py")}

 # Reachability seeds: explicit daemon roots + subprocess-launched steps,
 # restricted to committed modules. (A launched-but-uncommitted target is its
 # own integrity flavor, filed as a noted extension — not flagged here.)
    seeds = (_EXPLICIT_ROOTS | _subprocess_seeds(checkout)) & committed

 # BFS the transitive import closure, following committed imports (incl lazy).
    closure, queue = set(), list(seeds)
    while queue:
        m = queue.pop()
        if m in closure:
            continue
        closure.add(m)
        for dep in imp_map.get(m, ()):
            if dep in committed and dep not in closure:
                queue.append(dep)

    holes = []
    for m in sorted(closure):
        for nm in sorted(imp_map.get(m, ())):
            if nm not in committed:
                holes.append((f"{m}.py", nm))
    return holes


# Curated contract subset: FAST (<1s) + deterministic + hermetic (no network/API).
# These are the tests that EXECUTE cross-module call contracts, so a 7f7fa6f-style
# arity/signature drift surfaces as a failure here. Deliberately NOT the full
# harness — the live/network tests (test_tools live-loop, test_f1_email,
# test_natural_prompts, test_call_with_tools) are slow (~50s) and flaky, which
# would train bypass. test_experience_store is the critical one: it unpacks
# retrieve_for_role, so it catches the exact 7f7fa6f contract (proven at build
# time). Extend this list as new cross-module contracts gain deterministic tests.
_CONTRACT_TESTS = ("test_experience_store.py", "test_prep_pipeline.py")


def check_harness(checkout: Path) -> tuple:
    """Run the curated contract-sensitive tests against the fresh checkout.
    Returns (ok, detail). Authoritative catcher for the contract flavor (pyright
    is the cheap first-pass when present; here it is absent, so this carries it)."""
    env = dict(os.environ, PYTHONPATH=str(checkout / "scripts"))
    targets = [str(checkout / "scripts" / "harness" / t)
               for t in _CONTRACT_TESTS
               if (checkout / "scripts" / "harness" / t).exists()]
    if not targets:
        return True, "no contract tests present in checkout (skipped)"
 # pytest: contract mismatches surface as failures/errors (not skips).
    p = subprocess.run([sys.executable, "-m", "pytest", *targets,
                        "-q", "-p", "no:cacheprovider", "--no-header"],
                       cwd=str(checkout), env=env, capture_output=True, text=True)
 # pytest exit: 0=all pass, 1=tests failed, 2=usage, 5=no tests collected.
    if p.returncode in (0, 5):
        return True, f"contract tests pass ({', '.join(_CONTRACT_TESTS)}; pytest rc={p.returncode})"
    return False, f"contract tests FAILED (pytest rc={p.returncode}):\n{(p.stdout or '')[-1500:]}"


def check_pyright(checkout: Path) -> tuple:
    """Cheap first-pass contract check, IF pyright is available. Optional."""
    exe = shutil.which("pyright")
    if not exe:
        return None, "pyright not installed (skipped — harness is authoritative)"
    p = subprocess.run([exe, "--warnings", str(checkout / "scripts")],
                       capture_output=True, text=True)
    if p.returncode == 0:
        return True, "pyright clean"
    return False, f"pyright reported issues:\n{(p.stdout or '')[-1200:]}"


def run_check(rev: str = "HEAD") -> int:
    local_universe = _local_module_universe()
    checkout = _fresh_checkout(rev)
    failures = []
    try:
        holes = check_import_resolution(checkout, local_universe)
        if holes:
            failures.append("IMPORT-flavor hole(s) — committed code imports an "
                            "uncommitted local module:\n" +
                            "\n".join(f"    {f} imports '{m}' (not in the commit)"
                                      for f, m in holes))

        ok, detail = check_harness(checkout)
        if not ok:
            failures.append("CONTRACT-flavor / harness failure:\n    " + detail)

        pr_ok, pr_detail = check_pyright(checkout)
        if pr_ok is False:
            failures.append("pyright (first-pass) flagged:\n    " + pr_detail)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)

    if failures:
        sys.stderr.write(
            "\n╔═ PRE-PUSH BLOCKED — this push is NOT self-consistent ═╗\n"
            + "\n".join(failures)
            + "\n\n→ IF YOU ARE AN AGENT: STOP. This is a hard stop, not an error to\n"
              "  work around. Surface this block to the operator and let them decide. Do\n"
              "  not attempt to bypass it.\n"
              "\nThe integrity-hole bug class: this commit does not contain its own\n"
              "dependencies. The correct fix is to make the commit self-contained —\n"
              "commit the missing module, or align the cross-module contract — then\n"
              "re-push. Skipping the check is NOT a fix: `git push --no-verify`\n"
              "disables this gate entirely and is reserved for the operator's deliberate\n"
              "human override. An agent using it defeats the gate's whole purpose.\n"
              "(See CLAUDE.md: pre-push self-consistency gate.)\n"
              "╚════════════════════════════════════════════════════════╝\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_check())
