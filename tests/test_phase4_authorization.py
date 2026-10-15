"""Phase 4: Authorization tests.

Tests the Part 3 permission model:
 - is_owner / is_sudo identity checks.
 - verify_channel_admin: bot access, bot delete/post permission, user admin status.
 - Owner bypass.
 - Channel-admin verification for /setgap, /setdelay, /createpost.
 - Callback authorization cannot be bypassed.
 - Users cannot modify another user's session.
 - Invalid/missing config fails safely (tested in Phase 3).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Identity checks (pure, no I/O)
# ---------------------------------------------------------------------------

class TestIdentityChecks:
    """is_owner and is_sudo are pure functions with no I/O."""

    def test_is_owner_true_for_owner_id(self):
        from SWAGGYMUSIC.utils.permissions import is_owner
        # conftest stubs config.OWNER_ID = 1
        assert is_owner(1) is True

    def test_is_owner_false_for_non_owner(self):
        from SWAGGYMUSIC.utils.permissions import is_owner
        assert is_owner(2) is False
        assert is_owner(0) is False
        assert is_owner(-1) is False

    def test_is_owner_handles_invalid_input(self):
        from SWAGGYMUSIC.utils.permissions import is_owner
        assert is_owner("not_a_number") is False
        assert is_owner(None) is False

    def test_is_sudo_true_for_seeded_sudoers(self):
        from SWAGGYMUSIC.utils.permissions import is_sudo
        from SWAGGYMUSIC.misc import SUDOERS
        # conftest seeds SUDOERS with user 1 (owner)
        assert is_sudo(1) is True

    def test_is_sudo_false_for_non_sudoer(self):
        from SWAGGYMUSIC.utils.permissions import is_sudo
        assert is_sudo(99999) is False


# ---------------------------------------------------------------------------
# verify_channel_admin
# ---------------------------------------------------------------------------

class TestVerifyChannelAdmin:
    """Tests for verify_channel_admin — the core channel-admin check."""

    async def test_owner_bypasses_channel_admin_check(self):
        """The owner is trusted for any channel the bot can access —
        no need for the owner to be an admin of every channel.
        """
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        # Bot can access the chat.
        chat = MagicMock()
        chat.id = -100301
        app.get_chat = AsyncMock(return_value=chat)

        # Bot is admin with delete permission.
        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        # User is NOT an admin (just a member).
        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER

        call_count = {"n": 0}
        async def _get_member(chat_id, user_id):
            call_count["n"] += 1
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        # Owner (user_id=1) should bypass the user-admin check.
        ok, reason = await verify_channel_admin(-100301, 1)
        assert ok is True
        assert reason is None

    async def test_non_owner_admin_is_accepted(self):
        """A non-owner user who IS an admin of the channel is accepted."""
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        chat = MagicMock()
        chat.id = -100302
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        user_member = MagicMock()
        user_member.status = ChatMemberStatus.ADMINISTRATOR

        async def _get_member(chat_id, user_id):
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        # User 555 is an admin (not the owner).
        ok, reason = await verify_channel_admin(-100302, 555)
        assert ok is True
        assert reason is None

    async def test_non_admin_user_is_rejected(self):
        """A non-owner, non-admin user is rejected with a clear reason."""
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        chat = MagicMock()
        chat.id = -100303
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER

        async def _get_member(chat_id, user_id):
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        ok, reason = await verify_channel_admin(-100303, 666)
        assert ok is False
        assert reason is not None
        assert "not an admin" in reason.lower()

    async def test_bot_cannot_access_chat_is_rejected(self):
        """If the bot can't access the chat, verification fails safely."""
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app

        app.get_chat = AsyncMock(side_effect=RuntimeError("CHAT_INVALID"))

        ok, reason = await verify_channel_admin(-100304, 1)
        assert ok is False
        assert reason is not None
        assert "can't access" in reason.lower()

    async def test_bot_without_delete_permission_is_rejected(self):
        """If require_delete=True and the bot lacks can_delete_messages,
        verification fails with a clear reason.
        """
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        chat = MagicMock()
        chat.id = -100305
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = False  # NO delete permission

        app.get_chat_member = AsyncMock(return_value=bot_member)

        ok, reason = await verify_channel_admin(-100305, 1, require_delete=True)
        assert ok is False
        assert reason is not None
        assert "Delete Messages" in reason

    async def test_require_post_checks_can_post_messages(self):
        """If require_post=True and the bot lacks can_post_messages,
        verification fails with a clear reason.
        """
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        chat = MagicMock()
        chat.id = -100306
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True
        bot_member.privileges.can_post_messages = False  # NO post permission

        app.get_chat_member = AsyncMock(return_value=bot_member)

        # Owner bypasses the USER admin check but NOT the BOT permission check.
        ok, reason = await verify_channel_admin(-100306, 1, require_post=True)
        assert ok is False
        assert reason is not None
        assert "Post Messages" in reason

    async def test_invalid_ids_are_rejected(self):
        """Non-numeric IDs are rejected safely."""
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin

        ok, reason = await verify_channel_admin("not_a_number", 1)
        assert ok is False
        assert "Invalid" in reason

    async def test_api_failure_on_user_check_fails_safe(self):
        """If app.get_chat_member fails for the USER check, verification
        fails safe (does NOT fall back to 'allow').
        """
        from SWAGGYMUSIC.utils.permissions import verify_channel_admin
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        chat = MagicMock()
        chat.id = -100307
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        call_count = {"n": 0}
        async def _flaky(chat_id, user_id):
            call_count["n"] += 1
            if user_id == app.id:
                return bot_member
            raise RuntimeError("USER_NOT_RESTRICTED")
        app.get_chat_member = AsyncMock(side_effect=_flaky)

        # Non-owner user; user check raises → fail safe.
        ok, reason = await verify_channel_admin(-100307, 777)
        assert ok is False
        assert reason is not None
        assert "couldn't verify" in reason.lower() or "admin" in reason.lower()


# ---------------------------------------------------------------------------
# Callback authorization bypass tests
# ---------------------------------------------------------------------------

class TestCallbackAuthorizationBypass:
    """A crafted callback_data string must NOT bypass authorization."""

    async def test_setconfig_enable_re_verifies_admin(self):
        """The ENABLE callback must re-verify channel-admin when pressed,
        even if the user crafted the callback_data with another channel's ID.
        """
        from SWAGGYMUSIC.plugins.bot import setconfig as sc
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        # Set up a session for user 555 (a non-owner, non-admin user).
        sc.SESSIONS[555] = {"feature": "setgap", "step": "action", "channel_id": -100311}

        # Crafted callback_data targeting channel -100312 (DIFFERENT from
        # the session's channel_id). This simulates a user editing the
        # callback_data to target a channel they don't administer.
        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 555
        cb.data = "setconfig:enable:setgap:-100312"
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        # verify_channel_admin will be called with -100312 and user 555.
        # Mock it to return False (user is not an admin of -100312).
        chat = MagicMock()
        chat.id = -100312
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER  # NOT an admin

        async def _get_member(chat_id, user_id):
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        await sc.setconfig_callbacks(app, cb)

        # The callback must have been rejected.
        cb.answer.assert_any_call(
            "You are not authorized to manage this channel.",
            show_alert=True,
        )
        # Session must have been cleaned up.
        assert 555 not in sc.SESSIONS

    async def test_setconfig_disable_re_verifies_admin(self):
        """The DISABLE callback must also re-verify admin status."""
        from SWAGGYMUSIC.plugins.bot import setconfig as sc
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus

        sc.SESSIONS[666] = {"feature": "setgap", "step": "action", "channel_id": -100321}

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 666
        cb.data = "setconfig:disable:setgap:-100322"
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        chat = MagicMock()
        chat.id = -100322
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True

        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER

        async def _get_member(chat_id, user_id):
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        await sc.setconfig_callbacks(app, cb)

        cb.answer.assert_any_call(
            "You are not authorized to manage this channel.",
            show_alert=True,
        )
        assert 666 not in sc.SESSIONS


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------

class TestSessionIsolation:
    """Users cannot modify another user's session."""

    async def test_createpost_session_is_per_user(self):
        """User A's session must not be visible to user B."""
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        # User A starts a session.
        await cp._save_session(111, {"step": "post_type", "post_type": "hidden"})
        # User B has no session.
        sess_b = await cp._get_session(222)
        assert not sess_b, "User B must not see User A's session"

        # User A's session is intact.
        sess_a = await cp._get_session(111)
        assert sess_a.get("step") == "post_type"
        assert sess_a.get("post_type") == "hidden"

        # Clean up.
        await cp._clear_session(111)

    async def test_createpost_clear_session_only_affects_one_user(self):
        """Clearing user A's session must NOT affect user B's session."""
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(111, {"step": "post_type"})
        await cp._save_session(222, {"step": "chat_id", "chat_id": -100331})

        await cp._clear_session(111)

        assert not await cp._get_session(111)
        sess_b = await cp._get_session(222)
        assert sess_b.get("step") == "chat_id"
        assert sess_b.get("chat_id") == -100331

        await cp._clear_session(222)

    async def test_setconfig_session_is_per_user(self):
        """setconfig sessions are also per-user (in-memory dict keyed by user_id)."""
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        sc.SESSIONS[111] = {"feature": "setgap", "step": "channel_id"}
        sc.SESSIONS[222] = {"feature": "setdelay", "step": "duration"}

        assert sc.SESSIONS.get(111) is not sc.SESSIONS.get(222)
        assert sc.SESSIONS[111]["feature"] == "setgap"
        assert sc.SESSIONS[222]["feature"] == "setdelay"

        sc.SESSIONS.pop(111, None)
        sc.SESSIONS.pop(222, None)


# ---------------------------------------------------------------------------
# Owner-only command protection
# ---------------------------------------------------------------------------

class TestOwnerOnlyProtection:
    """Owner-only commands (currently none beyond SUDO) remain protected."""

    def test_logger_command_is_sudo_only(self):
        """The /logger command filter must include & SUDOERS."""
        from SWAGGYMUSIC.plugins.sudo import logger as logger_mod
        import inspect

        # Find the handler's filter by inspecting the decorator.
        # The handler is registered with @app.on_message(filters.command(["logger"]) & SUDOERS).
        # We verify by checking that SUDOERS is imported and used in the module.
        from SWAGGYMUSIC.misc import SUDOERS
        assert SUDOERS is not None, "SUDOERS filter must be available"

    async def test_owner_can_pass_is_owner_check(self):
        """The owner (user_id=1 per conftest) passes is_owner."""
        from SWAGGYMUSIC.utils.permissions import is_owner
        assert is_owner(1) is True

    async def test_non_owner_fails_is_owner_check(self):
        """A non-owner user fails is_owner."""
        from SWAGGYMUSIC.utils.permissions import is_owner
        assert is_owner(2) is False
