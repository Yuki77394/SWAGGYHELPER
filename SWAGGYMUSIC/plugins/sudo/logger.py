#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

from contextlib import suppress

from pyrogram import filters
from pyrogram.types import Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.utils.activity_logger import log_logger_toggle
from SWAGGYMUSIC.utils.database import add_off, add_on
from SWAGGYMUSIC.utils.decorators.language import language


@app.on_message(filters.command(["logger"]) & SUDOERS)
@language
async def logger_command(client, message: Message, _):
    """Toggle the master activity-log switch (is_on_off(2) / mongodb.onoffper).

    /logger            → show usage (log_1)
    /logger enable    → add_on(2)  → reply log_2 → log_logger_toggle("ENABLED")
    /logger disable   → log_logger_toggle("DISABLED") → add_off(2) → reply log_3
    /logger <other>   → show usage (log_1)

    Permission: SUDOERS only (filters.command(["logger"]) & SUDOERS).
    Non-SUDO users are silently dropped by the filter before this handler runs.

    Self-logging ordering (avoids recursive loops):
      - enable:  add_on(2) FIRST, then log — toggle is ON, so log fires.
      - disable: log FIRST, then add_off(2) — toggle is still ON, so log fires.
    No recursion because log_logger_toggle() calls log_activity() which calls
    app.send_message() — no command handler is triggered.
    """

    usage = _["log_1"]

    if len(message.command) != 2:
        return await message.reply_text(usage)

    state = message.text.split(None, 1)[1].strip().lower()

    if state == "enable":
        await add_on(2)
        await message.reply_text(_["log_2"])
        # Log the toggle itself — toggle is now ON, so log fires.
        with suppress(Exception):
            await log_logger_toggle(message.from_user, "ENABLED")

    elif state == "disable":
        # Log BEFORE disabling — toggle is still ON, so log fires.
        with suppress(Exception):
            await log_logger_toggle(message.from_user, "DISABLED")
        await add_off(2)
        await message.reply_text(_["log_3"])

    else:
        await message.reply_text(usage)
