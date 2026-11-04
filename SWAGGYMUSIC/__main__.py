#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
#
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
#
# All rights reserved.

import asyncio
import importlib

from pyrogram import idle

import config
from SWAGGYMUSIC import LOGGER, app
from SWAGGYMUSIC.core.mongo import verify_mongo_connection
from SWAGGYMUSIC.misc import sudo
from SWAGGYMUSIC.plugins import ALL_MODULES
from SWAGGYMUSIC.utils.database import ensure_channel_indexes
from config import BANNED_USERS


async def init():
    if not config.BOT_TOKEN:
        LOGGER(__name__).error("BOT_TOKEN is not set, exiting...")
        exit()

    # ─── Configuration validation gate ─────────────────────────────────
    # Validate ALL required config values BEFORE the MongoDB ping and
    # BEFORE app.start(). A missing API_ID / API_HASH / BOT_TOKEN /
    # MONGO_DB_URI / OWNER_ID would otherwise produce a confusing
    # Pyrogram or Motor error deep in startup. The validator raises
    # SystemExit with a clean, credential-free message naming only the
    # missing/invalid variables. See config.validate_config_for_startup.
    try:
        config.validate_config_for_startup()
    except SystemExit as e:
        LOGGER(__name__).error("%s", e)
        raise

    # ─── MongoDB connectivity gate ───────────────────────────────────
    # The Motor client is constructed lazily and never performs I/O at
    # import time. We MUST ping the server before doing anything else,
    # so a misconfigured / unreachable MONGO_DB_URI fails fast at boot
    # instead of producing confusing errors deep inside a handler.
    # See SWAGGYMUSIC/core/mongo.py:verify_mongo_connection.
    try:
        await verify_mongo_connection()
        LOGGER(__name__).info("MongoDB connection verified (ping OK).")
    except Exception as e:
        LOGGER(__name__).error(
            "MongoDB connection verification failed at startup: %s",
            e,
        )
        exit(1)

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
    # If the essential indexes cannot be created, ensure_channel_indexes
    # raises — the bot cannot function safely without the unique
    # constraints, so we abort here rather than continuing in a
    # degraded state that would silently allow duplicate jobs.
    try:
        await ensure_channel_indexes()
        LOGGER("SWAGGYMUSIC.autodelete").info(
            "Channel-management indexes ready (autodelete_settings, autodelete_jobs)."
        )
    except Exception as e:
        LOGGER("SWAGGYMUSIC.autodelete").error(
            "Could not initialize channel-management indexes: %s: %s "
            "— aborting startup (the unique constraints on chat_id and "
            "(chat_id, message_id) are required for correctness).",
            type(e).__name__, e,
        )
        exit(1)

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
