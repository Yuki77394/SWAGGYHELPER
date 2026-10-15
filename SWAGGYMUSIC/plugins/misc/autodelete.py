"""
SWAGGYMUSIC — standalone channel-management build.
====================================================

Channel Auto-Delete + Post-Gap control.

Commands (CHANNEL ONLY):
    /setdelay 5s | 30s | 1m | 5m | 1h | 22h | 24h | off
    /setgap   5s | 30s | 1m | 5m | 1h | 22h | 24h | off

`/setdelay on` and `/setgap on` are intentionally INVALID.

Limits:
    minimum 3 seconds
    maximum 24 hours = 86400 seconds

REPOSITORIES / COLLECTIONS (reuse of the existing MongoDB connection):
    mongodb.autodelete_settings  — one document per channel:
        {
            "chat_id": <channel_id>,
            "delay": <seconds>,          # present only when delay enabled
            "enabled": True,             # present only when delay enabled
            "gap": <seconds>,            # present only when gap enabled
            "gap_enabled": True,         # present only when gap enabled
            "last_gap_post_at": <ts>     # present only after first allowed post
        }
    mongodb.autodelete_jobs        — one document per pending deletion:
        {
            "_id": <ObjectId>,
            "chat_id": <channel_id>,
            "message_id": <message_id>,
            "delete_at": <int unix ts>,           # immutable after first insert
            "expires_at": <BSON Date UTC>,        # TTL safety-net — MUST be a
                                                   # BSON Date for the MongoDB
                                                   # TTL index to fire.
            "attempts": <int>,
            "original_post_time": <int unix ts>   # immutable after first insert
        }

DELAY RULE — STRICT:
    delete_at is ALWAYS computed as ``message.date`` (the original Telegram
    post time) + delay. Editing the post does NOT move delete_at, because
    the deletion job document is inserted ONCE via upsert + $setOnInsert.

EDITED-MESSAGE PROTECTION:
    Edited channel posts are filtered out BEFORE delay/gap logic runs —
    they cannot re-trigger delay scheduling, cannot consume a gap slot,
    and cannot update ``last_gap_post_at``. Two layers of defence:
        1. ``filters.channel`` is combined with an explicit check on
           ``message.edit_date`` (None for new posts).
        2. Even if Kurigram routes the edit through ``on_message`` and
           the edit_date attribute is somehow missing, the per-message
           unique index on (chat_id, message_id) plus the $setOnInsert
           update makes the second insert a no-op.

GAP RULE — STRICT (per-channel, atomic):
    The gap is per CHANNEL, not per user. A post is allowed only if
    no surviving post was made within the last ``gap`` seconds.
    Rejected posts (deleted because they violate the gap) MUST NOT:
        - update last_gap_post_at
        - extend the gap
        - become the new reference post
    This is implemented atomically using ``find_one_and_update`` with
    a precondition that ``last_gap_post_at`` is missing or older than
    the cutoff. Two simultaneous posters will race on the same Mongo
    document update — only one will receive a non-None return value.

DELAY + GAP TOGETHER:
    Both work independently. A post that violates the gap is deleted
    immediately and never gets a deletion job. A post that survives
    the gap check is queued for delayed deletion (if delay is enabled).

RESTART BEHAVIOUR:
    Settings persist in MongoDB. Pending deletion jobs persist. The
    background worker resumes on the next boot. Turning a feature off
    does not affect already-scheduled jobs (their delete_at stays).
"""

import asyncio
import re
import time
from contextlib import suppress

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from SWAGGYMUSIC import app
from SWAGGYMUSIC.core.mongo import mongodb
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils.activity_logger import (_format_duration,
                                               log_setdelay_change,
                                               log_setgap_change)
from SWAGGYMUSIC.utils.database import (_ttl_date_from_unix,
                                        ensure_channel_indexes, jobs_db,
                                        settings_db)

log = LOGGER(__name__)

MIN_SECONDS = 3
MAX_SECONDS = 24 * 60 * 60

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)

# Channel-side success responses are auto-deleted after this many seconds.
# The wait is done via a non-blocking asyncio.create_task so the bot stays
# fully responsive during the 2-minute window.
_CHANNEL_RESPONSE_TTL = 120


async def _delete_after(message: Message, delay: int) -> None:
    """Non-blocking delayed deletion helper.

    Spawns a background ``asyncio.create_task`` that sleeps for ``delay``
    seconds and then deletes the given message.  The bot stays fully
    responsive during the wait — this is NOT ``time.sleep()``.

    If the message is already deleted by the time the task fires, the
    resulting Telegram error is safely suppressed.
    """

    async def _inner():
        try:
            await asyncio.sleep(delay)
            with suppress(Exception):
                await message.delete()
        except Exception:
            pass

    asyncio.create_task(_inner())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_duration(value: str):
    """Parse ``3s``/``5m``/``22h`` into an int seconds value.

    Returns None for any unparseable input, including the bare keyword
    ``on`` (which is intentionally rejected by the command handlers).
    Limits: 3 seconds .. 24 hours inclusive.
    """
    match = _DURATION_RE.fullmatch(value or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
    if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        return None
    return seconds


def _is_setdelay_command(message: Message) -> bool:
    text = message.text or message.caption or ""
    return bool(re.match(r"(?i)^/setdelay(?:@\w+)?(?:\s+.+?)?\s*$", text))


def _is_setgap_command(message: Message) -> bool:
    text = message.text or message.caption or ""
    return bool(re.match(r"(?i)^/setgap(?:@\w+)?(?:\s+.+?)?\s*$", text))


def _post_unix_time(message: Message) -> int:
    """Return the original post time as a unix timestamp.

    Kurigram exposes ``message.date`` as a timezone-aware UTC datetime
    when the update came from Telegram (it always does for channel
    posts). We fall back to ``time.time()`` only if the field is
    unavailable, which is essentially impossible in production.
    """
    post_date = getattr(message, "date", None)
    if post_date is None:
        return int(time.time())
    try:
        return int(post_date.timestamp())
    except (AttributeError, OSError, ValueError):
        return int(time.time())


async def _bot_can_delete(chat_id: int) -> bool:
    """Return whether the bot has permission to delete channel posts."""
    try:
        member = await app.get_chat_member(chat_id, app.id)
        if member.status == ChatMemberStatus.OWNER:
            return True
        privileges = member.privileges
        return bool(privileges and privileges.can_delete_messages)
    except Exception as exc:
        # Permission check failure is operationally important — promote to
        # warning so the operator can see why gap/delay silently no-ops.
        # No secrets are included (chat_id is not secret).
        log.warning(
            "Channel admin check failed for %s: %s: %s",
            chat_id, type(exc).__name__, exc,
        )
        return False


async def _gap_allows_post(
    chat_id: int, post_time: int, gap: int, msg_id: int,
) -> bool:
    """Atomically reserve the next allowed-post slot for this channel.

    Posts arriving before the gap expires are NOT allowed to reserve
    the slot, therefore rejected posts cannot update last_gap_post_at
    and cannot extend the gap.

    Implemented as a single find_one_and_update with a precondition
    matching either "no last_gap_post_at yet" OR "last_gap_post_at
    older than the cutoff". The atomicity of Mongo's update means
    that if two posters race here, only one will receive a non-None
    document back — the other will receive None and be rejected.

    Stores both ``last_gap_post_at`` (the post's original Telegram
    timestamp) and ``last_gap_post_msg_id`` (the message ID) so the
    stale-reference detector can verify the reference still exists.
    """
    cutoff = post_time - gap
    updated = await settings_db.find_one_and_update(
        {
            "chat_id": chat_id,
            "gap_enabled": True,
            "gap": gap,
            "$or": [
                {"last_gap_post_at": {"$exists": False}},
                {"last_gap_post_at": {"$lte": cutoff}},
            ],
        },
        {
            "$set": {
                "last_gap_post_at": post_time,
                "last_gap_post_msg_id": msg_id,
            }
        },
        return_document=True,  # ReturnDocument.AFTER
    )
    accepted = updated is not None
    # Decision log — INFO level so the operator can trace gap decisions
    # without enabling DEBUG. No secrets: only chat_id / msg_id / timestamps.
    log.info(
        "gap_decision chat=%s msg=%s post_time=%s gap=%s cutoff=%s accepted=%s",
        chat_id, msg_id, post_time, gap, cutoff, accepted,
    )
    return accepted


# ---------------------------------------------------------------------------
# Stale-reference detection — manual-deletion safety
# ---------------------------------------------------------------------------
#
# When a post is accepted by the gap engine, its message_id is stored as
# ``last_gap_post_msg_id`` alongside ``last_gap_post_at`` (the post's
# original Telegram timestamp).  If the user later MANUALLY DELETES that
# accepted post from the channel, the stored reference becomes stale —
# the timestamp is still valid in MongoDB but the actual message is gone.
#
# Before making the gap decision for a NEW post, ``_resolve_stale_gap_reference``
# checks whether the stored reference message still EXISTS in Telegram.
# If it doesn't, the function resolves the latest EXISTING channel post
# before the current incoming post and updates the stored reference.
#
# This prevents a "phantom gap lock" — a situation where a deleted post
# continues to block new posts for the full gap duration.
#
# The existence check uses ``app.get_messages()`` (single API call) and
# the latest-existing-post resolution uses ``app.get_chat_history(limit=1,
# offset=1)`` (single API call, skips deleted messages automatically).
#
# Both the resolution update and the atomic gap check use preconditions
# to preserve atomicity — if two posts race, only one wins.


async def _message_exists(chat_id: int, message_id: int) -> bool:
    """Check whether a specific message still exists in the channel.

    Uses ``app.get_messages()`` — a single API call.  Returns ``True``
    if the message exists, ``False`` if it was deleted.

    If the API call fails for any reason (network error, timeout, etc.),
    returns ``True`` — the SAFE DEFAULT is to assume the message still
    exists, so we don't block posts due to API errors.  The spec says:
    "Do NOT treat every unexpected API/network error as proof that the
    message was manually deleted."
    """
    try:
        msg = await app.get_messages(chat_id, message_id)
        if msg is None:
            return False
        # Pyrogram returns a Message-like object with empty=True for
        # deleted/non-existent messages.
        if getattr(msg, "empty", False):
            return False
        # A message with no date is a deleted/empty placeholder.
        if getattr(msg, "date", None) is None:
            return False
        return True
    except Exception as exc:
        # API/network failure — promote to WARNING so transient
        # outages are visible. The safe-default return (True) is
        # preserved: we never treat an API error as proof of deletion.
        log.warning(
            "Message existence check failed for %s/%s: %s: %s (assuming exists)",
            chat_id, message_id, type(exc).__name__, exc,
        )
        return True


async def _get_latest_existing_post_before(
    chat_id: int, current_msg_id: int, *, min_msg_id: int = 1, limit: int = 25,
):
    """Find the latest EXISTING channel post before ``current_msg_id``.

    Walks backwards through the channel history starting from
    ``current_msg_id`` (using Pyrogram's ``message_id`` parameter on
    ``get_chat_history``), skips deleted/empty messages, and returns
    the first surviving post whose ``id < current_msg_id`` and
    ``id >= min_msg_id``.

    ``min_msg_id`` lets the caller constrain the search to the gap
    window (so we don't accidentally adopt an arbitrarily OLD channel
    post as the new gap reference).

    ``limit`` caps the number of messages walked (Telegram API limits
    + bounded work). 25 is a generous cap — under normal operation the
    relevant surviving post is within the most recent few messages.

    Returns the ``Message`` object, or ``None`` if no surviving post
    is found within the requested range, or on any error.
    """
    try:
        # Iterate backwards starting from current_msg_id. The first
        # surviving message with id < current_msg_id and id >= min_msg_id
        # is our candidate.
        async for msg in app.get_chat_history(
            chat_id,
            limit=limit,
            offset=0,
        ):
            mid = getattr(msg, "id", None)
            if mid is None:
                continue
            # Skip the current incoming post itself and any newer messages.
            if int(mid) >= int(current_msg_id):
                continue
            if int(mid) < int(min_msg_id):
                # We've walked past the requested floor — stop.
                break
            # Skip deleted/empty placeholders.
            if getattr(msg, "empty", False) or getattr(msg, "date", None) is None:
                continue
            return msg
        return None
    except Exception as exc:
        log.warning(
            "get_latest_existing_post failed for %s (before msg %s, min %s): %s: %s",
            chat_id, current_msg_id, min_msg_id, type(exc).__name__, exc,
        )
        return None


async def _resolve_stale_gap_reference(
    chat_id: int, current_message: Message, gap: int, current_post_time: int,
) -> None:
    """Detect and fix a stale gap reference caused by manual deletion.

    Called BEFORE ``_gap_allows_post()`` so the atomic gap check uses
    the correct reference.  Steps:

    1. Read the current settings document.
    2. If ``last_gap_post_msg_id`` exists, check if that message still
       exists in Telegram via ``_message_exists()``.
    3. If the message still exists → no correction needed, return.
    4. If the message was manually deleted (or deleted by /setdelay):
       a. KEEP the stored ``last_gap_post_at`` timestamp. The timestamp
          was set atomically by ``_gap_allows_post`` when the original
          post was accepted — it is a verified, persisted fact and
          remains valid even after the message itself is gone. This is
          the critical fix for Issues 1 & 2: previously, clearing the
          reference here caused the next ``_gap_allows_post`` to see
          ``last_gap_post_at`` missing and accept the post
          unconditionally, collapsing the gap window.
       b. Best-effort: look for a SURVIVING post WITHIN the gap window
          (``post_time >= current_post_time - gap`` and
          ``id < current_message.id``). If found, ADOPT it as the new
          reference — but ONLY if its timestamp is more recent than the
          stored one (so we never extend the gap window beyond what the
          stored timestamp + gap already implies; we only refresh the
          message_id pointer so future existence checks work).
       c. If no surviving post within the gap window is found, KEEP the
          stored timestamp (do NOT clear it). The next
          ``_gap_allows_post`` will use the stored timestamp to decide.

    The update uses a precondition (``last_gap_post_msg_id`` must match
    the stored value) to avoid race conditions — if another concurrent
    post already updated the reference, this update is a no-op.  The
    final gap decision is still made atomically by ``_gap_allows_post()``.

    Conservative behaviour per spec Issue 4:
    - "Do not blindly rely on a stale reference": we don't — we verify
      via ``_message_exists`` and refresh the pointer when possible.
    - "Do not restart the full gap timer unnecessarily": we don't —
      the stored timestamp is preserved, so the gap window is NOT reset.
    - "If no relevant surviving post exists, allow the new post": this
      applies when there is NO stored timestamp at all. In that case
      ``_gap_allows_post``'s ``$exists: False`` branch handles it. When
      a stored timestamp DOES exist, it is used as the basis for the
      gap decision (it is verified, persisted metadata, not a stale
      message pointer).

    This function NEVER raises — all exceptions are caught and logged
    at WARNING level.
    """
    try:
        setting = await settings_db.find_one({"chat_id": chat_id})
        if not setting or not setting.get("gap_enabled"):
            return

        stored_msg_id = setting.get("last_gap_post_msg_id")
        if not stored_msg_id:
            # No stored message_id — old document from before this fix,
            # OR the gap was just enabled. Can't check existence. Fall
            # through to the normal gap check (which handles the
            # missing-reference case by accepting the post).
            return

        stored_ts = setting.get("last_gap_post_at")

        # STEP 1: Check if the stored reference message still exists.
        if await _message_exists(chat_id, int(stored_msg_id)):
            # Reference is valid — no correction needed.
            return

        # STEP 2: Reference message is GONE (manual deletion or /setdelay).
        # The stored timestamp is STILL VALID — it was set atomically when
        # the original post was accepted. We MUST NOT clear it; doing so
        # would collapse the gap window (Issue 1 & Issue 2 root cause).
        #
        # Best-effort: try to find a SURVIVING post within the gap window
        # so we can refresh the message_id pointer. If we find one whose
        # timestamp is more recent than the stored timestamp, adopt it.
        # This never EXTENDS the gap window — it only refreshes the pointer.
        cutoff = current_post_time - gap
        latest_existing = await _get_latest_existing_post_before(
            chat_id,
            current_message.id,
            min_msg_id=1,  # don't constrain by msg_id floor; we filter by time below
            limit=25,
        )

        if latest_existing is not None:
            new_timestamp = _post_unix_time(latest_existing)
            new_msg_id = latest_existing.id

            # Only adopt the surviving post if its timestamp is within
            # the gap window AND more recent than the stored timestamp.
            # This ensures we never:
            #   - adopt an arbitrarily OLD post (which would make the
            #     gap check pass incorrectly — Issue 2 root cause)
            #   - extend the gap window beyond stored_ts + gap
            if new_timestamp < cutoff:
                # Surviving post is OLDER than the gap window — not
                # relevant. Keep the stored timestamp (which is more
                # recent and still valid).
                log.info(
                    "stale_gap_ref chat=%s stored_msg=%s stored_ts=%s: "
                    "surviving post %s is older than cutoff %s, keeping stored ts",
                    chat_id, stored_msg_id, stored_ts, new_msg_id, cutoff,
                )
                return

            if stored_ts is not None and new_timestamp <= int(stored_ts):
                # Surviving post is older than (or equal to) the stored
                # reference. Keep the stored reference — it's more recent.
                log.info(
                    "stale_gap_ref chat=%s stored_msg=%s stored_ts=%s: "
                    "surviving post %s ts=%s not more recent, keeping stored ts",
                    chat_id, stored_msg_id, stored_ts, new_msg_id, new_timestamp,
                )
                return

            # Adopt the surviving post as the new reference.
            # Precondition: only if last_gap_post_msg_id hasn't changed
            # (avoid race with another concurrent post).
            result = await settings_db.update_one(
                {
                    "chat_id": chat_id,
                    "last_gap_post_msg_id": stored_msg_id,
                },
                {
                    "$set": {
                        "last_gap_post_at": new_timestamp,
                        "last_gap_post_msg_id": new_msg_id,
                    },
                },
            )
            log.info(
                "stale_gap_ref chat=%s refreshed: msg %s (ts %s) → msg %s (ts %s), matched=%s",
                chat_id, stored_msg_id, stored_ts, new_msg_id, new_timestamp,
                getattr(result, "matched_count", "?"),
            )
        else:
            # No surviving post within the gap window. KEEP the stored
            # timestamp — it's all we have, and it's verified. The next
            # _gap_allows_post will use it to decide. DO NOT CLEAR.
            log.info(
                "stale_gap_ref chat=%s stored_msg=%s stored_ts=%s: "
                "no surviving post in gap window, keeping stored ts (NOT clearing)",
                chat_id, stored_msg_id, stored_ts,
            )
    except Exception as exc:
        # NEVER raise — the gap check still runs and will use whatever
        # state is in the database. Log at WARNING so the operator can
        # see the failure.
        log.warning(
            "Stale gap reference resolution failed for %s: %s: %s",
            chat_id, type(exc).__name__, exc,
        )


async def _queue_delete(chat_id: int, message_id: int, delay: int,
                        original_post_time: int):
    """Insert a deletion job WITHOUT overwriting an existing one.

    The per-(chat_id, message_id) unique index already prevents a
    duplicate document. On top of that, every "mutable-after-insert"
    field is written via $setOnInsert so that even if Mongo chose to
    match the existing document for the upsert, no field on it would
    change. The only field that the worker is allowed to update later
    is ``attempts`` / ``delete_at`` (for retry backoff).

    This is the protection the spec requires: reprocessing the same
    message (for example because it was edited) MUST NOT move
    ``delete_at`` forward.

    TTL field type:
        ``expires_at`` is stored as a BSON UTC Date (not an int). MongoDB
        TTL indexes ONLY fire on BSON Date values — pre-Part-2 stored an
        int here, which made the TTL index silently ineffective. The
        in-place migration in :func:`ensure_channel_indexes` converts
        any existing int-typed values. ``delete_at`` is kept as int
        because it is not TTL-managed and the worker's query/sort/retry
        arithmetic is cleaner with ints.
    """
    delete_at = int(original_post_time) + int(delay)
    expires_at_unix = delete_at + (7 * 24 * 60 * 60)
    expires_at_date = _ttl_date_from_unix(expires_at_unix)

    await jobs_db.update_one(
        {"chat_id": chat_id, "message_id": message_id},
        {
            "$setOnInsert": {
                "chat_id": chat_id,
                "message_id": message_id,
                "delete_at": delete_at,
                "expires_at": expires_at_date,
                "attempts": 0,
                "original_post_time": int(original_post_time),
            }
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

async def _delete_worker():
    await ensure_channel_indexes()

    # Best-effort sweep of any stale/expired documents that the TTL
    # index has not reaped yet (TTL reaper runs every ~60s on Mongo).
    # ``expires_at`` is now stored as a BSON Date — compare against a
    # BSON Date. (Pre-Part-2 int values are converted by
    # ``migrate_jobs_ttl_field`` during ``ensure_channel_indexes`` above,
    # but be defensive and also reap any int-typed values that are
    # older than the current time.)
    now_date = _ttl_date_from_unix(int(time.time()))
    now_int = int(time.time())
    with suppress(Exception):
        await jobs_db.delete_many(
            {
                "$or": [
                    {"expires_at": {"$lte": now_date}},
                    {"expires_at": {"$type": "int", "$lte": now_int}},
                    {"expires_at": {"$type": "long", "$lte": now_int}},
                ]
            }
        )

    while True:
        try:
            now_int = int(time.time())
            now_date = _ttl_date_from_unix(now_int)
            # Select jobs whose delete_at has passed AND whose
            # expires_at is still in the future. The ``expires_at``
            # clause accepts BOTH BSON Date (post-Part-2) and int/long
            # (pre-Part-2 documents not yet migrated, or documents
            # written by a worker restart race). This makes the worker
            # resilient during the in-place migration window.
            cursor = (
                jobs_db.find(
                    {
                        "delete_at": {"$lte": now_int},
                        "$or": [
                            {"expires_at": {"$gt": now_date}},
                            {"expires_at": {"$type": "int", "$gt": now_int}},
                            {"expires_at": {"$type": "long", "$gt": now_int}},
                        ],
                    },
                    {"chat_id": 1, "message_id": 1, "attempts": 1},
                )
                .sort("delete_at", 1)
                .limit(100)
            )
            jobs = await cursor.to_list(length=100)

            for job in jobs:
                # Per-job isolation: a single malformed record MUST NOT
                # stop processing of every other record. Each job is
                # processed inside its own try/except, and any error
                # (including KeyError on missing fields, TypeError on
                # wrong types, or a corrupt _id) is logged and skipped
                # — the loop continues to the next job.
                try:
                    job_id = job["_id"]
                    chat_id = job["chat_id"]
                    message_id = job["message_id"]
                    attempts = int(job.get("attempts", 0))
                except Exception as exc:
                    log.warning(
                        "Malformed autodelete_jobs document skipped (id=%r): %s: %s",
                        job.get("_id", "?") if isinstance(job, dict) else "?",
                        type(exc).__name__, exc,
                    )
                    continue

                # ─── Atomic claim ────────────────────────────────────
                # Move ``delete_at`` forward by a short grace window
                # (60s) using a precondition that ``delete_at`` is still
                # the value we read. This makes the worker safe even if
                # a future change ever introduces multiple concurrent
                # worker tasks: only one ``find_one_and_update`` will
                # match, the other will get None and skip the job.
                # Today there is only one worker, so this is purely
                # defensive — but it costs one round-trip and prevents
                # a whole class of future bugs.
                #
                # The claim window of 60s is long enough to absorb the
                # Telegram delete RPC + a small retry; if the worker
                # crashes mid-job, the next tick picks it up after 60s
                # (well within the 7-day TTL safety net).
                claim_until = int(time.time()) + 60
                claimed = await jobs_db.find_one_and_update(
                    {"_id": job_id, "delete_at": job.get("delete_at")},
                    {"$set": {"delete_at": claim_until}},
                    projection={"chat_id": 1, "message_id": 1},
                    return_document=True,  # ReturnDocument.AFTER
                )
                if claimed is None:
                    # Another worker (or a retry path) already moved
                    # delete_at — skip this round.
                    continue

                try:
                    await app.delete_messages(chat_id, message_id)
                    # Success: remove the temporary job immediately.
                    with suppress(Exception):
                        await jobs_db.delete_one({"_id": job_id})

                except FloodWait as exc:
                    # Telegram asked us to wait. Do NOT count this as an
                    # attempt — FloodWait is a rate-limit, not a
                    # permanent failure. Reschedule delete_at to
                    # now + wait_seconds (capped at 1h for sanity) and
                    # leave attempts unchanged so we don't exhaust the
                    # retry cap while the rate-limit is active.
                    wait = int(getattr(exc, "value", 0)) or 1
                    wait = min(max(wait, 1), 3600)
                    retry_at = int(time.time()) + wait
                    log.warning(
                        "Channel auto-delete FloodWait for %s/%s: wait=%ss, rescheduling to %s",
                        chat_id, message_id, wait, retry_at,
                    )
                    with suppress(Exception):
                        await jobs_db.update_one(
                            {"_id": job_id},
                            {"$set": {"delete_at": retry_at}},
                        )

                except Exception as exc:
                    error_name = type(exc).__name__
                    # Real RPC / DB / programming error. Log at WARNING
                    # so the operator can see it (was previously debug,
                    # which silently hid all per-job failures — Issue 7).
                    log.warning(
                        "Channel auto-delete failed for %s/%s: %s: %s",
                        chat_id, message_id, error_name, exc,
                    )

                    # Stop retrying after a reasonable cap. Permanent
                    # failures are also reaped by the TTL index.
                    if attempts >= 10:
                        log.warning(
                            "Channel auto-delete retry exhausted for %s/%s after %s attempts: %s",
                            chat_id, message_id, attempts, error_name,
                        )
                        with suppress(Exception):
                            await jobs_db.delete_one({"_id": job_id})
                    else:
                        retry_at = int(time.time()) + min(60, 2 ** attempts)
                        with suppress(Exception):
                            await jobs_db.update_one(
                                {"_id": job_id},
                                {
                                    "$set": {"delete_at": retry_at},
                                    "$inc": {"attempts": 1},
                                },
                            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Channel auto-delete worker error: %s: %s",
                type(exc).__name__, exc,
            )

        await asyncio.sleep(1)


_worker_task: "asyncio.Task | None" = None


async def _start_worker():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        await ensure_channel_indexes()
        _worker_task = asyncio.create_task(_delete_worker())


async def _bootstrap():
    await _start_worker()


# Plugins are imported from an already-running event loop by __main__.py.
# Start the persistent worker without requiring any edit to another file.
try:
    asyncio.get_running_loop().create_task(_bootstrap())
except RuntimeError:
    pass


# ---------------------------------------------------------------------------
# Invalid-time response (shared by /setdelay and /setgap)
# ---------------------------------------------------------------------------

INVALID_TEXT_DELAY = (
    "Invalid time selected!\n"
    "Should be under 24 hours or off!\n\n"
    "Use like this:\n"
    "/setdelay 1m or 5s or 22h or off (To turn off.)\n\n"
    "Maximum is 24 hours and minimum is 3 seconds."
)

INVALID_TEXT_GAP = (
    "Invalid time selected!\n"
    "Should be under 24 hours or off!\n\n"
    "Use like this:\n"
    "/setgap 1m or 5s or 22h or off (To turn off.)\n\n"
    "Maximum is 24 hours and minimum is 3 seconds."
)


# ---------------------------------------------------------------------------
# /setdelay — CHANNEL ONLY
# ---------------------------------------------------------------------------

@app.on_message(
    filters.channel & filters.regex(r"(?i)^/setdelay(?:@\w+)?(?:\s+(.+?))?\s*$"),
    group=-20,
)
async def setdelay_handler(_, message: Message):
    await _start_worker()

    # Explicit guard so behaviour is independent of any future Pyrogram
    # filter-quirk change.
    if message.chat.type != ChatType.CHANNEL:
        return

    # Channel posts have no normal from_user. Telegram only allows channel
    # members with posting rights / admins to publish, so this command is
    # usable by channel admins who can post — NOT restricted to OWNER/SUDO.
    if not await _bot_can_delete(message.chat.id):
        with suppress(Exception):
            await message.reply_text(
                "I need <b>Delete Messages</b> admin permission to work here."
            )
        return

    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    value = parts[1].strip() if len(parts) == 2 else ""

    # `/setdelay on` is intentionally invalid — delete command + show error.
    if value.lower() == "on":
        with suppress(Exception):
            await message.delete()
        with suppress(Exception):
            await message.reply_text(INVALID_TEXT_DELAY)
        return

    # ─── CHANNEL-SIDE OFF IS FORBIDDEN ─────────────────────────────────
    # /setdelay off in a channel is SILENTLY IGNORED:
    #   1. Delete the user's command immediately.
    #   2. Send NO bot response.
    #   3. Do NOT modify MongoDB.
    #   4. Do NOT create an activity log.
    # The ONLY way to disable SETDELAY is via the bot's private chat:
    #   /setdelay → Channel ID → 🔴 DISABLE
    if value.lower() == "off":
        with suppress(Exception):
            await message.delete()
        return

    seconds = _parse_duration(value)
    if seconds is None:
        with suppress(Exception):
            await message.delete()
        with suppress(Exception):
            await message.reply_text(INVALID_TEXT_DELAY)
        return

    # Fetch current settings BEFORE the update to detect state change
    prior = await settings_db.find_one({"chat_id": message.chat.id})
    was_enabled = bool(prior and prior.get("enabled"))
    old_delay = int(prior.get("delay") or 0) if was_enabled else 0

    await settings_db.update_one(
        {"chat_id": message.chat.id},
        {
            "$set": {
                "chat_id": message.chat.id,
                "delay": seconds,
                "enabled": True,
            }
        },
        upsert=True,
    )

    # Delete the user's command immediately (channel-side auto-cleanup).
    with suppress(Exception):
        await message.delete()

    # Send success response — it will be auto-deleted after 120 seconds
    # via the non-blocking _delete_after helper.
    response = None
    with suppress(Exception):
        response = await message.reply_text(f"Successfully updated to {value.lower()}!")
    if response:
        await _delete_after(response, _CHANNEL_RESPONSE_TTL)

    # Activity log — only if the state actually changed (source=CHANNEL)
    with suppress(Exception):
        if not was_enabled:
            # Was disabled, now enabled
            await log_setdelay_change(
                message.chat, message.from_user, "ENABLED",
                delay_value=_format_duration(seconds),
                source="CHANNEL",
            )
        elif old_delay != seconds:
            # Was enabled with a different value, now updated
            await log_setdelay_change(
                message.chat, message.from_user, "UPDATED",
                delay_value=_format_duration(seconds),
                old_value=_format_duration(old_delay),
                source="CHANNEL",
            )
        # If was_enabled and old_delay == seconds → no state change, no log


# ---------------------------------------------------------------------------
# /setgap — CHANNEL ONLY
# ---------------------------------------------------------------------------

@app.on_message(
    filters.channel & filters.regex(r"(?i)^/setgap(?:@\w+)?(?:\s+(.+?))?\s*$"),
    group=-19,
)
async def setgap_handler(_, message: Message):
    await _start_worker()

    if message.chat.type != ChatType.CHANNEL:
        return

    if not await _bot_can_delete(message.chat.id):
        with suppress(Exception):
            await message.reply_text(
                "I need <b>Delete Messages</b> admin permission to work here."
            )
        return

    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    value = parts[1].strip() if len(parts) == 2 else ""

    if value.lower() == "on":
        with suppress(Exception):
            await message.delete()
        with suppress(Exception):
            await message.reply_text(INVALID_TEXT_GAP)
        return

    # ─── CHANNEL-SIDE OFF IS FORBIDDEN ─────────────────────────────────
    # /setgap off in a channel is SILENTLY IGNORED:
    #   1. Delete the user's command immediately.
    #   2. Send NO bot response.
    #   3. Do NOT modify MongoDB.
    #   4. Do NOT create an activity log.
    # The ONLY way to disable SETGAP is via the bot's private chat:
    #   /setgap → Channel ID → 🔴 DISABLE
    if value.lower() == "off":
        with suppress(Exception):
            await message.delete()
        return

    seconds = _parse_duration(value)
    if seconds is None:
        with suppress(Exception):
            await message.delete()
        with suppress(Exception):
            await message.reply_text(INVALID_TEXT_GAP)
        return

    # Fetch current settings BEFORE the update to detect state change
    prior = await settings_db.find_one({"chat_id": message.chat.id})
    was_enabled = bool(prior and prior.get("gap_enabled"))
    old_gap = int(prior.get("gap") or 0) if was_enabled else 0

    await settings_db.update_one(
        {"chat_id": message.chat.id},
        {
            "$set": {
                "chat_id": message.chat.id,
                "gap": seconds,
                "gap_enabled": True,
            },
            # Start a fresh gap window when the gap value itself changes —
            # otherwise previously allowed posts would retroactively
            # invalidate new posts because last_gap_post_at would be very
            # recent.
            "$unset": {
                "last_gap_post_at": "",
                "last_gap_post_msg_id": "",
            },
        },
        upsert=True,
    )

    # Delete the user's command immediately (channel-side auto-cleanup).
    with suppress(Exception):
        await message.delete()

    # Send success response — it will be auto-deleted after 120 seconds
    # via the non-blocking _delete_after helper.
    response = None
    with suppress(Exception):
        response = await message.reply_text(
            f"Successfully updated gap to {value.lower()}!"
        )
    if response:
        await _delete_after(response, _CHANNEL_RESPONSE_TTL)

    # Activity log — only if the state actually changed (source=CHANNEL)
    with suppress(Exception):
        if not was_enabled:
            # Was disabled, now enabled
            await log_setgap_change(
                message.chat, message.from_user, "ENABLED",
                gap_value=_format_duration(seconds),
                source="CHANNEL",
            )
        elif old_gap != seconds:
            # Was enabled with a different value, now updated
            await log_setgap_change(
                message.chat, message.from_user, "UPDATED",
                gap_value=_format_duration(seconds),
                old_value=_format_duration(old_gap),
                source="CHANNEL",
            )
        # If was_enabled and old_gap == seconds → no state change, no log


# ---------------------------------------------------------------------------
# Process NEW channel posts — apply gap, then queue delay
# ---------------------------------------------------------------------------

def _is_edited_post(message: Message) -> bool:
    """True if this update is an EDIT of an existing channel post.

    Kurigram populates ``message.edit_date`` ONLY for edited messages —
    new posts have edit_date == None. This is the primary defence against
    edits being treated as new posts. The unique index on
    (chat_id, message_id) plus the $setOnInsert update is the secondary
    defence in case this attribute is ever unset for some reason.
    """
    return getattr(message, "edit_date", None) is not None


@app.on_message(filters.channel & filters.incoming, group=0)
async def autodelete_new_channel_post(_, message: Message):
    try:
        if message.chat.type != ChatType.CHANNEL:
            return

        # Defensive guard: skip the bot's OWN outgoing channel posts
        # (e.g. the "Successfully updated gap to 5m!" auto-reply).
        # ``filters.incoming`` SHOULD already exclude these, but
        # Kurigram's ``outgoing`` flag for channel posts sent by the
        # bot-as-admin is not 100% reliable across versions. Without
        # this guard, the bot's own auto-reply could become the first
        # gap reference and then get auto-deleted by ``_delete_after``
        # 120s later — collapsing the gap window (Issue 1 root cause).
        if getattr(message, "outgoing", False):
            return

        # EDITED-MESSAGE PROTECTION — primary filter.
        # An edit MUST NOT trigger delay scheduling, MUST NOT consume a
        # gap slot, MUST NOT update last_gap_post_at.
        if _is_edited_post(message):
            return

        setting = await settings_db.find_one({"chat_id": message.chat.id})
        if not setting:
            return

        # Even though the command handlers also check, this guard protects
        # the gap/delay side-effects when the bot lacks delete rights.
        if not await _bot_can_delete(message.chat.id):
            return

        is_delay_command = _is_setdelay_command(message)
        is_gap_command = _is_setgap_command(message)

        # Use the ORIGINAL Telegram post timestamp for gap calculations
        # (not time.time()) so the gap is measured from the actual post
        # time, not from when the bot happened to receive the update.
        post_time = _post_unix_time(message)

        # ---- GAP --------------------------------------------------------
        # Only one post is allowed per configured interval. Posts inside
        # the gap are deleted immediately. The gap is NOT extended by
        # those rejected posts (see _gap_allows_post).
        if (
            setting.get("gap_enabled")
            and setting.get("gap")
            and not is_delay_command
            and not is_gap_command
        ):
            gap = int(setting["gap"])

            # ─── STALE-REFERENCE DETECTION ──────────────────────────
            # Before the atomic gap check, verify that the stored
            # last-gap-post reference message still EXISTS in the channel.
            # If it was manually deleted (or deleted by /setdelay),
            # refresh the message_id pointer if a surviving post within
            # the gap window is found; otherwise KEEP the stored timestamp
            # so the gap window is not reset. See _resolve_stale_gap_reference.
            await _resolve_stale_gap_reference(
                message.chat.id, message, gap, post_time,
            )

            if not await _gap_allows_post(
                message.chat.id, post_time, gap, message.id,
            ):
                # Gap violation — delete the post immediately. Handle
                # FloodWait specifically so we don't silently swallow
                # rate-limit errors (Issue 7). Other exceptions (message
                # already gone, chat removed, etc.) are suppressed but
                # logged at INFO so the operator can see them.
                try:
                    await app.delete_messages(message.chat.id, message.id)
                except FloodWait as exc:
                    wait = int(getattr(exc, "value", 0)) or 1
                    log.warning(
                        "gap_reject_delete FloodWait chat=%s msg=%s wait=%ss (post NOT deleted; will retry on next post)",
                        message.chat.id, message.id, wait,
                    )
                except Exception as exc:
                    # Most common: MESSAGE_ID_INVALID / message already
                    # deleted by an admin. Not actionable, but log at
                    # INFO so silent suppression is visible.
                    log.info(
                        "gap_reject_delete failed chat=%s msg=%s: %s: %s",
                        message.chat.id, message.id,
                        type(exc).__name__, exc,
                    )
                # No delay job is queued for a rejected post.
                return

        # ---- DELAY ------------------------------------------------------
        if not setting.get("enabled"):
            return

        delay = int(setting.get("delay") or 0)
        if delay < MIN_SECONDS or delay > MAX_SECONDS:
            return

        if is_delay_command or is_gap_command:
            return

        # Use the ORIGINAL Telegram message date so the delay always
        # counts from the post time, not from when the bot happened to
        # receive the update. Editing the post later does not move
        # delete_at — see _queue_delete.  ``post_time`` was already
        # computed above for the gap check.
        await _queue_delete(message.chat.id, message.id, delay, post_time)

    except Exception as exc:
        # Per-post isolation: a failure here MUST NOT disable processing
        # of subsequent posts. The handler returns normally and Pyrogram
        # continues to dispatch the next update. Promoted from debug to
        # WARNING so the operator can actually see handler-level errors
        # (Issue 7). No secrets are logged.
        log.warning(
            "Channel auto-delete/gap handler failed chat=%s msg=%s: %s: %s",
            getattr(getattr(message, "chat", None), "id", "?"),
            getattr(message, "id", "?"),
            type(exc).__name__, exc,
        )


# ---------------------------------------------------------------------------
# Edited channel posts — explicit safety net
# ---------------------------------------------------------------------------
#
# Kurigram normally routes channel-post edits through ``on_edited_message``
# rather than ``on_message``. We register an explicit no-op handler here
# so that, even if Kurigram's behaviour changes in a future release, an
# edit can NEVER trigger the new-post handler chain above. This is the
# spec's "EDIT != NEW POST" rule implemented as a hard firewall.
@app.on_edited_message(filters.channel & filters.incoming, group=0)
async def _ignore_edited_channel_post(_, message: Message):
    return
