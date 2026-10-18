#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.
#
# /help handler — standalone channel-management build.
#
# The Help panel exposes the five channel-management features
# (2 rows x 2 buttons, plus the Link Restriction row):
#     Set Delay   (hb1)      Set Gap        (hb2)
#     Create Post (hb3)      Anonymous Post (hb4, /setanonymous)
#     Link Restriction (hb5, /setlink)
# All music-related buttons and helper pages have been removed.

from typing import Union

from pyrogram import filters, types
from pyrogram.types import InlineKeyboardMarkup, Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.utils import help_pannel
from SWAGGYMUSIC.utils.database import get_lang
from SWAGGYMUSIC.utils.decorators.language import LanguageStart, languageCB
from SWAGGYMUSIC.utils.inline.help import help_back_markup, private_help_panel
from config import BANNED_USERS, START_IMG_URL, SUPPORT_CHAT
from strings import get_string, helpers


# Telegram messages containing media (the private Help menu is a photo with a
# caption) cannot be edited with edit_message_text(). Keep the original media
# and edit its caption instead; text-only group/help messages use edit text.
_CAPTION_MEDIA_FIELDS = (
    "photo", "video", "animation", "document", "audio", "voice",
)


def _message_has_caption_media(message) -> bool:
    return any(
        getattr(message, field, None) is not None
        for field in _CAPTION_MEDIA_FIELDS
    )


async def _edit_callback_message(update, text: str, reply_markup) -> None:
    message = getattr(update, "message", None)
    if message is not None and _message_has_caption_media(message):
        await update.edit_message_caption(caption=text, reply_markup=reply_markup)
    else:
        await update.edit_message_text(text=text, reply_markup=reply_markup)


@app.on_message(filters.command(["help"]) & filters.private & ~BANNED_USERS)
@app.on_callback_query(filters.regex("settings_back_helper") & ~BANNED_USERS)
async def helper_private(
    client: app, update: Union[types.Message, types.CallbackQuery]
):
    is_callback = isinstance(update, types.CallbackQuery)
    if is_callback:
        try:
            await update.answer()
        except Exception:
            pass
        chat_id = update.message.chat.id
        language = await get_lang(chat_id)
        _ = get_string(language)
        keyboard = help_pannel(_, True)
        await _edit_callback_message(
            update, _["help_1"].format(SUPPORT_CHAT), reply_markup=keyboard
        )
    else:
        try:
            await update.delete()
        except Exception:
            pass
        language = await get_lang(update.chat.id)
        _ = get_string(language)
        keyboard = help_pannel(_)
        await update.reply_photo(
            photo=START_IMG_URL,
            has_spoiler=True,
            caption=_["help_1"].format(SUPPORT_CHAT),
            reply_markup=keyboard,
        )


@app.on_message(filters.command(["help"]) & filters.group & ~BANNED_USERS)
@LanguageStart
async def help_com_group(client, message: Message, _):
    keyboard = private_help_panel(_)
    await message.reply_text(_["help_2"], reply_markup=InlineKeyboardMarkup(keyboard))


@app.on_callback_query(filters.regex("help_callback") & ~BANNED_USERS)
@languageCB
async def helper_cb(client, CallbackQuery, _):
    try:
        await CallbackQuery.answer()
    except Exception:
        pass

    callback_data = (CallbackQuery.data or "").strip()
    parts = callback_data.split(None, 1)
    if len(parts) != 2:
        return
    cb = parts[1]
    keyboard = help_back_markup(_)
    page = {
        "hb1": helpers.HELP_1,
        "hb2": helpers.HELP_2,
        "hb3": helpers.HELP_3,
        "hb4": helpers.HELP_4,
        "hb5": helpers.HELP_5,
    }.get(cb)
    if page is not None:
        await _edit_callback_message(CallbackQuery, page, reply_markup=keyboard)


@app.on_callback_query(filters.regex("close") & ~BANNED_USERS)
async def close_helper(client, CallbackQuery):
    try:
        await CallbackQuery.answer()
    except Exception:
        pass
    try:
        await CallbackQuery.message.delete()
    except Exception:
        pass
