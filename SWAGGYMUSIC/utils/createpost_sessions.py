#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
Persistent session storage for the /createpost wizard.

=====================================================================
 WHY PERSISTENCE?
=====================================================================

Pre-Part-3, /createpost sessions lived in an in-memory dict. A bot
restart (deploy, crash, Heroku dyno recycle) lost every in-flight
session — the user saw "This setup has expired" on their next message
and had to restart the wizard from scratch, losing the post type, chat
ID, format, user ID, colour, and any text they had typed.

This module persists the minimum session state to MongoDB so a restart
can recover it. The user just continues typing as if nothing happened.

=====================================================================
 SCHEMA
=====================================================================

  mongodb.createpost_sessions  — one document per active session
  ------------------------------------------------------------
  {
    _id:              ObjectId,
    user_id:          <int>,                # UNIQUE index — one session per user
    step:             <str>,                # current wizard step
    post_type:        <str|None>,           # "hidden" | "normal" | None
    chat_id:          <int|None>,           # target channel/chat ID
    format:           <str|None>,           # "1" | "2" | None  (normal post)
    target_user_id:   <int|None>,           # user ID for the mention (normal post)
    target_user_name: <str|None>,           # cached display name (normal post)
    position:         <str|None>,           # "above" | "below" | None  (hidden)
    color:            <str|None>,           # "blue"|"green"|"red"|"default"|None
    hidden_text:      <str|None>,           # the visible text (hidden post)
    video_url:        <str|None>,           # the MP4 URL (hidden post)
    photo_ref:        <str|None>,           # photo file_id OR URL (normal post)
    created_at:       <BSON Date UTC>,      # when the session started
    expires_at:       <BSON Date UTC>       # TTL — session auto-deletes after 1h
  }

  Required indexes:
    - {user_id: 1}  UNIQUE   (one session per user)
    - {expires_at: 1}  TTL   (expireAfterSeconds=0 — auto-cleanup)

=====================================================================
 WHAT IS NOT PERSISTED
=====================================================================

  - Message entities (bold/italic/links from the user's text message).
    Kurigram's MessageEntity objects are not trivially JSON-serializable,
    and losing them on restart only means the user's bold/italic is
    dropped — the text itself is preserved. This is an acceptable
    trade-off vs. the complexity of serializing/deserializing raw MTProto
    objects. The in-memory session still keeps entities for the CURRENT
    run; only restart-loses them.

  - The Pyrogram User object for the mention target. We persist only
    the user_id + cached display name; on recovery we rebuild the
    mention from those. If the user has changed their name in the
    meantime, the post will use the new name (which is correct).

  - Photo file_id stability: Telegram file_ids are stable for the bot
    that received them, so persisting file_id is safe. If Telegram ever
    invalidates the file_id (extremely rare), the final send will fail
    with a clear error and the user can re-send the photo.

=====================================================================
 CONCURRENCY / ISOLATION
=====================================================================

  - One session per user (UNIQUE index on user_id). A user cannot have
    two concurrent /createpost sessions.
  - Sessions are keyed by user_id, so user A can never see or modify
    user B's session.
  - All updates filter on user_id, so a cross-user update is impossible
    even if a callback_data were crafted.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from SWAGGYMUSIC.core.mongo import mongodb
from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)

# ---------------------------------------------------------------------------
# Collection + indexes
# ---------------------------------------------------------------------------

sessions_db = mongodb.createpost_sessions

# Session lifetime — 1 hour of inactivity is a reasonable default. The
# TTL index reaps expired sessions automatically. The wizard is short
# enough that 1h is generous; if a user walks away mid-session, the
# session is cleaned up without operator intervention.
SESSION_TTL_SECONDS = 60 * 60

# Index names
_USER_ID_IDX = "user_id_1"
_EXPIRES_AT_IDX = "expires_at_1"

_indexes_ready = False


async def ensure_session_indexes() -> None:
    """Create the required indexes on createpost_sessions.

    Idempotent — safe to call on every boot. Called from
    ``ensure_channel_indexes`` (database.py) so the bot's startup index
    bootstrap covers this collection too.

    Essential indexes (startup fails if these cannot be established):
        - {user_id: 1}  UNIQUE   (one session per user)
    Safety-net indexes:
        - {expires_at: 1}  TTL   (auto-cleanup; best-effort)
    """
    global _indexes_ready
    if _indexes_ready:
        return

    try:
        await sessions_db.create_index(
            [("user_id", ASCENDING)], name=_USER_ID_IDX, unique=True,
        )
    except DuplicateKeyError:
        pass
    except OperationFailure as exc:
        code = getattr(exc, "code", None)
        # 85 (IndexOptionsConflict) / 86 (IndexKeySpecsConflict) —
        # an existing index with the same name but different spec.
        # Drop and recreate so the UNIQUE constraint is enforced.
        if code in (85, 86):
            log.warning(
                "createpost_sessions: index %s has conflicting spec (code=%s); dropping and recreating.",
                _USER_ID_IDX, code,
            )
            try:
                await sessions_db.drop_index(_USER_ID_IDX)
                await sessions_db.create_index(
                    [("user_id", ASCENDING)], name=_USER_ID_IDX, unique=True,
                )
            except Exception as exc2:
                log.error(
                    "createpost_sessions: could not recreate unique index %s: %s: %s",
                    _USER_ID_IDX, type(exc2).__name__, exc2,
                )
                raise
        else:
            log.error(
                "createpost_sessions: could not create unique index %s (code=%s): %s",
                _USER_ID_IDX, code, exc,
            )
            raise

    try:
        await sessions_db.create_index(
            [("expires_at", ASCENDING)],
            name=_EXPIRES_AT_IDX,
            expireAfterSeconds=0,
        )
    except Exception as exc:
        # TTL index is best-effort — if creation fails, expired
        # sessions will accumulate but the wizard still works. Log and
        # continue.
        log.warning(
            "createpost_sessions: could not create TTL index %s (%s: %s) "
            "— expired sessions will not be auto-reaped.",
            _EXPIRES_AT_IDX, type(exc).__name__, exc,
        )

    _indexes_ready = True


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry() -> datetime:
    return _now() + timedelta(seconds=SESSION_TTL_SECONDS)


# ---------------------------------------------------------------------------
# CRUD — all operations filter on user_id for isolation
# ---------------------------------------------------------------------------

async def load_session(user_id: int) -> Optional[Dict[str, Any]]:
    """Load the active session for ``user_id``, or None if expired/missing.

    Also refreshes ``expires_at`` so an active session stays alive
    while the user is interacting with it. (The TTL is a timeout for
    INACTIVITY, not a hard lifetime cap.)
    """
    await ensure_session_indexes()
    try:
        doc = await sessions_db.find_one_and_update(
            {"user_id": int(user_id)},
            {
                "$set": {"expires_at": _expiry()},
                # ``$currentDate`` would also work but we want a
                # timezone-aware UTC datetime, not a BSON Date with
                # server-local semantics.
            },
            return_document=True,  # ReturnDocument.AFTER
        )
        return doc
    except Exception as exc:
        log.warning(
            "createpost_sessions: load_session(%s) failed: %s: %s",
            user_id, type(exc).__name__, exc,
        )
        return None


async def save_session(user_id: int, data: Dict[str, Any]) -> None:
    """Upsert the session for ``user_id`` with the given data.

    Merges ``data`` into the existing session document (or creates a
    new one). Always refreshes ``expires_at``. Filters on ``user_id``
    so cross-user writes are impossible.
    """
    await ensure_session_indexes()
    if not isinstance(data, dict):
        return
    # Strip None values so we don't store explicit nulls for optional
    # fields — keeps the document tidy and avoids confusing truthiness
    # checks in the wizard.
    set_fields = {k: v for k, v in data.items() if v is not None}
    try:
        await sessions_db.update_one(
            {"user_id": int(user_id)},
            {
                "$set": {
                    "user_id": int(user_id),
                    "expires_at": _expiry(),
                    **set_fields,
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
    except Exception as exc:
        log.warning(
            "createpost_sessions: save_session(%s) failed: %s: %s",
            user_id, type(exc).__name__, exc,
        )


async def delete_session(user_id: int) -> None:
    """Delete the session for ``user_id``. No-op if none exists."""
    await ensure_session_indexes()
    try:
        await sessions_db.delete_one({"user_id": int(user_id)})
    except Exception as exc:
        log.warning(
            "createpost_sessions: delete_session(%s) failed: %s: %s",
            user_id, type(exc).__name__, exc,
        )


async def count_active_sessions() -> int:
    """Return the number of active (non-expired) sessions.

    Best-effort — used for diagnostics. Returns 0 on any error.
    """
    await ensure_session_indexes()
    try:
        return await sessions_db.count_documents({"expires_at": {"$gt": _now()}})
    except Exception:
        return 0


__all__ = [
    "sessions_db",
    "SESSION_TTL_SECONDS",
    "ensure_session_indexes",
    "load_session",
    "save_session",
    "delete_session",
    "count_active_sessions",
]
