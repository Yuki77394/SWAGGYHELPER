"""/setlink (ALL-LINK BLOCKER) handler tests.

Covers the ten required scenarios end to end with REAL Kurigram messages
(raw MTProto updates parsed by ``Message._parse``, exactly like a live
update — see tests/gap_harness.py):

  1. plain @username allowed          6. albums deleted whole
  2. direct URLs blocked              7. disabled feature deletes nothing
  3. hidden URLs (TEXT_LINK) blocked  8. unauthorized commands rejected
  4. Telegram invite links blocked    9. settings persist across restarts
  5. captions + edited posts blocked 10. existing features remain functional

Telegram calls are faked by the ``channel`` fixture (FakeBotChannel) and
the DB is mongomock-motor (conftest.py).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest
from gap_harness import (FakeBotChannel, at, enable_delay, enable_gap,
                         job_count, new_album_id, settings, tg_post)
from pyrogram import types
from pyrogram.enums import MessageEntityType

import SWAGGYMUSIC.plugins.misc.setlink as setlink_mod  # noqa: F401 (registers handlers)
from SWAGGYMUSIC import app
from SWAGGYMUSIC.utils import link_policy
from SWAGGYMUSIC.utils.database import (set_link_filter_enabled,
                                        settings_db)

# (pytest.ini: asyncio_mode = auto — async tests need no marker)

TEXT_LINK = MessageEntityType.TEXT_LINK
URL = MessageEntityType.URL
MENTION = MessageEntityType.MENTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def enable_link(chat_id: int) -> None:
    status = await set_link_filter_enabled(chat_id, True)
    assert status in {"ENABLED", "NO_CHANGE"}


async def disable_link(chat_id: int) -> None:
    await set_link_filter_enabled(chat_id, False)


async def post(ch: FakeBotChannel, msg_id: int, when, text: str = "post", *,
               media: Optional[str] = None, grouped_id: Optional[int] = None,
               entities: Optional[List[types.MessageEntity]] = None,
               caption_entities: Optional[List[types.MessageEntity]] = None,
               alive: bool = True):
    """A real channel post, optionally carrying real entity objects."""
    msg = await tg_post(ch, msg_id, when, text, media=media,
                        grouped_id=grouped_id, alive=alive)
    if entities is not None:
        msg.entities = list(entities)
    if caption_entities is not None:
        msg.caption_entities = list(caption_entities)
    return msg


def hidden(text_len: int, url: str, offset: int = 0):
    return [types.MessageEntity(type=TEXT_LINK, offset=offset,
                                length=text_len, url=url)]


async def dispatch_post(ch: FakeBotChannel, msg_id: int, when, text: str = "post", **kw):
    """Deliver through the REAL registered handler chain (all groups)."""
    from gap_harness import dispatch

    msg = await post(ch, msg_id, when, text, **kw)
    ran = await dispatch(msg)
    return msg, ran


def clear_caches() -> None:
    """Simulate a restart's effect on the in-process state."""
    link_policy._REJECTED.clear()
    link_policy._ALBUM_ITEMS.clear()
    link_policy._BLOCKED_ALBUMS.clear()


# ---------------------------------------------------------------------------
# 1+2+3+4 — verdicts on ordinary posts (through the real dispatch chain)
# ---------------------------------------------------------------------------

class TestVerdictsThroughDispatch:
    async def test_plain_username_is_allowed(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        msg, ran = await dispatch_post(ch, 11, when, "follow @ABHISHEK77394")
        assert ch.alive(11), "plain @username must NEVER be deleted"
        assert "link_blocker_new_channel_post" in ran
        assert not link_policy.was_rejected(ch.chat_id, 11)

    async def test_plain_username_with_mention_entity_is_allowed(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        entity = types.MessageEntity(type=MENTION, offset=7, length=14)
        await dispatch_post(ch, 12, when, "follow @ABHISHEK77394",
                            entities=[entity])
        assert ch.alive(12)

    async def test_direct_url_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 13, when, "check https://spam.com/x now")
        assert not ch.alive(13)
        assert link_policy.was_rejected(ch.chat_id, 13)

    async def test_bare_recognizable_domain_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 14, when, "visit instagram.com/user today")
        assert not ch.alive(14)

    async def test_hidden_url_behind_username_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 15, when, "@ABHISHEK77394",
                            entities=hidden(14, "https://spam.com"))
        assert not ch.alive(15), "a username hiding a clickable URL is deleted"

    async def test_hidden_url_behind_words_and_emojis_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 16, when, "🎉 Join us 🎉",
                            entities=hidden(11, "http://spam.io",
                                            offset=2))  # UTF-16 offsets
        assert not ch.alive(16)

    async def test_telegram_invite_link_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 17, when, "join t.me/+AbCdEf12345")
        assert not ch.alive(17)

    async def test_tg_deep_link_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 18, when, "tg://join?invite=AbCdEf")
        assert not ch.alive(18)


# ---------------------------------------------------------------------------
# 5 — captions and edited posts
# ---------------------------------------------------------------------------

class TestCaptionsAndEdits:
    async def test_url_in_a_media_caption_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 21, when, "sale at https://spam.com",
                            media="photo")
        assert not ch.alive(21)

    async def test_hidden_url_in_a_caption_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        caption_entities = [types.MessageEntity(type=TEXT_LINK, offset=0,
                                                length=5, url="https://spam.com")]
        await dispatch_post(ch, 22, when, "click", media="photo",
                            caption_entities=caption_entities)
        assert not ch.alive(22)

    async def test_edited_post_with_a_new_link_is_deleted(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        # A clean post first…
        clean = await post(ch, 23, when, "totally innocent text")
        await setlink_mod.link_blocker_new_channel_post(app, clean)
        assert ch.alive(23)
        # …then the edit smuggles a link in — the edit is deleted. The
        # edited update is a MagicMock post (ch.edited), so the deletion
        # is observed on the message's own delete coroutine.
        edited = ch.edited(23)
        edited.text = "now with https://spam.com inside"
        await setlink_mod.link_blocker_edited_channel_post(app, edited)
        edited.delete.assert_awaited_once()  # edits can never bypass the filter

    async def test_edit_of_a_clean_post_stays_alive(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        clean = await post(ch, 24, when, "first version @ABHISHEK77394")
        await setlink_mod.link_blocker_new_channel_post(app, clean)
        edited = ch.edited(24)
        edited.text = "second version, still no links"
        await setlink_mod.link_blocker_edited_channel_post(app, edited)
        edited.delete.assert_not_awaited()

    async def test_forwarded_style_post_with_link_is_deleted(self, channel):
        # Forwarded posts arrive as ordinary channel posts carrying their
        # text/caption — no special casing, but pinned here explicitly.
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 25, when, "forwarded: t.me/spamchannel")
        assert not ch.alive(25)


# ---------------------------------------------------------------------------
# 6 — albums deleted whole
# ---------------------------------------------------------------------------

class TestAlbums:
    async def test_album_with_a_link_is_deleted_whole(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        group = new_album_id()
        # Item 1 (clean) is delivered first and kept for the moment…
        await dispatch_post(ch, 31, when, "album", media="photo",
                            grouped_id=group)
        assert ch.alive(31)
        # …item 2 carries the link and condemns the album…
        await dispatch_post(ch, 32, when, "look at https://spam.com",
                            media="video", grouped_id=group)
        # …item 3 is clean but must follow the verdict.
        await dispatch_post(ch, 33, when, "", media="photo", grouped_id=group)
        assert not ch.alive(31) and not ch.alive(32) and not ch.alive(33), \
            "never intentionally leave a partial album"

    async def test_link_item_deleted_and_earlier_items_taken_down(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        group = new_album_id()
        await dispatch_post(ch, 34, when, "one", media="photo", grouped_id=group)
        await dispatch_post(ch, 35, when, "two", media="photo", grouped_id=group)
        await dispatch_post(ch, 36, when, "see https://spam.com",
                            media="photo", grouped_id=group)
        assert not any(ch.alive(m) for m in (34, 35, 36))

    async def test_album_without_links_is_kept(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        group = new_album_id()
        for i, mid in enumerate((37, 38, 39)):
            await dispatch_post(ch, mid, when, f"item {i}", media="photo",
                                grouped_id=group)
        assert all(ch.alive(m) for m in (37, 38, 39))

    async def test_condemned_album_persists_across_a_restart(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        group = new_album_id()
        await dispatch_post(ch, 41, when, "one", media="photo", grouped_id=group)
        await dispatch_post(ch, 42, when, "https://spam.com", media="photo",
                            grouped_id=group)
        assert not ch.alive(41) and not ch.alive(42)

        # A restart wipes every in-process registry…
        clear_caches()
        assert not link_policy.album_is_blocked(ch.chat_id, group)
        # …but the verdict was persisted, so a straggler item delivered
        # after the restart is still deleted (no partial album).
        doc = await settings(ch.chat_id)
        assert group in (doc.get("link_blocked_albums") or [])
        await dispatch_post(ch, 43, when, "", media="photo", grouped_id=group)
        assert not ch.alive(43)

    async def test_album_rule_not_active_when_feature_disabled(self, channel):
        ch, when = channel, at(0)
        group = new_album_id()
        await dispatch_post(ch, 44, when, "https://spam.com", media="photo",
                            grouped_id=group)
        assert ch.alive(44)


# ---------------------------------------------------------------------------
# 7 — disabled feature deletes nothing
# ---------------------------------------------------------------------------

class TestDisabledFeature:
    async def test_no_deletion_and_no_state_when_disabled(self, channel):
        ch, when = channel, at(0)
        # No settings document at all.
        msg, ran = await dispatch_post(ch, 51, when, "https://spam.com")
        assert ch.alive(51)
        assert not link_policy.was_rejected(ch.chat_id, 51)

    async def test_disabled_after_enabled_stops_deleting(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 52, when, "https://spam.com")
        assert not ch.alive(52)
        await disable_link(ch.chat_id)
        await dispatch_post(ch, 53, when, "https://spam.com")
        assert ch.alive(53)

    async def test_clean_album_tracking_is_off_when_disabled(self, channel):
        ch, when = channel, at(0)
        group = new_album_id()
        await dispatch_post(ch, 54, when, "one", media="photo", grouped_id=group)
        assert link_policy.take_album_items(ch.chat_id, group) == []


# ---------------------------------------------------------------------------
# 8 — unauthorized commands are rejected
# ---------------------------------------------------------------------------

class TestUnauthorized:
    def _admin_member(self):
        from pyrogram.enums import ChatMemberStatus

        member = AsyncMock()
        member.status = ChatMemberStatus.ADMINISTRATOR
        member.privileges.can_delete_messages = True
        return member

    def _plain_member(self):
        from pyrogram.enums import ChatMemberStatus

        member = AsyncMock()
        member.status = ChatMemberStatus.MEMBER
        member.privileges = None
        return member

    async def test_wizard_enable_by_non_admin_is_rejected(self, channel, cb_factory):
        ch = channel
        user_id = 4242
        await setlink_mod.setlink_private_start(app, _private_msg(user_id))
        # The bot holds Delete Messages and the user IS an admin when the
        # Channel ID is submitted…
        app.get_chat = AsyncMock(return_value=_channel_obj(ch.chat_id))

        async def admin_for_user(chat_id, user_id):
            if user_id == app.id:
                return self._admin_member()
            return self._admin_member()
        app.get_chat_member = AsyncMock(side_effect=admin_for_user)

        msg = _private_msg(user_id, text=str(ch.chat_id))
        await setlink_mod.setlink_channel_input(app, msg)
        assert setlink_mod.SESSIONS[user_id]["channel_id"] == ch.chat_id

        # …but the admin right is REVOKED before the button press: the
        # re-verification refuses, the session is cleared, no DB write.
        async def revoked_for_user(chat_id, user_id):
            if user_id == app.id:
                return self._admin_member()
            raise RuntimeError("user is no longer an admin")
        app.get_chat_member = AsyncMock(side_effect=revoked_for_user)

        query = cb_factory(user_id=user_id,
                           data=f"setlink:enable:{ch.chat_id}")
        await setlink_mod.setlink_callback(app, query)
        assert user_id not in setlink_mod.SESSIONS, "session cleared"
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")

    async def test_crafted_callback_without_session_is_rejected(self, channel, cb_factory):
        ch = channel
        query = cb_factory(user_id=4343, data=f"setlink:enable:{ch.chat_id}")
        await setlink_mod.setlink_callback(app, query)
        query.answer.assert_any_call("Setup expired. Use /setlink again.", show_alert=True)
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")

    async def test_channel_command_refused_without_bot_delete_rights(self, channel):
        ch = channel
        from pyrogram.enums import ChatMemberStatus

        member = AsyncMock()
        member.status = ChatMemberStatus.ADMINISTRATOR
        member.privileges.can_delete_messages = False
        app.get_chat_member = AsyncMock(return_value=member)

        msg = ch.new_post(61, at(0), "/setlink on")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg)
        msg.reply_text.assert_awaited_once()
        assert "Delete Messages" in msg.reply_text.await_args.args[0]
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")

    async def test_wizard_channel_input_by_non_admin_rejected(self, channel):
        ch = channel
        user_id = 4545
        await setlink_mod.setlink_private_start(app, _private_msg(user_id))
        app.get_chat = AsyncMock(return_value=_channel_obj(ch.chat_id))

        async def get_chat_member(chat_id, user_id):
            if user_id == app.id:
                return self._admin_member()
            return self._plain_member()          # the user is NOT an admin
        app.get_chat_member = AsyncMock(side_effect=get_chat_member)

        await setlink_mod.setlink_channel_input(app, _private_msg(user_id, text=str(ch.chat_id)))
        assert user_id not in setlink_mod.SESSIONS
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")


# ---------------------------------------------------------------------------
# 9 — settings persist across restarts
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_setting_lives_in_mongodb_not_in_memory(self, channel):
        ch, when = channel, at(0)
        await enable_link(ch.chat_id)
        doc = await settings(ch.chat_id)
        assert doc.get("link_filter_enabled") is True

        # Simulate a restart: in-process state is gone, a fresh process
        # reads the setting from MongoDB.
        clear_caches()
        assert not link_policy.album_is_blocked(ch.chat_id, 1)
        assert not link_policy.was_rejected(ch.chat_id, 1)
        await dispatch_post(ch, 71, when, "https://spam.com")
        assert not ch.alive(71), "the feature still deletes after a restart"

    async def test_disable_survives_and_cleans_up(self, channel):
        ch = channel
        await enable_gap(ch.chat_id, 15 * 60)
        await enable_link(ch.chat_id)
        status = await set_link_filter_enabled(ch.chat_id, False)
        assert status == "DISABLED"
        doc = await settings(ch.chat_id)
        # gap still on, link fields gone
        assert doc.get("gap_enabled") is True
        assert "link_filter_enabled" not in doc
        assert "link_blocked_albums" not in doc

    async def test_last_feature_leaving_removes_the_document(self, channel):
        ch = channel
        await enable_link(ch.chat_id)
        assert await settings(ch.chat_id)
        await set_link_filter_enabled(ch.chat_id, False)
        assert await settings(ch.chat_id) == {}, \
            "document removed when /setlink was the only feature"

    async def test_enable_disable_no_change_transitions(self, channel):
        ch = channel
        assert await set_link_filter_enabled(ch.chat_id, True) == "ENABLED"
        assert await set_link_filter_enabled(ch.chat_id, True) == "NO_CHANGE"
        assert await set_link_filter_enabled(ch.chat_id, False) == "DISABLED"
        assert await set_link_filter_enabled(ch.chat_id, False) == "NO_CHANGE"


# ---------------------------------------------------------------------------
# 10 — existing features remain functional (coordination)
# ---------------------------------------------------------------------------

class TestExistingFeaturesUnaffected:
    async def test_gap_does_not_count_link_deleted_posts(self, channel, clock):
        ch = channel
        await enable_gap(ch.chat_id, 15 * 60)
        await enable_link(ch.chat_id)
        # 10:00 — a link post: deleted by the link filter…
        await dispatch_post(ch, 81, at(0), "https://spam.com")
        assert not ch.alive(81)
        # …10:05 — an ordinary post is ALLOWED: the deleted link post must
        # never become the gap reference.
        await dispatch_post(ch, 82, at(5), "normal post")
        assert ch.alive(82), "gap/delay skipped chat msg: rejected by link filter"

    async def test_no_deletion_job_for_link_posts(self, channel, clock):
        ch = channel
        await enable_delay(ch.chat_id, 60)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 83, at(0), "https://spam.com")
        assert not ch.alive(83)
        assert await job_count(ch.chat_id, 83) == 0, \
            "a link-deleted post is never queued for /setdelay"

    async def test_delay_still_schedules_normal_posts(self, channel, clock):
        ch = channel
        await enable_delay(ch.chat_id, 60)
        await enable_link(ch.chat_id)
        await dispatch_post(ch, 84, at(0), "normal post @ABHISHEK77394")
        assert ch.alive(84)
        assert await job_count(ch.chat_id, 84) == 1

    async def test_setlink_command_post_never_touches_the_gap(self, channel, clock):
        ch = channel
        await enable_gap(ch.chat_id, 15 * 60)
        await enable_link(ch.chat_id)
        msg = ch.new_post(85, at(0), "/setlink off")
        await app_calls(setlink_mod.setlink_channel_handler, msg)
        doc = await settings(ch.chat_id)
        assert doc.get("gap_enabled") is True
        assert not doc.get("gap_history")
        # the engine refuses command posts the same way it refuses
        # /setdelay and /setgap commands
        engine = ch.new_post(85, at(0), "/setlink on")
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        await ad.autodelete_new_channel_post(app, engine)
        doc = await settings(ch.chat_id)
        assert not doc.get("gap_history")
        assert ch.alive(85)

    async def test_anonymous_filter_still_runs_after_link_filter(self, channel):
        # The anonymous feature is untouched: with it enabled and the link
        # feature OFF, an unsigned anonymous post is still deleted (its own
        # suite pins the full matrix; this is the smoke check).
        from gap_harness import enable_anonymous_filter, real_post

        ch = channel
        await enable_anonymous_filter(ch.chat_id)
        msg = real_post(ch.chat_id, 86, at(0), "unsigned anonymous post")
        await setlink_mod.link_blocker_new_channel_post(app, msg)  # link off: no-op
        from SWAGGYMUSIC.plugins.misc import anonymous as anon_mod
        await anon_mod.anonymous_post_filter(app, msg)
        assert not ch.alive(86)


# ---------------------------------------------------------------------------
# Channel-side /setlink command behaviour
# ---------------------------------------------------------------------------

class TestChannelCommand:
    async def test_setlink_on_enables_and_cleans_up(self, channel):
        ch = channel
        msg = ch.new_post(91, at(0), "/setlink on")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg)
        doc = await settings(ch.chat_id)
        assert doc.get("link_filter_enabled") is True
        msg.reply_text.assert_awaited_once()
        assert "Successfully enabled link restriction!" in msg.reply_text.await_args.args[0]
        msg.delete.assert_awaited()                      # command auto-deleted

    async def test_setlink_off_disables(self, channel):
        ch = channel
        await enable_link(ch.chat_id)
        msg = ch.new_post(92, at(0), "/setlink off")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg)
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")
        assert "Successfully disabled link restriction!" in msg.reply_text.await_args.args[0]

    async def test_invalid_option_shows_status_and_usage(self, channel):
        ch = channel
        await enable_link(ch.chat_id)
        msg = ch.new_post(93, at(0), "/setlink maybe")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg)
        text = msg.reply_text.await_args.args[0]
        assert "Current status for this channel: ON." in text
        assert "/setlink on" in text and "/setlink off" in text
        doc = await settings(ch.chat_id)
        assert doc.get("link_filter_enabled") is True   # unchanged

    async def test_state_change_is_logged_only_on_transition(self, channel):
        ch = channel
        msg = ch.new_post(94, at(0), "/setlink on")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg)   # OFF -> ON
        msg2 = ch.new_post(95, at(1), "/setlink on")
        msg2.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_handler(app, msg2)  # ON -> ON
        doc = await settings(ch.chat_id)
        assert doc.get("link_filter_enabled") is True


# ---------------------------------------------------------------------------
# Private-chat wizard happy path
# ---------------------------------------------------------------------------

class TestPrivateWizard:
    async def test_full_flow(self, channel, cb_factory):
        ch = channel
        user_id = 5151
        start_msg = _private_msg(user_id)
        start_msg.reply_text = AsyncMock()
        await setlink_mod.setlink_private_start(app, start_msg)
        assert user_id in setlink_mod.SESSIONS
        assert "LINK RESTRICTION" in start_msg.reply_text.await_args.args[0]

        app.get_chat = AsyncMock(return_value=_channel_obj(ch.chat_id))
        app.get_chat_member = AsyncMock(return_value=_admin_member())

        input_msg = _private_msg(user_id, text=str(ch.chat_id))
        input_msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_input(app, input_msg)
        status_text = input_msg.reply_text.await_args.args[0]
        assert "<b>Current status:</b> OFF 🔴" in status_text

        # ENABLE pressed
        query = cb_factory(user_id=user_id,
                           data=f"setlink:enable:{ch.chat_id}")
        await setlink_mod.setlink_callback(app, query)
        doc = await settings(ch.chat_id)
        assert doc.get("link_filter_enabled") is True
        assert user_id not in setlink_mod.SESSIONS
        query.message.edit_text.assert_awaited()
        assert "LINK RESTRICTION ENABLED 🟢" in query.message.edit_text.await_args.args[0]

    async def test_disable_button_disables_an_enabled_channel(self, channel, cb_factory):
        ch = channel
        user_id = 5152
        await enable_link(ch.chat_id)
        await setlink_mod.setlink_private_start(app, _private_msg(user_id))
        app.get_chat = AsyncMock(return_value=_channel_obj(ch.chat_id))
        app.get_chat_member = AsyncMock(return_value=_admin_member())
        input_msg = _private_msg(user_id, text=str(ch.chat_id))
        input_msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_input(app, input_msg)
        assert "<b>Current status:</b> ON 🟢" in input_msg.reply_text.await_args.args[0]

        query = cb_factory(user_id=user_id,
                           data=f"setlink:disable:{ch.chat_id}")
        await setlink_mod.setlink_callback(app, query)
        doc = await settings(ch.chat_id)
        assert not doc.get("link_filter_enabled")
        assert "LINK RESTRICTION DISABLED 🔴" in query.message.edit_text.await_args.args[0]

    async def test_cancel_clears_the_session(self, channel, cb_factory):
        user_id = 5252
        await setlink_mod.setlink_private_start(app, _private_msg(user_id))
        assert user_id in setlink_mod.SESSIONS
        query = cb_factory(user_id=user_id, data="setlink:cancel")
        await setlink_mod.setlink_callback(app, query)
        assert user_id not in setlink_mod.SESSIONS

    async def test_invalid_channel_id_reprompts(self, channel):
        user_id = 5353
        await setlink_mod.setlink_private_start(app, _private_msg(user_id))
        msg = _private_msg(user_id, text="not-a-number")
        msg.reply_text = AsyncMock()
        await setlink_mod.setlink_channel_input(app, msg)
        assert "Invalid Channel ID" in msg.reply_text.await_args.args[0]
        assert user_id in setlink_mod.SESSIONS     # session kept for a retry


# ---------------------------------------------------------------------------
# Small local builders (private-chat messages / member mocks)
# ---------------------------------------------------------------------------

def _private_msg(user_id: int, text: str = "/setlink"):
    from pyrogram.enums import ChatType

    msg = AsyncMock()
    msg.from_user = AsyncMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Admin"
    chat = AsyncMock()
    chat.id = user_id
    chat.type = ChatType.PRIVATE
    msg.chat = chat
    msg.text = text
    msg.caption = None
    return msg


def _channel_obj(chat_id: int):
    from pyrogram.enums import ChatType

    chat = AsyncMock()
    chat.id = chat_id
    chat.type = ChatType.CHANNEL
    chat.title = "Test channel"
    return chat


def _admin_member():
    from pyrogram.enums import ChatMemberStatus

    member = AsyncMock()
    member.status = ChatMemberStatus.OWNER
    member.privileges = None
    return member


async def app_calls(handler, message):
    await handler(app, message)
