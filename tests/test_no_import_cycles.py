"""Architecture guard: the alice package has no import cycles.

A real package boundary means the module dependency graph is acyclic. This test
parses every module's imports (without executing them), builds the internal
dependency graph, and asserts:

  1. The top-level (load-time) graph has no strongly-connected component larger
     than one module — i.e. no circular imports.
  2. No function-local `from alice...` import is a hidden cycle-breaker — i.e.
     none of them would create a cycle if hoisted to module top level. (If one
     did, the acyclic top-level graph would be an illusion propped up by a
     deferred import.) Function-local imports remain allowed, but only as
     deliberate lazy-loads, never to dodge a cycle.

If this fails, a new edge introduced a cycle; fix the dependency direction
(extract the shared piece into a lower-level module) rather than papering over it
with a function-local import.
"""
import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "src")
PKG = "alice"


def _build_graph():
    top, local, mods = {}, {}, set()
    base = os.path.join(ROOT, PKG)
    for dp, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            m = os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")
            mods.add(m)
            top.setdefault(m, set()); local.setdefault(m, set())
            for node in ast.walk(ast.parse(open(path).read(), path)):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                targets = []
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PKG):
                    targets.append(node.module)
                    targets += [f"{node.module}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Import):
                    targets += [a.name for a in node.names if a.name.startswith(PKG)]
                bucket = top if node.col_offset == 0 else local
                bucket[m].update(targets)
    return top, local, mods


def _normalize(t, mods):
    return t if t in mods else ".".join(t.split(".")[:-1])


def _edges(raw, mods):
    return {m: {n for t in deps if (n := _normalize(t, mods)) in mods and n != m}
            for m, deps in raw.items()}


def _sccs(edges, mods):
    index, low, onstack, stack, out, c = {}, {}, {}, [], [], [0]
    def strong(v):
        index[v] = low[v] = c[0]; c[0] += 1; stack.append(v); onstack[v] = True
        for w in edges.get(v, ()):
            if w not in index:
                strong(w); low[v] = min(low[v], low[w])
            elif onstack.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); onstack[w] = False; comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))
    for v in mods:
        if v not in index:
            strong(v)
    return out


def _reaches(src, dst, edges, seen=None):
    seen = seen or set()
    if src in seen:
        return False
    seen.add(src)
    return any(w == dst or _reaches(w, dst, edges, seen) for w in edges.get(src, ()))


def test_no_top_level_import_cycles():
    top, _, mods = _build_graph()
    cycles = _sccs(_edges(top, mods), mods)
    assert not cycles, f"top-level import cycles found: {cycles}"


def test_no_function_local_import_is_a_hidden_cycle_breaker():
    top, local, mods = _build_graph()
    te = _edges(top, mods)
    offenders = []
    for m, deps in local.items():
        for t in deps:
            n = _normalize(t, mods)
            if n in mods and n != m and _reaches(n, m, te):
                offenders.append(f"{m} <-> {n}")
    assert not offenders, (
        "function-local imports that hide a cycle (hoisting would loop): " + "; ".join(offenders)
    )
