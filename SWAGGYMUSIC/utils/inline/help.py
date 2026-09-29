#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.
#
# Help panel — standalone channel-management build.
#
# Only three categories remain:
#     hb1 — Set Delay   (/setdelay)
#     hb2 — Set Gap     (/setgap)
#     hb3 — Create Post (/createpost)
# Music-only categories (Play, VPlay, CPlay, Pause/Resume/Skip/Stop, Queue,
# Autoplay, Filters, Thumbnails, Sudo, VC Logger, etc.) have been removed.
# Premium custom-emoji icons have been removed from buttons because they
# caused oversized spacing on some clients.

from typing import Union

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from SWAGGYMUSIC import app


def help_pannel(_, START: Union[bool, int] = None):
    first = [
        InlineKeyboardButton(
            text=_["CLOSE_BUTTON"],
            callback_data="close",
            style=ButtonStyle.DANGER,
        )
    ]
    second = [
        InlineKeyboardButton(
            text=_["BACK_BUTTON"],
            callback_data="settingsback_helper",
            style=ButtonStyle.DANGER,
        ),
    ]
    mark = second if START else first
    upl = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=_["H_B_1"],
                    callback_data="help_callback hb1",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text=_["H_B_2"],
                    callback_data="help_callback hb2",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    text=_["H_B_3"],
                    callback_data="help_callback hb3",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            mark,
        ]
    )
    return upl


def help_back_markup(_):
    upl = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=_["BACK_BUTTON"],
                    callback_data="settings_back_helper",
                    style=ButtonStyle.DANGER,
                ),
            ]
        ]
    )
    return upl


def private_help_panel(_):
    buttons = [
        [
            InlineKeyboardButton(
                text=_["S_B_4"],
                url=f"https://t.me/{app.username}?start=help",
                style=ButtonStyle.PRIMARY,
            ),
        ],
    ]
    return buttons
