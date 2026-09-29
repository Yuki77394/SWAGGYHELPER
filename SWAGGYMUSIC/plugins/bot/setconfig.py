#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.
#
# Private-chat SETGAP / SETDELAY management wizard
# -----------------------------------------------
#
# This plugin provides the PRIVATE-CHAT interface for managing a channel's
# SETGAP / SETDELAY configuration.  It is the ONLY place from which a
# channel's SETGAP or SETDELAY can be DISABLED.
#
# Architecture distinction:
#
#   CHANNEL (plugins/misc/autodelete.py):
#     /setgap 5m     → ENABLE / UPDATE  (command auto-deleted, response auto-deleted after 120s)
#     /setdelay 10m  → ENABLE / UPDATE  (command auto-deleted, response auto-deleted after 120s)
#     /setgap off    → SILENT IGNORE    (command deleted, no response, no DB change)
#     /setdelay off  → SILENT IGNORE    (command deleted, no response, no DB change)
#
#   PRIVATE BOT CHAT (this file):
#     /setgap   → Channel ID → [🟢 ENABLE] [🔴 DISABLE] → duration (for ENABLE)
#     /setdelay → Channel ID → [🟢 ENABLE] [🔴 DISABLE] → duration (for ENABLE)
#
# The private chat supports BOTH enable and disable.  The channel only
# supports enable/update.  This is enforced in backend logic, not just UI.
#
# Permission: SUDOERS only.  Uses the existing SUDOERS mechanism from
# SWAGGYMUSIC.misc — no new permission system.
#
# Session handling: in-memory per-user sessions (SESSIONS dict), same pattern
# as plugins/misc/createpost.py.  Sessions are cleaned up on cancel,
# success, invalid flow, or any error.  No permanent MongoDB data for
# temporary state.

import asyncio
import re
from contextlib import suppress

from pyrogram import filters
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.errors import RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils.activity_logger import (_format_channel_id,
                                               _format_channel_title,
                                               _format_channel_username,
                                               _format_duration,
                                               log_setdelay_change,
                                               log_setgap_change)
from SWAGGYMUSIC.utils.database import settings_db

log = LOGGER(__name__)

# Duration parser — same limits as autodelete.py (3s .. 24h)
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)
MIN_SECONDS = 3
MAX_SECONDS = 24 * 60 * 60


SESSIONS = {}
LOCK = asyncio.Lock()
PREFIX = "setconfig"


# This filter only matches messages from users who are inside an active
# /setgap or /setdelay private-chat session.  It prevents this wizard from
# swallowing other commands.
ACTIVE_SESSION = filters.create(
    lambda _, __, message: bool(
        message.from_user
        and message.from_user.id in SESSIONS
        and message.chat.type == ChatType.PRIVATE
    )
)


# ---------------------------------------------------------------------------
# Duration parser (same logic as autodelete._parse_duration)
# ---------------------------------------------------------------------------

def _parse_duration(value: str):
    match = _DURATION_RE.fullmatch(value or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
    if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        return None
    return seconds


INVALID_DURATION_TEXT = (
    "❌ <b>Invalid duration.</b>\n\n"
    "Use: <code>30s</code> or <code>5m</code> or <code>1h</code>\n"
    "Minimum: 3 seconds\n"
    "Maximum: 24 hours"
)


# ---------------------------------------------------------------------------
# Bot admin verification (same as autodelete._bot_admin_status / _bot_can_delete)
# ---------------------------------------------------------------------------

async def _bot_can_delete(chat_id: int) -> bool:
    try:
        member = await app.get_chat_member(chat_id, app.id)
        if member.status == ChatMemberStatus.OWNER:
            return True
        privileges = member.privileges
        return bool(privileges and privileges.can_delete_messages)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _action_menu(feature: str, channel_id: int):
    """ENABLE / DISABLE / CANCEL keyboard for the private-chat wizard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="🟢 ENABLE",
                    callback_data=f"{PREFIX}:enable:{feature}:{channel_id}",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    text="🔴 DISABLE",
                    callback_data=f"{PREFIX}:disable:{feature}:{channel_id}",
                    style=ButtonStyle.DANGER,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ CANCEL",
                    callback_data=f"{PREFIX}:cancel:{feature}",
                    style=ButtonStyle.DANGER,
                ),
            ],
        ]
    )


def _cancel_menu(feature: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="❌ CANCEL",
                    callback_data=f"{PREFIX}:cancel:{feature}",
                    style=ButtonStyle.DANGER,
                ),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# /setgap — private chat entry point
# ---------------------------------------------------------------------------

@app.on_message(
    filters.command(["setgap"]) & filters.private & SUDOERS
)
async def setgap_private_start(_, message: Message):
    """Private-chat /setgap wizard — ask for the Channel ID."""
    user_id = message.from_user.id

    async with LOCK:
        SESSIONS[user_id] = {"feature": "setgap", "step": "channel_id"}

    await message.reply_text(
        "⚙️ <b>SETGAP CONFIGURATION</b>\n\n"
        "Please enter the <b>Channel ID</b> you want to manage.\n\n"
        "<i>Example: <code>-100123456789</code></i>",
        reply_markup=_cancel_menu("setgap"),
    )


# ---------------------------------------------------------------------------
# /setdelay — private chat entry point
# ---------------------------------------------------------------------------

@app.on_message(
    filters.command(["setdelay"]) & filters.private & SUDOERS
)
async def setdelay_private_start(_, message: Message):
    """Private-chat /setdelay wizard — ask for the Channel ID."""
    user_id = message.from_user.id

    async with LOCK:
        SESSIONS[user_id] = {"feature": "setdelay", "step": "channel_id"}

    await message.reply_text(
        "⚙️ <b>SETDELAY CONFIGURATION</b>\n\n"
        "Please enter the <b>Channel ID</b> you want to manage.\n\n"
        "<i>Example: <code>-100123456789</code></i>",
        reply_markup=_cancel_menu("setdelay"),
    )


# ---------------------------------------------------------------------------
# /cancel — explicit cancel for private-chat sessions
# ---------------------------------------------------------------------------

@app.on_message(filters.command(["cancel"]) & filters.private & SUDOERS)
async def setconfig_cancel_cmd(_, message: Message):
    user_id = message.from_user.id
    if user_id in SESSIONS:
        SESSIONS.pop(user_id, None)
        await message.reply_text("❌ <b>Configuration cancelled.</b>")
    else:
        await message.reply_text("❌ No active configuration session.")


# ---------------------------------------------------------------------------
# Callback handler — ENABLE / DISABLE / CANCEL buttons
# ---------------------------------------------------------------------------

@app.on_callback_query(filters.regex(rf"^{PREFIX}:"))
async def setconfig_callbacks(_, query: CallbackQuery):
    if not query.from_user:
        return await query.answer("Not available.", show_alert=True)

    user_id = query.from_user.id
    session = SESSIONS.get(user_id)

    data = query.data or ""

    # ─── CANCEL ───────────────────────────────────────────────────
    if data.startswith(f"{PREFIX}:cancel:"):
        feature = data.rsplit(":", 1)[1]
        SESSIONS.pop(user_id, None)
        await query.answer("Cancelled")
        with suppress(Exception):
            await query.message.edit_text("❌ <b>Configuration cancelled.</b>")
        return

    # Session must exist for any other callback
    if not session:
        return await query.answer(
            "This setup has expired. Use /setgap or /setdelay again.",
            show_alert=True,
        )

    feature = session.get("feature")

    # ─── ENABLE ────────────────────────────────────────────────────
    if data.startswith(f"{PREFIX}:enable:"):
        parts = data.split(":")
        if len(parts) < 4:
            return await query.answer("Invalid callback.", show_alert=True)

        cb_feature = parts[2]
        if cb_feature != feature:
            return await query.answer(
                f"This button is for /{feature}. Please use the correct wizard.",
                show_alert=True,
            )

        try:
            channel_id = int(parts[3])
        except ValueError:
            return await query.answer("Invalid Channel ID.", show_alert=True)

        session["channel_id"] = channel_id
        session["step"] = "duration"

        await query.answer("Enable selected")
        with suppress(Exception):
            header = "🟢 SETGAP ENABLE" if feature == "setgap" else "🟢 SETDELAY ENABLE"
            field = "gap" if feature == "setgap" else "deletion delay"
            await query.message.edit_text(
                f"<b>{header}</b>\n\n"
                f"Please enter the <b>{field} duration</b>.\n\n"
                "<i>Examples:\n<code>30s</code>\n<code>5m</code>\n<code>10m</code>\n<code>1h</code></i>",
                reply_markup=_cancel_menu(feature),
            )
        return

    # ─── DISABLE ───────────────────────────────────────────────────
    if data.startswith(f"{PREFIX}:disable:"):
        parts = data.split(":")
        if len(parts) < 4:
            return await query.answer("Invalid callback.", show_alert=True)

        cb_feature = parts[2]
        if cb_feature != feature:
            return await query.answer(
                f"This button is for /{feature}. Please use the correct wizard.",
                show_alert=True,
            )

        try:
            channel_id = int(parts[3])
        except ValueError:
            return await query.answer("Invalid Channel ID.", show_alert=True)

        await query.answer("Disabling…")

        # Resolve the chat for logging
        chat = None
        try:
            chat = await app.get_chat(channel_id)
        except Exception:
            pass

        # Perform the disable using the existing database layer
        success = await _do_disable(feature, channel_id)

        if success:
            ch_title = _format_channel_title(chat) if chat else "Not Available"
            ch_id = channel_id
            header = "🔴 SETGAP DISABLED" if feature == "setgap" else "🔴 SETDELAY DISABLED"

            with suppress(Exception):
                await query.message.edit_text(
                    f"<b>{header}</b>\n\n"
                    f"📌 <b>Channel:</b> {ch_title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{ch_id}</code>\n\n"
                    f"{'SETGAP' if feature == 'setgap' else 'SETDELAY'} has been disabled successfully.",
                )

            # Activity log — source=PRIVATE CHAT
            with suppress(Exception):
                if feature == "setgap":
                    await log_setgap_change(
                        chat, query.from_user, "DISABLED",
                        source="PRIVATE CHAT",
                    )
                else:
                    await log_setdelay_change(
                        chat, query.from_user, "DISABLED",
                        source="PRIVATE CHAT",
                    )
        else:
            with suppress(Exception):
                await query.message.edit_text(
                    "❌ <b>Failed to disable.</b>\n\n"
                    "The configuration may not have been enabled, "
                    "or the channel is inaccessible.",
                )

        SESSIONS.pop(user_id, None)
        return

    await query.answer("Unknown option.", show_alert=True)


# ---------------------------------------------------------------------------
# Text input handler — Channel ID + Duration
# ---------------------------------------------------------------------------

@app.on_message(
    ACTIVE_SESSION
    & ~filters.service
    & ~filters.command("setgap")
    & ~filters.command("setdelay")
    & ~filters.command("cancel")
)
async def setconfig_input(_, message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id
    session = SESSIONS.get(user_id)
    if not session:
        return

    step = session.get("step")
    feature = session.get("feature", "")
    raw = (message.text or message.caption or "").strip()

    # ─── Channel ID input ──────────────────────────────────────────
    if step == "channel_id":
        try:
            channel_id = int(raw)
        except (TypeError, ValueError):
            return await message.reply_text(
                "❌ <b>Invalid Channel ID.</b>\n\n"
                "Send a numeric Channel ID, for example:\n"
                "<code>-100123456789</code>"
            )

        # Verify the bot can access the channel
        chat = None
        try:
            chat = await app.get_chat(channel_id)
        except Exception as exc:
            return await message.reply_text(
                "❌ <b>Cannot access that channel.</b>\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:300]}</code>"
            )

        if not await _bot_can_delete(channel_id):
            return await message.reply_text(
                "❌ <b>I need Delete Messages permission in that channel.</b>\n\n"
                "Please add the bot as an admin with Delete Messages rights."
            )

        session["channel_id"] = channel_id
        session["step"] = "action"

        ch_title = _format_channel_title(chat)
        ch_uname = _format_channel_username(chat)

        await message.reply_text(
            "📌 <b>Channel:</b> " + ch_title + "\n"
            f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n"
            "🔗 <b>Username:</b> " + ch_uname + "\n\n"
            "<b>Choose an action:</b>",
            reply_markup=_action_menu(feature, channel_id),
        )
        return

    # ─── Duration input ─────────────────────────────────────────────
    if step == "duration":
        seconds = _parse_duration(raw)
        if seconds is None:
            return await message.reply_text(
                INVALID_DURATION_TEXT,
                reply_markup=_cancel_menu(feature),
            )

        channel_id = session.get("channel_id")
        if not channel_id:
            SESSIONS.pop(user_id, None)
            return await message.reply_text(
                "❌ Session expired. Please use /setgap or /setdelay again."
            )

        # Resolve chat for logging
        chat = None
        try:
            chat = await app.get_chat(channel_id)
        except Exception:
            pass

        # Perform the enable/update using the existing database layer
        status = await _do_enable(feature, channel_id, seconds)

        ch_title = _format_channel_title(chat) if chat else "Not Available"
        duration_str = _format_duration(seconds)
        header = "🟢 SETGAP ENABLED" if feature == "setgap" else "🟢 SETDELAY ENABLED"
        field = "Gap" if feature == "setgap" else "Delay"

        if status == "ENABLED":
            with suppress(Exception):
                await message.reply_text(
                    f"<b>{header}</b>\n\n"
                    f"📌 <b>Channel:</b> {ch_title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    f"⏱ <b>{field}:</b> <code>{duration_str}</code>\n\n"
                    f"{'SETGAP' if feature == 'setgap' else 'SETDELAY'} has been enabled successfully.",
                )
            with suppress(Exception):
                if feature == "setgap":
                    await log_setgap_change(
                        chat, message.from_user, "ENABLED",
                        gap_value=duration_str,
                        source="PRIVATE CHAT",
                    )
                else:
                    await log_setdelay_change(
                        chat, message.from_user, "ENABLED",
                        delay_value=duration_str,
                        source="PRIVATE CHAT",
                    )

        elif status == "UPDATED":
            old_val = _get_old_value(channel_id, feature)
            header_upd = "⚙️ SETGAP UPDATED" if feature == "setgap" else "⚙️ SETDELAY UPDATED"
            with suppress(Exception):
                await message.reply_text(
                    f"<b>{header_upd}</b>\n\n"
                    f"📌 <b>Channel:</b> {ch_title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    f"⏱ <b>Previous {field}:</b> <code>{old_val}</code>\n"
                    f"⏱ <b>New {field}:</b> <code>{duration_str}</code>\n\n"
                    f"{'SETGAP' if feature == 'setgap' else 'SETDELAY'} has been updated successfully.",
                )
            with suppress(Exception):
                if feature == "setgap":
                    await log_setgap_change(
                        chat, message.from_user, "UPDATED",
                        gap_value=duration_str,
                        old_value=old_val,
                        source="PRIVATE CHAT",
                    )
                else:
                    await log_setdelay_change(
                        chat, message.from_user, "UPDATED",
                        delay_value=duration_str,
                        old_value=old_val,
                        source="PRIVATE CHAT",
                    )

        elif status == "NO_CHANGE":
            # Same value was already configured — no state change, no log.
            with suppress(Exception):
                await message.reply_text(
                    f"ℹ️ <b>No change needed.</b>\n\n"
                    f"📌 <b>Channel:</b> {ch_title}\n"
                    f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
                    f"{'SETGAP' if feature == 'setgap' else 'SETDELAY'} is already set to "
                    f"<code>{duration_str}</code>."
                )

        else:
            with suppress(Exception):
                await message.reply_text(
                    "❌ <b>Configuration failed.</b>\n\n"
                    "Please try again."
                )

        SESSIONS.pop(user_id, None)
        return


# ---------------------------------------------------------------------------
# Database operations — reuse the existing schema and worker
# ---------------------------------------------------------------------------

async def _do_enable(feature: str, channel_id: int, seconds: int) -> str:
    """Enable or update SETGAP/SETDELAY for a channel.

    Returns "ENABLED", "UPDATED", or None on failure.
    Reuses the existing mongodb.autodelete_settings collection and schema.
    """
    try:
        prior = await settings_db.find_one({"chat_id": channel_id})
        if feature == "setgap":
            was_enabled = bool(prior and prior.get("gap_enabled"))
            old_val = int(prior.get("gap") or 0) if was_enabled else 0

            await settings_db.update_one(
                {"chat_id": channel_id},
                {
                    "$set": {
                        "chat_id": channel_id,
                        "gap": seconds,
                        "gap_enabled": True,
                    },
                    "$unset": {"last_gap_post_at": ""},
                },
                upsert=True,
            )

            if not was_enabled:
                return "ENABLED"
            elif old_val != seconds:
                # Store old value for the caller to use in the log
                _tmp = _format_duration(old_val)
                _set_old_value(channel_id, feature, _tmp)
                return "UPDATED"
            else:
                return "NO_CHANGE"  # Same value — no state change

        else:  # setdelay
            was_enabled = bool(prior and prior.get("enabled"))
            old_val = int(prior.get("delay") or 0) if was_enabled else 0

            await settings_db.update_one(
                {"chat_id": channel_id},
                {
                    "$set": {
                        "chat_id": channel_id,
                        "delay": seconds,
                        "enabled": True,
                    }
                },
                upsert=True,
            )

            if not was_enabled:
                return "ENABLED"
            elif old_val != seconds:
                _tmp = _format_duration(old_val)
                _set_old_value(channel_id, feature, _tmp)
                return "UPDATED"
            else:
                return "NO_CHANGE"  # Same value — no state change

    except Exception as exc:
        log.warning("_do_enable failed: %s: %s", type(exc).__name__, exc)
        return None


# Side-channel for passing old_value from _do_enable to the caller.
# This avoids changing the return type of _do_enable.
_OLD_VALUES = {}

def _set_old_value(channel_id: int, feature: str, old_val: str):
    _OLD_VALUES[(channel_id, feature)] = old_val

def _get_old_value(channel_id: int, feature: str) -> str:
    return _OLD_VALUES.pop((channel_id, feature), "N/A")


async def _do_disable(feature: str, channel_id: int) -> bool:
    """Disable SETGAP/SETDELAY for a channel.

    This is the ONLY code path that can disable these features.
    Channel-side /setgap off and /setdelay off are silently ignored.
    """
    try:
        if feature == "setgap":
            await settings_db.update_one(
                {"chat_id": channel_id},
                {"$unset": {
                    "gap_enabled": "", "gap": "", "last_gap_post_at": "",
                }},
            )
            remaining = await settings_db.find_one({"chat_id": channel_id})
            if remaining and not remaining.get("enabled") and "delay" not in remaining:
                await settings_db.delete_one({"chat_id": channel_id})
        else:  # setdelay
            await settings_db.update_one(
                {"chat_id": channel_id},
                {"$unset": {"enabled": "", "delay": ""}},
            )
            remaining = await settings_db.find_one({"chat_id": channel_id})
            if remaining and not remaining.get("gap_enabled") and "gap" not in remaining:
                await settings_db.delete_one({"chat_id": channel_id})
        return True
    except Exception as exc:
        log.warning("_do_disable failed: %s: %s", type(exc).__name__, exc)
        return False
