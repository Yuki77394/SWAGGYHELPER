#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

from pyrogram import filters
from pyrogram.types import Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.utils.database import add_off, add_on
from SWAGGYMUSIC.utils.decorators.language import language


@app.on_message(filters.command(["logger"]) & SUDOERS)
@language
async def logger_command(client, message: Message, _):
    """Toggle the master activity-log switch (is_on_off(2) / mongodb.onoffper).

    /logger            → show usage (log_1)
    /logger enable    → add_on(2)  → reply log_2
    /logger disable   → add_off(2) → reply log_3
    /logger <other>   → show usage (log_1)

    Permission: SUDOERS only (filters.command(["logger"]) & SUDOERS).
    Non-SUDO users are silently dropped by the filter before this handler runs.
    """

    usage = _["log_1"]

    if len(message.command) != 2:
        return await message.reply_text(usage)

    state = message.text.split(None, 1)[1].strip().lower()

    if state == "enable":
        await add_on(2)
        await message.reply_text(_["log_2"])
    elif state == "disable":
        await add_off(2)
        await message.reply_text(_["log_3"])
    else:
        await message.reply_text(usage)
