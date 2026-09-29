#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
#
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
#
# All rights reserved.

import asyncio
import importlib

from pyrogram import idle

import config
from SWAGGYMUSIC import LOGGER, app
from SWAGGYMUSIC.misc import sudo
from SWAGGYMUSIC.plugins import ALL_MODULES
from SWAGGYMUSIC.utils.database import ensure_channel_indexes
from config import BANNED_USERS


async def init():
    if not config.BOT_TOKEN:
        LOGGER(__name__).error("BOT_TOKEN is not set, exiting...")
        exit()

    await sudo()

    # Re-hydrate the in-memory banned/gbanned filter so previously
    # blocked users stay blocked across restarts.
    try:
        from SWAGGYMUSIC.utils.database import get_banned_users, get_gbanned

        for user_id in await get_gbanned():
            BANNED_USERS.add(user_id)
        for user_id in await get_banned_users():
            BANNED_USERS.add(user_id)
    except Exception:
        pass

    # Initialize MongoDB indexes for the channel-management collections
    # (autodelete_settings / autodelete_jobs). Idempotent on every boot.
    try:
        await ensure_channel_indexes()
        LOGGER("SWAGGYMUSIC.autodelete").info(
            "Channel-management indexes ready (autodelete_settings, autodelete_jobs)."
        )
    except Exception as e:
        LOGGER("SWAGGYMUSIC.autodelete").warning(
            f"Could not initialize channel-management indexes: {type(e).__name__}: {e}"
        )

    await app.start()

    for module_name in ALL_MODULES:
        importlib.import_module("SWAGGYMUSIC.plugins" + module_name)

    LOGGER("SWAGGYMUSIC.plugins").info("Successfully Imported Modules...")

    LOGGER("SWAGGYMUSIC").info(
        "ʙᴏᴛ sᴛᴀʀᴛᴇᴅ sᴜᴄᴄᴇssғᴜʟʟʏ — sᴛᴀɴᴅᴀʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ-ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴜᴛɪʟɪᴛʏ"
    )

    await idle()

    await app.stop()

    LOGGER("SWAGGYMUSIC").info(
        "Stopping 𝚮հҽ 𝚨Łꪮⲛ𝛆 🚩𝗧ε᧘‌ᴍ Bot..."
    )


def main():
    """Console entry point for packaged deployments."""
    asyncio.get_event_loop().run_until_complete(init())


if __name__ == "__main__":
    main()
