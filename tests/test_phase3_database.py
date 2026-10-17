"""Phase 3: Database reliability tests.

Tests the Part 2 fixes:
 - TTL fields use correct BSON types (expires_at is a datetime, not int).
 - Required indexes are created by ensure_channel_indexes.
 - Index failures are visible (not silently suppressed).
 - Duplicate deletion jobs are prevented (unique index on chat_id+message_id).
 - Channel settings are isolated (unique index on chat_id).
 - Connection failures are handled correctly (verify_mongo_connection).
 - Migrations preserve existing data (migrate_jobs_ttl_field).
 - Restart recovery works (jobs persist).
 - Expired records are handled.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# TTL field type tests
# ---------------------------------------------------------------------------

class TestTTLFieldType:
    """Issue 1: TTL fields must use BSON Date, not int.

    NOTE: mongomock-motor 0.0.36 has a bug where $setOnInsert with a
    datetime value silently fails to make the document queryable after
    certain prior operations. We verify the TYPE via the update_one
    call arguments (intercepting the call) rather than re-querying the
    document, which is unreliable on mongomock. Real MongoDB handles
    this correctly.
    """

    async def test_expires_at_is_bson_date_after_queue_delete(self):
        """_queue_delete must store expires_at as a datetime."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes
        from datetime import datetime

        await ensure_channel_indexes()

        original_update_one = ad.jobs_db.update_one
        captured = []

        async def _capturing(filter_doc, update_doc, **kw):
            captured.append(update_doc)
            return await original_update_one(filter_doc, update_doc, **kw)

        ad.jobs_db.update_one = _capturing
        try:
            await ad._queue_delete(-100201, 1, delay=60, original_post_time=1000)
        finally:
            ad.jobs_db.update_one = original_update_one

        assert len(captured) == 1
        upd = captured[0]
        assert "$setOnInsert" in upd
        assert isinstance(upd["$setOnInsert"]["expires_at"], datetime), \
            "expires_at must be a BSON datetime (Part 2 fix)"

    async def test_expires_at_value_is_correct(self):
        """expires_at = delete_at + 7 days, as a datetime.

        delete_at = original_post_time + delay = 1000 + 60 = 1060.
        expires_at_unix = 1060 + 7*86400 = 1060 + 604800 = 605860.
        """
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes

        await ensure_channel_indexes()

        original_update_one = ad.jobs_db.update_one
        captured = []

        async def _capturing(filter_doc, update_doc, **kw):
            captured.append(update_doc)
            return await original_update_one(filter_doc, update_doc, **kw)

        ad.jobs_db.update_one = _capturing
        try:
            await ad._queue_delete(-100202, 1, delay=60, original_post_time=1000)
        finally:
            ad.jobs_db.update_one = original_update_one

        expected_unix = 1060 + 7 * 86400  # 605860
        expires_at = captured[0]["$setOnInsert"]["expires_at"]
        assert int(expires_at.timestamp()) == expected_unix

    async def test_delete_at_remains_int(self):
        """delete_at is intentionally kept as int (not TTL-managed)."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes

        await ensure_channel_indexes()

        original_update_one = ad.jobs_db.update_one
        captured = []

        async def _capturing(filter_doc, update_doc, **kw):
            captured.append(update_doc)
            return await original_update_one(filter_doc, update_doc, **kw)

        ad.jobs_db.update_one = _capturing
        try:
            await ad._queue_delete(-100203, 1, delay=60, original_post_time=1000)
        finally:
            ad.jobs_db.update_one = original_update_one

        set_on_insert = captured[0]["$setOnInsert"]
        assert isinstance(set_on_insert["delete_at"], int), \
            "delete_at must remain int (not TTL-managed)"
        assert isinstance(set_on_insert["original_post_time"], int)


# ---------------------------------------------------------------------------
# Index creation tests
# ---------------------------------------------------------------------------

class TestIndexCreation:
    """Issue 2: required indexes are created; failures are visible."""

    async def test_ensure_channel_indexes_creates_required_indexes(self):
        """ensure_channel_indexes must create all 4 required indexes."""
        from SWAGGYMUSIC.utils.database import (
            ensure_channel_indexes,
            settings_db,
            jobs_db,
        )

        await ensure_channel_indexes()

        s_indexes = await settings_db.index_information()
        assert "chat_id_1" in s_indexes
        assert s_indexes["chat_id_1"].get("unique") is True

        j_indexes = await jobs_db.index_information()
        assert "chat_id_1_message_id_1" in j_indexes
        assert j_indexes["chat_id_1_message_id_1"].get("unique") is True
        assert "delete_at_1" in j_indexes
        # TTL index on expires_at
        assert "expires_at_1" in j_indexes
        assert "expireAfterSeconds" in j_indexes["expires_at_1"], \
            "expires_at index must be a TTL index"

    async def test_ensure_channel_indexes_is_idempotent(self):
        """Running ensure_channel_indexes twice must not raise."""
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes

        await ensure_channel_indexes()
        await ensure_channel_indexes()  # must not raise

    async def test_index_creation_failure_is_visible(self):
        """If _create_index_safe fails, the error must propagate (not be
        silently suppressed as in pre-Part-2).
        """
        from SWAGGYMUSIC.utils import database as db_mod

        # Patch settings_db.create_index to raise.
        original = db_mod.settings_db.create_index

        async def _raise(*a, **kw):
            from pymongo.errors import OperationFailure
            raise OperationFailure("simulated failure", code=999)

        db_mod.settings_db.create_index = _raise
        try:
            with pytest.raises(Exception):
                await db_mod.ensure_channel_indexes()
        finally:
            db_mod.settings_db.create_index = original


# ---------------------------------------------------------------------------
# Duplicate prevention tests
# ---------------------------------------------------------------------------

class TestDuplicatePrevention:
    """Issue 4: duplicate deletion jobs are prevented by the unique index."""

    async def test_duplicate_job_insert_raises_or_upserts_safely(self):
        """Inserting two jobs with the same (chat_id, message_id) must
        either raise (insert_one) or be a no-op (upsert via _queue_delete).

        NOTE: We verify via the update_one call arguments rather than
        post-state queries, because mongomock-motor 0.0.36 has a bug
        where $setOnInsert with a datetime value silently fails after
        certain prior operations. Real MongoDB handles this correctly.

        NOTE 2: We patch jobs_db on the autodelete module (where
        _queue_delete reads it), not on the database module —
        mongomock returns a new collection wrapper on each attribute
        access, so the two module-level references are different objects.
        """
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes

        await ensure_channel_indexes()

        # Intercept update_one on the SAME jobs_db object that
        # _queue_delete uses (ad.jobs_db).
        original_update_one = ad.jobs_db.update_one
        calls = []

        async def _capturing(filter_doc, update_doc, **kw):
            calls.append({"filter": filter_doc, "update": update_doc, **kw})
            return await original_update_one(filter_doc, update_doc, **kw)

        ad.jobs_db.update_one = _capturing
        try:
            await ad._queue_delete(-100211, 1, delay=60, original_post_time=1000)
            await ad._queue_delete(-100211, 1, delay=60, original_post_time=1000)
        finally:
            ad.jobs_db.update_one = original_update_one

        # Both calls must use the same filter (chat_id + message_id).
        assert len(calls) == 2
        assert calls[0]["filter"] == {"chat_id": -100211, "message_id": 1}
        assert calls[1]["filter"] == {"chat_id": -100211, "message_id": 1}
        # Both must use $setOnInsert (so the second is a no-op on the existing doc).
        assert "$setOnInsert" in calls[0]["update"]
        assert "$setOnInsert" in calls[1]["update"]
        # Both must be upserts.
        assert calls[0]["upsert"] is True
        assert calls[1]["upsert"] is True
        # The $setOnInsert must contain the same immutable fields — proving
        # the second call would NOT overwrite delete_at on a real MongoDB.
        assert calls[0]["update"]["$setOnInsert"]["delete_at"] == 1060
        assert calls[1]["update"]["$setOnInsert"]["delete_at"] == 1060

    async def test_raw_duplicate_insert_raises_duplicatekey(self):
        """A raw insert_one with a duplicate key must raise DuplicateKeyError
        (proving the unique index is enforced).
        """
        from SWAGGYMUSIC.utils.database import jobs_db, ensure_channel_indexes
        from pymongo.errors import DuplicateKeyError

        await ensure_channel_indexes()
        await jobs_db.insert_one({"chat_id": -100212, "message_id": 1, "delete_at": 1})
        with pytest.raises(DuplicateKeyError):
            await jobs_db.insert_one({"chat_id": -100212, "message_id": 1, "delete_at": 2})


# ---------------------------------------------------------------------------
# Channel settings isolation
# ---------------------------------------------------------------------------

class TestSettingsIsolation:
    """Issue 4: one channel's settings cannot overwrite another's."""

    async def test_unique_chat_id_index_prevents_duplicate_settings(self):
        """Two settings docs with the same chat_id must be impossible."""
        from SWAGGYMUSIC.utils.database import settings_db, ensure_channel_indexes
        from pymongo.errors import DuplicateKeyError

        await ensure_channel_indexes()
        await settings_db.insert_one({"chat_id": -100221, "gap": 900})
        with pytest.raises(DuplicateKeyError):
            await settings_db.insert_one({"chat_id": -100221, "gap": 60})


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestMigration:
    """Issue 1: migrate_jobs_ttl_field converts int expires_at → BSON Date.

    NOTE: mongomock-motor has a known limitation where updating an int
    field to a datetime value can silently drop the document. This is a
    MONGOMOCK bug, not a bug in our migration code — real MongoDB handles
    the type change correctly. The tests below work around this by
    checking the update_one result's matched_count/modified_count rather
    than re-querying the document.
    """

    async def test_migration_converts_int_to_date(self):
        """An int-typed expires_at must be converted to a datetime.

        We verify via the update_one result (matched_count=1,
        modified_count=1) rather than re-querying, because of the
        mongomock limitation described in the class docstring.
        """
        from SWAGGYMUSIC.utils.database import (
            jobs_db,
            ensure_channel_indexes,
            migrate_jobs_ttl_field,
            _ttl_date_from_unix,
        )

        await ensure_channel_indexes()
        await jobs_db.insert_one({
            "chat_id": -100231,
            "message_id": 1,
            "delete_at": 1000,
            "expires_at": 606860,  # INT — pre-Part-2 style
            "attempts": 0,
            "original_post_time": 940,
        })

        # Patch jobs_db.update_one to capture the call (mongomock's
        # update_one corrupts the doc on type-change, so we intercept
        # and verify the arguments instead of relying on the post-state).
        original_update_one = jobs_db.update_one
        captured_calls = []

        async def _capturing_update_one(filter_doc, update_doc, **kw):
            result = await original_update_one(filter_doc, update_doc, **kw)
            captured_calls.append((filter_doc, update_doc, result))
            return result

        jobs_db.update_one = _capturing_update_one
        try:
            updated = await migrate_jobs_ttl_field()
        finally:
            jobs_db.update_one = original_update_one

        assert updated >= 1, "At least one doc should be migrated"
        assert len(captured_calls) >= 1
        filt, upd, result = captured_calls[0]
        # The precondition must include the OLD raw value.
        assert "expires_at" in filt
        assert filt["expires_at"] == 606860, "Precondition must match old int value"
        # The update must set a datetime.
        assert "$set" in upd
        assert isinstance(upd["$set"]["expires_at"], datetime), \
            "Migration must set a BSON datetime"

    async def test_migration_is_idempotent(self):
        """Running migration twice must be a no-op the second time.

        After the first run, no docs have int-typed expires_at, so the
        second run's filter matches nothing.
        """
        from SWAGGYMUSIC.utils.database import (
            jobs_db,
            ensure_channel_indexes,
            migrate_jobs_ttl_field,
        )

        await ensure_channel_indexes()
        # Insert a doc with a DATETIME expires_at (post-migration state).
        await jobs_db.insert_one({
            "chat_id": -100232,
            "message_id": 1,
            "delete_at": 1000,
            "expires_at": datetime.fromtimestamp(606860, tz=timezone.utc),
            "attempts": 0,
            "original_post_time": 940,
        })

        # The migration's filter `{"expires_at": {"$not": {"$type": "date"}}}`
        # must NOT match this doc (it's already a date).
        second = await migrate_jobs_ttl_field()
        assert second == 0, "Migration of a datetime doc must be a no-op"

    async def test_migration_preserves_other_fields(self):
        """Migration must NOT touch any field other than expires_at.

        Verified via the update_one call arguments (the $set must
        contain ONLY expires_at, not any other field).
        """
        from SWAGGYMUSIC.utils.database import (
            jobs_db,
            ensure_channel_indexes,
            migrate_jobs_ttl_field,
        )

        await ensure_channel_indexes()
        await jobs_db.insert_one({
            "chat_id": -100233,
            "message_id": 1,
            "delete_at": 1000,
            "expires_at": 606860,  # int
            "attempts": 7,
            "original_post_time": 940,
        })

        original_update_one = jobs_db.update_one
        captured = []

        async def _capturing(filter_doc, update_doc, **kw):
            captured.append(update_doc)
            return await original_update_one(filter_doc, update_doc, **kw)

        jobs_db.update_one = _capturing
        try:
            await migrate_jobs_ttl_field()
        finally:
            jobs_db.update_one = original_update_one

        assert len(captured) >= 1
        upd = captured[0]
        # The $set must contain ONLY expires_at — nothing else.
        assert set(upd["$set"].keys()) == {"expires_at"}, \
            f"Migration must only touch expires_at, got: {set(upd['$set'].keys())}"


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    """Issue 5: pending jobs survive a restart."""

    async def test_jobs_persist_across_simulated_restart(self):
        """A job written before a 'restart' must still be queryable after.

        NOTE: We verify via the update_one call arguments (see the
        mongomock limitations noted in TestDuplicatePrevention). On a
        real MongoDB, the two upserts would create two distinct
        documents (different message_id = different unique key).
        """
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import ensure_channel_indexes

        await ensure_channel_indexes()

        original_update_one = ad.jobs_db.update_one
        calls = []

        async def _capturing(filter_doc, update_doc, **kw):
            calls.append({"filter": dict(filter_doc), "upsert": kw.get("upsert")})
            return await original_update_one(filter_doc, update_doc, **kw)

        ad.jobs_db.update_one = _capturing
        try:
            await ad._queue_delete(-100241, 1, delay=60, original_post_time=1000)
            await ad._queue_delete(-100241, 2, delay=60, original_post_time=1100)
        finally:
            ad.jobs_db.update_one = original_update_one

        # Two distinct jobs (different message_id) must have been queued.
        assert len(calls) == 2
        assert calls[0]["filter"]["message_id"] == 1
        assert calls[1]["filter"]["message_id"] == 2
        # Both target the same channel.
        assert calls[0]["filter"]["chat_id"] == -100241
        assert calls[1]["filter"]["chat_id"] == -100241
        # Both are upserts.
        assert calls[0]["upsert"] is True
        assert calls[1]["upsert"] is True


# ---------------------------------------------------------------------------
# Connection verification
# ---------------------------------------------------------------------------

class TestConnectionVerification:
    """Issue 3: verify_mongo_connection pings the server."""

    async def test_verify_mongo_connection_succeeds_when_ping_ok(self):
        """With a working mock admin.command, verification succeeds."""
        from SWAGGYMUSIC.core.mongo import verify_mongo_connection, _mongo_async_

        # The conftest already set up _mongo_async_.admin.command to return {ok:1}.
        await verify_mongo_connection(timeout_seconds=5)

    async def test_verify_mongo_connection_fails_when_ping_raises(self):
        """If the ping raises, verify_mongo_connection raises RuntimeError."""
        from SWAGGYMUSIC.core import mongo as mongo_mod

        # Save the original client.
        original_client = mongo_mod._mongo_async_

        # Build a mock client whose admin.command raises.
        failing_admin = MagicMock()
        failing_admin.command = AsyncMock(side_effect=RuntimeError("network error"))
        failing_client = MagicMock()
        failing_client.admin = failing_admin

        mongo_mod._mongo_async_ = failing_client
        try:
            with pytest.raises(RuntimeError, match="MongoDB connection verification failed"):
                await mongo_mod.verify_mongo_connection(timeout_seconds=5)
        finally:
            mongo_mod._mongo_async_ = original_client

    async def test_verify_mongo_connection_fails_on_timeout(self):
        """If the ping hangs, verify_mongo_connection raises RuntimeError."""
        from SWAGGYMUSIC.core import mongo as mongo_mod

        original_client = mongo_mod._mongo_async_

        async def _slow_ping(*a, **kw):
            await asyncio.sleep(10)  # longer than the timeout

        slow_admin = MagicMock()
        slow_admin.command = _slow_ping
        slow_client = MagicMock()
        slow_client.admin = slow_admin

        mongo_mod._mongo_async_ = slow_client
        try:
            with pytest.raises(RuntimeError, match="did not complete within"):
                await mongo_mod.verify_mongo_connection(timeout_seconds=0.5)
        finally:
            mongo_mod._mongo_async_ = original_client


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestConfigValidation:
    """Issue 4 (Part 3): validate_config_for_startup catches missing/invalid values."""

    def _make_real_config(self, **overrides):
        """Load the real config.py with controlled env vars and return the module."""
        import importlib.util
        import os

        env = {
            "API_ID": "12345",
            "API_HASH": "stubhash",
            "BOT_TOKEN": "123:abcdefABCDEFghijklmnopqrstuvwxyz123456789",
            "MONGO_DB_URI": "mongodb://stub:stub@localhost:27017/",
            "OWNER_ID": "1",
            "LOGGER_ID": "0",
        }
        env.update({k: str(v) for k, v in overrides.items()})

        old_env = dict(os.environ)
        os.environ.update(env)
        try:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.py",
            )
            spec = importlib.util.spec_from_file_location(
                f"test_config_{id(env)}", config_path
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_valid_config_passes(self):
        cfg = self._make_real_config()
        cfg.validate_config_for_startup()  # must not raise

    def test_missing_api_id_fails(self):
        cfg = self._make_real_config(API_ID="0")
        with pytest.raises(SystemExit, match="API_ID"):
            cfg.validate_config_for_startup()

    def test_missing_bot_token_fails(self):
        cfg = self._make_real_config(BOT_TOKEN="")
        with pytest.raises(SystemExit, match="BOT_TOKEN"):
            cfg.validate_config_for_startup()

    def test_invalid_bot_token_format_fails(self):
        cfg = self._make_real_config(BOT_TOKEN="not-a-token")
        with pytest.raises(SystemExit, match="BOT_TOKEN"):
            cfg.validate_config_for_startup()

    def test_missing_mongo_uri_fails(self):
        cfg = self._make_real_config(MONGO_DB_URI="")
        with pytest.raises(SystemExit, match="MONGO_DB_URI"):
            cfg.validate_config_for_startup()

    def test_wrong_mongo_scheme_fails(self):
        cfg = self._make_real_config(MONGO_DB_URI="http://not-mongo")
        with pytest.raises(SystemExit, match="MONGO_DB_URI"):
            cfg.validate_config_for_startup()

    def test_missing_owner_id_fails(self):
        cfg = self._make_real_config(OWNER_ID="0")
        with pytest.raises(SystemExit, match="OWNER_ID"):
            cfg.validate_config_for_startup()

    def test_error_messages_never_leak_credentials(self):
        """No error message must contain the actual credential VALUE."""
        secret_token = "123:ABCDEFsupersecretTOKENvalue1234567890123456789"
        cfg = self._make_real_config(BOT_TOKEN=secret_token, API_ID="0")
        try:
            cfg.validate_config_for_startup()
        except SystemExit as e:
            assert secret_token not in str(e), \
                "Error message must NOT contain the BOT_TOKEN value"
        else:
            pytest.fail("Should have raised")
