#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/KURIGRAMSWAG > project,
# and is released under the "GNU v3.0 License Agreement".
# Please see < https://github.com/Yuki77394/KURIGRAMSWAG/blob/master/LICENSE >
# All rights reserved.

from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.utils.database import get_lang, is_maintenance
from config import SUPPORT_CHAT
from strings import get_string


def language(mystic):
    async def wrapper(_, message, **kwargs):
        if await is_maintenance() is False:
            if message.from_user.id not in SUDOERS:
                return await message.reply_text(
                    text=(
                        f"{app.mention} ɪs ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ, ᴠɪsɪᴛ "
                        f"<a href={SUPPORT_CHAT}>sᴜᴘᴘᴏʀᴛ ᴄʜᴀᴛ</a> ғᴏʀ ᴋɴᴏᴡɪɴɢ "
                        "ᴛʜᴇ ʀᴇᴀsᴏɴ."
                    ),
                    disable_web_page_preview=True,
                )
        try:
            await message.delete()
        except Exception:
            pass

        try:
            language_str = get_string(await get_lang(message.chat.id))
        except Exception:
            language_str = get_string("en")
        return await mystic(_, message, language_str)

    return wrapper


def languageCB(mystic):
    async def wrapper(_, CallbackQuery, **kwargs):
        if await is_maintenance() is False:
            if CallbackQuery.from_user.id not in SUDOERS:
                return await CallbackQuery.answer(
                    f"{app.mention} ɪs ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ, ᴠɪsɪᴛ sᴜᴘᴘᴏʀᴛ ᴄʜᴀᴛ ғᴏʀ "
                    "ᴋɴᴏᴡɪɴɢ ᴛʜᴇ ʀᴇᴀsᴏɴ.",
                    show_alert=True,
                )
        try:
            language_str = get_string(await get_lang(CallbackQuery.message.chat.id))
        except Exception:
            language_str = get_string("en")
        return await mystic(_, CallbackQuery, language_str)

    return wrapper


def LanguageStart(mystic):
    async def wrapper(_, message, **kwargs):
        try:
            language_str = get_string(await get_lang(message.chat.id))
        except Exception:
            language_str = get_string("en")
        return await mystic(_, message, language_str)

    return wrapper
