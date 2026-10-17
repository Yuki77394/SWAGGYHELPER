"""Channel anonymous/Send-As post filter.

/setanonymous -> enter channel ID -> ENABLE/DISABLE buttons.
When enabled, posts whose sender identity is a chat/channel rather than an
identifiable user are deleted. Telegram does not expose the real admin ID
for these posts, so this filter cannot identify the human behind them.
"""

import asyncio
from contextlib import suppress

from pyrogram import filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils.database import settings_db
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
                + ("Posts without an identifiable personal sender will be deleted when Telegram exposes them as chat/channel-sent posts.\n\n" if enabled else "Protection is now turned off.\n\n")
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


@app.on_message(filters.channel & filters.incoming, group=-30)
async def anonymous_post_filter(_, message: Message):
    """Delete channel-identity posts that have no visible author signature.

    Telegram generally does not expose the human admin who published a channel
    post. This rule therefore checks the visible author signature, not the real
    admin identity. A channel post with no signature is deleted when sender_chat
    is present and from_user is absent. Posts with an author_signature are kept.
    """
    try:
        if message.chat.type != ChatType.CHANNEL or getattr(message, "outgoing", False):
            return
        if getattr(message, "edit_date", None) is not None:
            return
        setting = await settings_db.find_one({"chat_id": message.chat.id}, {"anonymous_filter_enabled": 1})
        if not setting or not setting.get("anonymous_filter_enabled"):
            return
        sender_chat = getattr(message, "sender_chat", None)
        sender_user = getattr(message, "from_user", None)
        # Personal-user posts are allowed. For channel-identity posts, keep
        # only those with a visible author signature; missing signatures are
        # the condition this feature is designed to remove.
        author_signature = (getattr(message, "author_signature", None) or "").strip()
        if sender_chat is not None and sender_user is None and not author_signature:
            member = await app.get_chat_member(message.chat.id, app.id)
            privileges = getattr(member, "privileges", None)
            can_delete = member.status == ChatMemberStatus.OWNER or bool(
                member.status == ChatMemberStatus.ADMINISTRATOR
                and privileges and privileges.can_delete_messages
            )
            if can_delete:
                await message.delete()
                log.info("anonymous post deleted chat=%s message=%s sender_chat=%s", message.chat.id, message.id, getattr(sender_chat, "id", None))
    except RPCError as exc:
        log.warning("anonymous filter Telegram error chat=%s message=%s: %s", message.chat.id, message.id, exc)
    except Exception as exc:
        log.exception("anonymous filter failed chat=%s message=%s: %s", getattr(message.chat, "id", None), getattr(message, "id", None), exc)
