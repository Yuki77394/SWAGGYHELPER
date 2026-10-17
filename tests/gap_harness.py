"""Shared helpers for the /setgap, /setdelay and anonymous-filter tests.

Not a test module (no ``test_`` prefix). Fixtures that use these helpers
live in ``conftest.py``.

* ``FakeBotChannel`` — an in-memory channel that behaves the way Telegram
  behaves for a BOT account:
    - ``get_messages`` works and returns an *empty* message for a deleted id;
    - ``delete_messages`` returns how many messages were really deleted
      (``pts_count`` semantics — 0 when the post was already gone);
    - ``get_chat_history`` raises BOT_METHOD_INVALID (bots cannot read
      channel history).
* ``real_post`` — builds a REAL ``pyrogram.types.Message`` (not a mock), so
  Kurigram's own filters (channel, incoming, regex, service ...) run on it.
* ``tg_post`` / ``tg_album`` — build the RAW MTProto update (``raw.types.
  Message`` with ``from_id``, ``grouped_id``, photo/video media ...) and run
  it through Kurigram's own ``Message._parse``, exactly like a live update.
* ``dispatch`` — replays Kurigram's ``Dispatcher.handler_worker`` loop over
  the handlers actually registered on ``app``: groups in ascending order,
  first matching handler per group, ``StopPropagation`` /
  ``ContinuePropagation`` honoured. ``dispatch_callback`` does the same for
  callback queries.
"""
from __future__ import annotations

import asyncio
import itertools
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram.enums import ChatType
from pyrogram.errors import BotMethodInvalid

GAP = 15 * 60  # 15 minutes
# "10:00" of the scenarios. Anchored one day in the past so that /setdelay
# jobs created by the tests are already due for the real worker clock.
BASE = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
    minute=0, second=0, microsecond=0,
)


def at(minutes: float, seconds: int = 0) -> datetime:
    """Scenario clock: ``at(15)`` is "10:15" (BASE + 15 minutes)."""
    return BASE + timedelta(minutes=minutes, seconds=seconds)


def ts(when: datetime) -> int:
    return int(when.timestamp())


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def mock_channel_message(chat_id: int, msg_id: int, when: datetime, text: str,
                         *, edit_date: Optional[datetime] = None) -> MagicMock:
    chat = MagicMock()
    chat.id = chat_id
    chat.type = ChatType.CHANNEL
    msg = MagicMock()
    msg.chat = chat
    msg.id = msg_id
    msg.text = text
    msg.caption = None
    msg.date = when
    msg.edit_date = edit_date
    msg.outgoing = False
    msg.entities = []
    msg.from_user = None
    msg.reply_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def real_post(chat_id: int, msg_id: int, when: datetime, text: str = "post", *,
              personal_user_id: Optional[int] = None,
              sender_chat_id: Optional[int] = "channel",
              author_signature: Optional[str] = None,
              service=None,
              edit_date: Optional[datetime] = None,
              media_group_id: Optional[int] = None):
    """A real Kurigram channel Message, as Message._parse_message builds it
    (without the ``raw`` update — see ``tg_post`` for that).

    * ``personal_user_id`` set  -> Telegram exposed the admin's account
      (from_id is a user): ``from_user`` is set and ``sender_chat`` is None.
    * otherwise ``sender_chat`` is the channel itself ("channel identity"),
      or another chat id / None when given explicitly.
    """
    from pyrogram import types

    from SWAGGYMUSIC import app

    chat = types.Chat(id=chat_id, type=ChatType.CHANNEL, title="Test channel", client=app)
    from_user = None
    sender_chat = None
    if personal_user_id is not None:
        from_user = types.User(id=personal_user_id, first_name="Admin", client=app)
    elif sender_chat_id == "channel":
        sender_chat = chat
    elif sender_chat_id is not None:
        sender_chat = types.Chat(id=sender_chat_id, type=ChatType.CHANNEL, title="Other", client=app)
    return types.Message(
        id=msg_id, date=when, chat=chat, from_user=from_user, sender_chat=sender_chat,
        text=text, author_signature=author_signature, service=service,
        edit_date=edit_date, outgoing=False, media_group_id=media_group_id, client=app,
    )


# ---------------------------------------------------------------------------
# Raw MTProto updates parsed by Kurigram itself
# ---------------------------------------------------------------------------

# Telegram's grouped_id is a signed 64-bit "long"; one value per album.
_ALBUM_GROUP_IDS = itertools.count(13_584_906_512_000_001)
# Raw ids of "other" channels used as a sender identity.
_OTHER_CHANNEL_IDS = itertools.count(1_700_000_001)


def new_album_id() -> int:
    return next(_ALBUM_GROUP_IDS)


def other_channel_raw_id() -> int:
    return next(_OTHER_CHANNEL_IDS)


def _raw_channel(raw_id: int, title: str):
    from pyrogram import raw

    # Vector fields default to [] exactly as TLObject.read() leaves them.
    return raw.types.Channel(
        id=raw_id, title=title, photo=raw.types.ChatPhotoEmpty(), date=0,
        broadcast=True, access_hash=raw_id * 7, usernames=[], restriction_reason=[],
    )


def _raw_user(user_id: int):
    from pyrogram import raw

    return raw.types.User(id=user_id, first_name="Admin", access_hash=user_id * 3,
                          usernames=[], restriction_reason=[])


def _raw_media(kind: Optional[str], n: int):
    from pyrogram import raw

    if kind is None:
        return None
    if kind == "photo":
        return raw.types.MessageMediaPhoto(photo=raw.types.Photo(
            id=10_000 + n, access_hash=1, file_reference=b"\x01", date=0,
            sizes=[raw.types.PhotoSize(type="x", w=1280, h=720, size=90_000)],
            dc_id=2, video_sizes=[],
        ))
    if kind == "video":
        return raw.types.MessageMediaDocument(document=raw.types.Document(
            id=20_000 + n, access_hash=1, file_reference=b"\x01", date=0,
            mime_type="video/mp4", size=4_000_000, dc_id=2,
            attributes=[raw.types.DocumentAttributeVideo(duration=12, w=1280, h=720)],
            thumbs=[], video_thumbs=[],
        ), alt_documents=[])
    raise ValueError(kind)


async def tg_post(ch: "FakeBotChannel", msg_id: int, when: datetime, text: str = "post", *,
                  user_id: Optional[int] = None, user_in_update: bool = True,
                  sender_channel_id: Optional[int] = None,
                  media: Optional[str] = None, grouped_id: Optional[int] = None,
                  post_author: Optional[str] = None, alive: bool = True):
    """A channel post as Kurigram parses it from Telegram's raw update.

    Sender (``from_id`` of the raw message):
      * default                -> omitted: the post is sent with the channel's
                                  own identity;
      * ``user_id``            -> PeerUser: the admin's personal account
                                  (``user_in_update=False``: the User object is
                                  missing from the update, as Telegram sometimes
                                  does);
      * ``sender_channel_id``  -> PeerChannel: sent with that channel's identity
                                  (a raw channel id; may be this channel's).
    ``media`` "photo"/"video" + ``grouped_id`` build album items.
    """
    from pyrogram import raw, types, utils

    from SWAGGYMUSIC import app

    channel_raw_id = utils.get_raw_peer_id(ch.chat_id)
    chats = {channel_raw_id: _raw_channel(channel_raw_id, "Test channel")}
    users = {}
    from_id = None
    if user_id is not None:
        from_id = raw.types.PeerUser(user_id=user_id)
        if user_in_update:
            users[user_id] = _raw_user(user_id)
    elif sender_channel_id is not None:
        from_id = raw.types.PeerChannel(channel_id=sender_channel_id)
        chats.setdefault(sender_channel_id, _raw_channel(sender_channel_id, "Other channel"))
    raw_message = raw.types.Message(
        id=msg_id, peer_id=raw.types.PeerChannel(channel_id=channel_raw_id),
        date=int(when.timestamp()), message=text, post=True, from_id=from_id,
        media=_raw_media(media, msg_id), grouped_id=grouped_id, post_author=post_author,
        entities=[], restriction_reason=[],
    )
    if alive:
        ch.messages[msg_id] = when
    return await types.Message._parse(app, raw_message, users, chats, replies=0)


async def tg_album(ch: "FakeBotChannel", first_id: int, when: datetime,
                   kinds: Sequence[str] = ("photo", "video", "photo"),
                   caption: str = "album", **sender) -> list:
    """The items of ONE media album (same grouped_id, consecutive ids, same
    date) — Telegram delivers each item as a separate update."""
    group_id = new_album_id()
    return [
        await tg_post(ch, first_id + i, when, caption if i == 0 else "",
                      media=kind, grouped_id=group_id, **sender)
        for i, kind in enumerate(kinds)
    ]


class FakeBotChannel:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.messages: Dict[int, datetime] = {}  # alive posts
        self.bot_deleted: List[int] = []
        self.get_messages_calls: List[int] = []      # every id asked about
        self.get_messages_requests: List[List[int]] = []  # one entry per API call
        self.get_messages_error: Optional[BaseException] = None
        self.get_messages_delay: float = 0.0
        self.delete_error: Optional[BaseException] = None
        self.delete_errors: List[BaseException] = []  # consumed one per call
        # Telegram performs the deletion, but the answer is lost (network):
        self.delete_then_raise: List[BaseException] = []  # consumed per call

    # -- test controls -----------------------------------------------------
    def new_post(self, msg_id: int, when: datetime, text: str = "post") -> MagicMock:
        self.messages[msg_id] = when
        return mock_channel_message(self.chat_id, msg_id, when, text)

    def new_real_post(self, msg_id: int, when: datetime, text: str = "post", **kw):
        self.messages[msg_id] = when
        return real_post(self.chat_id, msg_id, when, text, **kw)

    def edited(self, msg_id: int, *, with_edit_date: bool = True) -> MagicMock:
        """An update for an EXISTING post (edit). ``date`` stays the original."""
        when = self.messages[msg_id]
        return mock_channel_message(
            self.chat_id, msg_id, when, "edited text",
            edit_date=(when + timedelta(minutes=30)) if with_edit_date else None,
        )

    def delete_manually(self, msg_id: int) -> None:
        """An admin deletes the post in the Telegram app (no bot API call)."""
        self.messages.pop(msg_id, None)

    def alive(self, msg_id: int) -> bool:
        return msg_id in self.messages

    # -- Telegram API surface used by the bot ------------------------------
    async def get_messages(self, chat_id=None, message_ids=None, replies=1, **_):
        if self.get_messages_delay:
            await asyncio.sleep(self.get_messages_delay)
        if self.get_messages_error is not None:
            raise self.get_messages_error
        single = isinstance(message_ids, int)
        ids = [message_ids] if single else list(message_ids)
        self.get_messages_calls.extend(ids)
        self.get_messages_requests.append(list(ids))
        out = []
        for mid in ids:
            m = MagicMock()
            m.id = mid
            if chat_id == self.chat_id and mid in self.messages:
                m.empty = None
                m.date = self.messages[mid]
            else:  # Telegram returns MessageEmpty -> Message(empty=True, date=None)
                m.empty = True
                m.date = None
            out.append(m)
        return out[0] if single else out

    async def delete_messages(self, chat_id, message_ids, revoke=True, **_):
        if chat_id != self.chat_id:
            # The real worker also picks up due jobs other tests left in the
            # shared in-memory DB. Nothing of theirs lives here, and they
            # must not consume this channel's injected errors.
            return 0
        if self.delete_errors:
            raise self.delete_errors.pop(0)
        if self.delete_error is not None:
            raise self.delete_error
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        deleted = 0
        for mid in ids:
            if chat_id == self.chat_id and mid in self.messages:
                del self.messages[mid]
                self.bot_deleted.append(mid)
                deleted += 1
        if self.delete_then_raise:
            raise self.delete_then_raise.pop(0)
        return deleted  # channels.deleteMessages -> pts_count

    async def get_chat_history(self, *args, **kwargs):
        # Bots cannot read channel history: messages.getHistory -> BOT_METHOD_INVALID.
        raise BotMethodInvalid()
        yield  # pragma: no cover  (makes this an async generator like Kurigram's)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def enable_gap(chat_id: int, gap: int = GAP) -> None:
    from SWAGGYMUSIC.utils.database import settings_db
    await settings_db.update_one(
        {"chat_id": chat_id},
        {
            "$set": {"chat_id": chat_id, "gap": int(gap), "gap_enabled": True},
            "$unset": {"last_gap_post_at": "", "last_gap_post_msg_id": "", "gap_history": ""},
        },
        upsert=True,
    )


async def enable_delay(chat_id: int, delay: int) -> None:
    from SWAGGYMUSIC.utils.database import settings_db
    await settings_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "delay": int(delay), "enabled": True}},
        upsert=True,
    )


async def enable_anonymous_filter(chat_id: int) -> None:
    from SWAGGYMUSIC.utils.database import settings_db
    await settings_db.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "anonymous_filter_enabled": True}},
        upsert=True,
    )


async def settings(chat_id: int) -> dict:
    from SWAGGYMUSIC.utils.database import settings_db
    return await settings_db.find_one({"chat_id": chat_id}) or {}


def history_ids(doc: dict) -> List[int]:
    return [e["msg_id"] for e in doc.get("gap_history") or []]


def history_entry(doc: dict, msg_id: int) -> dict:
    return next(e for e in doc.get("gap_history") or [] if e["msg_id"] == msg_id)


async def publish(ch: FakeBotChannel, msg_id: int, when: datetime) -> bool:
    """A new post reaches the gap/delay handler; True if it survived."""
    from SWAGGYMUSIC import app
    from SWAGGYMUSIC.plugins.misc import autodelete as ad

    await ad.autodelete_new_channel_post(app, ch.new_post(msg_id, when))
    return ch.alive(msg_id)


def incoming(chat_id: int, msg_id: int) -> MagicMock:
    m = MagicMock()
    m.id = msg_id
    m.chat = MagicMock()
    m.chat.id = chat_id
    return m


async def run_worker_until(monkeypatch, done, max_wait: float = 3.0) -> None:
    """Run the real /setdelay worker until ``await done()`` is true."""
    from SWAGGYMUSIC.plugins.misc import autodelete as ad

    # Don't create the TTL index in the shared in-memory DB: mongomock
    # would then reap old-dated jobs written by unrelated tests.
    monkeypatch.setattr(ad, "ensure_channel_indexes", AsyncMock())
    worker = asyncio.create_task(ad._delete_worker())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        while not await done():
            assert loop.time() < deadline, "worker did not process the job in time"
            await asyncio.sleep(0.05)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def job_count(chat_id: int, msg_id: int) -> int:
    from SWAGGYMUSIC.utils.database import jobs_db
    return await jobs_db.count_documents({"chat_id": chat_id, "message_id": msg_id})


# ---------------------------------------------------------------------------
# Real handler registry + Kurigram dispatch loop
# ---------------------------------------------------------------------------

_FLUSH_LOCK = threading.Lock()


def _flush_registrations(loop) -> None:
    # One thread at a time: concurrent dispatch() calls must not both try
    # to run app.loop ("This event loop is already running").
    with _FLUSH_LOCK:
        if loop.is_closed() or loop.is_running() or not asyncio.all_tasks(loop):
            return
        loop.run_until_complete(asyncio.sleep(0.05))


async def registered_groups():
    """``app.dispatcher.groups`` with every channel plugin registered.

    Kurigram's ``add_handler`` schedules the registration as a task on
    ``app.loop``; that loop never runs under pytest, so pending
    registrations are flushed on a helper thread.
    """
    import SWAGGYMUSIC.plugins.bot.setconfig  # noqa: F401  (registers handlers)
    import SWAGGYMUSIC.plugins.misc.anonymous  # noqa: F401
    import SWAGGYMUSIC.plugins.misc.autodelete  # noqa: F401
    from SWAGGYMUSIC import app

    await asyncio.to_thread(_flush_registrations, app.loop)
    return app.dispatcher.groups


async def _replay(update, handler_type) -> list:
    """Kurigram's handler_worker loop for one update; returns the callbacks
    that ran, in order."""
    import pyrogram

    from SWAGGYMUSIC import app

    ran: list = []
    try:
        for group in (await registered_groups()).values():
            for handler in group:
                if not isinstance(handler, handler_type):
                    continue
                try:
                    if not await handler.check(app, update):
                        continue
                except Exception:
                    continue
                ran.append(handler.callback)
                try:
                    await handler.callback(app, update)
                except pyrogram.StopPropagation:
                    raise
                except pyrogram.ContinuePropagation:
                    continue
                except Exception:
                    pass  # Kurigram logs it and moves on to the next group
                break
    except pyrogram.StopPropagation:
        pass
    return ran


async def dispatch(message) -> List[str]:
    """Deliver ``message`` like Dispatcher.handler_worker does; returns the
    names of the handlers that ran, in order."""
    from pyrogram.handlers import MessageHandler

    return [callback.__name__ for callback in await _replay(message, MessageHandler)]


async def dispatch_callback(query) -> list:
    """Deliver a CallbackQuery like Dispatcher.handler_worker does; returns
    the handler callbacks (function objects) that ran, in order."""
    from pyrogram.handlers import CallbackQueryHandler

    return await _replay(query, CallbackQueryHandler)
