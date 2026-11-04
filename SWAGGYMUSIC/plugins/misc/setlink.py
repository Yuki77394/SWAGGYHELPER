"""Channel link restriction — the ALL-LINK BLOCKER.

/setlink on | /setlink off  -> CHANNEL ONLY: enable / disable link
restriction for the channel the command is posted in. Channel posts have
no normal from_user; Telegram only lets members with posting rights
publish, so — exactly like /setdelay and /setgap — the command is usable
by channel admins who can post, and the bot must hold the Delete
Messages right. The command message is deleted immediately and the bot's
reply auto-deletes after 120 seconds.

/setlink in the bot's PRIVATE chat -> the management wizard (same shape
as /setanonymous): enter the Channel ID, see the current status, choose
🟢 ENABLE / 🔴 DISABLE / ❌ CANCEL. The wizard verifies that the user is
an admin of the target channel (utils/permissions.verify_channel_admin —
re-verified on every button press) and that the bot can delete there.
The owner bypasses the channel-admin check; SUDOers still need to be an
admin of the specific channel.

MODERATION (handler groups):
    -40  link_blocker_new_channel_post    — new channel posts
    -40  link_blocker_edited_channel_post — edited channel posts (edits
          can NEVER smuggle a link past the filter)
    -18  /setlink channel command
Running before /setanonymous (-30) and the /setgap+/setdelay engine (0)
means a post deleted for its links can never become a gap reference or a
/setdelay job; the engine additionally refuses in-process rejected ids
(see SWAGGYMUSIC/utils/link_policy.py and anonymous_policy.py).

DETECTION (utils/link_policy.py):
    blocked — HTTP/HTTPS URLs, www links, recognizable bare domains
    (instagram.com, youtu.be, facebook.com, chat.whatsapp.com ...),
    t.me / telegram.me / telegram.dog links (invites included), tg://
    deep links, and any text, username or emoji made clickable with a
    TEXT_LINK entity. Checked in message text, captions, forwards,
    edits and media albums.
    allowed — plain @usernames with no URL attached, e-mail addresses,
    ordinary text.

ALBUMS: an album containing a prohibited link is deleted whole — the
current item, every item already delivered (in-process tracking) and
every later item of the same media_group_id; the verdict is persisted in
``link_blocked_albums`` so a restart mid-delivery cannot leave a partial
album (best-effort, like the /setgap engine's gap_albums).

SETTINGS: one document per channel in mongodb.autodelete_settings (the
shared settings document — /setgap, /setdelay and /setanonymous live
there too):
    { "chat_id": <channel_id>,
      "link_filter_enabled": True,
      "link_blocked_albums": [<media_group_id>, ...] }   # newest 50
Settings persist across restarts and redeployments.
"""

import asyncio
import time
from contextlib import suppress

from pyrogram import filters
from pyrogram.enums import (ButtonStyle, ChatMemberStatus, ChatType,
                            MessageServiceType)
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils import link_policy
from SWAGGYMUSIC.utils.activity_logger import log_setlink_change
from SWAGGYMUSIC.utils.database import (remember_blocked_album,
                                        set_link_filter_enabled,
                                        settings_db, queue_deletion_job)
from SWAGGYMUSIC.utils.permissions import verify_channel_admin

log = LOGGER(__name__)
PREFIX = "setlink"
SESSIONS = {}
LOCK = asyncio.Lock()

# The bot's channel-side reply auto-deletes after this many seconds —
# the same TTL the /setdelay and /setgap replies use.
_RESPONSE_TTL = 120


def _post_time(message: Message) -> int:
    with suppress(AttributeError, OSError, ValueError):
        return int(message.date.timestamp())
    return int(time.time())


async def _bot_can_delete(chat_id: int) -> bool:
    """True if the bot may delete posts in the channel (never raises)."""
    try:
        member = await app.get_chat_member(chat_id, app.id)
        if member.status == ChatMemberStatus.OWNER:
            return True
        privileges = getattr(member, "privileges", None)
        return bool(
            member.status == ChatMemberStatus.ADMINISTRATOR
            and privileges and privileges.can_delete_messages
        )
    except Exception as exc:
        log.warning("setlink bot-permission check failed for %s: %s: %s",
                    chat_id, type(exc).__name__, exc)
        return False


async def _delete_command_message(message: Message, *, reason: str) -> None:
    """Delete a command message from the channel and log the reason.

    Same contract as autodelete._delete_command_message: never raises —
    deletion failures (e.g. message already gone) are suppressed.
    """
    with suppress(Exception):
        await message.delete()
    log.info(
        "deletion reason=%s chat=%s msg=%s",
        reason,
        getattr(getattr(message, "chat", None), "id", "?"),
        getattr(message, "id", "?"),
    )


def _delete_after(message: Message, delay: int) -> None:
    """Non-blocking delayed deletion of the bot's own reply."""

    async def _inner():
        try:
            await asyncio.sleep(delay)
            with suppress(Exception):
                await message.delete()
            log.info(
                "deletion reason=BOT_REPLY_CLEANUP chat=%s msg=%s",
                getattr(getattr(message, "chat", None), "id", "?"),
                getattr(message, "id", "?"),
            )
        except Exception:
            pass

    asyncio.create_task(_inner())


# ---------------------------------------------------------------------------
# Private-chat wizard — same flow as /setanonymous
# ---------------------------------------------------------------------------

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


_ENABLED_TEXT = (
    "🚫 <b>Blocked:</b> posts, captions, edited posts and media albums that "
    "carry HTTP/HTTPS or www links, t.me and invite links, other website "
    "URLs, or any text hiding a clickable URL.\n\n"
    "✅ <b>Allowed:</b> plain @usernames with no URL attached, e-mail "
    "addresses and ordinary text."
)


@app.on_message(filters.command(["setlink"]) & filters.private)
async def setlink_private_start(_, message: Message):
    if not message.from_user:
        return
    async with LOCK:
        SESSIONS[message.from_user.id] = {"step": "channel_id"}
    await message.reply_text(
        "🔗 <b>LINK RESTRICTION</b>\n\n"
        "Please enter the <b>Channel ID</b> you want to manage.\n\n"
        "Example: <code>-100123456789</code>",
        reply_markup=_cancel_menu(),
    )


@app.on_callback_query(filters.regex(r"^setlink:"))
async def setlink_callback(_, query: CallbackQuery):
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
        return await query.answer("Setup expired. Use /setlink again.", show_alert=True)

    ok, reason = await verify_channel_admin(channel_id, user_id)
    if not ok:
        SESSIONS.pop(user_id, None)
        await query.answer("You cannot manage this channel.", show_alert=True)
        with suppress(Exception):
            await query.message.edit_text(reason)
        return

    enabled = parts[1] == "enable"
    try:
        status = await set_link_filter_enabled(channel_id, enabled)
        chat = None
        with suppress(Exception):
            chat = await app.get_chat(channel_id)
        title = getattr(chat, "title", None) or "Channel"
        await query.answer("Saved")
        with suppress(Exception):
            if enabled:
                await query.message.edit_text(
                    f"🔗 <b>LINK RESTRICTION ENABLED 🟢</b>\n\n"
                    f"📌 <b>Channel:</b> {title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    + _ENABLED_TEXT + "\n\n"
                    + "<i>Prohibited posts are deleted automatically; service "
                      "messages are never touched.</i>"
                )
            else:
                await query.message.edit_text(
                    f"🔗 <b>LINK RESTRICTION DISABLED 🔴</b>\n\n"
                    f"📌 <b>Channel:</b> {title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    "Restriction is now turned off — posts containing links "
                    "are no longer deleted."
                )
        if status != "NO_CHANGE":
            with suppress(Exception):
                await log_setlink_change(
                    chat, query.from_user, "ENABLED" if enabled else "DISABLED",
                    source="PRIVATE CHAT",
                )
        SESSIONS.pop(user_id, None)
    except Exception as exc:
        log.warning("setlink update failed for %s: %s", channel_id, exc)
        await query.answer("Could not update settings. Check bot permissions.", show_alert=True)


# This filter only matches messages from users who are inside an active
# /setlink private-chat session (same pattern as setconfig.py).
_ACTIVE_SESSION = filters.create(
    lambda _, __, message: bool(
        message.from_user
        and message.from_user.id in SESSIONS
        and message.chat.type == ChatType.PRIVATE
    )
)


@app.on_message(
    _ACTIVE_SESSION
    & ~filters.service
    & ~filters.command(["setlink", "cancel"]),
)
async def setlink_channel_input(_, message: Message):
    if not message.from_user:
        return
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("step") != "channel_id":
        return
    raw = (message.text or message.caption or "").strip()
    try:
        channel_id = int(raw)
    except ValueError:
        return await message.reply_text("❌ Invalid Channel ID. Send a numeric ID, e.g. <code>-100123456789</code>.")

    ok, reason = await verify_channel_admin(channel_id, message.from_user.id)
    if not ok:
        SESSIONS.pop(message.from_user.id, None)
        return await message.reply_text(reason)

    session.update({"channel_id": channel_id, "step": "action"})
    current = await settings_db.find_one(
        {"chat_id": channel_id}, {"link_filter_enabled": 1},
    )
    enabled = bool(current and current.get("link_filter_enabled"))
    state = "ON 🟢" if enabled else "OFF 🔴"
    chat = None
    with suppress(Exception):
        chat = await app.get_chat(channel_id)
    title = getattr(chat, "title", None) or "Channel"
    await message.reply_text(
        f"📌 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n"
        f"🔗 <b>Current status:</b> {state}"
        + ("\n\n⚠️ I need the <b>Delete Messages</b> admin right in that "
           "channel before this protection can work." if not await _bot_can_delete(channel_id) else "")
        + "\n\nChoose an action:",
        reply_markup=_menu(channel_id),
    )


# ---------------------------------------------------------------------------
# /setlink — CHANNEL ONLY
# ---------------------------------------------------------------------------

INVALID_TEXT_LINK = (
    "Invalid option selected!\n"
    "Use on or off!\n\n"
    "Use like this:\n"
    "/setlink on (To turn on.)\n"
    "/setlink off (To turn off.)\n\n"
    "Current status for this channel: {status}."
)


@app.on_message(
    filters.channel & filters.regex(r"(?i)^/setlink(?:@\w+)?(?:\s+(.+?))?\s*$"),
    group=-18,
)
async def setlink_channel_handler(_, message: Message):
    if message.chat.type != ChatType.CHANNEL:
        return

    # Channel posts have no normal from_user. Telegram only allows channel
    # members with posting rights / admins to publish, so this command is
    # usable by channel admins who can post — NOT restricted to OWNER/SUDO.
    if not await _bot_can_delete(message.chat.id):
        with suppress(Exception):
            await message.reply_text(
                "I need <b>Delete Messages</b> admin permission to work here."
            )
        return

    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    value = parts[1].strip().lower() if len(parts) == 2 else ""

    if value not in {"on", "off"}:
        current = await settings_db.find_one(
            {"chat_id": message.chat.id}, {"link_filter_enabled": 1},
        )
        status = "ON" if current and current.get("link_filter_enabled") else "OFF"
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETLINK_INVALID")
        response = None
        with suppress(Exception):
            response = await message.reply_text(INVALID_TEXT_LINK.format(status=status))
        if response:
            _delete_after(response, _RESPONSE_TTL)
        return

    enabled = value == "on"
    prior = await settings_db.find_one(
        {"chat_id": message.chat.id}, {"link_filter_enabled": 1},
    )
    was_enabled = bool(prior and prior.get("link_filter_enabled"))
    await set_link_filter_enabled(message.chat.id, enabled)

    await _delete_command_message(
        message,
        reason="COMMAND_CLEANUP_SETLINK_OFF" if not enabled else "COMMAND_CLEANUP_SETLINK_ON",
    )

    response = None
    with suppress(Exception):
        response = await message.reply_text(
            "Successfully enabled link restriction!" if enabled
            else "Successfully disabled link restriction!"
        )
    if response:
        _delete_after(response, _RESPONSE_TTL)

    # Activity log — only if the state actually changed (source=CHANNEL)
    if enabled != was_enabled:
        with suppress(Exception):
            await log_setlink_change(
                message.chat, message.from_user,
                "ENABLED" if enabled else "DISABLED",
                source="CHANNEL",
            )


# ---------------------------------------------------------------------------
# Moderation — new and edited channel posts (handler group -40)
# ---------------------------------------------------------------------------

async def _delete_post(message: Message, *, reason: str) -> bool:
    """Delete one post; queue a persistent retry when Telegram rate-limits.

    Mirrors the anonymous filter: a FloodWait must not leave a rejected
    post up, so the deletion job is queued for the /setdelay worker
    (reason ``LINK_REJECT_RETRY``) instead of being abandoned. Returns
    True when the delete call was sent without a FloodWait.
    """
    try:
        await message.delete()
    except FloodWait as exc:
        wait = min(max(int(getattr(exc, "value", 0) or 0), 1), 3600)
        with suppress(Exception):
            await queue_deletion_job(
                message.chat.id, message.id,
                delete_at=int(time.time()) + wait,
                original_post_time=_post_time(message),
                reason="LINK_REJECT_RETRY",
            )
        log.warning(
            "link post delete FloodWait chat=%s message=%s wait=%ss; retry queued",
            message.chat.id, message.id, wait,
        )
        return False
    log.info(
        "deletion reason=%s chat=%s msg=%s",
        reason, message.chat.id, message.id,
    )
    return True


async def _delete_album_siblings(chat_id: int, media_group_id: int,
                                 keep: int) -> None:
    """Best-effort deletion of the album items delivered before the verdict.

    Telegram delivered the clean items earlier and separately, so the whole
    album goes down in one channels.deleteMessages call. Failures (an item
    already gone, an RPC error) are logged and never raised: every later
    item is still deleted on arrival by the filter itself.
    """
    ids = [mid for mid in link_policy.take_album_items(chat_id, media_group_id)
           if mid != keep]
    if not ids:
        return
    try:
        await app.delete_messages(chat_id, ids)
        log.info(
            "deletion reason=LINK_ALBUM_ITEM chat=%s msg=%s album=%s",
            chat_id, ids, media_group_id,
        )
    except FloodWait as exc:
        wait = min(max(int(getattr(exc, "value", 0) or 0), 1), 3600)
        for mid in ids:
            with suppress(Exception):
                await queue_deletion_job(
                    chat_id, mid,
                    delete_at=int(time.time()) + wait,
                    original_post_time=int(time.time()),
                    reason="LINK_REJECT_RETRY",
                )
        log.warning(
            "link album delete FloodWait chat=%s album=%s wait=%ss; retries queued",
            chat_id, media_group_id, wait,
        )
    except Exception as exc:
        log.warning(
            "link album sibling delete failed chat=%s album=%s ids=%s: %s: %s",
            chat_id, media_group_id, ids, type(exc).__name__, exc,
        )


async def _moderate_channel_post(message: Message) -> None:
    """The shared verdict path for new posts and edits."""
    chat = message.chat
    chat_id = chat.id
    group_id = link_policy.media_group_id(message)
    hits = link_policy.find_prohibited_links(message)

    # Follow-up item of an album this filter already condemned: delete on
    # arrival, no further I/O. Completing a condemned album is kept even
    # if the feature is switched off mid-delivery (the /setgap engine
    # keeps album verdicts across a change the same way — an album is
    # never split).
    if group_id is not None and link_policy.album_is_blocked(chat_id, group_id):
        link_policy.mark_rejected(chat_id, message.id)
        await _delete_post(message, reason="LINK_ALBUM_ITEM")
        return

    if not hits and group_id is None:
        return  # fast path: ordinary post, no I/O

    setting = await settings_db.find_one(
        {"chat_id": chat_id},
        {"link_filter_enabled": 1, "link_blocked_albums": 1},
    )
    if not setting or not setting.get("link_filter_enabled"):
        return  # feature off — nothing is ever deleted

    # Restart recovery: this album was condemned before the restart, so
    # the delivered item must not survive the gap in in-process state.
    if group_id is not None and link_policy.persisted_blocked_album(setting, group_id):
        link_policy.block_album(chat_id, group_id)
        link_policy.mark_rejected(chat_id, message.id)
        await _delete_post(message, reason="LINK_ALBUM_ITEM")
        return

    if not hits:
        # Clean album item: remember it so a later item's link takes the
        # whole album down.
        link_policy.remember_album_item(chat_id, group_id, message.id)
        return

    # Prohibited link while the feature is enabled. Verdict FIRST (so
    # concurrent album items and the gap engine always see it), Telegram
    # calls after — the same order the anonymous filter uses.
    if group_id is not None:
        link_policy.block_album(chat_id, group_id)
        try:
            await remember_blocked_album(chat_id, group_id)
        except Exception as exc:
            log.warning(
                "link album verdict not persisted chat=%s album=%s: %s: %s",
                chat_id, group_id, type(exc).__name__, exc,
            )
    link_policy.mark_rejected(chat_id, message.id)

    if not await _bot_can_delete(chat_id):
        log.warning(
            "link post rejected but not deleted (no Delete Messages right) "
            "chat=%s message=%s", chat_id, message.id,
        )
        return

    kinds = ", ".join(kind for kind, _value in hits)
    values = "; ".join(value for _kind, value in hits if value)
    await _delete_post(message, reason="LINK_BLOCKED")
    if group_id is not None:
        await _delete_album_siblings(chat_id, group_id, keep=message.id)
    log.info(
        "link post deleted chat=%s message=%s kinds=[%s] links=%s album=%s edited=%s",
        chat_id, message.id, kinds,
        values[:512], group_id or "-",
        getattr(message, "edit_date", None) is not None,
    )


@app.on_message(filters.channel & filters.incoming, group=-40)
async def link_blocker_new_channel_post(_, message: Message):
    """Delete channel posts that carry prohibited links while /setlink is on.

    Runs in group -40, before the /setanonymous filter (-30), the
    /setdelay and /setgap commands (-20/-19) and the gap/delay engine
    (0). Service messages and the bot's own posts are never judged.
    """
    try:
        if message.chat.type != ChatType.CHANNEL:
            return
        if getattr(message, "outgoing", False):
            return
        if isinstance(getattr(message, "service", None), MessageServiceType):
            return
        await _moderate_channel_post(message)
    except RPCError as exc:
        log.warning("link filter Telegram error chat=%s message=%s: %s",
                    getattr(getattr(message, "chat", None), "id", None),
                    getattr(message, "id", None), exc)
    except Exception as exc:
        log.exception(
            "link filter failed chat=%s message=%s: %s",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "id", None), exc,
        )


@app.on_edited_message(filters.channel & filters.incoming, group=-40)
async def link_blocker_edited_channel_post(_, message: Message):
    """Edited channel posts run through the SAME verdict path.

    An edit can never smuggle a link past the filter: a clean post edited
    to contain a link is deleted, and so is a post whose new caption or
    hidden text_link entity carries one. (The gap engine's own
    on_edited_message no-op at group 0 is untouched; this handler simply
    runs before it.)
    """
    try:
        if message.chat.type != ChatType.CHANNEL:
            return
        if getattr(message, "outgoing", False):
            return
        if isinstance(getattr(message, "service", None), MessageServiceType):
            return
        await _moderate_channel_post(message)
    except RPCError as exc:
        log.warning("link filter Telegram error (edit) chat=%s message=%s: %s",
                    getattr(getattr(message, "chat", None), "id", None),
                    getattr(message, "id", None), exc)
    except Exception as exc:
        log.exception(
            "link filter failed (edit) chat=%s message=%s: %s",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "id", None), exc,
        )
