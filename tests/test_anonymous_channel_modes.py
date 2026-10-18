"""/setanonymous in Telegram's three channel posting modes.

Telegram (core.telegram.org/api/channel) shows who posted a channel message
in one of three ways, chosen in Channel settings:

  * Anonymous (default)  — every post is published as the channel; Telegram
    reveals no author at all (no from_id, no signature).
  * Sign Messages        — posts carry Telegram's text signature with the
    posting admin's name (post_author). The admin posted from their personal
    account; Telegram shows the name but not the account.
  * Sign Messages + Show Authors' Profiles ("profiles") — each admin chooses,
    per post, to send as their own profile (from_id = the admin's account),
    as the channel itself, or as another channel they own.

REPORTED BUG — an admin posting from their personal account in a channel
with Sign Messages on (the post shows their name) had the post classified
"own_channel" and deleted: since round 2 the policy treated Telegram's
signature as meaningless text, unlike the original filter, which kept
signed posts. Every post here is a genuine MTProto update parsed by Kurigram
and delivered through the real handler chain.
"""
from __future__ import annotations

import itertools
import logging
from unittest.mock import AsyncMock

import pytest
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
from pyrogram import types, utils
from pyrogram.enums import ChatType

ADMIN = 4242
NAME = "Shiv Royal"    # the signature Telegram adds in Sign Messages mode


def _policy():
    from SWAGGYMUSIC.utils import anonymous_policy

    return anonymous_policy


async def _protected(channel):
    await enable_gap(channel.chat_id)
    await enable_anonymous_filter(channel.chat_id)


async def _post(channel, msg_id, minute, mode, sender, signed):
    """One post as Telegram delivers it in ``mode`` for the given sender choice."""
    kw = {"channel_mode": mode, "post_author": NAME if signed else None}
    if sender == "self":
        kw["user_id"] = ADMIN
    elif sender == "other_channel":
        kw["sender_channel_id"] = other_channel_raw_id()
    return await tg_post(channel, msg_id, at(minute), **kw)


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

class TestReportedBug:
    async def test_admin_post_signed_by_telegram_is_kept(self, channel):
        """Sign Messages on: the admin posts from their personal account,
        Telegram signs it with their name. It is not anonymous."""
        await _protected(channel)
        post = await _post(channel, 101, 0, "signatures", "channel", signed=True)
        assert post.from_user is None and post.sender_chat.id == channel.chat_id  # Kurigram view
        assert post.author_signature == NAME
        ran = await dispatch(post)
        assert ran[0] == "anonymous_post_filter"
        assert channel.alive(101), "a signed admin post must not be deleted"
        doc = await settings(channel.chat_id)
        assert 101 not in (doc.get("anon_rejected_msg_ids") or [])
        assert history_ids(doc) == [101], "/setgap judged and accepted it"

    async def test_signed_posts_are_still_judged_by_setgap(self, channel):
        await _protected(channel)
        await dispatch(await _post(channel, 111, 0, "signatures", "channel", signed=True))
        await dispatch(await _post(channel, 112, 5, "signatures", "channel", signed=True))
        assert channel.alive(111) and not channel.alive(112)      # 112: inside the 15m gap
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [111]
        assert 112 not in (doc.get("anon_rejected_msg_ids") or []), "deleted by /setgap, not as anonymous"

    async def test_unsigned_post_in_a_signed_channel_is_deleted(self, channel):
        """Sign Messages on, but the post carries no author: anonymous."""
        await _protected(channel)
        await dispatch(await _post(channel, 121, 0, "signatures", "channel", signed=False))
        assert not channel.alive(121)


# ---------------------------------------------------------------------------
# Every documented mode and sender choice
# ---------------------------------------------------------------------------

MATRIX = [
    # mode,         sender,          signed, expected identity,  kept
    ("anonymous",  "channel",        False, "own_channel",      False),
    ("signatures", "channel",        True,  "signed_admin",     True),
    ("signatures", "channel",        False, "own_channel",      False),
    ("profiles",   "self",           True,  "personal",         True),
    ("profiles",   "self",           False, "personal",         True),
    ("profiles",   "channel",        False, "own_channel",      False),
    # Admins could have posted as their profile: posting as the channel is
    # the anonymous choice, whatever Telegram puts in post_author.
    ("profiles",   "channel",        True,  "own_channel",      False),
    ("profiles",   "other_channel",  False, "other_chat",       False),
    ("profiles",   "other_channel",  True,  "other_chat",       False),
    # Channel flags missing from the update (min/unknown): the post's own
    # signature decides.
    (None,         "channel",        True,  "signed_admin",     True),
    (None,         "channel",        False, "own_channel",      False),
]


@pytest.mark.parametrize("mode, sender, signed, identity, kept", MATRIX)
async def test_mode_matrix(channel, mode, sender, signed, identity, kept):
    policy = _policy()
    await _protected(channel)
    post = await _post(channel, 201, 0, mode, sender, signed)
    assert policy.sender_identity(post) == identity
    assert policy.should_reject_anonymous_post(post) is (not kept)
    await dispatch(post)
    assert channel.alive(201) is kept
    doc = await settings(channel.chat_id)
    if kept:
        assert history_ids(doc) == [201]
    else:
        assert history_ids(doc) == [] and doc["anon_rejected_msg_ids"] == [201]


@pytest.mark.parametrize("mode, profiles", [
    ("anonymous", False), ("signatures", False), ("profiles", True),
])
async def test_posting_mode_is_read_from_the_update(channel, mode, profiles):
    policy = _policy()
    post = await _post(channel, 301, 0, mode, "channel", signed=False)
    assert policy.channel_posting_mode(post.chat) == mode
    assert post.chat.show_message_sender_name is profiles


def test_posting_mode_unknown_for_mocks_and_min_channels():
    from unittest.mock import MagicMock

    policy = _policy()
    assert policy.channel_posting_mode(None) is None
    assert policy.channel_posting_mode(MagicMock()) is None
    chat = types.Chat(id=-1001, type=ChatType.CHANNEL, sign_messages=True,
                      show_message_sender_name=False, is_min=True)
    assert policy.channel_posting_mode(chat) is None, "min constructors may omit flags"


# ---------------------------------------------------------------------------
# Messages without the raw update (other code paths)
# ---------------------------------------------------------------------------

def test_parsed_fields_fallback():
    policy = _policy()
    chat = -1001777
    signed = real_post(chat, 1, at(0), author_signature=NAME)
    unsigned = real_post(chat, 2, at(0))
    blank = real_post(chat, 3, at(0), author_signature="   ")
    assert policy.sender_identity(signed) == policy.SENDER_SIGNED_ADMIN
    assert policy.is_channel_identity_post(signed) is False
    assert policy.should_reject_anonymous_post(signed) is False
    assert policy.sender_identity(unsigned) == policy.SENDER_OWN_CHANNEL
    assert policy.sender_identity(blank) == policy.SENDER_OWN_CHANNEL, "blank text is no signature"


# ---------------------------------------------------------------------------
# Never identify the admin; explain every deletion
# ---------------------------------------------------------------------------

class TestNoGuessingAndClearLogs:
    async def test_signed_post_needs_no_lookup_of_the_admin(self, channel):
        from SWAGGYMUSIC import app

        await _protected(channel)
        await dispatch(await _post(channel, 401, 0, "signatures", "channel", signed=True))
        assert channel.alive(401)
        app.get_users.assert_not_awaited()
        for call in app.get_chat_member.await_args_list:
            assert call.args[1] == app.id, "only the bot's own rights are checked"

    async def test_deletion_log_states_mode_and_signature_but_never_the_name(self, channel, caplog):
        caplog.set_level(logging.INFO)
        await _protected(channel)
        await dispatch(await _post(channel, 411, 0, "profiles", "channel", signed=True))
        assert not channel.alive(411)
        line = next(r.getMessage() for r in caplog.records
                    if "anonymous post deleted" in r.getMessage())
        assert "identity=own_channel" in line
        assert "channel_mode=profiles" in line and "signed=yes" in line
        assert NAME not in line

    async def test_anonymous_mode_deletion_warns_how_to_fix(self, channel, caplog):
        caplog.set_level(logging.INFO)
        await _protected(channel)
        await dispatch(await _post(channel, 421, 0, "anonymous", "channel", signed=False))
        await dispatch(await _post(channel, 422, 20, "anonymous", "channel", signed=False))
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING and "Sign Messages is OFF" in r.getMessage()]
        assert len(warnings) == 1, "warned once per channel, not per post"
        assert str(channel.chat_id) in warnings[0]


# ---------------------------------------------------------------------------
# /setanonymous wizard: refuse a setup that would delete every post
# ---------------------------------------------------------------------------

_WIZARD_CHANNELS = itertools.count(1_800_000_100)   # unique channel per test case


def _wizard_query(user_id, data):
    from SWAGGYMUSIC import app

    chat = types.Chat(id=user_id, type=ChatType.PRIVATE, client=app)
    message = types.Message(id=77, chat=chat, client=app)
    return types.CallbackQuery(client=app, id="w1", from_user=types.User(id=user_id, first_name="U", client=app),
                               chat_instance="ci", message=message, data=data)


@pytest.fixture
def wizard(monkeypatch):
    from SWAGGYMUSIC import app
    from SWAGGYMUSIC.plugins.misc import anonymous

    monkeypatch.setattr(anonymous, "verify_channel_admin", AsyncMock(return_value=(True, None)))
    calls = {"edit": AsyncMock(), "answer": AsyncMock()}
    monkeypatch.setattr(app, "edit_message_text", calls["edit"])
    monkeypatch.setattr(app, "answer_callback_query", calls["answer"])

    def channel_settings(channel_id, mode):
        sign, profiles = {"anonymous": (False, False), "signatures": (True, False),
                          "profiles": (True, True), None: (None, None)}[mode]
        chat = types.Chat(id=channel_id, type=ChatType.CHANNEL, title="My channel",
                          sign_messages=sign, show_message_sender_name=profiles, client=app)
        monkeypatch.setattr(app, "get_chat", AsyncMock(return_value=chat))

    calls["channel_settings"] = channel_settings
    return calls


class TestEnableGuard:
    async def test_enable_refused_when_sign_messages_is_off(self, wizard):
        from SWAGGYMUSIC.plugins.misc import anonymous

        channel_id = utils.get_channel_id(1_800_000_001)
        wizard["channel_settings"](channel_id, "anonymous")
        anonymous.SESSIONS[ADMIN] = {"step": "action", "channel_id": channel_id}
        await anonymous.setanonymous_callback(None, _wizard_query(ADMIN, f"setanonymous:enable:{channel_id}"))
        doc = await settings(channel_id)
        assert not doc.get("anonymous_filter_enabled"), "protection must stay off"
        text = wizard["edit"].await_args.kwargs["text"]
        assert "Sign Messages" in text and "every post" in text

    @pytest.mark.parametrize("mode", ["signatures", "profiles", None])
    async def test_enable_allowed_when_authors_are_visible_or_unknown(self, wizard, mode):
        from SWAGGYMUSIC.plugins.misc import anonymous

        channel_id = utils.get_channel_id(1_800_000_010 + ["signatures", "profiles", None].index(mode))
        wizard["channel_settings"](channel_id, mode)
        anonymous.SESSIONS[ADMIN] = {"step": "action", "channel_id": channel_id}
        await anonymous.setanonymous_callback(None, _wizard_query(ADMIN, f"setanonymous:enable:{channel_id}"))
        assert (await settings(channel_id))["anonymous_filter_enabled"] is True
        text = wizard["edit"].await_args.kwargs["text"]
        assert "ENABLED" in text

    @pytest.mark.parametrize("mode, enabled, expected", [
        ("anonymous", True, "deletes every post while Sign Messages is off"),
        ("anonymous", False, "before enabling"),
        ("signatures", False, "posts signed with an admin's name are kept"),
        ("profiles", True, "posts sent as an admin's own profile are kept"),
    ])
    async def test_status_screen_shows_the_channel_mode(self, wizard, monkeypatch, mode, enabled, expected):
        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.misc import anonymous
        from SWAGGYMUSIC.utils.database import settings_db

        channel_id = utils.get_channel_id(next(_WIZARD_CHANNELS))
        wizard["channel_settings"](channel_id, mode)
        if enabled:
            await settings_db.update_one({"chat_id": channel_id},
                                         {"$set": {"anonymous_filter_enabled": True}}, upsert=True)
        member = AsyncMock()
        member.status = __import__("pyrogram").enums.ChatMemberStatus.ADMINISTRATOR
        member.privileges.can_delete_messages = True
        monkeypatch.setattr(app, "get_chat_member", AsyncMock(return_value=member))
        sent = AsyncMock()
        monkeypatch.setattr(app, "send_message", sent)
        anonymous.SESSIONS[ADMIN] = {"step": "channel_id"}
        chat = types.Chat(id=ADMIN, type=ChatType.PRIVATE, client=app)
        msg = types.Message(id=5, chat=chat, text=str(channel_id), client=app,
                            from_user=types.User(id=ADMIN, first_name="U", client=app))
        await anonymous.setanonymous_channel_input(None, msg)
        text = sent.await_args.kwargs["text"]
        assert expected in text
        assert ("ON 🟢" if enabled else "OFF 🔴") in text

    async def test_disable_always_allowed(self, wizard):
        from SWAGGYMUSIC.plugins.misc import anonymous

        channel_id = utils.get_channel_id(1_800_000_020)
        wizard["channel_settings"](channel_id, "anonymous")
        anonymous.SESSIONS[ADMIN] = {"step": "action", "channel_id": channel_id}
        await anonymous.setanonymous_callback(None, _wizard_query(ADMIN, f"setanonymous:disable:{channel_id}"))
        assert (await settings(channel_id))["anonymous_filter_enabled"] is False
