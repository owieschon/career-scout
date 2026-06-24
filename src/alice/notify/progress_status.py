"""Progress-status UX for long-running Telegram tasks.

Three composing layers — immediate ack, edit-in-place progress, typing
keepalive — with one structural property:

  THE KEEPALIVE STOP MUST FIRE ON BOTH SUCCESS AND ERROR.

The API is an async context manager that puts the stop in a `finally`
block. There is no path where the keepalive lives past the work —
completion, return, exception, cancellation, or timeout, the keepalive is
cancelled. This guards against the "stuck typing…" failure mode where the
indicator shows "working" while the underlying task is dead.

A status indicator whose stop isn't structurally guaranteed is worse than
no indicator, because it deceives. The verification test
(scripts/harness/check_telegram_ux.py) asserts the indicator clears on the
error path, not just the success path.

Scope: this UI is for the working-but-opaque case only. Do not use it to
mask a silent failure (where the handler crashed or dropped the message
entirely). Wrapping a try/except that swallows the exception and shows
"done" anyway masks a real bug with cosmetic status.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from alice import repo_paths


# Default keepalive cadence — Telegram's "typing…" indicator times out
# after ~5s, so we re-fire just inside that window. 3.5s gives margin
# without spamming the API.
_KEEPALIVE_INTERVAL_S = 3.5


class ProgressStatus:
    """One in-flight status indicator for a Telegram chat. Use as an
    `async with`:

        async with ProgressStatus(bot, chat_id, "On it — checking your sheet…") as status:
            result = await long_running_work()
            await status.edit("Got the sheet, composing response…")
 # ... more work ...
 # On exit (success OR exception), the keepalive task is cancelled
 # in the `finally` block — the typing indicator clears, structurally.

    Args:
        bot:        the telegram.Bot instance (from context.bot in handlers)
        chat_id:    int chat id to send the status into
        initial:    initial ack text. If empty, no message is sent — only
                    the typing keepalive runs (useful for very short tasks
                    where an ack message would be more noise than help).
        keepalive_interval_s: seconds between chat_action refreshes. Default
                    3.5s (just inside the ~5s timeout).
    """

    def __init__(self, bot, chat_id: int, initial: str = "",
                 keepalive_interval_s: float = _KEEPALIVE_INTERVAL_S,
                 whimsical: bool = False,
                 whimsical_interval_s: float = 4.5):
        self._bot = bot
        self._chat_id = chat_id
        self._initial = initial
        self._interval = keepalive_interval_s
        self._whimsical = whimsical
        self._whimsical_interval = whimsical_interval_s
        self._message = None         # the status message we sent / are editing
        self._keepalive_task: Optional[asyncio.Task] = None
        self._whimsy_task: Optional[asyncio.Task] = None
        self._done = False           # marked True in __aexit__ regardless of path

    @property
    def message(self):
        """Read-only access to the in-flight status message. After
        __aexit__ this is the LAST edited state (e.g. whatever whimsical
        phrase was up when the work completed). _deliver_response uses
        this to know what message to edit-to-final-response."""
        return self._message

    async def __aenter__(self):
 # Send the initial ack if there is one. Best-effort — if Telegram
 # send fails, we still want the typing keepalive to run (and the
 # work to proceed); we don't make status optional.
        if self._initial:
            try:
                self._message = await self._bot.send_message(
                    chat_id=self._chat_id, text=self._initial,
                )
            except Exception as e:
 # Log but don't propagate — status is a UX nicety, not a
 # gate on the underlying work.
                print(f"[progress_status: initial ack send failed: {e}]")
                self._message = None

 # Start the keepalive task. Stored on self so __aexit__ can cancel
 # it deterministically.
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

 # Whimsical edit loop — periodically rewrites the ack message
 # with activity-aware playful phrases. Only runs if both
 # whimsical=True AND a message was sent (no point editing nothing).
        if self._whimsical and self._message is not None:
            self._whimsy_task = asyncio.create_task(self._whimsy_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
 # STRUCTURAL STOP — runs on both success and error paths. There is
 # no `return False` or conditional that can prevent reaching here:
 # asyncio context managers always invoke __aexit__ when the `async
 # with` block leaves scope, regardless of how it leaves (normal
 # return, raised exception, or task cancellation).
        self._done = True
        try:
 # Cancel BOTH background tasks (keepalive + whimsy) in the
 # finally pattern. If either has already finished, .cancel()
 # is a no-op; we still wait_for to make sure no cancellation
 # is left hanging.
            for task_attr in ("_keepalive_task", "_whimsy_task"):
                task = getattr(self, task_attr, None)
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(task, timeout=1.0)
        finally:
 # Final belt-and-suspenders: nil out the references so a
 # future rogue caller can't poke at a dead task.
            self._keepalive_task = None
            self._whimsy_task = None
 # Re-raise any exception that came through the `with` block — we
 # don't swallow errors. The structural stop is about the keepalive,
 # NOT about masking failures of the underlying work.
        return False

    async def _keepalive_loop(self) -> None:
        """Re-fire the 'typing…' indicator just inside Telegram's ~5s
        timeout, until cancelled. Cancellation comes from __aexit__'s
        `finally` block, so it fires on both success and error.

        Each iteration writes a one-line breadcrumb to
        feedback/progress-status.log so the keepalive's firing can be
        observed server-side rather than relying on Telegram client render
        visibility. The indicator itself is what the user sees; the log
        confirms the mechanism."""
        from datetime import datetime as _dt
        from pathlib import Path as _Path
        log_path = _Path(repo_paths.FEEDBACK / "progress-status.log")
        try:
            iter_n = 0
            while not self._done:
                iter_n += 1
                ts = _dt.now().isoformat(timespec="milliseconds")
                try:
                    result = await self._bot.send_chat_action(
                        chat_id=self._chat_id, action="typing",
                    )
                    line = f"{ts} chat={self._chat_id} iter={iter_n} chat_action=ok result={result}\n"
                except Exception as e:
                    line = f"{ts} chat={self._chat_id} iter={iter_n} chat_action=FAIL err={type(e).__name__}: {e}\n"
 # Best-effort: don't crash the loop if one send fails;
 # try again next tick. But do log so a misconfigured
 # send surfaces.
                    print(f"[progress_status: chat_action failed: {e}]")
                try:
                    with log_path.open("a") as f:
                        f.write(line)
                except Exception:
                    pass
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
 # Expected exit path. Don't re-raise — let the loop unwind cleanly.
            try:
                with log_path.open("a") as f:
                    f.write(f"{_dt.now().isoformat(timespec='milliseconds')} chat={self._chat_id} keepalive_cancelled (structural-stop fired)\n")
            except Exception:
                pass
            return

    async def _whimsy_loop(self) -> None:
        """Periodically rewrite the ack message with whimsical activity-
        aware phrases from whimsy.next_phrase(). Cadence is slower than
        the chat_action keepalive (default 4.5s vs 3.5s) so the visible
        text changes feel deliberate, not jittery.

        Activity awareness comes from llm.py calling whimsy.record_tool()
        after each successful tool, so the next edit pulls from that
        tool's phrase pool. Filler from the general pool when no tool
        has fired yet.

        Any edit_text failure is logged and the loop continues; the
        underlying work is never blocked on a UX detail. Both success and
        failure are logged to feedback/progress-status.log so the phrases
        that actually fired are observable."""
        from alice.llm import whimsy
        from datetime import datetime as _dt
        from pathlib import Path as _Path
        log_path = _Path(repo_paths.FEEDBACK / "progress-status.log")
        try:
            while not self._done:
 # First sleep, THEN edit — so the initial " thinking…"
 # ack stays visible long enough to register before we
 # start rotating.
                await asyncio.sleep(self._whimsical_interval)
                if self._done or self._message is None:
                    return
                ts = _dt.now().isoformat(timespec="milliseconds")
                phrase = whimsy.next_phrase()
                try:
                    await self._message.edit_text(text=phrase)
                    line = f"{ts} chat={self._chat_id} whimsy_edit=ok phrase={phrase!r}\n"
                except Exception as e:
 # Don't crash the loop; common cases: message_not_modified
 # (same text picked twice), rate limit, edit timeout.
                    line = f"{ts} chat={self._chat_id} whimsy_edit=FAIL phrase={phrase!r} err={type(e).__name__}: {e}\n"
                    print(f"[progress_status: whimsy edit failed: {e}]")
                try:
                    with log_path.open("a") as f:
                        f.write(line)
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    async def edit(self, text: str) -> None:
        """Update the status message in place via Telegram's `editMessageText`
        — same message id, updated text — so the chat doesn't accumulate
        status messages. One message, edited through the stages, gives a
        live progress line that doesn't spam the chat.

        No-op if no initial message was sent (i.e., the indicator runs
        keepalive-only) or if the edit fails (best-effort — we don't break
        the work for a UX detail)."""
        if self._message is None:
            return
        try:
            await self._message.edit_text(text=text)
        except Exception as e:
            print(f"[progress_status: edit_text failed: {e}]")

    async def replace_with(self, text: str) -> None:
        """Replace the status message's content with the final response.
        Same as `edit` but semantically intended as the terminal update —
        the work is done, this is what to leave in the chat. If no
        initial message was sent, this falls back to sending a new
        message (so the response is delivered either way)."""
        if self._message is None:
            try:
                self._message = await self._bot.send_message(
                    chat_id=self._chat_id, text=text,
                )
            except Exception as e:
                print(f"[progress_status: terminal send failed: {e}]")
            return
        await self.edit(text)


# ─── convenience for handlers that want a simpler API ────────────────────────

@contextlib.asynccontextmanager
async def progress_indicator(bot, chat_id: int, initial: str = ""):
    """Functional-style wrapper for handlers that prefer `async with` over
    constructing the class explicitly. Same structural-stop guarantee."""
    status = ProgressStatus(bot, chat_id, initial=initial)
    async with status as s:
        yield s
