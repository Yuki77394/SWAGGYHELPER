"""/setanonymous: posts sent with ANOTHER channel's identity.

Every post here is a genuine MTProto update parsed by Kurigram itself
(gap_harness.tg_post), then delivered through the real handler chain
(anonymous filter group -30 -> channel commands -> gap/delay engine group 0).

Policy under test (SWAGGYMUSIC/utils/anonymous_policy.py):
  * personal account exposed by Telegram (from_id is a user) -> allowed;
  * sent with this channel's identity                        -> rejected;
  * sent with ANOTHER channel's / chat's identity            -> rejected;
  * no sender metadata at all                                -> rejected (fail closed);
  * the bot never claims which admin sent a chat-identity post.
"""
from __future__ import annotations

import logging

import pytest
from pyrogram.enums import MessageServiceType
from gap_harness import (
    at,
    dispatch,
    enable_anonymous_filter,
    enable_gap,
    history_ids,
    other_channel_raw_id,
    real_post,
    settings,
    tg_post,
)
from pyrogram import utils

pytestmark = pytest.mark.asyncio

ADMIN = 4242


def _policy():
    from SWAGGYMUSIC.utils import anonymous_policy

    return anonymous_policy


class TestIdentityClassification:
    async def test_genuine_updates_parsed_by_kurigram(self, channel):
        policy = _policy()
        own_raw = utils.get_raw_peer_id(channel.chat_id)
        other = other_channel_raw_id()

        own = await tg_post(channel, 1, at(0))
        own_explicit = await tg_post(channel, 2, at(0), sender_channel_id=own_raw)
        foreign = await tg_post(channel, 3, at(0), sender_channel_id=other)
        foreign_signed = await tg_post(channel, 4, at(0), sender_channel_id=other, post_author="Alice")
        personal = await tg_post(channel, 5, at(0), user_id=ADMIN)
        personal_signed = await tg_post(channel, 6, at(0), user_id=ADMIN, post_author="Alice")
        # from_id is the admin's account, but the update omitted the User object
        personal_unresolved = await tg_post(channel, 7, at(0), user_id=ADMIN + 1, user_in_update=False)

        # What Kurigram itself exposes for these updates:
        assert foreign.from_user is None
        assert foreign.sender_chat.id == utils.get_channel_id(other) != channel.chat_id
        assert personal_unresolved.from_user is None and personal_unresolved.sender_chat is None

        identity = policy.sender_identity
        assert identity(own) == policy.SENDER_OWN_CHANNEL
        assert identity(own_explicit) == policy.SENDER_OWN_CHANNEL
        assert identity(foreign) == policy.SENDER_OTHER_CHAT
        assert identity(foreign_signed) == policy.SENDER_OTHER_CHAT
        assert identity(personal) == policy.SENDER_PERSONAL
        assert identity(personal_signed) == policy.SENDER_PERSONAL
        assert identity(personal_unresolved) == policy.SENDER_PERSONAL

        rejected = policy.is_channel_identity_post
        assert [rejected(m) for m in (own, own_explicit, foreign, foreign_signed)] == [True] * 4
        assert [rejected(m) for m in (personal, personal_signed, personal_unresolved)] == [False] * 3
        strict = policy.should_reject_anonymous_post
        assert all(strict(m) for m in (own, own_explicit, foreign, foreign_signed))
        assert not any(strict(m) for m in (personal, personal_signed, personal_unresolved))

    async def test_parsed_fields_without_raw_update(self):
        """Messages built without the raw update (other code paths) fall back
        to Kurigram's parsed fields with the same policy."""
        policy = _policy()
        chat = -1001500
        assert policy.is_channel_identity_post(real_post(chat, 1, at(0))) is True
        assert policy.is_channel_identity_post(real_post(chat, 2, at(0), sender_chat_id=-1009990001)) is True
        assert policy.sender_identity(real_post(chat, 2, at(0), sender_chat_id=-1009990001)) == \
            policy.SENDER_OTHER_CHAT
        assert policy.is_channel_identity_post(real_post(chat, 3, at(0), personal_user_id=ADMIN)) is False
        unknown = real_post(chat, 4, at(0), sender_chat_id=None)
        assert policy.sender_identity(unknown) == policy.SENDER_UNKNOWN
        assert policy.is_channel_identity_post(unknown) is False
        assert policy.should_reject_anonymous_post(unknown) is True
        service = real_post(chat, 5, at(0), service=MessageServiceType.PINNED_MESSAGE)
        assert policy.should_reject_anonymous_post(service) is False


class TestOtherChannelIdentityEndToEnd:
    async def test_other_channel_identity_post_is_rejected_and_never_recorded(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        post = await tg_post(channel, 11, at(0), sender_channel_id=other_channel_raw_id())
        ran = await dispatch(post)
        # Group order: link filter (-40) first, anonymous filter (-30) next.
        assert ran[:2] == ["link_blocker_new_channel_post", "anonymous_post_filter"]
        assert not channel.alive(11)
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [] and "last_gap_post_msg_id" not in doc
        assert doc["anon_rejected_msg_ids"] == [11]
        # It never holds the gap: a personal post right after is allowed.
        assert await self._personal(channel, 12, 1) is True
        assert history_ids(await settings(channel.chat_id)) == [12]

    async def test_signed_other_channel_identity_post_is_rejected(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        post = await tg_post(channel, 21, at(0), sender_channel_id=other_channel_raw_id(),
                             post_author="Alice")
        await dispatch(post)
        assert not channel.alive(21)

    async def test_personal_posts_are_allowed_even_without_user_object(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        await dispatch(await tg_post(channel, 31, at(0), user_id=ADMIN))
        await dispatch(await tg_post(channel, 32, at(15), user_id=ADMIN + 7, user_in_update=False))
        assert channel.alive(31) and channel.alive(32)
        assert history_ids(await settings(channel.chat_id)) == [31, 32]

    async def test_unknown_sender_is_rejected_when_protection_is_enabled(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        post = channel.new_real_post(39, at(0), sender_chat_id=None)
        await dispatch(post)
        assert not channel.alive(39)
        doc = await settings(channel.chat_id)
        assert 39 in doc.get("anon_rejected_msg_ids", [])
        assert history_ids(doc) == []

    async def test_filter_off_keeps_other_channel_identity_posts(self, channel):
        await enable_gap(channel.chat_id)
        await dispatch(await tg_post(channel, 41, at(0), sender_channel_id=other_channel_raw_id()))
        assert channel.alive(41)
        assert history_ids(await settings(channel.chat_id)) == [41]

    async def test_bot_never_resolves_or_names_a_human_admin(self, channel, caplog):
        """Telegram does not reveal who is behind a chat-identity post: the
        filter makes no user lookups and logs only what Telegram provided."""
        from SWAGGYMUSIC import app

        await enable_anonymous_filter(channel.chat_id)
        other = other_channel_raw_id()
        caplog.set_level(logging.INFO)
        await dispatch(await tg_post(channel, 51, at(0), sender_channel_id=other, post_author="Alice"))
        assert not channel.alive(51)
        app.get_users.assert_not_awaited()
        for call in app.get_chat_member.await_args_list:
            assert call.args[1] == app.id, "only the bot's own rights are looked up"
        line = next(r.getMessage() for r in caplog.records if "anonymous post deleted" in r.getMessage())
        assert f"sender_chat={utils.get_channel_id(other)}" in line
        assert "identity=other_chat" in line
        assert "Alice" not in line, "a signature is not reported as the sender"

    @staticmethod
    async def _personal(channel, msg_id, minute):
        await dispatch(await tg_post(channel, msg_id, at(minute), user_id=ADMIN))
        return channel.alive(msg_id)
