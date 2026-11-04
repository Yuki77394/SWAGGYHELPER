"""Create Post (Normal Post): target-user resolution.

Root cause covered here: Kurigram resolves a bare numeric User ID only when
the bot holds that user's access hash, either from its local peer cache
(``in_memory=True``, so emptied on every restart) or from Telegram's answer
to the bot zero-access-hash lookup. Otherwise ``get_users()`` raises
``PeerIdInvalid`` (chained from ``KeyError``), which the wizard used to
report as "Make sure the User ID is correct".

Two layers of tests:

* Handler tests drive ``createpost_input`` with the suite's stubbed ``app``
  (conftest). ``get_users`` / ``resolve_peer`` raise the same exception
  objects Kurigram 2.2.25 produces.
* Library-level tests run the real Kurigram ``resolve_peer``, ``get_users``,
  ``get_chat_members``, ``fetch_peers``, HTML parser and
  ``InlineKeyboardButton`` serializer against a simulated Telegram server.
  Only ``Session.invoke`` (the network) is faked.

No real Telegram API calls are made.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram import Client, enums, raw, types
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType
from pyrogram.errors import (
    ChannelInvalid,
    FloodWait,
    PeerIdInvalid,
    UserIdInvalid,
)

import config
from SWAGGYMUSIC import app
from SWAGGYMUSIC.plugins.misc import createpost as cp
from SWAGGYMUSIC.utils import user_resolution as ur
from SWAGGYMUSIC.utils.user_resolution import (
    MAX_USER_ID,
    InvalidUserIdInput,
    UserResolutionError,
    parse_user_id,
)

RESOLUTION_LOGGER = "SWAGGYMUSIC.utils.user_resolution"
CHAT_ID = -1001234567890
CHAT_RAW_ID = 1234567890
OLD_GENERIC_TEXT = "Make sure the User ID is correct"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def kurigram_peer_id_invalid(user_id: int) -> PeerIdInvalid:
    """The exact error Kurigram 2.2.25 resolve_peer() raises for an int ID it
    cannot resolve: ``raise PeerIdInvalid from KeyError('ID not found: …')``."""
    try:
        try:
            raise KeyError(f"ID not found: {user_id}")
        except KeyError as key_error:
            raise PeerIdInvalid from key_error
    except PeerIdInvalid as exc:
        return exc


def make_user(user_id: int, first_name: Optional[str] = "Alice", **kwargs) -> types.User:
    return types.User(id=user_id, first_name=first_name, **kwargs)


def admins_of(*users: types.User) -> MagicMock:
    """A get_chat_members() replacement yielding ``users`` as administrators."""

    async def _gen(chat_id, *args, **kwargs):
        for user in users:
            yield types.ChatMember(status=ChatMemberStatus.ADMINISTRATOR, user=user)

    return MagicMock(side_effect=_gen)


def make_message(user_id: int, text: Optional[str] = None, photo=None) -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.first_name = "Requester"
    msg.from_user.last_name = None
    msg.from_user.username = None
    msg.chat = MagicMock()
    msg.chat.type = ChatType.PRIVATE
    msg.text = text
    msg.caption = None
    msg.entities = []
    msg.photo = photo
    msg.reply_text = AsyncMock()
    return msg


def make_callback(user_id: int, data: str) -> MagicMock:
    cb = MagicMock()
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def replies(msg: MagicMock) -> List[str]:
    return [c.args[0] for c in msg.reply_text.call_args_list]


def channel_admin_member() -> MagicMock:
    member = MagicMock()
    member.status = ChatMemberStatus.ADMINISTRATOR
    member.privileges = MagicMock()
    member.privileges.can_post_messages = True
    member.privileges.can_delete_messages = True
    return member


def allow_final_send(monkeypatch) -> None:
    """Bot + requester are admins of the destination channel."""
    chat = MagicMock()
    chat.id = CHAT_ID
    chat.type = ChatType.CHANNEL
    monkeypatch.setattr(app, "get_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(app, "get_chat_member", AsyncMock(return_value=channel_admin_member()))


async def start_user_id_step(uid: int, fmt: str = "1") -> None:
    await cp._save_session(uid, {
        "step": "user_id", "post_type": "normal", "chat_id": CHAT_ID, "format": fmt,
    })


# ---------------------------------------------------------------------------
# 1. Strict validation of the typed ID (no API call involved)
# ---------------------------------------------------------------------------

class TestParseUserId:
    @pytest.mark.parametrize("text, expected", [
        ("123456789", 123456789),
        ("  987654321\n", 987654321),
        ("1", 1),
        ("0000777", 777),
        (str(MAX_USER_ID), MAX_USER_ID),
    ])
    def test_valid_ids(self, text, expected):
        assert parse_user_id(text) == expected

    @pytest.mark.parametrize("text", [
        "12ab34", "abc", "1_000", "+123", "12 34", "1.5", "1e9", "0x1F",
        "@username", "١٢٣",  # Arabic-Indic digits: int() accepts them
        "１２３",  # full-width digits
        "123​",
    ])
    def test_malformed_ids(self, text):
        with pytest.raises(InvalidUserIdInput) as info:
            parse_user_id(text)
        assert info.value.reason == InvalidUserIdInput.NOT_NUMERIC

    @pytest.mark.parametrize("text", ["-1001234567890", "-5", "-0"])
    def test_negative_ids_look_like_chats(self, text):
        with pytest.raises(InvalidUserIdInput) as info:
            parse_user_id(text)
        assert info.value.reason == InvalidUserIdInput.NEGATIVE

    @pytest.mark.parametrize("text", ["0", "000", str(MAX_USER_ID + 1), "9" * 40])
    def test_out_of_range_ids(self, text):
        with pytest.raises(InvalidUserIdInput) as info:
            parse_user_id(text)
        assert info.value.reason == InvalidUserIdInput.OUT_OF_RANGE

    @pytest.mark.parametrize("text", ["", "   ", None])
    def test_empty_input(self, text):
        with pytest.raises(InvalidUserIdInput) as info:
            parse_user_id(text)
        assert info.value.reason == InvalidUserIdInput.EMPTY

    def test_upper_bound_matches_kurigram(self):
        from pyrogram import utils

        assert MAX_USER_ID == utils.MAX_USER_ID
        assert utils.get_peer_type(MAX_USER_ID) == "user"
        with pytest.raises(ValueError):
            utils.get_peer_type(MAX_USER_ID + 1)


# ---------------------------------------------------------------------------
# 2. The Normal Post "user_id" step (stubbed app)
# ---------------------------------------------------------------------------

class TestUserIdStep:
    async def test_known_user_proceeds_to_colour_step(self, monkeypatch):
        """Scenario 1: the user's info is already available to the client."""
        uid = 7101
        await start_user_id_step(uid)
        target = make_user(424242, "Alice")
        monkeypatch.setattr(app, "get_users", AsyncMock(return_value=target))
        monkeypatch.setattr(app, "get_chat_members", admins_of())

        msg = make_message(uid, "424242")
        await cp.createpost_input(None, msg)

        app.get_users.assert_awaited_once_with(424242)
        app.get_chat_members.assert_not_called()
        sess = await cp._get_session(uid)
        assert sess["step"] == "color"
        assert sess["target_user_id"] == 424242
        assert sess["target_user_name"] == "Alice"
        assert sess["user"] is target
        (reply,) = replies(msg)
        assert "<a href='tg://user?id=424242'>Alice</a>" in reply
        assert "3/5" in reply
        await cp._clear_session(uid)

    @pytest.mark.parametrize("text", ["12ab34", "1_000", "+424242", "١٢٣", "42 42", "@alice"])
    async def test_malformed_input_is_rejected_without_api_calls(self, monkeypatch, text):
        """Scenario 4: letters / unsupported characters."""
        uid = 7102
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock())

        msg = make_message(uid, text)
        await cp.createpost_input(None, msg)

        app.get_users.assert_not_awaited()
        (reply,) = replies(msg)
        assert "Invalid User ID" in reply
        assert (await cp._get_session(uid))["step"] == "user_id"
        await cp._clear_session(uid)

    @pytest.mark.parametrize("text, expected", [
        ("0", "Invalid User ID"),
        (str(MAX_USER_ID + 1), "Invalid User ID"),
        ("-1001234567890", "group or channel ID"),
        ("", "No User ID received"),
    ])
    async def test_invalid_numeric_ids(self, monkeypatch, text, expected):
        """Scenario 3: syntactically invalid numeric IDs never reach Telegram."""
        uid = 7103
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock())

        msg = make_message(uid, text)
        await cp.createpost_input(None, msg)

        app.get_users.assert_not_awaited()
        (reply,) = replies(msg)
        assert expected in reply
        assert (await cp._get_session(uid))["step"] == "user_id"
        await cp._clear_session(uid)

    async def test_unresolvable_user_gets_actionable_message(self, monkeypatch, caplog):
        """Scenarios 2 and 6: valid ID, but no access hash is available to the bot.

        This is the reported bug. The reply must not claim the ID is wrong and
        must say how to fix it.
        """
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7104
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=kurigram_peer_id_invalid(555)))
        monkeypatch.setattr(app, "get_chat_members", admins_of(make_user(999, "OtherAdmin")))

        msg = make_message(uid, "555")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "can't access that user yet" in reply
        assert "@stub_bot" in reply and "<b>Start</b>" in reply
        assert OLD_GENERIC_TEXT not in reply
        assert "Invalid User ID" not in reply
        assert "PeerIdInvalid" not in reply  # no internals for ordinary users
        assert (await cp._get_session(uid))["step"] == "user_id"
        app.get_chat_members.assert_called_once()
        assert app.get_chat_members.call_args.args[0] == CHAT_ID
        assert app.get_chat_members.call_args.kwargs["filter"] == enums.ChatMembersFilter.ADMINISTRATORS

        log_text = caplog.text
        assert "operation='get_users'" in log_text
        assert "target_user_id=555" in log_text
        assert "error_class='PeerIdInvalid'" in log_text
        assert "rpc_error='400 PEER_ID_INVALID'" in log_text
        assert "no access hash in the local peer cache" in log_text
        assert "requester_id=7104" in log_text and f"chat_id={CHAT_ID}" in log_text
        assert "step='user_id'" in log_text
        assert "is not an administrator" in log_text
        await cp._clear_session(uid)

    async def test_user_found_in_destination_chat_admins(self, monkeypatch, caplog):
        """Scenario 2: not in the peer cache, but an admin of the target chat."""
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7105
        await start_user_id_step(uid, fmt="2")
        admin = make_user(31337, "Channel Owner")
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=kurigram_peer_id_invalid(31337)))
        monkeypatch.setattr(app, "get_chat_members", admins_of(make_user(1000, "X"), admin))

        msg = make_message(uid, "31337")
        await cp.createpost_input(None, msg)

        sess = await cp._get_session(uid)
        assert sess["step"] == "color"
        assert sess["target_user_id"] == 31337
        assert sess["target_user_name"] == "Channel Owner"
        assert sess["user"] is admin
        assert "found in the administrators" in caplog.text
        await cp._clear_session(uid)

    async def test_member_list_generator_is_closed_after_an_early_match(self, monkeypatch):
        state = {"closed": False, "yielded": 0}

        async def _gen(chat_id, *args, **kwargs):
            try:
                for uid in (31338, 1001, 1002):
                    state["yielded"] += 1
                    yield types.ChatMember(status=ChatMemberStatus.ADMINISTRATOR, user=make_user(uid, "A"))
            finally:
                state["closed"] = True

        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=kurigram_peer_id_invalid(31338)))
        monkeypatch.setattr(app, "get_chat_members", MagicMock(side_effect=_gen))

        user = await ur.resolve_user(app, 31338, chat_id=CHAT_ID)

        assert user.id == 31338
        assert state == {"closed": True, "yielded": 1}

    async def test_user_id_invalid_rpc_is_not_called_a_wrong_id(self, monkeypatch):
        uid = 7106
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=UserIdInvalid()))
        monkeypatch.setattr(app, "get_chat_members", admins_of())

        msg = make_message(uid, "556")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "can't access that user yet" in reply
        assert OLD_GENERIC_TEXT not in reply
        await cp._clear_session(uid)

    async def test_flood_wait_is_reported_with_wait_time(self, monkeypatch, caplog):
        """Scenario 5: a real Telegram RPC error."""
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7107
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=FloodWait(value=42)))
        monkeypatch.setattr(app, "get_chat_members", admins_of())

        msg = make_message(uid, "557")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "rate-limiting" in reply and "42 seconds" in reply
        app.get_chat_members.assert_not_called()  # not a peer problem
        assert (await cp._get_session(uid))["step"] == "user_id"
        assert "error_class='FloodWait'" in caplog.text
        assert "rpc_error='420 FLOOD_WAIT_X'" in caplog.text
        assert "origin='telegram rpc'" in caplog.text
        await cp._clear_session(uid)

    async def test_other_rpc_error_shows_telegram_error_id(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7108
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=ChannelInvalid()))

        msg = make_message(uid, "558")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "Telegram returned an error" in reply
        assert "<code>CHANNEL_INVALID</code>" in reply
        assert OLD_GENERIC_TEXT not in reply
        assert "error_class='ChannelInvalid'" in caplog.text
        await cp._clear_session(uid)

    async def test_unexpected_error_is_logged_with_secrets_redacted(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7109
        await start_user_id_step(uid)
        secret_text = f"boom token={config.BOT_TOKEN} hash={config.API_HASH} db={config.MONGO_DB_URI}"
        monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=RuntimeError(secret_text)))

        msg = make_message(uid, "559")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "Failed to fetch the user" in reply
        assert "RuntimeError" not in reply and "boom" not in reply
        assert "error_class='RuntimeError'" in caplog.text
        assert "Traceback" in caplog.text  # server-side diagnostics keep the trace
        for secret in (config.BOT_TOKEN, config.API_HASH, config.MONGO_DB_URI):
            assert secret not in caplog.text
        assert "[BOT_TOKEN redacted]" in caplog.text
        assert (await cp._get_session(uid))["step"] == "user_id"
        await cp._clear_session(uid)

    async def test_deleted_account_is_rejected(self, monkeypatch):
        uid = 7110
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(return_value=make_user(560, None, is_deleted=True)))

        msg = make_message(uid, "560")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "has been deleted" in reply
        assert (await cp._get_session(uid))["step"] == "user_id"
        await cp._clear_session(uid)

    async def test_no_user_in_telegram_answer_is_unresolvable(self, monkeypatch):
        """get_users() returns None (userEmpty / newer Kurigram) or, in 2.2.25,
        raises IndexError on an empty answer. Neither crashes the wizard."""
        for uid, side_effect in ((7111, [None]), (7112, IndexError("list index out of range"))):
            await start_user_id_step(uid)
            monkeypatch.setattr(app, "get_users", AsyncMock(side_effect=side_effect))
            monkeypatch.setattr(app, "get_chat_members", admins_of())

            msg = make_message(uid, "561")
            await cp.createpost_input(None, msg)

            (reply,) = replies(msg)
            assert "can't access that user yet" in reply
            assert (await cp._get_session(uid))["step"] == "user_id"
            await cp._clear_session(uid)

    async def test_missing_name_and_username_use_fallback(self, monkeypatch):
        """Scenario 7: a resolved user with no first name and no username."""
        uid = 7113
        await start_user_id_step(uid)
        target = make_user(562, None, last_name=None, username=None)
        monkeypatch.setattr(app, "get_users", AsyncMock(return_value=target))

        msg = make_message(uid, "562")
        await cp.createpost_input(None, msg)

        sess = await cp._get_session(uid)
        assert sess["step"] == "color"
        assert sess["target_user_name"] == "User"
        (reply,) = replies(msg)
        assert "<a href='tg://user?id=562'>User</a>" in reply
        await cp._clear_session(uid)

    async def test_display_name_is_html_escaped_in_confirmation(self, monkeypatch):
        uid = 7114
        await start_user_id_step(uid)
        monkeypatch.setattr(app, "get_users", AsyncMock(return_value=make_user(563, '<b>Tom & "J"</b>')))

        msg = make_message(uid, "563")
        await cp.createpost_input(None, msg)

        (reply,) = replies(msg)
        assert "&lt;b&gt;Tom &amp; &quot;J&quot;&lt;/b&gt;" in reply
        assert "<b>Tom" not in reply
        # The raw name is still what gets persisted and shown on the button.
        assert (await cp._get_session(uid))["target_user_name"] == '<b>Tom & "J"</b>'
        await cp._clear_session(uid)


# ---------------------------------------------------------------------------
# 3. Final send: mention, button, restart recovery
# ---------------------------------------------------------------------------

class TestFinalSend:
    @pytest.mark.parametrize("fmt, colour, style", [
        ("1", "blue", ButtonStyle.PRIMARY),
        ("2", "default", None),
    ])
    async def test_final_mention_caption_and_button(self, monkeypatch, fmt, colour, style):
        """Scenario 8: the generated post is unchanged by the fix."""
        uid = 7201
        allow_final_send(monkeypatch)
        name = 'Tom & "Jerry" <3'
        target = make_user(777001, name)
        await cp._save_session(uid, {
            "step": "photo", "post_type": "normal", "chat_id": CHAT_ID, "format": fmt,
            "target_user_id": target.id, "target_user_name": name, "color": colour,
        })
        cp.SESSIONS[uid]["user"] = target

        msg = make_message(uid, "https://example.com/photo.jpg")
        await cp.createpost_input(None, msg)

        app.resolve_peer.assert_any_await(777001)
        app.send_photo.assert_awaited_once()
        kwargs = app.send_photo.await_args.kwargs
        mention = '<a href="tg://user?id=777001">Tom &amp; &quot;Jerry&quot; &lt;3</a>'
        expected_caption = cp._format_one(mention) if fmt == "1" else cp._format_two(mention)
        assert kwargs["caption"] == expected_caption
        assert kwargs["chat_id"] == CHAT_ID
        assert kwargs["photo"] == "https://example.com/photo.jpg"
        (button,) = kwargs["reply_markup"].inline_keyboard[0]
        assert button.text == name
        assert button.user_id == 777001
        assert button.style == (style if style is not None else ButtonStyle.DEFAULT)
        assert any("Pᴏsᴛ Cʀᴇᴀᴛᴇᴅ" in r for r in replies(msg))
        assert not await cp._get_session(uid)

    async def test_restart_recovered_session_resolves_peer_then_sends(self, monkeypatch):
        """Scenario 10: restart mid-wizard (no in-memory User object). The peer
        is re-resolved before the mention/button are serialized."""
        uid = 7202
        allow_final_send(monkeypatch)
        await cp._save_session(uid, {
            "step": "photo", "post_type": "normal", "chat_id": CHAT_ID, "format": "2",
            "target_user_id": 777002, "target_user_name": "Restored", "color": "green",
        })
        cp.SESSIONS.pop(uid, None)  # simulate the restart

        msg = make_message(uid, "https://example.com/p.jpg")
        await cp.createpost_input(None, msg)

        app.resolve_peer.assert_any_await(777002)
        app.send_photo.assert_awaited_once()
        kwargs = app.send_photo.await_args.kwargs
        assert kwargs["caption"] == cp._format_two('<a href="tg://user?id=777002">Restored</a>')
        (button,) = kwargs["reply_markup"].inline_keyboard[0]
        assert (button.text, button.user_id, button.style) == ("Restored", 777002, ButtonStyle.SUCCESS)

    async def test_restart_recovered_session_with_unresolvable_user(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7203
        allow_final_send(monkeypatch)
        await cp._save_session(uid, {
            "step": "photo", "post_type": "normal", "chat_id": CHAT_ID, "format": "1",
            "target_user_id": 777003, "target_user_name": "Gone", "color": "red",
        })
        cp.SESSIONS.pop(uid, None)

        async def _resolve(peer_id):
            if peer_id == 777003:
                raise kurigram_peer_id_invalid(777003)
            return MagicMock()

        monkeypatch.setattr(app, "resolve_peer", AsyncMock(side_effect=_resolve))
        monkeypatch.setattr(app, "get_chat_members", admins_of())

        msg = make_message(uid, "https://example.com/p.jpg")
        await cp.createpost_input(None, msg)

        app.send_photo.assert_not_awaited()
        assert any("can no longer access the selected user" in r for r in replies(msg))
        assert "operation='resolve_peer'" in caplog.text
        assert "step='sending'" in caplog.text
        assert not await cp._get_session(uid)

    async def test_restart_recovered_session_user_found_in_admins(self, monkeypatch):
        uid = 7204
        allow_final_send(monkeypatch)
        await cp._save_session(uid, {
            "step": "photo", "post_type": "normal", "chat_id": CHAT_ID, "format": "1",
            "target_user_id": 777004, "target_user_name": "Admin", "color": "blue",
        })
        cp.SESSIONS.pop(uid, None)

        calls = {"n": 0}

        async def _resolve(peer_id):
            if peer_id == 777004:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise kurigram_peer_id_invalid(777004)
            return MagicMock()

        monkeypatch.setattr(app, "resolve_peer", AsyncMock(side_effect=_resolve))
        monkeypatch.setattr(app, "get_chat_members", admins_of(make_user(777004, "Admin")))

        msg = make_message(uid, "https://example.com/p.jpg")
        await cp.createpost_input(None, msg)

        app.send_photo.assert_awaited_once()
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 4. The complete Normal Post wizard after the fix
# ---------------------------------------------------------------------------

class TestNormalPostWorkflow:
    async def test_full_wizard_creates_the_post(self, monkeypatch):
        """Scenario 9: /createpost → Normal → chat ID → format → user ID →
        colour → photo → post sent."""
        uid = 7301
        allow_final_send(monkeypatch)
        target = make_user(888001, "Support Desk")
        monkeypatch.setattr(app, "get_users", AsyncMock(return_value=target))

        start = make_message(uid, "/createpost")
        await cp.createpost_start(None, start)
        assert (await cp._get_session(uid))["step"] == "post_type"

        await cp.createpost_callbacks(None, make_callback(uid, "createpost:type:normal"))
        assert (await cp._get_session(uid))["step"] == "chat_id"

        await cp.createpost_input(None, make_message(uid, str(CHAT_ID)))
        assert (await cp._get_session(uid))["step"] == "format"

        await cp.createpost_callbacks(None, make_callback(uid, "createpost:format:1"))
        assert (await cp._get_session(uid))["step"] == "user_id"

        step_msg = make_message(uid, " 888001 ")
        await cp.createpost_input(None, step_msg)
        assert (await cp._get_session(uid))["step"] == "color"
        assert "Support Desk" in replies(step_msg)[0]

        await cp.createpost_callbacks(None, make_callback(uid, "createpost:color:green"))
        assert (await cp._get_session(uid))["step"] == "photo"

        photo = MagicMock()
        photo.file_id = "AgAD-file-id"
        final = make_message(uid, None, photo=photo)
        await cp.createpost_input(None, final)

        app.send_photo.assert_awaited_once()
        kwargs = app.send_photo.await_args.kwargs
        assert kwargs["photo"] == "AgAD-file-id"
        assert kwargs["caption"] == cp._format_one('<a href="tg://user?id=888001">Support Desk</a>')
        (button,) = kwargs["reply_markup"].inline_keyboard[0]
        assert (button.text, button.user_id, button.style) == ("Support Desk", 888001, ButtonStyle.SUCCESS)
        assert any("Pᴏsᴛ Cʀᴇᴀᴛᴇᴅ" in r for r in replies(final))
        assert not await cp._get_session(uid)


# ---------------------------------------------------------------------------
# 5. Regression: other Create Post paths
# ---------------------------------------------------------------------------

class TestCreatePostRegression:
    async def test_restriction_message_keeps_contact_owner_button(self):
        msg = make_message(7401)
        await cp._send_restriction_message(msg)
        msg.reply_text.assert_awaited_once()
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        (button,) = markup.inline_keyboard[0]
        assert button.user_id == config.OWNER_ID

    async def test_restriction_message_survives_unresolvable_owner(self, monkeypatch, caplog):
        """The Contact Owner button needs OWNER_ID's peer. When Kurigram can't
        resolve it, the notice must still be sent and the session cleared."""
        caplog.set_level(logging.INFO, logger=RESOLUTION_LOGGER)
        uid = 7402
        chat = MagicMock()
        chat.type = ChatType.CHANNEL
        monkeypatch.setattr(app, "get_chat", AsyncMock(return_value=chat))
        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER

        async def _member(chat_id, member_id):
            return channel_admin_member() if member_id == app.id else user_member

        monkeypatch.setattr(app, "get_chat_member", AsyncMock(side_effect=_member))
        await cp._save_session(uid, {
            "step": "photo", "post_type": "normal", "chat_id": CHAT_ID, "format": "1",
            "target_user_id": 1, "target_user_name": "X", "color": "blue",
        })

        msg = make_message(uid, "https://example.com/p.jpg")

        async def _reply(text, reply_markup=None, **kwargs):
            if reply_markup is not None:
                raise kurigram_peer_id_invalid(config.OWNER_ID)
            return MagicMock()

        msg.reply_text = AsyncMock(side_effect=_reply)
        await cp.createpost_input(None, msg)

        texts = replies(msg)
        assert texts == [cp.RESTRICTION_TEXT, cp.RESTRICTION_TEXT]
        assert "reply_markup" not in msg.reply_text.await_args_list[1].kwargs
        app.send_photo.assert_not_awaited()
        assert not await cp._get_session(uid)
        assert "Contact Owner button" in caplog.text

    async def test_hidden_preview_flow_is_unaffected(self, monkeypatch):
        uid = 1  # owner: bypasses the user-admin check
        allow_final_send(monkeypatch)
        await cp._save_session(uid, {
            "step": "hidden_video", "post_type": "hidden", "chat_id": CHAT_ID,
            "position": "below", "hidden_text": "Hello",
        })
        cp.SESSIONS[uid]["hidden_entities"] = []
        monkeypatch.setattr(app, "get_users", AsyncMock())

        msg = make_message(uid, "https://files.catbox.moe/clip.mp4")
        await cp.createpost_input(None, msg)

        app.invoke.assert_awaited_once()
        sent = app.invoke.await_args.args[0]
        assert isinstance(sent, raw.functions.messages.SendMessage)
        assert sent.message == "​Hello"
        assert sent.invert_media is False
        app.get_users.assert_not_awaited()
        assert any("Hɪᴅᴅᴇɴ Pʀᴇᴠɪᴇᴡ Pᴏsᴛ Cʀᴇᴀᴛᴇᴅ" in r for r in replies(msg))
        assert not await cp._get_session(uid)

    async def test_chat_id_step_unchanged(self, monkeypatch):
        uid = 7403
        await cp._save_session(uid, {"step": "chat_id", "post_type": "normal"})
        msg = make_message(uid, "not-a-number")
        await cp.createpost_input(None, msg)
        assert "Invalid Chat ID" in replies(msg)[0]
        assert (await cp._get_session(uid))["step"] == "chat_id"
        await cp._clear_session(uid)


# ---------------------------------------------------------------------------
# 6. Real Kurigram 2.2.25 resolution code against a simulated Telegram
# ---------------------------------------------------------------------------

class SimulatedTelegram:
    """Server side of the calls Kurigram makes while resolving peers.

    Contract for bots, as documented by Telegram and Kurigram:
      * a full access hash is always accepted;
      * access_hash=0 is honoured only for users the server lets this bot see;
      * a user the bot cannot see is left out of users.getUsers' vector;
      * channel participants come back with full access hashes.
    """

    def __init__(self) -> None:
        self.users: Dict[int, dict] = {}
        self.channels: Dict[int, dict] = {}
        self.calls: List[tuple] = []

    def add_user(self, user_id: int, name: str, *, zero_hash_ok: bool) -> None:
        self.users[user_id] = {"name": name, "access_hash": user_id * 7919 + 13, "zero_ok": zero_hash_ok}

    def add_channel(self, raw_id: int, *, admins: List[int], creator: int) -> None:
        self.channels[raw_id] = {"access_hash": raw_id * 31 + 7, "admins": admins, "creator": creator}

    def raw_user(self, user_id: int) -> raw.types.User:
        rec = self.users[user_id]
        return raw.types.User(id=user_id, first_name=rec["name"], access_hash=rec["access_hash"],
                              usernames=[], restriction_reason=[])

    def raw_channel(self, raw_id: int) -> raw.types.Channel:
        return raw.types.Channel(id=raw_id, title="Target", photo=raw.types.ChatPhotoEmpty(), date=0,
                                 broadcast=True, access_hash=self.channels[raw_id]["access_hash"],
                                 usernames=[], restriction_reason=[])

    async def handle(self, query):
        if isinstance(query, raw.functions.users.GetUsers):
            self.calls.append(("users.getUsers", [(i.user_id, i.access_hash) for i in query.id]))
            out = []
            for item in query.id:
                rec = self.users.get(item.user_id)
                if rec and (item.access_hash == rec["access_hash"] or (item.access_hash == 0 and rec["zero_ok"])):
                    out.append(self.raw_user(item.user_id))
            return out
        if isinstance(query, raw.functions.channels.GetChannels):
            self.calls.append(("channels.getChannels", [i.channel_id for i in query.id]))
            return raw.types.messages.Chats(
                chats=[self.raw_channel(i.channel_id) for i in query.id if i.channel_id in self.channels])
        if isinstance(query, raw.functions.channels.GetParticipants):
            channel = self.channels[query.channel.channel_id]
            self.calls.append(("channels.getParticipants", type(query.filter).__name__, query.offset))
            assert isinstance(query.filter, raw.types.ChannelParticipantsAdmins)
            if query.offset:
                return raw.types.channels.ChannelParticipants(count=0, participants=[], chats=[], users=[])
            rights = raw.types.ChatAdminRights(post_messages=True)
            participants = [raw.types.ChannelParticipantCreator(user_id=channel["creator"], admin_rights=rights)]
            participants += [
                raw.types.ChannelParticipantAdmin(user_id=a, promoted_by=channel["creator"], date=0,
                                                  admin_rights=rights)
                for a in channel["admins"]
            ]
            member_ids = [channel["creator"], *channel["admins"]]
            return raw.types.channels.ChannelParticipants(
                count=len(participants), participants=participants, chats=[],
                users=[self.raw_user(u) for u in member_ids])
        raise AssertionError(f"unexpected request {type(query).__name__}")


class _SimulatedSession:
    def __init__(self, server: SimulatedTelegram) -> None:
        self.server = server

    async def invoke(self, query, *args, **kwargs):
        return await self.server.handle(query)


async def bot_process(server: SimulatedTelegram) -> Client:
    """A fresh bot process, configured like core/bot.py (in_memory=True)."""
    client = Client("resolution-test", api_id=1, api_hash="0" * 32, bot_token="1:x", in_memory=True)
    await client.storage.open()
    client.is_connected = True
    client.session = _SimulatedSession(server)
    return client


@pytest.fixture
def telegram() -> SimulatedTelegram:
    server = SimulatedTelegram()
    server.add_user(111, "Alice", zero_hash_ok=False)   # messaged the bot this run
    server.add_user(222, "Bob", zero_hash_ok=True)      # server lets the bot see him
    server.add_user(333, "Carol", zero_hash_ok=False)   # never interacted with the bot
    server.add_user(444, "Dave", zero_hash_ok=False)    # admin of the target channel
    server.add_user(500, "Owner", zero_hash_ok=False)   # creator of the target channel
    server.add_channel(CHAT_RAW_ID, admins=[444], creator=500)
    return server


class TestRealKurigramResolution:
    async def test_cached_peer_needs_no_zero_hash_lookup(self, telegram):
        client = await bot_process(telegram)
        await client.fetch_peers([telegram.raw_user(111)])  # e.g. Alice's message update

        user = await ur.resolve_user(client, 111, chat_id=CHAT_ID)

        assert (user.id, user.first_name) == (111, "Alice")
        assert telegram.calls == [("users.getUsers", [(111, telegram.users[111]["access_hash"])])]
        await client.storage.close()

    async def test_uncached_peer_resolved_by_zero_access_hash(self, telegram):
        client = await bot_process(telegram)

        user = await ur.resolve_user(client, 222, chat_id=CHAT_ID)

        assert user.first_name == "Bob"
        assert telegram.calls[0] == ("users.getUsers", [(222, 0)])
        await client.storage.close()

    async def test_unseen_user_is_unresolvable_not_invalid(self, telegram):
        client = await bot_process(telegram)

        with pytest.raises(UserResolutionError) as info:
            await ur.resolve_user(client, 333, chat_id=CHAT_ID)

        err = info.value
        assert err.kind == UserResolutionError.UNRESOLVABLE
        # The real library error: built client-side after Telegram's empty answer.
        assert isinstance(err.error, PeerIdInvalid)
        assert isinstance(err.error.__cause__, KeyError)
        assert telegram.calls[0] == ("users.getUsers", [(333, 0)])
        assert ("channels.getParticipants", "ChannelParticipantsAdmins", 0) in telegram.calls
        await client.storage.close()

    async def test_nonexistent_id_gives_the_same_answer(self, telegram):
        """A bot cannot tell a non-existent ID from an unseen user."""
        client = await bot_process(telegram)
        with pytest.raises(UserResolutionError) as info:
            await ur.resolve_user(client, 987654321)
        assert info.value.kind == UserResolutionError.UNRESOLVABLE
        assert isinstance(info.value.error, PeerIdInvalid)
        await client.storage.close()

    async def test_admin_list_fallback_caches_a_usable_peer(self, telegram):
        client = await bot_process(telegram)

        user = await ur.resolve_user(client, 444, chat_id=CHAT_ID)
        assert (user.id, user.first_name) == (444, "Dave")

        # The member list delivered Dave's access hash, so everything the final
        # send serializes now resolves locally, with no extra request.
        telegram.calls.clear()
        peer = await client.resolve_peer(444)
        assert peer.access_hash == telegram.users[444]["access_hash"]
        button = await types.InlineKeyboardButton(text="Dave", user_id=444).write(client)
        assert isinstance(button, raw.types.InputKeyboardButtonUserProfile)
        parsed = await client.parser.parse('<a href="tg://user?id=444">Dave</a>', enums.ParseMode.HTML)
        assert [type(e).__name__ for e in parsed["entities"]] == ["InputMessageEntityMentionName"]
        assert telegram.calls == []
        await client.storage.close()

    async def test_restart_forgets_previously_known_users(self, telegram):
        first = await bot_process(telegram)
        await first.fetch_peers([telegram.raw_user(111)])
        assert (await ur.resolve_user(first, 111)).first_name == "Alice"
        await first.storage.close()

        restarted = await bot_process(telegram)  # in_memory=True: empty peer table
        with pytest.raises(UserResolutionError) as info:
            await ur.resolve_user(restarted, 111)
        assert info.value.kind == UserResolutionError.UNRESOLVABLE
        await restarted.storage.close()

    async def test_unresolved_peer_breaks_mention_and_button(self, telegram):
        """Why the final send needs ensure_user_peer(): without the access hash
        Kurigram drops the mention and the profile button raises."""
        client = await bot_process(telegram)
        parsed = await client.parser.parse('<b><a href="tg://user?id=333">Carol</a></b>', enums.ParseMode.HTML)
        assert [type(e).__name__ for e in parsed["entities"]] == ["MessageEntityBold"]
        with pytest.raises(PeerIdInvalid):
            await types.InlineKeyboardButton(text="Carol", user_id=333).write(client)
        await client.storage.close()

    async def test_ensure_user_peer_after_restart(self, telegram):
        client = await bot_process(telegram)

        await ur.ensure_user_peer(client, 444, chat_id=CHAT_ID)  # admin → OK
        await types.InlineKeyboardButton(text="Dave", user_id=444).write(client)

        with pytest.raises(UserResolutionError) as info:
            await ur.ensure_user_peer(client, 333, chat_id=CHAT_ID)
        assert info.value.kind == UserResolutionError.UNRESOLVABLE
        await client.storage.close()

    async def test_wizard_step_with_the_real_client(self, telegram, monkeypatch):
        """The handler, driven end to end by the real Kurigram client."""
        client = await bot_process(telegram)
        monkeypatch.setattr(cp, "app", client)
        monkeypatch.setattr(client, "username", "stub_bot", raising=False)

        uid = 7501
        await start_user_id_step(uid)
        unseen = make_message(uid, "333")
        await cp.createpost_input(None, unseen)
        assert "can't access that user yet" in replies(unseen)[0]
        assert OLD_GENERIC_TEXT not in replies(unseen)[0]
        assert (await cp._get_session(uid))["step"] == "user_id"

        admin = make_message(uid, "444")
        await cp.createpost_input(None, admin)
        sess = await cp._get_session(uid)
        assert (sess["step"], sess["target_user_id"], sess["target_user_name"]) == ("color", 444, "Dave")
        await cp._clear_session(uid)
        await client.storage.close()
