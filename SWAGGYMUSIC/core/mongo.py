#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
MongoDB connection bootstrap.

This module is imported very early during package init (via
``SWAGGYMUSIC.utils.database``), so it MUST stay side-effect-safe: it
creates the client and the default-database handle, but it does NOT
perform any I/O at import time. The actual connectivity check (ping +
server selection) is deferred to :func:`verify_mongo_connection`, which
must be awaited from :func:`SWAGGYMUSIC.__main__.init` before the bot
starts serving traffic.

Why a separate verify step?
    ``AsyncIOMotorClient(...)`` returns immediately and does NOT open a
    connection — it lazily connects on the first operation. Treating the
    client object as proof of connectivity (the previous behaviour) let
    the bot boot with a completely wrong / unreachable MONGO_DB_URI and
    only fail later, deep inside a handler, with a confusing
    ``ServerSelectionTimeoutError``. The explicit ping surfaces that
    failure at startup with a clean error message and no credentials.

No credentials in errors:
    We never put ``MONGO_DB_URI`` (which contains the password) into a
    log line or exception string. Error messages name only the failure
    type (e.g. ``ServerSelectionTimeoutError``) and the server-side
    error text — never the URI.
"""

import asyncio
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_DB_URI

from ..logging import LOGGER

log = LOGGER(__name__)

# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------
#
# ``serverSelectionTimeoutMS`` controls how long Motor waits for a
# suitable server during the first operation (and during any subsequent
# operation that needs to pick a server). The Motor default is 30s,
# which is far too long for a startup gate — the operator wants to see
# "DB unreachable" within a few seconds, not half a minute. We lower it
# to 10s for the client used by the bot. The connect timeout is also
# lowered so a single unreachable host does not stall the whole ping.
#
# ``retryWrites`` is left at the Motor default (true on most modern
# MongoDB topologies) — it improves resilience on replica sets and
# sharded clusters without affecting standalone servers.
#
# ``maxPoolSize`` is left at the Motor default (100). The bot's
# concurrency is bounded by the number of concurrent Telegram updates,
# which is far below 100.

_CONNECT_TIMEOUT_MS = 10_000        # 10 seconds per host connect attempt
_SERVER_SELECTION_TIMEOUT_MS = 10_000  # 10 seconds to find a suitable server


def _build_client(uri: Optional[str]) -> AsyncIOMotorClient:
    """Construct an ``AsyncIOMotorClient`` with safe timeouts.

    Kept as a separate function so it can be unit-tested without
    touching the module-level singleton.
    """
    return AsyncIOMotorClient(
        uri,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
    )


log.info("Creating MongoDB client (no I/O yet — connection is lazy).")

try:
    # ``mongodb`` is the default database handle (``Anon`` was the
    # historical name in this project; the name has no special meaning
    # — it is just whatever database is in the URI, or "Anon" as a
    # fallback). We keep the same handle name so the rest of the code
    # base does not need to change.
    _mongo_async_ = _build_client(MONGO_DB_URI)
    mongodb = _mongo_async_.Anon
except Exception as exc:
    # Client construction itself basically never fails for a bad URI —
    # it fails on first use. But if it does fail (e.g. dnspython
    # missing), surface a clean error WITHOUT the URI.
    log.error(
        "MongoDB client construction failed: %s: %s "
        "(check that motor / dnspython are installed and MONGO_DB_URI is set)",
        type(exc).__name__, exc,
    )
    raise


# ---------------------------------------------------------------------------
# Connectivity verification — MUST be awaited at startup
# ---------------------------------------------------------------------------

async def verify_mongo_connection(timeout_seconds: float = 15.0) -> None:
    """Ping the MongoDB server and fail loudly if unreachable.

    Must be awaited from :func:`SWAGGYMUSIC.__main__.init` BEFORE
    ``app.start()`` so the bot never begins serving traffic against a
    dead database.

    Behaviour:
        - On success: returns ``None`` silently (the info log line is
          already emitted by the caller).
        - On failure: raises :class:`RuntimeError` with a clean message
          that does NOT include the URI. The caller is expected to
          ``exit(1)`` (or otherwise abort) — there is no useful
          fallback if the DB is unreachable.

    The ``timeout_seconds`` argument is an outer asyncio.wait_for guard
    so we never block forever even if the underlying Motor ping hangs
    for some unexpected reason.
    """

    if not MONGO_DB_URI:
        # No URI configured — fail fast with a clean message.
        raise RuntimeError(
            "MONGO_DB_URI is not configured. Set it in the environment "
            "before starting the bot."
        )

    async def _ping() -> None:
        # ``admin`` database ping is the cheapest server-side round trip.
        await _mongo_async_.admin.command("ping")

    try:
        await asyncio.wait_for(_ping(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"MongoDB ping did not complete within {timeout_seconds:.0f}s — "
            "the server is unreachable or the network is partitioned. "
            "Check MONGO_DB_URI, network ACLs, and cluster state. "
            "(URI omitted from this message for security.)"
        )
    except Exception as exc:
        # Common cases: ServerSelectionTimeoutError, OperationFailure
        # (auth), ConnectionFailure. None of these include the URI in
        # their str(), but be defensive and never re-raise the URI.
        raise RuntimeError(
            f"MongoDB connection verification failed: "
            f"{type(exc).__name__}: {exc} "
            "(URI omitted from this message for security.)"
        )


__all__ = ["mongodb", "verify_mongo_connection"]
