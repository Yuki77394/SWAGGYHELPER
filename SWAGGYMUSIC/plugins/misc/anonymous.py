"""Channel anonymous/Send-As post filter.

/setanonymous -> enter channel ID -> ENABLE/DISABLE buttons.
When enabled, only posts with a Telegram-exposed personal-account sender
are kept. Channel/chat identity posts and posts with unknown sender metadata
are deleted; service messages are left alone. Telegram does not expose the
real admin ID behind channel-identity posts, so this filter cannot identify
the human behind them.

Policy and /setgap coordination: SWAGGYMUSIC/utils/anonymous_policy.py.
"""

import asyncio
import time
from contextlib import suppress

from pyrogram import filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils import anonymous_policy
from SWAGGYMUSIC.utils.database import queue_deletion_job, settings_db
from SWAGGYMUSIC.utils.permissions import verify_channel_admin

log = LOGGER(__name__)
PREFIX = "setanonymous"
SESSIONS = {}
LOCK = asyncio.Lock()


def _menu(channel_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 ENABLE", callback_data=f"{PREFIX}:enable:{channel_id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton("🔴 DISABLE", callback_data=f"{PREFIX}:disable:{channel_id}", style=ButtonStyle.DANGER),
        ],
        [InlineKeyboardButton("❌ CANCEL", callback_data=f"{PREFIX}:cancel", style=ButtonStyle.DANGER)],
    ])


def _cancel_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data=f"{PREFIX}:cancel", style=ButtonStyle.DANGER)]])


@app.on_message(filters.command(["setanonymous"]) & filters.private)
async def setanonymous_start(_, message: Message):
    if not message.from_user:
        return
    async with LOCK:
        SESSIONS[message.from_user.id] = {"step": "channel_id"}
    await message.reply_text(
        "🛡️ <b>ANONYMOUS POST PROTECTION</b>\n\n"
        "Please enter the <b>Channel ID</b> you want to manage.\n\n"
        "Example: <code>-100123456789</code>",
        reply_markup=_cancel_menu(),
    )


@app.on_callback_query(filters.regex(r"^setanonymous:"))
async def setanonymous_callback(_, query: CallbackQuery):
    if not query.from_user:
        return await query.answer("Not available.", show_alert=True)
    user_id = query.from_user.id
    data = query.data or ""
    if data == f"{PREFIX}:cancel":
        SESSIONS.pop(user_id, None)
        await query.answer("Cancelled")
        with suppress(Exception):
            await query.message.edit_text("❌ Configuration cancelled.")
        return

    parts = data.split(":")
    if len(parts) != 3 or parts[1] not in {"enable", "disable"}:
        return await query.answer("Invalid option.", show_alert=True)
    try:
        channel_id = int(parts[2])
    except ValueError:
        return await query.answer("Invalid Channel ID.", show_alert=True)

    session = SESSIONS.get(user_id)
    if not session or session.get("channel_id") != channel_id:
        return await query.answer("Setup expired. Use /setanonymous again.", show_alert=True)

    ok, reason = await verify_channel_admin(channel_id, user_id)
    if not ok:
        SESSIONS.pop(user_id, None)
        await query.answer("You cannot manage this channel.", show_alert=True)
        with suppress(Exception):
            await query.message.edit_text(reason)
        return

    enabled = parts[1] == "enable"
    try:
        await settings_db.update_one(
            {"chat_id": channel_id},
            {"$set": {"chat_id": channel_id, "anonymous_filter_enabled": enabled}},
            upsert=True,
        )
        chat = await app.get_chat(channel_id)
        status = "ENABLED 🟢" if enabled else "DISABLED 🔴"
        await query.answer("Saved")
        with suppress(Exception):
            await query.message.edit_text(
                f"🛡️ <b>ANONYMOUS POST PROTECTION {status}</b>\n\n"
                f"📌 <b>Channel:</b> {chat.title or 'Channel'}\n"
                f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                + ("Only posts whose personal account Telegram exposes are kept. Channel/chat identity posts "
                   "and posts with unknown sender information are deleted.\n\n" if enabled else "Protection is now turned off.\n\n")
                + "<i>Telegram may not reveal the real admin behind a channel-identity post; this filter cannot recover that identity.</i>"
            )
        SESSIONS.pop(user_id, None)
    except Exception as exc:
        log.warning("setanonymous update failed for %s: %s", channel_id, exc)
        await query.answer("Could not update settings. Check bot permissions.", show_alert=True)


@app.on_message(
    filters.private & filters.text & ~filters.service & ~filters.command(["setanonymous", "cancel"]),
    group=-10,
)
async def setanonymous_channel_input(_, message: Message):
    if not message.from_user:
        return
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("step") != "channel_id":
        return
    raw = (message.text or "").strip()
    try:
        channel_id = int(raw)
    except ValueError:
        return await message.reply_text("❌ Invalid Channel ID. Send a numeric ID, e.g. <code>-100123456789</code>.")

    ok, reason = await verify_channel_admin(channel_id, message.from_user.id)
    if not ok:
        SESSIONS.pop(message.from_user.id, None)
        return await message.reply_text(reason)

    try:
        chat = await app.get_chat(channel_id)
        member = await app.get_chat_member(channel_id, app.id)
        if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            raise RuntimeError("Bot is not an administrator")
        privileges = getattr(member, "privileges", None)
        if member.status != ChatMemberStatus.OWNER and not (privileges and privileges.can_delete_messages):
            SESSIONS.pop(message.from_user.id, None)
            return await message.reply_text("❌ I must be an admin with <b>Delete Messages</b> permission in that channel.")
    except Exception:
        SESSIONS.pop(message.from_user.id, None)
        return await message.reply_text("❌ I cannot access that channel or I lack admin/Delete Messages permission. Add me as an admin and try again.")

    session.update({"step": "action", "channel_id": channel_id})
    current = await settings_db.find_one({"chat_id": channel_id})
    state = "ON 🟢" if current and current.get("anonymous_filter_enabled") else "OFF 🔴"
    await message.reply_text(
        f"📌 <b>Channel:</b> {chat.title or 'Channel'}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n"
        f"🛡️ <b>Current status:</b> {state}\n\nChoose an action:",
        reply_markup=_menu(channel_id),
    )


def _post_time(message: Message) -> int:
    with suppress(AttributeError, OSError, ValueError):
        return int(message.date.timestamp())
    return int(time.time())


@app.on_message(filters.channel & filters.incoming, group=-30)
async def anonymous_post_filter(_, message: Message):
    """Reject chat-identity posts while /setanonymous is enabled.

    Policy (anonymous_policy.should_reject_anonymous_post):
      * posts whose personal account Telegram exposes (raw ``from_id`` is a
        user) are allowed;
      * posts sent with this channel's or another chat's identity are rejected;
      * unknown sender metadata is rejected (fail closed); service messages
        are exempt. An ``author_signature`` is free text, not an account.
    Telegram does not reveal the human admin behind a chat-identity post, so
    this filter never claims to identify one: it logs only the sender chat
    Telegram reported.

    Runs in group -30, before the /setgap engine (group 0) handles the same
    update. The verdict is recorded BEFORE any Telegram call, so a rejected
    post can never become a /setgap reference — even if deleting it fails.
    Propagation is not stopped: the channel /setgap and /setdelay command
    handlers still process command posts.
    """
    try:
        if message.chat.type != ChatType.CHANNEL or getattr(message, "outgoing", False):
            return
        if getattr(message, "edit_date", None) is not None:
            return
        if not anonymous_policy.should_reject_anonymous_post(message):
            return
        setting = await settings_db.find_one({"chat_id": message.chat.id}, {"anonymous_filter_enabled": 1})
        if not setting or not setting.get("anonymous_filter_enabled"):
            return

        # Verdict first: in-process for the later handler groups of this
        # update, then in MongoDB for the /setgap engine's atomic accept.
        anonymous_policy.mark_rejected(message.chat.id, message.id)
        try:
            await anonymous_policy.record_rejection(message.chat.id, message.id)
        except Exception as exc:
            log.warning(
                "anonymous verdict not persisted chat=%s message=%s: %s: %s",
                message.chat.id, message.id, type(exc).__name__, exc,
            )

        member = await app.get_chat_member(message.chat.id, app.id)
        privileges = getattr(member, "privileges", None)
        can_delete = member.status == ChatMemberStatus.OWNER or bool(
            member.status == ChatMemberStatus.ADMINISTRATOR
            and privileges and privileges.can_delete_messages
        )
        if not can_delete:
            log.warning(
                "anonymous post rejected but not deleted (no Delete Messages right) chat=%s message=%s",
                message.chat.id, message.id,
            )
            return
        try:
            await message.delete()
        except FloodWait as exc:
            # Telegram refused for now: queue a persistent retry for the
            # deletion worker instead of leaving the rejected post up.
            wait = min(max(int(getattr(exc, "value", 0) or 0), 1), 3600)
            await queue_deletion_job(
                message.chat.id, message.id,
                delete_at=int(time.time()) + wait,
                original_post_time=_post_time(message),
                reason="ANON_REJECT_RETRY",
            )
            log.warning(
                "anonymous post delete FloodWait chat=%s message=%s wait=%ss; retry queued",
                message.chat.id, message.id, wait,
            )
            return
        log.info(
            "anonymous post deleted chat=%s message=%s identity=%s sender_chat=%s "
            "(chat identity; Telegram does not reveal which admin sent it)",
            message.chat.id, message.id,
            anonymous_policy.sender_identity(message),
            anonymous_policy.sender_chat_id(message),
        )
    except RPCError as exc:
        log.warning("anonymous filter Telegram error chat=%s message=%s: %s", message.chat.id, message.id, exc)
    except Exception as exc:
        log.exception("anonymous filter failed chat=%s message=%s: %s", getattr(message.chat, "id", None), getattr(message, "id", None), exc)
