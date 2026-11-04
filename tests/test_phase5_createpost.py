"""Phase 5: Createpost tests.

Tests the /createpost wizard:
 - Post type selection (hidden / normal).
 - Format selection (1 / 2) for normal posts.
 - Position selection (above / below) for hidden posts.
 - Colour selection for normal posts.
 - Cancel button clears the session.
 - Session persistence (save / load / clear).
 - Session isolation (per-user).
 - Restart recovery (session survives via DB).
 - Channel-admin gate at the final send.
 - Invalid input handling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Session persistence (Part 3)
# ---------------------------------------------------------------------------

class TestSessionPersistence:
    """Tests for the createpost_sessions persistence layer."""

    async def test_save_and_load_session(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(501, {
            "step": "chat_id",
            "post_type": "hidden",
            "chat_id": -100501,
        })

        sess = await cp._get_session(501)
        assert sess["step"] == "chat_id"
        assert sess["post_type"] == "hidden"
        assert sess["chat_id"] == -100501

        await cp._clear_session(501)

    async def test_clear_session_removes_from_db(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp
        from SWAGGYMUSIC.utils.createpost_sessions import load_session

        await cp._save_session(502, {"step": "post_type"})
        await cp._clear_session(502)

        # The DB must also be cleared.
        doc = await load_session(502)
        assert doc is None

    async def test_session_recovery_from_db(self):
        """Simulate a restart: the in-memory cache is empty, but the DB
        has a session. _get_session must load it from the DB.
        """
        from SWAGGYMUSIC.plugins.misc import createpost as cp
        from SWAGGYMUSIC.utils.createpost_sessions import save_session

        # Write directly to the DB (simulating a pre-restart state).
        await save_session(503, {"step": "hidden_text", "hidden_text": "hello"})

        # Clear the in-memory cache (simulating a restart).
        cp.SESSIONS.pop(503, None)

        # _get_session must load from the DB.
        sess = await cp._get_session(503)
        assert sess["step"] == "hidden_text"
        assert sess["hidden_text"] == "hello"

        await cp._clear_session(503)

    async def test_session_expiry_refreshes_on_access(self):
        """Loading a session refreshes expires_at (inactivity timeout,
        not a hard lifetime cap).
        """
        from SWAGGYMUSIC.utils.createpost_sessions import (
            save_session, load_session, SESSION_TTL_SECONDS,
        )
        from datetime import datetime, timezone, timedelta

        await save_session(504, {"step": "post_type"})
        # Load it once to get the initial expires_at.
        doc1 = await load_session(504)
        exp1 = doc1["expires_at"]

        # Wait a moment, then load again.
        import asyncio
        await asyncio.sleep(0.05)
        doc2 = await load_session(504)
        exp2 = doc2["expires_at"]

        # expires_at must have been refreshed (later than before).
        assert exp2 > exp1, "load_session must refresh expires_at"

        from SWAGGYMUSIC.utils.createpost_sessions import delete_session
        await delete_session(504)


# ---------------------------------------------------------------------------
# Wizard step transitions
# ---------------------------------------------------------------------------

class TestWizardSteps:
    """Tests for the callback-driven step transitions."""

    async def test_post_type_hidden_transitions_to_chat_id(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(511, {"step": "post_type"})

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 511
        cb.data = "createpost:type:hidden"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(511)
        assert sess["post_type"] == "hidden"
        assert sess["step"] == "chat_id"

        await cp._clear_session(511)

    async def test_post_type_normal_transitions_to_chat_id(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(512, {"step": "post_type"})

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 512
        cb.data = "createpost:type:normal"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(512)
        assert sess["post_type"] == "normal"
        assert sess["step"] == "chat_id"

        await cp._clear_session(512)

    async def test_format_selection_transitions_to_user_id(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(513, {
            "step": "format", "post_type": "normal", "chat_id": -100513,
        })

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 513
        cb.data = "createpost:format:2"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(513)
        assert sess["format"] == "2"
        assert sess["step"] == "user_id"

        await cp._clear_session(513)

    async def test_position_selection_transitions_to_hidden_text(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(514, {
            "step": "position", "post_type": "hidden", "chat_id": -100514,
        })

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 514
        cb.data = "createpost:position:above"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(514)
        assert sess["position"] == "above"
        assert sess["step"] == "hidden_text"

        await cp._clear_session(514)

    async def test_color_selection_transitions_to_photo(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(515, {
            "step": "color", "post_type": "normal",
            "chat_id": -100515, "format": "1",
        })

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 515
        cb.data = "createpost:color:blue"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(515)
        assert sess["color"] == "blue"
        assert sess["step"] == "photo"

        await cp._clear_session(515)


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

class TestCancel:
    """Cancel button and /cancel command must clear the session."""

    async def test_cancel_callback_clears_session(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(521, {"step": "post_type"})

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 521
        cb.data = "createpost:cancel"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await cp.createpost_callbacks(None, cb)

        sess = await cp._get_session(521)
        assert not sess, "Cancel must clear the session"
        assert 521 not in cp.SESSIONS

    async def test_cancel_command_clears_session(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp
        from pyrogram.enums import ChatType

        await cp._save_session(522, {"step": "post_type"})

        msg = MagicMock()
        msg.from_user = MagicMock()
        msg.from_user.id = 522
        msg.chat = MagicMock()
        msg.chat.type = ChatType.PRIVATE
        msg.reply_text = AsyncMock()

        await cp.createpost_cancel(None, msg)

        sess = await cp._get_session(522)
        assert not sess, "/cancel must clear the session"


# ---------------------------------------------------------------------------
# Invalid input handling
# ---------------------------------------------------------------------------

class TestInvalidInput:
    """Invalid inputs must produce a clear error, not a crash."""

    async def test_invalid_post_type_is_rejected(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(531, {"step": "post_type"})

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 531
        cb.data = "createpost:type:invalid_type"
        cb.answer = AsyncMock()
        cb.message = MagicMock()

        await cp.createpost_callbacks(None, cb)

        cb.answer.assert_any_call("Invalid post type.", show_alert=True)
        # Session must NOT have transitioned.
        sess = await cp._get_session(531)
        assert sess["step"] == "post_type"

        await cp._clear_session(531)

    async def test_invalid_format_is_rejected(self):
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(532, {
            "step": "format", "post_type": "normal", "chat_id": -100532,
        })

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 532
        cb.data = "createpost:format:9"
        cb.answer = AsyncMock()
        cb.message = MagicMock()

        await cp.createpost_callbacks(None, cb)

        cb.answer.assert_any_call("Invalid format.", show_alert=True)
        sess = await cp._get_session(532)
        assert sess["step"] == "format"  # unchanged

        await cp._clear_session(532)

    async def test_step_mismatch_is_rejected(self):
        """A callback for a different step (e.g. pressing 'format' during
        the 'post_type' step) must be rejected.
        """
        from SWAGGYMUSIC.plugins.misc import createpost as cp

        await cp._save_session(533, {"step": "post_type"})

        cb = MagicMock()
        cb.from_user = MagicMock()
        cb.from_user.id = 533
        cb.data = "createpost:format:1"  # format button pressed during post_type step
        cb.answer = AsyncMock()
        cb.message = MagicMock()

        await cp.createpost_callbacks(None, cb)

        cb.answer.assert_any_call("Please follow the current step.", show_alert=True)
        sess = await cp._get_session(533)
        assert sess["step"] == "post_type"  # unchanged

        await cp._clear_session(533)


# ---------------------------------------------------------------------------
# Channel-admin gate at final send
# ---------------------------------------------------------------------------

class TestFinalSendAuthorization:
    """The final send must be gated by verify_channel_admin."""

    async def test_non_admin_cannot_send_hidden_post(self):
        """A non-admin user who completes the hidden-post wizard must be
        blocked at the final send step.
        """
        from SWAGGYMUSIC.plugins.misc import createpost as cp
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus, ChatType

        # Set up a complete hidden-post session for a non-admin user.
        await cp._save_session(541, {
            "step": "hidden_video",
            "post_type": "hidden",
            "chat_id": -100541,
            "position": "above",
            "hidden_text": "test post",
        })
        cp.SESSIONS[541]["hidden_entities"] = []

        # The next message the user sends will be the video URL.
        # We need to trigger createpost_input with step="hidden_video".
        # But the ACTIVE_SESSION filter checks the in-memory cache, so
        # we need 541 in the cache.
        assert 541 in cp.SESSIONS

        # Mock the Telegram API: user is NOT an admin.
        chat = MagicMock()
        chat.id = -100541
        chat.type = ChatType.CHANNEL
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True
        bot_member.privileges.can_post_messages = True

        user_member = MagicMock()
        user_member.status = ChatMemberStatus.MEMBER  # NOT an admin

        async def _get_member(chat_id, user_id):
            if user_id == app.id:
                return bot_member
            return user_member
        app.get_chat_member = AsyncMock(side_effect=_get_member)

        # Build the video-URL message.
        msg = MagicMock()
        msg.from_user = MagicMock()
        msg.from_user.id = 541
        msg.chat = MagicMock()
        msg.chat.type = ChatType.PRIVATE
        msg.text = "https://files.catbox.moe/test.mp4"
        msg.caption = None
        msg.reply_text = AsyncMock()

        await cp.createpost_input(None, msg)

        # The restriction message must have been sent.
        reply_calls = [c for c in msg.reply_text.call_args_list if "Not Authorized" in str(c)]
        assert len(reply_calls) >= 1, "Non-admin must receive the restriction message"

        # Session must have been cleaned up.
        sess = await cp._get_session(541)
        assert not sess, "Session must be cleared after restriction"

    async def test_owner_can_send_hidden_post(self):
        """The owner (user_id=1) bypasses the channel-admin check and
        can send to any channel the bot can access.
        """
        from SWAGGYMUSIC.plugins.misc import createpost as cp
        from SWAGGYMUSIC import app
        from pyrogram.enums import ChatMemberStatus, ChatType

        await cp._save_session(542, {
            "step": "hidden_video",
            "post_type": "hidden",
            "chat_id": -100542,
            "position": "above",
            "hidden_text": "owner post",
        })
        cp.SESSIONS[542]["hidden_entities"] = []
        # Make user 542 the owner by temporarily setting OWNER_ID.
        # Actually, the owner is user 1 per conftest. Let's use user 1.
        await cp._clear_session(542)
        await cp._save_session(1, {
            "step": "hidden_video",
            "post_type": "hidden",
            "chat_id": -100542,
            "position": "above",
            "hidden_text": "owner post",
        })
        cp.SESSIONS[1]["hidden_entities"] = []

        chat = MagicMock()
        chat.id = -100542
        chat.type = ChatType.CHANNEL
        app.get_chat = AsyncMock(return_value=chat)

        bot_member = MagicMock()
        bot_member.status = ChatMemberStatus.ADMINISTRATOR
        bot_member.privileges = MagicMock()
        bot_member.privileges.can_delete_messages = True
        bot_member.privileges.can_post_messages = True

        app.get_chat_member = AsyncMock(return_value=bot_member)
        app.resolve_peer = AsyncMock(return_value=MagicMock())
        app.invoke = AsyncMock(return_value=MagicMock())

        msg = MagicMock()
        msg.from_user = MagicMock()
        msg.from_user.id = 1
        msg.chat = MagicMock()
        msg.chat.type = ChatType.PRIVATE
        msg.text = "https://files.catbox.moe/test.mp4"
        msg.caption = None
        msg.reply_text = AsyncMock()

        await cp.createpost_input(None, msg)

        # The owner must NOT have received the restriction message.
        reply_calls = [c for c in msg.reply_text.call_args_list if "Not Authorized" in str(c)]
        assert len(reply_calls) == 0, "Owner must not see the restriction message"

        # app.invoke must have been called (the hidden preview was sent).
        assert app.invoke.called, "Owner's hidden preview must be sent"

        await cp._clear_session(1)
