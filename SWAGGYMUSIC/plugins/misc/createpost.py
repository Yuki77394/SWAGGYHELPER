"""
SWAGGYMUSIC - Interactive Post Creator

Command: /createpost

Two post types:
1. Hidden Video Preview - manually written text + external MP4 URL.
2. Normal Post - existing Format 1 / Format 2 flow.

Sessions are kept in memory and are cleared if the bot restarts.
"""

import asyncio
import random
import re
from contextlib import suppress

from pyrogram import filters, raw
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.errors import RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from SWAGGYMUSIC import app


SESSIONS = {}
LOCK = asyncio.Lock()
PREFIX = "createpost"


# IMPORTANT: This filter only matches messages from users who are actually
# inside an active /createpost session. It prevents this wizard from
# swallowing /play, /ping, /song, etc. for everyone else.
ACTIVE_SESSION = filters.create(
    lambda _, __, message: bool(
        message.from_user and message.from_user.id in SESSIONS
    )
)


def _post_type_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎬 Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ",
                    callback_data=f"{PREFIX}:type:hidden",
                )
            ],
            [
                InlineKeyboardButton(
                    "🖼 Nᴏʀᴍᴀʟ Pᴏsᴛ",
                    callback_data=f"{PREFIX}:type:normal",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cᴀɴᴄᴇʟ",
                    callback_data=f"{PREFIX}:cancel",
                )
            ],
        ]
    )


def _format_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Fᴏʀᴍᴀᴛ 1",
                    callback_data=f"{PREFIX}:format:1",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Fᴏʀᴍᴀᴛ 2",
                    callback_data=f"{PREFIX}:format:2",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cᴀɴᴄᴇʟ",
                    callback_data=f"{PREFIX}:cancel",
                )
            ],
        ]
    )


def _preview_position_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬆️ Vɪᴅᴇᴏ Aʙᴏᴠᴇ Tᴇxᴛ",
                    callback_data=f"{PREFIX}:position:above",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬇️ Vɪᴅᴇᴏ Bᴇʟᴏᴡ Tᴇxᴛ",
                    callback_data=f"{PREFIX}:position:below",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cᴀɴᴄᴇʟ",
                    callback_data=f"{PREFIX}:cancel",
                )
            ],
        ]
    )


def _colour_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔵 Bʟᴜᴇ",
                    callback_data=f"{PREFIX}:color:blue",
                ),
                InlineKeyboardButton(
                    "🟢 Gʀᴇᴇɴ",
                    callback_data=f"{PREFIX}:color:green",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔴 Rᴇᴅ",
                    callback_data=f"{PREFIX}:color:red",
                ),
                InlineKeyboardButton(
                    "⚪ Dᴇғᴀᴜʟᴛ",
                    callback_data=f"{PREFIX}:color:default",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cᴀɴᴄᴇʟ",
                    callback_data=f"{PREFIX}:cancel",
                )
            ],
        ]
    )


def _button_style(value):
    return {
        "blue": ButtonStyle.PRIMARY,
        "green": ButtonStyle.SUCCESS,
        "red": ButtonStyle.DANGER,
    }.get(value)


def _format_one(mention: str) -> str:
    return (
        f"<b>❖ ʜᴇʏ ᴇᴠᴇʀʏᴏɴᴇ 👋</b>\n\n"
        f"<b>⊚ ɪғ ʏᴏᴜ ɴᴇᴇᴅ ᴀɴʏ ʜᴇʟᴘ • ꜱᴜᴘᴘᴏʀᴛ • ɢᴜɪᴅᴀɴᴄᴇ\n"
        f"ʏᴏᴜ ᴄᴀɴ ᴄᴏɴɴᴇᴄᴛ ᴅɪʀᴇᴄᴛʟʏ 💬✨</b>\n\n"
        f"<b>✦ ᴄᴏᴍᴇ {mention}</b>\n"
        f"<b>✦ ᴀʟᴡᴀʏꜱ ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ 🤍</b>\n\n"
        f"<b>•── ⋅ ⋅ ⋅ ───── ⋅ • ⋅ ───── ⋅ ⋅ ⋅ ──•</b>"
    )


def _format_two(mention: str) -> str:
    return (
        f"<b>Uꜱᴇʀɴᴀᴍᴇ Rᴇᴍᴏᴠᴇᴅ 🔄🔄</b>\n\n"
        f"<b>Dᴍ Hᴇʀᴇ : {mention} 💗💗</b>\n\n"
        f"<b>Aᴅᴅ Tᴏ Cᴏɴᴛᴀᴄᴛ Mᴇ Aʟᴡᴀʏꜱ 🚩💚</b>"
    )



def _entity_type_name(entity):
    """
    Return a stable lowercase entity type name.

    In Pyrogram/Kurigram, MessageEntity.type can be a MessageEntityType
    value whose value is a raw MessageEntity class (for example
    MessageEntityBold), not the string "bold". The previous implementation
    therefore failed to recognize formatting entities and silently dropped
    them. Handle strings, enum values and raw classes.
    """
    value = getattr(entity, "type", "")

    # Some versions expose a normal string/enum value.
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value.lower()

    if isinstance(value, str):
        return value.lower()

    # Pyrogram/Kurigram commonly exposes the raw MTProto class.
    name = getattr(value, "__name__", "")
    if name:
        name = name.lower()
        if name.startswith("messageentity"):
            name = name[len("messageentity"):]
        return name

    return str(value).lower()


def _shift_entity(entity, shift: int):
    """
    Convert a high-level Pyrogram/Kurigram MessageEntity into its raw
    MTProto equivalent and shift its offset by `shift` UTF-16 code units.
    """
    kind = _entity_type_name(entity)
    offset = int(entity.offset) + shift
    length = int(entity.length)

    mapping = {
        "bold": raw.types.MessageEntityBold,
        "italic": raw.types.MessageEntityItalic,
        "code": raw.types.MessageEntityCode,
        "pre": raw.types.MessageEntityPre,
        "underline": raw.types.MessageEntityUnderline,
        "strikethrough": raw.types.MessageEntityStrike,
        "strike": raw.types.MessageEntityStrike,
        "spoiler": raw.types.MessageEntitySpoiler,
        "blockquote": raw.types.MessageEntityBlockquote,
        "expandable_blockquote": raw.types.MessageEntityBlockquote,
        "text_link": raw.types.MessageEntityTextUrl,
        "text_url": raw.types.MessageEntityTextUrl,
        "url": raw.types.MessageEntityUrl,
        "mention": raw.types.MessageEntityMention,
        "hashtag": raw.types.MessageEntityHashtag,
        "cashtag": raw.types.MessageEntityCashtag,
        "email": raw.types.MessageEntityEmail,
        "phone_number": raw.types.MessageEntityPhone,
    }

    cls = mapping.get(kind)

    if cls is None:
        return None

    if kind == "text_link":
        return cls(offset=offset, length=length, url=entity.url)

    if kind == "pre":
        return cls(
            offset=offset,
            length=length,
            language=getattr(entity, "language", "") or "",
        )

    return cls(offset=offset, length=length)


async def _send_hidden_preview(
    chat_id: int,
    text: str,
    video_url: str,
    entities,
    show_above_text: bool,
):
    """
    Send a hidden-link MP4 preview through Telegram's raw MTProto
    messages.sendMessage API.

    `invert_media=True` places the webpage/video preview above the text.
    """
    hidden_char = "\u200b"

    # Do NOT add blank lines before the visible text. The old implementation
    # used "\u200b\n\n", which created the unwanted empty space in the post.
    final_text = hidden_char + text

    # All original user formatting starts after the one-character hidden URL.
    shift = 1
    raw_entities = []

    # Hidden URL entity at offset 0.
    raw_entities.append(
        raw.types.MessageEntityTextUrl(
            offset=0,
            length=1,
            url=video_url,
        )
    )

    for entity in entities or []:
        converted = _shift_entity(entity, shift)
        if converted is not None:
            raw_entities.append(converted)

    peer = await app.resolve_peer(chat_id)

    return await app.invoke(
        raw.functions.messages.SendMessage(
            peer=peer,
            no_webpage=False,
            invert_media=bool(show_above_text),
            random_id=random.randint(-(2**63), 2**63 - 1),
            message=final_text,
            entities=raw_entities,
        )
    )


async def _bot_admin_status(chat_id: int):
    """Check that the bot can access the target chat and can post."""
    try:
        chat = await app.get_chat(chat_id)
        member = await app.get_chat_member(chat_id, app.id)
    except Exception as exc:
        return (
            False,
            "❌ <b>I can't access that chat.</b>\n\n"
            f"<code>{type(exc).__name__}: {str(exc)[:300]}</code>",
        )

    if member.status not in (
        ChatMemberStatus.OWNER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        return (
            False,
            "❌ <b>I am not an administrator in that chat.</b>\n\n"
            "Please add/promote the bot as an admin and try again.",
        )

    if chat.type == ChatType.CHANNEL:
        privileges = member.privileges
        if not privileges or not privileges.can_post_messages:
            return (
                False,
                "❌ <b>I am an admin, but I don't have permission "
                "to post messages in this channel.</b>\n\n"
                "Enable <b>Post Messages</b> for the bot and try again.",
            )

    return True, None


@app.on_message(filters.command("createpost"))
async def createpost_start(_, message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id

    async with LOCK:
        SESSIONS[user_id] = {"step": "post_type"}

    await message.reply_text(
        "<b>🛠 Pᴏsᴛ Cʀᴇᴀᴛᴏʀ</b>\n\n"
        "<b>⚠️ Bᴇғᴏʀᴇ ᴜsɪɴɢ ᴛʜɪs ғᴇᴀᴛᴜʀᴇ:</b>\n"
        "Tʜᴇ ʙᴏᴛ ᴍᴜsᴛ ʙᴇ ᴀɴ <b>ᴀᴅᴍɪɴ</b> ɪɴ ʏᴏᴜʀ "
        "ɢʀᴏᴜᴘ/ᴄʜᴀɴɴᴇʟ.\n\n"
        "Cʜᴏᴏsᴇ ᴛʜᴇ ᴛʏᴘᴇ ᴏғ ᴘᴏsᴛ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴄʀᴇᴀᴛᴇ:",
        reply_markup=_post_type_menu(),
    )


@app.on_message(filters.command("cancel"))
async def createpost_cancel(_, message: Message):
    if message.from_user:
        SESSIONS.pop(message.from_user.id, None)

    await message.reply_text("❌ <b>Pᴏsᴛ Cʀᴇᴀᴛᴏʀ ᴄᴀɴᴄᴇʟʟᴇᴅ.</b>")


@app.on_callback_query(filters.regex(rf"^{PREFIX}:"))
async def createpost_callbacks(_, query: CallbackQuery):
    if not query.from_user:
        return await query.answer("Not available.", show_alert=True)

    user_id = query.from_user.id
    session = SESSIONS.get(user_id)

    if not session:
        return await query.answer(
            "This setup has expired. Use /createpost again.",
            show_alert=True,
        )

    data = query.data or ""

    if data == f"{PREFIX}:cancel":
        SESSIONS.pop(user_id, None)
        await query.answer("Cancelled")
        with suppress(Exception):
            await query.message.edit_text(
                "❌ <b>Pᴏsᴛ Cʀᴇᴀᴛᴏʀ ᴄᴀɴᴄᴇʟʟᴇᴅ.</b>"
            )
        return

    if data.startswith(f"{PREFIX}:type:"):
        if session.get("step") != "post_type":
            return await query.answer("Please follow the current step.", show_alert=True)

        selected_type = data.rsplit(":", 1)[1]
        if selected_type not in ("hidden", "normal"):
            return await query.answer("Invalid post type.", show_alert=True)

        session["post_type"] = selected_type
        session["step"] = "chat_id"

        await query.answer("Type selected")
        with suppress(Exception):
            if selected_type == "hidden":
                await query.message.edit_text(
                    "<b>🎬 Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ</b>\n\n"
                    "Sᴇɴᴅ ᴛʜᴇ <b>Cʜᴀᴛ ID</b> ᴡʜᴇʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛʜᴇ ᴘᴏsᴛ ᴛᴏ ʙᴇ sᴇɴᴛ.\n\n"
                    "<i>Example: -1001234567890</i>"
                )
            else:
                await query.message.edit_text(
                    "<b>🖼 Nᴏʀᴍᴀʟ Pᴏsᴛ</b>\n\n"
                    "Sᴇɴᴅ ᴛʜᴇ <b>Cʜᴀᴛ ID</b> ᴡʜᴇʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛʜᴇ ᴘᴏsᴛ ᴛᴏ ʙᴇ sᴇɴᴛ.\n\n"
                    "<i>Example: -1001234567890</i>"
                )
        return

    if data.startswith(f"{PREFIX}:format:"):
        if session.get("step") != "format":
            return await query.answer("Please follow the current step.", show_alert=True)

        selected_format = data.rsplit(":", 1)[1]
        if selected_format not in ("1", "2"):
            return await query.answer("Invalid format.", show_alert=True)

        session["format"] = selected_format
        session["step"] = "user_id"
        await query.answer("Format selected")
        with suppress(Exception):
            await query.message.edit_text(
                "<b>2/5 — Sᴇɴᴅ Uꜱᴇʀ ID</b>\n\n"
                "Send the Telegram User ID whose name and mention "
                "should appear in the post.\n\n"
                "<i>Example: 123456789</i>"
            )
        return

    if data.startswith(f"{PREFIX}:position:"):
        if session.get("post_type") != "hidden":
            return await query.answer(
                "This option is only for Hidden Preview.",
                show_alert=True,
            )

        if session.get("step") != "position":
            return await query.answer(
                "Please follow the current step.",
                show_alert=True,
            )

        position = data.rsplit(":", 1)[1]

        if position not in ("above", "below"):
            return await query.answer(
                "Invalid position.",
                show_alert=True,
            )

        session["position"] = position
        session["step"] = "hidden_text"

        await query.answer("Position selected")

        with suppress(Exception):
            await query.message.edit_text(
                "<b>2/4 — Sᴇɴᴅ Pᴏsᴛ Tᴇxᴛ</b>\n\n"
                "Send the complete text/caption you want in the post."
            )

        return

    if data.startswith(f"{PREFIX}:color:"):
        if session.get("step") != "color":
            return await query.answer("Please follow the current step.", show_alert=True)

        selected_color = data.rsplit(":", 1)[1]
        if selected_color not in ("blue", "green", "red", "default"):
            return await query.answer("Invalid colour.", show_alert=True)

        session["color"] = selected_color
        session["step"] = "photo"
        await query.answer("Colour selected")
        with suppress(Exception):
            await query.message.edit_text(
                "<b>4/5 — Sᴇɴᴅ Pʜᴏᴛᴏ</b>\n\n"
                "Send the image as a Telegram photo, or send a direct image URL."
            )
        return

    await query.answer("Unknown option.", show_alert=True)


@app.on_message(ACTIVE_SESSION & ~filters.service & ~filters.command("createpost") & ~filters.command("cancel"))
async def createpost_input(_, message: Message):
    if not message.from_user:
        return

    user_id = message.from_user.id
    session = SESSIONS.get(user_id)
    if not session:
        return

    step = session.get("step")
    raw = (message.text or message.caption or "").strip()

    # Target Chat ID — common for both post types.
    if step == "chat_id":
        try:
            chat_id = int(raw)
        except (TypeError, ValueError):
            return await message.reply_text(
                "❌ <b>Invalid Chat ID.</b>\n\n"
                "Send a numeric Chat ID, for example:\n"
                "<code>-1001234567890</code>"
            )

        ok, reason = await _bot_admin_status(chat_id)
        if not ok:
            return await message.reply_text(reason)

        session["chat_id"] = chat_id

        if session.get("post_type") == "hidden":
            session["step"] = "position"
            return await message.reply_text(
                "<b>✅ Bᴏᴛ Aᴅᴍɪɴ Vᴇʀɪғɪᴇᴅ.</b>\n\n"
                "<b>1/4 — Cʜᴏᴏsᴇ Vɪᴅᴇᴏ Pᴏsɪᴛɪᴏɴ</b>\n\n"
                "Choose whether the video preview should appear above "
                "or below your text.",
                reply_markup=_preview_position_menu(),
            )

        session["step"] = "format"
        return await message.reply_text(
            "<b>✅ Bᴏᴛ Aᴅᴍɪɴ Vᴇʀɪғɪᴇᴅ.</b>\n\n"
            "<b>1/5 — Cʜᴏᴏsᴇ Pᴏsᴛ Fᴏʀᴍᴀᴛ</b>",
            reply_markup=_format_menu(),
        )

    # Hidden Preview: manually supplied visible text.
    if step == "hidden_text":
        if not raw:
            return await message.reply_text(
                "❌ <b>No text received.</b>\n\nSend the text you want in the post."
            )

        if len(raw) > 4090:
            return await message.reply_text(
                "❌ <b>Text is too long.</b>\n\nPlease keep it under 4090 characters."
            )

        session["hidden_text"] = raw
        # Preserve formatting applied by the user in Telegram (bold,
        # italic, underline, links, etc.). message.text alone loses these
        # visual entities, so save them for the final send.
        session["hidden_entities"] = list(message.entities or [])
        session["step"] = "hidden_video"

        return await message.reply_text(
            "<b>3/4 — Sᴇɴᴅ Dɪʀᴇᴄᴛ Vɪᴅᴇᴏ URL</b>\n\n"
            "Send a direct <b>.mp4</b> URL.\n\n"
            "<i>Example: https://files.catbox.moe/6ourwe.mp4</i>"
        )

    # Hidden Preview: external video URL.
    if step == "hidden_video":
        video_url = raw

        if not re.match(r"^https?://\S+$", video_url, re.IGNORECASE):
            return await message.reply_text(
                "❌ <b>Invalid URL.</b>\n\n"
                "Send a direct HTTP/HTTPS video URL, preferably ending in <code>.mp4</code>."
            )

        if not re.search(r"\.mp4(?:$|[?#])", video_url, re.IGNORECASE):
            return await message.reply_text(
                "❌ <b>This doesn't look like a direct MP4 URL.</b>\n\n"
                "Please send a direct <code>.mp4</code> link."
            )

        session["video_url"] = video_url
        session["step"] = "hidden_sending"

        await message.reply_text("⏳ <b>Cʀᴇᴀᴛɪɴɢ Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ...</b>")

        try:
            visible_text = session["hidden_text"]
            original_entities = session.get("hidden_entities", [])

            await _send_hidden_preview(
                chat_id=session["chat_id"],
                text=visible_text,
                video_url=video_url,
                entities=original_entities,
                show_above_text=(session.get("position") == "above"),
            )

            await message.reply_text(
                "✅ <b>Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ Pᴏsᴛ Cʀᴇᴀᴛᴇᴅ.</b>"
            )

        except Exception as exc:
            await message.reply_text(
                "❌ <b>Fᴀɪʟᴇᴅ Tᴏ Cʀᴇᴀᴛᴇ Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ.</b>\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:700]}</code>"
            )

        finally:
            SESSIONS.pop(user_id, None)

        return

    # Normal Post: User ID.
    if step == "user_id":
        try:
            target_user_id = int(raw)
            if target_user_id <= 0:
                raise ValueError
            target_user = await app.get_users(target_user_id)
        except ValueError:
            return await message.reply_text(
                "❌ <b>Invalid User ID.</b>\n\nSend a valid numeric Telegram User ID."
            )
        except RPCError:
            return await message.reply_text(
                "❌ <b>I couldn't fetch that user.</b>\n\n"
                "Make sure the User ID is correct and try again."
            )
        except Exception as exc:
            return await message.reply_text(
                "❌ <b>Failed to fetch the user.</b>\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:300]}</code>"
            )

        session["user"] = target_user
        session["step"] = "color"
        name = target_user.first_name or "User"

        await message.reply_text(
            f"<b>✅ User:</b> <a href='tg://user?id={target_user.id}'>{name}</a>\n\n"
            "<b>3/5 — Cʜᴏᴏsᴇ Bᴜᴛᴛᴏɴ Cᴏʟᴏᴜʀ</b>",
            reply_markup=_colour_menu(),
        )
        return

    # Normal Post: Photo.
    if step == "photo":
        photo = None
        if message.photo:
            photo = message.photo.file_id
        elif raw:
            if not re.match(r"^https?://\S+$", raw, re.IGNORECASE):
                return await message.reply_text(
                    "❌ <b>Invalid image URL.</b>\n\n"
                    "Send a direct image URL or upload the image as a Telegram photo."
                )
            photo = raw

        if not photo:
            return await message.reply_text(
                "❌ <b>No photo received.</b>\n\n"
                "Please send a photo or direct image URL."
            )

        session["photo"] = photo
        session["step"] = "sending"
        await message.reply_text("⏳ <b>Cʀᴇᴀᴛɪɴɢ Yᴏᴜʀ Pᴏsᴛ...</b>")

        try:
            target_user = session["user"]
            name = target_user.first_name or "User"

            safe_name = (
                name.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
            )

            mention = f'<a href="tg://user?id={target_user.id}">{safe_name}</a>'

            caption = (
                _format_one(mention)
                if session["format"] == "1"
                else _format_two(mention)
            )

            style = _button_style(session.get("color"))
            button_kwargs = {"text": name, "user_id": target_user.id}
            if style is not None:
                button_kwargs["style"] = style

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(**button_kwargs)]]
            )

            await app.send_photo(
                chat_id=session["chat_id"],
                photo=session["photo"],
                caption=caption,
                reply_markup=keyboard,
            )

            await message.reply_text(
                "✅ <b>Pᴏsᴛ Cʀᴇᴀᴛᴇᴅ Sᴜᴄᴄᴇssғᴜʟʟʏ.</b>"
            )

        except Exception as exc:
            await message.reply_text(
                "❌ <b>Fᴀɪʟᴇᴅ Tᴏ Cʀᴇᴀᴛᴇ Tʜᴇ Pᴏsᴛ.</b>\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:700]}</code>"
            )

        finally:
            SESSIONS.pop(user_id, None)

        return
