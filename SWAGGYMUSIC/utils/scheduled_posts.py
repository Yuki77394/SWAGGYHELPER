#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
Persistent storage for the /Schedule scheduled-post manager.

Uses the existing MongoDB connection (``SWAGGYMUSIC.core.mongo.mongodb``);
no new client is created.

=====================================================================
 SCHEMA
=====================================================================

  mongodb.scheduled_posts  — one document per scheduled post
  ------------------------------------------------------------
  {
    _id:             ObjectId,
    schedule_id:     <int>,      # UNIQUE human-friendly number (#12)
    channel_id:      <int>,      # target channel
    channel_title:   <str>,      # cached for display
    creator_id:      <int>,      # admin who created it
    updated_by:      <int>,      # admin who changed it last
    content: {                   # see utils/schedule_content.py
      kind:  "text"|"photo"|"video"|"animation"|"document"|"audio"|"voice",
      text:  <str>,              # text, or the media caption
      entities: [ {type, offset, length, url?, language?,
                   custom_emoji_id?, user_id?, expandable?, ...} ],
      plain_text: <bool>,        # composed in Plain Text mode
      file_id, file_unique_id, has_spoiler, caption_above,   # media only
      link_preview: {disabled, above}                        # text only
    },
    content_hash:    <str>,      # identical-post detection
    schedule_type:   "once"|"daily",   # missing = "once" (posts made before
                                       # daily schedules existed)
    scheduled_at:    <int>,      # UTC epoch seconds — when to publish NEXT
                                 # ("once": the post time; "daily": the next
                                 # occurrence — the next_run_at of the spec)
    daily_time:      {hour, minute},   # "daily" only: IST wall-clock time
    start_date:      <str>,      # the IST date the admin picked ("2026-11-06"):
                                 # "once": the post date; "daily": the first
                                 # day it may run — kept exactly as picked
    timezone:        "Asia/Kolkata",   # zone the time was picked in (UI)
    status:          "scheduled"|"sending"|"sent"|"failed"|"cancelled"|"missed"
                     |"paused",  # "sent" = a completed one-time post;
                                 # "paused" = a stopped daily schedule
    created_at, updated_at:      <int UTC epoch seconds>,
    attempts:        <int>,      # delivery attempts (claims)
    retry_at:        <int>,      # FloodWait: not before this time
    claimed_at:      <int>,      # status "sending" only
    claim_token:     <str>,      # status "sending" only — the claim owner
    sent_at, sent_message_id, late_by: <int>,      # status "sent"
    delivery_warnings: [<str>],  # e.g. custom emoji downgraded by Telegram
    error: { code, message, at, uncertain },       # "failed" / "missed";
                                 # "daily": the last failed occurrence
    # "daily" only — execution state of the recurring schedule:
    last_run_at:     <int>,      # when the last occurrence was published
    last_run_key:    <str>,      # its IST day ("2026-11-05"): at most one
                                 # post per schedule per IST day
    last_sent_message_id: <int>,
    run_count, skip_count, failure_count: <int>,
    last_skip:       { occurrence, at, late_by },  # bot offline at that time
    paused_at:       <int>,
    draft_id:        <str>       # UNIQUE (sparse) — the wizard's confirm
  }                              # token: a double tap cannot create twice

  mongodb.schedule_counters
  ------------------------------------------------------------
  { _id: "scheduled_posts", seq: <int> }   # source of schedule_id

  Indexes (ensure_schedule_indexes, idempotent, called at boot):
    - {schedule_id: 1}                         UNIQUE
    - {draft_id: 1}                            UNIQUE, sparse
    - {status: 1, scheduled_at: 1}             (delivery worker)
    - {channel_id: 1, status: 1, scheduled_at: 1}   (panel lists)

=====================================================================
 STATUS MACHINE — every transition is ONE conditional update
=====================================================================

  scheduled --claim--> sending --> sent
      ^                   |   +--> failed     (Telegram refused, or the
      |                   |                    outcome is unknown)
      |                   +------> scheduled  (FloodWait: retry later)
      +-- reschedule -- failed / missed / cancelled
  scheduled --cancel--> cancelled          scheduled --late--> missed
  stale "sending" (bot stopped mid-send) --> failed (uncertain)

  "daily" schedules never finish on their own:
  scheduled --claim--> sending --> scheduled  (next occurrence — after a
      ^   |                                     publication, a failure or a
      |   +--pause--> paused                    missed time; the occurrence
      +----resume-----+                         is never sent again)
  stale "sending" --> scheduled (next occurrence, error "interrupted")

  The worker claims a post with ``find_one_and_update`` filtered on
  ``status: "scheduled"``: only one worker (or process) can win it. A
  claimed post is never claimed again, so a restart can never send it a
  second time — an interrupted send becomes "failed (outcome unknown)" for
  the admin to check, instead of a possible duplicate.
"""

import uuid
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from SWAGGYMUSIC.core.mongo import mongodb
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils import schedule_time
from SWAGGYMUSIC.utils.schedule_time import TIMEZONE_NAME

log = LOGGER(__name__)

scheduled_db = mongodb.scheduled_posts
counters_db = mongodb.schedule_counters

STATUS_SCHEDULED = "scheduled"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_MISSED = "missed"
STATUS_PAUSED = "paused"

TYPE_ONCE = "once"
TYPE_DAILY = "daily"

# The main panel lists schedules still needing attention; History the rest.
PENDING_STATUSES = (STATUS_SCHEDULED, STATUS_SENDING, STATUS_FAILED, STATUS_PAUSED)
HISTORY_STATUSES = (STATUS_SENT, STATUS_CANCELLED, STATUS_MISSED)
# What an admin may still change.
EDITABLE_STATUSES = (STATUS_SCHEDULED, STATUS_FAILED, STATUS_CANCELLED, STATUS_MISSED,
                     STATUS_PAUSED)
RESCHEDULABLE_STATUSES = (STATUS_SCHEDULED, STATUS_FAILED, STATUS_CANCELLED,
                          STATUS_MISSED)        # one-time posts
CANCELLABLE_STATUSES = (STATUS_SCHEDULED,)      # one-time posts
PAUSABLE_STATUSES = (STATUS_SCHEDULED,)         # daily schedules
RETIMABLE_STATUSES = (STATUS_SCHEDULED, STATUS_PAUSED)   # daily schedules
DELETABLE_STATUSES = (STATUS_SCHEDULED, STATUS_SENT, STATUS_FAILED,
                      STATUS_CANCELLED, STATUS_MISSED, STATUS_PAUSED)

# Filters for operations that only make sense for one schedule type.
# (Documents created before daily schedules existed have no schedule_type.)
_ONLY_ONCE = {"schedule_type": {"$ne": TYPE_DAILY}}
_ONLY_DAILY = {"schedule_type": TYPE_DAILY}

_COUNTER_ID = "scheduled_posts"
_indexes_ready = False


class DuplicateDraft(Exception):
    """The wizard's confirm token was already used (double tap)."""

    def __init__(self, existing: Optional[dict]):
        super().__init__("draft already scheduled")
        self.existing = existing


def schedule_type(doc: Optional[dict]) -> str:
    return TYPE_DAILY if (doc or {}).get("schedule_type") == TYPE_DAILY else TYPE_ONCE


def is_daily(doc: Optional[dict]) -> bool:
    return schedule_type(doc) == TYPE_DAILY


def daily_time_of(doc: dict) -> Tuple[int, int]:
    """(hour, minute) IST of a daily schedule."""
    value = doc.get("daily_time") or {}
    return int(value.get("hour", 0)), int(value.get("minute", 0))


def start_date_of(doc: dict) -> date:
    """The IST date the admin picked (older documents: the day they were made)."""
    try:
        return date.fromisoformat(doc.get("start_date") or "")
    except (TypeError, ValueError):
        return schedule_time.to_ist(int(doc.get("created_at") or doc["scheduled_at"])).date()


def next_occurrence(doc: dict, after: int, skip_key: Optional[str] = None) -> int:
    """The daily schedule's next occurrence strictly after ``after``."""
    hour, minute = daily_time_of(doc)
    return schedule_time.next_daily_run(hour, minute, after, skip_key)


async def ensure_schedule_indexes() -> None:
    """Create the scheduled_posts indexes. Idempotent; best-effort callers."""
    global _indexes_ready
    if _indexes_ready:
        return
    await scheduled_db.create_index(
        [("schedule_id", ASCENDING)], name="schedule_id_1", unique=True,
    )
    await scheduled_db.create_index(
        [("draft_id", ASCENDING)], name="draft_id_1", unique=True, sparse=True,
    )
    await scheduled_db.create_index(
        [("status", ASCENDING), ("scheduled_at", ASCENDING)],
        name="status_1_scheduled_at_1",
    )
    await scheduled_db.create_index(
        [("channel_id", ASCENDING), ("status", ASCENDING), ("scheduled_at", ASCENDING)],
        name="channel_id_1_status_1_scheduled_at_1",
    )
    _indexes_ready = True


async def next_schedule_id() -> int:
    doc = await counters_db.find_one_and_update(
        {"_id": _COUNTER_ID}, {"$inc": {"seq": 1}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------

async def find_identical(channel_id: int, digest: str, scheduled_at: int,
                         exclude: Optional[int] = None) -> Optional[dict]:
    """An active post with the same content at the same time, if any."""
    query: Dict[str, Any] = {
        "channel_id": int(channel_id),
        "content_hash": digest,
        "scheduled_at": int(scheduled_at),
        "status": {"$in": [STATUS_SCHEDULED, STATUS_SENDING]},
        **_ONLY_ONCE,
    }
    if exclude is not None:
        query["schedule_id"] = {"$ne": int(exclude)}
    return await scheduled_db.find_one(query)


async def find_identical_daily(channel_id: int, digest: str, hour: int,
                               minute: int) -> Optional[dict]:
    """A daily schedule of the same post at the same time (active or paused)."""
    return await scheduled_db.find_one({
        "channel_id": int(channel_id),
        "content_hash": digest,
        "daily_time": {"hour": int(hour), "minute": int(minute)},
        "status": {"$in": [STATUS_SCHEDULED, STATUS_SENDING, STATUS_PAUSED]},
        **_ONLY_DAILY,
    })


async def create_schedule(*, channel_id: int, channel_title: str, creator_id: int,
                          content: Dict[str, Any], digest: str, scheduled_at: int,
                          draft_id: str, kind: str = TYPE_ONCE,
                          daily_time: Optional[Tuple[int, int]] = None,
                          start_date: Optional[str] = None) -> dict:
    """Insert a new schedule ("once", or "daily" with ``daily_time`` and the
    first occurrence as ``scheduled_at``). Raises DuplicateDraft on a reused
    confirm token."""
    if kind == TYPE_DAILY and daily_time is None:
        raise ValueError("a daily schedule needs daily_time")
    existing = await scheduled_db.find_one({"draft_id": draft_id})
    if existing:
        raise DuplicateDraft(existing)
    now = schedule_time.now_ts()
    doc = {
        "schedule_id": await next_schedule_id(),
        "channel_id": int(channel_id),
        "channel_title": channel_title,
        "creator_id": int(creator_id),
        "updated_by": int(creator_id),
        "content": content,
        "content_hash": digest,
        "scheduled_at": int(scheduled_at),
        "timezone": TIMEZONE_NAME,
        "status": STATUS_SCHEDULED,
        "created_at": now,
        "updated_at": now,
        "attempts": 0,
        "draft_id": draft_id,
        "schedule_type": TYPE_DAILY if kind == TYPE_DAILY else TYPE_ONCE,
        "start_date": start_date or schedule_time.run_key(int(scheduled_at)),
    }
    if kind == TYPE_DAILY:
        doc.update({
            "daily_time": {"hour": int(daily_time[0]), "minute": int(daily_time[1])},
            "run_count": 0, "skip_count": 0, "failure_count": 0,
        })
    try:
        await scheduled_db.insert_one(doc)
    except DuplicateKeyError:
        raise DuplicateDraft(await scheduled_db.find_one({"draft_id": draft_id}))
    return doc


async def get_schedule(schedule_id: int) -> Optional[dict]:
    try:
        return await scheduled_db.find_one({"schedule_id": int(schedule_id)})
    except (TypeError, ValueError):
        return None


async def list_schedules(channel_id: int, view: str, page: int,
                         per_page: int) -> Tuple[List[dict], int]:
    """One page of a channel's posts: ``view`` "pending" (soonest first) or
    "history" (newest first). Returns (posts, total)."""
    statuses = PENDING_STATUSES if view == "pending" else HISTORY_STATUSES
    query = {"channel_id": int(channel_id), "status": {"$in": list(statuses)}}
    total = await scheduled_db.count_documents(query)
    order = ASCENDING if view == "pending" else DESCENDING
    cursor = (
        scheduled_db.find(query)
        .sort([("scheduled_at", order), ("schedule_id", order)])
        .skip(max(int(page), 0) * per_page)
        .limit(per_page)
    )
    return await cursor.to_list(length=per_page), total


async def _transition(schedule_id: int, allowed: Tuple[str, ...],
                      update: Dict[str, Any],
                      extra: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    query = {"schedule_id": int(schedule_id), "status": {"$in": list(allowed)}}
    query.update(extra or {})
    return await scheduled_db.find_one_and_update(
        query, update, return_document=ReturnDocument.AFTER,
    )


async def update_content(schedule_id: int, content: Dict[str, Any], digest: str,
                         by: int) -> Optional[dict]:
    """New text/formatting/media for a post that is not sending or sent."""
    return await _transition(schedule_id, EDITABLE_STATUSES, {"$set": {
        "content": content, "content_hash": digest,
        "updated_by": int(by), "updated_at": schedule_time.now_ts(),
    }})


async def reschedule(schedule_id: int, scheduled_at: int, by: int) -> Optional[dict]:
    """A new time; a failed / missed / cancelled post becomes scheduled again."""
    return await _transition(schedule_id, RESCHEDULABLE_STATUSES, {
        "$set": {
            "scheduled_at": int(scheduled_at), "status": STATUS_SCHEDULED,
            "start_date": schedule_time.run_key(int(scheduled_at)),
            "timezone": TIMEZONE_NAME, "updated_by": int(by), "updated_at": schedule_time.now_ts(),
            "attempts": 0,
        },
        "$unset": {"error": "", "retry_at": "", "claim_token": "", "claimed_at": ""},
    }, extra=_ONLY_ONCE)


async def cancel_schedule(schedule_id: int, by: int) -> Optional[dict]:
    """Stop a one-time post from being sent; it stays in History."""
    return await _transition(schedule_id, CANCELLABLE_STATUSES, {
        "$set": {"status": STATUS_CANCELLED, "updated_by": int(by), "updated_at": schedule_time.now_ts()},
        "$unset": {"retry_at": ""},
    }, extra=_ONLY_ONCE)


# — daily schedules ----------------------------------------------------------

async def pause_schedule(schedule_id: int, by: int) -> Optional[dict]:
    """Stop an active daily schedule (not while an occurrence is sending)."""
    now = schedule_time.now_ts()
    return await _transition(schedule_id, PAUSABLE_STATUSES, {
        "$set": {"status": STATUS_PAUSED, "paused_at": now,
                 "updated_by": int(by), "updated_at": now},
        "$unset": {"retry_at": ""},
    }, extra=_ONLY_DAILY)


async def resume_schedule(schedule_id: int, next_run: int, by: int,
                          last_run_key: Optional[str]) -> Optional[dict]:
    """Restart a paused daily schedule at ``next_run``.

    ``last_run_key`` is the value ``next_run`` was computed against: the
    update only applies if it is still current (compare-and-set), so the
    resumed schedule can never post twice on one IST day.
    """
    return await _transition(schedule_id, (STATUS_PAUSED,), {
        "$set": {"status": STATUS_SCHEDULED, "scheduled_at": int(next_run), "attempts": 0,
                 "updated_by": int(by), "updated_at": schedule_time.now_ts()},
        "$unset": {"paused_at": "", "retry_at": ""},
    }, extra={**_ONLY_DAILY, "last_run_key": last_run_key})


async def change_daily_time(schedule_id: int, hour: int, minute: int, next_run: int,
                            by: int, last_run_key: Optional[str]) -> Optional[dict]:
    """A new IST time for a daily schedule. An active one moves its next
    occurrence to ``next_run``; a paused one only remembers the time (the
    next occurrence is computed when it is resumed). Compare-and-set on
    ``last_run_key`` like resume_schedule."""
    now = schedule_time.now_ts()
    new_time = {"hour": int(hour), "minute": int(minute)}
    doc = await _transition(schedule_id, (STATUS_SCHEDULED,), {
        "$set": {"daily_time": new_time, "scheduled_at": int(next_run), "attempts": 0,
                 "updated_by": int(by), "updated_at": now},
        "$unset": {"retry_at": ""},
    }, extra={**_ONLY_DAILY, "last_run_key": last_run_key})
    if doc is None:
        doc = await _transition(schedule_id, (STATUS_PAUSED,), {
            "$set": {"daily_time": new_time, "updated_by": int(by), "updated_at": now},
        }, extra=_ONLY_DAILY)
    return doc


async def delete_schedule(schedule_id: int) -> bool:
    """Remove a post for good — never one that is being sent right now."""
    result = await scheduled_db.delete_one({
        "schedule_id": int(schedule_id), "status": {"$in": list(DELETABLE_STATUSES)},
    })
    return bool(result.deleted_count)


# ---------------------------------------------------------------------------
# Delivery worker operations
# ---------------------------------------------------------------------------

def new_claim_token() -> str:
    return uuid.uuid4().hex


async def claim_next_due(now: int, token: str) -> Optional[dict]:
    """Atomically move the oldest due post from scheduled to sending."""
    return await scheduled_db.find_one_and_update(
        {
            "status": STATUS_SCHEDULED,
            "scheduled_at": {"$lte": int(now)},
            "$or": [{"retry_at": {"$exists": False}}, {"retry_at": {"$lte": int(now)}}],
        },
        {
            "$set": {"status": STATUS_SENDING, "claimed_at": int(now),
                     "claim_token": token, "updated_at": int(now)},
            "$inc": {"attempts": 1},
            "$unset": {"retry_at": ""},
        },
        sort=[("scheduled_at", ASCENDING), ("schedule_id", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )


async def _finish(doc: dict, token: str, update: Dict[str, Any]) -> bool:
    """Apply the outcome of a claim — only while this worker still owns it."""
    update.setdefault("$unset", {})["claim_token"] = ""
    result = await scheduled_db.update_one(
        {"_id": doc["_id"], "status": STATUS_SENDING, "claim_token": token}, update,
    )
    return bool(result.modified_count)


async def mark_sent(doc: dict, token: str, *, message_id: Optional[int], now: int,
                    warnings: Optional[List[str]] = None) -> bool:
    fields = {
        "status": STATUS_SENT, "sent_at": int(now), "updated_at": int(now),
        "sent_message_id": message_id,
        "late_by": max(int(now) - int(doc["scheduled_at"]), 0),
    }
    if warnings:
        fields["delivery_warnings"] = list(warnings)
    if await _finish(doc, token, {"$set": fields, "$unset": {"error": ""}}):
        return True
    # The claim was taken over meanwhile (stale recovery) — the post WAS
    # sent, so record the truth anyway.
    log.warning("schedule #%s: claim lost before mark_sent; recording delivery anyway",
                doc.get("schedule_id"))
    await scheduled_db.update_one(
        {"_id": doc["_id"]}, {"$set": fields, "$unset": {"error": "", "claim_token": ""}},
    )
    return True


async def mark_failed(doc: dict, token: str, *, code: str, message: str, now: int,
                      uncertain: bool) -> bool:
    return await _finish(doc, token, {"$set": {
        "status": STATUS_FAILED, "updated_at": int(now),
        "error": {"code": code, "message": message[:500], "at": int(now),
                  "uncertain": bool(uncertain)},
    }})


async def mark_missed(doc: dict, token: str, *, now: int) -> bool:
    late = max(int(now) - int(doc["scheduled_at"]), 0)
    return await _finish(doc, token, {"$set": {
        "status": STATUS_MISSED, "updated_at": int(now),
        "error": {"code": "MISSED",
                  "message": f"The bot was offline at the scheduled time "
                             f"(found {late // 60} minute(s) late); not sent.",
                  "at": int(now), "uncertain": False},
    }})


async def defer(doc: dict, token: str, *, retry_at: int, now: int, reason: str) -> bool:
    """Telegram refused for now (FloodWait): back to scheduled, retry later."""
    return await _finish(doc, token, {"$set": {
        "status": STATUS_SCHEDULED, "retry_at": int(retry_at), "updated_at": int(now),
        "last_deferral": reason[:200],
    }, "$unset": {"claimed_at": ""}})


async def mark_daily_sent(doc: dict, token: str, *, message_id: Optional[int], now: int,
                          next_run: int, run_key: str,
                          warnings: Optional[List[str]] = None) -> bool:
    """A daily occurrence was published: record it and move to ``next_run``
    in the same update (so the occurrence can never be claimed again)."""
    fields: Dict[str, Any] = {
        "status": STATUS_SCHEDULED, "scheduled_at": int(next_run), "attempts": 0,
        "updated_at": int(now), "last_run_at": int(now), "last_run_key": run_key,
        "last_sent_message_id": message_id,
        "late_by": max(int(now) - int(doc["scheduled_at"]), 0),
    }
    unset = {"error": "", "claimed_at": "", "retry_at": ""}
    if warnings:
        fields["delivery_warnings"] = list(warnings)
    else:
        unset["delivery_warnings"] = ""
    if await _finish(doc, token, {"$set": fields, "$unset": unset, "$inc": {"run_count": 1}}):
        return True
    # The claim was taken over meanwhile (stale recovery already moved the
    # schedule on) — the occurrence WAS published, so record that anyway.
    log.warning("daily schedule #%s: claim lost before mark_daily_sent; recording delivery anyway",
                doc.get("schedule_id"))
    await scheduled_db.update_one({"_id": doc["_id"]}, {
        "$set": {"last_run_at": int(now), "last_run_key": run_key,
                 "last_sent_message_id": message_id},
        "$unset": {"error": ""},
        "$inc": {"run_count": 1},
    })
    return True


async def advance_daily(doc: dict, token: str, *, next_run: int, now: int,
                        error: Optional[Dict[str, Any]] = None,
                        skip: Optional[Dict[str, Any]] = None) -> bool:
    """Leave a daily occurrence unpublished and move to ``next_run``: it
    failed (``error``), was missed while the bot was offline (``skip``), or
    was already published (neither). The schedule stays active."""
    fields: Dict[str, Any] = {"status": STATUS_SCHEDULED, "scheduled_at": int(next_run),
                              "attempts": 0, "updated_at": int(now)}
    update: Dict[str, Any] = {"$set": fields, "$unset": {"claimed_at": "", "retry_at": ""}}
    if error is not None:
        fields["error"] = {**error, "occurrence": int(doc["scheduled_at"])}
        update["$inc"] = {"failure_count": 1}
    if skip is not None:
        fields["last_skip"] = skip
        update["$inc"] = {"skip_count": 1}
    return await _finish(doc, token, update)


_INTERRUPTED_ONCE = ("The bot stopped while sending this post, so Telegram's answer is "
                     "unknown. Check the channel before rescheduling to avoid a duplicate.")
_INTERRUPTED_DAILY = ("The bot stopped while sending this occurrence, so Telegram's answer "
                      "is unknown. It is not sent again (that could post it twice); the "
                      "schedule continues with the next day.")


async def recover_stale_sending(now: int, stale_after: int) -> List[dict]:
    """Schedules left in "sending" by a stopped bot.

    The send may or may not have reached Telegram, so it is NOT retried —
    that could post it twice. A one-time post becomes "failed (uncertain)"
    for the admin to check; a daily schedule records the uncertain error and
    continues with its next occurrence (skipping that IST day).
    """
    recovered = []
    cutoff = int(now) - int(stale_after)
    for _ in range(500):  # bounded: each pass handles one stale document
        doc = await scheduled_db.find_one({"status": STATUS_SENDING, "claimed_at": {"$lte": cutoff}})
        if doc is None:
            break
        error = {"code": "INTERRUPTED", "at": int(now), "uncertain": True}
        if is_daily(doc):
            occurrence = int(doc["scheduled_at"])
            error.update(message=_INTERRUPTED_DAILY, occurrence=occurrence)
            update = {"$set": {
                "status": STATUS_SCHEDULED, "attempts": 0, "updated_at": int(now), "error": error,
                "scheduled_at": next_occurrence(doc, max(int(now), occurrence),
                                                schedule_time.run_key(occurrence)),
            }, "$unset": {"claim_token": "", "claimed_at": "", "retry_at": ""},
                "$inc": {"failure_count": 1}}
        else:
            error["message"] = _INTERRUPTED_ONCE
            update = {"$set": {"status": STATUS_FAILED, "updated_at": int(now), "error": error},
                      "$unset": {"claim_token": ""}}
        updated = await scheduled_db.find_one_and_update(
            {"_id": doc["_id"], "status": STATUS_SENDING, "claim_token": doc.get("claim_token")},
            update, return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            recovered.append(updated)
    return recovered


async def next_due_at() -> Optional[int]:
    """When the next scheduled post becomes due (epoch seconds), or None.

    Lets the worker sleep until exactly then instead of polling. Two indexed
    lookups: the earliest post without a retry delay, and the earliest
    FloodWait-deferred one (due at its retry time).
    """
    due: List[int] = []
    first = await scheduled_db.find_one(
        {"status": STATUS_SCHEDULED, "retry_at": {"$exists": False}},
        sort=[("scheduled_at", ASCENDING)], projection={"scheduled_at": 1},
    )
    if first is not None:
        due.append(int(first["scheduled_at"]))
    deferred = await scheduled_db.find_one(
        {"status": STATUS_SCHEDULED, "retry_at": {"$exists": True}},
        sort=[("retry_at", ASCENDING)], projection={"scheduled_at": 1, "retry_at": 1},
    )
    if deferred is not None:
        due.append(max(int(deferred["retry_at"]), int(deferred["scheduled_at"])))
    return min(due) if due else None


__all__ = [
    "scheduled_db", "counters_db", "ensure_schedule_indexes", "next_schedule_id",
    "STATUS_SCHEDULED", "STATUS_SENDING", "STATUS_SENT", "STATUS_FAILED",
    "STATUS_CANCELLED", "STATUS_MISSED", "PENDING_STATUSES", "HISTORY_STATUSES",
    "EDITABLE_STATUSES", "RESCHEDULABLE_STATUSES", "CANCELLABLE_STATUSES",
    "DELETABLE_STATUSES", "DuplicateDraft", "find_identical", "create_schedule",
    "get_schedule", "list_schedules", "update_content", "reschedule",
    "cancel_schedule", "delete_schedule", "new_claim_token", "claim_next_due",
    "mark_sent", "mark_failed", "mark_missed", "defer", "recover_stale_sending",
    "STATUS_PAUSED", "TYPE_ONCE", "TYPE_DAILY", "PAUSABLE_STATUSES",
    "RETIMABLE_STATUSES", "schedule_type", "is_daily", "daily_time_of",
    "next_occurrence", "find_identical_daily", "pause_schedule", "resume_schedule",
    "change_daily_time", "mark_daily_sent", "advance_daily", "next_due_at",
    "start_date_of",
]
