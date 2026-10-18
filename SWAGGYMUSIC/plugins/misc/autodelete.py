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
            "last_gap_post_at": <ts>,    # current gap reference (original
            "last_gap_post_msg_id": <id>,#   Telegram time + message id)
            "gap_history": [             # newest GAP_HISTORY_LIMIT ACCEPTED
                {                        # posts, oldest first
                    "msg_id": <id>,
                    "post_at": <ts>,         # original Telegram post time
                    "media_group_id": <id>,  # album items only (one slot)
                    "bot_delete": "pending" | "confirmed",
                    "bot_deleted_at": <ts>,  # set only by the /setdelay
                },                           # worker (see MANUAL DELETION)
            ],
            "gap_albums": [              # newest GAP_ALBUM_LIMIT album
                {                        # verdicts (see MEDIA ALBUMS)
                    "media_group_id": <id>,
                    "verdict": "accepted" | "rejected",
                    "post_at": <ts>,         # the album's Telegram time
                    "msg_id": <id>,          # the item that decided
                },
            ],
            "anon_rejected_msg_ids": [<id>, ...],  # /setanonymous verdicts;
        }                                          # never gap references
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
            "original_post_time": <int unix ts>,  # immutable after first insert
            "reason": "GAP_REJECT_RETRY",         # only on retry jobs for
                                                   # rejected posts (FloodWait;
                                                   # or ANON_REJECT_RETRY)
            "inflight": True                       # a delete call was sent and
                                                   # its outcome is not known
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
    The gap is per CHANNEL, not per user. A new post is allowed only if
    no ACCEPTED post that still counts was made within the last ``gap``
    seconds, measured with original Telegram timestamps.
    Only accepted posts update the timer. Rejected posts (deleted because
    they violate the gap) and edited posts MUST NOT:
        - update last_gap_post_at
        - extend the gap
        - become the new reference post
    This is implemented atomically using ``find_one_and_update`` with
    a precondition that ``last_gap_post_at`` is missing or older than
    the cutoff. Two simultaneous posters will race on the same Mongo
    document update — only one will receive a non-None return value.
    On top of that, decisions for one channel are serialised in-process
    by a per-channel ``asyncio.Lock``.

MANUAL DELETION:
    Every accepted post is appended (in the same atomic update) to
    ``gap_history``. If the latest accepted post is deleted manually, the
    gap is measured from the most recent EARLIER accepted post that still
    exists, using that post's original Telegram timestamp. Example, gap
    15m: A 10:00 and B 10:15 accepted, B deleted manually -> a post at
    10:17 is allowed because A (10:00) is the reference again.
    Existence is checked with ``get_messages`` (bots cannot read channel
    history — ``get_chat_history`` fails with BOT_METHOD_INVALID). An API
    error is never taken as proof of deletion: the post keeps counting.

/SETDELAY VS MANUAL DELETION:
    A post deleted by the bot's own /setdelay job keeps counting for the
    gap (so /setdelay never shortens it); a post deleted manually does not.
    The two are told apart ONLY by what the worker observed, never by the
    clock: before calling Telegram the worker marks the history entry
    ``bot_delete="pending"``; Telegram's answer (``pts_count``) then makes
    it ``"confirmed"`` (>= 1 message deleted) or clears it (0 deleted: the
    post was already gone -> manual). An unknown outcome (network error,
    5xx, crash) keeps the mark, and the job's ``inflight`` flag lets the
    retry recognise that case. A refused call (RPC error/FloodWait) deleted
    nothing itself: it clears the mark and ``inflight`` only when no earlier
    attempt's outcome is unknown — otherwise both stay, because that
    earlier attempt may have deleted the post (unknown -> FloodWait ->
    "0 deleted" is still the bot's own deletion).
    A missing post without a bot mark is a manual deletion — even when its
    /setdelay deadline has passed.

MEDIA ALBUMS:
    Several photos/videos sent together form ONE post for the gap. Telegram
    delivers every item as a separate update (same ``media_group_id``), in
    any order and possibly concurrently, so the album is judged once and
    every item follows that verdict: the whole album is allowed — one gap
    slot, all items in ``gap_history`` with the album's timestamp, the
    reference set only by the first — or the whole album is deleted. The
    verdict is written to ``gap_albums`` with a compare-and-set ("this
    album has no verdict yet"), so concurrent items, a second process or a
    restart in the middle of the album all reach the same outcome; it is
    kept when /setgap is changed or disabled so an album being delivered
    at that moment is never split. /setdelay still schedules (and the
    worker marks) every item separately; manual-deletion recovery treats
    the album as one post that counts while any of its items counts.

/SETANONYMOUS INTERACTION:
    The anonymous filter (group -30) runs after the /setlink filter
    (group -40) and before this handler. A post it rejects is
    marked in-process and persisted in ``anon_rejected_msg_ids``; this
    handler skips it and the atomic accept refuses such ids, so a rejected
    post can never become the /setgap reference (see
    SWAGGYMUSIC/utils/anonymous_policy.py). Service messages (pins, title
    changes ...) are not posts: they are never judged or recorded by the
    gap.

/SETLINK INTERACTION:
    The link filter (SWAGGYMUSIC/plugins/misc/setlink.py) runs even
    earlier, in group -40. A post it rejects — a prohibited link, or any
    item of a media album it condemned — is marked in-process
    (link_policy.was_rejected); this handler skips it, so a link-deleted
    post can never become the /setgap reference or a /setdelay job. Its
    deletion (or FloodWait retry, reason ``LINK_REJECT_RETRY``) is
    handled by the link filter.

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
from typing import Any, Dict, List, Optional, Tuple

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, MessageServiceType
from pyrogram.errors import (FloodWait, InternalServerError, RPCError,
                             ServiceUnavailable)
from pyrogram.types import Message
from pymongo import ReturnDocument

from SWAGGYMUSIC import app
from SWAGGYMUSIC.core.mongo import mongodb
from SWAGGYMUSIC.logging import LOGGER
from SWAGGYMUSIC.utils import anonymous_policy, link_policy
from SWAGGYMUSIC.utils.activity_logger import (_format_duration,
                                               log_setdelay_change,
                                               log_setgap_change)
from SWAGGYMUSIC.utils.database import (_ttl_date_from_unix,
                                        deletion_job_document,
                                        ensure_channel_indexes, jobs_db,
                                        set_gap_duration, settings_db)

log = LOGGER(__name__)

MIN_SECONDS = 3
MAX_SECONDS = 24 * 60 * 60

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$", re.IGNORECASE)

# Channel-side success responses are auto-deleted after this many seconds.
# The wait is done via a non-blocking asyncio.create_task so the bot stays
# fully responsive during the 2-minute window.
_CHANNEL_RESPONSE_TTL = 120

# ─── Gap engine tuning ──────────────────────────────────────────────────────
# Accepted posts remembered per channel (manual-deletion fallback).
GAP_HISTORY_LIMIT = 50
# Upper bound (seconds) for ONE Telegram existence check. Kurigram's
# get_messages() sleeps through FloodWaits internally, so without this
# bound one slow call could stall every post of the channel.
GAP_VERIFY_TIMEOUT = 10.0
# Max existence checks for one decision. Beyond it the remaining accepted
# post is kept as the reference (conservative).
GAP_MAX_VERIFICATIONS = 10
# Re-evaluations when /setgap is changed while a post is being judged.
_GAP_DECISION_ATTEMPTS = 3
# Media-album verdicts remembered per channel (see MEDIA ALBUMS).
GAP_ALBUM_LIMIT = 100
ALBUM_ACCEPTED = "accepted"
ALBUM_REJECTED = "rejected"

# Outcomes of _decide_gap()
GAP_ACCEPTED = "accepted"
GAP_REJECTED = "rejected"
GAP_DUPLICATE = "duplicate"   # already-accepted post delivered again
GAP_DISABLED = "disabled"     # gap was turned off while deciding
GAP_ANON_REJECTED = "anonymous_rejected"  # rejected by /setanonymous

# Existence states of an accepted post
MSG_EXISTS = "exists"
MSG_DELETED = "deleted"            # confirmed gone, removed manually
MSG_BOT_DELETED = "bot_deleted"    # deleted by the bot's own /setdelay job
MSG_UNKNOWN = "unknown"            # Telegram could not tell us (API error)

# gap_history[].bot_delete — written ONLY by the /setdelay worker
BOT_DELETE_PENDING = "pending"      # delete call about to be / was sent
BOT_DELETE_CONFIRMED = "confirmed"  # Telegram reported the post deleted


async def _delete_command_message(message: Message, *, reason: str = "COMMAND_CLEANUP") -> None:
    """Delete a command message from the channel and log the deletion reason.

    Used by the /setgap and /setdelay channel-side handlers to clean up
    the user's command message after processing. The structured log line
    distinguishes this from GAP_REJECT and SETDELAY_EXPIRED deletions.

    Never raises — deletion failures (e.g. message already gone) are
    suppressed.
    """
    with suppress(Exception):
        await message.delete()
    log.info(
        "deletion reason=%s chat=%s msg=%s",
        reason,
        getattr(getattr(message, "chat", None), "id", "?"),
        getattr(message, "id", "?"),
    )


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
            # Structured deletion-reason log — BOT_REPLY_CLEANUP.
            # This is the 120-second auto-cleanup of the bot's own
            # success reply (e.g. "Successfully updated gap to 5m!").
            # It is NOT a gap-reject or delay-expiry deletion.
            log.info(
                "deletion reason=BOT_REPLY_CLEANUP chat=%s msg=%s",
                getattr(getattr(message, "chat", None), "id", "?"),
                getattr(message, "id", "?"),
            )
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


def _is_setlink_command(message: Message) -> bool:
    text = message.text or message.caption or ""
    return bool(re.match(r"(?i)^/setlink(?:@\w+)?(?:\s+.+?)?\s*$", text))


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


def _now() -> int:
    """Wall-clock unix time (patchable in tests)."""
    return int(time.time())


def _as_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` for missing/malformed values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enabled_gap(setting: Optional[dict]) -> Optional[int]:
    """The configured gap in seconds, or ``None`` when /setgap is off."""
    if not setting or not setting.get("gap_enabled"):
        return None
    gap = _as_int(setting.get("gap"))
    return gap if gap and gap > 0 else None


def _configured_delay(setting: Optional[dict]) -> Optional[int]:
    """The configured /setdelay delay in seconds, or ``None`` when off/invalid."""
    if not setting or not setting.get("enabled"):
        return None
    delay = _as_int(setting.get("delay") or 0)
    if delay is None or delay < MIN_SECONDS or delay > MAX_SECONDS:
        return None
    return delay


def _media_group_id(message: Any) -> Optional[int]:
    """The media album a post belongs to (Telegram ``grouped_id``), or None.

    Only a real integer id counts (Kurigram exposes ``media_group_id`` as an
    int; a numeric string from another Pyrogram fork is accepted too).
    """
    value = getattr(message, "media_group_id", None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _album_verdict(setting: Optional[dict], group_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """The stored /setgap verdict for an album (``gap_albums`` entry), if any."""
    if group_id is None or not setting:
        return None
    for raw in setting.get("gap_albums") or []:
        if (
            isinstance(raw, dict)
            and _as_int(raw.get("media_group_id")) == group_id
            and raw.get("verdict") in (ALBUM_ACCEPTED, ALBUM_REJECTED)
        ):
            return raw
    return None


async def _gap_allows_post(
    chat_id: int, post_time: int, gap: int, msg_id: int,
    media_group_id: Optional[int] = None,
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

    The SAME atomic update appends the accepted post to ``gap_history``
    (capped at ``GAP_HISTORY_LIMIT``) so that, if this post is later
    deleted manually, the previous accepted post can become the
    reference again. Rejected posts never reach this update, and the
    precondition refuses any message the /setanonymous filter rejected
    (``anon_rejected_msg_ids``).

    For the first item of a media album (``media_group_id``) the SAME
    update also stores the album's verdict (``gap_albums``), and it only
    matches while the album has no verdict yet: whoever decides the album
    first — this process or another one — decides it for every item.
    """
    cutoff = post_time - gap
    entry: Dict[str, Any] = {"msg_id": int(msg_id), "post_at": int(post_time)}
    query: Dict[str, Any] = {
        "chat_id": chat_id,
        "gap_enabled": True,
        "gap": gap,
        "anon_rejected_msg_ids": {"$ne": int(msg_id)},
        "$or": [
            {"last_gap_post_at": {"$exists": False}},
            {"last_gap_post_at": {"$lte": cutoff}},
        ],
    }
    push: Dict[str, Any] = {
        "gap_history": {"$each": [entry], "$slice": -GAP_HISTORY_LIMIT},
    }
    if media_group_id is not None:
        group_id = int(media_group_id)
        entry["media_group_id"] = group_id
        query["gap_albums.media_group_id"] = {"$ne": group_id}
        push["gap_albums"] = {
            "$each": [{
                "media_group_id": group_id, "verdict": ALBUM_ACCEPTED,
                "post_at": int(post_time), "msg_id": int(msg_id),
            }],
            "$slice": -GAP_ALBUM_LIMIT,
        }
    updated = await settings_db.find_one_and_update(
        query,
        {
            "$set": {
                "last_gap_post_at": post_time,
                "last_gap_post_msg_id": msg_id,
            },
            "$push": push,
        },
        return_document=True,  # ReturnDocument.AFTER
    )
    accepted = updated is not None
    # Decision log — INFO level so the operator can trace gap decisions
    # without enabling DEBUG. No secrets: only chat_id / msg_id / timestamps.
    log.info(
        "gap_decision chat=%s msg=%s album=%s post_time=%s gap=%s cutoff=%s accepted=%s",
        chat_id, msg_id, media_group_id, post_time, gap, cutoff, accepted,
    )
    return accepted


async def _record_album_rejection(
    chat_id: int, group_id: int, *, post_time: int, msg_id: int,
    gap: Optional[int],
) -> bool:
    """Store "rejected" as the album's verdict — only if it has none yet.

    With ``gap`` set the write also requires /setgap to still be that
    duration (the rejection was judged under it). Returns True if this call
    stored the verdict; False means another decision (or a /setgap change)
    came first, and the caller re-reads and follows what is stored.
    """
    query: Dict[str, Any] = {
        "chat_id": chat_id,
        "gap_albums.media_group_id": {"$ne": int(group_id)},
    }
    if gap is not None:
        query.update({"gap_enabled": True, "gap": gap})
    result = await settings_db.update_one(query, {"$push": {"gap_albums": {
        "$each": [{
            "media_group_id": int(group_id), "verdict": ALBUM_REJECTED,
            "post_at": int(post_time), "msg_id": int(msg_id),
        }],
        "$slice": -GAP_ALBUM_LIMIT,
    }}})
    stored = bool(getattr(result, "modified_count", 0))
    log.info(
        "gap_album_verdict chat=%s album=%s msg=%s verdict=rejected stored=%s",
        chat_id, group_id, msg_id, stored,
    )
    return stored


async def _join_accepted_album(
    chat_id: int, msg_id: int, group_id: int, verdict: Dict[str, Any],
) -> bool:
    """Record a further item of an ACCEPTED album in the accepted-post
    history, inside the album's single gap slot.

    The item gets the album's timestamp and never moves the gap reference,
    so the album consumes exactly one slot. Recorded only while the album
    is still part of the current gap window (some of its items are in the
    history): after a /setgap reset or disable the item is still kept (the
    album was allowed) but does not occupy the new window. Each recorded
    item can carry its own /setdelay mark. Returns True if recorded.
    """
    post_at = _as_int(verdict.get("post_at"))
    if post_at is None:
        return False
    entry = {"msg_id": int(msg_id), "post_at": post_at, "media_group_id": int(group_id)}
    try:
        result = await settings_db.update_one(
            {
                "chat_id": chat_id,
                "gap_history.media_group_id": int(group_id),
                "gap_history.msg_id": {"$ne": int(msg_id)},
                "anon_rejected_msg_ids": {"$ne": int(msg_id)},
            },
            {"$push": {"gap_history": {"$each": [entry], "$slice": -GAP_HISTORY_LIMIT}}},
        )
    except Exception as exc:
        log.warning(
            "Could not record album item %s/%s (album %s): %s: %s",
            chat_id, msg_id, group_id, type(exc).__name__, exc,
        )
        return False
    return bool(getattr(result, "modified_count", 0))


async def _follow_album_verdict(
    chat_id: int, msg_id: int, group_id: int, verdict: Dict[str, Any],
) -> str:
    """Apply the album's stored verdict to one of its items."""
    if verdict.get("verdict") == ALBUM_REJECTED:
        log.info(
            "gap_album_member chat=%s msg=%s album=%s verdict=rejected",
            chat_id, msg_id, group_id,
        )
        return GAP_REJECTED
    recorded = await _join_accepted_album(chat_id, msg_id, group_id, verdict)
    log.info(
        "gap_album_member chat=%s msg=%s album=%s verdict=accepted recorded=%s",
        chat_id, msg_id, group_id, recorded,
    )
    return GAP_ACCEPTED


# ---------------------------------------------------------------------------
# Manual-deletion safety — accepted-post history
# ---------------------------------------------------------------------------
#
# Every accepted post is stored in ``gap_history`` (msg_id + ORIGINAL
# Telegram timestamp) by _gap_allows_post, in the same atomic update that
# makes it the reference (``last_gap_post_at`` / ``last_gap_post_msg_id``).
#
# Before judging a NEW post, ``_resolve_stale_gap_reference`` makes sure
# the reference is the most recent accepted post that still counts. If
# the latest accepted post was MANUALLY deleted, it is dropped and the
# previous accepted post becomes the reference again — measured with its
# own original timestamp. Only accepted posts are ever candidates, so a
# rejected post (even one whose deletion failed) or a bot/command message
# can never become the reference.
#
# Why not read the channel history? ``get_chat_history`` (messages.getHistory)
# cannot be used by bots — Telegram answers BOT_METHOD_INVALID. The previous
# implementation relied on it, so in production a manually deleted reference
# was never repaired and kept blocking new posts for the whole gap.
# ``get_messages`` (channels.getMessages) IS available to bots and is used
# for the existence checks.
#
# Existence checks happen only when the newest accepted post would block
# the incoming post; posts outside the gap window cost no API call.
#
# A media album is ONE accepted post: all its items share one history
# "slot" (same ``media_group_id`` and timestamp). The slot counts while any
# item counts, and is dropped only when Telegram reports every item gone
# and none carries a /setdelay mark. One ``get_messages`` call checks all
# items of an album.


async def _telegram_message_state(chat_id: int, message_id: int) -> str:
    """Ask Telegram whether a channel message still exists.

    Returns ``MSG_EXISTS``, ``MSG_DELETED`` or ``MSG_UNKNOWN``. Only an
    explicit empty/missing message counts as deleted. Any API/network
    error, FloodWait or timeout returns ``MSG_UNKNOWN`` — an error is
    never taken as proof that the message was deleted manually.
    """
    try:
        msg = await asyncio.wait_for(
            app.get_messages(chat_id, message_id, replies=0),
            timeout=GAP_VERIFY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "Message existence check for %s/%s timed out after %ss (assuming exists)",
            chat_id, message_id, GAP_VERIFY_TIMEOUT,
        )
        return MSG_UNKNOWN
    except FloodWait as exc:
        log.warning(
            "Message existence check for %s/%s hit FloodWait %ss (assuming exists)",
            chat_id, message_id, getattr(exc, "value", "?"),
        )
        return MSG_UNKNOWN
    except Exception as exc:
        # API/network failure — WARNING so transient outages are visible.
        log.warning(
            "Message existence check failed for %s/%s: %s: %s (assuming exists)",
            chat_id, message_id, type(exc).__name__, exc,
        )
        return MSG_UNKNOWN

    if isinstance(msg, (list, tuple)):
        msg = msg[0] if msg else None
    if msg is None:
        return MSG_DELETED
    # Pyrogram returns Message(empty=True, date=None) for a deleted id.
    if getattr(msg, "empty", False):
        return MSG_DELETED
    if getattr(msg, "date", None) is None:
        return MSG_DELETED
    return MSG_EXISTS


async def _message_exists(chat_id: int, message_id: int) -> bool:
    """Check whether a specific message still exists in the channel.

    Returns ``False`` only when Telegram confirms the message is gone. If
    the API call fails for any reason (network error, timeout, etc.),
    returns ``True`` — the SAFE DEFAULT is to assume the message still
    exists: "Do NOT treat every unexpected API/network error as proof
    that the message was manually deleted."
    """
    return await _telegram_message_state(chat_id, message_id) != MSG_DELETED


async def _telegram_messages_state(chat_id: int, message_ids: List[int]) -> Dict[int, str]:
    """``_telegram_message_state`` for several messages in ONE API call.

    Telegram answers ``channels.getMessages`` with an empty message for each
    deleted id. An error or a missing answer for an id gives ``MSG_UNKNOWN``
    (never proof of deletion).
    """
    ids = [int(m) for m in message_ids]
    states = {m: MSG_UNKNOWN for m in ids}
    try:
        msgs = await asyncio.wait_for(
            app.get_messages(chat_id, ids, replies=0),
            timeout=GAP_VERIFY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        log.warning(
            "Existence check for %s/%s timed out after %ss (assuming they exist)",
            chat_id, ids, GAP_VERIFY_TIMEOUT,
        )
        return states
    except Exception as exc:  # FloodWait, RPC or network error
        log.warning(
            "Existence check failed for %s/%s: %s: %s (assuming they exist)",
            chat_id, ids, type(exc).__name__, exc,
        )
        return states
    if not isinstance(msgs, (list, tuple)):
        msgs = [msgs]
    for msg in msgs:
        msg_id = getattr(msg, "id", None)
        if isinstance(msg_id, bool) or not isinstance(msg_id, int) or msg_id not in states:
            continue
        if getattr(msg, "empty", False) or getattr(msg, "date", None) is None:
            states[msg_id] = MSG_DELETED
        else:
            states[msg_id] = MSG_EXISTS
    return states


def _history_entries(setting: dict) -> List[Dict[str, Any]]:
    """Accepted posts known for this channel, newest first.

    Normalises ``gap_history`` and folds in a legacy reference: documents
    written before the history existed only carry ``last_gap_post_at`` /
    ``last_gap_post_msg_id``. Each entry keeps its ORIGINAL Telegram
    timestamp (``post_at``) and the worker's ``bot_delete`` mark, if any.
    """
    entries: Dict[int, Dict[str, Any]] = {}
    for raw in setting.get("gap_history") or []:
        if not isinstance(raw, dict):
            continue
        msg_id = _as_int(raw.get("msg_id"))
        post_at = _as_int(raw.get("post_at"))
        if msg_id is None or post_at is None:
            continue
        entry: Dict[str, Any] = {"msg_id": msg_id, "post_at": post_at}
        if raw.get("bot_delete") in (BOT_DELETE_PENDING, BOT_DELETE_CONFIRMED):
            entry["bot_delete"] = raw["bot_delete"]
        group_id = _as_int(raw.get("media_group_id"))
        if group_id is not None:
            entry["media_group_id"] = group_id
        entries[msg_id] = entry

    ref_id = _as_int(setting.get("last_gap_post_msg_id"))
    ref_at = _as_int(setting.get("last_gap_post_at"))
    if ref_id is not None and ref_at is not None and ref_id not in entries:
        entries[ref_id] = {"msg_id": ref_id, "post_at": ref_at}

    return sorted(
        entries.values(), key=lambda e: (e["post_at"], e["msg_id"]), reverse=True,
    )


def _is_known_accepted(setting: dict, msg_id: int) -> bool:
    """True if ``msg_id`` was already accepted (edit/duplicate delivery)."""
    if _as_int(setting.get("last_gap_post_msg_id")) == msg_id:
        return True
    return any(
        isinstance(raw, dict) and _as_int(raw.get("msg_id")) == msg_id
        for raw in setting.get("gap_history") or []
    )


async def _accepted_post_state(chat_id: int, entry: Dict[str, Any]) -> str:
    """How an accepted post counts for the gap right now.

    ``MSG_EXISTS`` / ``MSG_UNKNOWN`` / ``MSG_BOT_DELETED`` -> it counts.
    ``MSG_DELETED`` -> it was removed manually and no longer counts.

    ``MSG_BOT_DELETED`` comes ONLY from the /setdelay worker's own record
    (``bot_delete`` = "confirmed", or "pending" while its delete call is in
    flight or ended with an unknown outcome) — never from the clock. Such a
    post counts whether or not it still exists, so Telegram is not asked.
    Every other post is checked with Telegram; a post Telegram reports as
    gone without a bot record was deleted manually, even if its /setdelay
    deadline has passed. API errors give ``MSG_UNKNOWN`` (still counts).
    """
    if entry.get("bot_delete") in (BOT_DELETE_PENDING, BOT_DELETE_CONFIRMED):
        return MSG_BOT_DELETED
    return await _telegram_message_state(chat_id, entry["msg_id"])


def _gap_slots(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group accepted posts into gap slots, newest first: one slot per
    single post and ONE slot per media album (its items share
    ``media_group_id``). ``ids`` are the slot's message ids, ascending."""
    slots: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for entry in entries:
        group_id = entry.get("media_group_id")
        key = ("album", group_id) if group_id is not None else ("post", entry["msg_id"])
        slot = slots.setdefault(key, {
            "media_group_id": group_id, "entries": [], "post_at": entry["post_at"],
        })
        slot["entries"].append(entry)
        slot["post_at"] = max(slot["post_at"], entry["post_at"])
    for slot in slots.values():
        slot["ids"] = sorted(e["msg_id"] for e in slot["entries"])
    return sorted(
        slots.values(), key=lambda s: (s["post_at"], s["ids"][-1]), reverse=True,
    )


async def _slot_state(chat_id: int, slot: Dict[str, Any]) -> str:
    """How an accepted post — or a whole album — counts for the gap now.

    A single post: ``_accepted_post_state``. An album counts while ANY item
    counts: an item with a /setdelay worker mark makes the album
    ``MSG_BOT_DELETED`` (no Telegram call); otherwise all items are checked
    in ONE call — any item still there -> ``MSG_EXISTS``, every item gone ->
    ``MSG_DELETED`` (removed manually), anything unverifiable ->
    ``MSG_UNKNOWN`` (still counts).
    """
    entries = slot["entries"]
    if len(entries) == 1:
        return await _accepted_post_state(chat_id, entries[0])
    if any(e.get("bot_delete") in (BOT_DELETE_PENDING, BOT_DELETE_CONFIRMED) for e in entries):
        return MSG_BOT_DELETED
    states = await _telegram_messages_state(chat_id, slot["ids"])
    if MSG_EXISTS in states.values():
        return MSG_EXISTS
    if all(state == MSG_DELETED for state in states.values()):
        return MSG_DELETED
    return MSG_UNKNOWN


async def _resolve_stale_gap_reference(
    chat_id: int, current_message: Message, gap: int, current_post_time: int,
) -> None:
    """Point the gap reference at the newest accepted post that still counts.

    Called right before the atomic gap check (_gap_allows_post) for a NEW
    post:

    1. Fast path — if the newest accepted post is at least ``gap`` older
       than the incoming post it cannot block it: nothing is verified and
       no Telegram call is made.
    2. Otherwise accepted posts are examined newest-first — a media album
       as ONE post (_gap_slots). Each one that would block the incoming
       post is classified (_slot_state):
         * still exists / deleted by the bot's own /setdelay job (worker
           record) / unverifiable (API error) -> it counts: it stays the
           reference and the walk stops (an album: if ANY item counts);
         * deleted manually -> it is dropped from the history and the walk
           continues with the next EARLIER accepted post (an album: only
           when ALL its items are gone).
       The first accepted post old enough not to block ends the walk and
       becomes the reference with its ORIGINAL Telegram timestamp. (Whether
       it still exists cannot change the decision, so it is not queried.)
       If no accepted post is left, the reference is cleared.
    3. The result is written with a compare-and-set on the reference that
       was read, so a concurrent accept or a /setgap reset is never
       overwritten.

    Rejected posts are never in the history, so they can never become the
    reference. Legacy documents holding only ``last_gap_post_at`` (no
    message id) are left alone. This function never raises; the atomic
    gap check still runs afterwards.
    """
    try:
        setting = await settings_db.find_one({"chat_id": chat_id})
        if _enabled_gap(setting) is None:
            return

        slots = _gap_slots(_history_entries(setting))
        if not slots:
            return

        current_id = _as_int(getattr(current_message, "id", None))
        current_group = _media_group_id(current_message)

        def blocks(slot: Dict[str, Any]) -> bool:
            return current_post_time - slot["post_at"] < gap

        def is_current(slot: Dict[str, Any]) -> bool:
            # never measure a post (or an album) against itself
            return (current_id is not None and current_id in slot["ids"]) or (
                current_group is not None and slot["media_group_id"] == current_group
            )

        if not blocks(slots[0]):
            return  # fast path: the newest accepted post is outside the window

        dropped: List[int] = []
        new_ref: Optional[Dict[str, Any]] = None
        checks = 0
        for slot in slots:
            if is_current(slot):
                continue
            if not blocks(slot):
                new_ref = slot
                break
            if checks >= GAP_MAX_VERIFICATIONS:
                new_ref = slot  # stop calling Telegram; keep it (conservative)
                break
            checks += 1
            state = await _slot_state(chat_id, slot)
            if state == MSG_DELETED:
                dropped.extend(slot["ids"])
                continue
            new_ref = slot  # exists / bot-deleted by /setdelay / unknown
            break

        stored_id = setting.get("last_gap_post_msg_id")
        stored_at = setting.get("last_gap_post_at")
        if not dropped and (
            new_ref is None or _as_int(stored_id) in new_ref["ids"]
        ):
            return  # the stored reference is still the right one
        # $pull only the manually deleted entries (never rewrite the whole
        # array: the /setdelay worker may be marking another entry).
        update: Dict[str, Any] = {}
        if dropped:
            update["$pull"] = {"gap_history": {"msg_id": {"$in": dropped}}}
        new_ref_id: Optional[int] = None
        if new_ref is not None:
            new_ref_id = (
                _as_int(stored_id) if _as_int(stored_id) in new_ref["ids"]
                else new_ref["ids"][0]
            )
            update["$set"] = {
                "last_gap_post_at": new_ref["post_at"],
                "last_gap_post_msg_id": new_ref_id,
            }
        else:
            update["$unset"] = {"last_gap_post_at": "", "last_gap_post_msg_id": ""}

        result = await settings_db.update_one(
            {
                "chat_id": chat_id,
                "gap_enabled": True,
                # compare-and-set: only if nobody changed the reference
                "last_gap_post_msg_id": stored_id,
                "last_gap_post_at": stored_at,
            },
            update,
        )
        log.info(
            "gap_reference_repaired chat=%s incoming=%s manually_deleted=%s "
            "new_ref=%s new_ref_ts=%s matched=%s",
            chat_id, current_id, dropped,
            new_ref_id,
            new_ref["post_at"] if new_ref else None,
            getattr(result, "matched_count", "?"),
        )
    except Exception as exc:
        log.warning(
            "Stale gap reference resolution failed for %s: %s: %s",
            chat_id, type(exc).__name__, exc,
        )


# Per-channel lock: (event loop, lock). One decision at a time per channel
# inside this process; MongoDB compare-and-set covers anything beyond it.
_GAP_LOCKS: Dict[int, Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _gap_lock(chat_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    holder = _GAP_LOCKS.get(chat_id)
    if holder is None or holder[0] is not loop:
        holder = (loop, asyncio.Lock())
        _GAP_LOCKS[chat_id] = holder
    return holder[1]


async def _decide_gap(chat_id: int, message: Message, post_time: int) -> str:
    """Judge a NEW channel post against /setgap.

    Returns ``GAP_ACCEPTED``, ``GAP_REJECTED``, ``GAP_DUPLICATE`` (an
    already-accepted post delivered again, e.g. an edit without
    ``edit_date`` — never re-judged, never deleted), ``GAP_DISABLED``
    (gap turned off meanwhile) or ``GAP_ANON_REJECTED`` (the /setanonymous
    filter rejected it: never judged, never recorded).

    The read -> repair -> atomic accept sequence runs under the channel's
    lock so concurrent posts are judged one after another. If the atomic
    check fails because /setgap was changed concurrently (not because of
    the gap itself), the post is judged again with the new settings.

    MEDIA ALBUMS — every item of an album (same ``media_group_id``) arrives
    as its own update, in any order, possibly concurrently. The album is
    judged ONCE, as one post, and the verdict is stored in ``gap_albums``
    with a compare-and-set ("only if this album has no verdict yet"), both
    for an acceptance (inside the atomic accept) and for a rejection. Every
    other item — in this process, another process or after a restart —
    finds the stored verdict and follows it: all items are kept (one gap
    slot, see _join_accepted_album) or all are deleted. A verdict also
    survives a /setgap change or disable, so an album that is being
    delivered at that moment is never split.
    """
    msg_id = int(message.id)
    group_id = _media_group_id(message)
    async with _gap_lock(chat_id):
        for _attempt in range(_GAP_DECISION_ATTEMPTS):
            setting = await settings_db.find_one({"chat_id": chat_id})
            gap = _enabled_gap(setting)
            verdict = _album_verdict(setting, group_id)
            if gap is None and verdict is None:
                return GAP_DISABLED
            if anonymous_policy.is_rejected_in(setting, msg_id):
                return GAP_ANON_REJECTED
            if _is_known_accepted(setting, msg_id):
                log.info(
                    "gap_duplicate chat=%s msg=%s ignored (already accepted)",
                    chat_id, msg_id,
                )
                return GAP_DUPLICATE
            if verdict is not None:
                # Another item of this album was judged already.
                return await _follow_album_verdict(chat_id, msg_id, group_id, verdict)

            await _resolve_stale_gap_reference(chat_id, message, gap, post_time)
            if await _gap_allows_post(chat_id, post_time, gap, msg_id, group_id):
                return GAP_ACCEPTED

            latest = await settings_db.find_one({"chat_id": chat_id})
            if anonymous_policy.is_rejected_in(latest, msg_id):
                return GAP_ANON_REJECTED
            if _album_verdict(latest, group_id) is not None:
                continue  # the album was decided meanwhile: follow that verdict
            if _enabled_gap(latest) == gap:
                if group_id is None:
                    return GAP_REJECTED
                if await _record_album_rejection(
                    chat_id, group_id, post_time=post_time, msg_id=msg_id, gap=gap,
                ):
                    return GAP_REJECTED
                continue  # decided or /setgap changed meanwhile: re-evaluate
            log.info(
                "gap settings changed while judging chat=%s msg=%s; re-evaluating",
                chat_id, msg_id,
            )
        if group_id is None:
            return GAP_REJECTED
        # /setgap kept changing: reject — through the album's verdict, so
        # every item of the album ends up with the same outcome.
        await _record_album_rejection(
            chat_id, group_id, post_time=post_time, msg_id=msg_id, gap=None,
        )
        verdict = _album_verdict(await settings_db.find_one({"chat_id": chat_id}), group_id)
        if verdict is not None:
            return await _follow_album_verdict(chat_id, msg_id, group_id, verdict)
        return GAP_REJECTED


async def _delete_gap_rejected_post(
    chat_id: int, message_id: int, post_time: int, gap: Optional[int],
    media_group_id: Optional[int] = None,
) -> None:
    """Delete a post rejected by the gap. Never raises.

    FloodWait: the post cannot be deleted right now, so a persistent
    deletion job (reason ``GAP_REJECT_RETRY``) is queued for when the wait
    is over — the existing worker deletes it later, even across restarts.
    The rejected post is NOT recorded as accepted, so it can never become
    the gap reference. Other errors (already deleted, no rights) are logged.
    """
    # Structured deletion-reason log — GAP_REJECT. This identifies WHICH
    # component deleted the message and WHY, so operators can distinguish
    # gap-reject deletions from delay-expiry deletions in the logs.
    log.info(
        "deletion reason=GAP_REJECT chat=%s msg=%s album=%s post_time=%s gap=%s",
        chat_id, message_id, media_group_id, post_time, gap,
    )
    try:
        await app.delete_messages(chat_id, message_id)
    except FloodWait as exc:
        wait = min(max(_as_int(getattr(exc, "value", 0)) or 1, 1), 3600)
        retry_at = _now() + wait
        try:
            await _queue_delete_at(
                chat_id, message_id,
                delete_at=retry_at,
                original_post_time=post_time,
                reason="GAP_REJECT_RETRY",
            )
            log.warning(
                "gap_reject_delete FloodWait chat=%s msg=%s wait=%ss; retry queued for %s",
                chat_id, message_id, wait, retry_at,
            )
        except Exception as queue_exc:
            log.warning(
                "gap_reject_delete FloodWait chat=%s msg=%s wait=%ss; could not queue retry: %s: %s",
                chat_id, message_id, wait, type(queue_exc).__name__, queue_exc,
            )
    except Exception as exc:
        # Most common: MESSAGE_ID_INVALID / message already deleted by an
        # admin. Not actionable, but log at INFO so suppression is visible.
        log.info(
            "gap_reject_delete failed chat=%s msg=%s: %s: %s",
            chat_id, message_id, type(exc).__name__, exc,
        )


async def _is_gap_post(chat_id: int, message_id: int) -> bool:
    """True if ``message_id`` is an accepted /setgap post (history entry or
    legacy reference) — only those need /setdelay deletion marks. A post is
    accepted before its /setdelay job is even queued, so this read cannot
    miss one. Errors -> True (mark anyway; marking is harmless)."""
    try:
        return await settings_db.find_one(
            {"chat_id": chat_id, "$or": [
                {"gap_history.msg_id": message_id},
                {"last_gap_post_msg_id": message_id},
            ]},
            {"_id": 1},
        ) is not None
    except Exception:
        return True


async def _gap_mark_bot_delete(chat_id: int, message_id: int, state: str) -> None:
    """Record, on the accepted-post history entry, what the /setdelay worker
    did to the post (``BOT_DELETE_PENDING`` before its delete call,
    ``BOT_DELETE_CONFIRMED`` once Telegram reported the deletion).

    Runs under the channel's gap lock: a gap decision either completes
    before the mark (and saw the post still present) or starts after it
    (and sees the mark). A legacy reference that predates the history is
    materialised as an entry so it can carry the mark. Posts that are not
    accepted gap posts are not touched. Best-effort; never raises.
    """
    fields: Dict[str, Any] = {"gap_history.$.bot_delete": state}
    if state == BOT_DELETE_CONFIRMED:
        fields["gap_history.$.bot_deleted_at"] = _now()
    try:
        async with _gap_lock(chat_id):
            result = await settings_db.update_one(
                {"chat_id": chat_id, "gap_history.msg_id": message_id},
                {"$set": fields},
            )
            if getattr(result, "matched_count", 0):
                return
            legacy = await settings_db.find_one(
                {"chat_id": chat_id, "last_gap_post_msg_id": message_id},
                {"last_gap_post_at": 1},
            )
            post_at = _as_int((legacy or {}).get("last_gap_post_at"))
            if post_at is None:
                return
            entry: Dict[str, Any] = {
                "msg_id": message_id, "post_at": post_at, "bot_delete": state,
            }
            if state == BOT_DELETE_CONFIRMED:
                entry["bot_deleted_at"] = _now()
            await settings_db.update_one(
                {
                    "chat_id": chat_id,
                    "last_gap_post_msg_id": message_id,
                    "gap_history.msg_id": {"$ne": message_id},
                },
                {"$push": {"gap_history": {
                    "$each": [entry], "$slice": -GAP_HISTORY_LIMIT,
                }}},
            )
    except Exception as exc:
        log.warning(
            "Could not mark gap post %s/%s as %s: %s: %s",
            chat_id, message_id, state, type(exc).__name__, exc,
        )


async def _gap_clear_bot_delete(chat_id: int, message_id: int) -> None:
    """Remove the /setdelay worker's mark: its delete call was refused, or
    Telegram had nothing to delete because the post was already removed
    manually. Best-effort; never raises."""
    try:
        async with _gap_lock(chat_id):
            await settings_db.update_one(
                {"chat_id": chat_id, "gap_history.msg_id": message_id},
                {"$unset": {
                    "gap_history.$.bot_delete": "",
                    "gap_history.$.bot_deleted_at": "",
                }},
            )
    except Exception as exc:
        log.warning(
            "Could not clear gap mark for %s/%s: %s: %s",
            chat_id, message_id, type(exc).__name__, exc,
        )


async def _after_refused_delete(
    job_id: Any, chat_id: int, message_id: int, *, tracks_gap: bool,
    earlier_outcome_unknown: bool, update: Dict[str, Any],
) -> None:
    """Bookkeeping after Telegram REFUSED a /setdelay delete call (FloodWait
    or an RPC error), then reschedule the job with ``update``.

    The refused call deleted nothing, so whether the bot deleted the post
    depends only on EARLIER attempts:

    * none of them has an unknown outcome -> the bot deleted nothing: the
      pending mark is cleared (if the post disappears now, an admin did it)
      and so is the job's ``inflight`` flag;
    * an earlier attempt's outcome is unknown (network error, 5xx, crash)
      -> it may have deleted the post already: the pending mark stays (the
      post keeps counting for /setgap, its history entry is never dropped as
      "manually deleted") and ``inflight`` stays, so a later "0 deleted"
      answer is read as the bot's own deletion, not a manual one.

    Never raises.
    """
    update = dict(update)
    if not earlier_outcome_unknown:
        if tracks_gap:
            await _gap_clear_bot_delete(chat_id, message_id)
        update["$unset"] = {"inflight": ""}
    with suppress(Exception):
        await jobs_db.update_one({"_id": job_id}, update)


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
    await _queue_delete_at(
        chat_id, message_id,
        delete_at=int(original_post_time) + int(delay),
        original_post_time=original_post_time,
    )


async def _queue_delete_at(
    chat_id: int, message_id: int, *, delete_at: int,
    original_post_time: int, reason: Optional[str] = None,
) -> None:
    """Insert a deletion job due at ``delete_at`` (never overwrites one).

    Shared by /setdelay (_queue_delete) and by the gap engine, which
    queues a retry when deleting a rejected post hits FloodWait. The
    document layout lives in database.deletion_job_document.
    """
    await jobs_db.update_one(
        {"chat_id": chat_id, "message_id": message_id},
        {"$setOnInsert": deletion_job_document(
            chat_id, message_id,
            delete_at=delete_at,
            original_post_time=original_post_time,
            reason=reason,
        )},
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
                    # ``delete_at`` MUST be projected: the atomic claim below
                    # matches on it. Without it the claim filter became
                    # {"delete_at": None}, never matched, and no job was
                    # ever deleted.
                    {"chat_id": 1, "message_id": 1, "attempts": 1,
                     "delete_at": 1, "reason": 1, "inflight": 1},
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
                    # ``inflight`` = a delete call was sent and its outcome
                    # is not known yet. It is cleared once Telegram answers
                    # definitively, so if it is still set at the next claim
                    # (network error, crash) that attempt may have deleted
                    # the post already.
                    {"$set": {"delete_at": claim_until, "inflight": True}},
                    projection={"chat_id": 1, "message_id": 1, "inflight": 1},
                    return_document=ReturnDocument.BEFORE,
                )
                if claimed is None:
                    # Another worker (or a retry path) already moved
                    # delete_at — skip this round.
                    continue
                earlier_outcome_unknown = bool(claimed.get("inflight"))

                reason = job.get("reason") or "SETDELAY_EXPIRED"
                # Only /setdelay deletions of accepted /setgap posts are
                # reported to the gap engine (retry jobs delete rejected
                # posts, which are never in the accepted-post history).
                tracks_gap = (
                    reason == "SETDELAY_EXPIRED"
                    and await _is_gap_post(chat_id, message_id)
                )
                if tracks_gap:
                    await _gap_mark_bot_delete(chat_id, message_id, BOT_DELETE_PENDING)
                try:
                    deleted_count = await app.delete_messages(chat_id, message_id)
                    # Structured deletion-reason log — SETDELAY_EXPIRED
                    # (or GAP_REJECT_RETRY / ANON_REJECT_RETRY for a
                    # rejected post whose first delete hit FloodWait).
                    # This identifies WHICH component deleted the message
                    # and WHY, so operators can distinguish gap-reject
                    # deletions from delay-expiry deletions in the logs.
                    log.info(
                        "deletion reason=%s chat=%s msg=%s deleted=%s",
                        reason, chat_id, message_id, deleted_count,
                    )
                    # Success: remove the temporary job immediately.
                    with suppress(Exception):
                        await jobs_db.delete_one({"_id": job_id})
                    if tracks_gap and type(deleted_count) is int:
                        if deleted_count > 0 or earlier_outcome_unknown:
                            # Telegram confirmed the bot's deletion — or had
                            # nothing left after an earlier attempt of ours
                            # whose outcome was unknown.
                            await _gap_mark_bot_delete(
                                chat_id, message_id, BOT_DELETE_CONFIRMED,
                            )
                        else:
                            # Nothing to delete and no earlier attempt of
                            # ours: an admin removed the post manually.
                            await _gap_clear_bot_delete(chat_id, message_id)
                    # A non-int answer is not a confirmation: the pending
                    # mark stays (the post keeps counting for the gap).

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
                    # Telegram refused THIS call, so it deleted nothing. If an
                    # earlier attempt's outcome is unknown, that attempt may
                    # still have deleted the post: keep the pending mark (the
                    # post keeps counting for /setgap) and keep ``inflight``
                    # so the next answer is interpreted correctly.
                    await _after_refused_delete(
                        job_id, chat_id, message_id,
                        tracks_gap=tracks_gap,
                        earlier_outcome_unknown=earlier_outcome_unknown,
                        update={"$set": {"delete_at": retry_at}},
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
                    # A Telegram error answer (4xx) means THIS call was
                    # refused: it deleted nothing. Network errors, 5xx and
                    # anything else leave the outcome unknown, so the
                    # pending mark and the job's ``inflight`` flag stay.
                    refused = isinstance(exc, RPCError) and not isinstance(
                        exc, (InternalServerError, ServiceUnavailable),
                    )

                    # Stop retrying after a reasonable cap. Permanent
                    # failures are also reaped by the TTL index.
                    if attempts >= 10:
                        log.warning(
                            "Channel auto-delete retry exhausted for %s/%s after %s attempts: %s",
                            chat_id, message_id, attempts, error_name,
                        )
                        if refused and tracks_gap and not earlier_outcome_unknown:
                            await _gap_clear_bot_delete(chat_id, message_id)
                        with suppress(Exception):
                            await jobs_db.delete_one({"_id": job_id})
                    else:
                        retry_at = int(time.time()) + min(60, 2 ** attempts)
                        retry_update: Dict[str, Any] = {
                            "$set": {"delete_at": retry_at},
                            "$inc": {"attempts": 1},
                        }
                        if refused:
                            # Same rule as FloodWait: a refusal clears nothing
                            # that an earlier unknown outcome left behind.
                            await _after_refused_delete(
                                job_id, chat_id, message_id,
                                tracks_gap=tracks_gap,
                                earlier_outcome_unknown=earlier_outcome_unknown,
                                update=retry_update,
                            )
                        else:
                            with suppress(Exception):
                                await jobs_db.update_one({"_id": job_id}, retry_update)

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
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETDELAY_INVALID")
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
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETDELAY_OFF")
        return

    seconds = _parse_duration(value)
    if seconds is None:
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETDELAY_INVALID")
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
    await _delete_command_message(message, reason="COMMAND_CLEANUP_SETDELAY_SUCCESS")

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
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETGAP_INVALID")
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
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETGAP_OFF")
        return

    seconds = _parse_duration(value)
    if seconds is None:
        await _delete_command_message(message, reason="COMMAND_CLEANUP_SETGAP_INVALID")
        with suppress(Exception):
            await message.reply_text(INVALID_TEXT_GAP)
        return

    # Fetch current settings BEFORE the update to detect state change
    # Enable / change the duration atomically. Re-running /setgap with the
    # SAME duration changes nothing: the running timer and the accepted-
    # post history are kept. A fresh gap window starts only on first
    # activation or a real duration change — otherwise previously allowed
    # posts would retroactively invalidate new posts under the new value.
    # (See database.set_gap_duration.)
    status, old_gap = await set_gap_duration(message.chat.id, seconds)

    # Delete the user's command immediately (channel-side auto-cleanup).
    await _delete_command_message(message, reason="COMMAND_CLEANUP_SETGAP_SUCCESS")

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
        if status == "ENABLED":
            # Was disabled, now enabled
            await log_setgap_change(
                message.chat, message.from_user, "ENABLED",
                gap_value=_format_duration(seconds),
                source="CHANNEL",
            )
        elif status == "UPDATED":
            # Was enabled with a different value, now updated
            await log_setgap_change(
                message.chat, message.from_user, "UPDATED",
                gap_value=_format_duration(seconds),
                old_value=_format_duration(old_gap),
                source="CHANNEL",
            )
        # NO_CHANGE (same duration) → no state change, no log


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

        # /SETANONYMOUS COORDINATION — the anonymous filter (group -30) has
        # already run for this very update. A post it rejected is never
        # judged by the gap (so it can never become the reference) and
        # never queued for /setdelay; its deletion (or retry) is handled
        # by the filter. The atomic accept also refuses persisted
        # rejections, see _gap_allows_post.
        if anonymous_policy.was_rejected(message.chat.id, message.id):
            log.info(
                "gap/delay skipped chat=%s msg=%s: rejected by anonymous filter",
                message.chat.id, message.id,
            )
            return

        # /SETLINK COORDINATION — the link filter (group -40) has already
        # run for this very update. A post it rejected (prohibited link,
        # or any item of an album it condemned) is never judged by the
        # gap (so it can never become the reference) and never queued for
        # /setdelay; its deletion is handled by the link filter.
        if link_policy.was_rejected(message.chat.id, message.id):
            log.info(
                "gap/delay skipped chat=%s msg=%s: rejected by link filter",
                message.chat.id, message.id,
            )
            return

        setting = await settings_db.find_one({"chat_id": message.chat.id})
        if not setting:
            return

        # Even though the command handlers also check, this guard protects
        # the gap/delay side-effects when the bot lacks delete rights.
        if not await _bot_can_delete(message.chat.id):
            return

        is_command = (_is_setdelay_command(message)
                      or _is_setgap_command(message)
                      or _is_setlink_command(message))

        # Use the ORIGINAL Telegram post timestamp for gap calculations
        # (not time.time()) so the gap is measured from the actual post
        # time, not from when the bot happened to receive the update.
        post_time = _post_unix_time(message)

        # /setdelay delay for this post (None when /setdelay is off).
        delay = None if is_command else _configured_delay(setting)

        # Service messages (pins, title/photo changes ...) are not posts:
        # they never consume or hold a gap slot. (/setdelay still applies.)
        is_service = isinstance(getattr(message, "service", None), MessageServiceType)

        # Media album (several photos/videos sent together): every item is
        # a separate update; the album is judged as ONE post (_decide_gap).
        album_id = _media_group_id(message)

        # ---- GAP --------------------------------------------------------
        # Only one post is allowed per configured interval. Posts inside
        # the gap are deleted immediately. The gap is NOT extended by
        # those rejected posts, and a manually deleted accepted post no
        # longer counts (see _decide_gap / _resolve_stale_gap_reference).
        # An item of an album that was already judged follows that verdict
        # even if /setgap was switched off in the meantime.
        if not is_command and not is_service and (
            _enabled_gap(setting) is not None
            or _album_verdict(setting, album_id) is not None
        ):
            decision = await _decide_gap(message.chat.id, message, post_time)
            if decision == GAP_ANON_REJECTED:
                return  # handled by the anonymous filter; never queued
            if decision == GAP_REJECTED:
                await _delete_gap_rejected_post(
                    message.chat.id, message.id, post_time, _enabled_gap(setting),
                    media_group_id=album_id,
                )
                # No delay job is queued for a rejected post.
                return

        # ---- DELAY ------------------------------------------------------
        if delay is None:
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
