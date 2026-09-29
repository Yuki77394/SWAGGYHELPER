#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton

import config
from SWAGGYMUSIC import app


def start_panel(_):
    buttons = [
        [
            InlineKeyboardButton(
                text=_["S_B_1"],
                url=f"https://t.me/{app.username}?startgroup=true",
                style=ButtonStyle.PRIMARY,
            ),
            InlineKeyboardButton(
                text=_["S_B_2"],
                url=config.SUPPORT_CHAT,
                style=ButtonStyle.SUCCESS,
            ),
        ],
    ]
    return buttons


def private_panel(_):
    buttons = [
        [
            InlineKeyboardButton(
                text=_["S_B_3"],
                url=f"https://t.me/{app.username}?startgroup=true",
                style=ButtonStyle.PRIMARY,
            )
        ],
        [
            InlineKeyboardButton(
                text=_["S_B_4"],
                callback_data="settings_back_helper",
                style=ButtonStyle.PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                text="⌯ ᴏᴡɴᴇʀ ⌯",
                user_id=config.OWNER_ID,
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                text="⌯ ɴᴇᴛᴡᴏʀᴋ ⌯",
                url="https://t.me/SpIcYxNeTwOrK",
                style=ButtonStyle.SUCCESS,
            ),
        ],
    ]

    return buttons
