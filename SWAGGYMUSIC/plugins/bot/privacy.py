#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.
#
# /privacy handler — preserved from the original SWAGGYMUSIC code.
# The text in the strings file already mentions SWAGGYMUSIC generically.

from pyrogram import filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.utils.decorators.language import language
from config import BANNED_USERS, PRIVACY_LINK


@app.on_message(filters.command("privacy") & ~BANNED_USERS)
@language
async def privacy_policy(client, message: Message, _):
    buttons = [
        [
            InlineKeyboardButton(
                text=_["PRIVACY_BUTTON"],
                url=PRIVACY_LINK,
                style=ButtonStyle.PRIMARY,
            ),
        ],
    ]
    await message.reply_text(
        _["privacy_1"],
        reply_markup=InlineKeyboardMarkup(buttons),
    )
