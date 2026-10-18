"""Regression tests: /setgap re-runs, /setdelay vs manual deletion, and the
/setanonymous filter's interaction with /setgap.

Most channel posts are delivered through ``dispatch`` (tests/gap_harness.py),
which replays Kurigram's dispatcher over the handlers really registered on
``app`` — the anonymous filter (group -30), the channel command handlers
(groups -20/-19) and the gap/delay engine (group 0) run in production order
on REAL ``pyrogram.types.Message`` objects.

Fixtures ``channel`` (a FakeBotChannel wired into ``app``) and ``clock``
are defined in conftest.py.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from gap_harness import (
    GAP,
    at,
    dispatch,
    enable_anonymous_filter,
    enable_delay,
    enable_gap,
    history_entry,
    history_ids,
    job_count,
    publish,
    real_post,
    run_worker_until,
    settings,
    ts,
)
from pyrogram.enums import MessageServiceType
from pyrogram.errors import ChannelPrivate, FloodWait, MessageDeleteForbidden

pytestmark = pytest.mark.asyncio

ADMIN = 4242  # a personal account Telegram exposes (from_user)


@pytest.fixture
def quiet_commands(monkeypatch):
    """Channel command side effects: no background worker, no 120 s reply
    timer; activity logs captured."""
    from SWAGGYMUSIC.plugins.misc import autodelete as ad

    monkeypatch.setattr(ad, "_delete_after", AsyncMock())
    monkeypatch.setattr(ad, "_start_worker", AsyncMock())
    logs = {"gap": AsyncMock(), "delay": AsyncMock()}
    monkeypatch.setattr(ad, "log_setgap_change", logs["gap"])
    monkeypatch.setattr(ad, "log_setdelay_change", logs["delay"])
    return logs


async def personal(channel, msg_id, minute, text="post"):
    """A post whose sender account Telegram exposes; True if it survived."""
    await dispatch(channel.new_real_post(msg_id, at(minute), text, personal_user_id=ADMIN))
    return channel.alive(msg_id)


async def command(channel, msg_id, minute, text):
    """An admin (personal account) sends a channel command; handlers run."""
    return await dispatch(channel.new_real_post(msg_id, at(minute), text, personal_user_id=ADMIN))


# ---------------------------------------------------------------------------
# Reproductions of the reported bugs (failed before the fix)
# ---------------------------------------------------------------------------

class TestReproductions:
    async def test_same_duration_setgap_in_channel_keeps_timer(self, channel, quiet_commands):
        await enable_gap(channel.chat_id)
        assert await publish(channel, 101, at(0)) is True          # A 10:00
        ran = await command(channel, 102, 5, "/setgap 15m")
        assert "setgap_handler" in ran
        assert history_ids(await settings(channel.chat_id)) == [101]
        assert await publish(channel, 103, at(6)) is False          # 10:06 rejected

    async def test_same_duration_private_wizard_keeps_timer(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        await enable_gap(channel.chat_id)
        assert await publish(channel, 111, at(0)) is True
        assert await sc._do_enable("setgap", channel.chat_id, 15 * 60) == "NO_CHANGE"
        assert history_ids(await settings(channel.chat_id)) == [111]
        assert await publish(channel, 112, at(6)) is False

    async def test_missing_post_after_delay_deadline_is_not_assumed_bot_deleted(
        self, channel, clock,
    ):
        """A is past its /setdelay deadline but the bot never confirmed deleting
        it (the admin removed it first). That is a MANUAL deletion."""
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await publish(channel, 121, at(0)) is True
        channel.delete_manually(121)   # admin, before the worker ran
        clock(at(7))
        assert await publish(channel, 122, at(7)) is True

    async def test_anonymous_rejected_post_is_never_a_gap_reference(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        ran = await dispatch(channel.new_real_post(131, at(0)))      # channel identity
        # Group order: link filter (-40) first, anonymous filter (-30) next.
        assert ran[:2] == ["link_blocker_new_channel_post", "anonymous_post_filter"]
        assert not channel.alive(131)
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [] and "last_gap_post_msg_id" not in doc
        # A personal-account post right after is not blocked by the rejected one.
        assert await personal(channel, 132, 1) is True
        assert history_ids(await settings(channel.chat_id)) == [132]

    async def test_anonymous_rejected_post_that_could_not_be_deleted_does_not_block(
        self, channel,
    ):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        channel.delete_errors = [FloodWait(value=30)]
        await dispatch(channel.new_real_post(141, at(0)))
        assert channel.alive(141)          # Telegram refused the delete for now
        assert await personal(channel, 142, 1) is True, "rejected post must not hold the gap"

    async def test_disabling_setgap_keeps_anonymous_protection(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        assert await sc._do_disable("setgap", channel.chat_id) is True
        doc = await settings(channel.chat_id)
        assert doc.get("anonymous_filter_enabled") is True


# ---------------------------------------------------------------------------
# 1. Re-running / changing /setgap
# ---------------------------------------------------------------------------

class TestSetgapRerun:
    async def test_user_example_rerun_same_duration(self, channel, quiet_commands):
        """A 10:00 allowed; /setgap 15m again at 10:05; 10:06 rejected; 10:15 allowed."""
        await enable_gap(channel.chat_id)
        assert await personal(channel, 201, 0) is True
        await command(channel, 202, 5, "/setgap 15m")
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 201
        assert doc["last_gap_post_at"] == ts(at(0)), "original Telegram timestamp kept"
        assert await personal(channel, 203, 6) is False
        assert await personal(channel, 204, 15) is True
        quiet_commands["gap"].assert_not_awaited()  # no state change -> no log

    async def test_rerun_keeps_history_needed_for_manual_deletion_fallback(
        self, channel, quiet_commands,
    ):
        await enable_gap(channel.chat_id)
        assert await personal(channel, 211, 0) is True
        assert await personal(channel, 212, 15) is True
        await command(channel, 213, 16, "/setgap 15m")
        assert history_ids(await settings(channel.chat_id)) == [211, 212]
        channel.delete_manually(212)
        assert await personal(channel, 214, 17) is True   # falls back to 211 (10:00)

    async def test_changing_duration_starts_fresh_window(self, channel, quiet_commands):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        await enable_gap(channel.chat_id)
        assert await personal(channel, 221, 0) is True
        await command(channel, 222, 5, "/setgap 10m")
        doc = await settings(channel.chat_id)
        assert doc["gap"] == 600
        assert "gap_history" not in doc and "last_gap_post_at" not in doc
        assert await personal(channel, 223, 6) is True       # fresh window
        assert await personal(channel, 224, 7) is False      # new 10m gap applies
        log = quiet_commands["gap"]
        log.assert_awaited_once()
        assert log.await_args.args[2] == "UPDATED"
        assert log.await_args.kwargs["old_value"] == "15 minutes"
        # Private wizard: change again -> UPDATED with the previous value.
        assert await sc._do_enable("setgap", channel.chat_id, 300) == "UPDATED"
        assert sc._get_old_value(channel.chat_id, "setgap") == "10 minutes"

    async def test_first_activation_and_reactivation(self, channel, quiet_commands):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc
        from SWAGGYMUSIC.utils.database import set_gap_duration

        assert await set_gap_duration(channel.chat_id, GAP) == ("ENABLED", 0)
        assert await set_gap_duration(channel.chat_id, GAP) == ("NO_CHANGE", GAP)
        assert await personal(channel, 231, 0) is True
        await sc._do_disable("setgap", channel.chat_id)
        assert await settings(channel.chat_id) == {}
        await command(channel, 232, 1, "/setgap 15m")
        assert quiet_commands["gap"].await_args.args[2] == "ENABLED"
        assert await personal(channel, 233, 2) is True   # fresh after re-enable

    async def test_concurrent_identical_reruns_never_double_reset(self, channel):
        from SWAGGYMUSIC.utils.database import set_gap_duration

        results = await asyncio.gather(*(set_gap_duration(channel.chat_id, GAP) for _ in range(6)))
        assert sorted(r[0] for r in results) == ["ENABLED"] + ["NO_CHANGE"] * 5
        assert await personal(channel, 241, 0) is True
        results = await asyncio.gather(
            *(set_gap_duration(channel.chat_id, GAP) for _ in range(6)),
            personal(channel, 242, 1),
        )
        assert results[-1] is False                      # inside A's window
        assert all(r == ("NO_CHANGE", GAP) for r in results[:-1])
        assert history_ids(await settings(channel.chat_id)) == [241]


# ---------------------------------------------------------------------------
# 2. /setdelay deletions vs manual deletions
# ---------------------------------------------------------------------------

async def _job(chat_id, msg_id):
    from SWAGGYMUSIC.utils.database import jobs_db
    return await jobs_db.find_one({"chat_id": chat_id, "message_id": msg_id})


class TestSetdelayVsManualDeletion:
    async def test_worker_confirmed_deletion_keeps_counting(self, channel, monkeypatch):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await personal(channel, 301, 0) is True

        async def done():
            return not await job_count(channel.chat_id, 301)

        await run_worker_until(monkeypatch, done)
        entry = history_entry(await settings(channel.chat_id), 301)
        assert entry["bot_delete"] == "confirmed" and entry["post_at"] == ts(at(0))
        channel.get_messages_calls.clear()
        assert await personal(channel, 302, 7) is False   # /setdelay never shortens the gap
        assert 301 not in channel.get_messages_calls, "bot record answers without Telegram"
        assert await personal(channel, 303, 15) is True

    async def test_worker_finds_post_already_gone_means_manual(self, channel, monkeypatch):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await personal(channel, 311, 0) is True
        channel.delete_manually(311)

        async def done():
            return not await job_count(channel.chat_id, 311)

        await run_worker_until(monkeypatch, done)
        assert "bot_delete" not in history_entry(await settings(channel.chat_id), 311)
        assert await personal(channel, 312, 7) is True

    async def test_refused_delete_clears_mark(self, channel, monkeypatch):
        """Telegram refuses the /setdelay delete (RPC error): nothing was
        deleted, so no bot mark; a later manual deletion is still manual."""
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await personal(channel, 321, 0) is True
        channel.delete_errors = [MessageDeleteForbidden()]

        async def attempted():
            job = await _job(channel.chat_id, 321)
            return job is not None and job["attempts"] == 1

        await run_worker_until(monkeypatch, attempted)
        job = await _job(channel.chat_id, 321)
        assert "inflight" not in job
        assert "bot_delete" not in history_entry(await settings(channel.chat_id), 321)
        assert channel.alive(321)
        channel.delete_manually(321)
        assert await personal(channel, 322, 7) is True

    async def test_floodwait_in_worker_clears_mark_and_keeps_attempts(self, channel, monkeypatch):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await personal(channel, 331, 0) is True
        channel.delete_errors = [FloodWait(value=600)]

        async def rescheduled():
            # The claim moves delete_at to now+60; the FloodWait reschedule
            # to now+600 — wait for the latter.
            job = await _job(channel.chat_id, 331)
            return job is not None and job["delete_at"] >= int(time.time()) + 500

        await run_worker_until(monkeypatch, rescheduled)
        job = await _job(channel.chat_id, 331)
        assert job["attempts"] == 0 and "inflight" not in job
        assert "bot_delete" not in history_entry(await settings(channel.chat_id), 331)

    async def test_unknown_outcome_then_nothing_left_is_bot_deletion(self, channel, monkeypatch):
        """The delete reached Telegram but the answer was lost (network): the
        mark stays pending (still counts, no Telegram call needed); the retry
        finds nothing left and confirms it as the bot's own deletion."""
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await personal(channel, 341, 0) is True
        channel.delete_then_raise = [OSError("connection reset")]

        async def first_attempt_done():
            job = await _job(channel.chat_id, 341)
            return job is not None and job["attempts"] == 1

        await run_worker_until(monkeypatch, first_attempt_done)
        assert (await _job(channel.chat_id, 341))["inflight"] is True
        assert history_entry(await settings(channel.chat_id), 341)["bot_delete"] == "pending"
        channel.get_messages_calls.clear()
        assert await personal(channel, 342, 7) is False
        assert 341 not in channel.get_messages_calls

        async def done():
            return not await job_count(channel.chat_id, 341)

        await run_worker_until(monkeypatch, done, max_wait=6)
        assert history_entry(await settings(channel.chat_id), 341)["bot_delete"] == "confirmed"

    async def test_legacy_reference_deleted_by_worker_still_counts(self, channel, monkeypatch):
        """Pre-history documents only have last_gap_post_*: the worker records
        its confirmed deletion for that reference too."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        channel.new_real_post(351, at(0))
        await settings_db.update_one(
            {"chat_id": channel.chat_id},
            {"$set": {"last_gap_post_at": ts(at(0)), "last_gap_post_msg_id": 351}},
        )
        await ad._queue_delete(channel.chat_id, 351, 5 * 60, ts(at(0)))

        async def done():
            return not await job_count(channel.chat_id, 351)

        await run_worker_until(monkeypatch, done)
        entry = history_entry(await settings(channel.chat_id), 351)
        assert entry == {"msg_id": 351, "post_at": ts(at(0)), "bot_delete": "confirmed",
                         "bot_deleted_at": entry["bot_deleted_at"]}
        assert await personal(channel, 352, 7) is False

    async def test_mark_waits_for_running_gap_decision(self, channel):
        """The worker's mark is written under the channel's gap lock, so it can
        never interleave with a decision's read -> verify -> write."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        assert await personal(channel, 361, 0) is True
        lock = ad._gap_lock(channel.chat_id)
        await lock.acquire()
        try:
            task = asyncio.create_task(
                ad._gap_mark_bot_delete(channel.chat_id, 361, ad.BOT_DELETE_PENDING))
            await asyncio.sleep(0.05)
            assert "bot_delete" not in history_entry(await settings(channel.chat_id), 361)
        finally:
            lock.release()
        await task
        assert history_entry(await settings(channel.chat_id), 361)["bot_delete"] == "pending"


# ---------------------------------------------------------------------------
# 3. Telegram API errors while verifying
# ---------------------------------------------------------------------------

class TestVerificationErrors:
    @pytest.mark.parametrize("error", [
        ChannelPrivate(), FloodWait(value=5), ConnectionError("down"), TimeoutError(),
    ])
    async def test_errors_never_count_as_deletion(self, channel, error):
        await enable_gap(channel.chat_id)
        assert await personal(channel, 401, 0) is True
        channel.delete_manually(401)            # really gone ...
        channel.get_messages_error = error      # ... but Telegram can't tell us
        assert await personal(channel, 402, 5) is False
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 401 and history_ids(doc) == [401]
        channel.get_messages_error = None       # Telegram answers again
        assert await personal(channel, 403, 6) is True


# ---------------------------------------------------------------------------
# 4. /setanonymous policy and its coordination with /setgap
# ---------------------------------------------------------------------------

class TestAnonymousPolicy:
    async def test_policy_on_real_messages(self):
        from SWAGGYMUSIC.utils.anonymous_policy import is_channel_identity_post as identity

        chat = -100500
        assert identity(real_post(chat, 1, at(0))) is True                       # channel identity
        # Signed by Telegram (Sign Messages): an admin's own post, not anonymous.
        # (Until round 4 this asserted True — the round-2 mistake that deleted
        # admins' signed posts; see tests/test_anonymous_channel_modes.py.)
        assert identity(real_post(chat, 2, at(0), author_signature="Alice")) is False
        assert identity(real_post(chat, 3, at(0), personal_user_id=ADMIN)) is False
        assert identity(real_post(chat, 4, at(0), service=MessageServiceType.PINNED_MESSAGE)) is False
        unknown = real_post(chat, 5, at(0), sender_chat_id=None)
        assert identity(unknown) is False  # not a confirmed channel identity
        from SWAGGYMUSIC.utils.anonymous_policy import should_reject_anonymous_post
        assert should_reject_anonymous_post(unknown) is True  # fail closed
        # Another channel's identity is a chat identity too (round 3).
        assert identity(real_post(chat, 6, at(0), sender_chat_id=-100999)) is True

    async def test_signed_channel_post_is_kept(self, channel):
        """Sign Messages on: Telegram signs the admin's post with their name.
        The post is attributed to an admin, so it is kept (and judged by
        /setgap) — the bot neither looks the name up nor logs it."""
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        await dispatch(channel.new_real_post(501, at(0), author_signature="Alice"))
        assert channel.alive(501)
        assert history_ids(await settings(channel.chat_id)) == [501]

    async def test_personal_posts_are_allowed_and_judged_by_gap(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        assert await personal(channel, 511, 0) is True
        await dispatch(channel.new_real_post(512, at(2)))           # anonymous -> filtered
        assert not channel.alive(512)
        assert await personal(channel, 513, 5) is False             # gap from 511 still applies
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [511] and doc["anon_rejected_msg_ids"] == [512]

    async def test_service_messages_are_neither_filtered_nor_gap_references(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        assert await personal(channel, 521, 0) is True
        pin = channel.new_real_post(522, at(20), "", service=MessageServiceType.PINNED_MESSAGE)
        await dispatch(pin)
        assert channel.alive(522), "anonymous filter must not delete service messages"
        assert history_ids(await settings(channel.chat_id)) == [521]
        assert await personal(channel, 523, 21) is True   # the pin did not take the slot

    async def test_channel_identity_command_still_runs(self, channel, quiet_commands):
        """The filter removes a channel-identity command post, but the command
        handlers (groups -20/-19) still process it."""
        await enable_gap(channel.chat_id, gap=600)
        await enable_anonymous_filter(channel.chat_id)
        ran = await dispatch(channel.new_real_post(531, at(0), "/setgap 15m"))
        # Group order: link filter (-40) first, anonymous filter (-30),
        # then the /setgap command handler (-19) still processes the post.
        assert ran[:3] == ["link_blocker_new_channel_post",
                           "anonymous_post_filter", "setgap_handler"]
        assert (await settings(channel.chat_id))["gap"] == 900
        assert not channel.alive(531)

    async def test_filter_disabled_channel_identity_posts_are_judged_normally(self, channel):
        await enable_gap(channel.chat_id)
        await dispatch(channel.new_real_post(541, at(0)))
        assert channel.alive(541)
        assert history_ids(await settings(channel.chat_id)) == [541]

    async def test_failed_anonymous_delete_queues_retry_and_worker_removes_it(
        self, channel, monkeypatch,
    ):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        channel.delete_errors = [FloodWait(value=1)]
        await dispatch(channel.new_real_post(551, at(0)))
        job = await _job(channel.chat_id, 551)
        assert job is not None and job["reason"] == "ANON_REJECT_RETRY"
        assert channel.alive(551)

        async def done():
            return not await job_count(channel.chat_id, 551)

        await run_worker_until(monkeypatch, done, max_wait=6)
        assert not channel.alive(551)
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [] and "last_gap_post_msg_id" not in doc

    async def test_bot_without_delete_right_never_records_rejected_post(self, channel, monkeypatch):
        from pyrogram.enums import ChatMemberStatus

        from SWAGGYMUSIC import app

        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        member = AsyncMock()
        member.status = ChatMemberStatus.ADMINISTRATOR
        member.privileges.can_delete_messages = False
        monkeypatch.setattr(app, "get_chat_member", AsyncMock(return_value=member))
        await dispatch(channel.new_real_post(561, at(0)))
        assert channel.alive(561)
        assert history_ids(await settings(channel.chat_id)) == []

    async def test_database_refuses_rejected_id_even_without_the_filter(self, channel):
        """Backstop for a second process / duplicate update: the persisted
        verdict makes the atomic accept refuse the post."""
        from SWAGGYMUSIC import app
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils import anonymous_policy

        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        await dispatch(channel.new_real_post(571, at(0)))
        anonymous_policy._REJECTED.clear()        # "restart": in-process verdicts gone
        assert await ad._gap_allows_post(channel.chat_id, ts(at(0)), GAP, 571) is False
        redelivered = channel.new_real_post(571, at(0))
        await ad.autodelete_new_channel_post(app, redelivered)   # filter bypassed
        assert history_ids(await settings(channel.chat_id)) == []
        assert channel.alive(571), "skipped, not judged/deleted by the gap"

    async def test_in_process_verdict_protects_when_persisting_fails(self, channel, monkeypatch):
        """MongoDB write of the verdict fails AND Telegram refuses the delete:
        the in-process verdict alone still keeps the post out of /setgap."""
        from SWAGGYMUSIC.utils import anonymous_policy

        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        monkeypatch.setattr(anonymous_policy, "record_rejection",
                            AsyncMock(side_effect=RuntimeError("mongo down")))
        channel.delete_errors = [FloodWait(value=30)]
        await dispatch(channel.new_real_post(591, at(0)))
        assert channel.alive(591)
        assert history_ids(await settings(channel.chat_id)) == []
        assert await personal(channel, 592, 1) is True

    async def test_rejection_after_recording_rebuilds_reference(self, channel):
        """If a post had (out of order) become the reference, recording the
        rejection removes it and the reference falls back to the previous
        accepted post."""
        from SWAGGYMUSIC.utils import anonymous_policy

        await enable_gap(channel.chat_id)
        assert await publish(channel, 581, at(0)) is True
        assert await publish(channel, 582, at(15)) is True
        await anonymous_policy.record_rejection(channel.chat_id, 582)
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [581] and "last_gap_post_msg_id" not in doc
        assert await publish(channel, 583, at(10)) is False    # 581 (10:00) blocks again
        assert (await settings(channel.chat_id))["last_gap_post_msg_id"] == 581

    async def test_settings_document_removed_only_when_all_features_off(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 60)
        await enable_anonymous_filter(channel.chat_id)
        assert await sc._do_disable("setdelay", channel.chat_id) is True
        assert await sc._do_disable("setgap", channel.chat_id) is True
        doc = await settings(channel.chat_id)
        assert doc["anonymous_filter_enabled"] is True
        assert "gap" not in doc and "delay" not in doc
        await settings_db.update_one(
            {"chat_id": channel.chat_id}, {"$set": {"anonymous_filter_enabled": False}})
        await enable_delay(channel.chat_id, 60)
        assert await sc._do_disable("setdelay", channel.chat_id) is True
        assert await settings(channel.chat_id) == {}


# ---------------------------------------------------------------------------
# 5. Concurrency, persistence, existing /setdelay and fallback behaviour
# ---------------------------------------------------------------------------

class TestConcurrencyPersistenceAndExisting:
    async def test_concurrent_posts_through_full_handler_chain(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        channel.get_messages_delay = 0.01
        posts = [channel.new_real_post(600 + i, at(0, i), personal_user_id=ADMIN) for i in range(4)]
        posts += [channel.new_real_post(610 + i, at(0, i)) for i in range(3)]   # anonymous
        await asyncio.gather(*(dispatch(p) for p in posts))
        alive_personal = [p.id for p in posts[:4] if channel.alive(p.id)]
        assert len(alive_personal) == 1
        assert not any(channel.alive(p.id) for p in posts[4:])
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == alive_personal
        assert sorted(doc["anon_rejected_msg_ids"]) == [610, 611, 612]

    async def test_restart_keeps_history_marks_and_verdicts(self, channel, monkeypatch):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils import anonymous_policy

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        await enable_anonymous_filter(channel.chat_id)
        assert await personal(channel, 621, 0) is True
        await dispatch(channel.new_real_post(622, at(1)))            # anonymous, rejected
        channel.delete_then_raise = [OSError("crash during delete")]

        async def first_attempt():
            job = await _job(channel.chat_id, 621)
            return job is not None and job["attempts"] == 1

        await run_worker_until(monkeypatch, first_attempt)
        # --- restart: every in-process structure is gone ---
        ad._GAP_LOCKS.clear()
        anonymous_policy._REJECTED.clear()
        doc = await settings(channel.chat_id)
        assert history_entry(doc, 621) == {"msg_id": 621, "post_at": ts(at(0)), "bot_delete": "pending"}
        assert doc["anon_rejected_msg_ids"] == [622]
        assert await personal(channel, 623, 7) is False               # 621 still counts

        async def done():
            return not await job_count(channel.chat_id, 621)

        await run_worker_until(monkeypatch, done, max_wait=6)
        assert history_entry(await settings(channel.chat_id), 621)["bot_delete"] == "confirmed"

    async def test_setdelay_command_and_jobs_unchanged(self, channel, quiet_commands, monkeypatch):
        await command(channel, 631, 0, "/setdelay 1m")
        doc = await settings(channel.chat_id)
        assert doc["enabled"] is True and doc["delay"] == 60
        assert not channel.alive(631)                  # command cleaned up
        quiet_commands["delay"].assert_awaited_once()
        assert await personal(channel, 632, 1) is True
        job = await _job(channel.chat_id, 632)
        assert job["delete_at"] == ts(at(1)) + 60 and job["original_post_time"] == ts(at(1))
        assert "reason" not in job

        async def done():
            return not await job_count(channel.chat_id, 632)

        await run_worker_until(monkeypatch, done)
        assert not channel.alive(632)
        assert "gap_history" not in await settings(channel.chat_id)  # no gap bookkeeping

    async def test_retry_exhaustion_unchanged(self, channel, monkeypatch):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import jobs_db

        await ad._queue_delete(channel.chat_id, 641, 60, ts(at(0)))
        await jobs_db.update_one({"chat_id": channel.chat_id, "message_id": 641},
                                 {"$set": {"attempts": 10}})
        channel.delete_errors = [MessageDeleteForbidden()]

        async def done():
            return not await job_count(channel.chat_id, 641)

        await run_worker_until(monkeypatch, done)

    async def test_manual_deletion_fallback_through_full_chain(self, channel):
        """The round-1 fallback still works end-to-end with /setanonymous on."""
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        assert await personal(channel, 651, 0) is True     # A 10:00
        assert await personal(channel, 652, 15) is True    # B 10:15
        channel.delete_manually(652)
        assert await personal(channel, 653, 17) is True    # C 10:17 allowed (A exists)
        doc = await settings(channel.chat_id)
        assert doc["last_gap_post_msg_id"] == 653 and history_ids(doc) == [651, 653]
