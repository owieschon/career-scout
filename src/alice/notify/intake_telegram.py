"""Telegram handlers for profile intake.

Three handlers, registered alongside the existing ones in telegram_bot.main():
  - document_intake_handler  (filters.Document.ALL) — a resume upload
  - voice_intake_handler      (filters.VOICE)        — a voice note
  - profile_confirm_handler   (CallbackQueryHandler, pattern ^pf:) — the
        confirm-then-commit button taps for a pending profile draft

They delegate all logic to intake.py / profile_store; nothing here scores or
sources. The confirm gate is enforced in profile_store, so even a bug here
cannot let the engine run on an unconfirmed profile.

The callback prefix is `pf:` — distinct from the existing `conf:` confirmation
machinery, so this composes without touching it.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from alice.persistence import intake
from alice import jobcfg

_CONFIRM_PREFIX = "pf:"


def _allowed_chat_id() -> int:
    return int(jobcfg.load().get("TELEGRAM_CHAT_ID", "0"))


def _user_id_for(chat_id: int) -> str:
    """The profile owner. ALICE_USER_ID when set (clone-ready); else the chat id
    so a single-user operator still gets a stable key."""
    return jobcfg.load().get("ALICE_USER_ID") or str(chat_id)


def _confirm_keyboard(user_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Looks right — lock it in", callback_data=f"{_CONFIRM_PREFIX}commit:{user_id}"),
        InlineKeyboardButton("Start over", callback_data=f"{_CONFIRM_PREFIX}cancel:{user_id}"),
    ]])


def parse_confirm_callback(callback_data: str) -> tuple[str, str] | None:
    """Parse `pf:<action>:<user_id>` -> (action, user_id), or None if not ours /
    malformed. Pure (unit-tested) so the handler stays a thin shell."""
    if not callback_data or not callback_data.startswith(_CONFIRM_PREFIX):
        return None
    rest = callback_data[len(_CONFIRM_PREFIX):]
    parts = rest.split(":", 1)
    if len(parts) != 2 or parts[0] not in ("commit", "cancel") or not parts[1]:
        return None
    return parts[0], parts[1]


async def _run_intake_and_reply(update: Update, *, resume_path=None, voice_path=None) -> None:
    chat_id = update.effective_chat.id
    user_id = _user_id_for(chat_id)
    msg = update.message
    try:
 # The intake (parse + LLM extraction + optional transcription) is
 # blocking; run it off the event loop.
        out = await asyncio.to_thread(
            intake.run_intake, user_id,
            resume_path=resume_path, voice_path=voice_path,
        )
    except intake.IntakeError as e:
 # IntakeError messages are written in external voice already.
        await msg.reply_text(str(e))
        return
    except Exception as e:
        await msg.reply_text(f"Something went wrong reading that: {e}")
        return

    await msg.reply_text(out["confirm_text"], reply_markup=_confirm_keyboard(user_id))


async def document_intake_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != _allowed_chat_id():
        return
    doc = update.message.document if update.message else None
    if doc is None:
        return
    suffix = Path(doc.file_name or "resume").suffix or ".bin"
    tg_file = await doc.get_file()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)
    try:
        await _run_intake_and_reply(update, resume_path=tmp_path)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


async def voice_intake_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != _allowed_chat_id():
        return
    voice = update.message.voice if update.message else None
    if voice is None:
        return
    tg_file = await voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)
    try:
        await _run_intake_and_reply(update, voice_path=tmp_path)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


async def profile_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != _allowed_chat_id():
        try:
            await query.answer("Not allowed")
        except Exception:
            pass
        return

    parsed = parse_confirm_callback(query.data or "")
    if parsed is None:
        try:
            await query.answer()
        except Exception:
            pass
        return
    action, user_id = parsed

    if action == "commit":
        try:
            prof = await asyncio.to_thread(intake.confirm_intake, user_id)
        except Exception as e:
            await query.answer("Couldn't confirm")
            await query.message.reply_text(f"I couldn't lock that in: {e}")
            return
        await query.answer("Locked in")
        name = prof.identity.name or "your profile"
        await query.edit_message_text(
            f"Locked in. {name}'s profile is set and I'll source and score against it from here."
        )
 # Auto-derive track-tailored resume variants from the uploaded resume
 # (post-confirm, so we never spend tokens on an unconfirmed profile).
 # Best-effort: a failure here never undoes the confirmation.
        try:
            variants = await asyncio.to_thread(intake.derive_variants_for, user_id)
        except Exception:
            variants = {}
        if variants:
            ready = [v for v in variants.values() if v.get("markdown") and not v.get("thin")]
            thin = [v for v in variants.values() if v.get("thin")]
            bits = []
            if ready:
                bits.append(
                    "I tailored your resume for "
                    + ", ".join(intake_track_display(v["track"]) for v in ready) + "."
                )
            if thin:
                bits.append(
                    "Thinner on "
                    + ", ".join(intake_track_display(v["track"]) for v in thin)
                    + " — not much in the resume to stand on there."
                )
            if bits:
                await query.message.reply_text(" ".join(bits))
    else:  # cancel
        await asyncio.to_thread(intake.cancel_intake, user_id)
        await query.answer("Cleared")
        await query.edit_message_text(
            "Cleared that draft. Send your resume again, or just tell me about your background and I'll rebuild it."
        )


def intake_track_display(track_key: str) -> str:
    from alice.persistence import generate_resume_variants
    return generate_resume_variants.TRACKS.get(track_key, {}).get("display", track_key)
