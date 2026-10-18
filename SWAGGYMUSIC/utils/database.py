#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
SWAGGYMUSIC database helpers — standalone channel-management build.

Music-only helpers (assistant, autoplay, music-on/off, stream queue,
thumbnail, playmode, music-cache, etc.) have been removed.

What remains is the shared infrastructure used by the Start UI, Help menu,
sudo system, banned-user filter, and the autodelete/setgap collections.

=====================================================================
 SCHEMA DOCUMENTATION (autodelete_settings + autodelete_jobs)
=====================================================================

  autodelete_settings  — one document per channel
  ------------------------------------------------------------
  {
    _id:                  ObjectId,
    chat_id:              <int>           # UNIQUE index, primary key
    delay:                <int seconds>,  # present only when /setdelay enabled
    enabled:              <bool>,         # present only when /setdelay enabled
    gap:                  <int seconds>,  # present only when /setgap enabled
    gap_enabled:          <bool>,         # present only when /setgap enabled
    last_gap_post_at:     <int unix ts>,  # present after first allowed gap post
    last_gap_post_msg_id: <int msg_id>,   # present after first allowed gap post
    gap_history: [                        # newest 50 ACCEPTED gap posts
      { msg_id: <int>, post_at: <int unix ts>,   # original Telegram time
        media_group_id: <int>,                   # album items only
        bot_delete: "pending"|"confirmed",       # written ONLY by the
        bot_deleted_at: <int unix ts> }          #   /setdelay worker
    ],                                    # (manual-deletion fallback; reset
                                          #  only on first activation or a
                                          #  duration change, removed on
                                          #  disable — see set_gap_duration)
    gap_albums: [                         # newest 100 media-album verdicts
      { media_group_id: <int>,            # (one /setgap decision per album;
        verdict: "accepted"|"rejected",   #  kept across /setgap changes so
        post_at: <int unix ts>,           #  an album is never split)
        msg_id: <int> }
    ],
    anonymous_filter_enabled: <bool>,     # /setanonymous
    anon_rejected_msg_ids: [<int>, ...]   # newest 200 posts rejected by the
                                          # anonymous filter (never accepted
                                          # as /setgap references)
    link_filter_enabled: <bool>,          # /setlink — delete channel posts
                                          # that carry prohibited links
    link_blocked_albums: [<int>, ...]     # newest 50 media-group ids the link
  }                                       # filter condemned (whole-album rule
                                          # survives a restart mid-delivery)

  autodelete_jobs  — one document per pending deletion
  ------------------------------------------------------------
  {
    _id:                  ObjectId,
    chat_id:              <int>,                # part of UNIQUE (chat_id, message_id)
    message_id:           <int>,                # part of UNIQUE (chat_id, message_id)
    delete_at:            <int unix ts>,        # when the worker should delete
                                                # (immutable after $setOnInsert;
                                                #  the worker may move it for retry)
    expires_at:           <BSON Date UTC>,      # TTL safety-net — MongoDB TTL
                                                # reaper deletes the doc at this
                                                # date. Stored as BSON Date so
                                                # the TTL index actually works
                                                # (MongoDB TTL indexes require
                                                #  BSON Date values).
    attempts:             <int>,                # retry counter (FloodWait does
                                                #  NOT increment this)
    original_post_time:   <int unix ts>,        # immutable after $setOnInsert
    reason:               "GAP_REJECT_RETRY"    # only on retry jobs for
                          | "ANON_REJECT_RETRY" #  rejected posts (FloodWait)
                          | "LINK_REJECT_RETRY",#  (see link_policy)
    inflight:             <bool>                # a delete call was sent and
                                                #  its outcome is unknown
  }

  Required indexes
  ------------------------------------------------------------
  settings_db:
    - {chat_id: 1}  UNIQUE              (essential — prevents duplicate settings)
  jobs_db:
    - {(chat_id, message_id): 1}  UNIQUE (essential — prevents duplicate jobs)
    - {delete_at: 1}                  (essential — worker query filter/sort)
    - {expires_at: 1}  TTL             (safety-net GC; expireAfterSeconds=0)

  Migration
  ------------------------------------------------------------
  See :func:`migrate_jobs_ttl_field` below. It runs on every boot and
  is idempotent: it converts any old integer-typed ``expires_at`` values
  to BSON Date. Safe to run repeatedly. See the function docstring for
  details.

  Rollback
  ------------------------------------------------------------
  To roll back to the pre-Part-2 schema:
    1. Drop the TTL index: ``db.autodelete_jobs.dropIndex("expires_at_1")``
    2. (Optional) Convert ``expires_at`` back to int — but this is NOT
       required; the worker treats any value as opaque.
    3. Recreate the old TTL index: ``db.autodelete_jobs.createIndex(
         {expires_at: 1}, {expireAfterSeconds: 0})`` — it will silently
       no-op on int values (Mongo TTL only fires on BSON Date), exactly
       the pre-fix behaviour.

  Compatibility notes
  ------------------------------------------------------------
  - ``delete_at`` is intentionally kept as an INT unix timestamp. It is
    used in worker queries (``{"delete_at": {"$lte": now}}``) and in
    retry scheduling. Converting it to BSON Date would require touching
    every worker query and every retry path; it is NOT needed for TTL
    (only ``expires_at`` is TTL-indexed). Keeping it as int preserves
    backward compatibility with existing documents and with the
    retry/FloodWait logic in autodelete.py.
  - ``original_post_time`` and ``last_gap_post_at`` are also kept as
    int unix timestamps — they are not TTL-managed and changing their
    type would break the gap arithmetic.
  - Only ``expires_at`` is converted to BSON Date, because that is the
    only field with a TTL index.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Union

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import (
    CollectionInvalid,
    DuplicateKeyError,
    OperationFailure,
)

from SWAGGYMUSIC.core.mongo import mongodb
from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)

# ---------------------------------------------------------------------------
# Collections kept by the channel-management build
# ---------------------------------------------------------------------------

authdb = mongodb.adminauth          # reserved for /auth feature parity (unused now)
authuserdb = mongodb.authuser       # reserved (unused now)
blacklist_chatdb = mongodb.blacklistChat
blockeddb = mongodb.blockedusers
chatsdb = mongodb.chats
gbansdb = mongodb.gban
langdb = mongodb.language
onoffdb = mongodb.onoffper
sudoersdb = mongodb.sudoers
usersdb = mongodb.tgusersdb

# ---------------------------------------------------------------------------
# Channel-management collections — the core of this build
# ---------------------------------------------------------------------------

settings_db = mongodb.autodelete_settings
jobs_db = mongodb.autodelete_jobs


# ---------------------------------------------------------------------------
# Timestamp helpers — single source of truth for TTL date conversion
# ---------------------------------------------------------------------------

def _ttl_date_from_unix(ts: int) -> datetime:
    """Convert a unix-seconds timestamp to a timezone-aware UTC
    ``datetime`` suitable for storing under a TTL-indexed field.

    MongoDB TTL indexes require BSON Date values. Motor / PyMongo
    encode naive ``datetime`` objects as BSON Date but treat them as
    UTC on the server; to avoid any ambiguity we always build a
    timezone-aware UTC datetime.
    """
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


# ---------------------------------------------------------------------------
# Index bootstrap — idempotent, called on every boot
# ---------------------------------------------------------------------------

# Index names — explicit so we can detect/manage them by name.
_SETTINGS_CHAT_ID_IDX = "chat_id_1"
_JOBS_CHAT_MSG_IDX = "chat_id_1_message_id_1"
_JOBS_DELETE_AT_IDX = "delete_at_1"
_JOBS_EXPIRES_AT_IDX = "expires_at_1"


async def _create_index_safe(collection, keys, *, name: str, **kwargs) -> bool:
    """Create one index, logging the outcome.

    Returns True if the index now exists with the requested spec.

    ``DuplicateKeyError`` is treated as a soft success: it means the
    index already exists with the same spec (or a compatible spec) —
    ``create_index`` is idempotent for the same spec, so this should be
    rare, but if it happens we treat it as "index is present".
    """
    try:
        await collection.create_index(keys, name=name, **kwargs)
        return True
    except DuplicateKeyError:
        log.info(
            "Index %s on %s already exists (DuplicateKey)",
            name, collection.name,
        )
        return True
    except OperationFailure as exc:
        code = getattr(exc, "code", None)
        log.warning(
            "Index creation failed on %s name=%s code=%s: %s",
            collection.name, name, code, exc,
        )
        raise
    except CollectionInvalid as exc:
        log.warning(
            "Index creation failed on %s name=%s: collection invalid: %s",
            collection.name, name, exc,
        )
        raise
    except Exception as exc:
        log.error(
            "Unexpected error creating index %s on %s: %s: %s",
            name, collection.name, type(exc).__name__, exc,
        )
        raise


async def _drop_index_safe(collection, name: str) -> None:
    """Drop an index by name, swallowing 'index not found' errors."""
    try:
        await collection.drop_index(name)
    except OperationFailure as exc:
        code = getattr(exc, "code", None)
        # 27 (IndexNotFound) is benign — nothing to drop.
        if code == 27:
            return
        log.warning(
            "Could not drop index %s on %s (code=%s): %s",
            name, collection.name, code, exc,
        )
        raise


async def _verify_unique_index(collection, name: str) -> None:
    """Confirm that index ``name`` exists and is unique.

    Raises :class:`RuntimeError` if the index is missing or not unique.
    Called after creation to validate the essential uniqueness
    guarantees the application relies on.
    """
    infos = await collection.index_information()
    info = infos.get(name)
    if info is None:
        raise RuntimeError(
            f"Required unique index {name!r} is missing on {collection.name}"
        )
    if not info.get("unique"):
        raise RuntimeError(
            f"Index {name!r} on {collection.name} exists but is NOT unique — "
            "the application cannot guarantee duplicate-job prevention. "
            "Drop the non-unique index and restart the bot to recreate it."
        )


async def ensure_channel_indexes() -> None:
    """Create the indexes required by autodelete_settings / autodelete_jobs.

    Idempotent — safe to call on every restart.

    Behaviour change vs. pre-Part-2:
        - Index creation errors are NO LONGER silently suppressed. The
          ``contextlib.suppress(Exception)`` wrapper is gone. Failures
          now propagate to :func:`SWAGGYMUSIC.__main__.init`, which
          aborts startup — running the bot without the unique
          constraints would silently allow duplicate settings and
          duplicate deletion jobs.
        - The TTL index on ``expires_at`` is recreated if it exists with
          a non-TTL spec (e.g. an old install that created it as a
          plain index). See ``_ensure_ttl_index_on_expires_at``.
        - After creation, the essential unique indexes are verified to
          exist and to actually be unique. This catches the case where
          an old non-unique index with the same name blocks creation of
          the unique one.

    Essential indexes (startup fails if these cannot be established):
        - settings_db:  {chat_id: 1}  UNIQUE
        - jobs_db:      {(chat_id, message_id): 1}  UNIQUE
        - jobs_db:      {delete_at: 1}              (worker query/sort)

    Safety-net indexes (created best-effort, not fatal if missing):
        - jobs_db:      {expires_at: 1}  TTL  (expireAfterSeconds=0)
    """
    # ─── 1. Essential unique index on settings_db.chat_id ──────────────
    try:
        await _create_index_safe(
            settings_db, [("chat_id", ASCENDING)],
            name=_SETTINGS_CHAT_ID_IDX, unique=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to create essential unique index {_SETTINGS_CHAT_ID_IDX!r} "
            f"on autodelete_settings: {type(exc).__name__}: {exc}"
        ) from exc
    await _verify_unique_index(settings_db, _SETTINGS_CHAT_ID_IDX)

    # ─── 2. Essential unique index on jobs_db.(chat_id, message_id) ────
    try:
        await _create_index_safe(
            jobs_db,
            [("chat_id", ASCENDING), ("message_id", ASCENDING)],
            name=_JOBS_CHAT_MSG_IDX, unique=True,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to create essential unique index {_JOBS_CHAT_MSG_IDX!r} "
            f"on autodelete_jobs: {type(exc).__name__}: {exc}"
        ) from exc
    await _verify_unique_index(jobs_db, _JOBS_CHAT_MSG_IDX)

    # ─── 3. Essential non-unique index on jobs_db.delete_at ────────────
    # The worker filters ``{"delete_at": {"$lte": now}}`` and sorts by
    # ``delete_at``. Without this index, every worker tick scans the
    # whole collection.
    try:
        await _create_index_safe(
            jobs_db, [("delete_at", ASCENDING)],
            name=_JOBS_DELETE_AT_IDX,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to create essential index {_JOBS_DELETE_AT_IDX!r} "
            f"on autodelete_jobs: {type(exc).__name__}: {exc}"
        ) from exc

    # ─── 4. TTL safety-net index on jobs_db.expires_at ─────────────────
    # TTL indexes REQUIRE BSON Date values. Pre-Part-2 the code wrote
    # integer timestamps into ``expires_at`` AND created a TTL index on
    # it — but MongoDB silently ignores non-Date values in TTL indexes,
    # so the TTL reaper never fired. We:
    #   a. Recreate the index if it exists with a non-TTL spec.
    #   b. Migrate any existing int-typed ``expires_at`` values to BSON
    #      Date (see ``migrate_jobs_ttl_field``).
    # The TTL index itself is best-effort — if the host MongoDB does
    # not allow TTL indexes (rare; e.g. some managed shared clusters),
    # the worker's manual sweep still works. We log a warning but do
    # not abort startup.
    await _ensure_ttl_index_on_expires_at()

    # ─── 5. Best-effort migration of existing int expires_at → Date ────
    # Safe to run on every boot. Idempotent. No data loss.
    try:
        await migrate_jobs_ttl_field()
    except Exception as exc:
        # Migration failure is not fatal — the worker can still process
        # jobs whose expires_at is a Date; jobs whose expires_at is
        # still an int will simply not be TTL-reaped (but the worker's
        # manual sweep will still clean them up by expires_at-as-int).
        log.warning(
            "jobs_db.expires_at migration failed (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )

    # ─── 6. createpost_sessions indexes ────────────────────────────────
    # Persistent /createpost wizard sessions (Part 3). Best-effort —
    # if creation fails the wizard still works in-memory, just without
    # restart recovery. The helper is imported lazily to avoid an
    # import cycle at module load.
    try:
        from SWAGGYMUSIC.utils.createpost_sessions import ensure_session_indexes
        await ensure_session_indexes()
    except Exception as exc:
        log.warning(
            "createpost_sessions index creation failed (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )


async def _ensure_ttl_index_on_expires_at() -> None:
    """Ensure ``expires_at`` has a TTL index with ``expireAfterSeconds=0``.

    If the index exists but is NOT a TTL index (e.g. an old install
    created it as a plain ascending index), it is dropped and recreated
    as a TTL index. If creation as TTL fails for any reason, a plain
    index is created as a fallback (so at least the worker's manual
    sweep query is efficient).
    """
    infos = await jobs_db.index_information()
    info = infos.get(_JOBS_EXPIRES_AT_IDX)

    if info is not None:
        # Index exists — check whether it's a TTL index.
        # Motor returns ``{"key": [("expires_at", 1)], "expireAfterSeconds": 0.0, "v": 2}``
        # for a TTL index. If ``expireAfterSeconds`` is absent, it's a
        # plain index — we must drop and recreate.
        if "expireAfterSeconds" in info:
            return
        log.warning(
            "Index %s on autodelete_jobs exists but is NOT a TTL index "
            "(missing expireAfterSeconds). Dropping and recreating as TTL.",
            _JOBS_EXPIRES_AT_IDX,
        )
        await _drop_index_safe(jobs_db, _JOBS_EXPIRES_AT_IDX)

    try:
        await _create_index_safe(
            jobs_db, [("expires_at", ASCENDING)],
            name=_JOBS_EXPIRES_AT_IDX, expireAfterSeconds=0,
        )
    except Exception as exc:
        # TTL index creation failed — fall back to a plain index so the
        # worker's manual sweep is at least index-accelerated.
        log.warning(
            "Could not create TTL index %s on autodelete_jobs (%s: %s) "
            "— falling back to a plain index. TTL reaping will NOT work; "
            "the worker's manual sweep will still clean up old jobs.",
            _JOBS_EXPIRES_AT_IDX, type(exc).__name__, exc,
        )
        try:
            await _create_index_safe(
                jobs_db, [("expires_at", ASCENDING)],
                name=_JOBS_EXPIRES_AT_IDX,
            )
        except Exception as exc2:
            log.warning(
                "Plain fallback for %s also failed: %s: %s "
                "— worker sweep will use a collection scan.",
                _JOBS_EXPIRES_AT_IDX, type(exc2).__name__, exc2,
            )


async def migrate_jobs_ttl_field(*, batch_size: int = 500) -> int:
    """Convert any int-typed ``expires_at`` values in jobs_db to BSON Date.

    Pre-Part-2 stored ``expires_at`` as an integer unix timestamp while
    creating a TTL index on it — but MongoDB TTL indexes only fire on
    BSON Date values, so the TTL reaper never actually deleted anything.
    This migration converts every existing int-typed ``expires_at`` to
    the equivalent BSON UTC Date so the TTL index starts working.

    Idempotent: a second run is a no-op (the filter matches only ints).

    Safe: each document is updated individually with a precondition that
    ``expires_at`` is still an int (in case the worker is concurrently
    inserting new docs with Date-typed expires_at — which it now does).

    Non-destructive: only the ``expires_at`` field is touched. No other
    field is changed, no document is deleted, no collection is dropped.

    Returns the number of documents updated.
    """
    total_updated = 0
    # Walk in batches using _id > last_seen to avoid the cursor being
    # invalidated by the updates themselves. The filter matches any
    # document whose expires_at is not a BSON Date.
    last_id = None
    while True:
        query = {"expires_at": {"$not": {"$type": "date"}}}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        cursor = (
            jobs_db.find(query, {"_id": 1, "expires_at": 1})
            .sort("_id", ASCENDING)
            .limit(batch_size)
        )
        batch = await cursor.to_list(length=batch_size)
        if not batch:
            break
        for doc in batch:
            last_id = doc["_id"]
            raw = doc.get("expires_at")
            if raw is None or isinstance(raw, datetime):
                continue
            try:
                ts = int(raw)
            except (TypeError, ValueError):
                log.warning(
                    "jobs_db doc %s has non-numeric expires_at=%r; skipping",
                    doc["_id"], raw,
                )
                continue
            new_date = _ttl_date_from_unix(ts)
            try:
                await jobs_db.update_one(
                    {"_id": doc["_id"], "expires_at": raw},
                    {"$set": {"expires_at": new_date}},
                )
                total_updated += 1
            except Exception as exc:
                log.warning(
                    "jobs_db expires_at migration failed for doc %s: %s: %s",
                    doc["_id"], type(exc).__name__, exc,
                )
        if len(batch) < batch_size:
            break
    if total_updated:
        log.info(
            "jobs_db.expires_at migration: converted %s document(s) from int to BSON Date.",
            total_updated,
        )
    return total_updated


# ---------------------------------------------------------------------------
# Shared channel-setting writes (/setgap, /setdelay, /setanonymous)
# ---------------------------------------------------------------------------

# Gap-window state: the reference post and the accepted-post history.
# (``gap_albums`` is deliberately NOT part of it: an album that is being
# delivered while /setgap changes keeps its verdict for all of its items.)
GAP_WINDOW_FIELDS = ("last_gap_post_at", "last_gap_post_msg_id", "gap_history")


async def set_gap_duration(chat_id: int, seconds: int) -> Tuple[str, int]:
    """Enable /setgap or change its duration, atomically.

    Used by the channel command and the private-chat wizard. Returns
    ``(status, previous_seconds)``:

    * ``"NO_CHANGE"`` — /setgap is already enabled with this duration.
      Nothing is written: the running timer (reference post) and the
      accepted-post history are preserved.
    * ``"ENABLED"`` — first activation (or re-activation after disable):
      a fresh gap window starts.
    * ``"UPDATED"`` — the duration changed: a fresh gap window starts, so
      posts accepted under the old duration do not block the new one.

    The reset is one compare-and-set that only matches a document whose gap
    is NOT already ``seconds``. Two identical commands racing each other,
    or a re-run racing a new post, can therefore never reset the window
    twice or wipe a post accepted in between.
    """
    seconds = int(seconds)
    for _attempt in range(3):
        if await settings_db.find_one(
            {"chat_id": chat_id, "gap_enabled": True, "gap": seconds}, {"_id": 1},
        ):
            return "NO_CHANGE", seconds

        prior = await settings_db.find_one_and_update(
            {
                "chat_id": chat_id,
                "$or": [{"gap_enabled": {"$ne": True}}, {"gap": {"$ne": seconds}}],
            },
            {
                "$set": {"chat_id": chat_id, "gap": seconds, "gap_enabled": True},
                "$unset": {field: "" for field in GAP_WINDOW_FIELDS},
            },
            return_document=ReturnDocument.BEFORE,
        )
        if prior is not None:
            if prior.get("gap_enabled"):
                try:
                    return "UPDATED", int(prior.get("gap") or 0)
                except (TypeError, ValueError):
                    return "UPDATED", 0
            return "ENABLED", 0

        # No document yet (or it was set to this duration concurrently).
        result = await settings_db.update_one(
            {"chat_id": chat_id},
            {"$setOnInsert": {"chat_id": chat_id, "gap": seconds, "gap_enabled": True}},
            upsert=True,
        )
        if result.upserted_id is not None:
            return "ENABLED", 0
    return "NO_CHANGE", seconds


async def delete_settings_if_unused(chat_id: int) -> bool:
    """Delete a channel's settings document only if NO feature uses it.

    /setgap, /setdelay, /setanonymous and /setlink all live in this
    document, so it may only go when all four are off. Done as one
    conditional delete so a feature enabled concurrently is never wiped.
    """
    result = await settings_db.delete_one({
        "chat_id": chat_id,
        "gap_enabled": {"$ne": True},
        "gap": {"$exists": False},
        "enabled": {"$ne": True},
        "delay": {"$exists": False},
        "anonymous_filter_enabled": {"$ne": True},
        "link_filter_enabled": {"$ne": True},
    })
    return bool(result.deleted_count)


async def set_link_filter_enabled(chat_id: int, enabled: bool) -> str:
    """Enable or disable /setlink for one channel.

    Used by the channel command and the private-chat wizard. Returns
    ``"ENABLED"``, ``"DISABLED"`` or ``"NO_CHANGE"`` (the setting already
    had this value — nothing was written). Disabling also drops the
    channel's condemned-album list and removes the settings document when
    no other feature uses it.
    """
    chat_id = int(chat_id)
    if await settings_db.find_one(
        {"chat_id": chat_id, "link_filter_enabled": bool(enabled)}, {"_id": 1},
    ):
        return "NO_CHANGE"
    if enabled:
        await settings_db.update_one(
            {"chat_id": chat_id},
            {"$set": {"chat_id": chat_id, "link_filter_enabled": True}},
            upsert=True,
        )
        return "ENABLED"
    result = await settings_db.update_one(
        {"chat_id": chat_id, "link_filter_enabled": True},
        {"$unset": {"link_filter_enabled": "", "link_blocked_albums": ""}},
    )
    if not result.modified_count:
        return "NO_CHANGE"
    await delete_settings_if_unused(chat_id)
    return "DISABLED"


# How many condemned album ids are kept per channel in MongoDB
# (link_policy documents the whole-album rule this bound serves).
LINK_BLOCKED_ALBUMS_LIMIT = 50


async def remember_blocked_album(chat_id: int, media_group_id: int) -> None:
    """Persist a condemned album id in the channel's settings document.

    Keeps the newest ``LINK_BLOCKED_ALBUMS_LIMIT`` ids so the whole-album
    rule survives a restart between album-item deliveries (mirrors the
    /setgap engine's ``gap_albums`` persistence). Best-effort: callers
    treat a failure as "album tracked in-process only".
    """
    await settings_db.update_one(
        {"chat_id": int(chat_id)},
        {"$push": {"link_blocked_albums": {
            "$each": [int(media_group_id)],
            "$slice": -LINK_BLOCKED_ALBUMS_LIMIT,
        }}},
        upsert=True,
    )


def deletion_job_document(
    chat_id: int, message_id: int, *, delete_at: int,
    original_post_time: int, reason: Optional[str] = None,
) -> dict:
    """The autodelete_jobs document for one pending deletion.

    ``reason`` is stored only for retry jobs that are not /setdelay
    deletions (``GAP_REJECT_RETRY``, ``ANON_REJECT_RETRY``).
    """
    delete_at = int(delete_at)
    job = {
        "chat_id": chat_id,
        "message_id": message_id,
        "delete_at": delete_at,
        # TTL safety net: BSON Date, 7 days after the deadline.
        "expires_at": _ttl_date_from_unix(delete_at + 7 * 24 * 60 * 60),
        "attempts": 0,
        "original_post_time": int(original_post_time),
    }
    if reason:
        job["reason"] = reason
    return job


async def queue_deletion_job(
    chat_id: int, message_id: int, *, delete_at: int,
    original_post_time: int, reason: Optional[str] = None,
) -> None:
    """Insert a job for the persistent deletion worker (never overwrites).

    ``$setOnInsert`` + the unique (chat_id, message_id) index mean an
    existing job — and its ``delete_at`` — is never changed.
    """
    await jobs_db.update_one(
        {"chat_id": chat_id, "message_id": message_id},
        {"$setOnInsert": deletion_job_document(
            chat_id, message_id, delete_at=delete_at,
            original_post_time=original_post_time, reason=reason,
        )},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Language helper (used by Start/Help/inline callbacks)
# ---------------------------------------------------------------------------

async def get_lang(chat_id: int) -> str:
    lang = await langdb.find_one({"chat_id": chat_id})
    if not lang:
        return "en"
    return lang["lang"]


async def set_lang(chat_id: int, lang: str) -> None:
    await langdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "lang": lang}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Maintenance flag (used by language decorator)
# ---------------------------------------------------------------------------

async def is_maintenance() -> bool:
    flag = await onoffdb.find_one({"on_off": 1})
    return bool(flag["mode"]) if flag else True


async def maintenance_on() -> None:
    await onoffdb.update_one(
        {"on_off": 1}, {"$set": {"mode": False}}, upsert=True
    )


async def maintenance_off() -> None:
    await onoffdb.update_one(
        {"on_off": 1}, {"$set": {"mode": True}}, upsert=True
    )


# ---------------------------------------------------------------------------
# on/off helpers (used by Start / privacy / sudo plugins)
# ---------------------------------------------------------------------------

async def is_on_off(on_off: int) -> bool:
    flag = await onoffdb.find_one({"on_off": on_off})
    return bool(flag["mode"]) if flag else False


async def add_on(on_off: int) -> None:
    await onoffdb.update_one(
        {"on_off": on_off}, {"$set": {"mode": True}}, upsert=True
    )


async def add_off(on_off: int) -> None:
    await onoffdb.update_one(
        {"on_off": on_off}, {"$set": {"mode": False}}, upsert=True
    )


# ---------------------------------------------------------------------------
# Served chats / users (kept for Start handler)
# ---------------------------------------------------------------------------

async def is_served_user(user_id: int) -> bool:
    return bool(await usersdb.find_one({"user_id": user_id}))


async def get_served_users() -> list:
    return [user["user_id"] for user in await usersdb.find({}).to_list(None)]


async def add_served_user(user_id: int) -> None:
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def get_served_chats() -> list:
    return [chat["chat_id"] for chat in await chatsdb.find({}).to_list(None)]


async def is_served_chat(chat_id: int) -> bool:
    return bool(await chatsdb.find_one({"chat_id": chat_id}))


async def add_served_chat(chat_id: int) -> None:
    await chatsdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Blacklisted chats (kept for Start handler exit path)
# ---------------------------------------------------------------------------

async def blacklisted_chats() -> list:
    return [chat["chat_id"] for chat in await blacklist_chatdb.find({}).to_list(None)]


async def blacklist_chat(chat_id: int) -> bool:
    if await blacklist_chatdb.find_one({"chat_id": chat_id}):
        return False
    await blacklist_chatdb.insert_one({"chat_id": chat_id})
    return True


async def whitelist_chat(chat_id: int) -> bool:
    if not await blacklist_chatdb.find_one({"chat_id": chat_id}):
        return False
    await blacklist_chatdb.delete_one({"chat_id": chat_id})
    return True


# ---------------------------------------------------------------------------
# Sudoers / ban helpers (kept for sudo plugin + start banner)
# ---------------------------------------------------------------------------

async def get_sudoers() -> list:
    sudoers_doc = await sudoersdb.find_one({"sudo": "sudo"})
    return sudoers_doc["sudoers"] if sudoers_doc else []


async def add_sudo(user_id: int) -> bool:
    sudoers = await get_sudoers()
    if user_id in sudoers:
        return False
    sudoers.append(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": sudoers}},
        upsert=True,
    )
    return True


async def remove_sudo(user_id: int) -> bool:
    sudoers = await get_sudoers()
    if user_id not in sudoers:
        return False
    sudoers.remove(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": sudoers}},
        upsert=True,
    )
    return True


async def get_gbanned() -> list:
    return [user["user_id"] for user in await gbansdb.find({}).to_list(None)]


async def is_gbanned_user(user_id: int) -> bool:
    return bool(await gbansdb.find_one({"user_id": user_id}))


async def add_gban_user(user_id: int) -> None:
    await gbansdb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def remove_gban_user(user_id: int) -> None:
    await gbansdb.delete_one({"user_id": user_id})


async def get_banned_users() -> list:
    return [user["user_id"] for user in await blockeddb.find({}).to_list(None)]


async def get_banned_count() -> int:
    return await blockeddb.count_documents({})


async def is_banned_user(user_id: int) -> bool:
    return bool(await blockeddb.find_one({"user_id": user_id}))


async def add_banned_user(user_id: int) -> None:
    await blockeddb.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def remove_banned_user(user_id: int) -> None:
    await blockeddb.delete_one({"user_id": user_id})


# ---------------------------------------------------------------------------
# Auth users — kept for parity, unused by channel-management build
# ---------------------------------------------------------------------------

async def _get_authusers(chat_id: int) -> Dict[str, int]:
    return await authuserdb.find_one({"chat_id": chat_id}) or {}


async def get_authuser_names(chat_id: int) -> List[str]:
    return [user["name"] for user in (await _get_authusers(chat_id)).get("users", [])]


async def get_authuser(chat_id: int, name: str) -> Union[bool, dict]:
    for user in (await _get_authusers(chat_id)).get("users", []):
        if user["name"] == name:
            return user
    return False


async def save_authuser(chat_id: int, name: str, note: dict) -> None:
    doc = await _get_authusers(chat_id)
    users = doc.get("users", [])
    users.append({"name": name, **note})
    await authuserdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "users": users}},
        upsert=True,
    )


async def delete_authuser(chat_id: int, name: str) -> bool:
    doc = await _get_authusers(chat_id)
    users = doc.get("users", [])
    new_users = [u for u in users if u["name"] != name]
    if len(new_users) == len(users):
        return False
    await authuserdb.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "users": new_users}},
        upsert=True,
    )
    return True
