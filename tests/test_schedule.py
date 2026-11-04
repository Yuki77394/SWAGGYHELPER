"""/Schedule — the hidden scheduled-post manager.

What is real here, and what is faked:

* the admin's messages are REAL Kurigram messages: raw MTProto updates
  (text + raw entities such as MessageEntityCustomEmoji, or raw media)
  parsed by ``types.Message._parse`` exactly like a live update;
* delivery uses Kurigram's REAL ``send_message`` / ``send_photo`` / ...
  methods; only ``app.invoke`` is replaced by ``FakeTelegram``, which
  records the raw ``messages.SendMessage`` / ``messages.SendMedia`` request
  and answers with the raw ``Updates`` Telegram would return. So the tests
  see the exact entities Kurigram puts on the wire;
* MongoDB is mongomock-motor (conftest.py); the clock is pinned.

Covered: access permissions, channel validation, listing + pagination,
the Add New workflow, IST conversion, past-time rejection, formatting and
Premium custom emoji through save AND delivery, Plain Text mode, edit /
reschedule / cancel / delete, restart safety, no double delivery, failure
recording, missed-schedule policy, handler conflicts, and that the command
stays out of the Help menu and the Start message.
"""
from __future__ import annotations

import ast
import asyncio
import itertools
import os
import re
import sys
import time
import tomllib
from contextlib import suppress
from datetime import date, datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pyrogram
import pytest
from gap_harness import (_raw_channel, _raw_media, _raw_user, dispatch,
                         dispatch_callback, registered_groups)
from pyrogram import StopPropagation, raw, types, utils
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import InlineKeyboardMarkup
from pyrogram.errors import ChatWriteForbidden, FloodWait, InternalServerError

import SWAGGYMUSIC.plugins.misc.schedule as sch  # noqa: F401 (registers handlers)
import SWAGGYMUSIC.plugins.misc.setlink as setlink_mod  # noqa: F401
from SWAGGYMUSIC import app
from SWAGGYMUSIC.misc import SUDOERS
from SWAGGYMUSIC.utils import schedule_content as content_mod
from SWAGGYMUSIC.utils import schedule_time as st
from SWAGGYMUSIC.utils import scheduled_posts as store
from SWAGGYMUSIC.utils.protection_phrase import has_radhe_radhe

REAL_START_WORKER = sch._start_worker   # before the autouse fixture stubs it
BOLD_PHRASE = "𝐑𝐚𝐝𝐡𝐞 𝐑𝐚𝐝𝐡𝐞"       # styled Unicode letters — still the phrase

# (pytest.ini: asyncio_mode = auto — async tests need no marker)

OWNER = 1                      # conftest: config.OWNER_ID, always a SUDOer
SUDO_ADMIN = 6201              # a SUDOer who is NOT the owner
STRANGER = 6299                # not a SUDOer
# "Now" for every test: 04-11-2026 12:00 PM IST (06:30 UTC).
NOW = int(datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc).timestamp())
CUSTOM_EMOJI_ID = 5368324170671202286   # a real-looking 64-bit document id

_CHANNEL_IDS = itertools.count(-1002000000001, -1)
_MSG_IDS = itertools.count(1000)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _isolation(monkeypatch):
    """Pinned clock, clean scheduler state and an empty scheduled_posts."""
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(st, "now_ts", lambda: clock.now)
    sch.SESSIONS.clear()
    sch._VERIFIED.clear()
    await store.scheduled_db.delete_many({"schedule_id": {"$exists": True}})
    monkeypatch.setattr(sch, "log_schedule_change", AsyncMock())
    # /Schedule starts the background worker; tests drive it explicitly
    # (process_due / recover_interrupted) — TestWorkerLoop runs the real loop.
    monkeypatch.setattr(sch, "_start_worker", AsyncMock())
    yield clock
    sch.SESSIONS.clear()
    sch._VERIFIED.clear()


@pytest.fixture
def clock(_isolation):
    return _isolation


@pytest.fixture
def sudo_admin():
    SUDOERS.add(SUDO_ADMIN)
    yield SUDO_ADMIN
    SUDOERS.discard(SUDO_ADMIN)


class FakeTelegram:
    """Answers messages.SendMessage / messages.SendMedia like Telegram."""

    def __init__(self):
        self.requests = []
        self.errors = []                 # raised by the next channel sends, in order
        self.delay = 0.0
        self.strip_custom_emoji = False
        self.media = {}
        self._ids = itertools.count(7000)

    def register(self, raw_media):
        inner = getattr(raw_media, "photo", None) or getattr(raw_media, "document", None)
        self.media[inner.id] = raw_media

    async def resolve_peer(self, peer_id):
        if isinstance(peer_id, int) and peer_id < 0:
            return raw.types.InputPeerChannel(channel_id=utils.get_raw_peer_id(peer_id), access_hash=7)
        return raw.types.InputPeerUser(user_id=int(peer_id), access_hash=3)

    async def invoke(self, query, *args, **kwargs):
        if not isinstance(query, (raw.functions.messages.SendMessage, raw.functions.messages.SendMedia)):
            return MagicMock()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.requests.append(query)
        # Injected errors hit channel deliveries only — never the private
        # notices the worker sends to the creator in between.
        if self.errors and isinstance(query.peer, raw.types.InputPeerChannel):
            raise self.errors.pop(0)
        entities = list(query.entities or [])
        if self.strip_custom_emoji:
            entities = [e for e in entities if not isinstance(e, raw.types.MessageEntityCustomEmoji)]
        media = None
        if isinstance(query, raw.functions.messages.SendMedia):
            inner = query.media.id
            source = self.media[inner.id]
            fields = {slot: getattr(source, slot) for slot in source.__slots__}   # TL objects use __slots__
            media = type(source)(**{**fields, "spoiler": query.media.spoiler})
        if isinstance(query.peer, raw.types.InputPeerChannel):
            peer = raw.types.PeerChannel(channel_id=query.peer.channel_id)
            chats, users = [_raw_channel(query.peer.channel_id, "Test channel")], []
            wrap = raw.types.UpdateNewChannelMessage
        else:
            peer = raw.types.PeerUser(user_id=query.peer.user_id)
            chats, users = [], [_raw_user(query.peer.user_id)]
            wrap = raw.types.UpdateNewMessage
        message = raw.types.Message(
            id=next(self._ids), peer_id=peer, date=NOW, message=query.message,
            entities=entities, media=media, out=True,
            post=isinstance(peer, raw.types.PeerChannel) or None,
            invert_media=query.invert_media, restriction_reason=[],
        )
        return raw.types.Updates(updates=[wrap(message=message, pts=1, pts_count=1)],
                                 users=users, chats=chats, date=NOW, seq=0)

    def to(self, chat_id):
        if chat_id < 0:
            raw_id = utils.get_raw_peer_id(chat_id)
            return [q for q in self.requests if getattr(q.peer, "channel_id", None) == raw_id]
        return [q for q in self.requests if getattr(q.peer, "user_id", None) == chat_id]


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(app, "invoke", fake.invoke)
    monkeypatch.setattr(app, "resolve_peer", fake.resolve_peer)
    for name in ("send_message", "send_photo", "send_video", "send_animation",
                 "send_document", "send_audio", "send_voice"):
        monkeypatch.setattr(app, name, MethodType(getattr(pyrogram.Client, name), app))
    return fake


def _member(status, *, can_post=True):
    member = MagicMock()
    member.status = status
    member.privileges = (MagicMock(can_post_messages=can_post, can_delete_messages=True)
                         if status == ChatMemberStatus.ADMINISTRATOR else None)
    return member


@pytest.fixture
def channel(monkeypatch):
    """A channel where the bot posts and every user asked about is an admin
    (``env.user_status`` / ``env.bot_can_post`` / ``env.chat_type`` tweak it)."""
    env = SimpleNamespace(id=next(_CHANNEL_IDS), user_status=ChatMemberStatus.ADMINISTRATOR,
                          bot_can_post=True, chat_type=ChatType.CHANNEL, reachable=True)

    async def get_chat(chat_id):
        if not env.reachable:
            raise pyrogram.errors.ChannelPrivate()
        chat = MagicMock()
        chat.id, chat.type, chat.title, chat.username = chat_id, env.chat_type, "Test channel", None
        return chat

    async def get_chat_member(chat_id, user_id):
        if user_id == app.id:
            return _member(ChatMemberStatus.ADMINISTRATOR, can_post=env.bot_can_post)
        return _member(env.user_status)

    monkeypatch.setattr(app, "get_chat", AsyncMock(side_effect=get_chat))
    monkeypatch.setattr(app, "get_chat_member", AsyncMock(side_effect=get_chat_member))
    return env


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _doc_media(n, mime, attributes, spoiler=None):
    return raw.types.MessageMediaDocument(document=raw.types.Document(
        id=40_000 + n, access_hash=11, file_reference=b"\x02", date=0, mime_type=mime,
        size=2048, dc_id=2, attributes=attributes, thumbs=[], video_thumbs=[],
    ), alt_documents=[], spoiler=spoiler)


def raw_media(kind, n, *, spoiler=None):
    if kind == "photo":
        media = _raw_media("photo", n)
        media.spoiler = spoiler
        return media
    if kind == "video":
        media = _raw_media("video", n)
        media.spoiler = spoiler
        return media
    return {
        "document": lambda: _doc_media(n, "application/pdf", [raw.types.DocumentAttributeFilename(file_name="a.pdf")]),
        "audio": lambda: _doc_media(n, "audio/mpeg", [raw.types.DocumentAttributeAudio(duration=9, title="T", performer="P")]),
        "voice": lambda: _doc_media(n, "audio/ogg", [raw.types.DocumentAttributeAudio(duration=3, voice=True)]),
        "animation": lambda: _doc_media(n, "video/mp4", [raw.types.DocumentAttributeAnimated(),
                                                          raw.types.DocumentAttributeVideo(duration=2, w=8, h=8)], spoiler),
        "sticker": lambda: _doc_media(n, "image/webp", [
            raw.types.DocumentAttributeSticker(alt="😀", stickerset=raw.types.InputStickerSetEmpty()),
            raw.types.DocumentAttributeImageSize(w=512, h=512)]),
        "location": lambda: raw.types.MessageMediaGeo(geo=raw.types.GeoPoint(long=1.0, lat=2.0, access_hash=1)),
    }[kind]()


async def admin_message(text="", *, entities=(), media=None, user_id=OWNER, grouped_id=None,
                        invert=None, telegram=None):
    """A REAL private message from an admin to the bot (raw update parsed by Kurigram)."""
    if telegram is not None and isinstance(media, (raw.types.MessageMediaPhoto, raw.types.MessageMediaDocument)):
        telegram.register(media)          # so the fake Telegram can echo it back
    raw_message = raw.types.Message(
        id=next(_MSG_IDS), peer_id=raw.types.PeerUser(user_id=user_id),
        from_id=raw.types.PeerUser(user_id=user_id), date=NOW, message=text,
        entities=list(entities), media=media, grouped_id=grouped_id,
        invert_media=invert, restriction_reason=[],
    )
    # Like a live update, the users list carries everyone a mention links to.
    users = {user_id: _raw_user(user_id)}
    for entity in entities:
        if isinstance(entity, raw.types.MessageEntityMentionName):
            users[entity.user_id] = _raw_user(entity.user_id)
    return await types.Message._parse(app, raw_message, users, {}, replies=0)


# A post using every formatting the spec lists. Emoji BEFORE the entities
# make Python string positions differ from Telegram's UTF-16 offsets.
RICH_TEXT = "🔥🔥 Bold Italic Under Strike Spoil mono code link 👍 x"


def rich_entities():
    def at(word):                          # UTF-16 offset, as Telegram counts
        start = RICH_TEXT.index(word)
        return len(RICH_TEXT[:start].encode("utf-16-le")) // 2

    return [
        raw.types.MessageEntityBold(offset=at("Bold"), length=4),
        raw.types.MessageEntityItalic(offset=at("Bold"), length=11),      # nested over Bold
        raw.types.MessageEntityUnderline(offset=at("Under"), length=5),
        raw.types.MessageEntityStrike(offset=at("Strike"), length=6),
        raw.types.MessageEntitySpoiler(offset=at("Spoil"), length=5),
        raw.types.MessageEntityCode(offset=at("mono"), length=4),
        raw.types.MessageEntityPre(offset=at("code"), length=4, language="python"),
        raw.types.MessageEntityTextUrl(offset=at("link"), length=4, url="https://example.com/p?q=1"),
        raw.types.MessageEntityCustomEmoji(offset=at("👍"), length=2, document_id=CUSTOM_EMOJI_ID),
        raw.types.MessageEntityBold(offset=at("👍"), length=2),          # custom emoji inside bold
    ]


def wire(entities):
    """Comparable view of raw entities as they travel to Telegram."""
    out = []
    for e in entities or []:
        out.append((type(e).__name__, e.offset, e.length, getattr(e, "url", None),
                    getattr(e, "language", None), getattr(e, "document_id", None),
                    getattr(e, "collapsed", None)))
    return sorted(out, key=repr)


def query(user_id, data, chat_id=None):
    message = MagicMock()
    message.chat.id = chat_id or user_id
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.delete = AsyncMock()
    q = MagicMock(spec=types.CallbackQuery)        # isinstance(q, CallbackQuery) holds
    q.from_user = MagicMock(id=user_id, first_name="Admin", last_name=None, username=None)
    q.message = message
    q.data = f"sch:{data}"
    q.answer = AsyncMock()
    return q


def text_msg(user_id, text):
    msg = AsyncMock()
    msg.from_user = MagicMock(id=user_id, first_name="Admin")
    msg.chat = MagicMock(id=user_id, type=ChatType.PRIVATE)
    msg.text, msg.caption, msg.media_group_id = text, None, None
    return msg


async def press(user_id, data):
    q = query(user_id, data)
    await sch.schedule_callbacks(app, q)
    return q


def edited(q):
    """(text, markup) of the panel a callback produced."""
    call = q.message.edit_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


def callbacks(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


def labels(markup):
    return [b.text for row in (markup.inline_keyboard if markup else []) for b in row]


async def open_channel(user_id, channel_id):
    """/Schedule -> Channel ID: returns the list reply (text, markup)."""
    sch._new_session(user_id)
    msg = text_msg(user_id, str(channel_id))
    assert await sch.handle_input(msg) is True
    call = msg.reply_text.await_args
    return call.args[0], call.kwargs.get("reply_markup")


async def schedule_doc(channel_id, *, when=None, status=store.STATUS_SCHEDULED,
                       text="Welcome Post", entities=None, creator=OWNER, **extra):
    content = {"kind": "text", "text": text, "entities": entities or [], "plain_text": False}
    doc = await store.create_schedule(
        channel_id=channel_id, channel_title="Test channel", creator_id=creator,
        content=content, digest=content_mod.content_hash(content),
        scheduled_at=when or NOW + 3600, draft_id=os.urandom(8).hex(),
    )
    if status != store.STATUS_SCHEDULED or extra:
        await store.scheduled_db.update_one({"_id": doc["_id"]}, {"$set": {"status": status, **extra}})
    return await store.get_schedule(doc["schedule_id"])


async def daily_doc(channel_id, hour, minute, *, first=None, status=store.STATUS_SCHEDULED,
                    text="Daily Post", entities=None, creator=OWNER, start=None, **extra):
    """A daily schedule stored exactly as the wizard stores one."""
    content = {"kind": "text", "text": text, "entities": entities or [], "plain_text": False}
    doc = await store.create_schedule(
        channel_id=channel_id, channel_title="Test channel", creator_id=creator,
        content=content, digest=content_mod.content_hash(content),
        scheduled_at=first or st.first_daily_run(hour, minute), draft_id=os.urandom(8).hex(),
        kind=store.TYPE_DAILY, daily_time=(hour, minute), start_date=start or st.ist_today().isoformat(),
    )
    if status != store.STATUS_SCHEDULED or extra:
        await store.scheduled_db.update_one({"_id": doc["_id"]}, {"$set": {"status": status, **extra}})
    return await store.get_schedule(doc["schedule_id"])


async def _new_screens(channel_id):
    """Every screen added with One-Time / Daily schedules (for callback checks)."""
    active = await daily_doc(channel_id, 8, 0, text="d1")
    paused = await daily_doc(channel_id, 9, 0, text="d2", status=store.STATUS_PAUSED)
    draft = {"kind": "text", "text": "t", "entities": []}
    daily = {"step": "confirm", "mode": "new", "kind": "daily", "channel_id": channel_id,
             "channel_title": "T", "draft": draft, "hour": 10, "daily_time": (10, 5),
             "scheduled_at": NOW + 3600}
    retime = dict(daily, mode="retime", target=active["schedule_id"], current_daily=[8, 0])
    return [sch.render_home(), sch.render_ask_channel({"purpose": "create", "kind": "daily"}),
            sch.render_ask_channel({"purpose": "manage"}), sch.render_types({"channel_title": "T"}),
            sch.render_item(active), sch.render_item(paused), sch.render_scheduled(active),
            sch.render_ask(active, "delete"), sch.render_hours(daily), sch.render_minutes(daily),
            sch.render_confirm(daily), sch.render_hours(retime), sch.render_confirm(retime)]


async def stored_wire(content):
    """The raw entities a stored post becomes on the wire."""
    entities = content_mod.deserialize_entities(content["entities"])
    for entity in entities:
        entity._client = app
    return wire([await entity.write() for entity in entities])


def u16_slice(text, offset, length):
    """The characters a Telegram entity (UTF-16 offset/length) covers."""
    data = text.encode("utf-16-le")
    return data[2 * offset: 2 * (offset + length)].decode("utf-16-le")


async def open_home(user_id=OWNER):
    msg = text_msg(user_id, "/Schedule")
    with pytest.raises(StopPropagation):
        await sch.schedule_command(app, msg)
    call = msg.reply_text.await_args
    return call.args[0], call.kwargs["reply_markup"]


def ist(day: int, hour: int, minute: int, month: int = 11, year: int = 2026) -> int:
    """Epoch seconds of an IST wall-clock time (November 2026 by default)."""
    return st.ist_timestamp(date(year, month, day), hour, minute)


# ---------------------------------------------------------------------------
# IST date/time handling
# ---------------------------------------------------------------------------

class TestIST:
    def test_ist_wall_time_to_utc_and_back(self):
        ts = st.ist_timestamp(date(2026, 11, 5), 22, 30)
        assert ts == int(datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc).timestamp())
        assert (st.fmt_date(ts), st.fmt_time(ts)) == ("05-11-2026", "10:30 PM IST")

    def test_date_boundary(self):
        # 02:00 IST on 05-11 is still 04-11 in UTC (20:30).
        ts = st.ist_timestamp(date(2026, 11, 5), 2, 0)
        assert datetime.fromtimestamp(ts, timezone.utc).strftime("%d %H:%M") == "04 20:30"
        assert (st.fmt_date(ts), st.fmt_time(ts)) == ("05-11-2026", "02:00 AM IST")

    @pytest.mark.parametrize("text, expected", [
        ("10:30 PM", (22, 30)), ("10:30pm", (22, 30)), ("10.30 p.m.", (22, 30)),
        ("12:00 AM", (0, 0)), ("12:30 PM", (12, 30)), ("12 am", (0, 0)), ("9 PM", (21, 0)),
        ("09:05 am", (9, 5)), ("22:30", (22, 30)), ("00:00", (0, 0)),
        ("13:00 PM", None), ("0:30 AM", None), ("24:00", None), ("10:60", None),
        ("ten", None), ("", None),
    ])
    def test_time_parsing(self, text, expected):
        assert st.parse_time(text) == expected

    @pytest.mark.parametrize("text, expected", [
        ("05-11-2026", date(2026, 11, 5)), ("5/11/2026", date(2026, 11, 5)),
        ("05.11.2026", date(2026, 11, 5)), ("31-02-2026", None), ("2026-11-05", None),
        ("11/05", None), ("", None),
    ])
    def test_date_parsing_is_day_first(self, text, expected):
        assert st.parse_date(text) == expected

    def test_twelve_hour_display(self):
        assert [st.fmt_hour(h) for h in (0, 1, 11, 12, 13, 23)] == \
            ["12 AM", "01 AM", "11 AM", "12 PM", "01 PM", "11 PM"]
        assert st.fmt_hhmm(0, 5) == "12:05 AM" and st.fmt_hhmm(12, 0) == "12:00 PM"

    def test_independent_of_the_server_timezone(self):
        expected = st.ist_timestamp(date(2026, 11, 5), 22, 30)
        old = os.environ.get("TZ")
        try:
            for zone in ("America/Los_Angeles", "Asia/Tokyo", "UTC"):
                os.environ["TZ"] = zone
                time.tzset()
                assert st.ist_timestamp(date(2026, 11, 5), 22, 30) == expected
                assert st.fmt_time(expected) == "10:30 PM IST"
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_past_and_range_rules(self):
        assert st.time_error(NOW - 1) == "That time is in the past."
        assert st.time_error(NOW) == "That time is in the past."
        assert "1 minute" in st.time_error(NOW + 30)
        assert st.time_error(NOW + 60) is None
        assert st.time_error(NOW + 400 * 86400) is not None
        today = st.ist_today()
        assert today == date(2026, 11, 4)
        assert st.date_error(date(2026, 11, 3)) == "That date is in the past."
        assert st.date_error(today) is None
        assert st.hour_is_open(today, 12) and not st.hour_is_open(today, 11)
        assert not st.minute_is_open(today, 12, 0) and st.minute_is_open(today, 12, 5)


# ---------------------------------------------------------------------------
# Capture + storage form of the formatting
# ---------------------------------------------------------------------------

class TestCapture:
    async def test_every_formatting_entity_is_stored(self):
        msg = await admin_message(RICH_TEXT, entities=rich_entities())
        content = content_mod.extract_content(msg)
        assert content["kind"] == "text" and content["text"] == RICH_TEXT
        types_stored = sorted(e["type"] for e in content["entities"])
        assert types_stored == sorted(["BOLD", "ITALIC", "UNDERLINE", "STRIKETHROUGH", "SPOILER",
                                       "CODE", "PRE", "TEXT_LINK", "CUSTOM_EMOJI", "BOLD"])
        emoji = next(e for e in content["entities"] if e["type"] == "CUSTOM_EMOJI")
        assert emoji["custom_emoji_id"] == str(CUSTOM_EMOJI_ID)          # exact 64-bit id
        assert next(e for e in content["entities"] if e["type"] == "PRE")["language"] == "python"
        # Rebuilt entities serialize to exactly the raw entities Telegram sent.
        rebuilt = content_mod.deserialize_entities(content["entities"])
        written = []
        for entity in rebuilt:
            entity._client = app
            written.append(await entity.write())
        assert wire(written) == wire(rich_entities())

    async def test_storage_form_survives_mongodb(self, channel):
        msg = await admin_message(RICH_TEXT, entities=rich_entities())
        content = content_mod.extract_content(msg)
        doc = await schedule_doc(channel.id, text=RICH_TEXT, entities=content["entities"])
        stored = (await store.get_schedule(doc["schedule_id"]))["content"]["entities"]
        assert stored == content["entities"]

    async def test_blockquote_mentions_and_auto_entities(self):
        text = "quote @someone #tag https://t.me/x hi"
        entities = [
            raw.types.MessageEntityBlockquote(offset=0, length=5, collapsed=True),
            raw.types.MessageEntityMention(offset=6, length=8),
            raw.types.MessageEntityHashtag(offset=15, length=4),
            raw.types.MessageEntityUrl(offset=20, length=12),
            raw.types.MessageEntityMentionName(offset=33, length=2, user_id=4242),
        ]
        msg = await admin_message(text, entities=entities)
        stored = content_mod.extract_content(msg)["entities"]
        assert {e["type"] for e in stored} == {"BLOCKQUOTE", "MENTION", "HASHTAG", "URL", "TEXT_MENTION"}
        assert next(e for e in stored if e["type"] == "BLOCKQUOTE")["expandable"] is True
        assert next(e for e in stored if e["type"] == "TEXT_MENTION")["user_id"] == 4242

    async def test_plain_text_mode_drops_formatting_and_keeps_text(self):
        msg = await admin_message(RICH_TEXT, entities=rich_entities())
        plain = content_mod.extract_content(msg, plain_text=True)
        assert plain["text"] == RICH_TEXT and plain["entities"] == [] and plain["plain_text"]
        assert content_mod.make_plain(content_mod.extract_content(msg))["entities"] == []

    async def test_media_caption_spoiler_and_layout(self):
        caption = "Big ✨ news"
        media = raw_media("photo", 1, spoiler=True)
        msg = await admin_message(caption, media=media, invert=True,
                                  entities=[raw.types.MessageEntityBold(offset=0, length=3)])
        content = content_mod.extract_content(msg)
        assert content["kind"] == "photo" and content["text"] == caption
        assert content["file_id"] == msg.photo.file_id
        assert content["has_spoiler"] is True and content["caption_above"] is True
        assert content["entities"] == [{"type": "BOLD", "offset": 0, "length": 3}]

    @pytest.mark.parametrize("kind", ["sticker", "location"])
    async def test_unsupported_messages_are_refused(self, kind):
        msg = await admin_message("", media=raw_media(kind, 2))
        with pytest.raises(content_mod.ContentError):
            content_mod.extract_content(msg)

    async def test_length_limits(self):
        with pytest.raises(content_mod.ContentError, match="too long"):
            content_mod.extract_content(await admin_message("x" * 4097))
        with pytest.raises(content_mod.ContentError, match="caption is too long"):
            content_mod.extract_content(await admin_message("y" * 1025, media=raw_media("photo", 3)))
        assert content_mod.extract_content(await admin_message("z" * 4096))["text"] == "z" * 4096
        assert content_mod.extract_content(
            await admin_message("y" * 1024, media=raw_media("photo", 4)))["text"] == "y" * 1024


# ---------------------------------------------------------------------------
# Delivery through Kurigram's real send methods (what goes on the wire)
# ---------------------------------------------------------------------------

class TestDelivery:
    async def test_text_formatting_and_custom_emoji_reach_telegram_unchanged(self, telegram, channel):
        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        sent, warnings = await content_mod.send_content(app, channel.id, content)
        request = telegram.to(channel.id)[-1]
        assert isinstance(request, raw.functions.messages.SendMessage)
        assert request.message == RICH_TEXT
        assert wire(request.entities) == wire(rich_entities())
        assert any(isinstance(e, raw.types.MessageEntityCustomEmoji) and e.document_id == CUSTOM_EMOJI_ID
                   for e in request.entities)
        assert isinstance(sent, types.Message) and warnings == []
        assert content_mod.custom_emoji_delivery(content, sent) == (1, 1)

    @pytest.mark.parametrize("kind", ["photo", "video", "animation", "document", "audio", "voice"])
    async def test_media_with_formatted_caption(self, telegram, channel, kind):
        media = raw_media(kind, 10, spoiler=True if kind in ("photo", "video", "animation") else None)
        entities = [raw.types.MessageEntityItalic(offset=0, length=4),
                    raw.types.MessageEntityCustomEmoji(offset=5, length=2, document_id=CUSTOM_EMOJI_ID)]
        msg = await admin_message("Look 🎉 now", media=media, entities=entities, telegram=telegram)
        content = content_mod.extract_content(msg)
        await content_mod.send_content(app, channel.id, content)
        request = telegram.to(channel.id)[-1]
        assert isinstance(request, raw.functions.messages.SendMedia)
        inner = getattr(media, "photo", None) or media.document
        assert request.media.id.id == inner.id and request.media.id.access_hash == inner.access_hash
        assert request.message == "Look 🎉 now"
        assert wire(request.entities) == wire(entities)
        if kind in ("photo", "video", "animation"):
            assert request.media.spoiler is True

    async def test_plain_text_is_never_reparsed_as_html(self, telegram, channel):
        text = "5 < 6 & <b>not bold</b> — literally"
        content = content_mod.extract_content(await admin_message(text), plain_text=True)
        await content_mod.send_content(app, channel.id, content)
        request = telegram.to(channel.id)[-1]
        assert request.message == text and not request.entities

    async def test_link_preview_choice_is_kept(self, telegram, channel):
        url = "https://example.com"
        msg = await admin_message(f"see {url}", entities=[raw.types.MessageEntityUrl(offset=4, length=19)])
        assert msg.link_preview_options.is_disabled          # the admin sent it without preview
        await content_mod.send_content(app, channel.id, content_mod.extract_content(msg))
        assert telegram.to(channel.id)[-1].no_webpage is True

    async def test_unresolvable_text_mention_degrades_to_plain_text(self, telegram, channel, monkeypatch):
        from SWAGGYMUSIC.utils.user_resolution import UserResolutionError

        async def fail(*args, **kwargs):
            raise UserResolutionError(UserResolutionError.UNRESOLVABLE, 4242)
        monkeypatch.setattr(content_mod, "ensure_user_peer", fail)
        msg = await admin_message("hi Bob", entities=[raw.types.MessageEntityMentionName(offset=3, length=3, user_id=4242),
                                                      raw.types.MessageEntityBold(offset=0, length=2)])
        _sent, warnings = await content_mod.send_content(app, channel.id, content_mod.extract_content(msg))
        request = telegram.to(channel.id)[-1]
        assert request.message == "hi Bob"
        assert wire(request.entities) == wire([raw.types.MessageEntityBold(offset=0, length=2)])
        assert warnings and "mention" in warnings[0]

    async def test_custom_emoji_downgrade_is_detected(self, telegram, channel):
        telegram.strip_custom_emoji = True
        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        sent, _w = await content_mod.send_content(app, channel.id, content)
        assert content_mod.custom_emoji_delivery(content, sent) == (1, 0)


# ---------------------------------------------------------------------------
# Access: secret, SUDO only, channel admins only
# ---------------------------------------------------------------------------

class TestAccess:
    @pytest.fixture
    def me(self, monkeypatch):
        monkeypatch.setattr(app, "me", types.User(id=app.id, first_name="Bot", username="stub_bot",
                                                  is_bot=True, client=app))

    def _command(self, user_id, text):
        chat = types.Chat(id=user_id, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=next(_MSG_IDS), chat=chat, text=text, client=app,
                            from_user=types.User(id=user_id, first_name="U", client=app))
        msg.reply_text = AsyncMock()
        return msg

    @pytest.mark.parametrize("text", ["/Schedule", "/schedule", "/SCHEDULE", "/Schedule@stub_bot"])
    async def test_sudo_opens_it_case_insensitively(self, me, text):
        msg = self._command(OWNER, text)
        ran = await dispatch(msg)
        assert ran[0] == "schedule_command" and len(ran) == 1, "handled first, then propagation stops"
        assert OWNER in sch.SESSIONS and sch.SESSIONS[OWNER]["step"] == "home"
        call = msg.reply_text.await_args
        assert "Sᴄʜᴇᴅᴜʟᴇᴅ Pᴏsᴛs" in call.args[0]
        assert callbacks(call.kwargs["reply_markup"]) == ["sch:ty:once", "sch:ty:daily", "sch:mg", "sch:abort"]
        assert labels(call.kwargs["reply_markup"]) == [
            "📌 Oɴᴇ-Tɪᴍᴇ Sᴄʜᴇᴅᴜʟᴇ", "🔁 Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ", "📋 Mᴀɴᴀɢᴇ Sᴄʜᴇᴅᴜʟᴇs", "❌ Cᴀɴᴄᴇʟ"]

    async def test_everyone_else_gets_no_answer(self, me):
        msg = self._command(STRANGER, "/Schedule")
        ran = await dispatch(msg)
        assert "schedule_command" not in ran and "schedule_input" not in ran
        assert STRANGER not in sch.SESSIONS
        msg.reply_text.assert_not_awaited()

    async def test_buttons_refuse_non_sudo_users(self, channel):
        sid = (await schedule_doc(channel.id))["schedule_id"]
        for data in (f"ls:{channel.id}:p:0", f"it:{sid}:p:0", f"dy:{sid}", "new:1"):
            q = await press(STRANGER, data)
            q.answer.assert_awaited_with("Not available.", show_alert=True)
            q.message.edit_text.assert_not_awaited()
        assert await store.get_schedule(sid) is not None

    async def test_sudo_who_is_not_a_channel_admin_is_refused(self, channel, sudo_admin):
        channel.user_status = ChatMemberStatus.MEMBER
        text, markup = await open_channel(sudo_admin, channel.id)
        assert "not an admin" in text
        assert callbacks(markup) == ["sch:retry", "sch:abort"]
        sid = (await schedule_doc(channel.id))["schedule_id"]
        q = await press(sudo_admin, f"it:{sid}:p:0")          # crafted button, other channel
        q.answer.assert_any_await("You can't manage this channel.", show_alert=True)
        q = await press(sudo_admin, f"dy:{sid}")
        assert await store.get_schedule(sid) is not None

    async def test_owner_bypasses_the_channel_admin_check(self, channel):
        channel.user_status = ChatMemberStatus.MEMBER
        text, _markup = await open_channel(OWNER, channel.id)
        assert "No scheduled posts found for this channel." in text

    async def test_not_in_help_menu_or_start_message(self):
        from strings import get_string, helpers
        from SWAGGYMUSIC.utils.inline.help import help_pannel
        from SWAGGYMUSIC.utils.inline.start import private_panel, start_panel

        _ = get_string("en")
        pages = [v for k, v in vars(helpers).items() if k.startswith("HELP_")]
        assert len(pages) >= 6
        buttons = [b for markup in (help_pannel(_), InlineKeyboardMarkup(private_panel(_)),
                                    InlineKeyboardMarkup(start_panel(_)))
                   for row in markup.inline_keyboard for b in row]
        everything = "\n".join(pages + [str(v) for v in _.values()]
                               + [f"{b.text} {b.callback_data}" for b in buttons]).lower()
        # (HELP_1 already says "already-scheduled deletion jobs" about /setdelay,
        # so the check targets the command and this feature's own names.)
        for spelling in ("schedule", "/sᴄʜᴇᴅᴜʟᴇ", "/ꜱᴄʜᴇᴅᴜʟᴇ", "sᴄʜᴇᴅᴜʟᴇᴅ ᴘᴏsᴛ",
                         "sᴄʜᴇᴅᴜʟᴇ ᴍᴀɴᴀɢᴇʀ", "sch:"):
            assert spelling not in everything, spelling


# ---------------------------------------------------------------------------
# Channel ID validation
# ---------------------------------------------------------------------------

class TestChannelValidation:
    @pytest.mark.parametrize("text", ["abc", "12345", "-12345", "@mychannel", "-100", ""])
    async def test_malformed_ids(self, channel, text):
        reply, markup = await open_channel(OWNER, text or " ")
        assert "Invalid Channel ID" in reply
        assert callbacks(markup) == ["sch:retry", "sch:abort"]

    async def test_unreachable_channel(self, channel):
        channel.reachable = False
        reply, markup = await open_channel(OWNER, channel.id)
        assert "can't access that channel" in reply and callbacks(markup) == ["sch:retry", "sch:abort"]

    async def test_groups_are_not_channels(self, channel):
        channel.chat_type = ChatType.SUPERGROUP
        reply, _ = await open_channel(OWNER, channel.id)
        assert "not a channel" in reply

    async def test_bot_needs_post_rights(self, channel):
        channel.bot_can_post = False
        reply, _ = await open_channel(OWNER, channel.id)
        assert "Post Messages" in reply

    async def test_retry_and_cancel(self, channel):
        await open_channel(OWNER, "nope")
        q = await press(OWNER, "retry")
        text, markup = edited(q)
        assert "Send the Channel ID" in text and sch.SESSIONS[OWNER]["step"] == "channel"
        q = await press(OWNER, "abort")
        assert "Sᴄʜᴇᴅᴜʟᴇ Mᴀɴᴀɢᴇʀ Cʟᴏsᴇᴅ" in edited(q)[0] and OWNER not in sch.SESSIONS


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

class TestList:
    async def test_empty_list(self, channel):
        text, markup = await open_channel(OWNER, channel.id)
        assert "No scheduled posts found for this channel." in text
        assert callbacks(markup) == [f"sch:new:{channel.id}", f"sch:ls:{channel.id}:p:0",
                                     f"sch:ls:{channel.id}:h:0", "sch:x"]
        assert labels(markup) == ["➕ Aᴅᴅ Nᴇᴡ", "🔄 Rᴇғʀᴇsʜ", "📜 Hɪsᴛᴏʀʏ", "❌ Cʟᴏsᴇ"]

    async def test_posts_with_ist_times_and_status(self, channel):
        a = await schedule_doc(channel.id, text="Welcome Post", when=st.ist_timestamp(date(2026, 11, 5), 22, 30))
        b = await schedule_doc(channel.id, text="Announcement", when=st.ist_timestamp(date(2026, 11, 6), 9, 0),
                               status=store.STATUS_FAILED, error={"code": "X", "message": "m"})
        await schedule_doc(channel.id, text="Old news", status=store.STATUS_SENT)
        text, markup = await open_channel(OWNER, channel.id)
        assert (f"<b>1.</b> Welcome Post · <code>#{a['schedule_id']}</code>\n"
                "   📅 05-11-2026\n   ⏰ 10:30 PM IST\n   Type: 📌 One-Time\n   Status: 🟢 Scheduled") in text
        assert (f"<b>2.</b> Announcement · <code>#{b['schedule_id']}</code>\n"
                "   📅 06-11-2026\n   ⏰ 09:00 AM IST\n   Type: 📌 One-Time\n   Status: 🔴 Failed") in text
        assert "Old news" not in text, "sent posts are not pending"
        assert callbacks(markup)[:2] == [f"sch:it:{a['schedule_id']}:p:0", f"sch:it:{b['schedule_id']}:p:0"]
        q = await press(OWNER, f"ls:{channel.id}:h:0")
        assert "Old news" in edited(q)[0] and "Welcome Post" not in edited(q)[0]

    async def test_pagination(self, channel):
        for i in range(12):
            await schedule_doc(channel.id, text=f"Post {i:02d}", when=NOW + 3600 * (i + 1))
        text, markup = await open_channel(OWNER, channel.id)
        assert "Post 00" in text and "Post 04" in text and "Post 05" not in text
        assert "Page 1/3 · 12 posts" in text
        assert f"sch:ls:{channel.id}:p:1" in callbacks(markup)
        q = await press(OWNER, f"ls:{channel.id}:p:2")
        page3, markup3 = edited(q)
        assert "Post 10" in page3 and "Post 11" in page3 and "Post 09" not in page3
        assert "<b>11.</b>" in page3 and f"sch:ls:{channel.id}:p:1" in callbacks(markup3)

    async def test_refresh_shows_changes(self, channel):
        await open_channel(OWNER, channel.id)
        await schedule_doc(channel.id, text="Fresh one")
        q = await press(OWNER, f"ls:{channel.id}:p:0")
        assert "Fresh one" in edited(q)[0]


# ---------------------------------------------------------------------------
# Add New — the whole wizard
# ---------------------------------------------------------------------------

async def start_compose(channel_id, *, kind="once", user_id=OWNER):
    """List -> ➕ Add New -> schedule type -> the composer."""
    await open_channel(user_id, channel_id)
    q = await press(user_id, f"new:{channel_id}")
    assert sch.SESSIONS[user_id]["step"] == "types"
    q = await press(user_id, f"ty:{kind}")
    assert sch.SESSIONS[user_id]["step"] == "compose" and sch.SESSIONS[user_id]["kind"] == kind
    return q


async def compose_and_preview(channel_id, telegram, *, text=RICH_TEXT, entities=None, media=None,
                              user_id=OWNER, kind="once"):
    q = await start_compose(channel_id, kind=kind, user_id=user_id)
    msg = await admin_message(text, entities=rich_entities() if entities is None else entities,
                              media=media, user_id=user_id, telegram=telegram)
    assert await sch.handle_input(msg) is True
    return q


async def pick(user_id, day: date, hour: int, minute: int):
    await press(user_id, f"day:{day.strftime('%Y%m%d')}")
    await press(user_id, f"hr:{hour:02d}")
    return await press(user_id, f"mn:{minute:02d}")


class TestAddNew:
    async def test_full_flow_preserves_formatting_through_save_and_delivery(self, channel, telegram, clock):
        await compose_and_preview(channel.id, telegram)
        session = sch.SESSIONS[OWNER]
        assert session["step"] == "preview"
        preview = telegram.to(OWNER)
        assert wire(preview[0].entities) == wire(rich_entities()), "exact preview first"
        assert "Pʀᴇᴠɪᴇᴡ" in preview[1].message and "1 custom emoji" in preview[1].message
        q = await press(OWNER, "nx")
        assert "Sᴇʟᴇᴄᴛ Dᴀᴛᴇ" in edited(q)[0]
        q = await pick(OWNER, date(2026, 11, 5), 22, 30)
        text, markup = edited(q)
        assert "Cᴏɴғɪʀᴍ Sᴄʜᴇᴅᴜʟᴇᴅ Pᴏsᴛ" in text
        assert f"📌 <b>Channel:</b> <code>{channel.id}</code>" in text
        assert "🗂 <b>Schedule Type:</b> 📌 One-Time" in text
        assert "📅 <b>Start Date:</b> 05-11-2026" in text and "⏰ <b>Time:</b> 10:30 PM IST" in text
        assert "⏭ <b>Next Execution:</b> 05-11-2026 10:30 PM IST" in text
        assert labels(markup) == ["✅ Cᴏɴғɪʀᴍ Sᴄʜᴇᴅᴜʟᴇ", "📅 Cʜᴀɴɢᴇ Dᴀᴛᴇ", "🕒 Cʜᴀɴɢᴇ Tɪᴍᴇ", "✏️ Eᴅɪᴛ Mᴇssᴀɢᴇ", "❌ Cᴀɴᴄᴇʟ"]
        q = await press(OWNER, "ok")
        assert "Pᴏsᴛ Sᴄʜᴇᴅᴜʟᴇᴅ" in edited(q)[0]
        docs = await store.scheduled_db.find({"channel_id": channel.id}).to_list(None)
        assert len(docs) == 1
        doc = docs[0]
        assert doc["status"] == "scheduled" and doc["creator_id"] == OWNER
        assert doc["scheduled_at"] == int(datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc).timestamp())
        assert doc["timezone"] == "Asia/Kolkata" and doc["content"]["text"] == RICH_TEXT
        for field in ("schedule_id", "channel_id", "creator_id", "content", "scheduled_at",
                      "status", "created_at", "updated_at"):
            assert field in doc, field
        # … and at the scheduled time the worker delivers the SAME entities.
        clock.now = doc["scheduled_at"]
        assert await sch.process_due() == 1
        delivered = telegram.to(channel.id)
        assert len(delivered) == 1 and delivered[0].message == RICH_TEXT
        assert wire(delivered[0].entities) == wire(rich_entities())
        sent = await store.get_schedule(doc["schedule_id"])
        assert sent["status"] == "sent" and isinstance(sent["sent_message_id"], int)

    async def test_media_post_flow(self, channel, telegram, clock):
        media = raw_media("photo", 31, spoiler=True)
        await compose_and_preview(channel.id, telegram, text="Caption ✨", media=media,
                                  entities=[raw.types.MessageEntityUnderline(offset=0, length=7)])
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 4), 18, 0)
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["content"]["kind"] == "photo" and doc["content"]["has_spoiler"] is True
        clock.now = doc["scheduled_at"] + 2
        await sch.process_due()
        request = telegram.to(channel.id)[0]
        assert isinstance(request, raw.functions.messages.SendMedia)
        assert request.media.id.id == media.photo.id and request.media.spoiler is True
        assert wire(request.entities) == wire([raw.types.MessageEntityUnderline(offset=0, length=7)])

    async def test_typed_date_and_time(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        msg = text_msg(OWNER, "06-11-2026 9:15 am")
        await sch.handle_input(msg)
        assert "⏰ <b>Time:</b> 09:15 AM IST" in msg.reply_text.await_args.args[0]
        assert sch.SESSIONS[OWNER]["scheduled_at"] == st.ist_timestamp(date(2026, 11, 6), 9, 15)
        await press(OWNER, "cd")                                  # 📅 Change Date: typed, time kept
        await sch.handle_input(text_msg(OWNER, "07-11-2026"))
        assert sch.SESSIONS[OWNER]["scheduled_at"] == st.ist_timestamp(date(2026, 11, 7), 9, 15)
        await press(OWNER, "ct")                                  # 🕒 Change Time: typed, date kept
        msg = text_msg(OWNER, "11:59 PM")
        await sch.handle_input(msg)
        assert sch.SESSIONS[OWNER]["scheduled_at"] == st.ist_timestamp(date(2026, 11, 7), 23, 59)

    async def test_past_dates_and_times_are_rejected(self, channel, telegram, clock):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        q = await press(OWNER, "day:20261103")
        q.answer.assert_any_await("That date is in the past.", show_alert=True)
        _text, markup = edited(await press(OWNER, "cal:202611"))
        assert "sch:day:20261103" not in callbacks(markup) and "sch:day:20261104" in callbacks(markup)
        await press(OWNER, "day:20261104")                     # today, 12:00 PM now
        q = await press(OWNER, "hr:11")
        q.answer.assert_any_await("That time is in the past.", show_alert=True)
        q = await press(OWNER, "hr:12")
        assert "sch:mn:00" not in callbacks(edited(q)[1]) and "sch:mn:05" in callbacks(edited(q)[1])
        msg = text_msg(OWNER, "11:30 AM")
        await sch.handle_input(msg)
        assert "That time is in the past." in msg.reply_text.await_args.args[0]
        msg = text_msg(OWNER, "12:00 PM")                     # exactly now
        await sch.handle_input(msg)
        assert "past" in msg.reply_text.await_args.args[0]
        await press(OWNER, "mn:30")                              # 12:30 PM — fine
        assert sch.SESSIONS[OWNER]["step"] == "confirm"
        clock.now = st.ist_timestamp(date(2026, 11, 4), 12, 31)  # time passes…
        q = await press(OWNER, "ok")
        q.answer.assert_any_await("That time is in the past.", show_alert=True)
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 0
        assert sch.SESSIONS[OWNER]["step"] == "hour" and "Pick a new time" in edited(q)[0]

    async def test_double_confirm_creates_one_schedule(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 10, 0)
        await press(OWNER, "ok")
        q = await press(OWNER, "ok")
        q.answer.assert_any_await("Already scheduled.", show_alert=True)
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 1

    async def test_two_simultaneous_confirm_taps_create_one_schedule(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 10, 0)
        await asyncio.gather(press(OWNER, "ok"), press(OWNER, "ok"), press(OWNER, "ok"))
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 1

    async def test_identical_post_at_the_same_time_is_refused(self, channel, telegram):
        for _ in range(2):
            await compose_and_preview(channel.id, telegram)
            await press(OWNER, "nx")
            q = await pick(OWNER, date(2026, 11, 5), 10, 0)
            q = await press(OWNER, "ok")
        q.answer.assert_any_await("An identical post is already scheduled for that time.", show_alert=True)
        assert "already scheduled" in edited(q)[0]
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 1

    async def test_plain_text_mode_in_the_composer(self, channel, telegram):
        await start_compose(channel.id)
        q = await press(OWNER, "pm")
        q.answer.assert_any_await("Plain text mode is ON — formatting will be removed.", show_alert=False)
        assert "Plain Text Mode:</b> ON" in edited(q)[0]
        await sch.handle_input(await admin_message(RICH_TEXT, entities=rich_entities(), telegram=telegram))
        draft = sch.SESSIONS[OWNER]["draft"]
        assert draft["entities"] == [] and draft["text"] == RICH_TEXT and draft["plain_text"]
        controls = telegram.to(OWNER)[-1].message
        assert "Plain text mode is active" in controls
        assert not telegram.to(OWNER)[-2].entities, "the preview is plain too"

    async def test_plain_text_and_restore_from_the_preview(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "pt")
        assert sch.SESSIONS[OWNER]["draft"]["entities"] == []
        assert not telegram.to(OWNER)[-2].entities
        await press(OWNER, "pt")                                # restore formatting
        assert len(sch.SESSIONS[OWNER]["draft"]["entities"]) == 10
        assert wire(telegram.to(OWNER)[-2].entities) == wire(rich_entities())

    async def test_edit_message_before_confirming(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 10, 0)
        q = await press(OWNER, "cm")
        assert "Cᴏᴍᴘᴏsᴇ Pᴏsᴛ" in edited(q)[0]
        await sch.handle_input(await admin_message("Second version", entities=[
            raw.types.MessageEntityStrike(offset=0, length=6)], telegram=telegram))
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 10, 0)
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["content"]["text"] == "Second version"
        assert doc["content"]["entities"] == [{"type": "STRIKETHROUGH", "offset": 0, "length": 6}]

    async def test_back_and_cancel_at_every_step(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        await press(OWNER, "day:20261105")
        await press(OWNER, "hr:10")
        assert "Sᴇʟᴇᴄᴛ Tɪᴍᴇ" in edited(await press(OWNER, "bk"))[0]       # minutes -> hours
        assert "Sᴇʟᴇᴄᴛ Dᴀᴛᴇ" in edited(await press(OWNER, "bk"))[0]       # hours -> date
        assert "Pʀᴇᴠɪᴇᴡ" in edited(await press(OWNER, "bk"))[0]          # date -> preview
        q = await press(OWNER, "abort")
        text, _ = edited(q)
        assert "Cancelled</b> — nothing was saved." in text and "No scheduled posts" in text
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 0
        # every wizard keyboard carries ❌ Cancel
        session = sch.SESSIONS[OWNER]
        session.update({"step": "compose", "mode": "new", "draft_id": "d"})
        for renderer in (sch.render_compose,):
            assert "sch:abort" in callbacks(renderer(session)[1])
        session.update({"day": "2026-11-05", "hour": 10, "scheduled_at": NOW + 7200,
                        "draft": {"kind": "text", "text": "t", "entities": []}})
        for rendered in (sch.render_calendar(session), sch.render_hours(session),
                         sch.render_minutes(session), sch.render_confirm(session),
                         sch.render_preview_controls(session)):
            assert "sch:abort" in callbacks(rendered[1])

    async def test_crafted_button_data_is_refused(self, channel, telegram):
        await compose_and_preview(channel.id, telegram)
        await press(OWNER, "nx")
        for data in ("cal:202613", "cal:202610", "cal:203001", "cal:2026", "day:20261332",
                     "day:20270101x", "day:abc"):
            q = await press(OWNER, data)
            q.answer.assert_any_await("Invalid option.", show_alert=True)
        assert sch.SESSIONS[OWNER]["step"] == "date" and sch.SESSIONS[OWNER]["cal"] is None
        _text, markup = edited(await press(OWNER, "cal:202612"))      # a real month still works
        assert "sch:day:20261225" in callbacks(markup)
        await press(OWNER, "day:20261225")
        for data in ("hr:24", "hr:-1", "hr:x"):
            (await press(OWNER, data)).answer.assert_any_await("Invalid option.", show_alert=True)
        await press(OWNER, "hr:10")
        (await press(OWNER, "mn:60")).answer.assert_any_await("Invalid option.", show_alert=True)
        (await press(OWNER, "zz")).answer.assert_any_await("Unknown option.", show_alert=True)
        q = await press(OWNER, f"ls:{channel.id}:p:-3")                   # negative page: first page
        assert "No scheduled posts found for this channel." in edited(q)[0]

    async def test_album_is_refused_once(self, channel, telegram):
        await start_compose(channel.id)
        replies = 0
        for i in range(3):
            msg = await admin_message("", media=raw_media("photo", 50 + i), grouped_id=999, telegram=telegram)
            msg.reply_text = AsyncMock()
            await sch.handle_input(msg)
            replies += msg.reply_text.await_count
        assert replies == 1 and sch.SESSIONS[OWNER]["draft"] is None


# ---------------------------------------------------------------------------
# Edit / Reschedule / Cancel / Delete
# ---------------------------------------------------------------------------

class TestManage:
    async def test_item_view(self, channel):
        doc = await schedule_doc(channel.id, when=st.ist_timestamp(date(2026, 11, 5), 22, 30))
        await open_channel(OWNER, channel.id)
        q = await press(OWNER, f"it:{doc['schedule_id']}:p:0")
        text, markup = edited(q)
        assert "📅 <b>Date:</b> 05-11-2026" in text and "⏰ <b>Time:</b> 10:30 PM IST" in text
        sid = doc["schedule_id"]
        assert callbacks(markup) == [f"sch:ed:{sid}", f"sch:rs:{sid}", f"sch:pv:{sid}",
                                     f"sch:ca:{sid}", f"sch:da:{sid}", f"sch:ls:{channel.id}:p:0"]

    async def test_edit_keeps_entities_and_status(self, channel, telegram):
        doc = await schedule_doc(channel.id)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"ed:{doc['schedule_id']}")
        assert sch.SESSIONS[OWNER]["step"] == "compose"
        await sch.handle_input(await admin_message(RICH_TEXT, entities=rich_entities(), telegram=telegram))
        controls = telegram.to(OWNER)[-1]
        assert "Sᴀᴠᴇ Cʜᴀɴɢᴇs" in str(controls.reply_markup)
        q = await press(OWNER, "nx")
        assert "Changes saved" in edited(q)[0]
        updated = await store.get_schedule(doc["schedule_id"])
        assert updated["status"] == "scheduled" and updated["scheduled_at"] == doc["scheduled_at"]
        entities = content_mod.deserialize_entities(updated["content"]["entities"])
        for e in entities:
            e._client = app
        assert wire([await e.write() for e in entities]) == wire(rich_entities())

    async def test_reschedule(self, channel):
        doc = await schedule_doc(channel.id, status=store.STATUS_FAILED,
                                 error={"code": "CHAT_WRITE_FORBIDDEN", "message": "x"})
        await open_channel(OWNER, channel.id)
        q = await press(OWNER, f"rs:{doc['schedule_id']}")
        assert "Rᴇsᴄʜᴇᴅᴜʟᴇ" in edited(q)[0]
        q = await pick(OWNER, date(2026, 11, 8), 7, 45)
        assert "Cᴏɴғɪʀᴍ Rᴇsᴄʜᴇᴅᴜʟᴇ" in edited(q)[0]
        q = await press(OWNER, "ok")
        assert "Rescheduled</b> to 08-11-2026 · 07:45 AM IST" in edited(q)[0]
        updated = await store.get_schedule(doc["schedule_id"])
        assert updated["scheduled_at"] == st.ist_timestamp(date(2026, 11, 8), 7, 45)
        assert updated["status"] == "scheduled" and "error" not in updated

    async def test_cancel_needs_confirmation_and_is_never_sent(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        q = await press(OWNER, f"ca:{sid}")
        assert callbacks(edited(q)[1]) == [f"sch:cy:{sid}", f"sch:it:{sid}:p:0"]
        assert (await store.get_schedule(sid))["status"] == "scheduled"
        q = await press(OWNER, f"cy:{sid}")
        assert "Schedule cancelled" in edited(q)[0]
        assert (await store.get_schedule(sid))["status"] == "cancelled"
        clock.now = doc["scheduled_at"] + 10
        assert await sch.process_due() == 0 and telegram.to(channel.id) == []
        q = await press(OWNER, f"ls:{channel.id}:h:0")
        assert "🚫 Cancelled" in edited(q)[0]

    async def test_delete_needs_confirmation(self, channel):
        doc = await schedule_doc(channel.id)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        q = await press(OWNER, f"da:{sid}")
        assert "can't be undone" in edited(q)[0]
        assert await store.get_schedule(sid) is not None
        q = await press(OWNER, f"dy:{sid}")
        assert f"Post <code>#{sid}</code> deleted" in edited(q)[0]
        assert await store.get_schedule(sid) is None

    async def test_nothing_changes_while_a_post_is_being_sent(self, channel):
        doc = await schedule_doc(channel.id, status=store.STATUS_SENDING, claimed_at=NOW, claim_token="t")
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        assert await store.update_content(sid, {"kind": "text", "text": "x", "entities": []}, "h", OWNER) is None
        assert await store.reschedule(sid, NOW + 9999, OWNER) is None
        assert await store.cancel_schedule(sid, OWNER) is None
        assert await store.delete_schedule(sid) is False
        for data in (f"ed:{sid}", f"rs:{sid}", f"ca:{sid}", f"da:{sid}", f"dy:{sid}"):
            await press(OWNER, data)
        assert (await store.get_schedule(sid))["status"] == "sending"

    async def test_preview_of_a_stored_post(self, channel, telegram):
        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        doc = await schedule_doc(channel.id, text=RICH_TEXT, entities=content["entities"])
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"pv:{doc['schedule_id']}")
        assert wire(telegram.to(OWNER)[0].entities) == wire(rich_entities())


# ---------------------------------------------------------------------------
# Storage guarantees (MongoDB layer)
# ---------------------------------------------------------------------------

def _new(channel_id, draft_id, **over):
    content = {"kind": "text", "text": "t", "entities": [], "plain_text": False}
    kwargs = dict(channel_id=channel_id, channel_title="T", creator_id=OWNER, content=content,
                  digest=content_mod.content_hash(content), scheduled_at=NOW + 600, draft_id=draft_id)
    kwargs.update(over)
    return store.create_schedule(**kwargs)


class TestStorage:
    async def test_indexes(self, monkeypatch):
        monkeypatch.setattr(store, "_indexes_ready", False)
        await store.ensure_schedule_indexes()
        await store.ensure_schedule_indexes()                      # idempotent
        info = await store.scheduled_db.index_information()
        assert info["schedule_id_1"]["key"] == [("schedule_id", 1)] and info["schedule_id_1"]["unique"]
        assert info["draft_id_1"]["unique"] and info["draft_id_1"]["sparse"]
        assert info["status_1_scheduled_at_1"]["key"] == [("status", 1), ("scheduled_at", 1)]
        assert info["channel_id_1_status_1_scheduled_at_1"]["key"] == \
            [("channel_id", 1), ("status", 1), ("scheduled_at", 1)]

    async def test_a_reused_confirm_token_is_refused(self, channel):
        first = await _new(channel.id, "tok-1")
        with pytest.raises(store.DuplicateDraft) as exc:
            await _new(channel.id, "tok-1")
        assert exc.value.existing["schedule_id"] == first["schedule_id"]
        assert await store.scheduled_db.count_documents({"draft_id": "tok-1"}) == 1

    async def test_racing_confirms_hit_the_unique_index(self, channel, monkeypatch):
        monkeypatch.setattr(store, "_indexes_ready", False)
        await store.ensure_schedule_indexes()
        real_next = store.next_schedule_id

        async def racing_next():      # the other tap inserts between the check and the insert
            await store.scheduled_db.insert_one({"schedule_id": 990001, "draft_id": "tok-race",
                                                 "channel_id": channel.id, "status": "scheduled"})
            return await real_next()
        monkeypatch.setattr(store, "next_schedule_id", racing_next)
        with pytest.raises(store.DuplicateDraft) as exc:
            await _new(channel.id, "tok-race")
        assert exc.value.existing["schedule_id"] == 990001
        assert await store.scheduled_db.count_documents({"draft_id": "tok-race"}) == 1

    async def test_schedule_ids_are_unique(self):
        ids = await asyncio.gather(*(store.next_schedule_id() for _ in range(25)))
        assert len(set(ids)) == 25

    async def test_only_the_claim_owner_records_the_outcome(self, channel):
        doc = await schedule_doc(channel.id, when=NOW)
        claimed = await store.claim_next_due(NOW, "A")
        assert claimed["status"] == "sending" and claimed["attempts"] == 1
        assert await store.claim_next_due(NOW, "B") is None           # nobody else can take it
        assert not await store.mark_failed(claimed, "B", code="X", message="m", now=NOW, uncertain=False)
        assert (await store.get_schedule(doc["schedule_id"]))["status"] == "sending"
        assert await store.mark_failed(claimed, "A", code="X", message="m", now=NOW, uncertain=False)
        assert (await store.get_schedule(doc["schedule_id"]))["status"] == "failed"

    async def test_status_rules(self, channel):
        sent = await schedule_doc(channel.id, status=store.STATUS_SENT)
        assert await store.cancel_schedule(sent["schedule_id"], OWNER) is None
        assert await store.update_content(sent["schedule_id"], {"kind": "text", "text": "x", "entities": []},
                                          "h", OWNER) is None, "a sent post can't be edited"
        assert await store.delete_schedule(sent["schedule_id"]) is True, "history can be cleaned up"
        missed = await schedule_doc(channel.id, status=store.STATUS_MISSED, error={"code": "MISSED"})
        back = await store.reschedule(missed["schedule_id"], NOW + 900, OWNER)
        assert back["status"] == "scheduled" and back["scheduled_at"] == NOW + 900 and "error" not in back
        assert back["attempts"] == 0


# ---------------------------------------------------------------------------
# Delivery worker
# ---------------------------------------------------------------------------

class TestWorker:
    async def test_due_post_is_sent_once_and_recorded(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id, when=NOW + 600)
        assert await sch.process_due() == 0, "not due yet"
        clock.now = NOW + 600
        assert await sch.process_due() == 1
        assert await sch.process_due() == 0
        assert len(telegram.to(channel.id)) == 1
        sent = await store.get_schedule(doc["schedule_id"])
        assert sent["status"] == "sent" and sent["sent_message_id"] == 7000
        assert "claim_token" not in sent and sent["late_by"] == 0
        notice = telegram.to(OWNER)[-1].message
        assert "Sᴄʜᴇᴅᴜʟᴇᴅ Pᴏsᴛ Sᴇɴᴛ" in notice and f"https://t.me/c/{str(channel.id)[4:]}/7000" in notice

    async def test_concurrent_workers_send_once(self, channel, telegram, clock):
        await schedule_doc(channel.id, when=NOW + 60)
        clock.now = NOW + 60
        telegram.delay = 0.05
        results = await asyncio.gather(*(sch.process_due() for _ in range(5)))
        assert sum(results) == 1 and len(telegram.to(channel.id)) == 1

    async def test_restart_keeps_schedules_and_never_resends(self, channel, telegram, clock):
        due = await schedule_doc(channel.id, when=NOW + 120)
        interrupted = await schedule_doc(channel.id, text="mid-send", status=store.STATUS_SENDING,
                                         claimed_at=NOW - 10, claim_token="old-process")
        # "restart": all in-memory state is gone — only MongoDB remains
        sch.SESSIONS.clear()
        sch._VERIFIED.clear()
        clock.now = NOW + 400
        assert await sch.recover_interrupted() == 1
        assert await sch.process_due() == 1
        assert len(telegram.to(channel.id)) == 1, "only the due post, the interrupted one is not resent"
        assert (await store.get_schedule(due["schedule_id"]))["status"] == "sent"
        failed = await store.get_schedule(interrupted["schedule_id"])
        assert failed["status"] == "failed" and failed["error"]["uncertain"] is True
        assert failed["error"]["code"] == "INTERRUPTED"
        assert await sch.process_due() == 0 and await sch.recover_interrupted() == 0

    async def test_floodwait_defers_then_sends(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id, when=NOW + 60)
        telegram.errors = [FloodWait(value=40)]
        clock.now = NOW + 60
        await sch.process_due()
        deferred = await store.get_schedule(doc["schedule_id"])
        assert deferred["status"] == "scheduled" and deferred["retry_at"] == NOW + 100
        clock.now = NOW + 90
        assert await sch.process_due() == 0
        clock.now = NOW + 100
        assert await sch.process_due() == 1
        assert (await store.get_schedule(doc["schedule_id"]))["status"] == "sent"
        assert len(telegram.to(channel.id)) == 2          # refused once, delivered once

    async def test_refused_delivery_is_recorded_and_reported(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id, when=NOW + 60)
        telegram.errors = [ChatWriteForbidden()]
        clock.now = NOW + 60
        await sch.process_due()
        failed = await store.get_schedule(doc["schedule_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "CHAT_WRITE_FORBIDDEN" and failed["error"]["uncertain"] is False
        assert "Sᴄʜᴇᴅᴜʟᴇᴅ Pᴏsᴛ Fᴀɪʟᴇᴅ" in telegram.to(OWNER)[-1].message
        assert await sch.process_due() == 0, "failed posts are never retried on their own"
        text, _ = sch.render_item(failed)
        assert "CHAT_WRITE_FORBIDDEN" in text

    async def test_refused_custom_emoji_post_gets_a_hint(self, channel, telegram, clock):
        from pyrogram.errors import ChatWriteForbidden, EntityBoundsInvalid

        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        doc = await schedule_doc(channel.id, text=RICH_TEXT, entities=content["entities"], when=NOW + 60)
        plain = await schedule_doc(channel.id, text="no emoji", when=NOW + 61)
        other = await schedule_doc(channel.id, text=RICH_TEXT, entities=content["entities"], when=NOW + 62)
        telegram.errors = [EntityBoundsInvalid(), EntityBoundsInvalid(), ChatWriteForbidden()]
        clock.now = NOW + 62
        assert await sch.process_due() == 3
        hinted = (await store.get_schedule(doc["schedule_id"]))["error"]
        assert hinted["code"] == "ENTITY_BOUNDS_INVALID" and "Fragment username" in hinted["message"]
        assert "Fragment" not in (await store.get_schedule(plain["schedule_id"]))["error"]["message"]
        assert "Fragment" not in (await store.get_schedule(other["schedule_id"]))["error"]["message"], \
            "a 403 (bot removed / no rights) has nothing to do with emoji"

    async def test_unknown_outcomes_are_failed_not_resent(self, channel, telegram, clock, monkeypatch):
        a = await schedule_doc(channel.id, text="server error", when=NOW + 60)
        telegram.errors = [InternalServerError()]
        clock.now = NOW + 60
        await sch.process_due()
        assert (await store.get_schedule(a["schedule_id"]))["error"]["uncertain"] is True
        b = await schedule_doc(channel.id, text="slow", when=NOW + 120)
        monkeypatch.setattr(sch, "SEND_TIMEOUT", 0.01)
        telegram.delay = 0.2
        clock.now = NOW + 120
        await sch.process_due()
        timed_out = await store.get_schedule(b["schedule_id"])
        assert timed_out["status"] == "failed" and timed_out["error"]["code"] == "TIMEOUT"
        assert timed_out["error"]["uncertain"] is True

    async def test_missed_policy_skip(self, channel, telegram, clock, monkeypatch):
        monkeypatch.setattr(sch.config, "SCHEDULE_MISSED_POLICY", "skip", raising=False)
        doc = await schedule_doc(channel.id, when=NOW + 60)
        clock.now = NOW + 60 + 3600                         # the bot was offline for an hour
        await sch.process_due()
        missed = await store.get_schedule(doc["schedule_id"])
        assert missed["status"] == "missed" and telegram.to(channel.id) == []
        assert "Mɪssᴇᴅ" in telegram.to(OWNER)[-1].message

    async def test_missed_policy_send_once_late(self, channel, telegram, clock, monkeypatch):
        monkeypatch.setattr(sch.config, "SCHEDULE_MISSED_POLICY", "send", raising=False)
        doc = await schedule_doc(channel.id, when=NOW + 60)
        clock.now = NOW + 60 + 3600
        await sch.process_due()
        await sch.process_due()
        sent = await store.get_schedule(doc["schedule_id"])
        assert sent["status"] == "sent" and sent["late_by"] == 3600
        assert len(telegram.to(channel.id)) == 1
        assert "60 min late" in telegram.to(OWNER)[-1].message

    async def test_small_delays_are_not_missed(self, channel, telegram, clock, monkeypatch):
        monkeypatch.setattr(sch.config, "SCHEDULE_MISSED_POLICY", "skip", raising=False)
        doc = await schedule_doc(channel.id, when=NOW + 60)
        clock.now = NOW + 60 + 120                          # 2 minutes late: within the grace
        await sch.process_due()
        assert (await store.get_schedule(doc["schedule_id"]))["status"] == "sent"

    async def test_custom_emoji_downgrade_is_reported(self, channel, telegram, clock):
        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        doc = await schedule_doc(channel.id, text=RICH_TEXT, entities=content["entities"], when=NOW + 60)
        telegram.strip_custom_emoji = True
        clock.now = NOW + 60
        await sch.process_due()
        sent = await store.get_schedule(doc["schedule_id"])
        assert sent["status"] == "sent"
        assert "replaced 1 of 1 custom emoji" in sent["delivery_warnings"][0]

    async def test_too_many_retries(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id, when=NOW + 60, attempts=sch.MAX_ATTEMPTS)
        clock.now = NOW + 60
        await sch.process_due()
        failed = await store.get_schedule(doc["schedule_id"])
        assert failed["status"] == "failed" and failed["error"]["code"] == "TOO_MANY_RETRIES"
        assert telegram.to(channel.id) == []

    async def test_one_bad_post_does_not_stop_the_others(self, channel, telegram, clock, monkeypatch):
        await schedule_doc(channel.id, text="first", when=NOW + 60)
        await schedule_doc(channel.id, text="second", when=NOW + 61)
        real = sch.deliver
        calls = []

        async def flaky(doc, token, now):
            calls.append(doc["schedule_id"])
            if len(calls) == 1:
                raise RuntimeError("bug")
            return await real(doc, token, now)
        monkeypatch.setattr(sch, "deliver", flaky)
        clock.now = NOW + 61
        assert await sch.process_due() == 2
        assert len(telegram.to(channel.id)) == 1


# ---------------------------------------------------------------------------
# Handler registration and conflicts with the existing features
# ---------------------------------------------------------------------------

class TestHandlers:
    @pytest.fixture
    def me(self, monkeypatch):
        monkeypatch.setattr(app, "me", types.User(id=app.id, first_name="Bot", username="stub_bot",
                                                  is_bot=True, client=app))

    async def test_groups(self):
        groups = await registered_groups()
        names = {g: [h.callback.__name__ for h in hs] for g, hs in groups.items()}
        assert names[-12] == ["schedule_command"]
        assert names[-11] == ["schedule_input"]

    async def test_callback_data_is_safe_and_short(self, channel):
        doc = await schedule_doc(channel.id)
        session = sch._new_session(OWNER, channel_id=channel.id, channel_title="T", step="confirm",
                                   mode="new", draft={"kind": "text", "text": "t", "entities": []},
                                   day="2026-11-05", hour=10, scheduled_at=NOW + 7200, target=doc["schedule_id"])
        markups = [sch.render_item(doc)[1], sch.render_compose(session)[1], sch.render_calendar(session)[1],
                   sch.render_hours(session)[1], sch.render_minutes(session)[1], sch.render_confirm(session)[1],
                   sch.render_preview_controls(session)[1], sch.render_scheduled(doc)[1],
                   sch.render_ask(doc, "delete")[1], sch.render_ask(doc, "cancel")[1],
                   (await sch.render_list(channel.id, "T"))[1]]
        markups += [rendered[1] for rendered in await _new_screens(channel.id)]
        for markup in markups:
            for data in callbacks(markup):
                assert data.startswith("sch:") and len(data.encode()) <= 64, data
                for unsafe in ("close", "help_callback", "settings_back_helper"):
                    assert unsafe not in data, data

    async def test_close_button_reaches_only_the_scheduler(self):
        import SWAGGYMUSIC.plugins.bot.help  # noqa: F401  (its "close" regex is unanchored)
        chat = types.Chat(id=OWNER, type=ChatType.PRIVATE, client=app)
        q = types.CallbackQuery(client=app, id="q", chat_instance="ci", data="sch:x",
                                from_user=types.User(id=OWNER, first_name="O", client=app),
                                message=types.Message(id=3, chat=chat, client=app))
        assert await dispatch_callback(q) == [sch.schedule_callbacks]

    async def test_no_other_feature_claims_a_scheduler_button(self, channel):
        """Every button the scheduler renders reaches its handler and no other
        callback handler of the bot (Help, Create Post, /setgap, /setlink, ...)."""
        import SWAGGYMUSIC.plugins.bot.help  # noqa: F401
        import SWAGGYMUSIC.plugins.misc.createpost  # noqa: F401
        import SWAGGYMUSIC.plugins.misc.setforward  # noqa: F401
        from pyrogram.handlers import CallbackQueryHandler

        doc = await schedule_doc(channel.id)
        session = sch._new_session(OWNER, channel_id=channel.id, channel_title="T", step="confirm",
                                   mode="new", draft={"kind": "text", "text": "t", "entities": [{"type": "BOLD", "offset": 0, "length": 1}]},
                                   day="2026-11-05", hour=10, scheduled_at=NOW + 7200, target=doc["schedule_id"])
        rendered = [sch.render_item(doc), sch.render_compose(session), sch.render_calendar(session),
                    sch.render_hours(session), sch.render_minutes(session), sch.render_confirm(session),
                    sch.render_preview_controls(session), sch.render_scheduled(doc),
                    sch.render_ask(doc, "delete"), sch.render_ask(doc, "cancel"),
                    await sch.render_list(channel.id, "T"), (None, sch._retry_markup())]
        rendered += await _new_screens(channel.id)
        all_data = {data for _text, markup in rendered for data in callbacks(markup)}
        assert len(all_data) > 50
        groups = await registered_groups()
        chat = types.Chat(id=OWNER, type=ChatType.PRIVATE, client=app)
        for data in sorted(all_data):
            q = types.CallbackQuery(client=app, id="q", chat_instance="ci", data=data,
                                    from_user=types.User(id=OWNER, first_name="O", client=app),
                                    message=types.Message(id=3, chat=chat, client=app))
            # Every handler whose filter accepts the data — not only the first
            # per group — so the result does not depend on plugin import order.
            matched = [handler.callback for handlers in groups.values() for handler in handlers
                       if isinstance(handler, CallbackQueryHandler) and await handler.check(app, q)]
            assert matched == [sch.schedule_callbacks], data

    async def test_input_is_consumed_and_hidden_from_other_wizards(self, me, channel):
        sch._new_session(OWNER)
        chat = types.Chat(id=OWNER, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=next(_MSG_IDS), chat=chat, text=str(channel.id), client=app,
                            from_user=types.User(id=OWNER, first_name="O", client=app))
        msg.reply_text = AsyncMock()
        ran = await dispatch(msg)
        assert ran == ["schedule_input"]
        assert sch.SESSIONS[OWNER]["channel_id"] == channel.id

    async def test_another_command_leaves_the_scheduler(self, me):
        sch._new_session(OWNER)
        chat = types.Chat(id=OWNER, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=next(_MSG_IDS), chat=chat, text="/setlink", client=app,
                            from_user=types.User(id=OWNER, first_name="O", client=app))
        msg.reply_text = AsyncMock()
        try:
            ran = await dispatch(msg)
            assert ran[0] == "schedule_input" and "setlink_private_start" in ran
            assert OWNER not in sch.SESSIONS
        finally:
            setlink_mod.SESSIONS.pop(OWNER, None)

    async def test_cancel_command_closes_the_scheduler(self, me):
        sch._new_session(OWNER)
        chat = types.Chat(id=OWNER, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=next(_MSG_IDS), chat=chat, text="/cancel", client=app,
                            from_user=types.User(id=OWNER, first_name="O", client=app))
        msg.reply_text = AsyncMock()
        ran = await dispatch(msg)
        assert ran == ["schedule_input"], "propagation stops: no second 'cancelled' reply"
        assert OWNER not in sch.SESSIONS
        assert "Sᴄʜᴇᴅᴜʟᴇ Mᴀɴᴀɢᴇʀ Cʟᴏsᴇᴅ" in msg.reply_text.await_args.args[0]

    async def test_users_without_a_session_are_untouched(self, me):
        chat = types.Chat(id=STRANGER, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=next(_MSG_IDS), chat=chat, text="hello", client=app,
                            from_user=types.User(id=STRANGER, first_name="S", client=app))
        assert "schedule_input" not in await dispatch(msg)

    def test_button_lettering_matches_the_create_post_panel(self):
        import SWAGGYMUSIC.plugins.misc.createpost as cp

        existing = set(open(cp.__file__, encoding="utf8").read())
        for label in ("Add New", "Refresh", "Close", "Cancel Schedule", "Confirm Schedule",
                      "Edit Message", "Change Time", "Reschedule", "Delete", "History"):
            converted = sch._sc(label)
            assert converted[0] == label[0]
            assert set(converted) <= existing | set(label), label
        assert sch._sc("Cancel") == "Cᴀɴᴄᴇʟ"                 # identical to Create Post's button


class TestWorkerLoop:
    """The background loop the bot starts at boot (not process_due by hand)."""

    async def test_boot_recovers_then_delivers_due_posts(self, channel, telegram, clock, monkeypatch):
        monkeypatch.setattr(sch, "MAX_IDLE_WAIT", 0.01)
        stuck = await schedule_doc(channel.id, text="stuck", status=store.STATUS_SENDING,
                                   claimed_at=NOW - 3600, claim_token="dead")
        due = await schedule_doc(channel.id, text="due while offline", when=NOW - 120)
        later = await schedule_doc(channel.id, text="later", when=NOW + 30)
        task = asyncio.create_task(sch._worker_loop())
        try:
            for _ in range(200):
                if (await store.get_schedule(due["schedule_id"]))["status"] == "sent":
                    break
                await asyncio.sleep(0.01)
            assert (await store.get_schedule(due["schedule_id"]))["status"] == "sent"
            assert (await store.get_schedule(stuck["schedule_id"]))["status"] == "failed"
            assert (await store.get_schedule(later["schedule_id"]))["status"] == "scheduled"
            clock.now = NOW + 30
            for _ in range(200):
                if (await store.get_schedule(later["schedule_id"]))["status"] == "sent":
                    break
                await asyncio.sleep(0.01)
            assert (await store.get_schedule(later["schedule_id"]))["status"] == "sent"
            await asyncio.sleep(0.05)
            assert [q.message for q in telegram.to(channel.id)] == ["due while offline", "later"]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_the_loop_survives_errors(self, monkeypatch):
        monkeypatch.setattr(sch, "ERROR_BACKOFF", 0.01)
        calls = []

        async def broken(now=None):
            calls.append(now)
            raise RuntimeError("mongo hiccup")
        monkeypatch.setattr(sch, "process_due", broken)
        task = asyncio.create_task(sch._worker_loop())
        await asyncio.sleep(0.1)
        assert not task.done() and len(calls) >= 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ===========================================================================
# One-Time and Daily schedules, "Radhe Radhe" protection, Heroku-safe worker
# ===========================================================================

# The requirement's table: input -> protection.
REQUIRED_CASES = [
    ("Radhe Radhe", True),
    ("RADHE RADHE", True),
    ("radhe radhe", True),
    ("rAdHe rAdHe", True),
    ("Hello Radhe Radhe ❤️", True),
    ("💯😍💯 Kya raam Radhe Radhe ek barabar hain?", True),
    ("Radhe Radhe 🙏 Good Morning", True),
    ("Hello\nRadhe Radhe", True),
    ("राधे राधे", True),
    ("राधे-राधे", True),
    ("رادھے رادھے", True),
    ("رادھے، رادھے", True),
    ("Hello Dear ❤️", False),
    ("Good Morning everyone", False),
]
MORE_CASES = [
    # other spellings / positions the requirement lists or implies
    ("Hello Dear Radhe Radhe ❤️", True), ("RaDHe RaDHe", True), ("राधे, राधे", True),
    ("राधे। राधे", True), ("رادھے-رادھے", True), ("Radhe-Radhe", True), ("Radhe, Radhe!", True),
    ("🙏Radhe Radhe🙏", True), ("(Radhe   Radhe)", True), ("Jai Shri Krishna.\n\nRadhe Radhe\n\n— Admin", True),
    ("सबको राधे राधे।", True), ("سب کو رادھے رادھے!", True),
    ("رادهے رادهے", True),                  # ه typed for ھ
    ("رادھے\u200F رادھے", True),             # an invisible right-to-left mark
    (BOLD_PHRASE, True),                     # styled Unicode letters still count
    ("Ｒａｄｈｅ Ｒａｄｈｅ", True),          # full-width letters
    # not the phrase: partial words, a single word, other text
    ("Radhe", False), ("Radhe Krishna", False), ("Radheshyam Radhe", False), ("Radhe Radheshyam", False),
    ("xRadhe Radhe", False), ("Radhe 🙏 Radhe", False), ("RadheRadhe", False),
    ("राधेश्याम राधे", False), ("राधे राधेश्याम", False), ("Radhe राधे", False), ("", False), (None, False),
]


class TestProtection:
    """The bot never adds anything; protection is ON only when the admin's own
    text contains "Radhe Radhe" (English, Hindi or Urdu)."""

    @pytest.mark.parametrize("text, expected", REQUIRED_CASES)
    def test_required_cases(self, text, expected):
        assert has_radhe_radhe(text) is expected

    @pytest.mark.parametrize("text, expected", MORE_CASES)
    def test_more_spellings_and_no_false_positives(self, text, expected):
        assert has_radhe_radhe(text) is expected

    def test_detection_only_reads_the_text(self):
        content = {"kind": "text", "text": "  💯 Radhe Radhe\n\nline 2  ", "entities": []}
        before = {**content}
        assert content_mod.is_protected(content) and content == before

    def test_one_shared_matcher(self, monkeypatch):
        seen = []
        monkeypatch.setattr(content_mod, "has_radhe_radhe", lambda text: seen.append(text) or True)
        assert content_mod.is_protected({"text": "anything"}) and seen == ["anything"]

    def test_plain_text_mode_keeps_the_text_and_the_protection(self):
        formatted = {"kind": "text", "text": "Radhe Radhe", "entities": [{"type": "BOLD", "offset": 0, "length": 5}]}
        plain = content_mod.make_plain(formatted)
        assert plain["text"] == "Radhe Radhe" and plain["entities"] == []
        assert content_mod.is_protected(plain) and content_mod.is_protected(formatted)

    def test_nothing_in_the_code_adds_text(self):
        source = Path(content_mod.__file__).read_text()
        assert "with_marker" not in source and "SCHEDULE_MARKER" not in source
        assert "𝐑" not in Path(sch.__file__).read_text()

    async def test_text_post_is_sent_exactly_as_written(self, telegram, channel):
        content = content_mod.extract_content(await admin_message(RICH_TEXT, entities=rich_entities()))
        assert not content_mod.is_protected(content)
        await content_mod.send_content(app, channel.id, content)
        request = telegram.to(channel.id)[-1]
        assert request.message == RICH_TEXT and "adhe" not in request.message.lower()
        assert wire(request.entities) == wire(rich_entities())

    async def test_phrase_in_the_middle_formatting_and_custom_emoji_untouched(self, telegram, channel):
        text = "💯😍💯 Kya raam Radhe Radhe ek barabar hain?\n\n  second   line\twith tab"
        start = len(text[:text.index("Radhe")].encode("utf-16-le")) // 2
        entities = [raw.types.MessageEntityCustomEmoji(offset=0, length=2, document_id=CUSTOM_EMOJI_ID),
                    raw.types.MessageEntityBold(offset=start, length=11),
                    raw.types.MessageEntitySpoiler(offset=start + 6, length=5),
                    raw.types.MessageEntityTextUrl(offset=start + 12, length=2, url="https://example.com")]
        content = content_mod.extract_content(await admin_message(text, entities=entities))
        assert content_mod.is_protected(content)
        await content_mod.send_content(app, channel.id, content)
        request = telegram.to(channel.id)[-1]
        assert request.message == text and request.message.count("Radhe") == 2
        assert wire(request.entities) == wire(entities)
        assert u16_slice(request.message, start, 11) == "Radhe Radhe"
        emoji = next(e for e in request.entities if isinstance(e, raw.types.MessageEntityCustomEmoji))
        assert emoji.document_id == CUSTOM_EMOJI_ID and u16_slice(request.message, 0, 2) == "💯"

    async def test_captions_are_unchanged(self, telegram, channel):
        entities = [raw.types.MessageEntitySpoiler(offset=0, length=6),
                    raw.types.MessageEntityCustomEmoji(offset=7, length=2, document_id=CUSTOM_EMOJI_ID)]
        msg = await admin_message("Secret 🎁", media=raw_media("photo", 61, spoiler=True),
                                  entities=entities, telegram=telegram, invert=True)
        await content_mod.send_content(app, channel.id, content_mod.extract_content(msg))
        request = telegram.to(channel.id)[-1]
        assert isinstance(request, raw.functions.messages.SendMedia)
        assert request.message == "Secret 🎁" and wire(request.entities) == wire(entities)
        assert request.media.spoiler is True and request.invert_media is True
        bare = await admin_message("", media=raw_media("document", 62), telegram=telegram)
        await content_mod.send_content(app, channel.id, content_mod.extract_content(bare))
        assert telegram.to(channel.id)[-1].message == "", "no caption is added to a media post"

    async def test_preview_shows_protection_and_the_real_post(self, channel, telegram):
        text = "Hello Dear Radhe Radhe ❤️"
        await compose_and_preview(channel.id, telegram, text=text, entities=[])
        preview, controls = telegram.to(OWNER)[0], telegram.to(OWNER)[1]
        assert preview.message == text
        assert "🛡 Protection: ON — the post contains “Radhe Radhe”" in controls.message   # HTML parsed

    async def test_protection_off_adds_nothing(self, channel, telegram, clock):
        text = "Hello Dear ❤️"
        await compose_and_preview(channel.id, telegram, text=text, entities=[])
        assert telegram.to(OWNER)[0].message == text
        assert "🛡 Protection: OFF — the post has no “Radhe Radhe”" in telegram.to(OWNER)[1].message
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 9, 0)
        await press(OWNER, "ok")
        clock.now = ist(5, 9, 0)
        await sch.process_due()
        assert telegram.to(channel.id)[0].message == text

    async def test_preview_and_delivery_are_identical(self, channel, telegram, clock):
        text = "Good morning ☀️\nराधे राधे 🙏"
        entities = [raw.types.MessageEntityItalic(offset=0, length=12),
                    raw.types.MessageEntityCustomEmoji(offset=13, length=1, document_id=CUSTOM_EMOJI_ID)]
        await compose_and_preview(channel.id, telegram, text=text, entities=entities, kind="daily")
        preview = telegram.to(OWNER)[0]
        await press(OWNER, "nx")
        await press(OWNER, "day:20261104")
        await press(OWNER, "hr:12")
        text_confirm, _ = edited(await press(OWNER, "mn:30"))
        assert "🛡 <b>Protection:</b> ON" in text_confirm
        await press(OWNER, "ok")
        clock.now = ist(4, 12, 30)
        await sch.process_due()
        delivered = telegram.to(channel.id)[0]
        assert (delivered.message, wire(delivered.entities)) == (preview.message, wire(preview.entities))
        assert delivered.message == text and wire(delivered.entities) == wire(entities)

    async def test_media_preview_and_delivery_are_identical(self, channel, telegram, clock):
        media = raw_media("video", 63, spoiler=True)
        await compose_and_preview(channel.id, telegram, text="رادھے رادھے 🌸", media=media,
                                  entities=[raw.types.MessageEntityUnderline(offset=0, length=11)])
        preview = telegram.to(OWNER)[0]
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 7, 0)
        await press(OWNER, "ok")
        clock.now = ist(5, 7, 0)
        await sch.process_due()
        delivered = telegram.to(channel.id)[0]
        assert delivered.message == preview.message == "رادھے رادھے 🌸"
        assert wire(delivered.entities) == wire(preview.entities)
        assert delivered.media.spoiler is preview.media.spoiler is True

    async def test_plain_text_mode_in_the_composer(self, channel, telegram):
        await start_compose(channel.id)
        await press(OWNER, "pm")
        await sch.handle_input(await admin_message("Radhe Radhe 🙏 Good Morning",
                                                   entities=[raw.types.MessageEntityBold(offset=0, length=11)],
                                                   telegram=telegram))
        preview, controls = telegram.to(OWNER)[0], telegram.to(OWNER)[1].message
        assert preview.message == "Radhe Radhe 🙏 Good Morning" and not preview.entities
        assert "Plain text mode is active" in controls and "Protection: ON" in controls

    async def test_item_view_shows_protection(self, channel):
        on = await schedule_doc(channel.id, text="Radhe Radhe 🙏 Good Morning")
        off = await daily_doc(channel.id, 8, 0, text="Good Morning everyone")
        await open_channel(OWNER, channel.id)
        assert "🛡 <b>Protection:</b> ON" in edited(await press(OWNER, f"it:{on['schedule_id']}:p:0"))[0]
        assert "🛡 <b>Protection:</b> OFF" in edited(await press(OWNER, f"it:{off['schedule_id']}:p:0"))[0]

    async def test_edit_message_recomputes_protection_and_adds_nothing(self, channel, telegram, clock):
        doc = await schedule_doc(channel.id, text="Hello Dear ❤️", when=NOW + 600)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"ed:{doc['schedule_id']}")
        new_text = "Updated: राधे राधे 🙏"
        await sch.handle_input(await admin_message(new_text, telegram=telegram))
        assert "Protection: ON" in telegram.to(OWNER)[-1].message
        text, _ = edited(await press(OWNER, "nx"))
        assert "Changes saved" in text and "Protection:</b> ON" in text
        assert (await store.get_schedule(doc["schedule_id"]))["content"]["text"] == new_text
        clock.now = NOW + 600
        await sch.process_due()
        assert telegram.to(channel.id)[0].message == new_text

    async def test_reschedule_keeps_the_post_untouched(self, channel, telegram, clock):
        text = "رادھے، رادھے — Good Morning"
        doc = await schedule_doc(channel.id, text=text, status=store.STATUS_FAILED,
                                 error={"code": "X", "message": "m"})
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rs:{doc['schedule_id']}")
        await pick(OWNER, date(2026, 11, 6), 10, 0)
        await press(OWNER, "ok")
        assert (await store.get_schedule(doc["schedule_id"]))["content"] == doc["content"]
        clock.now = ist(6, 10, 0)
        await sch.process_due()
        assert telegram.to(channel.id)[0].message == text

    async def test_full_length_posts_are_accepted_and_sent_unchanged(self, channel, telegram, clock):
        text = "z" * 4096
        assert content_mod.extract_content(await admin_message(text))["text"] == text
        await schedule_doc(channel.id, text=text, when=NOW + 60)
        clock.now = NOW + 60
        await sch.process_due()
        assert telegram.to(channel.id)[0].message == text

    async def test_activity_log_records_protection(self, channel, telegram, clock):
        await compose_and_preview(channel.id, telegram, text="Radhe Radhe", entities=[])
        await press(OWNER, "nx")
        await pick(OWNER, date(2026, 11, 5), 9, 0)
        await press(OWNER, "ok")
        clock.now = ist(5, 9, 0)
        await sch.process_due()
        logged = [(c.args[3], c.kwargs.get("protected")) for c in sch.log_schedule_change.await_args_list]
        assert logged == [("CREATED", True), ("SENT", True)]


class TestDailyCreation:
    async def test_home_menu_to_daily_schedule(self, channel, telegram):
        _text, markup = await open_home()
        assert "sch:ty:daily" in callbacks(markup)
        q = await press(OWNER, "ty:daily")
        text, markup = edited(q)
        assert "Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ" in text and "Send the Channel ID where the post should be published." in text
        assert callbacks(markup) == ["sch:hm", "sch:abort"]
        reply = text_msg(OWNER, str(channel.id))
        await sch.handle_input(reply)
        assert "Cᴏᴍᴘᴏsᴇ Pᴏsᴛ</b> — 🔁 Daily" in reply.reply_text.await_args.args[0]
        session = sch.SESSIONS[OWNER]
        assert (session["step"], session["kind"], session["mode"]) == ("compose", "daily", "new")
        await sch.handle_input(await admin_message(RICH_TEXT, entities=rich_entities(), telegram=telegram))
        text, markup = edited(await press(OWNER, "nx"))
        assert "Sᴛᴀʀᴛ Dᴀᴛᴇ" in text and "sch:day:20261104" in callbacks(markup)
        text, markup = edited(await press(OWNER, "day:20261104"))
        assert "Dᴀɪʟʏ Tɪᴍᴇ" in text and "every day" in text and "Start Date:</b> 04-11-2026" in text
        assert all(f"sch:hr:{h:02d}" in callbacks(markup) for h in range(24)), "no 'past' hours for a daily time"
        await press(OWNER, "hr:22")
        text, markup = edited(await press(OWNER, "mn:30"))
        assert "Cᴏɴғɪʀᴍ Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ" in text and "🗂 <b>Schedule Type:</b> 🔁 Daily" in text
        assert "📅 <b>Start Date:</b> 04-11-2026" in text and "⏰ <b>Time:</b> 10:30 PM IST · every day" in text
        assert "⏭ <b>Next Execution:</b> 04-11-2026 10:30 PM IST" in text and "ℹ️" not in text
        assert labels(markup) == ["✅ Cᴏɴғɪʀᴍ Sᴄʜᴇᴅᴜʟᴇ", "📅 Cʜᴀɴɢᴇ Dᴀᴛᴇ", "🕒 Cʜᴀɴɢᴇ Tɪᴍᴇ", "✏️ Eᴅɪᴛ Mᴇssᴀɢᴇ", "❌ Cᴀɴᴄᴇʟ"]
        text, _ = edited(await press(OWNER, "ok"))
        assert "Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ Cʀᴇᴀᴛᴇᴅ" in text and "Every day at 10:30 PM IST" in text
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["schedule_type"] == "daily" and doc["daily_time"] == {"hour": 22, "minute": 30}
        assert doc["start_date"] == "2026-11-04"
        assert doc["scheduled_at"] == ist(4, 22, 30) and doc["timezone"] == "Asia/Kolkata"
        assert doc["status"] == "scheduled" and doc["run_count"] == 0 and doc["creator_id"] == OWNER
        assert await stored_wire(doc["content"]) == wire(rich_entities())
        actions = [(c.args[3], c.kwargs.get("kind")) for c in sch.log_schedule_change.await_args_list]
        assert actions == [("CREATED", "daily")]

    async def test_one_time_from_the_home_menu(self, channel, telegram):
        await open_home()
        await press(OWNER, "ty:once")
        reply = text_msg(OWNER, str(channel.id))
        await sch.handle_input(reply)
        assert "Cᴏᴍᴘᴏsᴇ Pᴏsᴛ</b> — 📌 One-Time" in reply.reply_text.await_args.args[0]
        await sch.handle_input(await admin_message("Launch!", telegram=telegram))
        assert "Sᴇʟᴇᴄᴛ Dᴀᴛᴇ" in edited(await press(OWNER, "nx"))[0]
        await pick(OWNER, date(2026, 11, 5), 9, 0)
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["schedule_type"] == "once" and doc["scheduled_at"] == ist(5, 9, 0)

    async def test_channel_id_sent_at_home_opens_its_schedules(self, channel):
        await open_home()
        reply = text_msg(OWNER, str(channel.id))
        await sch.handle_input(reply)
        assert "No scheduled posts found for this channel." in reply.reply_text.await_args.args[0]

    async def test_manage_button(self, channel):
        await open_home()
        text, markup = edited(await press(OWNER, "mg"))
        assert "Send the Channel ID whose scheduled posts you want to manage." in text
        assert callbacks(markup) == ["sch:hm", "sch:abort"]
        reply = text_msg(OWNER, str(channel.id))
        await sch.handle_input(reply)
        assert "No scheduled posts found for this channel." in reply.reply_text.await_args.args[0]

    async def test_invalid_channel_retry_keeps_the_schedule_type(self, channel):
        await open_home()
        await press(OWNER, "ty:daily")
        await sch.handle_input(text_msg(OWNER, "nope"))
        text, _ = edited(await press(OWNER, "retry"))
        assert "Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ" in text and sch.SESSIONS[OWNER]["kind"] == "daily"

    async def test_typed_time_and_the_one_minute_lead(self, channel, telegram, clock):
        await compose_and_preview(channel.id, telegram, kind="daily")
        await press(OWNER, "nx")
        await press(OWNER, "day:20261104")                         # start today (12:00 PM now)
        msg = text_msg(OWNER, "7:05 am")
        await sch.handle_input(msg)
        text = msg.reply_text.await_args.args[0]
        assert "Next Execution:</b> 05-11-2026 07:05 AM IST" in text and "Start Date:</b> 04-11-2026" in text
        assert ("Today's 07:05 AM IST has already passed, so the first post will go out "
                "<b>tomorrow, 05-11-2026 07:05 AM IST</b>. The start date stays 04-11-2026.") in text
        for now, typed, first, note in (
                (NOW, "12:00 PM", "05-11-2026 12:00 PM IST", "has already passed"),          # it is 12:00 now
                (NOW, "12:01 PM", "04-11-2026 12:01 PM IST", None),                          # a minute away
                (NOW + 30, "12:01 PM", "05-11-2026 12:01 PM IST", "is less than a minute away")):
            clock.now = now
            await press(OWNER, "ct")
            assert sch.SESSIONS[OWNER]["step"] == "hour"
            msg = text_msg(OWNER, typed)
            await sch.handle_input(msg)
            text = msg.reply_text.await_args.args[0]
            assert f"Next Execution:</b> {first}" in text and "Start Date:</b> 04-11-2026" in text
            assert (note in text) if note else ("ℹ️" not in text)

    async def test_waiting_on_the_confirmation_moves_the_first_post(self, channel, telegram, clock):
        await compose_and_preview(channel.id, telegram, kind="daily")
        await press(OWNER, "nx")
        await press(OWNER, "day:20261104")
        await press(OWNER, "hr:12")
        await press(OWNER, "mn:30")                       # today 12:30 PM
        clock.now = ist(4, 12, 45)                         # the admin waited on the screen
        q = await press(OWNER, "ok")
        q.answer.assert_any_await("The first post time changed — please check it and confirm again.",
                                  show_alert=True)
        text, _ = edited(q)
        assert "moved to 05-11-2026 12:30 PM IST" in text and "has already passed" in text
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 0, "not silently"
        await press(OWNER, "ok")                           # confirmed again, knowingly
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["scheduled_at"] == ist(5, 12, 30) and doc["start_date"] == "2026-11-04"

    async def test_several_independent_daily_schedules(self, channel, telegram):
        for text, hour in (("Morning", 8), ("Evening", 20), ("Morning", 9)):
            await compose_and_preview(channel.id, telegram, kind="daily", text=text, entities=[])
            await press(OWNER, "nx")
            await press(OWNER, "day:20261104")
            await press(OWNER, f"hr:{hour:02d}")
            await press(OWNER, "mn:00")
            await press(OWNER, "ok")
        docs = await store.scheduled_db.find({"channel_id": channel.id}).to_list(None)
        assert len({d["schedule_id"] for d in docs}) == 3
        assert sorted((d["content"]["text"], d["daily_time"]["hour"]) for d in docs) == \
            [("Evening", 20), ("Morning", 8), ("Morning", 9)]
        await compose_and_preview(channel.id, telegram, kind="daily", text="Morning", entities=[])
        await press(OWNER, "nx")
        await press(OWNER, "day:20261104")
        await press(OWNER, "hr:08")
        await press(OWNER, "mn:00")
        q = await press(OWNER, "ok")
        q.answer.assert_any_await("An identical daily schedule already exists.", show_alert=True)
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 3

    async def test_back_navigation(self, channel, telegram):
        await open_home()
        await press(OWNER, "ty:daily")
        q = await press(OWNER, "hm")                                    # channel prompt -> home
        assert "sch:ty:daily" in callbacks(edited(q)[1]) and sch.SESSIONS[OWNER]["step"] == "home"
        await compose_and_preview(channel.id, telegram, kind="daily")
        await press(OWNER, "nx")
        await press(OWNER, "day:20261105")
        await press(OWNER, "hr:09")
        assert "Dᴀɪʟʏ Tɪᴍᴇ" in edited(await press(OWNER, "bk"))[0]      # minutes -> hours
        assert "Sᴛᴀʀᴛ Dᴀᴛᴇ" in edited(await press(OWNER, "bk"))[0]      # hours -> start date
        assert "Pʀᴇᴠɪᴇᴡ" in edited(await press(OWNER, "bk"))[0]         # start date -> preview
        await press(OWNER, "cm")                                         # ✏️ Edit Message -> composer
        assert "Nᴇᴡ Sᴄʜᴇᴅᴜʟᴇ" in edited(await press(OWNER, "bk"))[0]     # composer -> schedule type
        q = await press(OWNER, "bk")                                     # schedule type -> the list
        assert "No scheduled posts found for this channel." in edited(q)[0]
        q = await press(OWNER, "abort")
        assert await store.scheduled_db.count_documents({"channel_id": channel.id}) == 0


class TestDailyDelivery:
    async def test_published_every_day(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        sid = doc["schedule_id"]
        assert doc["scheduled_at"] == ist(4, 12, 30)
        clock.now = ist(4, 12, 30)
        assert await sch.process_due() == 1
        after = await store.get_schedule(sid)
        assert after["status"] == "scheduled" and after["scheduled_at"] == ist(5, 12, 30)
        assert after["last_run_key"] == "2026-11-04" and after["last_run_at"] == ist(4, 12, 30)
        assert after["run_count"] == 1 and isinstance(after["last_sent_message_id"], int)
        assert await sch.process_due() == 0, "an occurrence is never sent twice"
        clock.now = ist(5, 12, 30)
        assert await sch.process_due() == 1
        after = await store.get_schedule(sid)
        assert after["scheduled_at"] == ist(6, 12, 30) and after["run_count"] == 2
        assert after["last_run_key"] == "2026-11-05" and len(telegram.to(channel.id)) == 2
        assert telegram.to(OWNER) == [], "no private message for every routine daily post"

    async def test_midnight(self, channel, telegram, clock):
        clock.now = ist(4, 23, 50)
        doc = await daily_doc(channel.id, 0, 0)
        assert doc["scheduled_at"] == ist(5, 0, 0)
        clock.now = ist(5, 0, 0)
        await sch.process_due()
        after = await store.get_schedule(doc["schedule_id"])
        assert after["last_run_key"] == "2026-11-05" and after["scheduled_at"] == ist(6, 0, 0)

    def test_ist_daily_times(self):
        assert datetime.fromtimestamp(st.next_daily_run(5, 30, NOW), timezone.utc) == \
            datetime(2026, 11, 5, 0, 0, tzinfo=timezone.utc)                 # 05:30 IST = 00:00 UTC
        assert datetime.fromtimestamp(st.next_daily_run(0, 0, NOW), timezone.utc) == \
            datetime(2026, 11, 4, 18, 30, tzinfo=timezone.utc)               # midnight IST = 18:30 UTC
        assert st.next_daily_run(0, 15, ist(31, 23, 30, month=12)) == ist(1, 0, 15, month=1, year=2027)
        assert st.next_daily_run(23, 59, ist(30, 23, 59)) == ist(1, 23, 59, month=12)
        assert st.next_daily_run(8, 0, NOW, skip_key="2026-11-05") == ist(6, 8, 0)
        old = os.environ.get("TZ")
        try:
            for zone in ("America/New_York", "Asia/Tokyo", "UTC"):
                os.environ["TZ"] = zone
                time.tzset()
                assert st.next_daily_run(5, 30, NOW) == ist(5, 5, 30)
                assert st.run_key(ist(5, 0, 0)) == "2026-11-05"
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    async def test_missed_days_are_skipped_without_backlog(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)                  # due 04-11 12:30 PM
        clock.now = ist(7, 14, 0)                                   # offline for three days
        assert await sch.process_due() == 1
        after = await store.get_schedule(doc["schedule_id"])
        assert telegram.to(channel.id) == [], "no backlog of missed posts"
        assert after["status"] == "scheduled" and after["scheduled_at"] == ist(8, 12, 30)
        assert after["skip_count"] == 1 and after["last_skip"]["occurrence"] == ist(4, 12, 30)
        assert after["run_count"] == 0
        assert "Dᴀɪʟʏ Pᴏsᴛ Sᴋɪᴘᴘᴇᴅ" in telegram.to(OWNER)[-1].message
        assert await sch.process_due() == 0
        clock.now = ist(8, 12, 30)
        assert await sch.process_due() == 1 and len(telegram.to(channel.id)) == 1

    async def test_back_before_todays_time_still_posts_today(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        clock.now = ist(6, 9, 0)
        await sch.process_due()
        assert (await store.get_schedule(doc["schedule_id"]))["scheduled_at"] == ist(6, 12, 30)

    async def test_a_short_delay_is_not_a_missed_post(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        clock.now = ist(4, 12, 32)
        await sch.process_due()
        assert len(telegram.to(channel.id)) == 1
        assert (await store.get_schedule(doc["schedule_id"]))["late_by"] == 120

    async def test_floodwait_retries_the_same_occurrence(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        telegram.errors = [FloodWait(value=900)]                    # longer than the missed grace
        clock.now = ist(4, 12, 30)
        await sch.process_due()
        deferred = await store.get_schedule(doc["schedule_id"])
        assert deferred["status"] == "scheduled" and deferred["scheduled_at"] == ist(4, 12, 30)
        assert deferred["retry_at"] == ist(4, 12, 45)
        clock.now = ist(4, 12, 44)
        assert await sch.process_due() == 0
        clock.now = ist(4, 12, 45)
        assert await sch.process_due() == 1
        done = await store.get_schedule(doc["schedule_id"])
        assert done["last_run_key"] == "2026-11-04" and done["scheduled_at"] == ist(5, 12, 30)
        assert "last_skip" not in done, "a FloodWait retry is not a missed post"
        assert len(telegram.to(channel.id)) == 2                     # refused once, published once

    async def test_failure_is_recorded_and_the_schedule_continues(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        telegram.errors = [ChatWriteForbidden()]
        clock.now = ist(4, 12, 30)
        await sch.process_due()
        failed = await store.get_schedule(doc["schedule_id"])
        assert failed["status"] == "scheduled" and failed["scheduled_at"] == ist(5, 12, 30)
        assert failed["error"]["code"] == "CHAT_WRITE_FORBIDDEN"
        assert failed["error"]["occurrence"] == ist(4, 12, 30) and failed["failure_count"] == 1
        assert "last_run_key" not in failed
        notice = telegram.to(OWNER)[-1].message
        assert "Dᴀɪʟʏ Pᴏsᴛ Fᴀɪʟᴇᴅ" in notice and "next post: 05-11-2026 · 12:30 PM IST" in notice
        text, _ = sch.render_item(failed)
        assert "Last error (04-11-2026 · 12:30 PM IST)" in text and "CHAT_WRITE_FORBIDDEN" in text
        clock.now = ist(5, 12, 30)
        await sch.process_due()
        ok = await store.get_schedule(doc["schedule_id"])
        assert "error" not in ok and ok["run_count"] == 1 and ok["last_run_key"] == "2026-11-05"

    async def test_an_unknown_outcome_is_never_retried(self, channel, telegram, clock, monkeypatch):
        doc = await daily_doc(channel.id, 12, 30)
        monkeypatch.setattr(sch, "SEND_TIMEOUT", 0.01)
        telegram.delay = 0.2
        clock.now = ist(4, 12, 30)
        await sch.process_due()
        after = await store.get_schedule(doc["schedule_id"])
        assert after["error"]["code"] == "TIMEOUT" and after["error"]["uncertain"] is True
        assert after["scheduled_at"] == ist(5, 12, 30)
        telegram.delay = 0
        assert await sch.process_due() == 0

    async def test_paused_schedules_are_never_sent(self, channel, telegram, clock):
        await daily_doc(channel.id, 12, 30, status=store.STATUS_PAUSED)
        for day in (4, 5, 6):
            clock.now = ist(day, 12, 30)
            assert await sch.process_due() == 0
        assert telegram.to(channel.id) == []

    async def test_restart_mid_send_does_not_repost(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30, status=store.STATUS_SENDING,
                              claimed_at=ist(4, 12, 30), claim_token="dead-process", attempts=1)
        sch.SESSIONS.clear()           # the bot restarted: only MongoDB remains
        sch._VERIFIED.clear()
        clock.now = ist(4, 12, 40)
        assert await sch.recover_interrupted() == 1
        after = await store.get_schedule(doc["schedule_id"])
        assert after["status"] == "scheduled" and after["scheduled_at"] == ist(5, 12, 30)
        assert after["error"]["code"] == "INTERRUPTED" and after["error"]["uncertain"] is True
        assert "claim_token" not in after
        assert await sch.process_due() == 0 and telegram.to(channel.id) == []
        assert "Check the channel" in telegram.to(OWNER)[-1].message
        clock.now = ist(5, 12, 30)
        assert await sch.process_due() == 1

    async def test_an_already_published_day_is_never_sent_again(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 18, 0, last_run_key="2026-11-04")
        await store.scheduled_db.update_one({"_id": doc["_id"]}, {"$set": {"scheduled_at": ist(4, 18, 0)}})
        clock.now = ist(4, 18, 0)
        await sch.process_due()
        after = await store.get_schedule(doc["schedule_id"])
        assert telegram.to(channel.id) == [] and after["scheduled_at"] == ist(5, 18, 0)

    async def test_concurrent_workers_publish_once(self, channel, telegram, clock):
        await daily_doc(channel.id, 12, 30)
        clock.now = ist(4, 12, 30)
        telegram.delay = 0.05
        results = await asyncio.gather(*(sch.process_due() for _ in range(5)))
        assert sum(results) == 1 and len(telegram.to(channel.id)) == 1

    async def test_many_schedules_due_at_the_same_time(self, channel, telegram, clock):
        dailies = [await daily_doc(channel.id, 12, 30, text=f"Daily {i}") for i in range(3)]
        onces = [await schedule_doc(channel.id, when=ist(4, 12, 30), text=f"Once {i}") for i in range(2)]
        clock.now = ist(4, 12, 30)
        telegram.delay = 0.01
        assert sum(await asyncio.gather(sch.process_due(), sch.process_due())) == 5
        assert sorted(q.message.split("\n")[0] for q in telegram.to(channel.id)) == \
            ["Daily 0", "Daily 1", "Daily 2", "Once 0", "Once 1"]
        for doc in dailies:
            assert (await store.get_schedule(doc["schedule_id"]))["scheduled_at"] == ist(5, 12, 30)
        for doc in onces:
            assert (await store.get_schedule(doc["schedule_id"]))["status"] == "sent"

    async def test_one_time_floodwait_retry_is_not_missed(self, channel, telegram, clock, monkeypatch):
        monkeypatch.setattr(sch.config, "SCHEDULE_MISSED_POLICY", "skip", raising=False)
        doc = await schedule_doc(channel.id, when=NOW + 60)
        telegram.errors = [FloodWait(value=900)]
        clock.now = NOW + 60
        await sch.process_due()
        clock.now = NOW + 60 + 900
        await sch.process_due()
        assert (await store.get_schedule(doc["schedule_id"]))["status"] == "sent"

    async def test_claim_lost_after_publishing_still_records_it(self, channel, clock):
        doc = await daily_doc(channel.id, 12, 30)
        claimed = await store.claim_next_due(ist(4, 12, 30), "A")
        clock.now = ist(4, 12, 40)
        await store.recover_stale_sending(ist(4, 12, 40), 300)      # another process gave up on it
        await store.mark_daily_sent(claimed, "A", message_id=55, now=ist(4, 12, 41),
                                    next_run=ist(5, 12, 30), run_key="2026-11-04")
        after = await store.get_schedule(doc["schedule_id"])
        assert after["last_run_key"] == "2026-11-04" and after["last_sent_message_id"] == 55
        assert "error" not in after and after["scheduled_at"] == ist(5, 12, 30)


class TestDailyManagement:
    async def test_list_shows_type_and_next_run(self, channel):
        once = await schedule_doc(channel.id, text="Launch", when=ist(5, 22, 30))
        active = await daily_doc(channel.id, 8, 0, text="Good morning")
        paused = await daily_doc(channel.id, 21, 0, text="Good night", status=store.STATUS_PAUSED)
        text, _ = await open_channel(OWNER, channel.id)
        assert (f"Good morning · <code>#{active['schedule_id']}</code>\n"
                "   📅 Next: 05-11-2026\n   ⏰ 08:00 AM IST · every day\n   Type: 🔁 Daily\n"
                "   Status: 🟢 Active") in text
        assert (f"Good night · <code>#{paused['schedule_id']}</code>\n"
                "   📅 Next: — (paused)\n   ⏰ 09:00 PM IST · every day\n   Type: 🔁 Daily\n"
                "   Status: ⏸ Paused") in text
        assert (f"Launch · <code>#{once['schedule_id']}</code>\n"
                "   📅 05-11-2026\n   ⏰ 10:30 PM IST\n   Type: 📌 One-Time\n   Status: 🟢 Scheduled") in text

    async def test_daily_item_view(self, channel):
        doc = await daily_doc(channel.id, 8, 0, run_count=3, last_run_at=ist(4, 8, 0),
                              last_run_key="2026-11-04", last_sent_message_id=77)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        text, markup = edited(await press(OWNER, f"it:{sid}:p:0"))
        assert "🔁 <b>Repeats:</b> Every day at 08:00 AM IST" in text
        assert "⏭ <b>Next post:</b> 05-11-2026 · 08:00 AM IST" in text and "Status:</b> 🟢 Active" in text
        assert "✅ <b>Last post:</b> 04-11-2026 · 08:00 AM IST · message <code>77</code>" in text
        assert "🔢 <b>Posts published:</b> 3" in text
        assert callbacks(markup) == [f"sch:ed:{sid}", f"sch:rs:{sid}", f"sch:ps:{sid}", f"sch:pv:{sid}",
                                     f"sch:da:{sid}", f"sch:ls:{channel.id}:p:0"]
        assert {"🕒 Cʜᴀɴɢᴇ Tɪᴍᴇ", "⏸ Pᴀᴜsᴇ", "🗑 Dᴇʟᴇᴛᴇ"} <= set(labels(markup))

    async def test_one_time_item_view_names_its_type(self, channel):
        doc = await schedule_doc(channel.id)
        await open_channel(OWNER, channel.id)
        text, _ = edited(await press(OWNER, f"it:{doc['schedule_id']}:p:0"))
        assert "🗂 <b>Schedule:</b> 📌 One-Time" in text

    async def test_pause_and_resume(self, channel, telegram, clock):
        doc = await daily_doc(channel.id, 12, 30)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        text, markup = edited(await press(OWNER, f"ps:{sid}"))
        assert "Paused</b> — nothing is posted until you resume it." in text and "Status:</b> ⏸ Paused" in text
        assert f"sch:rm:{sid}" in callbacks(markup) and f"sch:ps:{sid}" not in callbacks(markup)
        assert (await store.get_schedule(sid))["status"] == "paused"
        clock.now = ist(4, 12, 30)
        assert await sch.process_due() == 0
        clock.now = ist(5, 9, 0)
        text, _ = edited(await press(OWNER, f"rm:{sid}"))
        assert "Resumed</b> — next post 05-11-2026 · 12:30 PM IST." in text
        resumed = await store.get_schedule(sid)
        assert resumed["status"] == "scheduled" and resumed["scheduled_at"] == ist(5, 12, 30)
        assert "paused_at" not in resumed
        assert [c.args[3] for c in sch.log_schedule_change.await_args_list] == ["PAUSED", "RESUMED"]
        clock.now = ist(5, 12, 30)
        assert await sch.process_due() == 1 and len(telegram.to(channel.id)) == 1

    async def test_resume_never_posts_twice_on_one_day(self, channel):
        doc = await daily_doc(channel.id, 18, 0, status=store.STATUS_PAUSED, last_run_key="2026-11-04")
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rm:{doc['schedule_id']}")       # today's 6 PM would be a 2nd post today
        assert (await store.get_schedule(doc["schedule_id"]))["scheduled_at"] == ist(5, 18, 0)

    async def test_change_time(self, channel):
        doc = await daily_doc(channel.id, 8, 0)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        text, markup = edited(await press(OWNER, f"rs:{sid}"))
        assert "Cʜᴀɴɢᴇ Tɪᴍᴇ</b> <code>#" in text and "Currently: every day at 08:00 AM IST" in text
        assert all(f"sch:hr:{h:02d}" in callbacks(markup) for h in range(24))
        await press(OWNER, "hr:18")
        text, _ = edited(await press(OWNER, "mn:45"))
        assert "Cᴏɴғɪʀᴍ Nᴇᴡ Tɪᴍᴇ" in text and "Old time:</b> every day at 08:00 AM IST" in text
        assert "New time:</b> every day at 06:45 PM IST" in text
        assert "Next post:</b> 04-11-2026 · 06:45 PM IST" in text
        text, _ = edited(await press(OWNER, "ok"))
        assert "Time changed</b> — every day at 06:45 PM IST; next post 04-11-2026 · 06:45 PM IST." in text
        changed = await store.get_schedule(sid)
        assert changed["daily_time"] == {"hour": 18, "minute": 45} and changed["scheduled_at"] == ist(4, 18, 45)
        assert changed["status"] == "scheduled"

    async def test_change_time_of_a_paused_schedule(self, channel):
        doc = await daily_doc(channel.id, 8, 0, status=store.STATUS_PAUSED)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rs:{doc['schedule_id']}")
        await press(OWNER, "hr:07")
        assert "Paused</b> — it restarts at the new time when you resume it." in \
            edited(await press(OWNER, "mn:15"))[0]
        assert "restarts at that time when you resume it" in edited(await press(OWNER, "ok"))[0]
        changed = await store.get_schedule(doc["schedule_id"])
        assert changed["status"] == "paused" and changed["daily_time"] == {"hour": 7, "minute": 15}
        assert changed["scheduled_at"] == doc["scheduled_at"]

    async def test_change_time_back_navigation(self, channel):
        doc = await daily_doc(channel.id, 8, 0)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rs:{doc['schedule_id']}")
        await press(OWNER, "hr:10")
        assert "Cʜᴀɴɢᴇ Tɪᴍᴇ" in edited(await press(OWNER, "bk"))[0]          # minutes -> hours
        assert "Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ</b> <code>#" in edited(await press(OWNER, "bk"))[0]   # -> the schedule
        assert (await store.get_schedule(doc["schedule_id"]))["daily_time"] == {"hour": 8, "minute": 0}

    async def test_delete_daily(self, channel):
        doc = await daily_doc(channel.id, 8, 0)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        text, _ = edited(await press(OWNER, f"da:{sid}"))
        assert "Dᴇʟᴇᴛᴇ Dᴀɪʟʏ Sᴄʜᴇᴅᴜʟᴇ" in text and "Every day at 08:00 AM IST" in text
        assert await store.get_schedule(sid) is not None
        assert f"Post <code>#{sid}</code> deleted" in edited(await press(OWNER, f"dy:{sid}"))[0]
        assert await store.get_schedule(sid) is None

    async def test_cancel_is_only_for_one_time_posts(self, channel):
        doc = await daily_doc(channel.id, 8, 0)
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        (await press(OWNER, f"ca:{sid}")).answer.assert_any_await("Not possible for this post right now.",
                                                                  show_alert=True)
        assert await store.cancel_schedule(sid, OWNER) is None
        assert await store.reschedule(sid, NOW + 999, OWNER) is None
        assert (await store.get_schedule(sid))["status"] == "scheduled"

    async def test_edit_daily_content(self, channel, telegram):
        doc = await daily_doc(channel.id, 8, 0)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"ed:{doc['schedule_id']}")
        await sch.handle_input(await admin_message(RICH_TEXT, entities=rich_entities(), telegram=telegram))
        assert "Changes saved" in edited(await press(OWNER, "nx"))[0]
        updated = await store.get_schedule(doc["schedule_id"])
        assert updated["content"]["text"] == RICH_TEXT and updated["daily_time"] == {"hour": 8, "minute": 0}
        assert updated["status"] == "scheduled" and updated["scheduled_at"] == doc["scheduled_at"]
        assert await stored_wire(updated["content"]) == wire(rich_entities())

    async def test_nothing_changes_while_an_occurrence_is_sending(self, channel):
        doc = await daily_doc(channel.id, 8, 0, status=store.STATUS_SENDING, claimed_at=NOW, claim_token="t")
        sid = doc["schedule_id"]
        await open_channel(OWNER, channel.id)
        for data in (f"ps:{sid}", f"rm:{sid}", f"rs:{sid}", f"dy:{sid}", f"ed:{sid}"):
            await press(OWNER, data)
        assert await store.pause_schedule(sid, OWNER) is None
        assert await store.change_daily_time(sid, 9, 0, NOW + 999, OWNER, None) is None
        assert (await store.get_schedule(sid))["status"] == "sending"

    async def test_history_lists_completed_one_time_posts(self, channel):
        await schedule_doc(channel.id, text="Done", status=store.STATUS_SENT, sent_at=NOW - 60)
        await daily_doc(channel.id, 8, 0, text="Daily one")
        await open_channel(OWNER, channel.id)
        text, _ = edited(await press(OWNER, f"ls:{channel.id}:h:0"))
        assert "Done" in text and "Status: ✅ Completed" in text and "Daily one" not in text


class TestWorkerScheduling:
    def test_sleep_until_the_next_due_post(self):
        assert sch._next_wait(NOW, None, 0) == sch.MAX_IDLE_WAIT
        assert sch._next_wait(NOW, NOW + 5, 0) == 5
        assert sch._next_wait(NOW, NOW + 3600, 0) == sch.MAX_IDLE_WAIT
        assert sch._next_wait(NOW, NOW - 10, 0) == 0.5
        assert sch._next_wait(NOW, NOW + 5, sch.BATCH) == 0

    async def test_next_due_at(self, channel):
        assert await store.next_due_at() is None
        await schedule_doc(channel.id, when=NOW + 500)
        await daily_doc(channel.id, 13, 0)                                      # NOW + 3600
        assert await store.next_due_at() == NOW + 500
        await schedule_doc(channel.id, when=NOW + 100, retry_at=NOW + 900)     # due at its retry time
        assert await store.next_due_at() == NOW + 500
        await schedule_doc(channel.id, when=NOW + 50, retry_at=NOW + 200)
        assert await store.next_due_at() == NOW + 200
        await daily_doc(channel.id, 12, 1, status=store.STATUS_PAUSED)          # paused: never due
        assert await store.next_due_at() == NOW + 200

    async def test_wakes_when_due_and_when_an_admin_adds_a_post(self, channel, telegram, monkeypatch):
        monkeypatch.setattr(st, "now_ts", lambda: int(time.time()))             # real time here
        start = int(time.time())
        await schedule_doc(channel.id, text="soon", when=start + 2)
        task = asyncio.create_task(sch._worker_loop())
        try:
            for _ in range(80):
                if telegram.to(channel.id):
                    break
                await asyncio.sleep(0.1)
            assert len(telegram.to(channel.id)) == 1, "slept until the post was due"
            # now it sleeps up to MAX_IDLE_WAIT (30 s) — a new post must not wait for that
            await schedule_doc(channel.id, text="now", when=int(time.time()))
            sch.wake_worker()
            started = time.time()
            for _ in range(30):
                if len(telegram.to(channel.id)) == 2:
                    break
                await asyncio.sleep(0.1)
            assert len(telegram.to(channel.id)) == 2 and time.time() - started < 3
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_one_worker_per_process(self, monkeypatch):
        started = []

        async def loop():
            started.append(1)
            await asyncio.sleep(3600)
        monkeypatch.setattr(sch, "_worker_loop", loop)
        monkeypatch.setattr(sch, "_worker_task", None)
        for _ in range(3):
            await REAL_START_WORKER()
        await asyncio.sleep(0)
        assert len(started) == 1
        sch._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await sch._worker_task


class TestDeployment:
    """requirements.txt and the Heroku entry point."""

    ROOT = Path(__file__).resolve().parent.parent
    # import name -> distribution in requirements.txt
    DISTRIBUTIONS = {"pyrogram": "kurigram", "motor": "motor", "pymongo": "pymongo",
                     "aiohttp": "aiohttp", "dotenv": "python-dotenv", "yaml": "pyyaml",
                     "heroku3": "heroku3"}

    @staticmethod
    def _names(lines):
        out = set()
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                out.add(re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0].lower())
        return out

    def test_every_third_party_import_is_in_requirements(self):
        listed = self._names((self.ROOT / "requirements.txt").read_text().splitlines())
        imported = set()
        files = list((self.ROOT / "SWAGGYMUSIC").rglob("*.py")) + list((self.ROOT / "strings").rglob("*.py"))
        for path in files + [self.ROOT / "config.py"]:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        third_party = {name for name in imported
                       if name not in sys.stdlib_module_names and name not in {"SWAGGYMUSIC", "strings", "config"}}
        unknown = third_party - set(self.DISTRIBUTIONS)
        assert not unknown, f"new third-party import(s) {unknown}: add them to requirements.txt (and here)"
        missing = {self.DISTRIBUTIONS[name] for name in third_party} - listed
        assert not missing, f"imported but missing from requirements.txt: {missing}"

    def test_pyproject_matches_requirements(self):
        listed = self._names((self.ROOT / "requirements.txt").read_text().splitlines())
        project = tomllib.loads((self.ROOT / "pyproject.toml").read_text())["project"]
        assert self._names(project["dependencies"]) == listed

    def test_pymongo_pin_is_compatible(self):
        from importlib.metadata import version

        line = next(l for l in (self.ROOT / "requirements.txt").read_text().splitlines()
                    if l.lower().startswith("pymongo"))
        clauses = re.findall(r"(>=|<=|==|<|>)\s*([\d.]+)", line)

        def numbers(text):
            return tuple(int(part) for part in text.split(".") if part.isdigit())

        def allowed(ver):
            checks = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
                      "<": lambda a, b: a < b, ">": lambda a, b: a > b}
            return all(checks[op](numbers(ver), numbers(num)) for op, num in clauses)
        assert clauses == [(">=", "4.9"), ("<", "5")]
        assert allowed(version("pymongo")), "the pymongo the tests run with"
        assert allowed("4.10.1"), "requirements-test.txt pins pymongo==4.10.1"
        assert allowed("4.9.0") and allowed("4.19.0") and not allowed("5.0.0") and not allowed("4.8.2")

    def test_single_heroku_worker_runs_the_scheduler(self):
        assert (self.ROOT / "Procfile").read_text().strip() == "worker: python3 -m SWAGGYMUSIC"
        assert "worker: python3 -m SWAGGYMUSIC" in (self.ROOT / "heroku.yml").read_text()
        source = Path(sch.__file__).read_text()
        assert "asyncio.get_running_loop().create_task(_start_worker())" in source


async def to_confirm(channel_id, telegram, *, kind, day: str, hour: int, minute: int, text="Post"):
    """Compose -> preview -> 📅 date -> 🕒 time; returns the confirmation (text, markup)."""
    await compose_and_preview(channel_id, telegram, kind=kind, text=text, entities=[])
    await press(OWNER, "nx")
    await press(OWNER, f"day:{day}")
    await press(OWNER, f"hr:{hour:02d}")
    return edited(await press(OWNER, f"mn:{minute:02d}"))


class TestDateTimeSelection:
    """Part 1: date and time are picked independently, start dates are respected."""

    async def test_one_time_example(self, channel, telegram, clock):
        text, _ = await to_confirm(channel.id, telegram, kind="once", day="20261106", hour=10, minute=30)
        for line in ("🗂 <b>Schedule Type:</b> 📌 One-Time", "📅 <b>Start Date:</b> 06-11-2026",
                     "⏰ <b>Time:</b> 10:30 AM IST", "⏭ <b>Next Execution:</b> 06-11-2026 10:30 AM IST"):
            assert line in text
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["scheduled_at"] == int(datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc).timestamp()), \
            "stored in UTC (10:30 IST = 05:00 UTC)"
        assert doc["start_date"] == "2026-11-06"
        for at in (ist(6, 10, 29), ist(6, 10, 30), ist(6, 10, 31)):
            clock.now = at
            await sch.process_due()
        assert len(telegram.to(channel.id)) == 1 and (await store.get_schedule(doc["schedule_id"]))["status"] == "sent"

    async def test_daily_created_before_todays_time(self, channel, telegram, clock):
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261104", hour=18, minute=0)
        assert "⏭ <b>Next Execution:</b> 04-11-2026 06:00 PM IST" in text and "ℹ️" not in text
        await press(OWNER, "ok")
        clock.now = ist(4, 18, 0)
        assert await sch.process_due() == 1

    async def test_daily_created_after_todays_time_starts_tomorrow_with_an_explanation(self, channel, telegram,
                                                                                         clock):
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261104", hour=10, minute=30)
        assert "📅 <b>Start Date:</b> 04-11-2026" in text, "the start date is not changed"
        assert "⏭ <b>Next Execution:</b> 05-11-2026 10:30 AM IST" in text
        assert "Today's 10:30 AM IST has already passed, so the first post will go out <b>tomorrow" in text
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["start_date"] == "2026-11-04" and doc["scheduled_at"] == ist(5, 10, 30)
        clock.now = ist(4, 23, 0)
        assert await sch.process_due() == 0
        clock.now = ist(5, 10, 30)
        assert await sch.process_due() == 1

    async def test_future_start_date_is_respected(self, channel, telegram, clock):
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261106", hour=10, minute=30)
        assert "📅 <b>Start Date:</b> 06-11-2026" in text and "Next Execution:</b> 06-11-2026 10:30 AM IST" in text
        assert "ℹ️" not in text
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["scheduled_at"] == ist(6, 10, 30) and doc["start_date"] == "2026-11-06"
        for day in (4, 5):                                       # nothing before the start date
            clock.now = ist(day, 10, 30)
            assert await sch.process_due() == 0
        clock.now = ist(6, 10, 30)
        assert await sch.process_due() == 1
        assert (await store.get_schedule(doc["schedule_id"]))["scheduled_at"] == ist(7, 10, 30)

    async def test_around_midnight(self, channel, telegram, clock):
        clock.now = ist(4, 23, 58)
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261104", hour=23, minute=59)
        assert "Next Execution:</b> 04-11-2026 11:59 PM IST" in text and "ℹ️" not in text
        clock.now = ist(4, 23, 59) + 30
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261104", hour=23, minute=59)
        assert "Next Execution:</b> 05-11-2026 11:59 PM IST" in text and "has already passed" in text
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261105", hour=0, minute=0)
        assert "📅 <b>Start Date:</b> 05-11-2026" in text                      # 30 s away: too close
        assert "Next Execution:</b> 06-11-2026 12:00 AM IST" in text and "no longer possible" in text
        text, _ = await to_confirm(channel.id, telegram, kind="once", day="20261105", hour=0, minute=0)
        assert "Pick a time at least 1 minute from now." in text                # one-time: refused
        assert sch.SESSIONS[OWNER]["step"] == "minute"
        clock.now = ist(5, 0, 0) + 5                                            # after midnight
        session = sch.SESSIONS[OWNER]
        session["cal"] = None
        _text, markup = sch.render_calendar(session)
        assert "sch:day:20261104" not in callbacks(markup) and "sch:day:20261105" in callbacks(markup)

    async def test_change_date_keeps_the_time(self, channel, telegram):
        await to_confirm(channel.id, telegram, kind="once", day="20261105", hour=10, minute=30)
        text, _ = edited(await press(OWNER, "cd"))
        assert "Sᴇʟᴇᴄᴛ Dᴀᴛᴇ" in text and "Currently: 05-11-2026 10:30 AM IST — the time stays the same." in text
        text, _ = edited(await press(OWNER, "day:20261107"))
        assert "Start Date:</b> 07-11-2026" in text and "Time:</b> 10:30 AM IST" in text
        assert sch.SESSIONS[OWNER]["scheduled_at"] == ist(7, 10, 30) and sch.SESSIONS[OWNER]["step"] == "confirm"

    async def test_change_time_keeps_the_date(self, channel, telegram):
        await to_confirm(channel.id, telegram, kind="once", day="20261105", hour=10, minute=30)
        text, _ = edited(await press(OWNER, "ct"))
        assert "Sᴇʟᴇᴄᴛ Tɪᴍᴇ" in text and "Date:</b> 05-11-2026" in text
        await press(OWNER, "hr:18")
        text, _ = edited(await press(OWNER, "mn:15"))
        assert "Start Date:</b> 05-11-2026" in text and "Time:</b> 06:15 PM IST" in text

    async def test_change_date_to_a_day_whose_time_has_passed_asks_for_a_time(self, channel, telegram):
        await to_confirm(channel.id, telegram, kind="once", day="20261105", hour=10, minute=0)
        await press(OWNER, "cd")
        text, markup = edited(await press(OWNER, "day:20261104"))           # today: 10:00 AM is past
        assert "That time is in the past." in text and sch.SESSIONS[OWNER]["step"] == "hour"
        assert "sch:hr:10" not in callbacks(markup) and "sch:hr:13" in callbacks(markup)
        await press(OWNER, "hr:13")
        text, _ = edited(await press(OWNER, "mn:00"))
        assert "Start Date:</b> 04-11-2026" in text and "Next Execution:</b> 04-11-2026 01:00 PM IST" in text

    async def test_daily_change_date_recomputes_the_next_execution(self, channel, telegram):
        text, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261104", hour=10, minute=30)
        assert "has already passed" in text
        text, _ = edited(await press(OWNER, "cd"))
        assert "Currently: starts 04-11-2026, every day at 10:30 AM IST — the time stays the same." in text
        text, _ = edited(await press(OWNER, "day:20261106"))
        assert "Start Date:</b> 06-11-2026" in text and "Next Execution:</b> 06-11-2026 10:30 AM IST" in text
        assert "ℹ️" not in text

    async def test_back_from_a_change_returns_to_the_unchanged_confirmation(self, channel, telegram):
        before, _ = await to_confirm(channel.id, telegram, kind="daily", day="20261105", hour=9, minute=15)
        await press(OWNER, "cd")
        assert edited(await press(OWNER, "bk"))[0] == before                # date untouched
        await press(OWNER, "ct")
        await press(OWNER, "hr:20")
        assert "Sᴇʟᴇᴄᴛ Mɪɴᴜᴛᴇs" not in edited(await press(OWNER, "bk"))[0]  # minutes -> hours
        assert edited(await press(OWNER, "bk"))[0] == before                # hours -> confirmation
        await press(OWNER, "ok")
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert doc["daily_time"] == {"hour": 9, "minute": 15} and doc["start_date"] == "2026-11-05"

    async def test_typed_start_date_with_time(self, channel, telegram):
        await compose_and_preview(channel.id, telegram, kind="daily", text="Post", entities=[])
        await press(OWNER, "nx")
        msg = text_msg(OWNER, "06-11-2026 10:30 AM")
        await sch.handle_input(msg)
        text = msg.reply_text.await_args.args[0]
        assert "Start Date:</b> 06-11-2026" in text and "Next Execution:</b> 06-11-2026 10:30 AM IST" in text

    async def test_reschedule_offers_change_date_and_change_time(self, channel):
        doc = await schedule_doc(channel.id, status=store.STATUS_FAILED, error={"code": "X", "message": "m"})
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rs:{doc['schedule_id']}")
        _text, markup = edited(await pick(OWNER, date(2026, 11, 8), 7, 45))
        assert {"sch:ok", "sch:cd", "sch:ct", "sch:abort"} == set(callbacks(markup))
        await press(OWNER, "cd")
        text, _ = edited(await press(OWNER, "day:20261109"))
        assert "Date:</b> 09-11-2026" in text and "Time:</b> 07:45 AM IST" in text

    async def test_start_date_survives_a_restart(self, channel, telegram, clock):
        await to_confirm(channel.id, telegram, kind="daily", day="20261106", hour=10, minute=30)
        await press(OWNER, "ok")
        sch.SESSIONS.clear()          # restart: only MongoDB remains
        sch._VERIFIED.clear()
        doc = await store.scheduled_db.find_one({"channel_id": channel.id})
        assert (doc["start_date"], doc["daily_time"], doc["timezone"]) == \
            ("2026-11-06", {"hour": 10, "minute": 30}, "Asia/Kolkata")
        await open_channel(OWNER, channel.id)
        text, _ = edited(await press(OWNER, f"it:{doc['schedule_id']}:p:0"))
        assert "📅 <b>Start Date:</b> 06-11-2026" in text and "Next post:</b> 06-11-2026 · 10:30 AM IST" in text
        clock.now = ist(6, 10, 30)
        assert await sch.process_due() == 1 and await sch.process_due() == 0

    async def test_resume_respects_a_future_start_date(self, channel):
        doc = await daily_doc(channel.id, 9, 0, start="2026-11-08", first=ist(8, 9, 0), status=store.STATUS_PAUSED)
        await open_channel(OWNER, channel.id)
        await press(OWNER, f"rm:{doc['schedule_id']}")
        assert (await store.get_schedule(doc["schedule_id"]))["scheduled_at"] == ist(8, 9, 0)
