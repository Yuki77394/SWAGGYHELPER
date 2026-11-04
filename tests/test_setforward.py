"""/setforward (FORWARD POST REMOVER) tests.

REAL Kurigram messages: raw MTProto updates — with and without Telegram's
forward header (``fwd_from``) — parsed by ``Message._parse`` exactly like a
live update, and delivered through the handlers actually registered on
``app`` (gap_harness.dispatch replays Kurigram's dispatcher loop). Telegram
calls are faked by the ``channel`` fixture (FakeBotChannel); the DB is
mongomock-motor (conftest.py).

  1. forwarded posts are deleted when enabled — text, photo, video,
     document, audio, voice, animation, sticker, location, contact, poll,
     albums — from every forward origin
  2. forwarded posts are kept when disabled
  3. normal (non-forwarded) posts are never deleted
  4. the setting persists in MongoDB across restarts
  5. unauthorized users cannot enable/disable the feature
  6. /setgap, /setdelay, /setlink and /setanonymous keep working
  7. channel command, private wizard, FloodWait / permission handling and
     handler-conflict checks
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from gap_harness import (_raw_channel, _raw_media, _raw_user, at, dispatch,
                         dispatch_callback, enable_anonymous_filter,
                         enable_delay, enable_gap, history_ids, job_count,
                         new_album_id, other_channel_raw_id,
                         registered_groups, run_worker_until, settings,
                         tg_post)
from pyrogram import ContinuePropagation, raw, types, utils
from pyrogram.enums import (ChatMemberStatus, ChatType, MessageMediaType,
                            MessageServiceType)
from pyrogram.errors import FloodWait

import SWAGGYMUSIC.plugins.misc.setforward as fwd_mod  # noqa: F401 (registers handlers)
import SWAGGYMUSIC.plugins.misc.setlink as setlink_mod  # noqa: F401 (registers handlers)
from SWAGGYMUSIC import app
from SWAGGYMUSIC.utils import anonymous_policy, forward_policy, link_policy
from SWAGGYMUSIC.utils.database import (jobs_db, set_forward_filter_enabled,
                                        set_link_filter_enabled)

# (pytest.ini: asyncio_mode = auto — async tests need no marker)

ORIGINAL_USER = 555_001          # the account a "user" forward came from
MEDIA_KINDS = (None, "photo", "video", "document", "audio", "voice",
               "animation", "sticker", "location", "contact", "poll")
ORIGINS = ("user", "hidden_user", "channel", "own_channel", "chat",
           "import", "bare")


# ---------------------------------------------------------------------------
# Builders — raw MTProto updates parsed by Kurigram itself
# ---------------------------------------------------------------------------

def _raw_document(n: int, mime: str, attributes: list):
    return raw.types.MessageMediaDocument(document=raw.types.Document(
        id=30_000 + n, access_hash=1, file_reference=b"\x01", date=0,
        mime_type=mime, size=12_345, dc_id=2, attributes=attributes,
        thumbs=[], video_thumbs=[],
    ), alt_documents=[])


def _media(kind: Optional[str], n: int):
    """Raw media of every kind a channel post can carry."""
    if kind in (None, "photo", "video"):
        return _raw_media(kind, n)
    if kind == "document":
        return _raw_document(n, "application/pdf", [
            raw.types.DocumentAttributeFilename(file_name="report.pdf")])
    if kind == "audio":
        return _raw_document(n, "audio/mpeg", [
            raw.types.DocumentAttributeAudio(duration=180, title="Song", performer="Artist"),
            raw.types.DocumentAttributeFilename(file_name="song.mp3")])
    if kind == "voice":
        return _raw_document(n, "audio/ogg", [
            raw.types.DocumentAttributeAudio(duration=5, voice=True)])
    if kind == "animation":
        return _raw_document(n, "video/mp4", [
            raw.types.DocumentAttributeAnimated(),
            raw.types.DocumentAttributeVideo(duration=3, w=320, h=240)])
    if kind == "sticker":
        return _raw_document(n, "image/webp", [
            raw.types.DocumentAttributeSticker(alt="😀", stickerset=raw.types.InputStickerSetEmpty()),
            raw.types.DocumentAttributeImageSize(w=512, h=512)])
    if kind == "location":
        return raw.types.MessageMediaGeo(geo=raw.types.GeoPoint(long=77.2, lat=28.6, access_hash=1))
    if kind == "contact":
        return raw.types.MessageMediaContact(phone_number="+10000000000", first_name="Jane",
                                             last_name="Doe", vcard="", user_id=0)
    if kind == "poll":
        text = lambda value: raw.types.TextWithEntities(text=value, entities=[])  # noqa: E731
        return raw.types.MessageMediaPoll(poll=raw.types.Poll(
            id=1, question=text("Q?"), hash=0, answers=[
                raw.types.PollAnswer(text=text("A"), option=b"0"),
                raw.types.PollAnswer(text=text("B"), option=b"1")]),
            results=raw.types.PollResults())
    raise ValueError(kind)


async def tg_forward(ch, msg_id: int, when: datetime, text: str = "forwarded post", *,
                     origin: str = "channel", media: Optional[str] = None,
                     grouped_id: Optional[int] = None, out: bool = False,
                     alive: bool = True):
    """A FORWARDED channel post as Kurigram parses it from Telegram's raw
    update (``fwd_from`` set). ``origin`` picks the forward header:

      user         -> from_id = PeerUser (a known account)
      hidden_user  -> from_name only (the account hides itself)
      channel      -> from_id = PeerChannel + channel_post (another channel)
      own_channel  -> the same, but THIS channel re-forwarding its own post
      chat         -> from_id = PeerChannel of a supergroup (anonymous admin)
      import       -> imported from a foreign chat service
      bare         -> a header Kurigram cannot turn into a MessageOrigin
                      (``forward_origin`` stays None)
    """
    channel_raw_id = utils.get_raw_peer_id(ch.chat_id)
    chats = {channel_raw_id: _raw_channel(channel_raw_id, "Test channel")}
    users = {}
    sent = int((when - timedelta(days=1)).timestamp())
    if origin == "user":
        header = raw.types.MessageFwdHeader(date=sent, from_id=raw.types.PeerUser(user_id=ORIGINAL_USER))
        users[ORIGINAL_USER] = _raw_user(ORIGINAL_USER)
    elif origin == "hidden_user":
        header = raw.types.MessageFwdHeader(date=sent, from_name="Hidden Sender")
    elif origin == "channel":
        source = other_channel_raw_id()
        chats[source] = _raw_channel(source, "Source channel")
        header = raw.types.MessageFwdHeader(date=sent, from_id=raw.types.PeerChannel(channel_id=source),
                                            channel_post=77)
    elif origin == "own_channel":
        header = raw.types.MessageFwdHeader(date=sent, from_id=raw.types.PeerChannel(channel_id=channel_raw_id),
                                            channel_post=3)
    elif origin == "chat":
        source = other_channel_raw_id()
        chats[source] = raw.types.Channel(id=source, title="Group", photo=raw.types.ChatPhotoEmpty(), date=0,
                                          megagroup=True, access_hash=source * 7, usernames=[],
                                          restriction_reason=[])
        header = raw.types.MessageFwdHeader(date=sent, from_id=raw.types.PeerChannel(channel_id=source))
    elif origin == "import":
        header = raw.types.MessageFwdHeader(date=sent, imported=True, post_author="Imported Author")
    elif origin == "bare":
        header = raw.types.MessageFwdHeader(date=sent)
    else:
        raise ValueError(origin)
    raw_message = raw.types.Message(
        id=msg_id, peer_id=raw.types.PeerChannel(channel_id=channel_raw_id),
        date=int(when.timestamp()), message=text, post=True, out=out, fwd_from=header,
        media=_media(media, msg_id), grouped_id=grouped_id, entities=[], restriction_reason=[],
    )
    if alive:
        ch.messages[msg_id] = when
    return await types.Message._parse(app, raw_message, users, chats, replies=0)


async def tg_plain(ch, msg_id: int, when: datetime, text: str = "normal post", *,
                   media: Optional[str] = None, grouped_id: Optional[int] = None,
                   reply_to: Optional[int] = None, via_bot: bool = False):
    """A NON-forwarded channel post (optionally a reply / sent via a bot)."""
    channel_raw_id = utils.get_raw_peer_id(ch.chat_id)
    chats = {channel_raw_id: _raw_channel(channel_raw_id, "Test channel")}
    users = {}
    via_bot_id = None
    if via_bot:
        via_bot_id = 4_242_001
        users[via_bot_id] = raw.types.User(id=via_bot_id, first_name="Inline bot", bot=True,
                                           access_hash=1, usernames=[], restriction_reason=[])
    raw_message = raw.types.Message(
        id=msg_id, peer_id=raw.types.PeerChannel(channel_id=channel_raw_id),
        date=int(when.timestamp()), message=text, post=True, media=_media(media, msg_id),
        grouped_id=grouped_id, via_bot_id=via_bot_id, entities=[], restriction_reason=[],
        reply_to=raw.types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None,
    )
    ch.messages[msg_id] = when
    return await types.Message._parse(app, raw_message, users, chats, replies=0)


async def enable_forward(chat_id: int) -> None:
    assert await set_forward_filter_enabled(chat_id, True) in {"ENABLED", "NO_CHANGE"}


def clear_caches() -> None:
    """Simulate a restart's effect on the in-process state."""
    forward_policy._REJECTED.clear()
    link_policy._REJECTED.clear()
    anonymous_policy._REJECTED.clear()


@pytest.fixture
def quiet(monkeypatch):
    """No 120 s reply timer; activity logs captured."""
    timers = MagicMock()
    log = AsyncMock()
    monkeypatch.setattr(fwd_mod, "_delete_after", timers)
    monkeypatch.setattr(fwd_mod, "log_setforward_change", log)
    return SimpleNamespace(timers=timers, log=log)


def _private_msg(user_id: int, text: str = "/setforward"):
    msg = AsyncMock()
    msg.from_user = AsyncMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Admin"
    msg.chat = AsyncMock()
    msg.chat.id = user_id
    msg.chat.type = ChatType.PRIVATE
    msg.text = text
    msg.caption = None
    return msg


def _channel_obj(chat_id: int):
    chat = AsyncMock()
    chat.id = chat_id
    chat.type = ChatType.CHANNEL
    chat.title = "Test channel"
    chat.username = None
    return chat


def _member(status, *, can_delete: bool = True):
    member = AsyncMock()
    member.status = status
    member.privileges = MagicMock(can_delete_messages=can_delete) if status == ChatMemberStatus.ADMINISTRATOR else None
    return member


def _bot_admin_user(user_status):
    """get_chat_member: the bot is an admin with Delete Messages; the user
    has ``user_status`` (or the call raises when it is an exception)."""
    async def get_chat_member(chat_id, user_id):
        if user_id == app.id:
            return _member(ChatMemberStatus.ADMINISTRATOR)
        if isinstance(user_status, BaseException):
            raise user_status
        return _member(user_status)
    return AsyncMock(side_effect=get_chat_member)


def _help_plugins():
    """Import the Help and Start plugins (registers their handlers); same
    aiohttp fallback as tests/test_help_menu.py for offline environments."""
    import importlib.util
    import sys
    import types as pytypes

    if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
        stub = pytypes.ModuleType("aiohttp")
        stub.ClientSession = stub.ClientTimeout = object
        sys.modules["aiohttp"] = stub
    import SWAGGYMUSIC.plugins.bot.help as help_mod
    import SWAGGYMUSIC.plugins.bot.start as start_mod
    return help_mod, start_mod


def _query(user_id: int, data: str):
    chat = MagicMock(id=user_id, type=ChatType.PRIVATE)
    message = MagicMock(chat=chat, edit_text=AsyncMock(), delete=AsyncMock())
    user = MagicMock(id=user_id, first_name="Admin", last_name=None, username=None)
    return MagicMock(from_user=user, message=message, data=data, answer=AsyncMock())


# ---------------------------------------------------------------------------
# Detection (utils/forward_policy.py)
# ---------------------------------------------------------------------------

class TestDetection:
    @pytest.mark.parametrize("origin, kind, parsed", [
        ("user", "user", types.MessageOriginUser),
        ("hidden_user", "hidden_user", types.MessageOriginHiddenUser),
        ("channel", "channel", types.MessageOriginChannel),
        ("own_channel", "channel", types.MessageOriginChannel),
        ("chat", "chat", types.MessageOriginChat),
        ("import", "import", types.MessageOriginImport),
        ("bare", "unknown", type(None)),
    ])
    async def test_every_forward_origin_is_detected(self, channel, origin, kind, parsed):
        msg = await tg_forward(channel, 1, at(0), origin=origin)
        assert isinstance(msg.forward_origin, parsed)        # what Kurigram built
        assert forward_policy.is_forwarded(msg) is True
        assert forward_policy.forward_origin_kind(msg) == kind

    async def test_header_without_message_origin_is_still_a_forward(self, channel):
        # Kurigram's own filters.forwarded (bool(forward_origin)) would miss
        # this one; Telegram's raw fwd_from header is authoritative.
        msg = await tg_forward(channel, 2, at(0), origin="bare")
        assert msg.forward_origin is None
        assert msg.raw.fwd_from is not None
        assert forward_policy.is_forwarded(msg) is True

    @pytest.mark.parametrize("media", MEDIA_KINDS)
    async def test_every_media_kind_is_detected(self, channel, media):
        msg = await tg_forward(channel, 3, at(0), media=media)
        if media is not None:
            assert msg.media == MessageMediaType[media.upper()]
        assert forward_policy.is_forwarded(msg) is True

    @pytest.mark.parametrize("media", MEDIA_KINDS)
    async def test_normal_posts_are_not_forwards(self, channel, media):
        msg = await tg_plain(channel, 4, at(0), media=media)
        assert forward_policy.is_forwarded(msg) is False

    async def test_replies_and_via_bot_posts_are_not_forwards(self, channel):
        assert forward_policy.is_forwarded(await tg_plain(channel, 5, at(0), reply_to=4)) is False
        assert forward_policy.is_forwarded(await tg_plain(channel, 6, at(0), via_bot=True)) is False

    def test_service_messages_are_never_forwards(self):
        chat = types.Chat(id=-100777, type=ChatType.CHANNEL, client=app)
        pin = types.Message(id=7, chat=chat, service=MessageServiceType.PINNED_MESSAGE, client=app,
                            forward_origin=types.MessageOriginHiddenUser(sender_user_name="x"))
        assert forward_policy.is_forwarded(pin) is False

    def test_parsed_forward_origin_without_raw_update(self):
        chat = types.Chat(id=-100778, type=ChatType.CHANNEL, client=app)
        msg = types.Message(id=8, chat=chat, text="fwd", client=app,
                            forward_origin=types.MessageOriginHiddenUser(sender_user_name="Someone"))
        assert msg.raw is None
        assert forward_policy.is_forwarded(msg) is True

    def test_legacy_pyrogram_forward_fields(self):
        legacy = SimpleNamespace(service=None, raw=None, forward_origin=None,
                                 forward_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert forward_policy.is_forwarded(legacy) is True
        assert forward_policy.is_forwarded(SimpleNamespace(service=None, raw=None,
                                                           forward_origin=None)) is False

    async def test_kurigram_deprecated_forward_properties_are_never_read(self, channel, caplog):
        msg = await tg_forward(channel, 9, at(0), origin="user")
        plain = await tg_plain(channel, 10, at(0))
        with caplog.at_level(logging.WARNING):
            assert forward_policy.is_forwarded(msg) and not forward_policy.is_forwarded(plain)
            forward_policy.forward_origin_kind(msg)
        assert "deprecated" not in caplog.text

    def test_mock_messages_used_by_other_suites_are_not_forwards(self, channel):
        # MagicMock posts answer every attribute with a truthy mock: they
        # must never look forwarded (other suites dispatch them).
        assert forward_policy.is_forwarded(channel.new_post(11, at(0))) is False
        assert forward_policy.is_forwarded(None) is False

    def test_in_process_verdicts_are_bounded(self):
        forward_policy.mark_rejected(-100111, 5)
        assert forward_policy.was_rejected(-100111, 5)
        assert not forward_policy.was_rejected(-100111, 6)
        assert not forward_policy.was_rejected("x", None)
        for n in range(forward_policy._REJECTED_MAX + 50):
            forward_policy.mark_rejected(-100555, n)
        assert len(forward_policy._REJECTED) == forward_policy._REJECTED_MAX


# ---------------------------------------------------------------------------
# 1 — forwarded posts are deleted when enabled (real dispatch chain)
# ---------------------------------------------------------------------------

class TestDeletedWhenEnabled:
    @pytest.mark.parametrize("media", MEDIA_KINDS)
    async def test_forwarded_post_of_every_media_kind_is_deleted(self, channel, media):
        await enable_forward(channel.chat_id)
        msg = await tg_forward(channel, 21, at(0), media=media)
        ran = await dispatch(msg)
        assert "forward_post_remover" in ran
        assert not channel.alive(21)
        assert forward_policy.was_rejected(channel.chat_id, 21)

    @pytest.mark.parametrize("origin", ORIGINS)
    async def test_forwarded_post_from_every_origin_is_deleted(self, channel, origin):
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 22, at(0), origin=origin))
        assert not channel.alive(22)

    async def test_forwarded_album_is_deleted_whole(self, channel):
        await enable_forward(channel.chat_id)
        group = new_album_id()
        for mid, kind in ((23, "photo"), (24, "video"), (25, "document")):
            await dispatch(await tg_forward(channel, mid, at(0), "album" if mid == 23 else "",
                                            media=kind, grouped_id=group))
        assert not any(channel.alive(m) for m in (23, 24, 25))

    async def test_forwarded_command_lookalike_is_deleted_not_obeyed(self, channel):
        await enable_forward(channel.chat_id)
        ran = await dispatch(await tg_forward(channel, 26, at(0), "/setforward off"))
        assert "setforward_channel_handler" not in ran
        assert not channel.alive(26)
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True


# ---------------------------------------------------------------------------
# 2 — forwarded posts are kept when disabled
# ---------------------------------------------------------------------------

class TestKeptWhenDisabled:
    async def test_no_settings_document(self, channel):
        await dispatch(await tg_forward(channel, 31, at(0)))
        assert channel.alive(31)
        assert not forward_policy.was_rejected(channel.chat_id, 31)

    async def test_other_features_on_forward_remover_off(self, channel):
        await enable_delay(channel.chat_id, 60)
        await dispatch(await tg_forward(channel, 32, at(0), media="photo"))
        assert channel.alive(32)

    async def test_disabled_after_enabled_stops_deleting(self, channel):
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 33, at(0)))
        assert not channel.alive(33)
        assert await set_forward_filter_enabled(channel.chat_id, False) == "DISABLED"
        await dispatch(await tg_forward(channel, 34, at(0)))
        assert channel.alive(34)


# ---------------------------------------------------------------------------
# 3 — normal (non-forwarded) posts are never deleted
# ---------------------------------------------------------------------------

class TestNormalPostsUntouched:
    @pytest.mark.parametrize("media", MEDIA_KINDS)
    async def test_normal_post_kept_and_filter_never_runs(self, channel, media):
        await enable_forward(channel.chat_id)
        ran = await dispatch(await tg_plain(channel, 41, at(0), media=media))
        assert channel.alive(41)
        assert "forward_post_remover" not in ran, "only forwarded posts reach the filter"

    async def test_normal_album_reply_and_via_bot_posts_kept(self, channel):
        await enable_forward(channel.chat_id)
        group = new_album_id()
        for mid in (42, 43):
            await dispatch(await tg_plain(channel, mid, at(0), media="photo", grouped_id=group))
        await dispatch(await tg_plain(channel, 44, at(0), "a reply", reply_to=42))
        await dispatch(await tg_plain(channel, 45, at(0), "via bot", via_bot=True))
        assert all(channel.alive(m) for m in (42, 43, 44, 45))

    async def test_bots_own_outgoing_forward_is_kept(self, channel):
        await enable_forward(channel.chat_id)
        ran = await dispatch(await tg_forward(channel, 46, at(0), out=True))
        assert "forward_post_remover" not in ran
        assert channel.alive(46)

    async def test_edited_updates_and_service_messages_are_not_judged(self, channel):
        await enable_forward(channel.chat_id)
        edited = await tg_forward(channel, 47, at(0))
        edited.edit_date = at(5)
        await fwd_mod.forward_post_remover(app, edited)
        assert channel.alive(47)
        service = await tg_forward(channel, 48, at(0))
        service.service = MessageServiceType.PINNED_MESSAGE
        await fwd_mod.forward_post_remover(app, service)
        assert channel.alive(48)


# ---------------------------------------------------------------------------
# 4 — persistence (MongoDB) across restarts
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_enable_disable_no_change_transitions(self, channel):
        cid = channel.chat_id
        assert await set_forward_filter_enabled(cid, True) == "ENABLED"
        assert await set_forward_filter_enabled(cid, True) == "NO_CHANGE"
        assert await set_forward_filter_enabled(cid, False) == "DISABLED"
        assert await set_forward_filter_enabled(cid, False) == "NO_CHANGE"

    async def test_setting_survives_a_restart(self, channel):
        await enable_forward(channel.chat_id)
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        clear_caches()                     # a fresh process: only MongoDB remains
        await dispatch(await tg_forward(channel, 51, at(0)))
        assert not channel.alive(51), "the remover still deletes after a restart"

    async def test_disable_survives_a_restart(self, channel):
        await enable_forward(channel.chat_id)
        await set_forward_filter_enabled(channel.chat_id, False)
        clear_caches()
        await dispatch(await tg_forward(channel, 52, at(0)))
        assert channel.alive(52)

    async def test_disabling_another_feature_never_drops_the_setting(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig

        cid = channel.chat_id
        await enable_forward(cid)
        await set_link_filter_enabled(cid, True)
        assert await set_link_filter_enabled(cid, False) == "DISABLED"
        assert (await settings(cid)).get("forward_filter_enabled") is True
        await enable_gap(cid)
        assert await setconfig._do_disable("setgap", cid) is True
        assert (await settings(cid)).get("forward_filter_enabled") is True
        await dispatch(await tg_forward(channel, 53, at(0)))
        assert not channel.alive(53)

    async def test_disabling_keeps_other_features_and_cleans_up(self, channel):
        cid = channel.chat_id
        await enable_gap(cid)
        await enable_forward(cid)
        await set_forward_filter_enabled(cid, False)
        doc = await settings(cid)
        assert doc.get("gap_enabled") is True and "forward_filter_enabled" not in doc

        lone = cid - 1_000_000             # a channel where it is the only feature
        await enable_forward(lone)
        await set_forward_filter_enabled(lone, False)
        assert await settings(lone) == {}, "document removed with the last feature"


# ---------------------------------------------------------------------------
# 5 — unauthorized users cannot enable / disable it
# ---------------------------------------------------------------------------

class TestUnauthorized:
    async def test_wizard_channel_input_by_non_admin_is_rejected(self, channel):
        uid = 6101
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        app.get_chat = AsyncMock(return_value=_channel_obj(channel.chat_id))
        app.get_chat_member = _bot_admin_user(ChatMemberStatus.MEMBER)
        msg = _private_msg(uid, str(channel.chat_id))
        await fwd_mod.setforward_channel_input(app, msg)
        assert "not an admin" in msg.reply_text.await_args.args[0]
        assert uid not in fwd_mod.SESSIONS
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")

    async def test_admin_right_revoked_before_the_button_press(self, channel):
        uid = 6102
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        app.get_chat = AsyncMock(return_value=_channel_obj(channel.chat_id))
        app.get_chat_member = _bot_admin_user(ChatMemberStatus.ADMINISTRATOR)
        await fwd_mod.setforward_channel_input(app, _private_msg(uid, str(channel.chat_id)))
        assert fwd_mod.SESSIONS[uid]["channel_id"] == channel.chat_id

        app.get_chat_member = _bot_admin_user(RuntimeError("no longer an admin"))
        query = _query(uid, f"setforward:enable:{channel.chat_id}")
        await fwd_mod.setforward_callback(app, query)
        query.answer.assert_any_call("You cannot manage this channel.", show_alert=True)
        assert uid not in fwd_mod.SESSIONS
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")

    async def test_crafted_callback_without_a_session_is_rejected(self, channel):
        query = _query(6103, f"setforward:enable:{channel.chat_id}")
        await fwd_mod.setforward_callback(app, query)
        query.answer.assert_any_call("Setup expired. Use /setforward again.", show_alert=True)
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")

    async def test_crafted_callback_for_another_channel_is_rejected(self, channel):
        uid = 6104
        await enable_forward(channel.chat_id)
        fwd_mod.SESSIONS[uid] = {"step": "action", "channel_id": channel.chat_id - 1}
        try:
            query = _query(uid, f"setforward:disable:{channel.chat_id}")
            await fwd_mod.setforward_callback(app, query)
            query.answer.assert_any_call("Setup expired. Use /setforward again.", show_alert=True)
            assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        finally:
            fwd_mod.SESSIONS.pop(uid, None)

    async def test_channel_command_refused_without_bot_delete_rights(self, channel, quiet):
        app.get_chat_member = AsyncMock(return_value=_member(ChatMemberStatus.ADMINISTRATOR,
                                                             can_delete=False))
        msg = channel.new_post(61, at(0), "/setforward on")
        await fwd_mod.setforward_channel_handler(app, msg)
        assert "Delete Messages" in msg.reply_text.await_args.args[0]
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")

    async def test_forwarded_off_command_is_never_obeyed(self, channel, quiet):
        await enable_forward(channel.chat_id)
        await fwd_mod.setforward_channel_handler(
            app, await tg_forward(channel, 62, at(0), "/setforward off"))
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        quiet.log.assert_not_awaited()

    async def test_command_in_a_group_changes_nothing(self, channel):
        chat = types.Chat(id=channel.chat_id, type=ChatType.SUPERGROUP, title="Group", client=app)
        msg = types.Message(id=63, chat=chat, text="/setforward on", client=app,
                            from_user=types.User(id=6105, first_name="U", client=app))
        ran = await dispatch(msg)
        assert "setforward_channel_handler" not in ran
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")

    async def test_owner_bypasses_the_channel_admin_check(self, channel):
        owner = 1                                     # conftest: config.OWNER_ID
        await fwd_mod.setforward_private_start(app, _private_msg(owner))
        app.get_chat = AsyncMock(return_value=_channel_obj(channel.chat_id))
        app.get_chat_member = _bot_admin_user(ChatMemberStatus.MEMBER)   # owner is NOT an admin
        await fwd_mod.setforward_channel_input(app, _private_msg(owner, str(channel.chat_id)))
        await fwd_mod.setforward_callback(app, _query(owner, f"setforward:enable:{channel.chat_id}"))
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        assert owner not in fwd_mod.SESSIONS


# ---------------------------------------------------------------------------
# 6 — existing features keep working (coordination)
# ---------------------------------------------------------------------------

class TestCoordination:
    async def test_handler_order_for_a_forwarded_post(self, channel):
        await enable_forward(channel.chat_id)
        ran = await dispatch(await tg_forward(channel, 71, at(0)))
        expected = ["link_blocker_new_channel_post", "anonymous_post_filter",
                    "forward_post_remover", "autodelete_new_channel_post"]
        assert [name for name in ran if name in expected] == expected

    async def test_gap_never_counts_a_forward_deleted_post(self, channel, clock):
        await enable_gap(channel.chat_id, 15 * 60)
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 72, at(0)))
        assert not channel.alive(72)
        assert history_ids(await settings(channel.chat_id)) == []
        await dispatch(await tg_plain(channel, 73, at(5)))
        assert channel.alive(73), "the deleted forward never became the gap reference"
        assert history_ids(await settings(channel.chat_id)) == [73]

    async def test_no_setdelay_job_for_a_forward_deleted_post(self, channel, clock):
        await enable_delay(channel.chat_id, 60)
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 74, at(0), media="video"))
        assert not channel.alive(74)
        assert await job_count(channel.chat_id, 74) == 0

    async def test_setdelay_still_schedules_normal_posts(self, channel, clock):
        await enable_delay(channel.chat_id, 60)
        await enable_forward(channel.chat_id)
        await dispatch(await tg_plain(channel, 75, at(0)))
        assert channel.alive(75)
        assert await job_count(channel.chat_id, 75) == 1

    async def test_with_the_remover_off_a_forward_is_an_ordinary_post(self, channel, clock):
        await enable_gap(channel.chat_id, 15 * 60)
        await enable_delay(channel.chat_id, 60)
        await dispatch(await tg_forward(channel, 76, at(0)))
        assert channel.alive(76)
        assert history_ids(await settings(channel.chat_id)) == [76]
        assert await job_count(channel.chat_id, 76) == 1

    async def test_link_filter_owns_a_forward_with_a_link(self, channel):
        await set_link_filter_enabled(channel.chat_id, True)
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 77, at(0), "spam https://spam.com"))
        assert not channel.alive(77)
        assert channel.bot_deleted.count(77) == 1, "one delete call, not two"
        assert link_policy.was_rejected(channel.chat_id, 77)
        assert not forward_policy.was_rejected(channel.chat_id, 77)

    async def test_anonymous_filter_owns_an_anonymous_forward(self, channel):
        await enable_anonymous_filter(channel.chat_id)
        await enable_forward(channel.chat_id)
        await dispatch(await tg_forward(channel, 78, at(0)))   # posted as the channel
        assert not channel.alive(78)
        assert channel.bot_deleted.count(78) == 1
        assert anonymous_policy.was_rejected(channel.chat_id, 78)
        assert not forward_policy.was_rejected(channel.chat_id, 78)

    async def test_setforward_command_post_never_touches_the_gap(self, channel, clock, quiet):
        await enable_gap(channel.chat_id, 15 * 60)
        ran = await dispatch(await tg_plain(channel, 79, at(0), "/setforward on"))
        assert "setforward_channel_handler" in ran
        doc = await settings(channel.chat_id)
        assert doc["forward_filter_enabled"] is True and history_ids(doc) == []
        assert not channel.alive(79)                          # command auto-deleted
        await dispatch(await tg_plain(channel, 80, at(1)))
        assert channel.alive(80), "the command post never held the gap slot"

    async def test_existing_channel_commands_still_work(self, channel, quiet, monkeypatch):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        monkeypatch.setattr(ad, "_delete_after", AsyncMock())
        monkeypatch.setattr(ad, "_start_worker", AsyncMock())
        monkeypatch.setattr(setlink_mod, "_delete_after", MagicMock())
        await enable_forward(channel.chat_id)
        for mid, text in ((81, "/setgap 5m"), (82, "/setdelay 30s"), (83, "/setlink on")):
            await dispatch(await tg_plain(channel, mid, at(0), text))
        doc = await settings(channel.chat_id)
        assert doc["gap"] == 300 and doc["delay"] == 30 and doc["link_filter_enabled"] is True
        assert doc["forward_filter_enabled"] is True


# ---------------------------------------------------------------------------
# 7a — channel command
# ---------------------------------------------------------------------------

class TestChannelCommand:
    async def test_on_enables_cleans_up_and_auto_deletes_the_reply(self, channel, quiet):
        msg = channel.new_post(91, at(0), "/setforward on")
        await fwd_mod.setforward_channel_handler(app, msg)
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        assert msg.reply_text.await_args.args[0] == "Successfully enabled forward post remover!"
        msg.delete.assert_awaited()                                  # command removed
        assert quiet.timers.call_args.args[1] == 120                 # reply removed in 120 s
        quiet.log.assert_awaited_once()
        assert quiet.log.await_args.args[2] == "ENABLED"
        assert quiet.log.await_args.kwargs["source"] == "CHANNEL"

    async def test_off_disables(self, channel, quiet):
        await enable_forward(channel.chat_id)
        msg = channel.new_post(92, at(0), "/setforward off")
        await fwd_mod.setforward_channel_handler(app, msg)
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")
        assert msg.reply_text.await_args.args[0] == "Successfully disabled forward post remover!"
        assert quiet.log.await_args.args[2] == "DISABLED"

    @pytest.mark.parametrize("text", ["/setforward", "/setforward maybe", "/setforward yes please"])
    async def test_invalid_option_shows_status_and_usage(self, channel, quiet, text):
        await enable_forward(channel.chat_id)
        msg = channel.new_post(93, at(0), text)
        await fwd_mod.setforward_channel_handler(app, msg)
        reply = msg.reply_text.await_args.args[0]
        assert "Current status for this channel: ON." in reply
        assert "/setforward on" in reply and "/setforward off" in reply
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        msg.delete.assert_awaited()
        quiet.log.assert_not_awaited()

    @pytest.mark.parametrize("text", ["/setforward@stub_bot on", "/SETFORWARD ON", "/setforward  On "])
    async def test_bot_mention_and_case_variants(self, channel, quiet, text):
        ran = await dispatch(await tg_plain(channel, 94, at(0), text))
        assert "setforward_channel_handler" in ran
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True

    async def test_logged_only_on_a_real_change(self, channel, quiet):
        for mid in (95, 96):
            await fwd_mod.setforward_channel_handler(app, channel.new_post(mid, at(0), "/setforward on"))
        quiet.log.assert_awaited_once()

    async def test_lookalike_commands_are_not_matched(self, channel, quiet):
        for mid, text in ((97, "/setforwarding on"), (98, "/setforwardon"), (99, "please /setforward on")):
            ran = await dispatch(await tg_plain(channel, mid, at(0), text))
            assert "setforward_channel_handler" not in ran, text
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")


# ---------------------------------------------------------------------------
# 7b — private-chat wizard
# ---------------------------------------------------------------------------

class TestPrivateWizard:
    async def _to_menu(self, channel, uid):
        start = _private_msg(uid)
        await fwd_mod.setforward_private_start(app, start)
        assert "FORWARD POST REMOVER" in start.reply_text.await_args.args[0]
        app.get_chat = AsyncMock(return_value=_channel_obj(channel.chat_id))
        app.get_chat_member = AsyncMock(return_value=_member(ChatMemberStatus.OWNER))
        msg = _private_msg(uid, str(channel.chat_id))
        await fwd_mod.setforward_channel_input(app, msg)
        return msg

    async def test_enable_flow(self, channel, quiet):
        uid = 7101
        menu = await self._to_menu(channel, uid)
        assert "<b>Current status:</b> OFF 🔴" in menu.reply_text.await_args.args[0]
        buttons = [b.callback_data for row in menu.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
                   for b in row]
        assert buttons == [f"setforward:enable:{channel.chat_id}",
                           f"setforward:disable:{channel.chat_id}", "setforward:cancel"]
        assert all(len(data.encode()) <= 64 for data in buttons)
        query = _query(uid, f"setforward:enable:{channel.chat_id}")
        await fwd_mod.setforward_callback(app, query)
        assert (await settings(channel.chat_id))["forward_filter_enabled"] is True
        assert "FORWARD POST REMOVER ENABLED 🟢" in query.message.edit_text.await_args.args[0]
        assert uid not in fwd_mod.SESSIONS
        assert quiet.log.await_args.kwargs["source"] == "PRIVATE CHAT"

    async def test_disable_flow(self, channel, quiet):
        uid = 7102
        await enable_forward(channel.chat_id)
        menu = await self._to_menu(channel, uid)
        assert "<b>Current status:</b> ON 🟢" in menu.reply_text.await_args.args[0]
        query = _query(uid, f"setforward:disable:{channel.chat_id}")
        await fwd_mod.setforward_callback(app, query)
        assert not (await settings(channel.chat_id)).get("forward_filter_enabled")
        assert "FORWARD POST REMOVER DISABLED 🔴" in query.message.edit_text.await_args.args[0]

    async def test_cancel_clears_the_session(self, channel):
        uid = 7103
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        await fwd_mod.setforward_callback(app, _query(uid, "setforward:cancel"))
        assert uid not in fwd_mod.SESSIONS

    async def test_invalid_channel_id_reprompts(self, channel):
        uid = 7104
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        try:
            msg = _private_msg(uid, "not-a-number")
            await fwd_mod.setforward_channel_input(app, msg)
            assert "Invalid Channel ID" in msg.reply_text.await_args.args[0]
            assert uid in fwd_mod.SESSIONS                 # kept for a retry
        finally:
            fwd_mod.SESSIONS.pop(uid, None)

    async def test_another_command_leaves_the_wizard_and_reaches_its_handler(self, channel, monkeypatch):
        uid = 7105
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        with pytest.raises(ContinuePropagation):
            await fwd_mod.setforward_channel_input(app, _private_msg(uid, "/setlink"))
        assert uid not in fwd_mod.SESSIONS

        # Production loads plugins in sorted order, so in handler group 0
        # this wizard's input handler is registered BEFORE /setlink's (and
        # /logger's) command handlers — the case the pass-through exists for.
        from SWAGGYMUSIC.plugins import ALL_MODULES
        assert ALL_MODULES.index(".misc.setforward") < ALL_MODULES.index(".misc.setlink")

        # Through the real dispatcher: /setlink still opens ITS wizard. (The
        # test session may register the plugins in another order: then
        # /setlink's handler simply sees the command first.)
        monkeypatch.setattr(app, "me", types.User(id=app.id, first_name="Bot", username="stub_bot",
                                                  is_bot=True, client=app))
        await fwd_mod.setforward_private_start(app, _private_msg(uid))
        chat = types.Chat(id=uid, type=ChatType.PRIVATE, client=app)
        command = types.Message(id=5, chat=chat, text="/setlink", client=app,
                                from_user=types.User(id=uid, first_name="Admin", client=app))
        try:
            ran = await dispatch(command)
            assert "setlink_private_start" in ran
            assert uid in setlink_mod.SESSIONS
            if "setforward_channel_input" in ran:          # production order
                assert ran.index("setforward_channel_input") < ran.index("setlink_private_start")
                assert uid not in fwd_mod.SESSIONS
        finally:
            setlink_mod.SESSIONS.pop(uid, None)
            fwd_mod.SESSIONS.pop(uid, None)


# ---------------------------------------------------------------------------
# 7c — FloodWait / missing rights
# ---------------------------------------------------------------------------

class TestDeletionFailures:
    async def test_floodwait_queues_a_persistent_retry(self, channel, monkeypatch):
        await enable_forward(channel.chat_id)
        channel.delete_errors = [FloodWait(value=30)]
        before = int(time.time())
        await dispatch(await tg_forward(channel, 101, at(0), media="photo"))
        assert channel.alive(101)                          # Telegram refused for now
        job = await jobs_db.find_one({"chat_id": channel.chat_id, "message_id": 101})
        assert job["reason"] == "FORWARD_REJECT_RETRY"
        assert before + 30 <= job["delete_at"] <= int(time.time()) + 30
        assert forward_policy.was_rejected(channel.chat_id, 101)

        # The existing /setdelay worker performs the retry.
        await jobs_db.update_one({"_id": job["_id"]}, {"$set": {"delete_at": int(time.time()) - 1}})

        async def gone():
            return not channel.alive(101)
        await run_worker_until(monkeypatch, gone)
        assert await job_count(channel.chat_id, 101) == 0

    async def test_without_delete_rights_the_post_is_kept_but_never_counted(self, channel, clock, caplog):
        await enable_gap(channel.chat_id, 15 * 60)
        await enable_forward(channel.chat_id)
        app.get_chat_member = AsyncMock(return_value=_member(ChatMemberStatus.ADMINISTRATOR,
                                                             can_delete=False))
        with caplog.at_level(logging.WARNING):
            await fwd_mod.forward_post_remover(app, await tg_forward(channel, 102, at(0)))
        assert channel.alive(102)
        assert "no Delete Messages right" in caplog.text
        assert forward_policy.was_rejected(channel.chat_id, 102)


# ---------------------------------------------------------------------------
# Handler registration / conflicts
# ---------------------------------------------------------------------------

class TestRegistration:
    async def test_groups_are_unique_and_ordered(self):
        groups = await registered_groups()
        names = {g: [h.callback.__name__ for h in hs] for g, hs in groups.items()}
        assert names[-25] == ["forward_post_remover"]
        assert names[-17] == ["setforward_channel_handler"]
        assert names[-40][0] == "link_blocker_new_channel_post"
        assert names[-30] == ["anonymous_post_filter"]
        assert -40 < -30 < -25 < -20 < -19 < -18 < -17 < 0

    async def test_wizard_callbacks_never_hit_other_handlers(self):
        _help_plugins()   # their "close" / "help_callback" regexes are unanchored
        for data in ("setforward:cancel", "setforward:enable:-1001234567890",
                     "setforward:disable:-1001234567890"):
            for unsafe in ("close", "help_callback", "settings_back_helper", "settingsback_helper"):
                assert unsafe not in data       # those handlers use unanchored regexes
            chat = types.Chat(id=1, type=ChatType.PRIVATE, client=app)
            query = types.CallbackQuery(
                client=app, id="q", chat_instance="ci", data=data,
                from_user=types.User(id=8101, first_name="U", client=app),
                message=types.Message(id=1, chat=chat, client=app))
            assert await dispatch_callback(query) == [fwd_mod.setforward_callback], data
