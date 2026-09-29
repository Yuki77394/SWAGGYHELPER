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
    channel_id: int, value: str, user=None
) -> None:
    """Log that /setdelay was used to update the auto-delete delay for a
    channel.

    ``user`` is the admin who issued the command.  For anonymous channel
    posts ``message.from_user`` is None — pass None and the log shows
    ``Channel Admin`` instead of a user mention.
    """

    actor = _build_mention(user) if user is not None else "Channel Admin"

    text = (
        "⏱ <b>SET DELAY UPDATED</b>\n\n"
        f"📢 <b>Channel:</b> <code>{channel_id}</code>\n"
        f"👤 <b>By:</b> {actor}\n"
        f"🔧 <b>New Delay:</b> <code>{value}</code>"
    )
    await log_activity(text)


async def log_setgap_change(
    channel_id: int, value: str, user=None
) -> None:
    """Log that /setgap was used to update the post-gap interval for a
    channel.
    """

    actor = _build_mention(user) if user is not None else "Channel Admin"

    text = (
        "📏 <b>SET GAP UPDATED</b>\n\n"
        f"📢 <b>Channel:</b> <code>{channel_id}</code>\n"
        f"👤 <b>By:</b> {actor}\n"
        f"🔧 <b>New Gap:</b> <code>{value}</code>"
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
