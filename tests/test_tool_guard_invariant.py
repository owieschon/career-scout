"""The mutating-tool guard invariant, in the CI path.

A mutating tool must be refused at registration time if it has no guard. The
invariant fires at import in src/alice/tools.py; this test guards it against
regression (the original assertion lived only in the manual harness)."""
import pytest

from alice import tools


def test_register_refuses_unguarded_mutating_tool():
    with pytest.raises(RuntimeError):
        @tools.register_tool(name="_test_unguarded_mutator", description="x",
                             input_schema={"type": "object"}, mutating=True, guard=None)
        def _bad(_):  # pragma: no cover - registration raises before use
            return None


def test_register_allows_guarded_mutating_tool():
    def _guard(_obj):
        return None

    @tools.register_tool(name="_test_guarded_mutator", description="x",
                         input_schema={"type": "object"}, mutating=True, guard=_guard)
    def _ok(_):
        return "ok"
    assert any(t["name"] == "_test_guarded_mutator" for t in tools.TOOLS_REGISTRY)
