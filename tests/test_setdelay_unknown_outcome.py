"""/setdelay worker: a deletion whose outcome is UNKNOWN, followed by a
FloodWait (or a refusal) on the retry.

Attempt 1 reaches Telegram — the post IS deleted — but the answer is lost
(network error). The worker cannot know, so the accepted post's history
entry keeps ``bot_delete="pending"`` and the job keeps ``inflight``.
Attempt 2 is refused (FloodWait / RPC error): that refusal says nothing
about attempt 1, so neither the pending mark nor ``inflight`` may be
cleared. Otherwise the post (already gone) would look MANUALLY deleted:
/setgap would drop it from the history and fall back to an older reference,
and the next retry (Telegram: 0 deleted) would "confirm" a manual deletion.

All runs use the real worker (_delete_worker) against FakeBotChannel.
"""
from __future__ import annotations

import time

import pytest
from gap_harness import (
    at,
    dispatch,
    enable_delay,
    enable_gap,
    history_entry,
    history_ids,
    job_count,
    run_worker_until,
    settings,
    tg_post,
    ts,
)
from pyrogram.errors import FloodWait, InternalServerError, MessageDeleteForbidden

pytestmark = pytest.mark.asyncio

ADMIN = 4242


async def _job(chat_id, msg_id):
    from SWAGGYMUSIC.utils.database import jobs_db
    return await jobs_db.find_one({"chat_id": chat_id, "message_id": msg_id})


async def _make_due(chat_id, msg_id):
    """The retry wait is over: make the job due right now."""
    from SWAGGYMUSIC.utils.database import jobs_db
    await jobs_db.update_one({"chat_id": chat_id, "message_id": msg_id},
                             {"$set": {"delete_at": int(time.time()) - 1}})


async def _personal(channel, msg_id, minute):
    await dispatch(await tg_post(channel, msg_id, at(minute), user_id=ADMIN))
    return channel.alive(msg_id)


async def _setup(channel):
    await enable_gap(channel.chat_id)
    await enable_delay(channel.chat_id, 5 * 60)


async def _accepted_post_with_lost_answer(channel, monkeypatch, msg_id, first_error):
    """``msg_id`` accepted at 10:00 (gap + delay on); worker attempt 1
    deletes it but the answer is lost (``first_error``)."""
    assert await _personal(channel, msg_id, 0) is True
    channel.delete_then_raise = [first_error]

    async def first_attempt_done():
        job = await _job(channel.chat_id, msg_id)
        return job is not None and job["attempts"] == 1

    await run_worker_until(monkeypatch, first_attempt_done)
    assert not channel.alive(msg_id), "attempt 1 really deleted the post"
    job = await _job(channel.chat_id, msg_id)
    assert job["inflight"] is True
    assert history_entry(await settings(channel.chat_id), msg_id)["bot_delete"] == "pending"


class TestUnknownOutcomeThenFloodWait:
    @pytest.mark.parametrize("first_error", [
        OSError("connection reset"), InternalServerError(),
    ], ids=["network", "5xx"])
    async def test_pending_mark_and_inflight_survive_floodwait(self, channel, monkeypatch, first_error):
        await _setup(channel)
        await _accepted_post_with_lost_answer(channel, monkeypatch, 701, first_error)

        channel.delete_errors = [FloodWait(value=600)]
        await _make_due(channel.chat_id, 701)

        async def rescheduled():
            job = await _job(channel.chat_id, 701)
            return job is not None and job["delete_at"] >= int(time.time()) + 500

        await run_worker_until(monkeypatch, rescheduled)
        job = await _job(channel.chat_id, 701)
        assert job["inflight"] is True, "attempt 1's outcome is still unknown"
        assert job["attempts"] == 1, "FloodWait does not count as an attempt"
        doc = await settings(channel.chat_id)
        assert history_entry(doc, 701) == {"msg_id": 701, "post_at": ts(at(0)), "bot_delete": "pending"}
        assert doc["last_gap_post_msg_id"] == 701 and doc["last_gap_post_at"] == ts(at(0))

    async def test_gap_reference_is_not_corrupted_while_waiting(self, channel, monkeypatch):
        """During the FloodWait the post is gone from Telegram; /setgap must
        still count it (it may be the bot's own deletion) without asking
        Telegram, and must not drop it from the history."""
        await enable_gap(channel.chat_id)
        assert await _personal(channel, 710, -20) is True      # older accepted post (09:40)
        await enable_delay(channel.chat_id, 5 * 60)            # (710 gets no /setdelay job)
        await _accepted_post_with_lost_answer(channel, monkeypatch, 711, OSError("reset"))
        channel.delete_errors = [FloodWait(value=600)]
        await _make_due(channel.chat_id, 711)

        async def rescheduled():
            job = await _job(channel.chat_id, 711)
            return job is not None and job["delete_at"] >= int(time.time()) + 500

        await run_worker_until(monkeypatch, rescheduled)
        channel.get_messages_calls.clear()
        assert await _personal(channel, 712, 7) is False        # 10:07: 711 (10:00) still counts
        assert 711 not in channel.get_messages_calls
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [710, 711]
        assert doc["last_gap_post_msg_id"] == 711

    async def test_next_retry_confirms_the_bot_deletion(self, channel, monkeypatch):
        await _setup(channel)
        await _accepted_post_with_lost_answer(channel, monkeypatch, 721, OSError("reset"))
        channel.delete_errors = [FloodWait(value=600)]
        await _make_due(channel.chat_id, 721)

        async def rescheduled():
            job = await _job(channel.chat_id, 721)
            return job is not None and job["delete_at"] >= int(time.time()) + 500

        await run_worker_until(monkeypatch, rescheduled)
        await _make_due(channel.chat_id, 721)                   # the wait is over

        async def done():
            return not await job_count(channel.chat_id, 721)

        await run_worker_until(monkeypatch, done)
        # Telegram now reports 0 deleted: the post was removed by attempt 1.
        entry = history_entry(await settings(channel.chat_id), 721)
        assert entry["bot_delete"] == "confirmed"
        assert await _personal(channel, 722, 7) is False         # still counts
        assert await _personal(channel, 723, 15) is True

    async def test_repeated_floodwaits_keep_the_mark(self, channel, monkeypatch):
        await _setup(channel)
        await _accepted_post_with_lost_answer(channel, monkeypatch, 731, OSError("reset"))
        for _ in range(3):
            channel.delete_errors = [FloodWait(value=600)]
            await _make_due(channel.chat_id, 731)

            async def rescheduled():
                job = await _job(channel.chat_id, 731)
                return job is not None and job["delete_at"] >= int(time.time()) + 500

            await run_worker_until(monkeypatch, rescheduled)
            assert (await _job(channel.chat_id, 731))["inflight"] is True
            assert history_entry(await settings(channel.chat_id), 731)["bot_delete"] == "pending"


class TestUnknownOutcomeThenRefusal:
    async def test_refusal_after_unknown_outcome_keeps_mark_and_inflight(self, channel, monkeypatch):
        await _setup(channel)
        await _accepted_post_with_lost_answer(channel, monkeypatch, 741, OSError("reset"))
        channel.delete_errors = [MessageDeleteForbidden()]
        await _make_due(channel.chat_id, 741)

        async def second_attempt_done():
            job = await _job(channel.chat_id, 741)
            return job is not None and job["attempts"] == 2

        await run_worker_until(monkeypatch, second_attempt_done)
        job = await _job(channel.chat_id, 741)
        assert job["inflight"] is True
        assert history_entry(await settings(channel.chat_id), 741)["bot_delete"] == "pending"
        assert await _personal(channel, 742, 7) is False


class TestFirstAttemptRefusalsUnchanged:
    """Without an earlier unknown outcome a refusal still means "nothing was
    deleted": the mark is cleared and ``inflight`` removed (round-2 rules)."""

    async def test_floodwait_on_first_attempt_clears(self, channel, monkeypatch):
        await _setup(channel)
        assert await _personal(channel, 751, 0) is True
        channel.delete_errors = [FloodWait(value=600)]

        async def rescheduled():
            job = await _job(channel.chat_id, 751)
            return job is not None and job["delete_at"] >= int(time.time()) + 500

        await run_worker_until(monkeypatch, rescheduled)
        job = await _job(channel.chat_id, 751)
        assert "inflight" not in job and job["attempts"] == 0
        assert "bot_delete" not in history_entry(await settings(channel.chat_id), 751)
        channel.delete_manually(751)                       # then an admin deletes it
        assert await _personal(channel, 752, 7) is True    # manual -> no longer counts

    async def test_refusal_on_first_attempt_clears(self, channel, monkeypatch):
        await _setup(channel)
        assert await _personal(channel, 761, 0) is True
        channel.delete_errors = [MessageDeleteForbidden()]

        async def attempted():
            job = await _job(channel.chat_id, 761)
            return job is not None and job["attempts"] == 1

        await run_worker_until(monkeypatch, attempted)
        assert "inflight" not in await _job(channel.chat_id, 761)
        assert "bot_delete" not in history_entry(await settings(channel.chat_id), 761)
