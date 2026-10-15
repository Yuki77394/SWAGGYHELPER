#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
Centralized authorization helpers for the SWAGGYHELPER bot.

This module is the single source of truth for "who can do what". Every
command/callback that needs to authorize an action MUST go through one
of the helpers here, so the permission model is consistent across the
whole bot.

=====================================================================
 AUTHORIZATION MATRIX
=====================================================================

  Category            | Who                              | Examples
  ------------------- | -------------------------------- | -------------------
  Public              | Anyone (except banned users)    | /start, /help, /privacy
  Authenticated user  | Any logged-in Telegram user      | /createpost (initiation)
  Channel-admin       | Admin of the TARGET channel      | /setgap, /setdelay
                      |                                  | (in private chat AND in
                      |                                  |  the channel itself),
                      |                                  | /createpost (final send)
  SUDO                | Users in mongodb.sudoers         | /logger
  Owner-only          | config.OWNER_ID only             | (none currently — all
                      |                                  |  owner actions are also
                      |                                  |  SUDO-gated and the owner
                      |                                  |  is always a SUDOer)

=====================================================================
 KEY DESIGN RULES
=====================================================================

1. **Never trust the UI.** Hiding a button is not authorization. Every
   callback handler MUST re-validate permissions when the action is
   executed. A user can craft an arbitrary callback_data string and
   send it to the bot.

2. **Channel-admin verification is done via the Telegram API.** We call
   ``app.get_chat_member(channel_id, user_id)`` and check the member
   status. This is the ONLY reliable way to know whether a user is an
   admin of a channel they do not own. It requires the bot to be a
   member/admin of the target channel.

3. **Fail safely.** If the Telegram API call fails (network error,
   channel inaccessible, user not findable), we DENY the action and
   tell the user what to do. We never fall back to "allow" on an
   API error.

4. **Owner is always a SUDOer.** ``misc.sudo()`` adds ``OWNER_ID`` to
   the SUDOERS filter at startup. The owner can additionally be
   identified directly via ``config.OWNER_ID`` for owner-only actions
   (none currently).

5. **No credentials in errors.** Error messages returned to users and
   written to logs never contain BOT_TOKEN, API_HASH, MONGO_DB_URI,
   etc.

=====================================================================
 CHANNEL-ADMIN VERIFICATION
=====================================================================

``verify_channel_admin(channel_id, user_id)`` returns an ``(ok, reason)``
tuple where ``reason`` is a user-facing HTML string when ``ok`` is
False. It checks:

  a. The bot can access the channel (``app.get_chat`` succeeds).
  b. The bot is an admin with the required permission in the channel
     (default: ``can_delete_messages``; configurable per call).
  c. The user is an admin or the owner of the channel
     (``app.get_chat_member`` returns OWNER or ADMINISTRATOR).

If any step fails, ``ok`` is False and ``reason`` explains what the
user must do. The function NEVER raises — all exceptions are caught
and converted to a safe ``(False, reason)`` return.
"""

from typing import Optional, Tuple

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import RPCError

import config
from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)


# ---------------------------------------------------------------------------
# Identity checks — pure, no I/O
# ---------------------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    """True if ``user_id`` is the configured bot owner.

    The owner is always also a SUDOer (``misc.sudo()`` seeds
    ``OWNER_ID`` into the SUDOERS filter at startup), so
    ``is_sudo(user_id)`` is also True for the owner.
    """
    try:
        return int(user_id) == int(config.OWNER_ID)
    except (TypeError, ValueError):
        return False


def is_sudo(user_id: int) -> bool:
    """True if ``user_id`` is in the SUDOERS filter.

    SUDOERS is populated at startup by ``misc.sudo()`` from
    ``mongodb.sudoers`` and always includes ``config.OWNER_ID``.
    """
    try:
        return bool(user_id in SUDOERS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Channel-admin verification — performs Telegram API calls
# ---------------------------------------------------------------------------

def _bot_can_delete(member) -> bool:
    """True if the bot's ChatMember object grants delete permission."""
    if member.status == ChatMemberStatus.OWNER:
        return True
    privileges = getattr(member, "privileges", None)
    return bool(privileges and privileges.can_delete_messages)


def _bot_can_post(member) -> bool:
    """True if the bot's ChatMember object grants post permission
    (channels only).
    """
    if member.status == ChatMemberStatus.OWNER:
        return True
    privileges = getattr(member, "privileges", None)
    return bool(privileges and privileges.can_post_messages)


def _user_is_admin(member) -> bool:
    """True if the user's ChatMember object indicates admin/owner status."""
    return member.status in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    )


async def verify_channel_admin(
    channel_id: int,
    user_id: int,
    *,
    require_delete: bool = True,
    require_post: bool = False,
) -> Tuple[bool, Optional[str]]:
    """Verify that ``user_id`` is an admin of ``channel_id`` and that the
    bot has the required permissions there.

    Returns ``(True, None)`` on success, or ``(False, reason_html)``
    on failure. ``reason_html`` is a user-facing HTML string that
    explains what the user must do.

    Parameters
    ----------
    channel_id : int
        The target channel/chat ID. Must be a chat the bot can access.
    user_id : int
        The Telegram user ID claiming to be an admin.
    require_delete : bool
        If True (default), the bot must have ``can_delete_messages``
        in the channel.
    require_post : bool
        If True, the bot must have ``can_post_messages`` (channels
        only). Set this for /createpost-style flows where the bot
        will POST to the channel.

    The function NEVER raises. All API errors are converted to a
    safe ``(False, reason)`` return.
    """
    # ─── 1. Validate IDs ──────────────────────────────────────────────
    try:
        channel_id = int(channel_id)
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False, "❌ <b>Invalid channel or user ID.</b>"

    # ─── 2. Bot must be able to access the chat ───────────────────────
    try:
        chat = await app.get_chat(channel_id)
    except Exception as exc:
        # Common cases: CHAT_ID_INVALID, CHAT_INVALID, PEER_ID_INVALID,
        # ChannelPrivate (kicked/left), network errors.
        log.info(
            "verify_channel_admin: bot cannot access chat %s for user %s: %s: %s",
            channel_id, user_id, type(exc).__name__, exc,
        )
        return False, (
            "❌ <b>I can't access that chat.</b>\n\n"
            "Make sure I'm a member of the chat (and an admin, if it's a "
            "channel). If the chat is private, add me as an admin first.\n\n"
            f"<code>{type(exc).__name__}</code>"
        )

    # ─── 3. Bot's own permissions in the chat ─────────────────────────
    try:
        bot_member = await app.get_chat_member(channel_id, app.id)
    except Exception as exc:
        log.warning(
            "verify_channel_admin: cannot fetch bot's membership in %s: %s: %s",
            channel_id, type(exc).__name__, exc,
        )
        return False, (
            "❌ <b>I couldn't verify my own permissions in that chat.</b>\n\n"
            "Please try again in a moment. If the problem persists, re-add "
            "me as an admin in the chat."
        )

    if require_delete and not _bot_can_delete(bot_member):
        return False, (
            "❌ <b>I need <i>Delete Messages</i> permission in that chat.</b>\n\n"
            "Please promote me as an admin with the <b>Delete Messages</b> "
            "right enabled, then try again."
        )

    if require_post and not _bot_can_post(bot_member):
        return False, (
            "❌ <b>I need <i>Post Messages</i> permission in that channel.</b>\n\n"
            "Please promote me as an admin with the <b>Post Messages</b> "
            "right enabled, then try again."
        )

    # ─── 4. Owner bypass — owner can manage any channel the bot is in ─
    # The owner is always trusted for any channel the bot can access.
    # This preserves the owner's global administrative control without
    # requiring the owner to be an admin of every single channel.
    if is_owner(user_id):
        return True, None

    # ─── 5. User's admin status in the chat ───────────────────────────
    try:
        user_member = await app.get_chat_member(channel_id, user_id)
    except Exception as exc:
        log.info(
            "verify_channel_admin: cannot fetch user %s membership in %s: %s: %s",
            user_id, channel_id, type(exc).__name__, exc,
        )
        return False, (
            "❌ <b>I couldn't verify your admin status in that chat.</b>\n\n"
            "This usually means I don't have permission to see the chat's "
            "member list. Make sure I'm an admin with <b>Invite Users</b> "
            "or <b>Manage Chat</b> rights, or that you are visibly an admin "
            "in the chat.\n\n"
            f"<code>{type(exc).__name__}</code>"
        )

    if not _user_is_admin(user_member):
        return False, (
            "❌ <b>You are not an admin of that chat.</b>\n\n"
            "Only the channel owner or an administrator can manage this "
            "bot's settings for that chat."
        )

    return True, None


async def user_can_manage_channel(
    channel_id: int,
    user_id: int,
    *,
    require_delete: bool = True,
) -> bool:
    """Boolean wrapper around :func:`verify_channel_admin`.

    Use this when you don't need the user-facing reason string (e.g.
    inside a tight guard). Returns True/False only.
    """
    ok, _reason = await verify_channel_admin(
        channel_id, user_id, require_delete=require_delete,
    )
    return ok


__all__ = [
    "is_owner",
    "is_sudo",
    "verify_channel_admin",
    "user_can_manage_channel",
]
