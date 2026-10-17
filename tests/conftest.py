"""Pytest configuration + shared fixtures for the SWAGGYHELPER test suite.

This conftest stubs out the heavyweight runtime singletons (the Pyrogram
client, the config module, the SUDOERS filter) so the unit tests can
import the project modules WITHOUT a real Telegram connection or a real
MongoDB. Database tests use mongomock-motor (an in-memory MongoDB
replacement that supports the Motor async API).

No real Telegram API calls are made. No real MongoDB is required.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# 1. Stub `config` BEFORE any project module is imported.
# ---------------------------------------------------------------------------
# The project's `config.py` reads from env vars at import time and the
# `core/bot.py` constructor needs API_ID/API_HASH/BOT_TOKEN. We inject a
# stub module with safe defaults so the import chain succeeds without a
# real .env file.

_STUB_CONFIG = types.ModuleType("config")
_STUB_CONFIG.API_ID = 12345
_STUB_CONFIG.API_HASH = "stub_api_hash_value_for_tests_only"
_STUB_CONFIG.BOT_TOKEN = "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # FAKE — test stub only
_STUB_CONFIG.MONGO_DB_URI = "mongodb://stub:stub@localhost:27017/"
_STUB_CONFIG.OWNER_ID = 1
_STUB_CONFIG.LOGGER_ID = 0  # 0 = activity logging disabled
_STUB_CONFIG.SUPPORT_CHAT = "https://t.me/stub_support"
_STUB_CONFIG.SUPPORT_CHANNEL = "https://t.me/stub_channel"
_STUB_CONFIG.HEROKU_API_KEY = ""
_STUB_CONFIG.HEROKU_APP_NAME = ""
# Same type as the real config (a Kurigram user filter): the Start/Help
# plugins use ``~BANNED_USERS`` in their handler filters.
from pyrogram import filters as _pyro_filters  # noqa: E402
_STUB_CONFIG.BANNED_USERS = _pyro_filters.user()
_STUB_CONFIG.START_IMG_URL = "https://example.com/stub.jpg"
_STUB_CONFIG.PING_IMG_URL = "https://example.com/stub.jpg"
_STUB_CONFIG.PRIVACY_LINK = "https://example.com/privacy"
_STUB_CONFIG.RANDOM_THUMBS = [_STUB_CONFIG.START_IMG_URL]
_STUB_CONFIG.DEBUG_IGNORE_LOG = True
_STUB_CONFIG.adminlist = {}
_STUB_CONFIG.confirmer = {}

def _stub_get_thumb():
    return _STUB_CONFIG.START_IMG_URL

_STUB_CONFIG.get_thumb = _stub_get_thumb

def _stub_time_to_seconds(time):
    parts = str(time).split(":")
    return sum(int(x) * 60**i for i, x in enumerate(reversed(parts)))

_STUB_CONFIG.time_to_seconds = _stub_time_to_seconds

def _stub_validate_config_for_startup():
    """No-op stub for the startup config validator (tests bypass it)."""
    pass

_STUB_CONFIG.validate_config_for_startup = _stub_validate_config_for_startup

sys.modules["config"] = _STUB_CONFIG

# ---------------------------------------------------------------------------
# 2. Make the project importable.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
# Local user-site packages (motor, pymongo, kurigram, etc.)
sys.path.insert(0, "/home/z/.local/lib/python3.13/site-packages")

# ---------------------------------------------------------------------------
# 3. Stub the Pyrogram client BEFORE importing SWAGGYMUSIC (which
#    constructs `app = Alone()` at package import time).
# ---------------------------------------------------------------------------
# We replace the `Alone` client with a MagicMock so `app.start()`,
# `app.send_message()`, etc. are no-ops. Individual tests can override
# specific methods on `app` as needed.

import logging  # noqa: E402

# Configure logging for tests — capture to a buffer so test output is clean.
logging.basicConfig(level=logging.WARNING)

# Stub the SWAGGYMUSIC package's `app` and `LOGGER` before they're
# constructed by `Alone()`. We do this by monkey-patching
# `SWAGGYMUSIC.core.bot.Alone.__init__` to a no-op AFTER the module is
# imported but BEFORE `app = Alone()` runs. The cleanest way is to
# pre-inject a stub `app` into `sys.modules` under the name
# `SWAGGYMUSIC`... but that's fragile. Instead, we let the real import
# happen and patch `app` afterwards.

# The import below triggers `app = Alone()` which calls `super().__init__()`
# with the stub config. Kurigram's Client.__init__ is tolerant of stub
# values (it doesn't connect until .start() is called), so this works.
import SWAGGYMUSIC  # noqa: E402  -- triggers app = Alone()
from SWAGGYMUSIC import app  # noqa: E402

# Replace app's network methods with AsyncMock so no real Telegram calls happen.
def _existing_message(message_id):
    """A Message-like mock for a message that still exists in the chat."""
    msg = MagicMock()
    msg.id = message_id
    msg.empty = None
    msg.date = datetime.now(timezone.utc)
    return msg


async def _default_get_messages(chat_id=None, message_ids=None, *args, **kwargs):
    """Default get_messages: every requested message still exists.

    (A bare AsyncMock returned a MagicMock whose truthy ``.empty`` made every
    message look DELETED.) Tests that need a deleted message override
    ``app.get_messages`` themselves.
    """
    if message_ids is None or isinstance(message_ids, int):
        return _existing_message(message_ids)
    return [_existing_message(mid) for mid in message_ids]


def _install_default_app_mocks():
    app.get_chat = AsyncMock()
    app.get_chat_member = AsyncMock()
    app.get_messages = AsyncMock(side_effect=_default_get_messages)
    app.get_users = AsyncMock()
    app.delete_messages = AsyncMock()
    app.send_message = AsyncMock()
    app.send_photo = AsyncMock()
    app.reply_text = AsyncMock()
    app.resolve_peer = AsyncMock(return_value=MagicMock())
    app.invoke = AsyncMock()
    app.get_chat_history = MagicMock()  # returns an async iterator; tests set side_effect


_install_default_app_mocks()

# `app.id` and `app.username` are used by handlers.
app.id = 9999999
app.username = "stub_bot"
app.name = "Stub Bot"
app.mention = "@stub_bot"

# ---------------------------------------------------------------------------
# 4. In-memory MongoDB via mongomock-motor.
# ---------------------------------------------------------------------------
# We replace `SWAGGYMUSIC.core.mongo.mongodb` (and the derived collections
# in `SWAGGYMUSIC.utils.database`) with mongomock-motor instances so DB
# tests run without a real MongoDB.

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

_mock_mongo_client = AsyncMongoMockClient()
_mock_mongodb = _mock_mongo_client["Anon"]

# Patch the module-level `mongodb` handle in core.mongo.
import SWAGGYMUSIC.core.mongo as _core_mongo  # noqa: E402
_core_mongo.mongodb = _mock_mongodb
# Also patch the `_mongo_async_` client so verify_mongo_connection works.
_mock_admin = MagicMock()
_mock_admin.command = AsyncMock(return_value={"ok": 1})
_mock_mongo_client.admin = _mock_admin
_core_mongo._mongo_async_ = _mock_mongo_client

# Re-bind the collection handles in database.py to the mock DB.
import SWAGGYMUSIC.utils.database as _db_mod  # noqa: E402
_db_mod.settings_db = _mock_mongodb.autodelete_settings
_db_mod.jobs_db = _mock_mongodb.autodelete_jobs
# Re-bind all the other collections too.
_db_mod.authdb = _mock_mongodb.adminauth
_db_mod.authuserdb = _mock_mongodb.authuser
_db_mod.blacklist_chatdb = _mock_mongodb.blacklistChat
_db_mod.blockeddb = _mock_mongodb.blockedusers
_db_mod.chatsdb = _mock_mongodb.chats
_db_mod.gbansdb = _mock_mongodb.gban
_db_mod.langdb = _mock_mongodb.language
_db_mod.onoffdb = _mock_mongodb.onoffper
_db_mod.sudoersdb = _mock_mongodb.sudoers
_db_mod.usersdb = _mock_mongodb.tgusersdb

# Re-bind the createpost_sessions collection.
import SWAGGYMUSIC.utils.createpost_sessions as _cps_mod  # noqa: E402
_cps_mod.sessions_db = _mock_mongodb.createpost_sessions

# Re-bind settings_db / jobs_db in autodelete.py and setconfig.py (they
# imported the handles at module load time, so we need to patch the
# references in those modules too).
import SWAGGYMUSIC.plugins.misc.autodelete as _ad  # noqa: E402
_ad.settings_db = _mock_mongodb.autodelete_settings
_ad.jobs_db = _mock_mongodb.autodelete_jobs

import SWAGGYMUSIC.plugins.bot.setconfig as _sc  # noqa: E402
_sc.settings_db = _mock_mongodb.autodelete_settings

# ---------------------------------------------------------------------------
# 5. SUDOERS filter — seed with the owner + a test sudoer.
# ---------------------------------------------------------------------------
from SWAGGYMUSIC.misc import SUDOERS  # noqa: E402
SUDOERS.add(1)  # owner
SUDOERS.add2 = lambda u: SUDOERS.add(u)  # helper for tests

# ---------------------------------------------------------------------------
# 6. Pytest fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

@pytest.fixture(autouse=True)
def _fresh_app_mocks():
    """Give every test fresh default Telegram API mocks.

    Many tests assign ``app.<method> = AsyncMock(...)`` directly and never
    restore it, so one test's "message deleted" / FloodWait mock leaked
    into later tests and made results depend on test order.
    """
    _install_default_app_mocks()
    yield
    _install_default_app_mocks()


@pytest.fixture
def mock_db():
    """Yield the in-memory MongoDB handle."""
    return _mock_mongodb


# Unique channel ids for FakeBotChannel-based tests (shared by all files).
# Real channel-format ids (-100…): gap_harness.tg_post derives the raw
# channel id from them to build genuine MTProto updates.
import itertools  # noqa: E402

_FAKE_CHANNEL_IDS = itertools.count(-1000000300101, -1)


@pytest.fixture
def channel(monkeypatch):
    """A FakeBotChannel (see tests/gap_harness.py) wired into ``app``."""
    from gap_harness import FakeBotChannel
    from pyrogram.enums import ChatMemberStatus

    fake = FakeBotChannel(next(_FAKE_CHANNEL_IDS))
    monkeypatch.setattr(app, "get_messages", fake.get_messages)
    monkeypatch.setattr(app, "delete_messages", fake.delete_messages)
    monkeypatch.setattr(app, "get_chat_history", fake.get_chat_history)
    admin = MagicMock()
    admin.status = ChatMemberStatus.ADMINISTRATOR
    admin.privileges = MagicMock()
    admin.privileges.can_delete_messages = True
    monkeypatch.setattr(app, "get_chat_member", AsyncMock(return_value=admin))
    return fake


@pytest.fixture
def clock(monkeypatch):
    """Pin the gap engine's wall clock (``autodelete._now``)."""
    import SWAGGYMUSIC.plugins.misc.autodelete as ad
    from gap_harness import at, ts

    state = {"now": ts(at(0))}

    def set_now(when) -> None:
        state["now"] = ts(when)

    monkeypatch.setattr(ad, "_now", lambda: state["now"])
    return set_now


# NOTE: An autouse cleanup fixture that calls delete_many({}) on mongomock
# has been intentionally REMOVED. mongomock-motor 0.0.36 has a bug where
# after delete_many({}) is called, subsequent insert_one/update_one calls
# that store datetime values silently fail (the document is "inserted"
# with an _id but then immediately lost — count_documents returns 0).
# This is a MONGOMOCK bug, not a bug in the project code.
#
# Instead of relying on cleanup fixtures, each test uses UNIQUE chat_id /
# message_id values (e.g. -100211, -100212, ...) so tests do not
# contaminate each other. This is the standard pattern for integration
# tests against a shared database.

@pytest.fixture
def clean_collections():
    """Drop all test collections before each test that uses this fixture."""
    for col_name in [
        "autodelete_settings", "autodelete_jobs", "createpost_sessions",
        "sudoers", "onoffper",
    ]:
        try:
            asyncio.get_event_loop().run_until_complete(
                _mock_mongodb[col_name].delete_many({})
            )
        except Exception:
            pass
    yield
    # Cleanup after test too.
    for col_name in [
        "autodelete_settings", "autodelete_jobs", "createpost_sessions",
    ]:
        try:
            asyncio.get_event_loop().run_until_complete(
                _mock_mongodb[col_name].delete_many({})
            )
        except Exception:
            pass


def make_message(
    *,
    chat_id: int = -1001234567890,
    message_id: int = 1,
    text: str = "",
    edit_date: Optional[datetime] = None,
    outgoing: bool = False,
    from_user_id: Optional[int] = None,
    chat_type: str = "channel",
    date: Optional[datetime] = None,
) -> MagicMock:
    """Build a Pyrogram-Message-like mock for handler tests."""
    if date is None:
        date = datetime.now(timezone.utc)

    chat = MagicMock()
    chat.id = chat_id
    if chat_type == "channel":
        # ChatType.CHANNEL enum value
        from pyrogram.enums import ChatType as _CT
        chat.type = _CT.CHANNEL
    elif chat_type == "private":
        from pyrogram.enums import ChatType as _CT
        chat.type = _CT.PRIVATE
    else:
        from pyrogram.enums import ChatType as _CT
        chat.type = _CT.GROUP

    msg = MagicMock()
    msg.chat = chat
    msg.id = message_id
    msg.text = text
    msg.caption = None
    msg.date = date
    msg.edit_date = edit_date
    msg.outgoing = outgoing
    msg.entities = []

    if from_user_id is not None:
        user = MagicMock()
        user.id = from_user_id
        user.first_name = "Test User"
        user.last_name = None
        user.username = None
        msg.from_user = user
    else:
        msg.from_user = None

    msg.reply_text = AsyncMock()
    msg.delete = AsyncMock()
    msg.reply_photo = AsyncMock()

    return msg


def make_callback_query(
    *,
    user_id: int = 1,
    data: str = "",
    message_text: str = "",
) -> MagicMock:
    """Build a CallbackQuery-like mock."""
    from pyrogram.enums import ChatType as _CT

    chat = MagicMock()
    chat.id = 1
    chat.type = _CT.PRIVATE

    msg = MagicMock()
    msg.chat = chat
    msg.text = message_text
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()

    user = MagicMock()
    user.id = user_id
    user.first_name = "Test"
    user.last_name = None
    user.username = None

    q = MagicMock()
    q.from_user = user
    q.message = msg
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()

    return q


def make_chat_member(
    *,
    status: str = "administrator",
    can_delete: bool = True,
    can_post: bool = True,
    user_id: Optional[int] = None,
) -> MagicMock:
    """Build a ChatMember-like mock."""
    from pyrogram.enums import ChatMemberStatus as _CMS

    member = MagicMock()
    if status == "owner":
        member.status = _CMS.OWNER
    elif status == "administrator":
        member.status = _CMS.ADMINISTRATOR
    elif status == "member":
        member.status = _CMS.MEMBER
    else:
        member.status = _CMS.MEMBER

    privs = MagicMock()
    privs.can_delete_messages = can_delete
    privs.can_post_messages = can_post
    member.privileges = privs if status != "owner" else None

    if user_id is not None:
        u = MagicMock()
        u.id = user_id
        member.user = u

    return member


@pytest.fixture
def msg_factory():
    return make_message


@pytest.fixture
def cb_factory():
    return make_callback_query


@pytest.fixture
def member_factory():
    return make_chat_member
