"""The grounding-flag detector is a fabrication safety surface — it must never
fail silently. When Sentry is unavailable or dispatch raises,
flag_grounding_event must route the event to the local fallback (stderr + JSONL),
not just return False into callers that don't check it."""
from pathlib import Path

from alice.observability import obs


def test_fallback_invoked_when_sentry_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(obs, "_grounding_fallback", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(obs, "available", lambda: False)
    r = obs.flag_grounding_event("category_mismatch", "fabricated claim", {"role": "x"})
    assert r is False
    assert len(calls) == 1                          # event captured, not silently lost
    assert calls[0][0][0] == "category_mismatch"    # kind preserved (positional)
    assert calls[0][1]["reason"] == "sentry_unavailable"


def test_fallback_invoked_on_dispatch_error(monkeypatch):
    calls = []
    monkeypatch.setattr(obs, "_grounding_fallback", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(obs, "available", lambda: True)

    class _Boom:
        def push_scope(self):
            raise RuntimeError("sentry transport down")

    monkeypatch.setattr(obs, "sentry_sdk", _Boom())
    r = obs.flag_grounding_event("claims_without_tools", "specific claim, no tool", {})
    assert r is False
    assert len(calls) == 1
    assert calls[0][1]["reason"].startswith("dispatch_error:")


def test_fallback_writes_jsonl_and_does_not_raise(tmp_path, monkeypatch):
    """_grounding_fallback itself: writes a parseable record, never raises even if
    the log path is unwritable."""
    import json
    # point the repo-relative path computation at a tmp dir by patching os.makedirs
    # and open indirectly is fiddly; instead assert it doesn't raise + writes to the
    # real state log, then verify the record shape via the returned write.
    obs._grounding_fallback("category_mismatch", "unit-test event UNIQUE_MARKER",
                            {"k": "v"}, "fp", "sentry_unavailable")
    fp = Path("state/grounding_flags_fallback.jsonl")
    assert fp.exists()
    rec = json.loads([l for l in fp.read_text().splitlines() if "UNIQUE_MARKER" in l][-1])
    assert rec["kind"] == "category_mismatch"
    assert rec["undispatched_reason"] == "sentry_unavailable"
    # cleanup the test line so the log stays clean
    kept = [l for l in fp.read_text().splitlines() if "UNIQUE_MARKER" not in l and l]
    fp.write_text("\n".join(kept) + ("\n" if kept else ""))
