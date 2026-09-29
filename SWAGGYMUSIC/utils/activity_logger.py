#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.
#
# Centralized Activity / Audit Logger
# -----------------------------------
#
# All activity logs are sent to config.LOGGER_ID, gated behind the single
# master toggle ``is_on_off(2)`` (MongoDB collection ``onoffper``, document
# ``{"on_off": 2}``).  The toggle is controlled by the /logger command
# (plugins/sudo/logger.py).  There are NO separate per-feature toggles —
# when logging is enabled, all activity helpers below send their messages;
# when disabled, all of them silently no-op.
#
# Safety guarantees:
#   - If config.LOGGER_ID is 0 / missing, log_activity() returns silently.
#   - If the Telegram send fails for any reason, the exception is caught
#     and forwarded to the application logger (SWAGGYMUSIC.activity_logger).
#     The original command handler that triggered the log is NEVER crashed.
#   - No passwords, OTPs, session strings, API keys, bot tokens, cookies,
#     or other credentials are ever logged.  Only user mentions, user IDs,
#     usernames, channel IDs, and setting values are included in log text.
#   - User display names are HTML-escaped before being embedded in the
#     <a href="tg://user?id=..."> mention to prevent injection.

import html
from datetime import datetime, timezone
from typing import Optional

from SWAGGYMUSIC import app
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils.database import is_on_off

log = LOGGER("SWAGGYMUSIC.activity_logger")


# ---------------------------------------------------------------------------
# Core sender — every helper below routes through this single function.
# ---------------------------------------------------------------------------

async def log_activity(text: str) -> None:
    """Send ``text`` to ``config.LOGGER_ID`` if activity logging is on.

    This is the single point of entry to the LOGGER_ID channel.  All helper
    functions below call this — they never call ``app.send_message`` directly,
    so the ``is_on_off(2)`` check and error handling exist in exactly one
    place.
    """

    try:
        import config

        if not config.LOGGER_ID:
            # LOGGER_ID not configured — silently no-op.
            return

        if not await is_on_off(2):
            # Master activity-log toggle is OFF — silently no-op.
            return

        await app.send_message(chat_id=config.LOGGER_ID, text=text)

    except Exception as exc:
        # Never crash the caller.  Log the error and move on.
        log.warning(
            "Activity log failed: %s: %s",
            type(exc).__name__,
            exc,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_mention(user) -> str:
    """Build a clickable HTML ``<a href="tg://user?id=...">`` mention.

    Works with any object that has ``id`` and ``first_name`` attributes
    (Pyrogram ``User``).  The display name is HTML-escaped to prevent
    injection.  If the user object is None or missing attributes, a safe
    fallback string is returned.
    """

    if user is None:
        return "Unknown User"

    try:
        name = (user.first_name or "").strip()
        if not name:
            # Fall back to last_name, then to a generic label.
            name = (getattr(user, "last_name", None) or "").strip()
        if not name:
            name = "User"

        safe_name = html.escape(name, quote=False)
        user_id = int(user.id)

        return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    except Exception:
        return "Unknown User"


def _format_username(user) -> str:
    """Return ``@username`` or ``Not Available``."""

    if user is None:
        return "Not Available"

    username = getattr(user, "username", None)
    return f"@{username}" if username else "Not Available"


def _format_timestamp() -> str:
    """Return a human-readable UTC timestamp for log entries."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_channel_title(chat) -> str:
    """Return the channel title, or 'Not Available' if unavailable.

    Works with any Pyrogram Chat object.  For private channels or chats
    where the title is not set, returns a safe fallback.
    """

    if chat is None:
        return "Not Available"

    try:
        title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        if title:
            return html.escape(str(title), quote=False)
    except Exception:
        pass

    return "Not Available"


def _format_channel_username(chat) -> str:
    """Return ``@username`` for the channel, or 'Not Available'.

    Private channels do not have a public username.
    """

    if chat is None:
        return "Not Available"

    try:
        username = getattr(chat, "username", None)
        if username:
            return f"@{username}"
    except Exception:
        pass

    return "Not Available"


def _format_channel_id(chat) -> int:
    """Return the numeric channel ID, or 0 if unavailable."""

    if chat is None:
        return 0

    try:
        return int(getattr(chat, "id", 0))
    except Exception:
        return 0


def _format_user_id(user) -> str:
    """Return the numeric user ID as a string, or 'Not Available'."""

    if user is None:
        return "Not Available"

    try:
        return str(int(getattr(user, "id", 0)))
    except Exception:
        return "Not Available"


def _format_duration(seconds: int) -> str:
    """Convert a seconds value to a human-readable duration string.

    Examples: 3 → "3 seconds", 60 → "1 minute", 600 → "10 minutes",
    3600 → "1 hour", 79200 → "22 hours".
    """

    try:
        seconds = int(seconds)
    except Exception:
        return str(seconds)

    if seconds <= 0:
        return "0 seconds"

    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"

    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    hours = seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


# ---------------------------------------------------------------------------
# Activity helpers — one per logical event.
# Each builds its text and delegates to log_activity().
# ---------------------------------------------------------------------------

async def log_user_start(user) -> None:
    """Log that a user started the bot (replaces the old inline block in
    plugins/bot/start.py).  Produces the same text as the original
    implementation so the LOGGER_ID output is visually unchanged.
    """

    text = (
        f"{_build_mention(user)} ᴊᴜsᴛ sᴛᴀʀᴛᴇᴅ ᴛʜᴇ ʙᴏᴛ.\n\n"
        f"<b>ᴜsᴇʀ ɪᴅ :</b> <code>{int(getattr(user, 'id', 0))}</code>\n"
        f"<b>ᴜsᴇʀɴᴀᴍᴇ :</b> {_format_username(user)}"
    )
    await log_activity(text)


async def log_createpost_attempt(user) -> None:
    """Log that a non-SUDO user reached the final CreatePost submission
    step and was denied access.

    This is called ONLY at the two final-stage SUDO gates in
    plugins/misc/createpost.py (hidden-preview gate and normal-post gate).
    Each gate fires at most once per /createpost session, so a single
    restricted attempt produces exactly one log entry — no global
    memory cache is needed for deduplication.
    """

    text = (
        "🔒 <b>CREATE POST ACCESS ATTEMPT</b>\n\n"
        f"👤 <b>User:</b> {_build_mention(user)}\n"
        f"🆔 <b>ID:</b> <code>{int(getattr(user, 'id', 0))}</code>\n"
        f"🔗 <b>Username:</b> {_format_username(user)}\n\n"
        "📌 <b>Action:</b>\n"
        "User attempted to create a post but does not have access to CreatePost.\n\n"
        "📊 <b>Status:</b> ❌ ACCESS DENIED\n\n"
        f"🕐 <b>Time:</b> <code>{_format_timestamp()}</code>"
    )
    await log_activity(text)


async def log_setdelay_change(
    chat, user, status: str, *,
    delay_value: Optional[str] = None,
    old_value: Optional[str] = None,
) -> None:
    """Log a /setdelay state change for a specific channel.

    Parameters
    ----------
    chat : pyrogram.types.Chat
        The channel where the change happened (``message.chat``).
    user : pyrogram.types.User or None
        The admin who issued the command.  None for anonymous channel posts.
    status : str
        One of ``"ENABLED"``, ``"DISABLED"``, ``"UPDATED"``.
    delay_value : str, optional
        The new delay value (human-readable, e.g. ``"10 minutes"``).
        Required for ENABLED and UPDATED.
    old_value : str, optional
        The previous delay value (human-readable).  Required for UPDATED.
    """

    ch_title = _format_channel_title(chat)
    ch_id = _format_channel_id(chat)
    ch_uname = _format_channel_username(chat)
    actor = _build_mention(user) if user is not None else "Channel Admin"
    actor_id = _format_user_id(user)
    timestamp = _format_timestamp()

    if status == "ENABLED":
        header = "🟢 <b>SETDELAY ENABLED</b>"
        delay_line = f"⏱ <b>Delay:</b> <code>{delay_value or 'N/A'}</code>\n\n"
    elif status == "DISABLED":
        header = "🔴 <b>SETDELAY DISABLED</b>"
        delay_line = ""  # No delay value when disabling
    elif status == "UPDATED":
        header = "⚙️ <b>SETDELAY UPDATED</b>"
        delay_line = (
            f"⏱ <b>Previous Delay:</b> <code>{old_value or 'N/A'}</code>\n"
            f"⏱ <b>New Delay:</b> <code>{delay_value or 'N/A'}</code>\n\n"
        )
    else:
        header = f"⏱ <b>SETDELAY {html.escape(status, quote=False)}</b>"
        delay_line = ""

    text = (
        f"{header}\n\n"
        f"📌 <b>Channel:</b> {ch_title}\n"
        f"🆔 <b>Channel ID:</b> <code>{ch_id}</code>\n"
        f"🔗 <b>Username:</b> {ch_uname}\n\n"
        f"👤 <b>Changed By:</b> {actor}\n"
        f"🆔 <b>User ID:</b> <code>{actor_id}</code>\n\n"
        f"{delay_line}"
        f"📊 <b>Status:</b> {status}\n\n"
        f"🕐 <b>Time:</b> <code>{timestamp}</code>"
    )
    await log_activity(text)


async def log_setgap_change(
    chat, user, status: str, *,
    gap_value: Optional[str] = None,
    old_value: Optional[str] = None,
) -> None:
    """Log a /setgap state change for a specific channel.

    Parameters
    ----------
    chat : pyrogram.types.Chat
        The channel where the change happened.
    user : pyrogram.types.User or None
        The admin who issued the command.  None for anonymous channel posts.
    status : str
        One of ``"ENABLED"``, ``"DISABLED"``, ``"UPDATED"``.
    gap_value : str, optional
        The new gap value (human-readable).  Required for ENABLED and UPDATED.
    old_value : str, optional
        The previous gap value (human-readable).  Required for UPDATED.
    """

    ch_title = _format_channel_title(chat)
    ch_id = _format_channel_id(chat)
    ch_uname = _format_channel_username(chat)
    actor = _build_mention(user) if user is not None else "Channel Admin"
    actor_id = _format_user_id(user)
    timestamp = _format_timestamp()

    if status == "ENABLED":
        header = "🟢 <b>SETGAP ENABLED</b>"
        gap_line = f"📏 <b>Gap:</b> <code>{gap_value or 'N/A'}</code>\n\n"
    elif status == "DISABLED":
        header = "🔴 <b>SETGAP DISABLED</b>"
        gap_line = ""
    elif status == "UPDATED":
        header = "⚙️ <b>SETGAP UPDATED</b>"
        gap_line = (
            f"⏱ <b>Previous Gap:</b> <code>{old_value or 'N/A'}</code>\n"
            f"⏱ <b>New Gap:</b> <code>{gap_value or 'N/A'}</code>\n\n"
        )
    else:
        header = f"📏 <b>SETGAP {html.escape(status, quote=False)}</b>"
        gap_line = ""

    text = (
        f"{header}\n\n"
        f"📌 <b>Channel:</b> {ch_title}\n"
        f"🆔 <b>Channel ID:</b> <code>{ch_id}</code>\n"
        f"🔗 <b>Username:</b> {ch_uname}\n\n"
        f"👤 <b>Changed By:</b> {actor}\n"
        f"🆔 <b>User ID:</b> <code>{actor_id}</code>\n\n"
        f"{gap_line}"
        f"📊 <b>Status:</b> {status}\n\n"
        f"🕐 <b>Time:</b> <code>{timestamp}</code>"
    )
    await log_activity(text)


async def log_logger_toggle(user, action: str) -> None:
    """Log that the /logger command itself was toggled.

    Called from plugins/sudo/logger.py:
      - /logger enable  → add_on(2) first, then log_logger_toggle(user, "ENABLED")
      - /logger disable → log_logger_toggle(user, "DISABLED") first, then add_off(2)

    The ordering ensures the log fires while the master toggle is still ON.
    No recursive loops — log_logger_toggle just calls log_activity() which
    calls app.send_message(); no command handler is triggered.
    """

    actor = _build_mention(user) if user is not None else "SUDO User"
    actor_id = _format_user_id(user)
    timestamp = _format_timestamp()

    if action == "ENABLED":
        header = "📝 <b>LOGGER ENABLED</b>"
    elif action == "DISABLED":
        header = "📝 <b>LOGGER DISABLED</b>"
    else:
        header = f"📝 <b>LOGGER {html.escape(action, quote=False)}</b>"

    text = (
        f"{header}\n\n"
        f"👤 <b>Changed By:</b> {actor}\n"
        f"🆔 <b>User ID:</b> <code>{actor_id}</code>\n\n"
        f"📊 <b>Status:</b> {action}\n\n"
        f"🕐 <b>Time:</b> <code>{timestamp}</code>"
    )
    await log_activity(text)


async def log_setgap_update(
    channel_id: int, message_id: int, action: str
) -> None:
    """Log that the gap engine took an action on a channel post (allowed /
    deleted / etc.).

    This is a lower-level log for the auto-delete worker's gap decisions.
    Currently provided for future use — the worker does not call it yet.
    """

    text = (
        "📐 <b>GAP ENGINE UPDATE</b>\n\n"
        f"📢 <b>Channel:</b> <code>{channel_id}</code>\n"
        f"💬 <b>Message ID:</b> <code>{message_id}</code>\n"
        f"🔧 <b>Action:</b> {action}"
    )
    await log_activity(text)
