"""Regression tests: /setgap after MANUAL deletion of accepted posts.

These tests drive the real channel-post handler
(``autodelete_new_channel_post``) against ``FakeBotChannel`` — a small
in-memory channel that behaves the way Telegram behaves for a **bot**
account:

* ``get_messages`` works and returns an *empty* message for a deleted id;
* ``delete_messages`` returns how many messages were really deleted
  (``pts_count`` semantics — 0 when the post was already gone);
* ``get_chat_history`` is NOT available to bots and raises
  ``BOT_METHOD_INVALID`` (this is what made the previous fallback lookup
  fail in production, so a manually deleted post kept blocking the gap).

Rules under test (from the bug report):
 1. Only accepted posts update the gap timer.
 2. If the latest accepted post is manually deleted, the gap is measured
    from the most recent earlier surviving accepted post ...
 3. ... using that post's ORIGINAL Telegram timestamp.
 4. Gap 15m: A 10:00, B 10:15, B deleted manually, C 10:17 -> C allowed.
 5. Rejected posts and edited posts never reset the timer.
 6. MongoDB persistence, existing commands and /setdelay keep working.
 7. Telegram API errors and concurrent posts are handled safely.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from gap_harness import (
    GAP,
    at,
    enable_delay,
    enable_gap,
    history_entry,
    history_ids,
    incoming,
    job_count,
    mock_channel_message,
    publish,
    run_worker_until,
    settings,
    ts,
)
from pyrogram.errors import FloodWait, MessageDeleteForbidden

pytestmark = pytest.mark.asyncio

# Fixtures ``channel`` (FakeBotChannel wired into app) and ``clock`` live in
# conftest.py; the fake channel itself is in tests/gap_harness.py.


# ---------------------------------------------------------------------------
# 1-4. Manual deletion of the latest accepted post
# ---------------------------------------------------------------------------

class TestManualDeletionFallback:
    async def test_user_example_latest_deleted_earlier_post_survives(self, channel):
        """Gap 15m. A 10:00, B 10:15. B deleted manually. C 10:17 -> ALLOWED."""
        await enable_gap(channel.chat_id)
        assert await publish(channel, 101, at(0)) is True    # A
        assert await publish(channel, 102, at(15)) is True   # B (exact boundary)

        channel.delete_manually(102)
        assert await publish(channel, 103, at(17)) is True, (
            "C must be allowed: B was deleted manually and A (10:00) still exists"
        )

        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 103
        assert doc["last_gap_post_at"] == ts(at(17))
        assert 102 not in history_ids(doc), "manually deleted post must leave the history"
        # The gap now runs from C again.
        assert await publish(channel, 104, at(20)) is False

    async def test_reference_rolls_back_to_original_timestamp_of_earlier_post(self, channel):
        """The repaired reference is A's ORIGINAL Telegram timestamp, not the
        deleted post's time and not the time the deletion was noticed."""
        from SWAGGYMUSIC.plugins.misc.autodelete import _resolve_stale_gap_reference

        await enable_gap(channel.chat_id)
        assert await publish(channel, 201, at(0)) is True
        assert await publish(channel, 202, at(15)) is True
        channel.delete_manually(202)

        await _resolve_stale_gap_reference(
            channel.chat_id, incoming(channel.chat_id, 203), GAP, ts(at(17)),
        )
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 201
        assert doc["last_gap_post_at"] == ts(at(0))
        assert history_ids(doc) == [201]

    async def test_surviving_earlier_post_inside_window_still_blocks(self, channel):
        """When the fallback post itself is inside the gap window, the new post
        is measured from it and rejected. (History seeded as if B had been
        accepted under an older 10-minute gap.)"""
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        channel.new_post(301, at(0))
        channel.new_post(302, at(10))
        await settings_db.update_one(
            {"chat_id": channel.chat_id},
            {"$set": {
                "last_gap_post_at": ts(at(10)), "last_gap_post_msg_id": 302,
                "gap_history": [
                    {"msg_id": 301, "post_at": ts(at(0))},
                    {"msg_id": 302, "post_at": ts(at(10))},
                ],
            }},
        )
        channel.delete_manually(302)

        assert await publish(channel, 303, at(12)) is False, "12 min after A (10:00) < 15 min"
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 301
        assert doc["last_gap_post_at"] == ts(at(0))
        assert await publish(channel, 304, at(15)) is True, "15 min after A -> allowed"

    async def test_deleting_middle_post_does_not_reopen_the_gap(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 351, at(0)) is True
        assert await publish(channel, 352, at(15)) is True
        assert await publish(channel, 353, at(30)) is True
        channel.delete_manually(352)  # not the latest one
        assert await publish(channel, 354, at(35)) is False, "C (10:30) still exists"

    async def test_several_latest_posts_deleted(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 361, at(0)) is True
        assert await publish(channel, 362, at(15)) is True
        assert await publish(channel, 363, at(30)) is True
        channel.delete_manually(363)
        channel.delete_manually(362)
        assert await publish(channel, 364, at(31)) is True

    async def test_no_surviving_accepted_post_allows_next_post(self, channel):
        """The only accepted post was deleted manually: nothing left to measure from."""
        await enable_gap(channel.chat_id)
        assert await publish(channel, 401, at(0)) is True
        channel.delete_manually(401)
        assert await publish(channel, 402, at(5)) is True
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 402
        assert history_ids(doc) == [402]

    async def test_missing_reference_is_rebuilt_from_history(self, channel):
        """Self-healing: history present but the reference fields missing
        (e.g. a hand-edited document) -> the newest accepted post that still
        exists becomes the reference again instead of letting a post through."""
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        channel.new_post(481, at(0))
        await settings_db.update_one(
            {"chat_id": channel.chat_id},
            {"$set": {"gap_history": [{"msg_id": 481, "post_at": ts(at(0))}]}},
        )
        assert await publish(channel, 482, at(5)) is False
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 481
        assert doc["last_gap_post_at"] == ts(at(0))

    async def test_no_telegram_lookup_when_reference_cannot_block(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 451, at(0)) is True
        assert await publish(channel, 452, at(20)) is True
        assert channel.get_messages_calls == [], "no API call needed outside the gap window"


# ---------------------------------------------------------------------------
# 5. Only accepted posts are references; rejected/edited posts never reset
# ---------------------------------------------------------------------------

class TestOnlyAcceptedPostsCount:
    async def test_rejected_posts_never_reset_or_extend_the_timer(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 501, at(0)) is True
        for msg_id, minute in ((502, 3), (503, 10), (504, 14.98)):
            assert await publish(channel, msg_id, at(minute)) is False
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_at"] == ts(at(0))
        assert history_ids(doc) == [501]
        assert await publish(channel, 505, at(15)) is True
        assert history_ids(await settings(channel.chat_id)) == [501, 505]

    async def test_rejected_post_that_survived_deletion_is_not_eligible(self, channel):
        """A rejected post whose deletion failed stays in the channel, but it is
        NOT an accepted post and must never become the gap reference."""
        await enable_gap(channel.chat_id)
        assert await publish(channel, 551, at(0)) is True
        channel.delete_error = MessageDeleteForbidden()
        assert await publish(channel, 552, at(5)) is True  # survived: delete failed
        channel.delete_error = None
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 551 and history_ids(doc) == [551]

        channel.delete_manually(551)
        # 552 (10:05) still exists but was rejected -> not a reference.
        assert await publish(channel, 553, at(8)) is True

    async def test_edited_post_never_resets_timer(self, channel):
        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        assert await publish(channel, 601, at(0)) is True
        assert await publish(channel, 602, at(15)) is True

        await ad.autodelete_new_channel_post(app, channel.edited(601))
        await ad.autodelete_new_channel_post(app, channel.edited(602))
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 602
        assert doc["last_gap_post_at"] == ts(at(15))
        assert history_ids(doc) == [601, 602]
        assert channel.alive(601) and channel.alive(602)

    async def test_redelivered_accepted_post_is_never_deleted(self, channel):
        """Defence in depth: if an edit/duplicate arrives WITHOUT edit_date, an
        already-accepted post must not be re-judged (and deleted) against the
        newer reference."""
        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        assert await publish(channel, 651, at(0)) is True
        assert await publish(channel, 652, at(15)) is True
        await ad.autodelete_new_channel_post(app, channel.edited(651, with_edit_date=False))
        assert channel.alive(651), "accepted post A was deleted by its own re-delivery"
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 652


# ---------------------------------------------------------------------------
# 6. /setdelay interaction, persistence, commands
# ---------------------------------------------------------------------------

class TestSetDelayAndPersistence:
    async def test_setdelay_deletion_does_not_release_the_gap(self, channel, monkeypatch):
        """A post deleted by the bot's own /setdelay job — confirmed by the
        worker — still counts for the gap."""
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await publish(channel, 701, at(0)) is True

        async def done():
            return not await job_count(channel.chat_id, 701)

        await run_worker_until(monkeypatch, done)  # the real /setdelay worker
        assert channel.bot_deleted == [701]
        assert history_entry(await settings(channel.chat_id), 701)["bot_delete"] == "confirmed"
        assert await publish(channel, 702, at(7)) is False
        assert await publish(channel, 703, at(15)) is True

    async def test_manual_deletion_before_setdelay_deadline_releases_gap(self, channel, clock):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await publish(channel, 751, at(0)) is True
        channel.delete_manually(751)
        clock(at(3))
        assert await publish(channel, 752, at(3)) is True

    async def test_setdelay_worker_deletes_due_post(self, channel, monkeypatch):
        """Regression: the worker's find() projection omitted ``delete_at``,
        so its atomic claim filtered on {"delete_at": None}, never matched,
        and no /setdelay job was ever executed."""
        await enable_delay(channel.chat_id, 60)
        assert await publish(channel, 771, at(0)) is True
        assert await job_count(channel.chat_id, 771) == 1

        async def done():
            return not await job_count(channel.chat_id, 771)

        await run_worker_until(monkeypatch, done)
        assert channel.bot_deleted == [771]
        assert not channel.alive(771)

    async def test_setdelay_worker_detects_post_already_deleted_manually(
        self, channel, clock, monkeypatch,
    ):
        """Manual deletion before the /setdelay deadline, noticed only by the
        worker (Telegram deletes 0 messages) -> treated as manual deletion."""
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await publish(channel, 781, at(0)) is True
        assert await job_count(channel.chat_id, 781) == 1
        channel.delete_manually(781)

        async def done():
            return not await job_count(channel.chat_id, 781)

        await run_worker_until(monkeypatch, done)
        assert channel.bot_deleted == []  # Telegram had nothing left to delete

        clock(at(7))
        assert await publish(channel, 782, at(7)) is True

    async def test_setdelay_jobs_still_queued_for_accepted_posts_only(self, channel):
        from SWAGGYMUSIC.utils.database import jobs_db

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 60)
        assert await publish(channel, 801, at(0)) is True
        assert await publish(channel, 802, at(1)) is False
        jobs = await jobs_db.find({"chat_id": channel.chat_id}).to_list(None)
        assert [(j["message_id"], j["delete_at"]) for j in jobs] == [(801, ts(at(1)))]

    async def test_history_is_persisted_and_survives_restart(self, channel):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        assert await publish(channel, 851, at(0)) is True
        assert await publish(channel, 852, at(15)) is True
        doc = await settings(channel.chat_id)
        assert [(e["msg_id"], e["post_at"]) for e in doc["gap_history"]] == [
            (851, ts(at(0))), (852, ts(at(15))),
        ]
        ad._GAP_LOCKS.clear()  # "restart": no in-process state survives
        channel.delete_manually(852)
        assert await publish(channel, 853, at(16)) is True

    async def test_legacy_settings_without_history(self, channel):
        """Documents written before this fix only have last_gap_post_*."""
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        channel.new_post(901, at(0))
        await settings_db.update_one(
            {"chat_id": channel.chat_id},
            {"$set": {"last_gap_post_at": ts(at(0)), "last_gap_post_msg_id": 901}},
        )
        assert await publish(channel, 902, at(5)) is False  # legacy ref still exists
        channel.delete_manually(901)
        assert await publish(channel, 903, at(6)) is True
        assert history_ids(await settings(channel.chat_id)) == [903]

    async def test_setgap_enable_update_disable_reset_history(self, channel, monkeypatch):
        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.bot import setconfig as sc
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        monkeypatch.setattr(ad, "_delete_after", AsyncMock())
        monkeypatch.setattr(ad, "_start_worker", AsyncMock())
        await enable_gap(channel.chat_id)
        assert await publish(channel, 951, at(0)) is True

        cmd = mock_channel_message(channel.chat_id, 952, at(1), "/setgap 10m")
        await ad.setgap_handler(app, cmd)
        doc = await settings(channel.chat_id)
        assert doc["gap"] == 600
        assert "gap_history" not in doc and "last_gap_post_at" not in doc

        assert await publish(channel, 953, at(2)) is True
        assert await sc._do_enable("setgap", channel.chat_id, 300) == "UPDATED"
        doc = await settings(channel.chat_id)
        assert "gap_history" not in doc and "last_gap_post_msg_id" not in doc

        assert await publish(channel, 954, at(3)) is True
        await enable_delay(channel.chat_id, 60)
        assert await sc._do_disable("setgap", channel.chat_id) is True
        doc = await settings(channel.chat_id)
        assert "gap_history" not in doc and "gap_enabled" not in doc
        assert doc["enabled"] is True and doc["delay"] == 60


# ---------------------------------------------------------------------------
# 7. Telegram API errors and concurrency
# ---------------------------------------------------------------------------

class TestErrorsAndConcurrency:
    async def test_api_error_while_verifying_keeps_the_timer(self, channel):
        """An unverifiable reference is NOT treated as deleted."""
        await enable_gap(channel.chat_id)
        assert await publish(channel, 1001, at(0)) is True
        channel.get_messages_error = RuntimeError("network down")
        assert await publish(channel, 1002, at(5)) is False
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 1001 and history_ids(doc) == [1001]

        channel.get_messages_error = None
        channel.delete_manually(1001)
        assert await publish(channel, 1003, at(6)) is True

    async def test_floodwait_while_verifying_keeps_the_timer(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 1011, at(0)) is True
        channel.delete_manually(1011)
        channel.get_messages_error = FloodWait(value=120)
        assert await publish(channel, 1012, at(5)) is False
        assert (await settings(channel.chat_id))["last_gap_post_msg_id"] == 1011

    async def test_hanging_verification_is_bounded(self, channel, monkeypatch):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        monkeypatch.setattr(ad, "GAP_VERIFY_TIMEOUT", 0.05, raising=False)
        await enable_gap(channel.chat_id)
        assert await publish(channel, 1021, at(0)) is True
        channel.get_messages_delay = 30  # e.g. Kurigram sleeping through a FloodWait
        result = await asyncio.wait_for(publish(channel, 1022, at(5)), timeout=5)
        assert result is False

    async def test_floodwait_on_rejected_post_delete_is_retried_later(
        self, channel, clock, monkeypatch,
    ):
        from SWAGGYMUSIC.utils.database import jobs_db

        await enable_gap(channel.chat_id)
        assert await publish(channel, 1031, at(0)) is True
        channel.delete_error = FloodWait(value=42)
        clock(at(5))
        assert await publish(channel, 1032, at(5)) is True  # not deleted yet ...
        job = await jobs_db.find_one({"chat_id": channel.chat_id, "message_id": 1032})
        assert job is not None, "... but a persistent retry deletion must be queued"
        assert job.get("reason") == "GAP_REJECT_RETRY"
        assert job["delete_at"] == ts(at(5)) + 42
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 1031 and history_ids(doc) == [1031]

        # Once the FloodWait is over, the worker removes the rejected post.
        channel.delete_error = None

        async def done():
            return not await job_count(channel.chat_id, 1032)

        await run_worker_until(monkeypatch, done)
        assert not channel.alive(1032) and channel.alive(1031)
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 1031 and history_ids(doc) == [1031]

    async def test_concurrent_posts_after_manual_deletion_only_one_wins(self, channel):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 1101, at(0)) is True
        assert await publish(channel, 1102, at(15)) is True
        channel.delete_manually(1102)
        channel.get_messages_delay = 0.01  # force interleaving

        results = await asyncio.gather(
            publish(channel, 1103, at(17)),
            publish(channel, 1104, at(17)),
            publish(channel, 1105, at(17, 1)),
        )
        assert sum(results) == 1, f"exactly one concurrent post may win, got {results}"
        doc = await settings(channel.chat_id)
        winner = [mid for mid, ok in zip((1103, 1104, 1105), results, strict=True) if ok][0]
        assert doc["last_gap_post_msg_id"] == winner
        assert history_ids(doc) == [1101, winner]

    async def test_concurrent_first_posts_only_one_wins(self, channel):
        await enable_gap(channel.chat_id)
        results = await asyncio.gather(*(publish(channel, 1200 + i, at(0, i)) for i in range(5)))
        assert sum(results) == 1, results


# ---------------------------------------------------------------------------
# Compatibility with the real Kurigram API (no network)
# ---------------------------------------------------------------------------

class TestKurigramCompatibility:
    async def test_calls_match_real_client_signatures(self):
        import inspect

        from pyrogram import Client

        inspect.signature(Client.get_messages).bind(None, -1001, 5, replies=0)
        inspect.signature(Client.delete_messages).bind(None, -1001, 5)

    async def test_real_message_objects_are_classified(self, monkeypatch):
        from pyrogram import types

        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        by_id = {
            1: types.Message(id=1, date=at(0)),
            2: types.Message(id=2, empty=True),  # what Kurigram builds from MessageEmpty
            3: None,                             # single id, nothing returned
        }

        async def get_messages(chat_id, message_ids, replies=1):
            return by_id[message_ids]

        monkeypatch.setattr(app, "get_messages", get_messages)
        assert await ad._telegram_message_state(-1001, 1) == ad.MSG_EXISTS
        assert await ad._telegram_message_state(-1001, 2) == ad.MSG_DELETED
        assert await ad._telegram_message_state(-1001, 3) == ad.MSG_DELETED
