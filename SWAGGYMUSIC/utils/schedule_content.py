"""Post content for the /Schedule manager: capture, storage form, delivery.

=====================================================================
 WHY ENTITIES, NOT HTML
=====================================================================

The admin composes the post in Telegram's own editor (bold, italic,
underline, strikethrough, spoiler, monospace, links, Premium custom emoji
...) and sends it to the bot. Telegram delivers the text together with its
formatting as a list of message entities, with offsets and lengths counted
in UTF-16 code units. The post is stored exactly in that form — the plain
text plus one dict per entity — and rebuilt into Kurigram 2.2.25
``types.MessageEntity`` objects for delivery. Nothing goes through an
HTML or Markdown round trip, so every entity Telegram can carry survives,
custom emoji ids included.

Storage form of one entity (``serialize_entity``)::

    {"type": "BOLD", "offset": 0, "length": 5}                  # name of
    {"type": "TEXT_LINK", "offset": 6, "length": 4, "url": "…"}  # enums.
    {"type": "CUSTOM_EMOJI", "offset": 11, "length": 2,          # Message-
     "custom_emoji_id": "5368324170671202286"}                   # EntityType
    {"type": "PRE", "offset": 14, "length": 9, "language": "py"}
    {"type": "TEXT_MENTION", …, "user_id": 123}
    {"type": "BLOCKQUOTE", …, "expandable": true}

``custom_emoji_id`` is kept as a string: it is a 64-bit id and MongoDB /
JSON tools must not round it.

Delivery always passes ``parse_mode=ParseMode.DISABLED``: this bot's client
defaults to HTML, and Kurigram re-parses the text with the default parse
mode whenever a message has no entities — a plain-text post containing
``<`` or ``&`` would otherwise be mangled.

=====================================================================
 SUPPORTED POSTS
=====================================================================

Text, or ONE photo / video / GIF (animation) / document / audio / voice
note with an optional caption. The media is stored as the Telegram
``file_id`` (stable for the bot that received it). Media spoilers, the
caption-above-media option and the link-preview choice of a text post
(disabled / shown above the text) are kept too. Albums, stickers, video
notes, polls, locations, contacts and other special messages are refused
with a clear message.

Telegram limits: text up to 4096 and captions up to 1024 characters
(UTF-16 code units).

=====================================================================
 CLEANUP PROTECTION ("Radhe Radhe")
=====================================================================

A post is sent exactly as the admin wrote it: the scheduler never adds,
removes or changes any text, emoji, signature or phrase. A separate
channel-cleanup script keeps posts that contain "Radhe Radhe", so a post
is *protected* only when the admin's own text already contains the phrase
(English, Hindi or Urdu — see utils/protection_phrase.py). ``is_protected``
only reads the stored original text; it never modifies anything.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import types
from pyrogram.enums import MessageEntityType, MessageMediaType, ParseMode

from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils.protection_phrase import has_radhe_radhe
from SWAGGYMUSIC.utils.user_resolution import (UserResolutionError,
                                               ensure_user_peer)

log = LOGGER(__name__)

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

MEDIA_KINDS = ("photo", "video", "animation", "document", "audio", "voice")
KIND_LABELS = {
    "text": "Text",
    "photo": "Photo",
    "video": "Video",
    "animation": "GIF",
    "document": "Document",
    "audio": "Audio",
    "voice": "Voice note",
}
KIND_ICONS = {
    "text": "📝",
    "photo": "🖼",
    "video": "🎬",
    "animation": "🎞",
    "document": "📄",
    "audio": "🎵",
    "voice": "🎙",
}
# Media that support a spoiler cover and the caption-above-media layout.
_VISUAL_KINDS = ("photo", "video", "animation")

UNSUPPORTED_TEXT = (
    "❌ <b>This kind of message can't be scheduled.</b>\n\n"
    "Send text, or ONE photo, video, GIF, document, audio or voice note "
    "(with an optional caption)."
)


class ContentError(Exception):
    """The admin's message cannot become a scheduled post (user-facing HTML)."""


def utf16_len(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# Entities <-> storage form
# ---------------------------------------------------------------------------

def serialize_entity(entity: Any) -> Optional[Dict[str, Any]]:
    """One Kurigram MessageEntity as a plain dict (None if unusable)."""
    name = getattr(getattr(entity, "type", None), "name", None)
    if not isinstance(name, str) or name not in MessageEntityType.__members__:
        return None
    if name == "UNKNOWN":
        return None
    try:
        offset, length = int(entity.offset), int(entity.length)
    except (AttributeError, TypeError, ValueError):
        return None
    if offset < 0 or length <= 0:
        return None
    data: Dict[str, Any] = {"type": name, "offset": offset, "length": length}
    if name == "TEXT_LINK":
        url = getattr(entity, "url", None)
        if not url:
            return None
        data["url"] = str(url)
    elif name == "PRE":
        # MessageEntityPre.language is a REQUIRED TL field ("" = none).
        data["language"] = str(getattr(entity, "language", None) or "")
    elif name == "CUSTOM_EMOJI":
        emoji_id = getattr(entity, "custom_emoji_id", None)
        if not emoji_id:
            return None
        data["custom_emoji_id"] = str(emoji_id)
    elif name == "TEXT_MENTION":
        user_id = getattr(getattr(entity, "user", None), "id", None)
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        data["user_id"] = user_id
    elif name == "BLOCKQUOTE":
        expandable = getattr(entity, "expandable", None)
        if expandable is not None:
            data["expandable"] = bool(expandable)
    elif name == "DATE_TIME":
        unix_time = getattr(entity, "unix_time", None)
        if not isinstance(unix_time, int):
            return None
        data["unix_time"] = unix_time
        date_format = getattr(entity, "date_time_format", None)
        if date_format:
            data["date_time_format"] = str(date_format)
    return data


def serialize_entities(entities: Optional[List[Any]]) -> List[Dict[str, Any]]:
    out = []
    for entity in entities or []:
        data = serialize_entity(entity)
        if data is not None:
            out.append(data)
    return out


def deserialize_entities(items: Optional[List[Dict[str, Any]]]) -> List["types.MessageEntity"]:
    """The storage form back into Kurigram MessageEntity objects."""
    out: List[types.MessageEntity] = []
    for item in items or []:
        try:
            etype = MessageEntityType[item["type"]]
            kwargs: Dict[str, Any] = {
                "type": etype,
                "offset": int(item["offset"]),
                "length": int(item["length"]),
            }
        except (KeyError, TypeError, ValueError):
            log.warning("schedule: skipped malformed stored entity %r", item)
            continue
        if etype is MessageEntityType.TEXT_LINK:
            if not item.get("url"):
                continue
            kwargs["url"] = str(item["url"])
        elif etype is MessageEntityType.PRE:
            kwargs["language"] = str(item.get("language") or "")
        elif etype is MessageEntityType.CUSTOM_EMOJI:
            if not item.get("custom_emoji_id"):
                continue
            kwargs["custom_emoji_id"] = str(item["custom_emoji_id"])
        elif etype is MessageEntityType.TEXT_MENTION:
            if not isinstance(item.get("user_id"), int):
                continue
            kwargs["user"] = types.User(id=int(item["user_id"]))
        elif etype is MessageEntityType.BLOCKQUOTE:
            if "expandable" in item:
                kwargs["expandable"] = bool(item["expandable"])
        elif etype is MessageEntityType.DATE_TIME:
            if not isinstance(item.get("unix_time"), int):
                continue
            kwargs["unix_time"] = int(item["unix_time"])
            if item.get("date_time_format"):
                kwargs["date_time_format"] = str(item["date_time_format"])
        out.append(types.MessageEntity(**kwargs))
    return out


# ---------------------------------------------------------------------------
# Capture (the admin's message -> storage form)
# ---------------------------------------------------------------------------

def _media_kind(message: Any) -> Optional[str]:
    media = getattr(message, "media", None)
    if media is None or media == MessageMediaType.WEB_PAGE:
        return "text"
    for kind in MEDIA_KINDS:
        if getattr(message, kind, None) is not None:
            return kind
    return None


def extract_content(message: Any, *, plain_text: bool = False) -> Dict[str, Any]:
    """The post the admin sent, in storage form. Raises ContentError.

    ``plain_text`` drops every formatting entity on purpose and keeps the
    text unchanged ("Paste as Plain Text").
    """
    kind = _media_kind(message)
    if kind is None:
        raise ContentError(UNSUPPORTED_TEXT)
    if kind == "text":
        text = str(getattr(message, "text", None) or "")
        entities = getattr(message, "entities", None)
        if not text.strip():
            raise ContentError(UNSUPPORTED_TEXT)
        if utf16_len(text) > TEXT_LIMIT:
            raise ContentError(
                f"❌ <b>The text is too long.</b>\n\nTelegram allows {TEXT_LIMIT} "
                "characters per post. Please shorten it and send it again."
            )
    else:
        text = str(getattr(message, "caption", None) or "")
        entities = getattr(message, "caption_entities", None)
        if utf16_len(text) > CAPTION_LIMIT:
            raise ContentError(
                f"❌ <b>The caption is too long.</b>\n\nBots can post captions of "
                f"up to {CAPTION_LIMIT} characters. Please shorten it and send it again."
            )

    content: Dict[str, Any] = {
        "kind": kind,
        "text": text,
        "entities": [] if plain_text else serialize_entities(entities),
        "plain_text": bool(plain_text),
    }
    if kind == "text":
        options = getattr(message, "link_preview_options", None)
        if isinstance(options, types.LinkPreviewOptions):
            content["link_preview"] = {
                "disabled": bool(options.is_disabled),
                "above": bool(options.show_above_text),
            }
    else:
        media = getattr(message, kind)
        file_id = getattr(media, "file_id", None)
        if not file_id:
            raise ContentError(UNSUPPORTED_TEXT)
        content["file_id"] = str(file_id)
        content["file_unique_id"] = str(getattr(media, "file_unique_id", None) or "")
        if kind in _VISUAL_KINDS:
            content["has_spoiler"] = bool(getattr(message, "has_media_spoiler", None))
            content["caption_above"] = bool(getattr(message, "show_caption_above_media", None))
    return content


def make_plain(content: Dict[str, Any]) -> Dict[str, Any]:
    """The same post with every formatting entity removed (text kept)."""
    plain = dict(content)
    plain["entities"] = []
    plain["plain_text"] = True
    return plain


# ---------------------------------------------------------------------------
# Summaries for the UI
# ---------------------------------------------------------------------------

def entity_counts(content: Dict[str, Any]) -> Tuple[int, int]:
    """(all entities, custom emoji entities) of a stored post."""
    entities = content.get("entities") or []
    custom = sum(1 for item in entities if item.get("type") == "CUSTOM_EMOJI")
    return len(entities), custom


def kind_label(content: Dict[str, Any]) -> str:
    kind = content.get("kind", "text")
    label = f"{KIND_ICONS.get(kind, '📝')} {KIND_LABELS.get(kind, 'Post')}"
    if kind != "text" and (content.get("text") or "").strip():
        label += " + caption"
    return label


def formatting_label(content: Dict[str, Any]) -> str:
    if content.get("plain_text"):
        return "📋 Plain text (formatting removed)"
    total, custom = entity_counts(content)
    if not total:
        return "No formatting"
    label = f"{total} formatting entit{'y' if total == 1 else 'ies'}"
    if custom:
        label += f" ({custom} custom emoji)"
    return label


def is_protected(content: Dict[str, Any]) -> bool:
    """Cleanup protection: the admin's own text / caption contains
    "Radhe Radhe". Plain Text mode keeps the text, so it does not change
    this. Read-only — the post is never modified to protect it."""
    return has_radhe_radhe(content.get("text"))


def protection_label(content: Dict[str, Any]) -> str:
    if is_protected(content):
        return "ON — the post contains “Radhe Radhe”"
    return "OFF — the post has no “Radhe Radhe”"


def preview_line(content: Dict[str, Any], limit: int = 32) -> str:
    """A one-line plain-text preview of the post (not HTML-escaped)."""
    text = " ".join((content.get("text") or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    kind = content.get("kind", "text")
    if kind == "text":
        return text or "(empty)"
    label = f"{KIND_ICONS.get(kind, '📎')} {KIND_LABELS.get(kind, 'Media')}"
    return f"{label} · {text}" if text else label


def content_hash(content: Dict[str, Any]) -> str:
    """Fingerprint used to refuse an identical post at the same time."""
    material = {
        "kind": content.get("kind"),
        "text": content.get("text") or "",
        "entities": content.get("entities") or [],
        "media": content.get("file_unique_id") or content.get("file_id") or "",
        "spoiler": bool(content.get("has_spoiler")),
        "above": bool(content.get("caption_above")),
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

async def _resolvable_entities(client: Any, chat_id: int, entities: List[Any],
                               context: Optional[dict]) -> Tuple[List[Any], int]:
    """Drop text mentions whose user Telegram no longer lets the bot address.

    Kurigram resolves a TEXT_MENTION's user while serializing the request;
    after a restart the in-memory peer cache is empty, and an unresolvable
    mention would make the whole post fail. The mention's text is kept, only
    the link to the profile is lost — reported as a delivery warning.
    """
    kept, dropped = [], 0
    for entity in entities:
        if entity.type is MessageEntityType.TEXT_MENTION:
            try:
                await ensure_user_peer(
                    client, int(entity.user.id),
                    chat_id=chat_id if int(chat_id) < 0 else None,
                    context=context,
                )
            except UserResolutionError:
                dropped += 1
                continue
        kept.append(entity)
    return kept, dropped


async def send_content(client: Any, chat_id: int, content: Dict[str, Any], *,
                       context: Optional[dict] = None) -> Tuple[Any, List[str]]:
    """Send a stored post to ``chat_id``; returns (sent message, warnings).

    Raises whatever Telegram raises (FloodWait, RPCError ...) and
    ContentError for a stored post that cannot be sent at all.
    """
    warnings: List[str] = []
    entities, dropped = await _resolvable_entities(
        client, chat_id, deserialize_entities(content.get("entities")), context,
    )
    if dropped:
        warnings.append(
            f"{dropped} user mention(s) could not be linked to the profile "
            "(Telegram no longer lets the bot address that user); the text was kept."
        )
    kind = content.get("kind")
    text = content.get("text") or ""
    common = {"parse_mode": ParseMode.DISABLED}

    if kind == "text":
        options = None
        preview = content.get("link_preview")
        if isinstance(preview, dict):
            options = types.LinkPreviewOptions(
                is_disabled=bool(preview.get("disabled")),
                show_above_text=bool(preview.get("above")),
            )
        sent = await client.send_message(
            chat_id, text, entities=entities or None,
            link_preview_options=options, **common,
        )
        return sent, warnings

    file_id = content.get("file_id")
    if kind not in MEDIA_KINDS or not file_id:
        raise ContentError("The stored post has no media to send.")
    media_kwargs: Dict[str, Any] = {
        "caption": text, "caption_entities": entities or None, **common,
    }
    if kind in _VISUAL_KINDS:
        media_kwargs["has_spoiler"] = bool(content.get("has_spoiler")) or None
        media_kwargs["show_caption_above_media"] = bool(content.get("caption_above")) or None
    sender = {
        "photo": client.send_photo,
        "video": client.send_video,
        "animation": client.send_animation,
        "document": client.send_document,
        "audio": client.send_audio,
        "voice": client.send_voice,
    }[kind]
    sent = await sender(chat_id, file_id, **media_kwargs)
    return sent, warnings


def custom_emoji_delivery(content: Dict[str, Any], sent: Any) -> Tuple[int, int]:
    """(custom emoji sent, custom emoji Telegram kept) for a delivered post.

    Telegram replaces custom emoji with their standard fallback emoji when
    the bot may not use them in that chat (channels: only bots with a
    Fragment username). The message Telegram returns shows what was kept.
    """
    _total, wanted = entity_counts(content)
    if not wanted or not isinstance(sent, types.Message):
        return wanted, wanted
    entities = (sent.caption_entities if content.get("kind") != "text" else sent.entities) or []
    kept = sum(1 for e in entities if getattr(e, "type", None) is MessageEntityType.CUSTOM_EMOJI)
    return wanted, kept
