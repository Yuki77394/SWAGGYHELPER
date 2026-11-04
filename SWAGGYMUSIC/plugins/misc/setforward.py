"""Channel FORWARD POST REMOVER.

/setforward on | /setforward off  -> CHANNEL ONLY: enable / disable the
Forward Post Remover for the channel the command is posted in. Channel
posts have no normal from_user; Telegram only lets members with posting
rights publish, so — exactly like /setlink, /setdelay and /setgap — the
command is usable by channel admins who can post, and the bot must hold
the Delete Messages right. A FORWARDED "/setforward ..." post is content,
not a command, and is never obeyed. The command message is deleted
immediately and the bot's reply auto-deletes after 120 seconds.

/setforward in the bot's PRIVATE chat -> the management wizard (same shape
as /setlink and /setanonymous): enter the Channel ID, see the current
status, choose 🟢 ENABLE / 🔴 DISABLE / ❌ CANCEL. The wizard verifies that
the user is an admin of the target channel (utils/permissions.
verify_channel_admin — re-verified on every button press) and that the bot
can delete there. The bot owner bypasses the channel-admin check; SUDOers
still need to be an admin of the specific channel.

MODERATION (handler groups):
    -40  /setlink filter            (link_blocker_new_channel_post)
    -30  /setanonymous filter       (anonymous_post_filter)
    -25  forward_post_remover       — NEW forwarded channel posts
    -17  /setforward channel command
      0  /setgap + /setdelay engine (autodelete_new_channel_post)
Running after the link and anonymous filters means a post one of them
already rejected for this update is left to it (no second delete call);
running before the /setgap+/setdelay engine means a forwarded post deleted
here can never become a gap reference or a /setdelay job (the engine skips
forward_policy.was_rejected ids). See SWAGGYMUSIC/utils/forward_policy.py.

DETECTION (utils/forward_policy.py): Telegram's forward header on the raw
update (``fwd_from``), Kurigram 2.2.25's ``Message.forward_origin``, and the
Pyrogram 2.0 forward fields as a fallback. Every kind of post is covered —
text, photos, videos, documents, audio, voice notes, stickers, polls,
locations ... — from every origin (user, hidden user, group, channel,
import). Non-forwarded posts, service messages and the bot's own posts are
never touched. Edited updates are not judged: an edit can never add a
forward header to a post.

SETTINGS: one field in the channel's document in mongodb.autodelete_settings
(the shared settings document — /setgap, /setdelay, /setanonymous and
/setlink live there too):
    { "chat_id": <channel_id>, "forward_filter_enabled": True }
Settings persist across restarts and redeployments.
"""

import asyncio
import time
from contextlib import suppress

from pyrogram import ContinuePropagation, filters
from pyrogram.enums import (ButtonStyle, ChatMemberStatus, ChatType,
                            MessageServiceType)
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)

from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils import anonymous_policy, forward_policy, link_policy
from SWAGGYMUSIC.utils.activity_logger import (_format_channel_title,
                                               log_setforward_change)
from SWAGGYMUSIC.utils.database import (queue_deletion_job,
                                        set_forward_filter_enabled,
                                        settings_db)
from SWAGGYMUSIC.utils.permissions import verify_channel_admin

log = LOGGER(__name__)
PREFIX = "setforward"
SESSIONS = {}
LOCK = asyncio.Lock()

# The bot's channel-side reply auto-deletes after this many seconds —
# the same TTL the /setlink, /setdelay and /setgap replies use.
_RESPONSE_TTL = 120


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

async def _forwarded_post(_, __, message) -> bool:
    return forward_policy.is_forwarded(message)


# Matches forwarded posts only, so the moderation handler never runs for
# (and costs nothing on) an ordinary post, and the channel command never
# obeys a forwarded "/setforward ..." post.
_FORWARDED = filters.create(_forwarded_post, name="ForwardedPostFilter")


async def _in_active_session(_, __, message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in SESSIONS
        and message.chat.type == ChatType.PRIVATE
    )


# This filter only matches messages from users who are inside an active
# /setforward private-chat session (same pattern as setlink.py).
_ACTIVE_SESSION = filters.create(_in_active_session, name="SetForwardSessionFilter")


# ---------------------------------------------------------------------------
# Helpers (same contracts as the ones in setlink.py)
# ---------------------------------------------------------------------------

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
        log.warning("setforward bot-permission check failed for %s: %s: %s",
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
# Private-chat wizard — same flow as /setlink and /setanonymous
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
    "🚫 <b>Deleted:</b> every forwarded post — text, photos, videos, "
    "documents and any other media — forwarded from users, hidden "
    "accounts, groups or channels. A forwarded album goes whole.\n\n"
    "✅ <b>Kept:</b> normal posts that are not forwarded."
)


@app.on_message(filters.command(["setforward"]) & filters.private)
async def setforward_private_start(_, message: Message):
    if not message.from_user:
        return
    async with LOCK:
        SESSIONS[message.from_user.id] = {"step": "channel_id"}
    await message.reply_text(
        "↪️ <b>FORWARD POST REMOVER</b>\n\n"
        "Please enter the <b>Channel ID</b> you want to manage.\n\n"
        "Example: <code>-100123456789</code>",
        reply_markup=_cancel_menu(),
    )


@app.on_callback_query(filters.regex(r"^setforward:"))
async def setforward_callback(_, query: CallbackQuery):
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
        return await query.answer("Setup expired. Use /setforward again.", show_alert=True)

    # Never trust the UI: the admin right is re-verified on every press
    # (it may have been revoked, or the callback_data may be crafted).
    ok, reason = await verify_channel_admin(channel_id, user_id)
    if not ok:
        SESSIONS.pop(user_id, None)
        await query.answer("You cannot manage this channel.", show_alert=True)
        with suppress(Exception):
            await query.message.edit_text(reason)
        return

    enabled = parts[1] == "enable"
    try:
        status = await set_forward_filter_enabled(channel_id, enabled)
        chat = None
        with suppress(Exception):
            chat = await app.get_chat(channel_id)
        title = _format_channel_title(chat)
        await query.answer("Saved")
        with suppress(Exception):
            if enabled:
                await query.message.edit_text(
                    f"↪️ <b>FORWARD POST REMOVER ENABLED 🟢</b>\n\n"
                    f"📌 <b>Channel:</b> {title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    + _ENABLED_TEXT + "\n\n"
                    + "<i>Forwarded posts are deleted automatically; service "
                      "messages are never touched.</i>"
                )
            else:
                await query.message.edit_text(
                    f"↪️ <b>FORWARD POST REMOVER DISABLED 🔴</b>\n\n"
                    f"📌 <b>Channel:</b> {title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    "The remover is now turned off — forwarded posts are no "
                    "longer deleted."
                )
        if status != "NO_CHANGE":
            with suppress(Exception):
                await log_setforward_change(
                    chat, query.from_user, "ENABLED" if enabled else "DISABLED",
                    source="PRIVATE CHAT",
                )
        SESSIONS.pop(user_id, None)
    except Exception as exc:
        log.warning("setforward update failed for %s: %s", channel_id, exc)
        await query.answer("Could not update settings. Check bot permissions.", show_alert=True)


@app.on_message(
    _ACTIVE_SESSION
    & ~filters.service
    & ~filters.command(["setforward", "cancel"]),
)
async def setforward_channel_input(_, message: Message):
    if not message.from_user:
        return
    raw = (message.text or message.caption or "").strip()
    if raw.startswith("/"):
        # Another command (e.g. /setlink): the user left this wizard. End
        # the session and let the command reach its own handler — a
        # Channel ID never starts with "/".
        SESSIONS.pop(message.from_user.id, None)
        raise ContinuePropagation
    session = SESSIONS.get(message.from_user.id)
    if not session or session.get("step") != "channel_id":
        return
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
        {"chat_id": channel_id}, {"forward_filter_enabled": 1},
    )
    enabled = bool(current and current.get("forward_filter_enabled"))
    state = "ON 🟢" if enabled else "OFF 🔴"
    chat = None
    with suppress(Exception):
        chat = await app.get_chat(channel_id)
    title = _format_channel_title(chat)
    await message.reply_text(
        f"📌 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n"
        f"↪️ <b>Current status:</b> {state}"
        "\n\nChoose an action:",
        reply_markup=_menu(channel_id),
    )


# ---------------------------------------------------------------------------
# /setforward — CHANNEL ONLY
# ---------------------------------------------------------------------------

INVALID_TEXT_FORWARD = (
    "Invalid option selected!\n"
    "Use on or off!\n\n"
    "Use like this:\n"
    "/setforward on (To turn on.)\n"
    "/setforward off (To turn off.)\n\n"
    "Current status for this channel: {status}."
)


@app.on_message(
    filters.channel
    & ~_FORWARDED
    & filters.regex(r"(?i)^/setforward(?:@\w+)?(?:\s+(.+?))?\s*$"),
    group=-17,
)
async def setforward_channel_handler(_, message: Message):
    if message.chat.type != ChatType.CHANNEL:
        return
    if forward_policy.is_forwarded(message):
        return  # forwarded content is never a command (also excluded by the filter)

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
            {"chat_id": message.chat.id}, {"forward_filter_enabled": 1},
        )
        status = "ON" if current and current.get("forward_filter_enabled") else "OFF"
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETFORWARD_INVALID")
        response = None
        with suppress(Exception):
            response = await message.reply_text(INVALID_TEXT_FORWARD.format(status=status))
        if response:
            _delete_after(response, _RESPONSE_TTL)
        return

    enabled = value == "on"
    status = await set_forward_filter_enabled(message.chat.id, enabled)

    await _delete_command_message(
        message,
        reason="COMMAND_CLEANUP_SETFORWARD_OFF" if not enabled else "COMMAND_CLEANUP_SETFORWARD_ON",
    )

    response = None
    with suppress(Exception):
        response = await message.reply_text(
            "Successfully enabled forward post remover!" if enabled
            else "Successfully disabled forward post remover!"
        )
    if response:
        _delete_after(response, _RESPONSE_TTL)

    # Activity log — only if the state actually changed (source=CHANNEL)
    if status != "NO_CHANGE":
        with suppress(Exception):
            await log_setforward_change(
                message.chat, message.from_user,
                "ENABLED" if enabled else "DISABLED",
                source="CHANNEL",
            )


# ---------------------------------------------------------------------------
# Moderation — new forwarded channel posts (handler group -25)
# ---------------------------------------------------------------------------

def _media_label(message: Message) -> str:
    """The post's media kind for logs (``photo``, ``video`` ...) or ``text``."""
    name = getattr(getattr(message, "media", None), "name", None)
    return name.lower() if isinstance(name, str) else "text"


async def _delete_post(message: Message, *, reason: str) -> bool:
    """Delete one post; queue a persistent retry when Telegram rate-limits.

    Mirrors the link and anonymous filters: a FloodWait must not leave a
    rejected post up, so the deletion job is queued for the /setdelay
    worker (reason ``FORWARD_REJECT_RETRY``) instead of being abandoned.
    Returns True when the delete call was sent without a FloodWait.
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
                reason="FORWARD_REJECT_RETRY",
            )
        log.warning(
            "forwarded post delete FloodWait chat=%s message=%s wait=%ss; retry queued",
            message.chat.id, message.id, wait,
        )
        return False
    log.info(
        "deletion reason=%s chat=%s msg=%s",
        reason, message.chat.id, message.id,
    )
    return True


async def _moderate_forwarded_post(message: Message) -> None:
    chat_id = message.chat.id

    # The /setlink (-40) or /setanonymous (-30) filter already rejected this
    # very update: that filter owns its deletion (or FloodWait retry), and
    # the gap engine already skips it through that filter's verdict.
    if link_policy.was_rejected(chat_id, message.id):
        log.info("forward filter skipped chat=%s msg=%s: rejected by link filter",
                 chat_id, message.id)
        return
    if anonymous_policy.was_rejected(chat_id, message.id):
        log.info("forward filter skipped chat=%s msg=%s: rejected by anonymous filter",
                 chat_id, message.id)
        return

    setting = await settings_db.find_one(
        {"chat_id": chat_id}, {"forward_filter_enabled": 1},
    )
    if not setting or not setting.get("forward_filter_enabled"):
        return  # feature off — nothing is ever deleted

    # Verdict FIRST (so the gap/delay engine in group 0 always sees it),
    # Telegram calls after — the same order the link filter uses.
    forward_policy.mark_rejected(chat_id, message.id)

    if not await _bot_can_delete(chat_id):
        log.warning(
            "forwarded post rejected but not deleted (no Delete Messages right) "
            "chat=%s message=%s", chat_id, message.id,
        )
        return

    if await _delete_post(message, reason="FORWARD_BLOCKED"):
        log.info(
            "forwarded post deleted chat=%s message=%s origin=%s media=%s album=%s",
            chat_id, message.id, forward_policy.forward_origin_kind(message),
            _media_label(message), link_policy.media_group_id(message) or "-",
        )


@app.on_message(filters.channel & filters.incoming & _FORWARDED, group=-25)
async def forward_post_remover(_, message: Message):
    """Delete forwarded channel posts while /setforward is on.

    Runs in group -25: after the /setlink filter (-40) and the
    /setanonymous filter (-30), before the channel commands
    (-20/-19/-18/-17) and the /setgap + /setdelay engine (0). Only
    forwarded posts reach it (``_FORWARDED``); service messages, edits and
    the bot's own posts are never judged.
    """
    try:
        if message.chat.type != ChatType.CHANNEL:
            return
        if getattr(message, "outgoing", False):
            return
        if getattr(message, "edit_date", None) is not None:
            return
        if isinstance(getattr(message, "service", None), MessageServiceType):
            return
        await _moderate_forwarded_post(message)
    except RPCError as exc:
        log.warning("forward filter Telegram error chat=%s message=%s: %s",
                    getattr(getattr(message, "chat", None), "id", None),
                    getattr(message, "id", None), exc)
    except Exception as exc:
        log.exception(
            "forward filter failed chat=%s message=%s: %s",
            getattr(getattr(message, "chat", None), "id", None),
            getattr(message, "id", None), exc,
        )
