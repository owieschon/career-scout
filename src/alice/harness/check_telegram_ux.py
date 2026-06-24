"""Telegram UX failing-direction tests. Two structural guarantees must be
provable:

  1. TEXT NEVER BLOCKED. When a pending button confirmation exists, typed
     input must still resolve (and pass through to Alice). Buttons are a
     fast-path, not a gate. Test: pending confirmation + typed 'yes'
     resolves it via try_resolve_by_text.

  2. KEEPALIVE STOP IS STRUCTURAL. The progress-status typing indicator's
     stop must fire on the ERROR path, not just success. Test: wrap a
     coroutine that raises mid-flight in `async with ProgressStatus(...)`,
     and assert the keepalive task is cancelled when the exception leaves
     the block. A success-only test does NOT prove the guard — the stuck-
     indicator bug lives on the exception path.

Run: python3 scripts/harness/check_telegram_ux.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

from alice.notify import button_ux       # noqa: E402
from alice.notify import progress_status # noqa: E402


# ─── Test 1: text never blocked (escape hatch) ───────────────────────────────

def test_text_resolves_pending_confirmation() -> bool:
    """The non-negotiable: typed text must resolve a pending confirmation.
    The buttons are an accelerator, not a gate. If the user types 'yes', that
    has to work even if a [Yes][No] keyboard is showing."""
    print("\n[Test 1] type-while-buttons-show: typed text resolves pending confirmation")

    # Start clean
    for c in button_ux.get_pending():
        button_ux._do_resolve(c["conf_id"], c["options"][0]["code"], source="test_cleanup")

    # Register a pending yes/no — this is the state the bot would be in
    # right after Alice called ask_confirmation and the keyboard was sent.
    reg = button_ux.register(
        question="Should I proceed with prep for both?",
        options=button_ux.YES_NO,
    )
    pending = button_ux.get_pending()
    assert len(pending) == 1, f"expected 1 pending, got {len(pending)}"
    assert pending[0]["conf_id"] == reg["conf_id"], "wrong conf_id staged"
    print(f"  → pending confirmation registered: {reg['conf_id']}")

    # Simulate the user typing 'yes' instead of tapping. The handler's escape-
    # hatch (try_resolve_by_text) must resolve it.
    resolved = button_ux.try_resolve_by_text("yes")
    if resolved is None:
        print("  FAIL — typed 'yes' did NOT resolve pending confirmation. Buttons would block text.")
        return False
    if resolved.get("resolved_choice") != "yes":
        print(f"  FAIL — resolved to {resolved.get('resolved_choice')!r}, expected 'yes'")
        return False
    if resolved.get("resolved_via") != "text_match_exact":
        print(f"  FAIL — resolved_via={resolved.get('resolved_via')!r}, expected 'text_match_exact'")
        return False
    print(f"  → typed 'yes' resolved via={resolved['resolved_via']}, choice={resolved['resolved_choice']}")

    # Pending should now be empty
    remaining = button_ux.get_pending()
    if remaining:
        print(f"  FAIL — confirmation should be resolved; still {len(remaining)} pending")
        return False
    print("  → pending list emptied after resolution")

    # Variation: typed text that doesn't match an option should return None
    # (typed text falls through to Alice normally — not consumed)
    reg2 = button_ux.register(
        question="A or B?",
        options=[{"code": "a", "label": "A"}, {"code": "b", "label": "B"}],
    )
    not_resolved = button_ux.try_resolve_by_text("tell me about ATC role")
    if not_resolved is not None:
        print(f"  FAIL — unrelated text resolved a confirmation; that means text-content is being consumed inappropriately")
        return False
    print("  → unrelated text returns None (handler treats as regular message; pending stays open)")
    # Clean up the AB confirmation
    button_ux._do_resolve(reg2["conf_id"], "a", source="test_cleanup")

    # Variation: numeric positional match
    reg3 = button_ux.register(
        question="Pick one",
        options=[
            {"code": "alpha", "label": "Alpha"},
            {"code": "beta",  "label": "Beta"},
            {"code": "gamma", "label": "Gamma"},
        ],
    )
    pos = button_ux.try_resolve_by_text("2")
    if pos is None or pos.get("resolved_choice") != "beta":
        print(f"  FAIL — positional match '2' should resolve to 'beta' (index 1); got {pos.get('resolved_choice') if pos else None}")
        return False
    print(f"  → positional match '2' → 'beta' via={pos['resolved_via']}")

    print("  PASS — text is never blocked; all three resolution paths (exact, positional, no-match-passthrough) work")
    return True


# ─── Test 2: keepalive stops on ERROR path (structural guard) ────────────────

class _FakeBot:
    """Minimal stand-in for telegram.Bot. Records chat_action and message
    calls so we can assert behavior without real network I/O."""
    def __init__(self):
        self.chat_actions = []          # list of (chat_id, action)
        self.messages_sent = []         # list of (chat_id, text)
        self.edits = []
        self.chat_action_should_raise = False

    async def send_message(self, chat_id, text, **kwargs):
        self.messages_sent.append((chat_id, text))
        class _Msg:
            def __init__(self, mid): self.message_id = mid
            async def edit_text(self, text=None, **kw): pass
        return _Msg(99999)

    async def send_chat_action(self, chat_id, action):
        self.chat_actions.append((chat_id, action))
        if self.chat_action_should_raise:
            raise RuntimeError("simulated chat_action failure")

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


async def _exercise_progress_error_path() -> bool:
    """The structural test. Wrap a coroutine that RAISES in `async with
    ProgressStatus(...)`. Capture the keepalive task; assert it is
    cancelled when the exception leaves the block.

    If the keepalive task is still running after the async-with exits on
    the error path, the typing indicator would be stuck — the failure mode
    this guard exists to prevent. The structural-stop guarantee says
    __aexit__'s `finally` cancels the task regardless of how the block
    exited.
    """
    bot = _FakeBot()
    captured_task = {"task": None}

    # Patch the keepalive cadence to fast so the loop has time to fire
    # at least once before we raise.
    class _FastStatus(progress_status.ProgressStatus):
        def __init__(self, *a, **kw):
            kw["keepalive_interval_s"] = 0.05
            super().__init__(*a, **kw)

    raised = False
    try:
        async with _FastStatus(bot, chat_id=42, initial="") as s:
            captured_task["task"] = s._keepalive_task
            # Let the keepalive fire at least once before we raise
            await asyncio.sleep(0.15)
            # Raise mid-flight
            raise RuntimeError("simulated work failure")
    except RuntimeError as e:
        if str(e) == "simulated work failure":
            raised = True
        else:
            print(f"  FAIL — unexpected exception: {e!r}")
            return False

    if not raised:
        print("  FAIL — exception did not propagate out of async-with; progress is swallowing errors")
        return False

    task = captured_task["task"]
    if task is None:
        print("  FAIL — keepalive task was never captured")
        return False

    # The structural-stop assertion. After the async-with exited on the
    # exception path, the keepalive task must have been cancelled (or at
    # minimum stopped). If it's still running, the indicator would be
    # stuck.
    if not task.done():
        # Give the cancellation a moment to settle (it should already)
        await asyncio.sleep(0.05)

    if not task.done():
        print(f"  FAIL — keepalive task is STILL RUNNING after error-path exit. Stuck-indicator bug present.")
        return False

    # We expect the task to be cancelled OR to have completed cleanly.
    # Either is fine — both mean the indicator stopped.
    if task.cancelled():
        print("  → keepalive task cancelled cleanly on error path (structural-stop guarantee held)")
    else:
        # Could be cancelled-then-finished, which task.done() returns True for
        # without task.cancelled() necessarily being True if the loop
        # exited gracefully on its CancelledError catch.
        print("  → keepalive task done (either cancelled or gracefully exited via CancelledError)")

    return True


def test_keepalive_stops_on_error() -> bool:
    print("\n[Test 2] error-midway-clears: ProgressStatus keepalive stops on error path")
    return asyncio.run(_exercise_progress_error_path())


# ─── Bonus test: keepalive also stops on SUCCESS (for completeness) ─────────

async def _exercise_progress_success_path() -> bool:
    bot = _FakeBot()
    captured_task = {"task": None}

    class _FastStatus(progress_status.ProgressStatus):
        def __init__(self, *a, **kw):
            kw["keepalive_interval_s"] = 0.05
            super().__init__(*a, **kw)

    async with _FastStatus(bot, chat_id=42, initial="On it...") as s:
        captured_task["task"] = s._keepalive_task
        # Let keepalive fire
        await asyncio.sleep(0.15)
        # Normal completion (no raise)

    task = captured_task["task"]
    if task is None:
        print("  FAIL — keepalive task was never captured (success path)")
        return False
    if not task.done():
        await asyncio.sleep(0.05)
    if not task.done():
        print(f"  FAIL — keepalive still running after success-path exit")
        return False
    print(f"  → success path: keepalive stopped. chat_actions fired: {len(bot.chat_actions)} (expected ≥1)")
    if len(bot.chat_actions) < 1:
        print("  WARN — keepalive never sent chat_action; cadence too slow for test window")
    return True


def test_keepalive_stops_on_success() -> bool:
    print("\n[Test 3 — bonus] keepalive stops on success path (completeness check)")
    return asyncio.run(_exercise_progress_success_path())


def main() -> int:
    print("=== Telegram UX failing-direction tests ===")
    print("(text-not-blocked + error-clears)")

    t1 = test_text_resolves_pending_confirmation()
    t2 = test_keepalive_stops_on_error()
    t3 = test_keepalive_stops_on_success()

    print(f"\n=== Summary ===")
    print(f"  PASS  text-while-buttons-show: {t1}")
    print(f"  PASS  error-midway-clears (structural-stop): {t2}")
    print(f"  PASS  success-path keepalive-clear (completeness): {t3}")
    if t1 and t2 and t3:
        print("\nBoth non-negotiables passed via failing-direction tests. "
              "Live verification (operator tapping/typing through actual bot) is "
              "the next layer; this proves the mechanism.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
