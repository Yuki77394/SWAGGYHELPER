#
# Copyright (C) 2021-2022 by Yuki77394@Github, < https://github.com/yuki77394 >.
# This file is part of < https://github.com/Yuki77394/SWAGGYHELPER > project,
# and is released under the "MIT License".
# Please see < https://github.com/Yuki77394/SWAGGYHELPER/blob/main/LICENSE >
# All rights reserved.

"""
Resolve a Telegram user from a numeric User ID (used by /createpost).

=====================================================================
 WHY A NUMERIC ID IS NOT ALWAYS ENOUGH
=====================================================================

The MTProto API addresses a user as ``(user_id, access_hash)``. Telegram's
peer documentation (https://core.telegram.org/api/peers) says:

    "if you have only a user/channel/supergroup ID without any kind of
     access hash, you cannot interact with that peer."

Kurigram's ``get_users(<int>)`` (2.2.25, methods/advanced/resolve_peer.py)
therefore:

  1. looks the ID up in the client's local peer cache. The cache is filled
     from updates and API responses that carry the user, e.g. the user sent
     the bot a message or appeared in a member list;
  2. on a miss, calls ``users.getUsers`` with ``access_hash=0``: the "zero
     access hash" that bots must use when they hold no access hash;
  3. raises ``PeerIdInvalid`` (chained from ``KeyError('ID not found: …')``)
     if Telegram's answer still contains no usable user. Telegram answers
     that way both for a user this bot cannot see and for an ID that
     belongs to nobody, so a bot cannot tell those two cases apart.

This bot runs ``Client(in_memory=True)`` (core/bot.py), so the local cache
only holds peers seen since the last process start. Every restart or
redeploy empties it.

=====================================================================
 WHAT THIS MODULE DOES
=====================================================================

  * ``parse_user_id``: strict validation of the typed ID (ASCII digits
    only, 1 … MAX_USER_ID). ``int()`` alone also accepts "1_000", "+5",
    "٣٣٣" and other spellings that are not Telegram IDs.
  * ``resolve_user``: Kurigram's standard path above, plus one more
    supported source of access hashes, the administrator list of the
    destination chat. The wizard verified the bot is an admin there, so it
    is a common chat; Telegram returns full user objects for its
    participants and Kurigram caches their access hashes.
  * ``ensure_user_peer``: the same guarantee for the final send. The
    caption's ``tg://user?id=`` mention and the profile button both make
    Kurigram resolve the user's peer while it serializes the request.
  * ``UserResolutionError``: says which failure happened, so "valid ID
    this bot cannot access", "deleted account" and "Telegram RPC error"
    get different, actionable messages instead of "invalid ID".
  * ``log_resolution_failure``: diagnostic logging of the operation,
    target ID, exception class, Telegram error and context, with
    credentials redacted.

Access hashes are never fabricated, guessed, or persisted and reused
across bot sessions (Telegram: access hashes "may not be reused across
different accounts or different login/auth sessions of the same
account", and ``in_memory=True`` starts a new bot authorization on every
restart).
"""

import re
import traceback
from contextlib import suppress
from typing import Any, Mapping, Optional

import pyrogram
from pyrogram import enums
from pyrogram import utils as _pyrogram_utils
from pyrogram.errors import FloodWait, PeerIdInvalid, RPCError, UserIdInvalid

import config
from SWAGGYMUSIC.logging import LOGGER

log = LOGGER(__name__)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

# Kurigram accepts user IDs in 1 … MAX_USER_ID (2**40 - 1 in 2.2.25) and its
# get_peer_type() raises ValueError outside that range. Reuse the library's
# own bound so validation and resolution can never disagree.
MAX_USER_ID: int = int(getattr(_pyrogram_utils, "MAX_USER_ID", (1 << 40) - 1))

# ASCII digits only: ``\d`` would also match non-ASCII digits such as "٣".
_ASCII_DIGITS = re.compile(r"[0-9]+")
_MAX_INPUT_LENGTH = 32


class InvalidUserIdInput(ValueError):
    """The text the user typed is not a syntactically valid Telegram user ID."""

    EMPTY = "empty"
    NOT_NUMERIC = "not_numeric"
    NEGATIVE = "negative"          # looks like a group / channel ID
    OUT_OF_RANGE = "out_of_range"  # 0, or larger than MAX_USER_ID

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_user_id(text: Optional[str]) -> int:
    """Return the user ID in ``text`` or raise :class:`InvalidUserIdInput`.

    Surrounding whitespace is ignored. Anything other than plain ASCII
    digits is rejected, as is any value outside 1 … MAX_USER_ID.
    """
    value = (text or "").strip()
    if not value:
        raise InvalidUserIdInput(InvalidUserIdInput.EMPTY)
    if value.startswith("-") and _ASCII_DIGITS.fullmatch(value[1:]):
        raise InvalidUserIdInput(InvalidUserIdInput.NEGATIVE)
    if not _ASCII_DIGITS.fullmatch(value):
        raise InvalidUserIdInput(InvalidUserIdInput.NOT_NUMERIC)
    if len(value) > _MAX_INPUT_LENGTH:
        raise InvalidUserIdInput(InvalidUserIdInput.OUT_OF_RANGE)
    user_id = int(value)
    if not 1 <= user_id <= MAX_USER_ID:
        raise InvalidUserIdInput(InvalidUserIdInput.OUT_OF_RANGE)
    return user_id


# ---------------------------------------------------------------------------
# Resolution errors
# ---------------------------------------------------------------------------

# Errors meaning "this bot cannot identify that user from the ID alone":
# either no such user exists, or the bot holds no access hash for them and
# Telegram did not return them for the zero access hash.
_UNRESOLVABLE_ERRORS = (PeerIdInvalid, UserIdInvalid)


class UserResolutionError(Exception):
    """Resolving a user ID failed. ``kind`` says why."""

    UNRESOLVABLE = "unresolvable"  # valid ID, but no user returned to this bot
    DELETED = "deleted"            # Telegram returned a deleted account
    RPC_ERROR = "rpc_error"        # any other Telegram RPC error
    UNEXPECTED = "unexpected"      # not a Telegram error (bug, network, ...)

    def __init__(self, kind: str, user_id: int, error: Optional[BaseException] = None):
        super().__init__(f"{kind} (user_id={user_id})")
        self.kind = kind
        self.user_id = user_id
        self.error = error

    @property
    def rpc_id(self) -> Optional[str]:
        """Telegram's error identifier, e.g. ``FLOOD_WAIT_X`` (RPC errors only)."""
        if isinstance(self.error, RPCError):
            return getattr(self.error, "ID", None) or getattr(self.error, "NAME", None)
        return None

    @property
    def flood_wait_seconds(self) -> Optional[int]:
        if isinstance(self.error, FloodWait):
            try:
                return int(self.error.value)
            except (TypeError, ValueError):
                return None
        return None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

_SECRET_CONFIG_NAMES = (
    "BOT_TOKEN",
    "API_HASH",
    "MONGO_DB_URI",
    "STRING_SESSION",
    "HEROKU_API_KEY",
)
_BOT_TOKEN_SHAPE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")


def _redact(text: Any) -> str:
    """Remove credentials from free text before it is logged."""
    text = str(text)
    for name in _SECRET_CONFIG_NAMES:
        secret = getattr(config, name, None)
        if isinstance(secret, str) and len(secret) >= 8:
            text = text.replace(secret, f"[{name} redacted]")
    return _BOT_TOKEN_SHAPE.sub("[bot token redacted]", text)


def _error_origin(exc: BaseException) -> Optional[str]:
    if isinstance(exc, PeerIdInvalid) and isinstance(exc.__cause__, KeyError):
        # resolve_peer() builds this error itself after Telegram's answer
        # contained no usable user; it is not an error Telegram returned.
        return (
            "kurigram resolve_peer: no access hash in the local peer cache and "
            "Telegram's users.getUsers(access_hash=0) answer had no usable user"
        )
    if isinstance(exc, RPCError):
        return "telegram rpc"
    if isinstance(exc, IndexError):
        return "kurigram get_users: Telegram answered users.getUsers with an empty vector"
    return None


def _peer_cache_kind(client: Any) -> str:
    in_memory = getattr(client, "in_memory", None)
    if in_memory is True:
        return "in-memory (emptied on every restart)"
    if in_memory is False:
        return "persistent"
    return "unknown"


def log_resolution_failure(
    client: Any,
    operation: str,
    user_id: Any,
    exc: BaseException,
    context: Optional[Mapping[str, Any]] = None,
    *,
    unexpected: bool = False,
) -> None:
    """Log one failed user-resolution operation without credentials.

    Records the operation, the target user ID, the exception class, the
    Telegram RPC error (code, ID and message) and non-sensitive context
    such as the requesting user, the destination chat and the wizard step.
    """
    fields = {"operation": operation, "target_user_id": user_id}
    for key, value in (context or {}).items():
        if value is not None:
            fields[key] = value
    fields["error_class"] = type(exc).__name__
    if isinstance(exc, RPCError):
        rpc_id = getattr(exc, "ID", None) or getattr(exc, "NAME", None)
        fields["rpc_error"] = f"{getattr(exc, 'CODE', '?')} {rpc_id}"
    origin = _error_origin(exc)
    if origin:
        fields["origin"] = origin
    fields["message"] = _redact(exc)
    fields["peer_cache"] = _peer_cache_kind(client)
    fields["kurigram"] = getattr(pyrogram, "__version__", "?")

    line = " ".join(f"{key}={value!r}" for key, value in fields.items())
    if unexpected:
        trace = _redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        log.error("User resolution failed unexpectedly: %s\n%s", line, trace)
    else:
        log.warning("User resolution failed: %s", line)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_ADMIN_SCAN_LIMIT = 200  # upper bound on admins scanned (one getParticipants page)


def _context(context: Optional[Mapping[str, Any]], chat_id: Optional[int]) -> dict:
    ctx = dict(context or {})
    ctx.setdefault("chat_id", chat_id)
    return ctx


async def _find_chat_admin(client: Any, chat_id: int, user_id: int, ctx: Mapping[str, Any]):
    """Return ``user_id``'s User from ``chat_id``'s admin list, or None.

    ``channels.getParticipants`` returns full user objects. Kurigram's
    ``invoke()`` stores their access hashes in the peer cache, so the
    user's peer resolves locally afterwards.
    """
    operation = "get_chat_members(administrators)"
    members = None
    try:
        members = client.get_chat_members(
            chat_id,
            filter=enums.ChatMembersFilter.ADMINISTRATORS,
            limit=_ADMIN_SCAN_LIMIT,
        )
        async for member in members:
            member_user = getattr(member, "user", None)
            if member_user is not None and getattr(member_user, "id", None) == user_id:
                log.info(
                    "User resolution: target_user_id=%s found in the administrators "
                    "of chat_id=%s (access hash obtained from the member list).",
                    user_id, chat_id,
                )
                return member_user
    except RPCError as exc:
        log_resolution_failure(client, operation, user_id, exc, ctx)
        return None
    except Exception as exc:  # noqa: BLE001 - fallback must never break the wizard
        log_resolution_failure(client, operation, user_id, exc, ctx, unexpected=True)
        return None
    finally:
        aclose = getattr(members, "aclose", None)
        if aclose is not None:
            with suppress(Exception):
                await aclose()

    log.info(
        "User resolution: target_user_id=%s is not an administrator of chat_id=%s.",
        user_id, chat_id,
    )
    return None


def _checked(user: Any, user_id: int) -> Any:
    if getattr(user, "is_deleted", False):
        log.info("User resolution: target_user_id=%s is a deleted account.", user_id)
        raise UserResolutionError(UserResolutionError.DELETED, user_id)
    return user


async def resolve_user(
    client: Any,
    user_id: int,
    *,
    chat_id: Optional[int] = None,
    context: Optional[Mapping[str, Any]] = None,
):
    """Return the ``pyrogram.types.User`` for ``user_id``.

    Order:
      1. ``client.get_users(user_id)``: Kurigram's local peer cache, then
         Telegram's ``users.getUsers`` with the bot zero access hash.
      2. If that cannot identify the user and ``chat_id`` is given: the
         administrator list of ``chat_id``.

    Raises :class:`UserResolutionError` if neither source yields the user.
    """
    ctx = _context(context, chat_id)
    operation = "get_users"
    first_error: Optional[BaseException] = None

    try:
        user = await client.get_users(user_id)
    except _UNRESOLVABLE_ERRORS as exc:
        log_resolution_failure(client, operation, user_id, exc, ctx)
        first_error = exc
    except RPCError as exc:
        log_resolution_failure(client, operation, user_id, exc, ctx)
        raise UserResolutionError(UserResolutionError.RPC_ERROR, user_id, exc) from exc
    except IndexError as exc:
        # Kurigram 2.2.25 get_users() indexes [0] into Telegram's answer, so an
        # empty answer surfaces as IndexError.
        log_resolution_failure(client, operation, user_id, exc, ctx)
        first_error = exc
    except Exception as exc:  # noqa: BLE001
        log_resolution_failure(client, operation, user_id, exc, ctx, unexpected=True)
        raise UserResolutionError(UserResolutionError.UNEXPECTED, user_id, exc) from exc
    else:
        if user is not None:
            return _checked(user, user_id)
        # userEmpty / no user in Telegram's answer (newer Kurigram returns None).
        log.warning(
            "User resolution failed: operation='get_users' target_user_id=%s "
            "result=None (Telegram returned no user object) context=%r",
            user_id, ctx,
        )

    if chat_id is not None:
        admin = await _find_chat_admin(client, chat_id, user_id, ctx)
        if admin is not None:
            return _checked(admin, user_id)

    raise UserResolutionError(UserResolutionError.UNRESOLVABLE, user_id, first_error)


async def ensure_user_peer(
    client: Any,
    user_id: int,
    *,
    chat_id: Optional[int] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> None:
    """Make sure Kurigram can resolve ``user_id``'s peer.

    Needed before sending a message whose caption mentions the user
    (``tg://user?id=``) or whose keyboard has an ``InlineKeyboardButton(
    user_id=...)``: Kurigram resolves the peer for both while serializing
    the request. If the button cannot be resolved the send raises
    ``PeerIdInvalid``, and an unresolved mention is silently dropped.

    Uses the same sources as :func:`resolve_user`; a peer that is already
    cached costs no network request. Raises :class:`UserResolutionError`.
    """
    ctx = _context(context, chat_id)
    operation = "resolve_peer"
    first_error: Optional[BaseException] = None

    try:
        await client.resolve_peer(user_id)
        return
    except _UNRESOLVABLE_ERRORS as exc:
        log_resolution_failure(client, operation, user_id, exc, ctx)
        first_error = exc
    except RPCError as exc:
        log_resolution_failure(client, operation, user_id, exc, ctx)
        raise UserResolutionError(UserResolutionError.RPC_ERROR, user_id, exc) from exc
    except Exception as exc:  # noqa: BLE001
        log_resolution_failure(client, operation, user_id, exc, ctx, unexpected=True)
        raise UserResolutionError(UserResolutionError.UNEXPECTED, user_id, exc) from exc

    if chat_id is not None and await _find_chat_admin(client, chat_id, user_id, ctx) is not None:
        try:
            await client.resolve_peer(user_id)
            return
        except RPCError as exc:
            log_resolution_failure(client, "resolve_peer (after admin list)", user_id, exc, ctx)
            first_error = exc

    raise UserResolutionError(UserResolutionError.UNRESOLVABLE, user_id, first_error)


__all__ = [
    "MAX_USER_ID",
    "InvalidUserIdInput",
    "UserResolutionError",
    "parse_user_id",
    "resolve_user",
    "ensure_user_peer",
    "log_resolution_failure",
]
