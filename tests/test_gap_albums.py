"""/setgap and media albums (several photos/videos sent together).

Telegram delivers every item of an album as a SEPARATE update; all items
carry the same ``media_group_id`` (raw ``grouped_id``). Kurigram dispatches
them independently — possibly concurrently and in any order.

Rules under test:
  * the album is ONE post: it is allowed as a whole or rejected (deleted)
    as a whole — never the first item kept and the rest deleted;
  * it consumes ONE gap slot;
  * the verdict is decided once (atomically, also across processes and
    restarts) and every other item follows it;
  * /setdelay still schedules every item, and /setgap manual-deletion
    recovery treats the album as one post that counts while any item counts.

Items are genuine MTProto updates (photo / video media, grouped_id) parsed by
Kurigram (gap_harness.tg_album) and delivered through the real handler chain.
"""
from __future__ import annotations

import asyncio

import pytest
from gap_harness import (
    at,
    dispatch,
    enable_anonymous_filter,
    enable_delay,
    enable_gap,
    history_entry,
    history_ids,
    job_count,
    other_channel_raw_id,
    run_worker_until,
    settings,
    tg_album,
    tg_post,
    ts,
)
from pyrogram.errors import FloodWait

pytestmark = pytest.mark.asyncio

ADMIN = 4242


async def _single(channel, msg_id, minute, **sender):
    await dispatch(await tg_post(channel, msg_id, at(minute), **sender))
    return channel.alive(msg_id)


async def _deliver(items, *, concurrently=False):
    if concurrently:
        await asyncio.gather(*(dispatch(item) for item in items))
    else:
        for item in items:
            await dispatch(item)


def _alive(channel, items):
    return [channel.alive(item.id) for item in items]


def _verdicts(doc):
    return {v["media_group_id"]: v["verdict"] for v in doc.get("gap_albums") or []}


async def _job(chat_id, msg_id):
    from SWAGGYMUSIC.utils.database import jobs_db
    return await jobs_db.find_one({"chat_id": chat_id, "message_id": msg_id})


# ---------------------------------------------------------------------------
# The album is one post
# ---------------------------------------------------------------------------

class TestAlbumIsOnePost:
    async def test_every_item_of_an_accepted_album_is_kept(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 101, at(0), kinds=("photo", "video", "photo", "video"))
        group = album[0].media_group_id
        await _deliver(album)
        assert _alive(channel, album) == [True] * 4, "the whole album must survive"
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [101, 102, 103, 104]
        for item in album:
            entry = history_entry(doc, item.id)
            assert entry["media_group_id"] == group and entry["post_at"] == ts(at(0))
        assert doc["last_gap_post_msg_id"] == 101 and doc["last_gap_post_at"] == ts(at(0))
        assert _verdicts(doc) == {group: "accepted"}

    async def test_album_consumes_exactly_one_gap_slot(self, channel):
        await enable_gap(channel.chat_id)
        await _deliver(await tg_album(channel, 111, at(0)))
        assert await _single(channel, 120, 5) is False      # the album holds the slot
        assert await _single(channel, 121, 15) is True      # one gap after the album
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [111, 112, 113, 121]
        assert doc["last_gap_post_msg_id"] == 121

    async def test_rejected_album_is_deleted_completely(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 130, 0) is True        # A 10:00
        album = await tg_album(channel, 131, at(5), kinds=("photo", "video", "photo"))
        await _deliver(album)
        assert _alive(channel, album) == [False] * 3
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [130] and doc["last_gap_post_msg_id"] == 130
        assert _verdicts(doc) == {album[0].media_group_id: "rejected"}
        assert await _single(channel, 140, 15) is True        # the album never took a slot

    async def test_items_delivered_in_reverse_order(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 151, at(0), kinds=("video", "photo", "photo"))
        await _deliver(list(reversed(album)))
        assert _alive(channel, album) == [True] * 3
        doc = await settings(channel.chat_id)
        assert sorted(history_ids(doc)) == [151, 152, 153]
        assert doc["last_gap_post_msg_id"] == 153, "the first item processed decides"

    async def test_redelivered_item_is_not_judged_again(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 161, at(0))
        await _deliver(album)
        await dispatch(album[1])                              # duplicate update
        assert _alive(channel, album) == [True] * 3
        assert history_ids(await settings(channel.chat_id)) == [161, 162, 163]


# ---------------------------------------------------------------------------
# Asynchronous / concurrent delivery
# ---------------------------------------------------------------------------

class TestConcurrentAlbums:
    async def test_concurrent_items_of_an_allowed_album(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 200, -5) is True         # 09:55, deleted below
        channel.delete_manually(200)
        channel.get_messages_delay = 0.01                      # decisions overlap
        album = await tg_album(channel, 201, at(0), kinds=("photo", "video") * 5)
        await _deliver(album, concurrently=True)
        assert _alive(channel, album) == [True] * 10
        doc = await settings(channel.chat_id)
        assert sorted(history_ids(doc)) == list(range(201, 211))
        assert _verdicts(doc) == {album[0].media_group_id: "accepted"}

    async def test_concurrent_items_of_a_rejected_album(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 220, 0) is True
        channel.get_messages_delay = 0.01
        album = await tg_album(channel, 221, at(5), kinds=("photo", "video") * 5)
        await _deliver(album, concurrently=True)
        assert _alive(channel, album) == [False] * 10
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [220]
        assert _verdicts(doc) == {album[0].media_group_id: "rejected"}

    async def test_two_albums_at_once_exactly_one_survives_whole(self, channel):
        await enable_gap(channel.chat_id)
        first = await tg_album(channel, 231, at(0), kinds=("photo",) * 4)
        second = await tg_album(channel, 241, at(0), kinds=("video",) * 4)
        interleaved = [item for pair in zip(first, second, strict=True) for item in pair]
        await _deliver(interleaved, concurrently=True)
        outcome = (_alive(channel, first), _alive(channel, second))
        assert outcome in (([True] * 4, [False] * 4), ([False] * 4, [True] * 4))

    async def test_album_racing_a_single_post(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 251, at(0))
        single = await tg_post(channel, 250, at(0))
        await _deliver([album[0], single, album[1], album[2]], concurrently=True)
        survivors = (_alive(channel, album), channel.alive(250))
        assert survivors in (([True] * 3, False), ([False] * 3, True))

    @pytest.mark.parametrize("rejected_first", [False, True])
    async def test_two_processes_without_shared_lock(self, channel, monkeypatch, rejected_first):
        """Two bot processes do not share the in-process lock: MongoDB's
        compare-and-set on the album verdict alone keeps the album whole."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        monkeypatch.setattr(ad, "_gap_lock", lambda chat_id: asyncio.Lock())
        await enable_gap(channel.chat_id)
        if rejected_first:
            assert await _single(channel, 260, 0) is True          # blocks the albums
        else:
            assert await _single(channel, 260, -5) is True         # deleted -> free window
            channel.delete_manually(260)
        channel.get_messages_delay = 0.01
        first = await tg_album(channel, 261, at(5), kinds=("photo",) * 5)
        second = await tg_album(channel, 271, at(5), kinds=("video",) * 5)
        await _deliver(first + second, concurrently=True)
        outcome = (_alive(channel, first), _alive(channel, second))
        if rejected_first:
            assert outcome == ([False] * 5, [False] * 5)
        else:
            assert outcome in (([True] * 5, [False] * 5), ([False] * 5, [True] * 5))
        doc = await settings(channel.chat_id)
        verdicts = _verdicts(doc)
        assert len(doc["gap_albums"]) == 2, "exactly one verdict per album"
        assert sorted(verdicts.values()) == (["rejected", "rejected"] if rejected_first
                                            else ["accepted", "rejected"])
        ids = history_ids(doc)
        assert len(ids) == len(set(ids)), "no item recorded twice"


class TestVerdictCompareAndSet:
    """Deterministic cross-process interleavings (another bot process acts
    between this item's read and its write)."""

    async def test_accept_refused_when_another_process_rejected_the_album(self, channel, monkeypatch):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad
        from SWAGGYMUSIC.utils.database import settings_db

        await enable_gap(channel.chat_id)
        assert await _single(channel, 280, 0) is True
        album = await tg_album(channel, 281, at(5))
        group = album[0].media_group_id
        real_resolve = ad._resolve_stale_gap_reference

        async def other_process_meanwhile(chat_id, message, gap, post_time):
            await real_resolve(chat_id, message, gap, post_time)
            # Another process rejected this album (item 283) and an admin then
            # deleted the blocking post 280, so the gap window looks free.
            await settings_db.update_one({"chat_id": chat_id}, {
                "$push": {"gap_albums": {"media_group_id": group, "verdict": "rejected",
                                         "post_at": ts(at(5)), "msg_id": 283}},
                "$unset": {"last_gap_post_at": "", "last_gap_post_msg_id": "", "gap_history": ""},
            })

        monkeypatch.setattr(ad, "_resolve_stale_gap_reference", other_process_meanwhile)
        await dispatch(album[0])
        assert not channel.alive(281), "the stored rejection wins over a free window"
        doc = await settings(channel.chat_id)
        assert _verdicts(doc) == {group: "rejected"} and len(doc["gap_albums"]) == 1
        assert history_ids(doc) == [] and "last_gap_post_msg_id" not in doc

    async def test_rejection_is_stored_only_once(self, channel):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        group = 13_584_906_599_000_001
        first = await ad._record_album_rejection(
            channel.chat_id, group, post_time=ts(at(0)), msg_id=1, gap=900)
        again = await ad._record_album_rejection(
            channel.chat_id, group, post_time=ts(at(0)), msg_id=2, gap=900)
        other_gap = await ad._record_album_rejection(
            channel.chat_id, group + 1, post_time=ts(at(0)), msg_id=3, gap=600)
        assert (first, again, other_gap) == (True, False, False)
        doc = await settings(channel.chat_id)
        assert [(v["media_group_id"], v["msg_id"]) for v in doc["gap_albums"]] == [(group, 1)]

    async def test_accept_is_refused_once_the_album_has_a_verdict(self, channel):
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        group = 13_584_906_599_000_101
        assert await ad._gap_allows_post(channel.chat_id, ts(at(0)), 900, 11, group) is True
        await enable_gap(channel.chat_id)                      # free window, verdict kept
        assert await ad._gap_allows_post(channel.chat_id, ts(at(0)), 900, 12, group) is False
        assert await ad._gap_allows_post(channel.chat_id, ts(at(0)), 900, 13) is True


# ---------------------------------------------------------------------------
# Restarts and setting changes while an album is being delivered
# ---------------------------------------------------------------------------

def _restart():
    from SWAGGYMUSIC.plugins.misc import autodelete as ad
    from SWAGGYMUSIC.utils import anonymous_policy

    ad._GAP_LOCKS.clear()
    anonymous_policy._REJECTED.clear()


class TestAlbumVerdictPersistence:
    async def test_items_after_restart_follow_accepted_verdict(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 301, at(0))
        await _deliver(album[:2])
        _restart()
        await dispatch(album[2])
        assert _alive(channel, album) == [True] * 3
        assert history_ids(await settings(channel.chat_id)) == [301, 302, 303]

    async def test_items_after_restart_follow_rejected_verdict(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 310, 0) is True
        album = await tg_album(channel, 311, at(5))
        await _deliver(album[:2])
        _restart()
        await dispatch(album[2])
        assert _alive(channel, album) == [False] * 3

    async def test_duration_change_mid_album_keeps_rejection_whole(self, channel):
        from SWAGGYMUSIC.utils.database import set_gap_duration

        await enable_gap(channel.chat_id)
        assert await _single(channel, 320, 0) is True
        album = await tg_album(channel, 321, at(5))
        await dispatch(album[0])
        assert await set_gap_duration(channel.chat_id, 600) == ("UPDATED", 900)
        await _deliver(album[1:])
        assert _alive(channel, album) == [False] * 3

    async def test_duration_change_mid_album_keeps_acceptance_whole(self, channel):
        from SWAGGYMUSIC.utils.database import set_gap_duration

        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 331, at(0))
        await dispatch(album[0])
        assert await set_gap_duration(channel.chat_id, 600) == ("UPDATED", 900)
        await _deliver(album[1:])
        assert _alive(channel, album) == [True] * 3
        # The new duration starts a fresh window: the album posted before the
        # change does not occupy it.
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [] and "last_gap_post_msg_id" not in doc
        assert await _single(channel, 340, 1) is True

    async def test_disabling_setgap_mid_album_keeps_rejection_whole(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 3600)         # keeps the settings document
        assert await _single(channel, 350, 0) is True
        album = await tg_album(channel, 351, at(5))
        await dispatch(album[0])
        assert await sc._do_disable("setgap", channel.chat_id) is True
        await _deliver(album[1:])
        assert _alive(channel, album) == [False] * 3
        assert await job_count(channel.chat_id, 352) == 0

    async def test_after_disable_new_albums_are_not_judged(self, channel):
        from SWAGGYMUSIC.plugins.bot import setconfig as sc

        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 3600)
        assert await _single(channel, 360, 0) is True
        await sc._do_disable("setgap", channel.chat_id)
        album = await tg_album(channel, 361, at(1))
        await _deliver(album)
        assert _alive(channel, album) == [True] * 3
        assert all([await job_count(channel.chat_id, item.id) for item in album])


# ---------------------------------------------------------------------------
# Albums + /setdelay
# ---------------------------------------------------------------------------

class TestAlbumsWithSetdelay:
    async def test_every_item_gets_its_own_setdelay_job(self, channel):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        album = await tg_album(channel, 401, at(0))
        await _deliver(album, concurrently=True)
        for item in album:
            job = await _job(channel.chat_id, item.id)
            assert job["delete_at"] == ts(at(0)) + 300
            assert job["original_post_time"] == ts(at(0)) and "reason" not in job

    async def test_rejected_album_items_get_no_setdelay_job(self, channel):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        assert await _single(channel, 410, 0) is True
        album = await tg_album(channel, 411, at(5))
        await _deliver(album)
        assert [await job_count(channel.chat_id, item.id) for item in album] == [0, 0, 0]

    async def test_setdelay_deleting_the_album_keeps_it_counting(self, channel, monkeypatch):
        await enable_gap(channel.chat_id)
        await enable_delay(channel.chat_id, 5 * 60)
        album = await tg_album(channel, 421, at(0))
        await _deliver(album)

        async def all_deleted():
            return not any([await job_count(channel.chat_id, item.id) for item in album])

        await run_worker_until(monkeypatch, all_deleted)
        doc = await settings(channel.chat_id)
        assert [history_entry(doc, item.id)["bot_delete"] for item in album] == ["confirmed"] * 3
        channel.get_messages_calls.clear()
        assert await _single(channel, 430, 7) is False      # /setdelay never shortens the gap
        assert not set(channel.get_messages_calls) & {421, 422, 423}
        assert await _single(channel, 431, 15) is True

    async def test_rejected_album_floodwait_queues_a_retry_per_item(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 440, 0) is True
        album = await tg_album(channel, 441, at(5))
        channel.delete_errors = [FloodWait(value=30)] * 3
        await _deliver(album)
        for item in album:
            job = await _job(channel.chat_id, item.id)
            assert job is not None and job["reason"] == "GAP_REJECT_RETRY"
        assert history_ids(await settings(channel.chat_id)) == [440]


# ---------------------------------------------------------------------------
# Albums + manual deletion recovery
# ---------------------------------------------------------------------------

class TestAlbumsAndManualDeletion:
    async def test_fully_deleted_album_falls_back_to_previous_post(self, channel):
        """Gap 15m: A 10:00; album B 10:15; admin deletes every item of B;
        a post at 10:17 is allowed because A still exists."""
        await enable_gap(channel.chat_id)
        assert await _single(channel, 500, 0) is True
        album = await tg_album(channel, 501, at(15))
        await _deliver(album)
        for item in album:
            channel.delete_manually(item.id)
        assert await _single(channel, 510, 17) is True
        doc = await settings(channel.chat_id)
        assert history_ids(doc) == [500, 510] and doc["last_gap_post_msg_id"] == 510

    async def test_partially_deleted_album_still_counts(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 520, 0) is True
        album = await tg_album(channel, 521, at(15))
        await _deliver(album)
        channel.delete_manually(521)
        channel.delete_manually(523)
        assert await _single(channel, 530, 17) is False        # 522 is still in the channel
        assert history_ids(await settings(channel.chat_id)) == [520, 521, 522, 523]

    async def test_album_is_checked_with_one_telegram_call(self, channel):
        await enable_gap(channel.chat_id)
        album = await tg_album(channel, 541, at(0), kinds=("photo",) * 6)
        await _deliver(album)
        channel.get_messages_requests.clear()
        assert await _single(channel, 550, 5) is False
        assert channel.get_messages_requests == [[541, 542, 543, 544, 545, 546]]

    async def test_unverifiable_album_keeps_counting(self, channel):
        await enable_gap(channel.chat_id)
        assert await _single(channel, 560, 0) is True
        album = await tg_album(channel, 561, at(15))
        await _deliver(album)
        for item in album:
            channel.delete_manually(item.id)
        channel.get_messages_error = ConnectionError("down")
        assert await _single(channel, 570, 17) is False
        assert history_ids(await settings(channel.chat_id)) == [560, 561, 562, 563]

    async def test_album_bot_mark_on_one_item_keeps_album_counting(self, channel):
        """One item deleted by /setdelay (worker mark), the rest manually."""
        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        await enable_gap(channel.chat_id)
        assert await _single(channel, 580, 0) is True
        album = await tg_album(channel, 581, at(15))
        await _deliver(album)
        await ad._gap_mark_bot_delete(channel.chat_id, 582, ad.BOT_DELETE_CONFIRMED)
        for item in album:
            channel.delete_manually(item.id)
        assert await _single(channel, 590, 17) is False


# ---------------------------------------------------------------------------
# Albums + /setanonymous
# ---------------------------------------------------------------------------

class TestAlbumsAndAnonymousFilter:
    async def test_channel_identity_album_is_removed_by_the_filter_only(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        album = await tg_album(channel, 601, at(0))
        await _deliver(album, concurrently=True)
        assert _alive(channel, album) == [False] * 3
        doc = await settings(channel.chat_id)
        assert sorted(doc["anon_rejected_msg_ids"]) == [601, 602, 603]
        assert history_ids(doc) == [] and "gap_albums" not in doc

    async def test_other_channel_identity_album_is_removed(self, channel):
        await enable_anonymous_filter(channel.chat_id)
        album = await tg_album(channel, 611, at(0), sender_channel_id=other_channel_raw_id())
        await _deliver(album)
        assert _alive(channel, album) == [False] * 3

    async def test_personal_album_is_judged_as_one_post(self, channel):
        await enable_gap(channel.chat_id)
        await enable_anonymous_filter(channel.chat_id)
        album = await tg_album(channel, 621, at(0), user_id=ADMIN)
        await _deliver(album, concurrently=True)
        assert _alive(channel, album) == [True] * 3
        late = await tg_album(channel, 631, at(5), user_id=ADMIN)
        await _deliver(late, concurrently=True)
        assert _alive(channel, late) == [False] * 3


# ---------------------------------------------------------------------------
# Helper: which messages are albums
# ---------------------------------------------------------------------------

class TestMediaGroupId:
    async def test_only_plain_integers_identify_an_album(self):
        from unittest.mock import MagicMock

        from SWAGGYMUSIC.plugins.misc import autodelete as ad

        msg = MagicMock()
        assert ad._media_group_id(msg) is None              # mocks are not albums
        msg.media_group_id = True
        assert ad._media_group_id(msg) is None
        msg.media_group_id = 13584906512345678
        assert ad._media_group_id(msg) == 13584906512345678
        msg.media_group_id = None
        assert ad._media_group_id(msg) is None
