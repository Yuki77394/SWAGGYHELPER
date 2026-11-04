"""Channel anonymous/Send-As post filter.

/setanonymous -> enter channel ID -> ENABLE/DISABLE buttons.
When enabled, posts Telegram attributes to an admin's personal account are
kept: posts sent as the admin's profile (Show Authors' Profiles) and posts
Telegram signs with the admin's name (Sign Messages). Anonymous posts —
published as the channel without a signature, or sent with another
channel's identity — are deleted; service messages are left alone.
Telegram does not expose the admin behind a signed or channel-identity
post, and this filter never tries to identify them.

Enabling is refused while the channel's Sign Messages setting is off:
Telegram then publishes EVERY post anonymously, so the filter would delete
all of them, the admins' own posts included.

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


# What the protection does in each of Telegram's channel posting modes.
_MODE_LINES = {
    anonymous_policy.MODE_ANONYMOUS: (
        "✍️ <b>Sign Messages:</b> OFF — Telegram publishes every post as the channel "
        "and hides who wrote it."
    ),
    anonymous_policy.MODE_SIGNATURES: (
        "✍️ <b>Sign Messages:</b> ON — posts signed with an admin's name are kept; "
        "unsigned posts are deleted."
    ),
    anonymous_policy.MODE_PROFILES: (
        "✍️ <b>Sign Messages + Authors' Profiles:</b> ON — posts sent as an admin's own "
        "profile are kept; posts sent as the channel or as another channel are deleted."
    ),
}
_SIGN_MESSAGES_OFF = (
    "⚠️ <b>Sign Messages is OFF in this channel.</b>\n\n"
    "Telegram then publishes every post as the channel and hides who wrote it, so the "
    "bot cannot tell your own posts from anonymous ones — protection would delete "
    "<b>every post</b>, yours included.\n\n"
    "Turn on <b>Channel settings → Sign Messages</b> (optionally <b>Show Authors' "
    "Profiles</b>), then enable protection again."
)

# Channels already warned (in this process) that Sign Messages is off.
_SIGN_OFF_WARNED = set()


def _warn_sign_messages_off(chat_id: int) -> None:
    if chat_id in _SIGN_OFF_WARNED:
        return
    _SIGN_OFF_WARNED.add(chat_id)
    log.warning(
        "anonymous protection: Sign Messages is OFF in channel %s — Telegram publishes "
        "every post there as the channel and hides who wrote it, so EVERY post is deleted "
        "(admins' own posts included). Turn on Sign Messages (optionally Show Authors' "
        "Profiles) or disable /setanonymous for this channel.",
        chat_id,
    )


async def _channel_and_mode(channel_id: int):
    """(chat, posting mode) — (None, None) if the channel cannot be read."""
    try:
        chat = await app.get_chat(channel_id)
    except Exception as exc:
        log.warning("setanonymous could not read channel %s: %s: %s",
                    channel_id, type(exc).__name__, exc)
        return None, None
    return chat, anonymous_policy.channel_posting_mode(chat)


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
    chat, mode = await _channel_and_mode(channel_id)
    if enabled and mode == anonymous_policy.MODE_ANONYMOUS:
        # Every post would be anonymous, so every post would be deleted.
        SESSIONS.pop(user_id, None)
        await query.answer("Turn on Sign Messages in the channel first.", show_alert=True)
        with suppress(Exception):
            await query.message.edit_text(
                f"🛡️ <b>ANONYMOUS POST PROTECTION NOT ENABLED</b>\n\n"
                f"📌 <b>Channel:</b> {chat.title or 'Channel'}\n"
                f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                + _SIGN_MESSAGES_OFF
            )
        log.info("setanonymous enable refused for %s: Sign Messages is off", channel_id)
        return
    try:
        await settings_db.update_one(
            {"chat_id": channel_id},
            {"$set": {"chat_id": channel_id, "anonymous_filter_enabled": enabled}},
            upsert=True,
        )
        if chat is None:
            chat = await app.get_chat(channel_id)
        status = "ENABLED 🟢" if enabled else "DISABLED 🔴"
        await query.answer("Saved")
        with suppress(Exception):
            await query.message.edit_text(
                f"🛡️ <b>ANONYMOUS POST PROTECTION {status}</b>\n\n"
                f"📌 <b>Channel:</b> {chat.title or 'Channel'}\n"
                f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                + ((_MODE_LINES.get(mode) or (
                    "Posts Telegram attributes to an admin (own profile, or signed with the admin's "
                    "name) are kept; anonymous posts are deleted."
                )) + "\n\n" if enabled else "Protection is now turned off.\n\n")
                + "<i>Telegram does not reveal which admin is behind a signed or channel-identity post; "
                "the bot never tries to identify them.</i>"
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
    enabled = bool(current and current.get("anonymous_filter_enabled"))
    state = "ON 🟢" if enabled else "OFF 🔴"
    mode = anonymous_policy.channel_posting_mode(chat)
    mode_line = _MODE_LINES.get(mode)
    warning = ""
    if mode == anonymous_policy.MODE_ANONYMOUS:
        warning = ("\n\n⚠️ <b>Protection deletes every post while Sign Messages is off.</b> "
                   "Turn it on in Channel settings first." if enabled else
                   "\n\n⚠️ Turn on <b>Sign Messages</b> in Channel settings before enabling.")
    await message.reply_text(
        f"📌 <b>Channel:</b> {chat.title or 'Channel'}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n"
        f"🛡️ <b>Current status:</b> {state}"
        + (f"\n{mode_line}" if mode_line else "")
        + warning
        + "\n\nChoose an action:",
        reply_markup=_menu(channel_id),
    )


def _post_time(message: Message) -> int:
    with suppress(AttributeError, OSError, ValueError):
        return int(message.date.timestamp())
    return int(time.time())


@app.on_message(filters.channel & filters.incoming, group=-30)
async def anonymous_post_filter(_, message: Message):
    """Reject anonymous posts while /setanonymous is enabled.

    Policy (anonymous_policy.should_reject_anonymous_post):
      * kept — posts Telegram attributes to an admin's personal account:
        sent as the admin's profile (raw ``from_id`` is a user), or signed by
        Telegram with the admin's name in Sign Messages mode;
      * deleted — published as the channel without a signature (or as the
        channel in Authors' Profiles mode), sent with another chat's
        identity, or unreadable sender metadata (fail closed);
      * service messages are exempt.
    Telegram does not reveal the human admin behind a signed or chat-identity
    post, so this filter never identifies one: it logs only the identity
    class, the channel's posting mode, whether the post was signed (never the
    name) and the sender chat Telegram reported.

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
        identity = anonymous_policy.sender_identity(message)
        mode = anonymous_policy.channel_posting_mode(message.chat)
        if mode == anonymous_policy.MODE_ANONYMOUS:
            _warn_sign_messages_off(message.chat.id)

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
            "anonymous post deleted chat=%s message=%s identity=%s channel_mode=%s signed=%s "
            "sender_chat=%s (not attributed to an admin's account; the bot never names an admin)",
            message.chat.id, message.id, identity, mode or "unknown",
            "yes" if anonymous_policy.has_post_signature(message) else "no",
            anonymous_policy.sender_chat_id(message),
        )
    except RPCError as exc:
        log.warning("anonymous filter Telegram error chat=%s message=%s: %s", message.chat.id, message.id, exc)
    except Exception as exc:
        log.exception("anonymous filter failed chat=%s message=%s: %s", getattr(message.chat, "id", None), getattr(message, "id", None), exc)
