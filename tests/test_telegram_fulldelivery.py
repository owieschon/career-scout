"""_send_alice_reply (the secondary reply path) must deliver a long reply in
full via _split_for_telegram, not drop it on the 4096-char overflow."""
import asyncio
from pathlib import Path

from alice.notify import telegram_bot as tb


class _Msg:
    message_id = 42


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return _Msg()


def test_long_reply_chunked_in_full(monkeypatch):
    monkeypatch.setattr(tb, "_save_history", lambda *a, **k: None)
    bot = _Bot()
    long = ("This is a paragraph of the reply.\n\n" * 400)  # ~13k chars > limit
    asyncio.run(tb._send_alice_reply(bot, 1, long, {}))
    assert len(bot.sent) >= 2                                   # actually chunked
    assert all(len(t) <= tb._TELEGRAM_MSG_LIMIT for t, _ in bot.sent)
    # full delivery: combined content is preserved (chunks are separate messages,
    # so join with a space to restore the boundaries the chunker trimmed)
    combined = " ".join(t for t, _ in bot.sent)
    assert len(combined.split()) == len(long.split())          # no words lost


def test_short_reply_single_message(monkeypatch):
    monkeypatch.setattr(tb, "_save_history", lambda *a, **k: None)
    bot = _Bot()
    asyncio.run(tb._send_alice_reply(bot, 1, "short reply", {}))
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == "short reply"
